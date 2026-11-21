"""Export recorded frames with an explicit choice of columns.

Three representations of the same signal are stored separately on every
frame:

``raw``     the unsigned integer exactly as it appears in the frame
``signed``  the same bits after two's-complement extension
``physical`` raw * scale + offset
``enum``    the text name for that raw value, if the DBC defines one

The export asks which of them to include.  A CSV without raw values is a
view for a data analyst; one with raw values is an audit trail that can be
replayed against the DBC later.  Neither is "the" export, so the choice is
explicit and recorded in the header.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Iterable


COLUMN_GROUPS: dict[str, list[str]] = {
    "identity": ["seq", "time_text", "scenario", "label", "source", "dbc",
                 "message", "frame_id", "is_extended", "dlc"],
    "frame": ["payload_hex", "payload_bytes", "payload_ascii"],
    "raw": ["raw_unsigned", "raw_signed"],
    "physical": ["physical", "unit"],
    "enum": ["enum_text", "enum_known"],
    "layout": ["start", "length", "byte_order", "scale", "offset",
               "bit_offsets", "bit_text"],
    "status": ["status", "reason", "in_defined_range", "accepted",
               "verdict_codes", "library_error"],
}


DEFAULT_GROUPS = ["identity", "frame", "physical", "enum", "status"]


def _signal_columns(groups: Iterable[str]) -> list[str]:
    columns: list[str] = []
    for group in groups:
        columns.extend(COLUMN_GROUPS[group])
    return columns


def export_frames_csv(frames: list[dict], groups: list[str],
                      dbc_filter: list[str] | None = None,
                      include_header_comment: bool = True) -> str:
    """One row per (frame, database, signal) so A/B rows line up.

    The frame-level identity columns are read from the frame; everything
    else is read from the decoded signal.  ``status`` and ``reason`` keep
    the signal's applicability, which is why a not-applicable row has an
    empty raw value rather than a fabricated zero.
    """
    groups = [g for g in groups if g in COLUMN_GROUPS]
    selected = _signal_columns(groups)
    frame_columns = ["time_text", "scenario", "label", "source", "dbc",
                     "message", "frame_id", "is_extended", "dlc"]
    if "frame" in groups:
        frame_columns += ["payload_hex", "payload_bytes", "payload_ascii"]
    signal_columns = [
        c for c in selected
        if c not in ("seq",) + tuple(frame_columns)
    ]
    header = ["frame_seq"] + [
        c for c in frame_columns if c in selected
    ] + ["signal"] + signal_columns

    buffer = io.StringIO()
    if include_header_comment:
        buffer.write(
            "# CANLab export | columns: "
            + ",".join(groups)
            + " | raw: "
            + ("included" if "raw" in groups else "omitted")
            + " | one row per frame x database x signal; "
            + "not_applicable rows carry no raw/physical value by design\n"
        )
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)

    for frame in frames:
        if dbc_filter and frame["dbc_key"] not in dbc_filter:
            continue
        for dbc_key, decoded in frame.get("decodes", {}).items():
            if dbc_filter and dbc_key not in dbc_filter:
                continue
            verdict = frame.get("verdicts", {}).get(dbc_key)
            oracle = frame.get("oracle", {}).get(dbc_key) or {}
            for sig in decoded["signals"]:
                row = {
                    "frame_seq": frame["seq"],
                    "time_text": frame["time_text"],
                    "scenario": frame.get("scenario", ""),
                    "label": frame.get("label", ""),
                    "source": frame.get("source", ""),
                    "dbc": dbc_key,
                    "message": decoded.get("message") or "",
                    "frame_id": frame["frame_id_hex"],
                    "is_extended": frame["is_extended"],
                    "dlc": frame["dlc"],
                    "payload_hex": frame["payload_hex"],
                    "payload_bytes": " ".join(str(b) for b in frame["payload_bytes"]),
                    "payload_ascii": frame["payload_ascii"],
                    "signal": sig["name"],
                    "raw_unsigned": _blank(sig.get("raw_unsigned")),
                    "raw_signed": _blank(sig.get("raw_signed")),
                    "physical": _blank(sig.get("physical")),
                    "unit": sig.get("unit", ""),
                    "enum_text": sig.get("enum_text") or "",
                    "enum_known": _blank(sig.get("enum_known")),
                    "start": sig["start"],
                    "length": sig["length"],
                    "byte_order": sig["byte_order"],
                    "scale": sig["scale"],
                    "offset": sig["offset"],
                    "bit_offsets": " ".join(str(b) for b in sig["bit_offsets"]),
                    "bit_text": sig.get("bit_text", ""),
                    "status": sig["status"],
                    "reason": sig.get("reason") or sig.get("error") or "",
                    "in_defined_range": _blank(sig.get("in_defined_range")),
                    "accepted": _blank(verdict["accepted"]) if verdict else "",
                    "verdict_codes": " ".join(
                        f["code"] for f in (verdict or {}).get("findings", [])
                    ),
                    "library_error": (
                        (oracle.get("diagnostic") or {}).get("exception", "")
                        if oracle else ""
                    ),
                }
                writer.writerow([row.get(col, "") for col in header])
    return buffer.getvalue()


def export_frames_json(frames: list[dict], groups: list[str],
                       dbc_filter: list[str] | None = None) -> str:
    groups = [g for g in groups if g in COLUMN_GROUPS]
    keep = set(_signal_columns(groups))
    keep_frame = "frame" in groups
    out: list[dict[str, Any]] = []
    for frame in frames:
        if dbc_filter and frame["dbc_key"] not in dbc_filter:
            continue
        entry: dict[str, Any] = {
            "seq": frame["seq"],
            "time_text": frame["time_text"],
            "scenario": frame.get("scenario", ""),
            "label": frame.get("label", ""),
            "source": frame.get("source", ""),
            "frame_id": frame["frame_id_hex"],
            "is_extended": frame["is_extended"],
            "dlc": frame["dlc"],
        }
        if keep_frame:
            entry["payload_hex"] = frame["payload_hex"]
            entry["payload_bytes"] = frame["payload_bytes"]
            entry["payload_ascii"] = frame["payload_ascii"]
        entry["databases"] = {}
        for dbc_key, decoded in frame.get("decodes", {}).items():
            if dbc_filter and dbc_key not in dbc_filter:
                continue
            verdict = frame.get("verdicts", {}).get(dbc_key)
            oracle = frame.get("oracle", {}).get(dbc_key) or {}
            signals = []
            for sig in decoded["signals"]:
                signals.append({k: v for k, v in sig.items()
                                if k in keep or k == "name"})
            entry["databases"][dbc_key] = {
                "message": decoded.get("message"),
                "signals": signals,
                "accepted": verdict["accepted"] if verdict else None,
                "verdict_codes": [
                    f["code"] for f in (verdict or {}).get("findings", [])
                ],
                "library_error": (oracle.get("diagnostic") or {}).get(
                    "exception") if oracle else None,
            }
        out.append(entry)
    payload = {
        "format": "canlab.frames.v1",
        "columns": groups,
        "raw_included": "raw" in groups,
        "frames": out,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _blank(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
