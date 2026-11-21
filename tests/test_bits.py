"""Bit layout: the one thing everything else depends on.

Two kinds of checks live here:

* fixed vectors with a hand-computed expected bit set, which pin down the
  Intel/Motorola mapping independently of any library;
* an exhaustive parity sweep against cantools for every start/length/order/
  sign combination that fits in 8 bytes.  cantools is imported only for
  this test; the suite skips it when the optional dependency is absent.
"""

from __future__ import annotations

import pytest

from canlab.bits import (
    SignalDef,
    apply_sign,
    extract_raw,
    network_bit_of,
    physical_to_raw,
    raw_to_physical,
    sawtooth_to_bit_offset,
    signal_bit_offsets,
    write_signal_bits,
)


# ---------------------------------------------------------------------------
# Fixed vectors
# ---------------------------------------------------------------------------

def test_linear_offset_of_dbc_start_is_the_lsb():
    # Intel start 8, length 16 -> bytes 1..2, MSB first.
    sig = SignalDef("Speed", start=8, length=16)
    assert signal_bit_offsets(sig) == list(range(23, 7, -1))


def test_motorola_start_is_the_msb_in_sawtooth_notation():
    # DBC start 39 = byte 4, bit 7 (sawtooth) = linear bit 32.
    # 12 bits walk backwards through sawtooth order: 39..32, then 47..44.
    sig = SignalDef("Torque", start=39, length=12, byte_order="big_endian")
    assert signal_bit_offsets(sig) == [39, 38, 37, 36, 35, 34, 33, 32,
                                       47, 46, 45, 44]


def test_motorola_full_byte_pack():
    # start 7 = MSB of byte 0; 16 bits cover bytes 0 and 1 MSB-first.
    sig = SignalDef("V", start=7, length=16, byte_order="big_endian")
    assert signal_bit_offsets(sig) == list(range(7, -1, -1)) + list(range(15, 7, -1))


def test_sawtooth_and_network_conversions_agree():
    for bit in range(64):
        assert sawtooth_to_bit_offset(bit) == network_bit_of(bit)
        # Byte/bit decomposition is the same statement written twice.
        assert network_bit_of(bit) == 8 * (bit // 8) + (7 - bit % 8)


def test_zero_length_signal_has_no_bits():
    assert signal_bit_offsets(SignalDef("X", start=0, length=0)) == []


# ---------------------------------------------------------------------------
# Hand-computed extraction
# ---------------------------------------------------------------------------

def test_intel_extraction_is_little_endian():
    # 0x1234 in bytes 0..1 little endian is "34 12".
    sig = SignalDef("Speed", start=0, length=16)
    assert extract_raw(sig, bytes.fromhex("34 12")) == 0x1234


def test_motorola_extraction_is_big_endian():
    # Same 0x1234, big endian bytes "12 34".
    sig = SignalDef("Torque", start=7, length=16, byte_order="big_endian")
    assert extract_raw(sig, bytes.fromhex("12 34")) == 0x1234


def test_motorola_does_not_cross_a_byte_boundary_when_it_fits():
    # start 3, length 4 -> the low nibble of byte 0, MSB first.
    sig = SignalDef("Nibble", start=3, length=4, byte_order="big_endian")
    assert signal_bit_offsets(sig) == [3, 2, 1, 0]
    assert extract_raw(sig, bytes([0b0000_1010])) == 0b1010


def test_signed_extension():
    assert apply_sign(0b1111, 4, True) == -1
    assert apply_sign(0b1000, 4, True) == -8
    assert apply_sign(0b0111, 4, True) == 7
    assert apply_sign(0b1111, 4, False) == 15


def test_scale_and_offset():
    sig = SignalDef("Current", start=0, length=12, scale=0.1, offset=-100)
    assert raw_to_physical(1000, sig) == pytest.approx(0.0)
    assert raw_to_physical(0, sig) == pytest.approx(-100.0)


def test_physical_to_raw_rounds_binary32_values():
    # 42 % with scale 0.4 is exactly 105 raw.
    sig = SignalDef("Throttle", start=0, length=8, scale=0.4)
    assert physical_to_raw(42.0, sig) == 105
    # 0.01 scale, 88.88 km/h -> 8888.
    speed = SignalDef("Speed", start=0, length=16, scale=0.01)
    assert physical_to_raw(88.88, speed) == 8888


def test_physical_to_raw_rejects_negative_unsigned():
    from canlab.bits import LayoutError
    sig = SignalDef("U", start=0, length=8)
    with pytest.raises(LayoutError):
        physical_to_raw(-1, sig)


def test_write_signal_bits_round_trips_extract():
    payload = bytearray(8)
    for byte_order in ("little_endian", "big_endian"):
        for start in range(0, 64, 3):
            for length in (1, 3, 8, 12):
                sig = SignalDef(f"S{start}_{length}", start=start,
                                length=length, byte_order=byte_order)
                offsets = signal_bit_offsets(sig)
                if not offsets or max(offsets) >= 64:
                    continue
                write_signal_bits(sig, 0b1011 & ((1 << length) - 1), payload)
                assert extract_raw(sig, bytes(payload)) == (
                    0b1011 & ((1 << length) - 1)), (byte_order, start, length)


# ---------------------------------------------------------------------------
# Exhaustive parity with cantools
# ---------------------------------------------------------------------------

def _cantools_db():
    cantools = pytest.importorskip("cantools")
    return cantools


def test_parity_with_cantools_over_all_layouts():
    """Every start/length/order/sign combination that fits in 8 bytes."""
    cantools = _cantools_db()

    template = """
VERSION ""
NS_ :
    CM_
    VAL_

BS_:

BU_: ECU

BO_ 256 Probe: 8 ECU
{signals}
"""
    checked = 0
    payloads = [bytes([0x00] * 8), bytes.fromhex("01 23 45 67 89 AB CD EF"),
                bytes.fromhex("FF FF FF FF FF FF FF FF"),
                bytes.fromhex("80 00 80 00 80 00 80 00")]
    for byte_order, suffix in (("little_endian", "1"), ("big_endian", "0")):
        for is_signed, sign in ((False, "+"), (True, "-")):
            for length in (1, 2, 4, 7, 8, 9, 12, 13, 16, 24, 31, 32):
                starts = range(0, 64) if byte_order == "little_endian" else range(0, 64)
                lines = []
                names = []
                for index, start in enumerate(starts):
                    name = f"S{index}"
                    sig = SignalDef(name, start=start, length=length,
                                    byte_order=byte_order, is_signed=is_signed)
                    offsets = signal_bit_offsets(sig)
                    if not offsets or max(offsets) > 63:
                        continue
                    lines.append(
                        f' SG_ {name} : {start}|{length}@{suffix}{sign} (1,0) '
                        f'[0|1000] "" ECU'
                    )
                    names.append((name, sig))
                if not lines:
                    continue
                db = cantools.database.load_string(
                    template.format(signals="\n".join(lines)),
                    database_format="dbc", strict=False,
                )
                message = db.get_message_by_frame_id(256)
                for payload in payloads:
                    theirs = message.decode(payload, decode_choices=False)
                    for name, sig in names:
                        ours = apply_sign(extract_raw(sig, payload),
                                          sig.length, is_signed)
                        assert ours == theirs[name], (
                            byte_order, is_signed, length, sig.start,
                            payload.hex())
                        checked += 1
    # Guard against the sweep silently shrinking to a handful of cases.
    assert checked > 5000, checked
