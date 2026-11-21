"""Stdlib-only HTTP server and JSON API.

No web framework: the project must run with ``python -m canlab`` after
installing nothing but the optional oracle.  Routes are deliberately small
and every response is JSON produced by the modules above, so the browser
never has to infer a decode.

Security posture: bound to 127.0.0.1, no CORS, uploads limited to text
files under 4 MiB, and the DBC text is never executed.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .export import COLUMN_GROUPS, DEFAULT_GROUPS, export_frames_csv, export_frames_json
from .library_oracle import compare_definitions
from .session import Session

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, "canlab", "static")
FIXTURE_DIR = os.path.join(ROOT, "fixtures")

MAX_UPLOAD_BYTES = 4 * 1024 * 1024
_SESSION = Session()
_SAMPLES: list[dict] | None = None


def _samples() -> list[dict]:
    global _SAMPLES
    if _SAMPLES is None:
        path = os.path.join(FIXTURE_DIR, "frames", "samples.json")
        with open(path, "r", encoding="utf-8") as handle:
            _SAMPLES = json.load(handle)["scenarios"]
    return _SAMPLES


def _fixture_files() -> list[dict]:
    directory = os.path.join(FIXTURE_DIR, "dbc")
    files = []
    if os.path.isdir(directory):
        for name in sorted(os.listdir(directory)):
            if name.endswith(".dbc"):
                files.append({"filename": name})
    return files


def _error(message: str, status: int = 400, **extra: Any) -> tuple[int, dict]:
    return status, {"ok": False, "error": message, **extra}


class Handler(BaseHTTPRequestHandler):
    server_version = "CANLab/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers -------------------------------------------------------
    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, body: str,
                   content_type: str = "text/csv; charset=utf-8",
                   filename: str | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header(
                "Content-Disposition", f'attachment; filename="{filename}"'
            )
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD_BYTES:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        if not raw.strip():
            return {}
        return json.loads(raw.decode("utf-8"))

    @staticmethod
    def _frame_id(text: Any) -> int:
        if isinstance(text, int):
            return text
        value = str(text).strip()
        return int(value, 16) if value.lower().startswith("0x") else int(value)

    # -- routing -------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return self._serve_static("index.html")
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])
        if path == "/api/state":
            query = parse_qs(parsed.query)
            include = query.get("frames", ["1"])[0] != "0"
            return self._send_json(200, {
                "ok": True,
                "session": _SESSION.snapshot(include_frames=include),
                "samples": _samples(),
                "fixtures": _fixture_files(),
                "export_groups": COLUMN_GROUPS,
                "default_export_groups": DEFAULT_GROUPS,
            })
        if path == "/api/export":
            return self._export(parse_qs(parsed.query))
        return self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            payload = self._read_json()
        except Exception as exc:  # noqa: BLE001
            return self._send_json(*_error(f"invalid JSON body: {exc}"))

        try:
            if path == "/api/dbc/upload":
                return self._upload_dbc(payload)
            if path == "/api/dbc/load-fixture":
                return self._load_fixture(payload)
            if path == "/api/dbc/remove":
                key = str(payload.get("key", ""))
                removed = _SESSION.remove_dbc(key)
                return self._send_json(*(
                    (200, {"ok": True}) if removed
                    else _error(f"database {key!r} is not loaded", 404)
                ))
            if path == "/api/frame/preview":
                return self._preview_frame(payload)
            if path == "/api/frame/send":
                return self._send_frame(payload)
            if path == "/api/frame/import":
                return self._import_frame(payload)
            if path == "/api/frames/clear":
                _SESSION.clear_frames()
                return self._send_json(200, {"ok": True})
            if path == "/api/samples/load":
                return self._load_samples(payload)
            if path == "/api/compare":
                return self._compare(payload)
        except Exception as exc:  # noqa: BLE001 - never leak a stack to the page
            return self._send_json(500, {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "hint": "The request was rejected by CANLab; no partial "
                        "frame was stored.",
            })
        return self._send_json(404, {"ok": False, "error": "not found"})

    # -- handlers ------------------------------------------------------
    def _serve_static(self, name: str) -> None:
        safe = os.path.normpath(name)
        if safe.startswith("..") or os.path.isabs(safe):
            return self._send_json(403, {"ok": False, "error": "forbidden"})
        full = os.path.join(STATIC_DIR, safe)
        if not os.path.isfile(full):
            return self._send_json(404, {"ok": False, "error": "not found"})
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as handle:
            data = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + (
            "; charset=utf-8" if ctype.startswith(("text/", "application/javascript",
                                                   "application/json")) else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _upload_dbc(self, payload: dict) -> None:
        text = payload.get("text")
        filename = str(payload.get("filename") or "uploaded.dbc")
        if text is None:
            return self._send_json(*_error("body must contain 'text'"))
        if len(text.encode("utf-8")) > MAX_UPLOAD_BYTES:
            return self._send_json(*_error("DBC larger than 4 MiB", 413))
        if not re.search(r"^\s*BO_\s+\d+", text, re.MULTILINE):
            return self._send_json(*_error(
                "the text contains no BO_ message definition; is this a DBC?",
                422,
            ))
        loaded = _SESSION.add_dbc(filename, text)
        return self._send_json(200, {
            "ok": True,
            "key": loaded.key,
            "session": _SESSION.snapshot(include_frames=False),
        })

    def _load_fixture(self, payload: dict) -> None:
        filename = os.path.basename(str(payload.get("filename") or ""))
        full = os.path.join(FIXTURE_DIR, "dbc", filename)
        if not filename.endswith(".dbc") or not os.path.isfile(full):
            return self._send_json(*_error(f"unknown fixture {filename!r}", 404))
        with open(full, "r", encoding="utf-8") as handle:
            text = handle.read()
        loaded = _SESSION.add_dbc(filename, text)
        return self._send_json(200, {
            "ok": True,
            "key": loaded.key,
            "session": _SESSION.snapshot(include_frames=False),
        })

    def _load_samples(self, payload: dict) -> None:
        scenario = str(payload.get("scenario") or "")
        wanted = [s for s in _samples()
                  if not scenario or s.get("id") == scenario]
        if not wanted:
            return self._send_json(*_error(f"unknown scenario {scenario!r}", 404))
        results = []
        for sample in wanted:
            dbc_key = self._ensure_fixture(str(sample.get("dbc") or ""))
            if dbc_key is None:
                results.append({"scenario": sample.get("id"),
                                "ok": False,
                                "findings": [{"code": "fixture_missing",
                                              "severity": "error",
                                              "scope": "session",
                                              "message": "fixture DBC is not available",
                                              "detail": sample.get("dbc", ""),
                                              "location": {}}]})
                continue
            targets = [dbc_key]
            also = sample.get("also_send_to")
            if also:
                extra = self._ensure_fixture(str(also))
                if extra:
                    targets.append(extra)
            if "raw_hex" in sample:
                data = bytes.fromhex(
                    re.sub(r"[^0-9A-Fa-f]", "", str(sample["raw_hex"]))
                )
                for target in targets:
                    _SESSION.record(
                        dbc_key=target,
                        frame_id=int(sample["frame_id"]),
                        is_extended=bool(sample.get("is_extended")),
                        payload=data,
                        requested_dlc=int(sample.get("dlc", len(data))),
                        mux_value=sample.get("mux_value"),
                        nested_mux={},
                        values={},
                        sender_findings=[],
                        sender_ok=True,
                        label=str(sample.get("title") or ""),
                        scenario=str(sample.get("id") or ""),
                        source="scenario",
                    )
                results.append({"scenario": sample.get("id"), "ok": True,
                                "stage": "recorded", "targets": targets})
                continue
            for index, target in enumerate(targets):
                result = _SESSION.send(
                    dbc_key=target,
                    frame_id=int(sample["frame_id"]),
                    is_extended=bool(sample.get("is_extended")),
                    dlc=int(sample.get("dlc", 8)),
                    mux_value=sample.get("mux_value"),
                    nested_mux=sample.get("nested_mux") or {},
                    values=sample.get("values") if index == 0 else {},
                    label=str(sample.get("title") or ""),
                    scenario=str(sample.get("id") or ""),
                    source="scenario",
                )
                results.append({
                    "scenario": sample.get("id"), "target": target,
                    "ok": result["ok"],
                    "findings": result.get("findings", []),
                })
        self._send_json(200, {"ok": True, "results": results,
                              "session": _SESSION.snapshot()})

    def _ensure_fixture(self, filename: str) -> str | None:
        """Load a fixture DBC if it is not already loaded; return its key."""
        filename = os.path.basename(filename)
        for key, loaded in _SESSION.dbcs.items():
            if loaded.filename == filename:
                return key
        full = os.path.join(FIXTURE_DIR, "dbc", filename)
        if not filename.endswith(".dbc") or not os.path.isfile(full):
            return None
        with open(full, "r", encoding="utf-8") as handle:
            return _SESSION.add_dbc(filename, handle.read()).key

    def _preview_frame(self, payload: dict) -> None:
        """Encode without recording: the sender's live byte preview."""
        dbc = _SESSION.dbcs.get(str(payload.get("dbc") or ""))
        result = _SESSION.prepare(
            dbc_key=str(payload.get("dbc") or ""),
            frame_id=self._frame_id(payload.get("frame_id", 0)),
            is_extended=bool(payload.get("is_extended", False)),
            dlc=int(payload.get("dlc", 8)),
            mux_value=payload.get("mux_value"),
            nested_mux={str(k): int(v) for k, v in
                        (payload.get("nested_mux") or {}).items()},
            values={str(k): float(v) for k, v in
                    (payload.get("values") or {}).items()},
            force=bool(payload.get("force", False)),
        )
        if result.get("ok"):
            msg = dbc.db.message_by_id(
                self._frame_id(payload.get("frame_id", 0)),
                bool(payload.get("is_extended", False)),
            ) if dbc else None
            if msg is not None:
                from .decode import decode_message
                decoded = decode_message(
                    msg, result.pop("payload"), str(payload.get("dbc")),
                    msg.frame_id, msg.is_extended,
                )
                result["decode"] = decoded.to_dict()
        result.pop("payload", None)
        self._send_json(200 if result.get("ok") else 422, result)

    def _send_frame(self, payload: dict) -> None:
        dbc_key = str(payload.get("dbc") or "")
        frame_id = self._frame_id(payload.get("frame_id", 0))
        is_extended = bool(payload.get("is_extended", False))
        values = payload.get("values") or {}
        nested = payload.get("nested_mux") or {}
        result = _SESSION.send(
            dbc_key=dbc_key,
            frame_id=frame_id,
            is_extended=is_extended,
            dlc=int(payload.get("dlc", 8)),
            mux_value=payload.get("mux_value"),
            nested_mux={str(k): int(v) for k, v in nested.items()},
            values={str(k): float(v) for k, v in values.items()},
            label=str(payload.get("label") or ""),
            scenario=str(payload.get("scenario") or ""),
            source=str(payload.get("source") or "manual"),
            force=bool(payload.get("force", False)),
        )
        status = 200 if result["ok"] else 422
        self._send_json(status, result)

    def _import_frame(self, payload: dict) -> None:
        raw_hex = re.sub(r"[^0-9A-Fa-f]", "", str(payload.get("payload_hex") or ""))
        if not raw_hex or len(raw_hex) % 2:
            return self._send_json(*_error(
                "payload_hex must contain an even number of hex digits", 422
            ))
        data = bytes.fromhex(raw_hex)
        if len(data) > 64:
            return self._send_json(*_error("a frame cannot exceed 64 bytes", 422))
        record = _SESSION.record(
            dbc_key=str(payload.get("dbc") or ""),
            frame_id=self._frame_id(payload.get("frame_id", 0)),
            is_extended=bool(payload.get("is_extended", False)),
            payload=data,
            requested_dlc=len(data),
            mux_value=None,
            nested_mux={},
            values={},
            sender_findings=[],
            sender_ok=True,
            label=str(payload.get("label") or "imported"),
            source="import",
        )
        return self._send_json(200, {"ok": True, "frame": record.to_dict()})

    def _compare(self, payload: dict) -> None:
        left = str(payload.get("left") or "")
        right = str(payload.get("right") or "")
        if left not in _SESSION.dbcs or right not in _SESSION.dbcs:
            return self._send_json(*_error(
                "both databases must be loaded before they can be compared",
                404,
            ))
        diffs = compare_definitions(
            _SESSION.dbcs[left].db, _SESSION.dbcs[right].db
        )
        return self._send_json(200, {
            "ok": True,
            "left": left,
            "right": right,
            "differences": diffs,
            "left_definition_findings": [
                f.to_dict() for f in _SESSION.dbcs[left].definition_findings
            ],
            "right_definition_findings": [
                f.to_dict() for f in _SESSION.dbcs[right].definition_findings
            ],
        })

    def _export(self, query: dict[str, list[str]]) -> None:
        fmt = query.get("format", ["csv"])[0]
        groups = query.get("group") or DEFAULT_GROUPS
        groups = [g for g in groups if g in COLUMN_GROUPS]
        if not groups:
            return self._send_json(*_error("select at least one column group"))
        dbc_filter = query.get("dbc") or None
        frames = _SESSION.snapshot()["frames"]
        if not frames:
            return self._send_json(*_error(
                "no frames recorded yet; send or import one first", 409
            ))
        if fmt == "json":
            body = export_frames_json(frames, groups, dbc_filter)
            return self._send_text(
                200, body, "application/json; charset=utf-8",
                "canlab-frames.json",
            )
        body = export_frames_csv(frames, groups, dbc_filter)
        return self._send_text(200, body, "text/csv; charset=utf-8",
                               "canlab-frames.csv")


def create_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="canlab",
        description="CAN frame / DBC workbench. Serves a local web UI.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--load", action="append", default=[],
        help="load a DBC fixture at startup (filename under fixtures/dbc/)",
    )
    parser.add_argument(
        "--dbc", action="append", default=[],
        help="load an arbitrary DBC path at startup",
    )
    parser.add_argument("--open", action="store_true",
                        help="print the URL and keep serving")
    args = parser.parse_args(argv)

    for name in args.load:
        full = os.path.join(FIXTURE_DIR, "dbc", os.path.basename(name))
        if not os.path.isfile(full):
            parser.error(f"fixture not found: {name}")
        with open(full, "r", encoding="utf-8") as handle:
            _SESSION.add_dbc(os.path.basename(name), handle.read())
    for path in args.dbc:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            _SESSION.add_dbc(os.path.basename(path), handle.read())

    httpd = create_server(args.host, args.port)
    url = f"http://{args.host}:{args.port}/"
    print(f"CANLab listening on {url}", flush=True)
    if args.open:
        print("Press Ctrl+C to stop.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
