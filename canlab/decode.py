"""Decode one frame against one database, keeping every intermediate value.

The output of :func:`decode_frame` is the single structure the UI renders
and the exporter serialises.  Nothing is collapsed: the raw unsigned
integer, the sign-extended integer, the physical float and the text enum
are kept separately, because "raw 0xFB", "raw -5", "-5.0", and "Init" are
four different facts and a CSV export may need any subset of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .bits import (
    DbcDatabase,
    LayoutError,
    MessageDef,
    SignalDef,
    active_signals,
    apply_sign,
    choice_label,
    extract_raw,
    raw_to_physical,
    signal_bit_offsets,
    signal_status,
)

NOT_APPLICABLE = "not_applicable"
ACTIVE = "active"
ERROR = "error"


@dataclass
class SignalResult:
    name: str
    status: str
    reason: str | None
    start: int
    length: int
    byte_order: str
    is_signed: bool
    is_multiplexer: bool
    multiplexer_signal: str | None
    multiplexer_ids: list[int]
    scale: float
    offset: float
    unit: str
    minimum: float | None
    maximum: float | None
    bit_offsets: list[int]
    raw_unsigned: int | None = None
    raw_signed: int | None = None
    physical: float | None = None
    enum_text: str | None = None
    enum_known: bool | None = None
    in_defined_range: bool | None = None
    comment: str = ""
    bit_text: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "reason": self.reason,
            "start": self.start,
            "length": self.length,
            "byte_order": self.byte_order,
            "is_signed": self.is_signed,
            "is_multiplexer": self.is_multiplexer,
            "multiplexer_signal": self.multiplexer_signal,
            "multiplexer_ids": self.multiplexer_ids,
            "scale": self.scale,
            "offset": self.offset,
            "unit": self.unit,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "bit_offsets": self.bit_offsets,
            "raw_unsigned": self.raw_unsigned,
            "raw_signed": self.raw_signed,
            "physical": self.physical,
            "enum_text": self.enum_text,
            "enum_known": self.enum_known,
            "in_defined_range": self.in_defined_range,
            "comment": self.comment,
            "bit_text": self.bit_text,
            "error": self.error,
        }


@dataclass
class DecodedFrame:
    dbc: str
    message: str | None
    frame_id: int
    is_extended: bool
    dlc: int
    payload_hex: str
    expected_dlc: int | None
    dlc_ok: bool
    signals: list[SignalResult]
    mux_events: list[dict]
    mux_values: dict[str, int]
    frame_errors: list[str] = field(default_factory=list)
    message_found: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "dbc": self.dbc,
            "message": self.message,
            "frame_id": self.frame_id,
            "is_extended": self.is_extended,
            "dlc": self.dlc,
            "payload_hex": self.payload_hex,
            # The bit map draws from these.  They are the bytes that were
            # actually decoded, so a short or long frame keeps its own
            # shape instead of being padded to the definition's length.
            "payload_bytes": [int(part, 16)
                              for part in self.payload_hex.split()] if self.payload_hex else [],
            "expected_dlc": self.expected_dlc,
            "dlc_ok": self.dlc_ok,
            "signals": [s.to_dict() for s in self.signals],
            "mux_events": self.mux_events,
            "mux_values": self.mux_values,
            "frame_errors": self.frame_errors,
            "message_found": self.message_found,
        }


def _bits_string(payload: bytes, offsets: list[int]) -> str:
    """The occupied bits in MSB-first order, grouped by source byte."""
    out: list[str] = []
    last_byte: int | None = None
    for off in offsets:
        byte_i, bit_i = divmod(off, 8)
        if last_byte is not None and byte_i != last_byte:
            out.append(".")
        out.append(str((payload[byte_i] >> bit_i) & 1))
        last_byte = byte_i
    return "".join(out)


def _base_result(sig: SignalDef) -> SignalResult:
    return SignalResult(
        name=sig.name,
        status=ACTIVE,
        reason=None,
        start=sig.start,
        length=sig.length,
        byte_order=sig.byte_order,
        is_signed=sig.is_signed,
        is_multiplexer=sig.is_multiplexer,
        multiplexer_signal=sig.multiplexer_signal,
        multiplexer_ids=list(sig.multiplexer_ids),
        scale=sig.scale,
        offset=sig.offset,
        unit=sig.unit,
        minimum=sig.minimum,
        maximum=sig.maximum,
        bit_offsets=[],
        comment=sig.comment,
    )


def decode_message(msg: MessageDef, payload: bytes, dbc_name: str,
                   frame_id: int, is_extended: bool) -> DecodedFrame:
    frame_errors: list[str] = []
    dlc_ok = len(payload) == msg.length
    if not dlc_ok:
        frame_errors.append(
            f"frame is {len(payload)} bytes but {dbc_name} defines "
            f"{msg.name!r} as {msg.length} bytes"
        )

    # Resolve multiplexing against the bytes that actually arrived.  A
    # truncated frame can still yield a selector if the selector fits.
    usable = payload[: msg.length] if len(payload) > msg.length else payload
    try:
        mux_values, mux_events = active_signals(msg, usable)
    except LayoutError as exc:
        mux_values, mux_events = {}, [{"signal": "?", "role": "error",
                                       "message": str(exc)}]

    results: list[SignalResult] = []
    for sig in msg.signals:
        result = _base_result(sig)
        try:
            result.bit_offsets = signal_bit_offsets(sig)
        except LayoutError as exc:
            result.status = ERROR
            result.error = str(exc)
            results.append(result)
            continue

        if result.bit_offsets and max(result.bit_offsets) >= 8 * len(payload):
            result.status = ERROR
            result.error = (
                f"signal needs bit {max(result.bit_offsets)} "
                f"(byte {max(result.bit_offsets) // 8}) but the frame has "
                f"only {len(payload)} bytes"
            )
            results.append(result)
            continue

        status, reason = signal_status(msg, sig, mux_values)
        if status == NOT_APPLICABLE:
            result.status = NOT_APPLICABLE
            result.reason = reason
            # Deliberately no raw / physical: the bits belong to whatever
            # branch the selector actually chose, and reading them as this
            # signal would be a fabrication.  The layout is still reported.
            results.append(result)
            continue

        try:
            raw = extract_raw(sig, payload)
        except LayoutError as exc:
            result.status = ERROR
            result.error = str(exc)
            results.append(result)
            continue

        value = apply_sign(raw, sig.length, sig.is_signed)
        physical = raw_to_physical(value, sig)
        result.raw_unsigned = raw
        result.raw_signed = value
        result.physical = physical
        result.bit_text = _bits_string(payload, result.bit_offsets)
        if sig.choices:
            label = choice_label(value, sig)
            result.enum_known = label is not None
            result.enum_text = label
        if sig.minimum is not None and sig.maximum is not None:
            result.in_defined_range = sig.minimum <= physical <= sig.maximum
        results.append(result)

    return DecodedFrame(
        dbc=dbc_name,
        message=msg.name,
        frame_id=frame_id,
        is_extended=is_extended,
        dlc=len(payload),
        payload_hex=payload.hex(" ").upper(),
        expected_dlc=msg.length,
        dlc_ok=dlc_ok,
        signals=results,
        mux_events=mux_events,
        mux_values=mux_values,
        frame_errors=frame_errors,
    )


def decode_unknown(dbc: DbcDatabase, payload: bytes, frame_id: int,
                   is_extended: bool) -> DecodedFrame:
    return DecodedFrame(
        dbc=dbc.filename,
        message=None,
        frame_id=frame_id,
        is_extended=is_extended,
        dlc=len(payload),
        payload_hex=payload.hex(" ").upper(),
        expected_dlc=None,
        dlc_ok=True,
        signals=[],
        mux_events=[],
        mux_values={},
        frame_errors=[
            f"no message with {'extended' if is_extended else 'standard'} "
            f"id 0x{frame_id:X} is defined in {dbc.filename}"
        ],
        message_found=False,
    )


def decode_frame(dbc: DbcDatabase, frame_id: int, is_extended: bool,
                 payload: bytes) -> DecodedFrame:
    msg = dbc.message_by_id(frame_id, is_extended)
    if msg is None:
        return decode_unknown(dbc, payload, frame_id, is_extended)
    return decode_message(msg, payload, dbc.filename, frame_id, is_extended)
