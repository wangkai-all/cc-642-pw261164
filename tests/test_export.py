"""Export: raw, physical and enum are separate and the choice is explicit."""

from __future__ import annotations

import csv
import io
import json

import pytest

from canlab.export import COLUMN_GROUPS, DEFAULT_GROUPS, export_frames_csv, \
    export_frames_json


@pytest.fixture()
def recorded(lab_session):
    """Three frames: a clean one, a mux miss, and a short frame."""
    lab_session.send("demo_ecu.dbc", 0x100, False, 8, None, {},
                     {"Counter": 7, "Speed": 88.88, "CoolantTemp": 85,
                      "EngineTorque": -123.5, "AuxFlags": 5,
                      "ThrottlePct": 42, "LampState": 2},
                     label="nominal", scenario="nominal")
    lab_session.send("demo_ecu.dbc", 0x101, False, 8, 5, {}, {},
                     label="mux miss", scenario="mux-miss", force=True)
    lab_session.send("demo_ecu.dbc", 0x100, False, 6, None, {},
                     {"Counter": 1}, label="short", scenario="short",
                     force=True)
    return [f.to_dict() for f in lab_session.frames]


def parse_csv(text: str) -> tuple[list[str], list[dict]]:
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    rows = list(reader)
    return list(reader.fieldnames or []), rows


def test_header_comment_states_whether_raw_is_included(recorded):
    with_raw = export_frames_csv(recorded, ["identity", "raw", "physical"])
    without_raw = export_frames_csv(recorded, ["identity", "physical"])
    assert "raw: included" in with_raw.splitlines()[0]
    assert "raw: omitted" in without_raw.splitlines()[0]
    # The comment also explains why NA rows are blank, not zero.
    assert "not_applicable" in with_raw.splitlines()[0]


def test_raw_enums_and_physical_are_distinct_columns(recorded):
    header, rows = parse_csv(export_frames_csv(
        recorded, ["identity", "raw", "physical", "enum", "layout"]))
    for column in ("raw_unsigned", "raw_signed", "physical", "unit",
                   "enum_text", "enum_known", "scale", "offset",
                   "bit_offsets", "byte_order"):
        assert column in header

    lamp = next(r for r in rows if r["signal"] == "LampState"
                and r["scenario"] == "nominal")
    assert lamp["raw_unsigned"] == "2"
    assert lamp["raw_signed"] == "2"
    assert lamp["physical"] == "2.0"
    assert lamp["enum_text"] == "Blink"
    assert lamp["enum_known"] == "true"

    torque = next(r for r in rows if r["signal"] == "EngineTorque"
                  and r["scenario"] == "nominal")
    assert torque["raw_unsigned"] == "3849"
    assert torque["raw_signed"] == "-247"
    assert float(torque["physical"]) == pytest.approx(-123.5)
    assert torque["unit"] == "Nm"


def test_without_raw_the_raw_columns_are_absent(recorded):
    header, _ = parse_csv(export_frames_csv(
        recorded, ["identity", "physical", "enum", "status"]))
    assert "raw_unsigned" not in header
    assert "raw_signed" not in header
    assert "physical" in header


def test_na_rows_have_no_raw_or_physical_value(recorded):
    _, rows = parse_csv(export_frames_csv(
        recorded, ["identity", "raw", "physical", "status"]))
    na = [r for r in rows
          if r["scenario"] == "mux-miss" and r["status"] == "not_applicable"]
    assert na, "the mux-miss frame must export its not-applicable rows"
    assert {r["signal"] for r in na} == {
        "MotorRpm", "MotorCurrent", "ChargeVoltage", "ChargeTimer",
        "ServiceCode"}
    for row in na:
        assert row["raw_unsigned"] == ""
        assert row["raw_signed"] == ""
        assert row["physical"] == ""
        assert "Mode=5" in row["reason"]


def test_every_row_names_its_database_and_signal(recorded):
    _, rows = parse_csv(export_frames_csv(recorded, ["identity", "status"]))
    assert rows
    for row in rows:
        assert row["dbc"]
        assert row["signal"]
        assert row["frame_seq"]
        assert row["message"]
    # Three databases decode each frame, so every frame contributes
    # one set of rows per database that defines the message.
    scenarios = {r["scenario"] for r in rows}
    assert scenarios == {"nominal", "mux-miss", "short"}


def test_csv_without_raw_preserves_every_value_that_was_included(recorded):
    with_raw, rows_with = parse_csv(export_frames_csv(
        recorded, ["identity", "raw", "physical"]))
    _, rows_without = parse_csv(export_frames_csv(
        recorded, ["identity", "physical"]))
    assert len(rows_with) == len(rows_without)
    key = lambda r: (r["frame_seq"], r["dbc"], r["signal"])
    by_key = {key(r): r for r in rows_with}
    for row in rows_without:
        assert row["physical"] == by_key[key(row)]["physical"]


def test_json_export_marks_raw_inclusion_and_keeps_databases_apart(recorded):
    payload = json.loads(export_frames_csv_json(recorded,
                                                ["identity", "raw"]))
    assert payload["format"] == "canlab.frames.v1"
    assert payload["raw_included"] is True
    frame = payload["frames"][0]
    assert "payload_hex" not in frame  # the frame group was not selected
    signal = frame["databases"]["demo_ecu.dbc"]["signals"][0]
    assert "raw_unsigned" in signal

    payload = json.loads(export_frames_csv_json(recorded, ["identity"]))
    assert payload["raw_included"] is False
    signal = payload["frames"][0]["databases"]["demo_ecu.dbc"]["signals"][0]
    assert "raw_unsigned" not in signal
    assert "physical" not in signal


def test_unknown_groups_are_ignored(recorded):
    header, _ = parse_csv(export_frames_csv(recorded, ["identity", "nonsense"]))
    assert header[0] == "frame_seq"
    assert "nonsense" not in header


def test_default_groups_leave_raw_out(recorded):
    assert "raw" not in DEFAULT_GROUPS
    header, _ = parse_csv(export_frames_csv(recorded, DEFAULT_GROUPS))
    assert "raw_unsigned" not in header
    assert "physical" in header
    assert "enum_text" in header


def test_each_column_group_is_disjoint_and_known():
    seen: dict[str, str] = {}
    for group, columns in COLUMN_GROUPS.items():
        for column in columns:
            assert column not in seen, (column, group, seen.get(column))
            seen[column] = group


def export_frames_csv_json(frames, groups):
    return export_frames_json(frames, groups)
