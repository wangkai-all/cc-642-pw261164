"""Recording session shared by the sender and the receiver page.

The sender and receiver are *separate* pages on purpose: the point of the
exercise is that bytes leave one place and are interpreted in another.  A
session is a tiny in-process store (no external broker, no network) that
holds the frames the sender produced and the databases currently loaded.

Every frame keeps:

* the raw bytes exactly as sent, hex and ASCII;
* the sender's intent (selector, per-signal physical values, pad byte);
* the receiver-side decode of the very same bytes, computed at receive
  time against whichever databases are loaded;
* the business verdict (accepted / rejected) and the findings that led to
  it, separate from the library diagnostics.

Frames are never dropped.  A frame the definition rejects stays in the
list, marked, with its bytes intact.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .bits import DbcDatabase, LayoutError, MessageDef, encode_frame
from .decode import decode_frame
from .library_oracle import (
    LibraryLoadResult,
    cross_check_frame,
    load_with_cantools,
)
from .validators import (
    Finding,
    check_frame,
    validate_database,
    validate_for_send,
)


@dataclass
class LoadedDbc:
    key: str
    filename: str
    text: str
    db: DbcDatabase
    library: LibraryLoadResult

    def __post_init__(self) -> None:
        # Validated once at load; the findings drive every later view.
        self.definition_findings = validate_database(self.db)

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for f in self.definition_findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        return {
            "key": self.key,
            "filename": self.filename,
            "messages": [
                {
                    "frame_id": m.frame_id,
                    "name": m.name,
                    "length": m.length,
                    "is_extended": m.is_extended,
                    "signals": [
                        {
                            "name": s.name,
                            "start": s.start,
                            "length": s.length,
                            "byte_order": s.byte_order,
                            "is_signed": s.is_signed,
                            "scale": s.scale,
                            "offset": s.offset,
                            "minimum": s.minimum,
                            "maximum": s.maximum,
                            "unit": s.unit,
                            "choices": {str(k): v for k, v in s.choices.items()},
                            "is_multiplexer": s.is_multiplexer,
                            "multiplexer_signal": s.multiplexer_signal,
                            "multiplexer_ids": list(s.multiplexer_ids),
                            "comment": s.comment,
                            "line": m.bookmarks.get(s.name).line
                            if s.name in m.bookmarks else None,
                        }
                        for s in m.signals
                    ],
                    "comment": m.comment,
                }
                for m in self.db.messages
            ],
            "warnings": self.db.warnings,
            "definition_findings": [
                f.to_dict() for f in self.definition_findings
            ],
            "definition_counts": counts,
            "library": self.library.to_dict(),
        }


@dataclass
class FrameRecord:
    seq: int
    timestamp: float
    dbc_key: str
    frame_id: int
    is_extended: bool
    payload: bytes
    requested_dlc: int
    mux_value: int | None
    nested_mux: dict[str, int]
    values: dict[str, float]
    sender_findings: list[dict]
    sender_ok: bool
    label: str = ""
    scenario: str = ""
    source: str = "manual"   # manual | scenario | import

    # Filled at receive time (see Session.record).
    decodes: dict[str, Any] = field(default_factory=dict)
    verdicts: dict[str, Any] = field(default_factory=dict)
    oracle: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_decode: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "time_text": time.strftime(
                "%H:%M:%S", time.localtime(self.timestamp)
            ) + f".{int(self.timestamp * 1000) % 1000:03d}",
            "dbc_key": self.dbc_key,
            "frame_id": self.frame_id,
            "frame_id_hex": f"0x{self.frame_id:X}",
            "is_extended": self.is_extended,
            "dlc": len(self.payload),
            "requested_dlc": self.requested_dlc,
            "payload_hex": self.payload.hex(" ").upper(),
            "payload_bytes": list(self.payload),
            "payload_ascii": "".join(
                chr(b) if 32 <= b < 127 else "." for b in self.payload
            ),
            "mux_value": self.mux_value,
            "nested_mux": self.nested_mux,
            "values": self.values,
            "sender_ok": self.sender_ok,
            "sender_findings": self.sender_findings,
            "label": self.label,
            "scenario": self.scenario,
            "source": self.source,
        }
        if include_decode:
            data["decodes"] = {
                key: value.to_dict() for key, value in self.decodes.items()
            }
            data["verdicts"] = {
                key: {
                    "accepted": verdict.accepted,
                    "findings": [f.to_dict() for f in verdict.findings],
                }
                for key, verdict in self.verdicts.items()
            }
            data["oracle"] = self.oracle
        return data


class Session:
    """The whole in-memory state of one browser session."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._seq = itertools.count(1)
        self.dbcs: dict[str, LoadedDbc] = {}
        self.frames: list[FrameRecord] = []
        self.max_frames = 500

    # -- databases -----------------------------------------------------
    def add_dbc(self, filename: str, text: str, key: str | None = None) -> LoadedDbc:
        with self._lock:
            key = key or filename
            if key in self.dbcs:
                suffix = 2
                while f"{key}#{suffix}" in self.dbcs:
                    suffix += 1
                key = f"{key}#{suffix}"
            from .dbc import parse_dbc_text
            db = parse_dbc_text(text, filename=filename)
            library = load_with_cantools(text, filename)
            loaded = LoadedDbc(key=key, filename=filename, text=text,
                               db=db, library=library)
            self.dbcs[key] = loaded
            self._redecode_all()
            return loaded

    def remove_dbc(self, key: str) -> bool:
        with self._lock:
            if key not in self.dbcs:
                return False
            del self.dbcs[key]
            self.frames = [f for f in self.frames if f.dbc_key != key]
            self._redecode_all()
            return True

    # -- frames --------------------------------------------------------
    def record(self, *, dbc_key: str, frame_id: int, is_extended: bool,
               payload: bytes, requested_dlc: int, mux_value: int | None,
               nested_mux: dict[str, int], values: dict[str, float],
               sender_findings: list[dict], sender_ok: bool,
               label: str = "", scenario: str = "",
               source: str = "manual") -> FrameRecord:
        with self._lock:
            record = FrameRecord(
                seq=next(self._seq),
                timestamp=time.time(),
                dbc_key=dbc_key,
                frame_id=frame_id,
                is_extended=is_extended,
                payload=payload,
                requested_dlc=requested_dlc,
                mux_value=mux_value,
                nested_mux=nested_mux,
                values=values,
                sender_findings=sender_findings,
                sender_ok=sender_ok,
                label=label,
                scenario=scenario,
                source=source,
            )
            self.frames.append(record)
            if len(self.frames) > self.max_frames:
                self.frames = self.frames[-self.max_frames:]
            self._decode(record)
            return record

    def _decode(self, record: FrameRecord) -> None:
        record.decodes = {}
        record.verdicts = {}
        record.oracle = {}
        for key, loaded in self.dbcs.items():
            decoded = decode_frame(
                loaded.db, record.frame_id, record.is_extended, record.payload
            )
            record.decodes[key] = decoded
            # Only judge against a definition that actually claims this
            # frame; "no message with this id" is not a frame rejection.
            msg = loaded.db.message_by_id(record.frame_id, record.is_extended)
            if msg is not None:
                record.verdicts[key] = check_frame(msg, record.payload, decoded)
                oracle = cross_check_frame(
                    loaded.library, msg, record.payload,
                    [s.to_dict() for s in decoded.signals],
                    mux_values=decoded.mux_values,
                )
                record.oracle[key] = oracle.to_dict()

    def _redecode_all(self) -> None:
        for record in self.frames:
            self._decode(record)

    def clear_frames(self) -> None:
        with self._lock:
            self.frames.clear()

    # -- sender --------------------------------------------------------
    def prepare(self, dbc_key: str, frame_id: int, is_extended: bool,
                dlc: int, mux_value: int | None,
                nested_mux: dict[str, int] | None,
                values: dict[str, float], force: bool = False
                ) -> dict[str, Any]:
        """Validate and encode without recording.

        The sender page calls this on every edit so the byte preview and
        the rejection list come from the *same* code path that ``send``
        uses.  A preview therefore cannot disagree with what gets stored.
        """
        with self._lock:
            loaded = self.dbcs.get(dbc_key)
            if loaded is None:
                return {
                    "ok": False, "stage": "session", "payload_hex": None,
                    "findings": [Finding(
                        code="dbc_not_loaded",
                        severity="error",
                        scope="session",
                        message=f"database {dbc_key!r} is not loaded",
                        detail="Load the DBC first; the sender cannot invent "
                               "a layout.",
                    ).to_dict()],
                }
            msg = loaded.db.message_by_id(frame_id, is_extended)
            if msg is None:
                return {
                    "ok": False, "stage": "session", "payload_hex": None,
                    "findings": [Finding(
                        code="message_not_defined",
                        severity="error",
                        scope="session",
                        message=(
                            f"{dbc_key} has no "
                            f"{'extended' if is_extended else 'standard'} "
                            f"message 0x{frame_id:X}"
                        ),
                        detail="Choose a message defined in the loaded DBC.",
                        location={
                            "frame_id": frame_id,
                            "is_extended": is_extended,
                            "available": [
                                {"frame_id": m.frame_id,
                                 "is_extended": m.is_extended,
                                 "name": m.name}
                                for m in loaded.db.messages
                            ],
                        },
                    ).to_dict()],
                }

            nested_mux = nested_mux or {}
            findings = validate_for_send(msg, dlc, values, mux_value, nested_mux)
            if any(f.severity == "error" for f in findings) and not force:
                return {
                    "ok": False, "stage": "preflight", "payload_hex": None,
                    "message": msg.name,
                    "findings": [f.to_dict() for f in findings],
                }
            try:
                payload = encode_frame(
                    msg, dlc, mux_value, values,
                    nested_mux=nested_mux, force=force,
                )
            except LayoutError as exc:
                return {
                    "ok": False, "stage": "encode", "payload_hex": None,
                    "message": msg.name,
                    "findings": [f.to_dict() for f in findings] + [Finding(
                        code="encode_rejected",
                        severity="error",
                        scope="frame",
                        message=str(exc),
                        detail=(
                            "The value does not fit the signal's bit width "
                            "or scale. This is a *sender* rejection: the DBC "
                            "loaded fine and the library can model it."
                        ),
                    ).to_dict()],
                }
            return {
                "ok": True, "stage": "prepared", "message": msg.name,
                "payload": payload,
                "payload_hex": payload.hex(" ").upper(),
                "findings": [f.to_dict() for f in findings],
            }

    def send(self, dbc_key: str, frame_id: int, is_extended: bool,
             dlc: int, mux_value: int | None,
             nested_mux: dict[str, int] | None,
             values: dict[str, float], label: str = "",
             scenario: str = "", source: str = "manual",
             force: bool = False) -> dict[str, Any]:
        with self._lock:
            prepared = self.prepare(
                dbc_key, frame_id, is_extended, dlc, mux_value,
                nested_mux, values, force=force,
            )
            if not prepared["ok"]:
                return prepared
            record = self.record(
                dbc_key=dbc_key, frame_id=frame_id,
                is_extended=is_extended, payload=prepared["payload"],
                requested_dlc=dlc, mux_value=mux_value,
                nested_mux=nested_mux or {}, values=values,
                sender_findings=prepared["findings"], sender_ok=True,
                label=label, scenario=scenario, source=source,
            )
            return {
                "ok": True,
                "stage": "sent",
                "frame": record.to_dict(),
                "findings": prepared["findings"],
                "message": prepared["message"],
            }

    # -- snapshot ------------------------------------------------------
    def snapshot(self, include_frames: bool = True) -> dict[str, Any]:
        with self._lock:
            return {
                "dbcs": {
                    key: loaded.summary() for key, loaded in self.dbcs.items()
                },
                "frames": [
                    f.to_dict() for f in self.frames
                ] if include_frames else [],
                "count": len(self.frames),
                "max_frames": self.max_frames,
            }
