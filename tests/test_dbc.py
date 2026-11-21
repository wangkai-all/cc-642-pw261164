"""Parsing the DBC dialect: frame ids, multiplexing, choices, comments."""

from __future__ import annotations

import pytest

from canlab.dbc import parse_dbc_text, split_frame_id


def test_extended_frame_id_high_bit():
    assert split_frame_id(0x80000000 | 0x18FF50E5) == (0x18FF50E5, True, None)


def test_bare_id_above_0x7ff_is_extended_with_a_warning():
    frame_id, extended, warning = split_frame_id(0x1ABC)
    assert (frame_id, extended) == (0x1ABC, True)
    assert warning and "extended" in warning


def test_standard_frame_id_stays_standard():
    assert split_frame_id(0x100) == (0x100, False, None)


def test_demo_messages_and_kinds(demo):
    by_id = {m.frame_id: m for m in demo.messages}
    assert set(by_id) == {0x100, 0x101, 0x102, 0x103, 0x18FF50E5}
    assert by_id[0x100].is_extended is False
    assert by_id[0x18FF50E5].is_extended is True
    assert by_id[0x100].length == 8
    assert by_id[0x100].line > 0


def test_signal_fields_are_parsed(demo):
    msg = demo.message_by_id(0x100, False)
    torque = next(s for s in msg.signals if s.name == "EngineTorque")
    assert (torque.start, torque.length) == (39, 12)
    assert torque.byte_order == "big_endian"
    assert torque.is_signed is True
    assert torque.scale == pytest.approx(0.5)
    assert (torque.minimum, torque.maximum) == (-1000.0, 1000.0)
    assert torque.unit == "Nm"


def test_val_blocks_become_text_choices(demo):
    msg = demo.message_by_id(0x100, False)
    lamp = next(s for s in msg.signals if s.name == "LampState")
    assert lamp.choices == {0: "Off", 1: "On", 2: "Blink", 3: "Fault"}


def test_simple_multiplexing(demo):
    msg = demo.message_by_id(0x101, False)
    mode = next(s for s in msg.signals if s.name == "Mode")
    assert mode.is_multiplexer is True
    assert mode.multiplexer_signal is None
    rpm = next(s for s in msg.signals if s.name == "MotorRpm")
    assert rpm.multiplexer_signal == "Mode"
    assert rpm.multiplexer_ids == (1,)
    voltage = next(s for s in msg.signals if s.name == "ChargeVoltage")
    assert voltage.multiplexer_ids == (2,)


def test_nested_multiplexing_from_sg_mul_val(demo):
    msg = demo.message_by_id(0x102, False)
    sub = next(s for s in msg.signals if s.name == "Sub")
    assert sub.is_multiplexer is True
    assert sub.multiplexer_signal == "Group"
    assert sub.multiplexer_ids == (1,)
    leaf_a = next(s for s in msg.signals if s.name == "LeafA")
    assert leaf_a.multiplexer_signal == "Sub"
    assert leaf_a.multiplexer_ids == (2,)
    leaf_b = next(s for s in msg.signals if s.name == "LeafB")
    assert leaf_b.multiplexer_signal == "Sub"
    assert leaf_b.multiplexer_ids == (3,)


def test_comments_and_source_lines(demo):
    msg = demo.message_by_id(0x101, False)
    assert "Multiplexed command frame" in msg.comment
    # Every signal has a bookmark, so findings can cite a line.
    for sig in msg.signals:
        assert sig.name in msg.bookmarks
        assert msg.bookmarks[sig.name].line > msg.line


def test_ns_block_entries_do_not_warn(demo):
    # A standard NS_ block lists keywords by indentation; none of those
    # lines is a statement and none should produce a parse warning.
    assert demo.warnings == []


def test_conflict_a_parses_despite_conflicts(conflict_a):
    msg = conflict_a.message_by_id(0x302, False)
    assert msg is not None
    assert {s.name for s in msg.signals} == {"PackVoltage", "TorqueRaw", "SigOverrun"}


def test_malformed_lines_are_reported_not_fatal():
    db = parse_dbc_text(
        'BO_ 256 Good: 8 ECU\n SG_ A : 0|8@1+ (1,0) [0|255] "" ECU\n'
        'SG_ Broken : not-a-layout\n',
        "bad.dbc",
    )
    assert len(db.messages) == 1
    assert any("line 3" in w for w in db.warnings)


def test_metadata_statements_are_skipped_without_warning():
    db = parse_dbc_text(
        'BO_ 256 Good: 8 ECU\n SG_ A : 0|8@1+ (1,0) [0|255] "" ECU\n'
        'BA_ "GenMsgCycleTime" BO_ 256 100;\n'
        'SIG_GROUP_ 256 Group 1 : A;\n',
        "meta.dbc",
    )
    assert len(db.messages) == 1
    assert db.warnings == []
