"""Bit-exact DBC signal layout engine for CANLab.

CANLab deliberately does *not* rely on cantools to answer the question
"which bits does this signal occupy?".  That question has one correct
answer per DBC dialect (Intel = LSB-first little endian, Motorola =
network/MSB-first big endian with the DBC "sawtooth" start bit), and the
whole point of this tool is to show the layout *and* the raw frame so a
human can audit both definitions side by side.  cantools is used as an
independent second opinion and its diagnostics are reported as library
diagnostics (see ``library_oracle.py``), never mixed into business
validation (see ``validators.py``).

Terminology used throughout this module
---------------------------------------
``start``      The start bit exactly as written in the DBC, i.e. the
               "sawtooth" index: byte N occupies indices 8N..8N+7 with
               bit 7 at 8N and bit 0 at 8N+7.
``bit_offset`` The LSB-first linear bit index (0 = LSB of byte 0).  This is
               what gets drawn in the bit map.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class LayoutError(ValueError):
    """A signal cannot be placed inside a frame of the declared length."""


@dataclass
class SignalDef:
    name: str
    start: int
    length: int
    byte_order: str = "little_endian"  # or "big_endian"
    is_signed: bool = False
    scale: float = 1.0
    offset: float = 0.0
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    choices: dict[int, str] = field(default_factory=dict)
    is_multiplexer: bool = False
    multiplexer_signal: str | None = None
    multiplexer_ids: tuple[int, ...] = ()
    comment: str = ""


@dataclass(frozen=True)
class Bookmark:
    """A comment attached to a specific DBC source line, for citations."""

    line: int
    text: str


@dataclass
class MessageDef:
    frame_id: int
    name: str
    length: int
    signals: list[SignalDef]
    is_extended: bool = False
    senders: list[str] = field(default_factory=list)
    comment: str = ""
    bookmarks: dict[str, Bookmark] = field(default_factory=dict)  # signal name -> line
    line: int = 0


@dataclass
class DbcDatabase:
    filename: str
    messages: list[MessageDef]
    nodes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def message_by_id(self, frame_id: int, is_extended: bool) -> MessageDef | None:
        for m in self.messages:
            if m.frame_id == frame_id and m.is_extended == is_extended:
                return m
        return None


# ---------------------------------------------------------------------------
# Bit geometry
# ---------------------------------------------------------------------------

def sawtooth_to_bit_offset(bitnum: int) -> int:
    """Convert a DBC sawtooth bit index to an LSB-first linear bit offset."""
    return 8 * (bitnum // 8) + (7 - (bitnum % 8))


def network_bit_of(sawtooth_bitnum: int) -> int:
    """Convert a DBC sawtooth index to a network ("MSB-first") bit index."""
    return 8 * (sawtooth_bitnum // 8) + (7 - (sawtooth_bitnum % 8))


def signal_bit_offsets(sig: SignalDef) -> list[int]:
    """Return the occupied LSB-first linear bit offsets, MSB first.

    Intel (little endian): the DBC start bit is the LSB; the signal grows
    towards higher linear offsets.

    Motorola (big endian): the DBC start bit is the MSB in *sawtooth*
    notation.  Conversion to the network (MSB-first) index puts it at the
    left edge; the signal then advances one network bit at a time, which
    walks towards lower bit numbers inside the byte and wraps from the LSB
    of a byte to the MSB of the next byte.  Network index ``n`` corresponds
    to linear offset ``8*(n//8) + 7 - (n%8)``.
    """
    if sig.length <= 0:
        return []
    if sig.byte_order == "big_endian":
        start_net = network_bit_of(sig.start)
        return [
            network_bit_of(start_net + i)
            for i in range(sig.length)
        ]
    # Little endian: the DBC start bit is the LSB, so MSB-first order is
    # the reverse of the ascending linear offsets.
    return list(range(sig.start + sig.length - 1, sig.start - 1, -1))


def _linear_offsets(sig: SignalDef) -> list[int]:
    """The same set as ``signal_bit_offsets`` but computed directly in the
    LSB-first space, which is what extraction uses."""
    return signal_bit_offsets(sig)


def extract_raw(sig: SignalDef, payload: bytes) -> int:
    """Extract the *unsigned raw* integer occupied by ``sig``.

    The caller is responsible for applying two's-complement sign
    extension; keeping this function purely bit-level makes the bit map
    and the decode share one source of truth.
    """
    offsets = signal_bit_offsets(sig)
    if not offsets:
        raise LayoutError(f"signal {sig.name!r} has zero length")
    if max(offsets) >= 8 * len(payload):
        raise LayoutError(
            f"signal {sig.name!r} needs bit {max(offsets)} but the frame "
            f"only has {8 * len(payload)} bits"
        )
    value = 0
    # ``offsets`` is MSB first, so shift left as we walk.
    for off in offsets:
        byte_i, bit_i = divmod(off, 8)
        value = (value << 1) | ((payload[byte_i] >> bit_i) & 1)
    return value


def apply_sign(raw: int, length: int, is_signed: bool) -> int:
    if not is_signed:
        return raw
    sign_bit = 1 << (length - 1)
    if raw & sign_bit:
        return raw - (1 << length)
    return raw


def raw_to_physical(raw: int, sig: SignalDef) -> float:
    return raw * sig.scale + sig.offset


def choice_label(raw: int, sig: SignalDef) -> str | None:
    return sig.choices.get(raw) if sig.choices else None


# ---------------------------------------------------------------------------
# Multiplexing
# ---------------------------------------------------------------------------

def active_signals(msg: MessageDef, payload: bytes) -> tuple[dict[str, int], list[dict]]:
    """Resolve the multiplex tree for one frame.

    Returns ``(mux_values, events)`` where ``events`` records every
    decision with the raw value that produced it, so the UI can show *why*
    a signal is "not applicable" instead of silently decoding zero.

    A multiplexer nested under another one (``m3M``) is only read when its
    own selector condition holds.  A selector whose parent chooses another
    branch therefore has no value at all, and every signal below it is
    *not applicable* rather than zero.

    This mirrors cantools' semantics: a multiplexed signal is only
    decodable when the current multiplexer value is one of its declared
    ``multiplexer_ids``.
    """
    muxers = {s.name: s for s in msg.signals if s.is_multiplexer}
    mux_values: dict[str, int] = {}
    events: list[dict] = []
    blocked: dict[str, str] = {}

    def resolve(name: str, stack: tuple[str, ...]) -> str | None:
        """Return a reason string when ``name`` cannot be read."""
        if name in mux_values or name in blocked:
            return blocked.get(name)
        if name in stack:
            reason = f"multiplexer cycle: {' -> '.join(stack + (name,))}"
            blocked[name] = reason
            return reason
        sig = muxers.get(name)
        if sig is None:
            return f"multiplexer {name!r} is not defined in the message"
        parent = sig.multiplexer_signal
        if parent:
            parent_reason = resolve(parent, stack + (name,))
            if parent_reason is not None:
                blocked[name] = (
                    f"parent multiplexer {parent!r} is not decodable: "
                    f"{parent_reason}"
                )
                return blocked[name]
            parent_value = mux_values.get(parent)
            if parent_value not in sig.multiplexer_ids:
                expected = ", ".join(str(v) for v in sorted(sig.multiplexer_ids))
                blocked[name] = (
                    f"multiplexer {name!r} is only defined when "
                    f"{parent}={expected}, but {parent}={parent_value}"
                )
                return blocked[name]
        try:
            raw = extract_raw(sig, payload)
        except LayoutError as exc:
            blocked[name] = f"multiplexer {name!r} cannot be read: {exc}"
            return blocked[name]
        value = apply_sign(raw, sig.length, sig.is_signed)
        mux_values[name] = value
        events.append({
            "signal": name,
            "role": "multiplexer",
            "start": sig.start,
            "length": sig.length,
            "byte_order": sig.byte_order,
            "raw": raw,
            "value": value,
        })
        return None

    for name in muxers:
        reason = resolve(name, ())
        if reason is not None and name not in blocked:
            blocked[name] = reason

    for name, reason in blocked.items():
        events.append({
            "signal": name,
            "role": "unresolved",
            "message": reason,
        })

    return mux_values, events


def signal_status(
    msg: MessageDef, sig: SignalDef, mux_values: dict[str, int]
) -> tuple[str, str | None]:
    """Return ``('active'|'not_applicable', reason)`` for one signal."""
    if not sig.multiplexer_signal:
        return "active", None
    parent = sig.multiplexer_signal
    if parent not in {s.name for s in msg.signals if s.is_multiplexer}:
        return "not_applicable", (
            f"multiplexer {parent!r} is not defined in this message"
        )
    if parent not in mux_values:
        return "not_applicable", (
            f"multiplexer {parent!r} has no value for this frame, so this "
            f"signal cannot be interpreted"
        )
    parent_value = mux_values[parent]
    if parent_value in sig.multiplexer_ids:
        return "active", None
    expected = ", ".join(str(v) for v in sorted(sig.multiplexer_ids))
    return "not_applicable", (
        f"multiplexer {parent}={parent_value}; this signal is defined only "
        f"for {expected}"
    )


# ---------------------------------------------------------------------------
# Encoding (used by the local frame sender)
# ---------------------------------------------------------------------------

def physical_to_raw(value: float, sig: SignalDef) -> int:
    """Quantise a physical value to the integer the frame carries.

    Round-half-away-from-zero, matching what a human filling in a test frame
    expects.  A zero scale is a definition error and is rejected by the
    validators before this runs.
    """
    if sig.scale == 0:
        raise LayoutError(f"signal {sig.name!r} has scale 0; cannot invert it")
    raw = round((value - sig.offset) / sig.scale)
    if not sig.is_signed:
        if raw < 0:
            raise LayoutError(
                f"signal {sig.name!r} is unsigned but the requested value "
                f"maps to raw {raw}"
            )
        if raw >= 1 << sig.length:
            raise LayoutError(
                f"signal {sig.name!r} is {sig.length} bits wide unsigned; "
                f"raw {raw} does not fit"
            )
    else:
        if raw < -(1 << (sig.length - 1)) or raw >= (1 << (sig.length - 1)):
            raise LayoutError(
                f"signal {sig.name!r} is {sig.length} bits wide signed; "
                f"raw {raw} does not fit"
            )
        raw &= (1 << sig.length) - 1
    return raw


def write_signal_bits(sig: SignalDef, raw_unsigned: int, payload: bytearray) -> None:
    """Write the low ``length`` bits of ``raw_unsigned`` into ``payload``."""
    offsets = signal_bit_offsets(sig)
    if not offsets:
        raise LayoutError(f"signal {sig.name!r} has zero length")
    if max(offsets) >= 8 * len(payload):
        raise LayoutError(
            f"signal {sig.name!r} needs bit {max(offsets)} but the frame "
            f"only has {8 * len(payload)} bits"
        )
    if raw_unsigned < 0 or raw_unsigned >= 1 << sig.length:
        raise LayoutError(
            f"raw {raw_unsigned} does not fit in {sig.length} bits"
        )
    for index, off in enumerate(offsets):
        bit = (raw_unsigned >> (sig.length - 1 - index)) & 1
        byte_i, bit_i = divmod(off, 8)
        mask = 1 << bit_i
        if bit:
            payload[byte_i] |= mask
        else:
            payload[byte_i] &= ~mask & 0xFF


def encode_frame(msg: MessageDef, dlc: int, mux_value: int | None,
                 values: dict[str, float], pad: int = 0x00,
                 nested_mux: dict[str, int] | None = None,
                 force: bool = False) -> bytes:
    """Encode a frame for the sender.

    ``mux_value`` sets the message's root multiplexer.  ``nested_mux`` sets
    selectors further down the tree.  ``values`` maps signal names to
    *physical* values.  Signals not supplied stay at the padding pattern.
    Supplying a signal that the chosen selector does not select is a
    rejection: silently writing those bits would create a frame the DBC
    says is meaningless.
    """
    payload = bytearray([pad & 0xFF] * dlc)
    root = next(
        (s for s in msg.signals if s.is_multiplexer and not s.multiplexer_signal),
        None,
    )
    nested_mux = nested_mux or {}
    mux_values: dict[str, int] = {}

    def fits(sig: SignalDef) -> bool:
        offsets = signal_bit_offsets(sig)
        return bool(offsets) and max(offsets) < 8 * dlc

    if root is not None:
        if not fits(root):
            if not force:
                raise LayoutError(
                    f"multiplexer {root.name!r} does not fit in a "
                    f"{dlc}-byte frame"
                )
        else:
            effective = mux_value if mux_value is not None else 0
            raw = physical_to_raw(effective, root)
            write_signal_bits(root, raw, payload)
            mux_values[root.name] = raw

    # Selectors further down the tree, in declaration order so a parent is
    # always resolved before its children.
    for sig in msg.signals:
        if not (sig.is_multiplexer and sig.multiplexer_signal):
            continue
        parent_value = mux_values.get(sig.multiplexer_signal)
        if parent_value is None or parent_value not in sig.multiplexer_ids:
            continue
        if not fits(sig):
            # With ``force`` the frame is shortened on purpose; a signal
            # whose bits left the frame is not written at all.  Clipping
            # would fabricate a value the receiver could not distinguish
            # from a real one.
            if force:
                continue
            raise LayoutError(
                f"multiplexer {sig.name!r} does not fit in a {dlc}-byte frame"
            )
        physical = nested_mux.get(sig.name, values.get(sig.name, 0.0))
        raw = physical_to_raw(physical, sig)
        write_signal_bits(sig, raw, payload)
        mux_values[sig.name] = raw

    for sig in msg.signals:
        if sig.is_multiplexer or sig.name not in values:
            continue
        # Nested selectors were written above and are tracked in
        # ``mux_values`` by raw value; children compare against the raw
        # selector value, so a selector that carries a scale/offset must
        # still be matched by its raw bit content.
        #
        # ``force`` relaxes frame-length errors only.  It must never make
        # the sender write a signal the selected branch does not define:
        # those bits belong to another signal, and writing them would
        # manufacture a value with no definition behind it.
        if sig.multiplexer_signal:
            parent_value = mux_values.get(sig.multiplexer_signal)
            if parent_value is None or parent_value not in sig.multiplexer_ids:
                raise LayoutError(
                    f"signal {sig.name!r} is not applicable for "
                    f"multiplexer value {parent_value}; refusing to write it"
                )
        if not fits(sig):
            if force:
                continue
            raise LayoutError(
                f"signal {sig.name!r} needs bit {max(signal_bit_offsets(sig))} "
                f"but the frame only has {8 * dlc} bits"
            )
        write_signal_bits(sig, physical_to_raw(values[sig.name], sig), payload)
    return bytes(payload)
