"""Business validation: definitions, frames and sender pre-flight.

Every finding must be locatable in two places at once: the definition
(message/signal name *and source line*) and, where it applies, the raw
frame (bytes, bit span).  These tests assert both halves.
"""

from __future__ import annotations

import pytest

from canlab.decode import decode_message
from canlab.validators import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    check_frame,
    validate_database,
    validate_for_send,
)


def codes(findings) -> list[str]:
    return [f.code for f in findings]


# ---------------------------------------------------------------------------
# Definition checks
# ---------------------------------------------------------------------------

def test_valid_database_has_no_findings(demo):
    assert validate_database(demo) == []


def test_overlap_is_located_at_source_lines(conflict_a):
    findings = validate_database(conflict_a)
    overlap = [f for f in findings if f.code == "signals_overlap"]
    assert len(overlap) == 1
    finding = overlap[0]
    assert finding.severity == SEVERITY_ERROR
    loc = finding.location
    assert loc["message"] == "ConflictOverlap"
    assert loc["message_line"] > 0
    # Both owners of the shared bits must be named, with their DBC lines.
    a, b = loc["a"], loc["b"]
    assert {a["signal"], b["signal"]} == {"PackVoltage", "TorqueRaw"}
    assert a["signal_line"] and b["signal_line"]
    # And the shared bit span is stated, so it can be found in the frame.
    assert loc["shared_linear_bits"] == [12, 13, 14, 15]


def test_out_of_frame_signal_is_located(conflict_a):
    findings = [f for f in validate_database(conflict_a)
                if f.code == "signal_out_of_frame"]
    assert len(findings) == 1
    loc = findings[0].location
    assert loc["signal"] == "SigOverrun"
    assert loc["signal_line"] > 0
    assert loc["start"] == 60 and loc["length"] == 8
    # The linear span is what the bit map uses.
    assert max(loc["bit_offsets"]) > 63


def test_conflict_b_declares_messages_outside_its_own_length(conflict_b):
    findings = validate_database(conflict_b)
    out = [f for f in findings if f.code == "signal_out_of_frame"]
    assert {f.location["signal"] for f in out} == {"Cruise", "Reserved", "SigOverrun"}
    assert {f.severity for f in out} == {SEVERITY_ERROR}


def test_length_mismatch_between_definitions_is_visible(conflict_a, conflict_b):
    a = conflict_a.message_by_id(0x300, False)
    b = conflict_b.message_by_id(0x300, False)
    assert (a.length, b.length) == (8, 6)


def test_inverted_limits_are_a_definition_error():
    from canlab.dbc import parse_dbc_text
    db = parse_dbc_text(
        'BO_ 256 M: 8 ECU\n'
        ' SG_ X : 0|8@1+ (1,0) [200|100] "" ECU\n',
        "limits.dbc",
    )
    findings = validate_database(db)
    assert "signal_limits_inverted" in codes(findings)
    assert findings[0].location["signal_line"] == 2


def test_zero_scale_is_a_definition_error():
    from canlab.dbc import parse_dbc_text
    db = parse_dbc_text(
        'BO_ 256 M: 8 ECU\n'
        ' SG_ X : 0|8@1+ (0,0) [0|255] "" ECU\n',
        "scale.dbc",
    )
    assert "signal_zero_scale" in codes(validate_database(db))


def test_missing_multiplexer_parent_is_a_definition_error():
    from canlab.dbc import parse_dbc_text
    db = parse_dbc_text(
        'BO_ 256 M: 8 ECU\n'
        ' SG_ Child m3 : 8|8@1+ (1,0) [0|255] "" ECU\n'
        'SG_MUL_VAL_ 256 Child Ghost 3-3;\n',
        "mux.dbc",
    )
    findings = validate_database(db)
    assert "multiplexer_missing" in codes(findings)
    finding = next(f for f in findings if f.code == "multiplexer_missing")
    assert finding.location["signal"] == "Child"
    assert "Ghost" in finding.message


def test_mux_ids_without_a_parent_are_reported():
    from canlab.dbc import parse_dbc_text
    db = parse_dbc_text(
        'BO_ 256 M: 8 ECU\n'
        ' SG_ Child m3 : 8|8@1+ (1,0) [0|255] "" ECU\n',
        "mux.dbc",
    )
    findings = validate_database(db)
    assert "multiplexer_ids_without_parent" in codes(findings)
    assert findings[0].location["signal"] == "Child"


def test_duplicate_frame_id_is_reported():
    from canlab.dbc import parse_dbc_text
    db = parse_dbc_text(
        'BO_ 256 A: 8 ECU\n SG_ X : 0|8@1+ (1,0) [0|255] "" ECU\n'
        'BO_ 256 B: 8 ECU\n SG_ Y : 0|8@1+ (1,0) [0|255] "" ECU\n',
        "dup.dbc",
    )
    findings = [f for f in validate_database(db)
                if f.code == "duplicate_frame_id"]
    assert len(findings) == 1
    assert sorted(d["line"] for d in findings[0].location["definitions"]) == [1, 3]


def test_mux_siblings_may_share_bits(demo):
    # Mode=1 and Mode=2 branches reuse bytes 1..4 by design.  A naive
    # overlap check would call that a conflict; it is not, because the
    # selector makes the two sets mutually exclusive.
    msg = demo.message_by_id(0x101, False)
    assert msg is not None
    assert validate_database(demo) == []


# ---------------------------------------------------------------------------
# Frame verdicts
# ---------------------------------------------------------------------------

def test_clean_frame_is_accepted(demo):
    msg = demo.message_by_id(0x100, False)
    payload = bytes.fromhex("01 02 03 AB CD EF 11 22")
    decoded = decode_message(msg, payload, "demo_ecu.dbc", 0x100, False)
    verdict = check_frame(msg, payload, decoded)
    assert verdict.accepted is True
    assert verdict.findings == []


def test_frame_length_mismatch_is_rejected_and_located(demo):
    msg = demo.message_by_id(0x100, False)
    payload = bytes.fromhex("01 02 03 AB CD EF")
    decoded = decode_message(msg, payload, "demo_ecu.dbc", 0x100, False)
    verdict = check_frame(msg, payload, decoded)
    assert verdict.accepted is False
    length = [f for f in verdict.findings if f.code == "frame_length_mismatch"]
    assert len(length) == 1
    assert length[0].location["received_length"] == 6
    assert length[0].location["message_length"] == 8
    assert length[0].location["message_line"] > 0
    # The unreadable signals are listed separately, each with its source line.
    unreadable = [f for f in verdict.findings if f.code == "signal_unreadable"]
    assert {f.location["signal"] for f in unreadable} == {
        "AuxFlags", "ThrottlePct", "LampState"}
    assert all(f.location["signal_line"] for f in unreadable)


def test_out_of_range_value_is_findable_but_bits_stay_valid(demo):
    msg = demo.message_by_id(0x100, False)
    # EngineTorque raw 0x7FE = 2046 * 0.5 = 1023 Nm, above the declared 1000.
    payload = bytearray(8)
    payload[4] = 0x7F
    payload[5] = 0xE0
    decoded = decode_message(msg, bytes(payload), "demo_ecu.dbc", 0x100, False)
    verdict = check_frame(msg, bytes(payload), decoded)
    finding = next(f for f in verdict.findings
                   if f.code == "physical_out_of_declared_range")
    # The frame is *readable*: this is a data-quality warning, not a
    # decoding rejection, and the verdict says so by accepting it.
    assert finding.severity == SEVERITY_WARNING
    assert verdict.accepted is True
    assert finding.location["signal"] == "EngineTorque"
    assert finding.location["physical"] == pytest.approx(1023.0)
    assert finding.location["raw_unsigned"] == 0x7FE
    # Bits valid, range violated: the distinction is in the detail text.
    assert "bits are valid" in finding.detail


def test_not_applicable_is_not_a_frame_error(demo):
    # Mode=5 selects no branch.  "Not applicable" is a property of the
    # frame + definition pair, not a rejection; only Mode itself leaves
    # its declared 0..3 range, and that is a warning.
    msg = demo.message_by_id(0x101, False)
    payload = bytes.fromhex("05 11 22 33 44 55 66 01")
    decoded = decode_message(msg, payload, "demo_ecu.dbc", 0x101, False)
    verdict = check_frame(msg, payload, decoded)
    assert verdict.accepted is True
    assert [f.code for f in verdict.findings] == [
        "physical_out_of_declared_range"]
    assert verdict.findings[0].location["signal"] == "Mode"


# ---------------------------------------------------------------------------
# Sender pre-flight
# ---------------------------------------------------------------------------

def test_send_length_difference_is_only_a_warning(demo):
    msg = demo.message_by_id(0x103, False)  # FreeCounter, ends at bit 39
    findings = validate_for_send(msg, 6, {}, None)
    assert "send_length_differs_from_definition" in codes(findings)
    diff = next(f for f in findings
                if f.code == "send_length_differs_from_definition")
    assert diff.severity == SEVERITY_WARNING


def test_send_signal_outside_short_frame_is_an_error(demo):
    msg = demo.message_by_id(0x100, False)
    findings = validate_for_send(msg, 6, {}, None)
    out = [f for f in findings if f.code == "send_signal_outside_frame"]
    assert {f.location["signal"] for f in out} == {
        "AuxFlags", "ThrottlePct", "LampState"}
    assert {f.severity for f in out} == {SEVERITY_ERROR}
    assert all(f.location["requested_length"] == 6 for f in out)


def test_send_refuses_signal_from_unselected_branch(demo):
    msg = demo.message_by_id(0x101, False)
    findings = validate_for_send(msg, 8, {"ChargeVoltage": 400}, mux_value=1)
    assert "send_signal_not_applicable" in codes(findings)
    finding = next(f for f in findings
                   if f.code == "send_signal_not_applicable")
    assert finding.severity == SEVERITY_ERROR
    assert "Mode=1" in finding.message


def test_send_allows_signal_from_selected_branch(demo):
    msg = demo.message_by_id(0x101, False)
    findings = validate_for_send(msg, 8, {"MotorRpm": 1200}, mux_value=1)
    assert findings == []


def test_send_nested_selector_is_checked_against_its_own_parent(demo):
    msg = demo.message_by_id(0x102, False)
    # Sub=2 with Group=1: LeafA is applicable.
    assert validate_for_send(msg, 8, {"LeafA": 9}, mux_value=1,
                             nested_mux={"Sub": 2}) == []
    # Sub=3: LeafA is not.
    findings = validate_for_send(msg, 8, {"LeafA": 9}, mux_value=1,
                                 nested_mux={"Sub": 3})
    assert "send_signal_not_applicable" in codes(findings)
    assert "Sub=3" in findings[0].message


def test_send_range_violation_is_a_warning_not_a_rejection(demo):
    msg = demo.message_by_id(0x100, False)
    findings = validate_for_send(msg, 8, {"EngineTorque": 1010}, None)
    above = [f for f in findings
             if f.code == "send_value_above_defined_maximum"]
    assert len(above) == 1
    assert above[0].severity == SEVERITY_WARNING


def test_send_invalid_length_is_an_error(demo):
    msg = demo.message_by_id(0x100, False)
    findings = validate_for_send(msg, 65, {}, None)
    assert "send_length_invalid" in codes(findings)
    assert next(f for f in findings
                if f.code == "send_length_invalid").severity == SEVERITY_ERROR
