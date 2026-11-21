"""Business validation, kept strictly separate from library diagnostics.

Why the split matters
---------------------
``cantools`` refuses to *build* a database when two signals overlap, a
signal spills past the end of its message, or a message id is duplicated
with conflicting definitions.  That is a library-level ``Error`` /
``ParseError``: it describes what the library could not model.  It is not
the same thing as "this frame is wrong for this ECU", and conflating the
two produces useless messages such as *"UnsupportedDatabaseFormatError"*
next to a perfectly readable raw frame.

CANLab therefore has three distinct diagnostic channels:

``library``
    What cantools said.  Rendered verbatim, in its own panel, labelled with
    the library version and the exact call that produced it.

``definition``
    CANLab's own static checks on a database: overlapping bits, out-of-frame
    signals, duplicate ids, multiplexing conflicts, zero scale, inverted
    limits.  Each finding carries the defining source line, the signal name,
    and the exact bit span, so it can be located in the file.

``frame``
    Per-frame business decisions: the DLC does not match the definition, a
    signal's raw bits sit outside its declared range, a needed multiplexer
    branch is not selected, a value cannot be encoded.  These are recorded
    in the receive trace with the rejected frame preserved (never dropped).

``UnsupportedDatabaseFormatError``, ``DecodeError`` and friends never leak
into ``definition`` or ``frame``, and business rejections never appear as
library errors.  The API returns all three channels side by side so a UI
can display the boundary instead of describing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .bits import (
    DbcDatabase,
    LayoutError,
    MessageDef,
    SignalDef,
    signal_bit_offsets,
)

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"


@dataclass
class Finding:
    code: str
    severity: str
    scope: str                 # "definition" | "frame"
    message: str
    detail: str
    location: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "scope": self.scope,
            "message": self.message,
            "detail": self.detail,
            "location": self.location,
        }


def _location(message: MessageDef, signal: SignalDef | None = None,
              extra: dict[str, Any] | None = None) -> dict[str, Any]:
    loc: dict[str, Any] = {
        "message": message.name,
        "frame_id": message.frame_id,
        "is_extended": message.is_extended,
        "message_line": message.line,
        "message_length": message.length,
    }
    if signal is not None:
        loc.update({
            "signal": signal.name,
            "signal_line": message.bookmarks.get(signal.name).line
            if signal.name in message.bookmarks else None,
            "start": signal.start,
            "length": signal.length,
            "byte_order": signal.byte_order,
            "multiplexer_signal": signal.multiplexer_signal,
            "multiplexer_ids": list(signal.multiplexer_ids),
        })
        try:
            offsets = signal_bit_offsets(signal)
            loc["bit_offsets"] = offsets
            if offsets:
                loc["first_bit"] = min(offsets)
                loc["last_bit"] = max(offsets)
        except LayoutError:
            pass
    if extra:
        loc.update(extra)
    return loc


def _sawtooth_span(sig: SignalDef) -> str:
    if sig.byte_order == "big_endian":
        return f"sawtooth {sig.start}..{sig.start - sig.length + 1} (Motorola, start is the MSB)"
    return f"sawtooth {sig.start}..{sig.start + sig.length - 1} (Intel, start is the LSB)"


def validate_database(db: DbcDatabase) -> list[Finding]:
    findings: list[Finding] = []

    # Duplicate frame ids.
    seen: dict[tuple[int, bool], MessageDef] = {}
    for msg in db.messages:
        key = (msg.frame_id, msg.is_extended)
        if key in seen:
            other = seen[key]
            findings.append(Finding(
                code="duplicate_frame_id",
                severity=SEVERITY_ERROR,
                scope="definition",
                message=(
                    f"frame 0x{msg.frame_id:X} is defined twice "
                    f"({other.name!r} at line {other.line}, "
                    f"{msg.name!r} at line {msg.line})"
                ),
                detail=(
                    "Two messages claim the same identifier and frame kind. "
                    "A receiver cannot know which layout to apply; pick one "
                    "definition per id or move one to a different id."
                ),
                location={
                    "frame_id": msg.frame_id,
                    "is_extended": msg.is_extended,
                    "definitions": [
                        {"message": other.name, "line": other.line,
                         "length": other.length,
                         "signals": [s.name for s in other.signals]},
                        {"message": msg.name, "line": msg.line,
                         "length": msg.length,
                         "signals": [s.name for s in msg.signals]},
                    ],
                },
            ))
        else:
            seen[key] = msg

    for msg in db.messages:
        if msg.length < 0 or msg.length > 64:
            findings.append(Finding(
                code="message_length_invalid",
                severity=SEVERITY_ERROR,
                scope="definition",
                message=(
                    f"message {msg.name!r} declares a length of {msg.length} "
                    f"bytes; CAN classic allows 0..8 and CAN FD 0..64"
                ),
                detail="Frame length must be between 0 and 64 bytes.",
                location=_location(msg),
            ))
        if not msg.signals:
            findings.append(Finding(
                code="message_without_signals",
                severity=SEVERITY_WARNING,
                scope="definition",
                message=f"message {msg.name!r} defines no signals",
                detail="The frame can still be received, but nothing in it "
                       "will be interpreted.",
                location=_location(msg),
            ))

        by_name: dict[str, list[SignalDef]] = {}
        for sig in msg.signals:
            by_name.setdefault(sig.name, []).append(sig)

            if sig.length <= 0:
                findings.append(Finding(
                    code="signal_zero_length",
                    severity=SEVERITY_ERROR,
                    scope="definition",
                    message=f"signal {sig.name!r} in {msg.name!r} has length "
                            f"{sig.length}",
                    detail="A signal must occupy at least one bit.",
                    location=_location(msg, sig),
                ))
                continue

            if sig.scale == 0:
                findings.append(Finding(
                    code="signal_zero_scale",
                    severity=SEVERITY_ERROR,
                    scope="definition",
                    message=f"signal {sig.name!r} in {msg.name!r} has scale 0",
                    detail=(
                        "Physical = raw * scale + offset cannot be inverted, "
                        "so a sender cannot compute a raw value and every "
                        "physical value would collapse to the offset."
                    ),
                    location=_location(msg, sig),
                ))

            if sig.minimum is not None and sig.maximum is not None \
                    and sig.minimum > sig.maximum:
                findings.append(Finding(
                    code="signal_limits_inverted",
                    severity=SEVERITY_WARNING,
                    scope="definition",
                    message=(
                        f"signal {sig.name!r} in {msg.name!r} declares "
                        f"minimum {sig.minimum} > maximum {sig.maximum}"
                    ),
                    detail="Range checks against this signal can never pass.",
                    location=_location(msg, sig),
                ))

            try:
                offsets = signal_bit_offsets(sig)
            except LayoutError as exc:
                findings.append(Finding(
                    code="signal_layout_error",
                    severity=SEVERITY_ERROR,
                    scope="definition",
                    message=f"signal {sig.name!r} cannot be placed: {exc}",
                    detail=_sawtooth_span(sig),
                    location=_location(msg, sig),
                ))
                continue

            over = [o for o in offsets if o >= 8 * msg.length]
            if over:
                findings.append(Finding(
                    code="signal_out_of_frame",
                    severity=SEVERITY_ERROR,
                    scope="definition",
                    message=(
                        f"signal {sig.name!r} in {msg.name!r} needs bit "
                        f"{max(offsets)} (byte {max(offsets) // 8}) but the "
                        f"message is only {msg.length} bytes"
                    ),
                    detail=(
                        f"{_sawtooth_span(sig)}; occupied linear bits "
                        f"{min(offsets)}..{max(offsets)}, frame capacity "
                        f"0..{8 * msg.length - 1}."
                    ),
                    location=_location(msg, sig),
                ))

            if sig.multiplexer_signal == sig.name:
                findings.append(Finding(
                    code="multiplexer_self_parent",
                    severity=SEVERITY_ERROR,
                    scope="definition",
                    message=(
                        f"signal {sig.name!r} in {msg.name!r} is its own "
                        f"multiplexer parent"
                    ),
                    detail="The selector would have to be known before it "
                           "can be read; the branch can never resolve.",
                    location=_location(msg, sig),
                ))

            if sig.multiplexer_signal and not any(
                other.name == sig.multiplexer_signal and other.is_multiplexer
                for other in msg.signals
            ):
                findings.append(Finding(
                    code="multiplexer_missing",
                    severity=SEVERITY_ERROR,
                    scope="definition",
                    message=(
                        f"signal {sig.name!r} in {msg.name!r} is gated by "
                        f"multiplexer {sig.multiplexer_signal!r}, which is "
                        f"not defined in this message"
                    ),
                    detail="Without the selector's definition the branch can "
                           "never be selected, so the signal is always not "
                           "applicable.",
                    location=_location(msg, sig),
                ))

            if not sig.multiplexer_signal and sig.multiplexer_ids:
                findings.append(Finding(
                    code="multiplexer_ids_without_parent",
                    severity=SEVERITY_ERROR,
                    scope="definition",
                    message=(
                        f"signal {sig.name!r} in {msg.name!r} lists selector "
                        f"values {list(sig.multiplexer_ids)} but is not "
                        f"attached to a multiplexer"
                    ),
                    detail="The ids are inert; the signal will always be "
                           "decoded as present.",
                    location=_location(msg, sig),
                ))

        # Duplicate signal names and overlapping bit spans.
        for name, group in by_name.items():
            if len(group) > 1:
                findings.append(Finding(
                    code="duplicate_signal_name",
                    severity=SEVERITY_ERROR,
                    scope="definition",
                    message=(
                        f"message {msg.name!r} defines signal {name!r} "
                        f"{len(group)} times"
                    ),
                    detail="Both definitions occupy the frame; a receiver "
                           "cannot address them separately by name.",
                    location=_location(msg, group[0], {
                        "occurrences": [
                            {"line": msg.bookmarks.get(name).line
                             if name in msg.bookmarks else None,
                             "start": s.start, "length": s.length}
                            for s in group
                        ],
                    }),
                ))

        findings.extend(_overlaps(msg))

    return findings


def _may_share_bits(a: SignalDef, b: SignalDef) -> bool:
    """True only for siblings that can never be present at the same time.

    Signals gated by the same multiplexer with disjoint selector values are
    mutually exclusive by construction, so sharing bits is legal (and is
    the normal way a DBC saves space).  Everything else -- including a
    multiplexer signal versus its own child, and two root signals -- is a
    conflict.  An unknown parent is never allowed to excuse an overlap.
    """
    if a.multiplexer_signal is None or b.multiplexer_signal is None:
        return False
    if a.multiplexer_signal != b.multiplexer_signal:
        return False
    return not (set(a.multiplexer_ids) & set(b.multiplexer_ids))


def _overlaps(msg: MessageDef) -> list[Finding]:
    findings: list[Finding] = []
    spans: list[tuple[SignalDef, set[int]]] = []
    for sig in msg.signals:
        try:
            spans.append((sig, set(signal_bit_offsets(sig))))
        except LayoutError:
            continue

    for i in range(len(spans)):
        a, abits = spans[i]
        for j in range(i + 1, len(spans)):
            b, bbits = spans[j]
            shared = abits & bbits
            if not shared:
                continue
            if _may_share_bits(a, b):
                continue
            findings.append(Finding(
                code="signals_overlap",
                severity=SEVERITY_ERROR,
                scope="definition",
                message=(
                    f"{a.name!r} and {b.name!r} in {msg.name!r} share "
                    f"{len(shared)} bit(s)"
                ),
                detail=(
                    f"{a.name}: {_sawtooth_span(a)}, linear bits "
                    f"{min(abits)}..{max(abits)}. "
                    f"{b.name}: {_sawtooth_span(b)}, linear bits "
                    f"{min(bbits)}..{max(bbits)}. Shared linear bits: "
                    f"{sorted(shared)}. Two signals cannot both own the same "
                    f"bit unless mutually exclusive multiplexer branches "
                    f"select them."
                ),
                location={
                    "message": msg.name,
                    "frame_id": msg.frame_id,
                    "message_line": msg.line,
                    "message_length": msg.length,
                    "shared_linear_bits": sorted(shared),
                    "a": _location(msg, a),
                    "b": _location(msg, b),
                },
            ))
    return findings


# ---------------------------------------------------------------------------
# Frame-level business checks
# ---------------------------------------------------------------------------

@dataclass
class FrameVerdict:
    accepted: bool
    findings: list[Finding]

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "findings": [f.to_dict() for f in self.findings],
        }


def check_frame(msg: MessageDef, payload: bytes,
                decoded: Any) -> FrameVerdict:
    """Business acceptance of a received frame against one definition."""
    findings: list[Finding] = []

    if len(payload) != msg.length:
        findings.append(Finding(
            code="frame_length_mismatch",
            severity=SEVERITY_ERROR,
            scope="frame",
            message=(
                f"frame has {len(payload)} bytes, definition expects "
                f"{msg.length}"
            ),
            detail=(
                f"{msg.name!r} is declared with {msg.length} bytes at line "
                f"{msg.line}. Signals are still evaluated against the bytes "
                f"that arrived; bits beyond the payload are reported as "
                f"unreadable rather than zero."
            ),
            location=_location(msg, extra={"received_length": len(payload)}),
        ))

    for result in decoded.signals:
        if result.status == "error":
            findings.append(Finding(
                code="signal_unreadable",
                severity=SEVERITY_ERROR,
                scope="frame",
                message=f"signal {result.name!r} could not be read",
                detail=result.error or "",
                location=_location(
                    msg,
                    extra={"signal": result.name, "signal_line":
                           msg.bookmarks.get(result.name).line
                           if result.name in msg.bookmarks else None},
                ),
            ))
            continue
        if result.status != "active":
            continue
        if result.in_defined_range is False:
            findings.append(Finding(
                code="physical_out_of_declared_range",
                severity=SEVERITY_WARNING,
                scope="frame",
                message=(
                    f"signal {result.name!r} decoded to {result.physical} "
                    f"{result.unit} outside its declared range "
                    f"[{result.minimum}, {result.maximum}]"
                ),
                detail=(
                    f"raw unsigned {result.raw_unsigned}, raw signed "
                    f"{result.raw_signed}, physical {result.physical}. The "
                    f"bits are valid; only the declared range is violated."
                ),
                location=_location(
                    msg,
                    extra={"signal": result.name, "signal_line":
                           msg.bookmarks.get(result.name).line
                           if result.name in msg.bookmarks else None,
                           "raw_unsigned": result.raw_unsigned,
                           "physical": result.physical},
                ),
            ))

    errors = [f for f in findings if f.severity == SEVERITY_ERROR]
    return FrameVerdict(accepted=not errors, findings=findings)


def validate_for_send(msg: MessageDef, dlc: int,
                      values: dict[str, float],
                      mux_value: int | None,
                      nested_mux: dict[str, int] | None = None) -> list[Finding]:
    """Pre-flight checks for the local frame sender.

    These are *sender* rejections, not library errors: the DBC loaded
    fine, the library can encode the frame, but the requested scenario is
    not meaningful.
    """
    findings: list[Finding] = []
    nested_mux = nested_mux or {}
    if dlc < 0 or dlc > 64:
        findings.append(Finding(
            code="send_length_invalid",
            severity=SEVERITY_ERROR,
            scope="frame",
            message=f"requested frame length {dlc} is outside 0..64 bytes",
            detail="CAN classic supports at most 8 bytes; CAN FD at most 64.",
            location=_location(msg, extra={"requested_length": dlc}),
        ))
    if dlc != msg.length:
        findings.append(Finding(
            code="send_length_differs_from_definition",
            severity=SEVERITY_WARNING,
            scope="frame",
            message=(
                f"requested frame length {dlc} differs from the "
                f"{msg.length}-byte definition"
            ),
            detail=(
                "The sender allows this on purpose so short/long frames can "
                "be exercised; the receiver will flag the mismatch."
            ),
            location=_location(msg, extra={"requested_length": dlc}),
        ))

    for sig in msg.signals:
        if sig.is_multiplexer:
            continue
        # Does this signal's *layout* fit the requested length?  That is a
        # frame-length question, not a value question, and it is reported
        # once per signal whether or not a value was supplied: the sender
        # cannot write the bits at all in this frame.
        offsets = signal_bit_offsets(sig)
        if offsets and max(offsets) >= 8 * dlc:
            findings.append(Finding(
                code="send_signal_outside_frame",
                severity=SEVERITY_ERROR,
                scope="frame",
                message=(
                    f"signal {sig.name!r} occupies bits "
                    f"{min(offsets)}..{max(offsets)}, outside the "
                    f"{dlc}-byte frame"
                ),
                detail=(
                    f"The signal is defined as {sig.start}|{sig.length} "
                    f"({sig.byte_order}). Shortening the frame moves these "
                    f"bits out of reach; they are never silently zero-filled."
                ),
                location=_location(msg, sig, extra={"requested_length": dlc}),
            ))
            continue
        if sig.name not in values:
            continue
        if sig.multiplexer_signal:
            selector_value = nested_mux.get(
                sig.multiplexer_signal, mux_value
            )
            if selector_value is None:
                continue
            if selector_value not in sig.multiplexer_ids:
                findings.append(Finding(
                    code="send_signal_not_applicable",
                    severity=SEVERITY_ERROR,
                    scope="frame",
                    message=(
                        f"signal {sig.name!r} is not applicable when "
                        f"{sig.multiplexer_signal}={selector_value}"
                    ),
                    detail=(
                        f"{sig.name!r} is defined only for selector values "
                        f"{list(sig.multiplexer_ids)}; the sender refuses to "
                        f"write those bits."
                    ),
                    location=_location(msg, sig),
                ))
        if sig.minimum is not None and values[sig.name] < sig.minimum:
            findings.append(Finding(
                code="send_value_below_defined_minimum",
                severity=SEVERITY_WARNING,
                scope="frame",
                message=(
                    f"{sig.name}={values[sig.name]} is below the declared "
                    f"minimum {sig.minimum}"
                ),
                detail="The value is encodable but violates the definition "
                       "range, which is exactly the case a test frame often "
                       "needs to exercise.",
                location=_location(msg, sig),
            ))
        if sig.maximum is not None and values[sig.name] > sig.maximum:
            findings.append(Finding(
                code="send_value_above_defined_maximum",
                severity=SEVERITY_WARNING,
                scope="frame",
                message=(
                    f"{sig.name}={values[sig.name]} is above the declared "
                    f"maximum {sig.maximum}"
                ),
                detail="The value is encodable but violates the definition "
                       "range.",
                location=_location(msg, sig),
            ))
    return findings
