"""HTTP API contract tests.

These start the real ``ThreadingHTTPServer`` on an ephemeral port and talk
to it with ``urllib`` - the same surface the browser uses.  The global
session is replaced per test so tests do not leak frames into each other.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from canlab import server as server_module
from canlab.session import Session


@pytest.fixture()
def http(demo_text, monkeypatch):
    session = Session()
    monkeypatch.setattr(server_module, "_SESSION", session)
    monkeypatch.setattr(server_module, "_SAMPLES", None)
    httpd = server_module.create_server("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", session
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def get_json(base: str, path: str):
    try:
        with urllib.request.urlopen(base + path) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def post_json(base: str, path: str, body: dict):
    data = json.dumps(body).encode()
    request = urllib.request.Request(
        base + path, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_index_and_static_assets_are_served(http):
    base, _ = http
    with urllib.request.urlopen(base + "/") as response:
        body = response.read().decode()
    assert "CANLab" in body
    assert "/static/app.js" in body
    for name in ("app.js", "bits.js", "style.css"):
        with urllib.request.urlopen(base + "/static/" + name) as response:
            assert response.status == 200
            assert len(response.read()) > 0


def test_static_path_traversal_is_refused(http):
    base, _ = http
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(base + "/static/../server.py")
    assert info.value.code in (403, 404)


def test_state_exposes_fixtures_samples_and_groups(http):
    base, session = http
    status, payload = get_json(base, "/api/state")
    assert status == 200
    assert payload["ok"] is True
    assert payload["session"]["dbcs"] == {}
    assert {f["filename"] for f in payload["fixtures"]} >= {
        "demo_ecu.dbc", "conflict_a.dbc", "conflict_b.dbc"}
    assert {s["id"] for s in payload["samples"]} >= {
        "mux-miss", "ab-powertrain"}
    assert "raw" in payload["export_groups"]
    assert "raw" not in payload["default_export_groups"]


def test_load_fixture_then_preview_then_send(http):
    base, session = http
    status, loaded = post_json(base, "/api/dbc/load-fixture",
                               {"filename": "demo_ecu.dbc"})
    assert status == 200
    key = loaded["key"]

    status, preview = post_json(base, "/api/frame/preview", {
        "dbc": key, "frame_id": 0x101, "is_extended": False, "dlc": 8,
        "mux_value": 1, "values": {"MotorRpm": 1200},
    })
    assert status == 200
    assert preview["ok"] is True
    assert preview["payload_hex"] == "01 B0 04 00 00 00 00 00"
    assert "payload" not in preview
    statuses = {s["name"]: s["status"] for s in preview["decode"]["signals"]}
    assert statuses["ChargeVoltage"] == "not_applicable"
    assert preview["decode"]["payload_bytes"] == [1, 0xB0, 4, 0, 0, 0, 0, 0]

    status, sent = post_json(base, "/api/frame/send", {
        "dbc": key, "frame_id": 0x101, "is_extended": False, "dlc": 8,
        "mux_value": 1, "values": {"MotorRpm": 1200},
    })
    assert status == 200
    assert sent["ok"] is True
    assert sent["stage"] == "sent"
    assert len(session.frames) == 1
    assert sent["frame"]["oracle"]["demo_ecu.dbc"]["ok"] is True


def test_preview_rejection_returns_422_without_recording(http):
    base, session = http
    post_json(base, "/api/dbc/load-fixture", {"filename": "demo_ecu.dbc"})
    status, payload = post_json(base, "/api/frame/preview", {
        "dbc": "demo_ecu.dbc", "frame_id": 0x101, "dlc": 8,
        "mux_value": 1, "values": {"ChargeVoltage": 400},
    })
    assert status == 422
    assert payload["ok"] is False
    assert payload["stage"] == "preflight"
    assert any(f["code"] == "send_signal_not_applicable"
               for f in payload["findings"])
    assert session.frames == []


def test_preview_length_change_is_refused_with_located_findings(http):
    base, _ = http
    post_json(base, "/api/dbc/load-fixture", {"filename": "demo_ecu.dbc"})
    status, payload = post_json(base, "/api/frame/preview", {
        "dbc": "demo_ecu.dbc", "frame_id": 0x100, "dlc": 6,
    })
    assert status == 422
    out = [f for f in payload["findings"]
           if f["code"] == "send_signal_outside_frame"]
    assert {f["location"]["signal"] for f in out} == {
        "AuxFlags", "ThrottlePct", "LampState"}


def test_load_all_samples_then_ab_compare(http):
    base, session = http
    for name in ("demo_ecu.dbc", "conflict_a.dbc", "conflict_b.dbc"):
        post_json(base, "/api/dbc/load-fixture", {"filename": name})

    status, result = post_json(base, "/api/samples/load", {})
    assert status == 200
    assert result["ok"] is True
    assert result["session"]["count"] >= 15

    # The A/B sample was recorded against both conflicting databases.
    victim = next(f for f in result["session"]["frames"]
                  if f["scenario"] == "ab-powertrain")
    assert set(victim["decodes"]) >= {"conflict_a.dbc", "conflict_b.dbc"}
    assert victim["decodes"]["conflict_a.dbc"]["message"] == "Powertrain"
    assert victim["decodes"]["conflict_b.dbc"]["message"] == "Powertrain"
    a = {s["name"]: s for s in victim["decodes"]["conflict_a.dbc"]["signals"]}
    b = {s["name"]: s for s in victim["decodes"]["conflict_b.dbc"]["signals"]}
    assert a["Torque"]["physical"] != b["Torque"]["physical"]
    # B declares 6 bytes, so a clean 8-byte frame is rejected there.
    assert victim["verdicts"]["conflict_b.dbc"]["accepted"] is False

    status, compare = post_json(base, "/api/compare", {
        "left": "conflict_a.dbc", "right": "conflict_b.dbc",
    })
    assert status == 200
    assert compare["ok"] is True
    assert compare["left_definition_findings"]
    assert compare["right_definition_findings"]
    kinds = {entry["kind"] for entry in compare["differences"]}
    assert "message_differs" in kinds


def test_import_raw_frame_keeps_the_bytes(http):
    base, session = http
    post_json(base, "/api/dbc/load-fixture", {"filename": "demo_ecu.dbc"})
    status, result = post_json(base, "/api/frame/import", {
        "dbc": "demo_ecu.dbc", "frame_id": "0x100", "is_extended": False,
        "payload_hex": "01 02 03 AB CD EF",
    })
    assert status == 200
    frame = result["frame"]
    assert frame["payload_hex"] == "01 02 03 AB CD EF"
    assert frame["dlc"] == 6
    assert frame["verdicts"]["demo_ecu.dbc"]["accepted"] is False
    assert session.frames[0].source == "import"


def test_export_requires_frames_and_honours_groups(http):
    base, _ = http
    status, payload = post_json(base, "/api/dbc/load-fixture",
                                {"filename": "demo_ecu.dbc"})
    status, error = get_json(base, "/api/export?format=csv&group=identity")
    assert status == 409

    post_json(base, "/api/frame/send", {
        "dbc": "demo_ecu.dbc", "frame_id": 0x100, "dlc": 8,
        "values": {"Counter": 5},
    })
    with urllib.request.urlopen(
            base + "/api/export?format=csv&group=identity&group=physical") as r:
        body = r.read().decode()
    assert "raw: omitted" in body
    assert "raw_unsigned" not in body
    assert "physical" in body

    with urllib.request.urlopen(
            base + "/api/export?format=json&group=identity&group=raw") as r:
        payload = json.loads(r.read().decode())
    assert payload["raw_included"] is True
    signal = payload["frames"][0]["databases"]["demo_ecu.dbc"]["signals"][0]
    assert "raw_unsigned" in signal


def test_unknown_scenario_and_fixture_are_404(http):
    base, _ = http
    status, payload = post_json(base, "/api/samples/load",
                                {"scenario": "does-not-exist"})
    assert status == 404
    status, payload = post_json(base, "/api/dbc/load-fixture",
                                {"filename": "nope.dbc"})
    assert status == 404


def test_invalid_json_body_is_a_400(http):
    base, _ = http
    request = urllib.request.Request(
        base + "/api/frame/send", data=b"{not json",
        headers={"Content-Type": "application/json"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(request)
    assert info.value.code == 400
