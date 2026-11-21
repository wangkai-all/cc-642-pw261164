"""The cantools oracle: what the library says, kept at arm's length.

CANLab's own decode is authoritative for the bit map and the per-signal
values.  cantools is loaded *independently* and used to answer one narrow
question: "does a mature, widely used DBC library agree with this frame?"

Everything cantools emits is reported through this module under a
``library`` channel with:

* the exact call (``cantools.database.load_string(..., strict=False)``) and
  the library version,
* the exception class name, message, and the call site inside cantools,
* the business meaning CANLab assigns to the same situation.

That last column is the point of the exercise.  A cantools ``DecodeError``
for an unselected multiplexer branch is not a corrupt database; it is the
library enforcing "this signal has no value here", which CANLab represents
as ``not_applicable``.  Conversely, a business rejection (DLC mismatch,
value outside the declared range) never becomes a library error.  The
boundary is explicit in the payload shape, not in prose.
"""

from __future__ import annotations

import tempfile
import traceback
from dataclasses import dataclass, field
from typing import Any

try:  # The oracle is optional; CANLab works without cantools installed.
    import cantools
    import cantools.database
    from cantools.database.errors import DecodeError, EncodeError
    _CANTOOLS_AVAILABLE = True
    _CANTOOLS_VERSION = getattr(cantools, "__version__", "unknown")
except Exception:  # pragma: no cover - import guard
    cantools = None  # type: ignore[assignment]
    _CANTOOLS_AVAILABLE = False
    _CANTOOLS_VERSION = "not installed"
    DecodeError = EncodeError = Exception  # type: ignore[assignment,misc]

from .bits import DbcDatabase, MessageDef


LIBRARY_CALL_LOAD = (
    "cantools.database.load_string(text, database_format='dbc', strict=False)"
)
LIBRARY_CALL_DECODE = "db.decode_message(frame_id, data, decode_choices=False)"


@dataclass
class LibraryDiagnostic:
    call: str
    exception: str | None
    message: str
    detail: str
    business_meaning: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "call": self.call,
            "exception": self.exception,
            "message": self.message,
            "detail": self.detail,
            "business_meaning": self.business_meaning,
        }


@dataclass
class LibraryLoadResult:
    available: bool
    version: str
    ok: bool
    strict_ok: bool | None = None
    strict_error: LibraryDiagnostic | None = None
    messages: list[dict] = field(default_factory=list)
    diagnostics: list[LibraryDiagnostic] = field(default_factory=list)
    db: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "version": self.version,
            "ok": self.ok,
            "strict_ok": self.strict_ok,
            "strict_error": self.strict_error.to_dict()
            if self.strict_error else None,
            "messages": self.messages,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
        }


def _where(exc: BaseException) -> str:
    """The innermost cantools frame that raised, as ``file:line``."""
    frames = traceback.extract_tb(exc.__traceback__)
    for frame in reversed(frames):
        if "cantools" in frame.filename:
            return f"{frame.filename.split('cantools/')[-1]}:{frame.lineno}"
    return frames[-1].filename.split("/")[-1] + f":{frames[-1].lineno}" if frames else "?"


def load_with_cantools(text: str, filename: str = "uploaded.dbc") -> LibraryLoadResult:
    """Load a DBC twice: strict (library default) and lenient.

    The strict call answers "would a standard toolchain accept this
    database?"  The lenient call is what CANLab itself behaves like, so a
    database with overlapping definitions can still be inspected.  When the
    strict call fails and the lenient one succeeds, that difference is
    itself a finding, not an error.
    """
    if not _CANTOOLS_AVAILABLE:
        return LibraryLoadResult(
            available=False,
            version=_CANTOOLS_VERSION,
            ok=False,
            diagnostics=[LibraryDiagnostic(
                call=LIBRARY_CALL_LOAD,
                exception="ImportError",
                message="cantools is not installed; the oracle is disabled",
                detail="Install the optional dependency: pip install cantools",
                business_meaning=(
                    "CANLab can still load, decode and validate the "
                    "database; only the independent cross-check is "
                    "unavailable."
                ),
            )],
        )

    result = LibraryLoadResult(
        available=True, version=_CANTOOLS_VERSION, ok=False,
    )

    strict_db = None
    try:
        strict_db = cantools.database.load_string(
            text, database_format="dbc", strict=True,
        )
        result.strict_ok = True
    except Exception as exc:  # noqa: BLE001 - the library's exception surface is broad
        result.strict_ok = False
        result.strict_error = LibraryDiagnostic(
            call="cantools.database.load_string(text, database_format='dbc', strict=True)",
            exception=type(exc).__name__,
            message=str(exc),
            detail=(
                f"raised at {_where(exc)}. The strict loader rejects the "
                f"whole database, so the offending definition cannot be "
                f"inspected through cantools' normal API."
            ),
            business_meaning=(
                "A standard toolchain will refuse this database. CANLab "
                "loads it anyway and shows the conflict at its source line; "
                "this is a *definition* problem, not a decoding failure."
            ),
        )
        result.diagnostics.append(result.strict_error)

    try:
        lenient_db = cantools.database.load_string(
            text, database_format="dbc", strict=False,
        )
    except Exception as exc:  # noqa: BLE001
        diagnostic = LibraryDiagnostic(
            call=LIBRARY_CALL_LOAD,
            exception=type(exc).__name__,
            message=str(exc),
            detail=f"raised at {_where(exc)}",
            business_meaning=(
                "Not even the lenient loader accepts the file. CANLab's own "
                "reader may still parse the frames it can understand."
            ),
        )
        result.diagnostics.append(diagnostic)
        return result

    result.ok = True
    result.db = lenient_db
    for msg in lenient_db.messages:
        result.messages.append({
            "frame_id": msg.frame_id,
            "name": msg.name,
            "length": msg.length,
            "is_extended": msg.is_extended_frame,
            "signal_count": len(msg.signals),
            "signals": [
                {
                    "name": sig.name,
                    "start": sig.start,
                    "length": sig.length,
                    "byte_order": sig.byte_order,
                    "is_signed": sig.is_signed,
                    "is_multiplexer": sig.is_multiplexer,
                    "multiplexer_signal": sig.multiplexer_signal,
                    "multiplexer_ids": sig.multiplexer_ids or [],
                }
                for sig in msg.signals
            ],
        })

    if result.strict_ok is False and result.ok:
        result.diagnostics.append(LibraryDiagnostic(
            call=LIBRARY_CALL_LOAD,
            exception=None,
            message=(
                "strict loading failed but lenient loading succeeded; "
                "cantools can model the database only when its consistency "
                "checks are disabled"
            ),
            detail=(
                "This is the documented strict/lenient split in cantools. "
                "CANLab calls the lenient path and reports the conflicts "
                "itself with source lines."
            ),
            business_meaning=(
                "Definition conflicts are tolerated by CANLab so they can be "
                "displayed and compared; they are not suppressed."
            ),
        ))
    return result


@dataclass
class OracleFrame:
    ok: bool
    values: dict[str, Any]
    diagnostic: LibraryDiagnostic | None
    disagreement: list[dict]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "values": self.values,
            "diagnostic": self.diagnostic.to_dict() if self.diagnostic else None,
            "disagreement": self.disagreement,
        }


def cross_check_frame(library: LibraryLoadResult, msg: MessageDef,
                      payload: bytes, our_results: list[dict],
                      mux_values: dict[str, int] | None = None) -> OracleFrame:
    """Decode one frame with cantools and compare with CANLab.

    ``our_results`` is the ``signals`` list from a CANLab ``DecodedFrame``.
    Only signals CANLab resolved as *active* are compared.  When cantools
    refuses the frame outright because of a multiplexer condition CANLab
    considers "not applicable" (``mux_values`` explains which), that is not
    a disagreement: the two tools encode the same rule differently, and the
    diagnostic says so.
    """
    if not library.ok or library.db is None:
        return OracleFrame(
            ok=False,
            values={},
            diagnostic=LibraryDiagnostic(
                call=LIBRARY_CALL_DECODE,
                exception="Unavailable",
                message="cantools could not load this database; no cross-check ran",
                detail="See the library load diagnostics.",
                business_meaning=(
                    "No independent opinion is available for this database. "
                    "CANLab's own decode and validation still ran."
                ),
            ),
            disagreement=[],
        )

    try:
        raw_values = library.db.decode_message(
            msg.frame_id, payload, decode_choices=False, scaling=False,
        )
    except Exception as exc:  # noqa: BLE001
        return OracleFrame(
            ok=False,
            values={},
            diagnostic=LibraryDiagnostic(
                call=LIBRARY_CALL_DECODE,
                exception=type(exc).__name__,
                message=str(exc),
                detail=(
                    f"raised at {_where(exc)}. Message {msg.name!r} "
                    f"(0x{msg.frame_id:X}) was defined {len(payload)} bytes "
                    f"of data."
                ),
                business_meaning=_classify_decode_error(exc, msg),
            ),
            disagreement=[],
        )

    ours = {r["name"]: r for r in our_results}
    disagreement: list[dict] = []
    compared: dict[str, Any] = {}
    for name, value in raw_values.items():
        mine = ours.get(name)
        if mine is None:
            disagreement.append({
                "signal": name,
                "kind": "cantools_only",
                "detail": (
                    "cantools decoded a value for a signal CANLab did not "
                    "produce; the two databases disagree on this signal."
                ),
            })
            continue
        if mine["status"] != "active":
            continue
        compared[name] = value
        if mine.get("raw_signed") is None:
            continue
        if value != mine["raw_signed"]:
            disagreement.append({
                "signal": name,
                "kind": "raw_value_mismatch",
                "cantools": value,
                "canlab": mine["raw_signed"],
                "detail": (
                    f"cantools raw {value} vs CANLab raw {mine['raw_signed']} "
                    f"for {name!r}; one of the two layouts is wrong. "
                    f"CANLab layout: {mine['byte_order']} start "
                    f"{mine['start']} length {mine['length']}."
                ),
            })
    for name, result in ours.items():
        if result["status"] == "active" and name not in raw_values:
            disagreement.append({
                "signal": name,
                "kind": "canlab_only",
                "detail": (
                    "CANLab decoded this signal but cantools returned no "
                    "value for it."
                ),
            })
    return OracleFrame(ok=True, values=compared, diagnostic=None,
                       disagreement=disagreement)


def _classify_decode_error(exc: BaseException, msg: MessageDef) -> str:
    text = str(exc)
    lowered = text.lower()
    if "expected multiplexer id" in lowered:
        return (
            "Business condition, not a library failure: the frame selected "
            "a multiplexer branch that this signal does not cover. CANLab "
            "reports the signal as not applicable (the bits belong to the "
            "selected branch and are never read as this signal); cantools "
            "encodes the same rule as a DecodeError."
        )
    if "wrong data size" in lowered:
        return (
            "Business condition: the received frame length does not match "
            "the definition. CANLab keeps the frame, decodes the bytes that "
            "are present, and marks the length mismatch as a frame finding."
        )
    if "overlapping" in lowered or "does not fit" in lowered:
        return (
            "Definition conflict that cantools cannot model even leniently. "
            "CANLab still lays out the bits so the conflict can be located "
            "at its source line."
        )
    if "unpacking failed" in lowered:
        return (
            "The library's bit-level unpacker rejected the data layout; "
            "CANLab decodes bit by bit and reports the exact signal whose "
            "span leaves the frame."
        )
    return (
        "The library could not decode this frame. CANLab's own bit-level "
        "decode is reported separately; compare the per-signal layouts."
    )


def compare_definitions(left: DbcDatabase, right: DbcDatabase) -> list[dict]:
    """Structural differences between two databases, for the A/B view.

    Only messages present in *both* files are compared field by field; a
    message present in one file only is reported as such.  This is what
    makes the "same raw frame, two DBCs" question answerable.
    """
    diffs: list[dict] = []
    right_by_key = {
        (m.frame_id, m.is_extended): m for m in right.messages
    }
    left_keys = {(m.frame_id, m.is_extended) for m in left.messages}
    for msg in left.messages:
        key = (msg.frame_id, msg.is_extended)
        other = right_by_key.get(key)
        if other is None:
            diffs.append({
                "kind": "message_missing_in_right",
                "frame_id": msg.frame_id,
                "is_extended": msg.is_extended,
                "message": msg.name,
                "detail": (
                    f"{left.filename} defines {msg.name!r} at line "
                    f"{msg.line}; {right.filename} has no message with this "
                    f"id and frame kind."
                ),
            })
            continue
        entry = _compare_message(msg, other, left.filename, right.filename)
        if entry:
            diffs.append(entry)
    for msg in right.messages:
        key = (msg.frame_id, msg.is_extended)
        if key not in left_keys:
            diffs.append({
                "kind": "message_missing_in_left",
                "frame_id": msg.frame_id,
                "is_extended": msg.is_extended,
                "message": msg.name,
                "detail": (
                    f"{right.filename} defines {msg.name!r} at line "
                    f"{msg.line}; {left.filename} has no message with this "
                    f"id and frame kind."
                ),
            })
    return diffs


def _compare_message(a: MessageDef, b: MessageDef, left_name: str,
                     right_name: str) -> dict | None:
    changes: list[dict] = []
    if a.length != b.length:
        changes.append({
            "field": "length",
            "left": a.length,
            "right": b.length,
            "detail": (
                f"message length differs: {a.length} bytes (line {a.line}) "
                f"vs {b.length} bytes (line {b.line})"
            ),
        })
    b_signals = {s.name: s for s in b.signals}
    a_signals = {s.name: s for s in a.signals}
    for name, sig in a_signals.items():
        other = b_signals.get(name)
        if other is None:
            changes.append({
                "field": "signal_missing_in_right",
                "signal": name,
                "left": _layout_summary(sig),
                "right": None,
                "detail": (
                    f"signal {name!r} is defined in {left_name} but not in "
                    f"{right_name}"
                ),
            })
            continue
        for field in ("start", "length", "byte_order", "is_signed", "scale",
                      "offset", "unit", "minimum", "maximum",
                      "is_multiplexer", "multiplexer_signal",
                      "multiplexer_ids", "choices"):
            lv = getattr(sig, field)
            rv = getattr(other, field)
            if field == "multiplexer_ids":
                lv, rv = list(lv), list(rv)
            if lv != rv:
                changes.append({
                    "field": field,
                    "signal": name,
                    "left": lv,
                    "right": rv,
                    "detail": (
                        f"{name}.{field}: {lv!r} ({left_name}) vs {rv!r} "
                        f"({right_name})"
                    ),
                })
    for name in b_signals:
        if name not in a_signals:
            changes.append({
                "field": "signal_missing_in_left",
                "signal": name,
                "left": None,
                "right": _layout_summary(b_signals[name]),
                "detail": (
                    f"signal {name!r} is defined in {right_name} but not in "
                    f"{left_name}"
                ),
            })
    if not changes:
        return None
    return {
        "kind": "message_differs",
        "frame_id": a.frame_id,
        "is_extended": a.is_extended,
        "message": a.name,
        "changes": changes,
        "detail": (
            f"message {a.name!r} (0x{a.frame_id:X}) differs between "
            f"{left_name} and {right_name}"
        ),
    }


def _layout_summary(sig) -> dict:
    return {
        "start": sig.start,
        "length": sig.length,
        "byte_order": sig.byte_order,
        "is_signed": sig.is_signed,
        "scale": sig.scale,
        "offset": sig.offset,
        "unit": sig.unit,
        "multiplexer_signal": sig.multiplexer_signal,
        "multiplexer_ids": list(sig.multiplexer_ids),
    }
