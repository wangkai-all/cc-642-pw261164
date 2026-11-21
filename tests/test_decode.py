"""Decoding one frame: raw, physical and text kept apart; NA is not zero.

The central claim under test is that a signal which the frame's selector
does not select carries **no** raw and **no** physical value.  A decoder
that returns 0 there is indistinguishable from a real zero and would let a
downstream consumer act on a value nobody transmitted.
"""

from __future__ import annotations

import pytest

from canlab.decode import (
    ACTIVE,
    ERROR,
    NOT_APPLICABLE,
    decode_frame,
    decode_message,
)


def by_name(decoded) -> dict:
    return {s.name: s for s in decoded.signals}


def test_each_representation_is_stored_separately(demo):
    msg = demo.message_by_id(0x100, False)
    payload = bytes.fromhex("01 02 03 AB CD EF 11 22")
    decoded = decode_message(msg, payload, "demo_ecu.dbc", 0x100, False)
    signals = by_name(decoded)

    lamp = signals["LampState"]
    # raw 2, physical 2.0, text "Blink" - all three retained.
    assert lamp.raw_unsigned == 2
    assert lamp.raw_signed == 2
    assert lamp.physical == 2.0
    assert lamp.enum_text == "Blink"
    assert lamp.enum_known is True

    temp = signals["CoolantTemp"]
    assert temp.raw_unsigned == 0xAB
    assert temp.physical == pytest.approx(0xAB - 40)
    assert temp.unit == "degC"


def test_negative_raw_keeps_both_signed_and_unsigned(demo):
    msg = demo.message_by_id(0x100, False)
    # EngineTorque is Motorola 39|12 signed; build a frame with raw 0x800
    # (= -2048 * 0.5 = -1024 Nm) in bits 32..47.
    payload = bytearray(8)
    payload[4] = 0x80
    decoded = decode_message(msg, bytes(payload), "demo_ecu.dbc", 0x100, False)
    torque = by_name(decoded)["EngineTorque"]
    assert torque.raw_unsigned == 0x800
    assert torque.raw_signed == -2048
    assert torque.physical == pytest.approx(-1024.0)


def test_bits_are_reported_msb_first(demo):
    msg = demo.message_by_id(0x100, False)
    decoded = decode_message(msg, bytes(8), "demo_ecu.dbc", 0x100, False)
    speed = by_name(decoded)["Speed"]
    assert speed.bit_offsets == list(range(23, 7, -1))
    # A dot marks each byte boundary so the strip shows the byte split.
    assert speed.bit_text == "00000000.00000000"


def test_unselected_branch_has_no_value(demo):
    msg = demo.message_by_id(0x101, False)
    payload = bytes.fromhex("05 11 22 33 44 55 66 01")
    decoded = decode_message(msg, payload, "demo_ecu.dbc", 0x101, False)
    signals = by_name(decoded)

    assert signals["Mode"].status == ACTIVE
    assert signals["Mode"].raw_unsigned == 5
    assert decoded.mux_values == {"Mode": 5}

    for name in ("MotorRpm", "MotorCurrent", "ChargeVoltage",
                 "ChargeTimer", "ServiceCode"):
        sig = signals[name]
        assert sig.status == NOT_APPLICABLE, name
        # The whole point: no fabricated zero.
        assert sig.raw_unsigned is None
        assert sig.raw_signed is None
        assert sig.physical is None
        assert sig.enum_text is None
        assert "Mode=5" in sig.reason
    # Bits outside the mux are still decoded.
    assert signals["Heartbeat"].status == ACTIVE
    assert signals["Heartbeat"].raw_unsigned == 1


def test_not_applicable_signal_still_reports_its_layout(demo):
    # The bit map must be able to draw *where* the signal would have been,
    # even though the value is withheld.
    msg = demo.message_by_id(0x101, False)
    decoded = decode_message(
        msg, bytes.fromhex("05 11 22 33 44 55 66 01"), "demo_ecu.dbc", 0x101, False)
    rpm = by_name(decoded)["MotorRpm"]
    assert rpm.bit_offsets == list(range(23, 7, -1))
    assert (rpm.start, rpm.length, rpm.byte_order) == (8, 16, "little_endian")


def test_selected_branch_decodes_normally(demo):
    msg = demo.message_by_id(0x101, False)
    payload = bytes.fromhex("01 B0 04 00 00 00 00 01")
    decoded = decode_message(msg, payload, "demo_ecu.dbc", 0x101, False)
    signals = by_name(decoded)
    assert signals["MotorRpm"].status == ACTIVE
    assert signals["MotorRpm"].physical == 1200
    assert signals["ChargeVoltage"].status == NOT_APPLICABLE


def test_nested_multiplexing_reads_only_the_selected_leaf(demo):
    msg = demo.message_by_id(0x102, False)
    # byte 0 = 0x21: Group (low nibble) = 1, Sub (high nibble) = 2
    payload = bytes.fromhex("21 09 07 00 00 00 00 03")
    decoded = decode_message(msg, payload, "demo_ecu.dbc", 0x102, False)
    signals = by_name(decoded)

    assert decoded.mux_values == {"Group": 1, "Sub": 2}
    assert signals["LeafA"].status == ACTIVE
    assert signals["LeafA"].raw_unsigned == 9
    assert signals["LeafB"].status == NOT_APPLICABLE
    assert "Sub=2" in signals["LeafB"].reason
    assert signals["PlainGroup"].raw_unsigned == 7


def test_blocked_nested_selector_poisons_its_children(demo):
    msg = demo.message_by_id(0x102, False)
    payload = bytes.fromhex("55 09 00 00 00 00 00 03")
    decoded = decode_message(msg, payload, "demo_ecu.dbc", 0x102, False)
    signals = by_name(decoded)
    assert decoded.mux_values == {"Group": 5}
    assert "Sub" not in decoded.mux_values
    for name in ("Sub", "LeafA", "LeafB"):
        assert signals[name].status == NOT_APPLICABLE, name
        assert signals[name].raw_unsigned is None
    # The mux resolution events explain the block rather than hiding it.
    blocked = [e for e in decoded.mux_events if e["role"] == "unresolved"]
    assert any(e["signal"] == "Sub" for e in blocked)


def test_extended_frame_decodes_with_motorola_layout(demo):
    msg = demo.message_by_id(0x18FF50E5, True)
    # 400.5 V (raw 4005), -12.34 A (raw -1234 as 16-bit two's complement),
    # 45 degC (raw 85 with offset -40), state 1, tail 0x1234.
    payload = bytes.fromhex("0F A5 FB 2E 55 01 34 12")
    decoded = decode_message(msg, payload, "demo_ecu.dbc", 0x18FF50E5, True)
    assert decoded.is_extended is True
    signals = by_name(decoded)
    assert signals["OutputVoltage"].physical == pytest.approx(400.5)
    assert signals["OutputCurrent"].physical == pytest.approx(-12.34)
    assert signals["Temperature"].physical == pytest.approx(45)
    assert signals["SerialTail"].raw_unsigned == 0x1234


@pytest.mark.parametrize("length", [0, 6, 9, 10, 64])
def test_length_mismatch_is_reported_and_bytes_are_kept(demo, length):
    msg = demo.message_by_id(0x100, False)
    payload = bytes((i * 7) & 0xFF for i in range(length))
    decoded = decode_message(msg, payload, "demo_ecu.dbc", 0x100, False)
    assert decoded.dlc == length
    assert decoded.expected_dlc == 8
    assert decoded.dlc_ok == (length == 8)
    assert decoded.payload_hex == payload.hex(" ").upper()


def test_short_frame_marks_out_of_frame_signals_unreadable(demo):
    msg = demo.message_by_id(0x100, False)
    decoded = decode_message(
        msg, bytes.fromhex("01 02 03 AB CD EF"), "demo_ecu.dbc", 0x100, False)
    signals = by_name(decoded)
    # Signals wholly inside the 6 bytes still decode.
    assert signals["Speed"].status == ACTIVE
    assert signals["EngineTorque"].status == ACTIVE
    # Signals past bit 47 do not, and say which bit was missing.
    for name, bit in (("AuxFlags", 51), ("ThrottlePct", 59),
                      ("LampState", 63)):
        assert signals[name].status == ERROR, name
        assert f"bit {bit}" in signals[name].error
        assert signals[name].raw_unsigned is None


def test_unknown_frame_id_is_not_a_decode_failure(demo):
    decoded = decode_frame(demo, 0x123, False, bytes(8))
    assert decoded.message is None
    assert decoded.message_found is False
    assert decoded.signals == []
    assert any("0x123" in e for e in decoded.frame_errors)


def test_decoded_payload_bytes_match_the_frame(demo):
    msg = demo.message_by_id(0x100, False)
    decoded = decode_message(
        msg, bytes.fromhex("01 02 03 04 05 06 07 08"), "demo_ecu.dbc", 0x100,
        False)
    data = decoded.to_dict()
    # The UI's bit map draws from this; it must be the received bytes, not
    # a padded copy of the definition's length.
    assert data["payload_bytes"] == [1, 2, 3, 4, 5, 6, 7, 8]
