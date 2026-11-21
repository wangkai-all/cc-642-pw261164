"""The local sender: pre-flight, encoding and recording.

The sender's contract is that the preview and the sent frame come from the
same code path, so a preview cannot disagree with what is stored.  These
tests exercise ``Session.prepare`` and ``Session.send`` directly, plus the
negative boundary and frame-length controls the page exposes.
"""

from __future__ import annotations

import pytest

from canlab.session import Session


def prepare(session: Session, **overrides):
    args = dict(
        dbc_key="demo_ecu.dbc", frame_id=0x100, is_extended=False, dlc=8,
        mux_value=None, nested_mux={}, values={}, force=False,
    )
    args.update(overrides)
    return session.prepare(**args)


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def test_intel_and_motorola_signals_are_encoded_together(lab_session):
    prepared = prepare(lab_session, values={
        "Counter": 1, "Speed": 88.88, "CoolantTemp": 85,
        "EngineTorque": -123.5, "AuxFlags": 5, "ThrottlePct": 42,
        "LampState": 2,
    })
    assert prepared["ok"] is True, prepared
    # Counter=1, Speed=8888 (0x22B8) Intel, CoolantTemp=125 (85+40),
    # EngineTorque=-247 signed 12-bit Motorola, AuxFlags=5, Throttle=105,
    # LampState=2.
    assert prepared["payload_hex"] == "01 B8 22 7D F0 90 95 26"
    assert prepared["findings"] == []


def test_round_trip_through_the_decoder(lab_session):
    """Encode with the sender, decode with the receiver: same values."""
    prepared = prepare(lab_session, values={
        "CoolantTemp": 85, "EngineTorque": -123.5, "Speed": 88.88,
    })
    assert prepared["ok"] is True
    payload = prepared["payload"]
    loaded = lab_session.dbcs["demo_ecu.dbc"]
    msg = loaded.db.message_by_id(0x100, False)
    from canlab.decode import decode_message
    decoded = decode_message(msg, payload, "demo_ecu.dbc", 0x100, False)
    by_name = {s.name: s for s in decoded.signals}
    assert by_name["CoolantTemp"].physical == pytest.approx(85)
    assert by_name["EngineTorque"].physical == pytest.approx(-123.5)
    assert by_name["Speed"].physical == pytest.approx(88.88)


def test_multiplexer_value_selects_the_branch(lab_session):
    prepared = prepare(lab_session, frame_id=0x101, mux_value=2, values={
        "ChargeVoltage": 400, "ChargeTimer": 30,
    })
    assert prepared["ok"] is True
    payload = prepared["payload"]
    assert payload[0] == 2
    # ChargeVoltage 400 V / 0.05 = raw 8000 = 0x1F40, Intel at bit 8.
    assert payload[1] == 0x40
    assert payload[2] == 0x1F


def test_selected_branch_round_trips_not_applicable_on_the_other(lab_session):
    prepared = prepare(lab_session, frame_id=0x101, mux_value=1,
                       values={"MotorRpm": 1200})
    assert prepared["ok"] is True
    loaded = lab_session.dbcs["demo_ecu.dbc"]
    msg = loaded.db.message_by_id(0x101, False)
    from canlab.decode import decode_message
    decoded = decode_message(msg, prepared["payload"], "demo_ecu.dbc",
                             0x101, False)
    by_name = {s.name: s for s in decoded.signals}
    assert by_name["MotorRpm"].physical == 1200
    assert by_name["ChargeVoltage"].status == "not_applicable"
    assert by_name["ChargeVoltage"].raw_unsigned is None


# ---------------------------------------------------------------------------
# Rejections: not applicable, out of frame, out of range
# ---------------------------------------------------------------------------

def test_sender_refuses_a_signal_the_selector_did_not_choose(lab_session):
    prepared = prepare(lab_session, frame_id=0x101, mux_value=1,
                       values={"ChargeVoltage": 400})
    assert prepared["ok"] is False
    assert prepared["stage"] == "preflight"
    codes = {f["code"] for f in prepared["findings"]}
    assert "send_signal_not_applicable" in codes


def test_force_still_refuses_to_write_unselected_bits(lab_session):
    """``force`` overrides frame-length errors, never mux meaning."""
    prepared = prepare(lab_session, frame_id=0x101, mux_value=1,
                       values={"ChargeVoltage": 400}, force=True)
    assert prepared["ok"] is False
    assert prepared["stage"] == "encode"
    assert "not applicable" in prepared["findings"][-1]["message"]


def test_short_frame_is_refused_with_each_lost_signal_located(lab_session):
    prepared = prepare(lab_session, dlc=6, values={"AuxFlags": 0})
    assert prepared["ok"] is False
    assert prepared["stage"] == "preflight"
    out = [f for f in prepared["findings"]
           if f["code"] == "send_signal_outside_frame"]
    assert {f["location"]["signal"] for f in out} == {
        "AuxFlags", "ThrottlePct", "LampState"}
    # Each finding points at a DBC source line and at the requested length.
    assert all(f["location"]["signal_line"] for f in out)
    assert all(f["location"]["requested_length"] == 6 for f in out)


def test_force_emits_the_short_frame_without_inventing_values(lab_session):
    prepared = prepare(lab_session, dlc=6, force=True, values={
        "Counter": 9, "AuxFlags": 0, "ThrottlePct": 0, "LampState": 0,
    })
    assert prepared["ok"] is True
    assert len(prepared["payload"]) == 6
    assert prepared["payload"][0] == 9
    # The out-of-frame signals were dropped; their bits do not exist, so
    # they cannot have been written as zero.
    payload = prepared["payload"]
    assert len(payload) == 6


def test_value_beyond_declared_maximum_is_a_warning_not_a_rejection(
        lab_session):
    prepared = prepare(lab_session, values={"EngineTorque": 1010})
    assert prepared["ok"] is True
    codes = {f["code"] for f in prepared["findings"]}
    assert "send_value_above_defined_maximum" in codes


def test_value_not_encodable_in_its_bit_width_is_an_encode_rejection(
        lab_session):
    prepared = prepare(lab_session, values={"CoolantTemp": 1000})
    assert prepared["ok"] is False
    assert prepared["stage"] == "encode"
    assert prepared["findings"][-1]["code"] == "encode_rejected"


def test_invalid_frame_length_is_refused(lab_session):
    prepared = prepare(lab_session, dlc=65)
    assert prepared["ok"] is False
    assert {f["code"] for f in prepared["findings"]} >= {"send_length_invalid"}


def test_unknown_database_and_message_are_reported_in_the_session_stage(
        lab_session):
    missing_db = prepare(lab_session, dbc_key="nope.dbc")
    assert missing_db["stage"] == "session"
    assert missing_db["findings"][0]["code"] == "dbc_not_loaded"

    missing_msg = prepare(lab_session, frame_id=0x7FF)
    assert missing_msg["stage"] == "session"
    finding = missing_msg["findings"][0]
    assert finding["code"] == "message_not_defined"
    assert finding["location"]["available"]


# ---------------------------------------------------------------------------
# Nested multiplexing
# ---------------------------------------------------------------------------

def test_nested_sender_writes_the_leaf_for_the_selected_sub_branch(
        lab_session):
    prepared = prepare(lab_session, frame_id=0x102, mux_value=1,
                       nested_mux={"Sub": 2},
                       values={"Sub": 2, "LeafA": 9})
    assert prepared["ok"] is True, prepared
    assert prepared["payload"][0] & 0x0F == 1   # Group = 1
    assert (prepared["payload"][0] >> 4) & 0x0F == 2  # Sub = 2
    assert prepared["payload"][1] == 9


def test_nested_sender_refuses_a_leaf_from_another_sub_branch(lab_session):
    prepared = prepare(lab_session, frame_id=0x102, mux_value=1,
                       nested_mux={"Sub": 3}, values={"LeafA": 9})
    assert prepared["ok"] is False
    finding = next(f for f in prepared["findings"]
                   if f["code"] == "send_signal_not_applicable")
    assert "Sub=3" in finding["message"]


# ---------------------------------------------------------------------------
# Negative boundary control
# ---------------------------------------------------------------------------

def test_most_negative_raw_boundary_encodes_as_the_sign_bit_only(lab_session):
    # EngineTorque is signed 12 bits; the most negative raw is -2048,
    # which is physical -1024 Nm.  Only the top bit of the 12 is set.
    prepared = prepare(lab_session, values={"EngineTorque": -1024})
    assert prepared["ok"] is True
    # bits 32..47 = 0x8000 -> byte 4 = 0x80 with the rest of the word 0.
    assert prepared["payload"][4] == 0x80
    # And the encoder flagged the value as below the declared minimum.
    assert {f["code"] for f in prepared["findings"]} == {
        "send_value_below_defined_minimum"}


def test_unsigned_signal_refuses_a_negative_value(lab_session):
    prepared = prepare(lab_session, values={"Speed": -1})
    assert prepared["ok"] is False
    assert prepared["stage"] == "encode"


# ---------------------------------------------------------------------------
# Session bookkeeping
# ---------------------------------------------------------------------------

def test_preview_does_not_record_and_send_does(lab_session):
    before = len(lab_session.frames)
    prepare(lab_session, values={"Counter": 3})
    assert len(lab_session.frames) == before

    result = lab_session.send(
        "demo_ecu.dbc", 0x100, False, 8, None, {},
        {"Counter": 3}, scenario="test",
    )
    assert result["ok"] is True
    assert result["stage"] == "sent"
    assert len(lab_session.frames) == before + 1
    frame = result["frame"]
    assert frame["payload_hex"].startswith("03")
    assert frame["scenario"] == "test"
    # The recorded frame carries a decode and a verdict from every loaded DBC.
    assert set(frame["decodes"]) == set(lab_session.dbcs)
    assert "demo_ecu.dbc" in frame["verdicts"]


def test_rejected_send_records_nothing(lab_session):
    before = len(lab_session.frames)
    result = lab_session.send(
        "demo_ecu.dbc", 0x101, False, 8, 1, {},
        {"ChargeVoltage": 400},
    )
    assert result["ok"] is False
    assert len(lab_session.frames) == before


def test_redecoding_happens_when_a_database_is_added_or_removed(
        demo_text, conflict_a_text):
    session = Session()
    session.add_dbc("demo_ecu.dbc", demo_text)
    session.send("demo_ecu.dbc", 0x100, False, 8, None, {},
                 {"Counter": 1})
    frame = session.frames[0]
    assert set(frame.decodes) == {"demo_ecu.dbc"}
    # conflict_a does not define 0x100: it is added as an unknown message,
    # not as a decode failure.
    session.add_dbc("conflict_a.dbc", conflict_a_text)
    assert set(frame.decodes) == {"demo_ecu.dbc", "conflict_a.dbc"}
    other = frame.decodes["conflict_a.dbc"]
    assert other.message_found is False
    assert "0x100" in other.frame_errors[0]

    session.remove_dbc("conflict_a.dbc")
    assert set(frame.decodes) == {"demo_ecu.dbc"}


def test_frames_are_bounded_but_never_silently_dropped_before_that(
        demo_text):
    session = Session()
    session.add_dbc("demo_ecu.dbc", demo_text)
    session.max_frames = 3
    for n in range(5):
        session.send("demo_ecu.dbc", 0x100, False, 8, None, {},
                     {"Counter": n})
    assert len(session.frames) == 3
    assert [f.payload[0] for f in session.frames] == [2, 3, 4]
