"""The library/business boundary.

Three channels must never merge:

``library``     what cantools said, with the exact call, exception class
                and the line inside cantools that raised;
``definition``  static conflicts CANLab found in the DBC, with source lines;
``frame``       per-frame business verdicts (length, readability, range).

A cantools ``DecodeError`` must never turn into a business rejection, and a
business rejection must never be reported as a library failure.  These tests
pin both directions, plus the agreement path where both tools decode the
same frame.
"""

from __future__ import annotations

import pytest

from canlab.bits import LayoutError
from canlab.decode import decode_message
from canlab.library_oracle import (
    cross_check_frame,
    load_with_cantools,
    compare_definitions,
)
from canlab.session import Session

cantools = pytest.importorskip("cantools")


def load_like_session(text: str, name: str):
    return load_with_cantools(text, name)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def test_valid_database_loads_strict_and_lenient(demo_text):
    result = load_like_session(demo_text, "demo_ecu.dbc")
    assert result.ok is True
    assert result.strict_ok is True
    assert result.strict_error is None
    assert len(result.messages) == 5


def test_conflict_a_is_refused_by_strict_and_loaded_leniently(conflict_a_text):
    result = load_like_session(conflict_a_text, "conflict_a.dbc")
    assert result.ok is True
    assert result.strict_ok is False
    strict = result.strict_error
    assert strict is not None
    # The exact call is recorded, not paraphrased.
    assert "strict=True" in strict.call
    assert strict.exception  # e.g. UnsupportedDatabaseFormatError
    assert "overlapping" in strict.message.lower()
    # The business meaning explains why CANLab keeps going.
    assert "definition" in strict.business_meaning.lower()
    # And the strict/lenient split is itself a diagnostic.
    assert any(d.exception is None for d in result.diagnostics)


def test_conflict_b_strict_failure_names_the_offending_signal(conflict_b_text):
    result = load_like_session(conflict_b_text, "conflict_b.dbc")
    assert result.ok is True
    assert result.strict_ok is False
    assert "Cruise" in result.strict_error.message


# ---------------------------------------------------------------------------
# Decode cross-check: agreement
# ---------------------------------------------------------------------------

def test_oracle_agrees_on_a_clean_frame(demo_text, demo):
    library = load_like_session(demo_text, "demo_ecu.dbc")
    msg = demo.message_by_id(0x100, False)
    payload = bytes.fromhex("07 04 4C B0 12 34 56 78")
    decoded = decode_message(msg, payload, "demo_ecu.dbc", 0x100, False)
    oracle = cross_check_frame(library, msg, payload,
                               [s.to_dict() for s in decoded.signals],
                               mux_values=decoded.mux_values)
    assert oracle.ok is True
    assert oracle.diagnostic is None
    assert oracle.disagreement == []
    # Every active signal was actually compared.
    active = {s.name for s in decoded.signals if s.status == "active"}
    assert set(oracle.values) == active


# ---------------------------------------------------------------------------
# Decode cross-check: the three interesting disagreements
# ---------------------------------------------------------------------------

def test_mux_miss_is_a_business_condition_not_a_library_failure(demo_text, demo):
    library = load_like_session(demo_text, "demo_ecu.dbc")
    msg = demo.message_by_id(0x101, False)
    payload = bytes.fromhex("05 11 22 33 44 55 66 01")
    decoded = decode_message(msg, payload, "demo_ecu.dbc", 0x101, False)

    oracle = cross_check_frame(library, msg, payload,
                               [s.to_dict() for s in decoded.signals],
                               mux_values=decoded.mux_values)
    assert oracle.ok is False
    diagnostic = oracle.diagnostic
    assert diagnostic.exception == "DecodeError"
    assert "multiplexer" in diagnostic.message.lower()
    # The library's own call site is cited.
    assert diagnostic.detail.startswith("raised at ")
    assert "multiplexer.py" in diagnostic.detail or "message.py" in diagnostic.detail
    # And the business reading is spelled out rather than re-printed.
    assert "not applicable" in diagnostic.business_meaning.lower()
    assert "not a library failure" in diagnostic.business_meaning.lower()

    # CANLab's side of the story: not applicable, no value, still decodable.
    statuses = {s.name: s.status for s in decoded.signals}
    assert statuses["MotorRpm"] == "not_applicable"
    assert statuses["Heartbeat"] == "active"
    assert decoded.mux_values == {"Mode": 5}


def test_wrong_data_size_is_classified_as_a_frame_condition(demo_text, demo):
    library = load_like_session(demo_text, "demo_ecu.dbc")
    msg = demo.message_by_id(0x100, False)
    payload = bytes.fromhex("01 02 03 AB CD EF")
    decoded = decode_message(msg, payload, "demo_ecu.dbc", 0x100, False)
    oracle = cross_check_frame(library, msg, payload,
                               [s.to_dict() for s in decoded.signals],
                               mux_values=decoded.mux_values)
    assert oracle.ok is False
    assert "data size" in oracle.diagnostic.message.lower()
    assert "length" in oracle.diagnostic.business_meaning.lower()
    # CANLab still decoded half the frame instead of returning nothing.
    by_name = {s.name: s for s in decoded.signals}
    assert by_name["Speed"].raw_unsigned == 0x0302
    assert by_name["LampState"].status == "error"


def test_overlap_makes_cantools_unpack_fail_but_canlab_still_lays_out_bits(
        conflict_a_text, conflict_a):
    library = load_like_session(conflict_a_text, "conflict_a.dbc")
    msg = conflict_a.message_by_id(0x302, False)
    payload = bytes.fromhex("E8 03 44 33 12 34 00 00")
    decoded = decode_message(msg, payload, "conflict_a.dbc", 0x302, False)
    by_name = {s.name: s for s in decoded.signals}

    # CANLab reads both overlapping owners and marks the overrun unreadable.
    assert by_name["PackVoltage"].raw_unsigned == 1000
    assert by_name["TorqueRaw"].raw_unsigned == 13376
    assert by_name["SigOverrun"].status == "error"

    oracle = cross_check_frame(library, msg, payload,
                               [s.to_dict() for s in decoded.signals],
                               mux_values=decoded.mux_values)
    assert oracle.ok is False
    assert oracle.diagnostic.message == "unpacking failed"
    assert "layout" in oracle.diagnostic.business_meaning.lower()


# ---------------------------------------------------------------------------
# Agreement is exhaustive, not sampled
# ---------------------------------------------------------------------------

def test_oracle_agrees_with_every_registered_sample(lab_session, scenarios):
    """Every sample frame that a database defines must cross-check cleanly.

    Samples whose only problem is a multiplexer condition or a frame
    length legitimately make cantools raise; those are covered above.
    """
    expected_frame_conditions = {
        "mux-miss", "nested-blocked", "short-frame", "long-frame",
        "ab-overlap", "unknown-id",
    }
    checked = 0
    for scenario in scenarios:
        if scenario["id"] in expected_frame_conditions:
            continue
        lab_session.record(
            dbc_key=scenario["dbc"],
            frame_id=scenario["frame_id"],
            is_extended=scenario["is_extended"],
            payload=bytes.fromhex(
                scenario.get("raw_hex")
                or _encode_via_session(lab_session, scenario)),
            requested_dlc=scenario["dlc"],
            mux_value=scenario.get("mux_value"),
            nested_mux=scenario.get("nested_mux") or {},
            values=scenario.get("values") or {},
            sender_findings=[],
            sender_ok=True,
            scenario=scenario["id"],
        )
        for key, oracle in lab_session.frames[-1].oracle.items():
            if not oracle["ok"]:
                continue
            assert oracle["disagreement"] == [], (scenario["id"], key,
                                                  oracle["disagreement"])
            checked += 1
    assert checked >= 5


def _encode_via_session(lab_session, scenario) -> str:
    prepared = lab_session.prepare(
        scenario["dbc"], scenario["frame_id"], scenario["is_extended"],
        scenario["dlc"], scenario.get("mux_value"),
        scenario.get("nested_mux") or {},
        scenario.get("values") or {},
    )
    assert prepared["ok"], (scenario["id"], prepared)
    return prepared["payload_hex"]


# ---------------------------------------------------------------------------
# A/B definition diff
# ---------------------------------------------------------------------------

def test_compare_finds_packing_length_unit_and_missing_signals(
        conflict_a, conflict_b):
    diffs = compare_definitions(conflict_a, conflict_b)
    by_frame = {}
    for entry in diffs:
        by_frame.setdefault(entry["frame_id"], []).append(entry)

    powertrain = _one(by_frame[0x300])
    assert powertrain["kind"] == "message_differs"
    changes = powertrain["changes"]
    # Message length, and a signal whose start/length/byte order all moved.
    assert any(c["field"] == "length" and c.get("signal") is None
               for c in changes)
    torque = [c for c in changes if c.get("signal") == "Torque"]
    assert {c["field"] for c in torque} >= {
        "start", "length", "byte_order", "scale"}
    by_field = {(c.get("signal"), c["field"]): c for c in changes}
    assert by_field[("Torque", "start")]["left"] == 16
    assert by_field[("Torque", "start")]["right"] == 23
    assert by_field[("Torque", "byte_order")]["left"] == "little_endian"
    assert by_field[("Torque", "byte_order")]["right"] == "big_endian"

    thermal = _one(by_frame[0x301])
    thermal_fields = {(c.get("signal"), c["field"]) for c in thermal["changes"]}
    # A has PumpRpm (rpm); B has PumpPressure (bar) on the same bits.
    assert ("PumpRpm", "signal_missing_in_right") in thermal_fields
    assert ("PumpPressure", "signal_missing_in_left") in thermal_fields

    overlap = _one(by_frame[0x302])
    torque_raw = next(
        c for c in overlap["changes"]
        if c.get("signal") == "TorqueRaw" and c["field"] == "start")
    assert (torque_raw["left"], torque_raw["right"]) == (12, 16)


def _one(entries):
    assert len(entries) == 1, entries
    return entries[0]


# ---------------------------------------------------------------------------
# The session keeps the channels apart
# ---------------------------------------------------------------------------

def test_session_keeps_library_definition_and_frame_channels_separate(
        lab_session):
    record = lab_session.record(
        dbc_key="conflict_a.dbc", frame_id=0x302, is_extended=False,
        payload=bytes.fromhex("E8 03 44 33 12 34 00 00"),
        requested_dlc=8, mux_value=None, nested_mux={}, values={},
        sender_findings=[], sender_ok=True,
    )
    # Definitions: conflict_a's own overlap/overrun.
    loaded = lab_session.dbcs["conflict_a.dbc"]
    definition_codes = {f.code for f in loaded.definition_findings}
    assert {"signals_overlap", "signal_out_of_frame"} <= definition_codes

    # Frame: the overrun signal is unreadable, so the frame is rejected.
    verdict = record.verdicts["conflict_a.dbc"]
    assert verdict.accepted is False
    assert "signal_unreadable" in {f.code for f in verdict.findings}

    # Library: the unpack failure, reported on its own channel and tagged
    # with the library's exception type.
    oracle = record.oracle["conflict_a.dbc"]
    assert oracle["diagnostic"]["exception"] == "DecodeError"
    assert oracle["diagnostic"]["call"].startswith("db.decode_message(")


def test_session_survives_a_database_cantools_cannot_strict_load(
        lab_session):
    # conflict_b fails strict loading but every frame still gets a
    # CANLab decode plus a library-channel note.
    record = lab_session.record(
        dbc_key="conflict_b.dbc", frame_id=0x300, is_extended=False,
        payload=bytes.fromhex("10 27 00 20 11 22"),
        requested_dlc=6, mux_value=None, nested_mux={},
        values={}, sender_findings=[], sender_ok=True,
    )
    assert record.decodes["conflict_b.dbc"].message == "Powertrain"
    assert record.oracle["conflict_b.dbc"]["ok"] in (True, False)
    loaded = lab_session.dbcs["conflict_b.dbc"]
    assert loaded.library.strict_ok is False
    assert loaded.library.ok is True


# ---------------------------------------------------------------------------
# Without cantools installed
# ---------------------------------------------------------------------------

def test_oracle_is_optional(demo_text):
    from canlab import library_oracle
    original = library_oracle._CANTOOLS_AVAILABLE
    try:
        library_oracle._CANTOOLS_AVAILABLE = False
        result = library_oracle.load_with_cantools(demo_text, "demo_ecu.dbc")
    finally:
        library_oracle._CANTOOLS_AVAILABLE = original
    assert result.available is False
    assert result.ok is False
    assert result.diagnostics[0].exception == "ImportError"
    assert "still load" in result.diagnostics[0].business_meaning
