"""A line-aware reader for the classic Vector DBC format.

The reader is intentionally forgiving: a database that two ECUs disagree
about must be loadable so the disagreement can be *shown*.  Syntax errors
are collected with their source line instead of aborting the load; semantic
checks live in ``validators.py``.

Only the subset of DBC that affects frame layout and value interpretation is
modelled.  Everything else (attributes, value tables, signal groups) is
parsed just far enough to ignore it safely.
"""

from __future__ import annotations

import os
import re

from .bits import Bookmark, DbcDatabase, MessageDef, SignalDef

_BO_RE = re.compile(
    r'^BO_\s+(?P<id>\d+)\s+(?P<name>[A-Za-z_]\w*)\s*:\s*'
    r'(?P<len>\d+)\s+(?P<senders>[^;]*?)\s*(?:;.*)?$'
)
_SG_RE = re.compile(
    r'^SG_\s+(?P<name>[A-Za-z_]\w*)\s*'
    r'(?P<mux>M|m\d+(?:M)?)?\s*:\s*'
    r'(?P<start>\d+)\|(?P<len>\d+)@(?P<order>[01])(?P<sign>[+-])\s*'
    r'\(\s*(?P<scale>[^,]+?)\s*,\s*(?P<offset>[^)]+?)\s*\)\s*'
    r'\[\s*(?P<min>[^|]*?)\s*\|\s*(?P<max>[^\]]*?)\s*\]\s*'
    r'"(?P<unit>[^"]*)"\s*(?P<receivers>[^;]*?)\s*(?:;.*)?$'
)
_VAL_RE = re.compile(
    r'^VAL_\s+(?P<id>\d+)\s+(?P<sig>[A-Za-z_]\w*)\s+(?P<body>.*?);\s*$'
)
_CM_SG_RE = re.compile(
    r'^CM_\s+SG_\s+(?P<id>\d+)\s+(?P<sig>[A-Za-z_]\w*)\s+"(?P<text>.*)";\s*$'
)
_CM_BO_RE = re.compile(r'^CM_\s+BO_\s+(?P<id>\d+)\s+"(?P<text>.*)";\s*$')
_SIG_VALTYPE_RE = re.compile(
    r'^SIG_VALTYPE_\s+(?P<id>\d+)\s+(?P<sig>[A-Za-z_]\w*)\s*:\s*(?P<type>\d+)\s*;'
)
_SG_MUL_VAL_RE = re.compile(
    r'^SG_MUL_VAL_\s+(?P<id>\d+)\s+(?P<sig>[A-Za-z_]\w*)\s+'
    r'(?P<mux>[A-Za-z_]\w*)\s+(?P<ranges>[^;]*);'
)
_NUMBER_RE = re.compile(r'^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$')

# Statements whose bodies we intentionally skip.  Listed so that an
# unrecognised line beginning with one of these is not reported as a
# parse error.
_IGNORED_PREFIXES = (
    "VERSION", "NS_", "BS_", "NS_DESC_", "CM_", "BA_DEF_", "BA_DEF_DEF_",
    "BA_DEF_REL_", "BA_DEF_DEF_REL_", "BA_REL_", "BA_DEF_SGTYPE_",
    "BA_SGTYPE_", "SIG_TYPE_REF_", "SIG_GROUP_", "SGTYPE_", "SGTYPE_VAL_",
    "VAL_TABLE_", "EV_", "ENVVAR_DATA_", "CAT_DEF_", "CAT_", "FILTER",
    "SIGTYPE_VALTYPE_", "BO_TX_BU_", "BA_",
)


def _num(text: str, default: float = 0.0) -> float:
    text = (text or "").strip()
    if not text or not _NUMBER_RE.match(text):
        return default
    return float(text)


def _optional_num(text: str) -> float | None:
    text = (text or "").strip()
    if not text or not _NUMBER_RE.match(text):
        return None
    return float(text)


def _int(text: str, default: int = 0) -> int:
    try:
        return int(str(text).strip(), 0)
    except (TypeError, ValueError):
        return default


def _choice_pairs(body: str) -> dict[int, str]:
    """Parse ``0 "Off" 1 "On"`` into ``{0: 'Off', 1: 'On'}``."""
    result: dict[int, str] = {}
    for raw, label in re.findall(r'(-?\d+)\s+"((?:[^"\\]|\\.)*)"', body):
        result[int(raw)] = label
    return result


def split_frame_id(raw_id: int) -> tuple[int, bool, str | None]:
    """Return ``(id, is_extended, warning)``.

    Vector tools set bit 31 for 29-bit identifiers.  Some exports write the
    bare identifier instead; a value above 0x7FF can then only be extended.
    """
    if raw_id & 0x80000000:
        return raw_id & 0x1FFFFFFF, True, None
    if raw_id > 0x7FF:
        return raw_id, True, (
            "frame id above 0x7FF is written without the extended-frame flag; "
            "read as a 29-bit extended frame"
        )
    return raw_id, False, None


def _parse_mux_token(token: str | None) -> tuple[bool, tuple[int, ...], bool]:
    """Return ``(is_multiplexer, ids, is_multiplexed)``.

    ``M``  -> this signal is the selector.
    ``m3`` -> child of the message multiplexer, active for selector 3.
    ``m3M`` -> child for selector 3 *and* itself a nested selector.
    """
    if not token:
        return False, (), False
    is_mux = token.endswith("M") or token == "M"
    if not token.startswith("m"):
        return is_mux, (), False
    body = token[1:]
    if body.endswith("M"):
        body = body[:-1]
    ids = tuple(_int(part) for part in body.split(",") if part.strip())
    return is_mux, ids, True


def parse_dbc_text(text: str, filename: str = "<memory>") -> DbcDatabase:
    messages: list[MessageDef] = []
    nodes: list[str] = []
    warnings: list[str] = []
    by_key: dict[tuple[int, bool], MessageDef] = {}
    current: MessageDef | None = None
    pending_choices: list[tuple[int, str, dict[int, str], int]] = []
    pending_mulval: list[tuple[int, str, str, list[tuple[int, int]], int]] = []
    float_signals: set[tuple[int, str]] = set()
    in_ns_block = False

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue

        # The NS_ block lists every statement keyword used by the file.  Its
        # entries look like statements but carry no definition.
        if line.startswith("NS_") and line.rstrip().endswith(":"):
            in_ns_block = True
            continue
        if in_ns_block:
            # NS_ entries are indented; the first unindented line ends the
            # block and is processed normally.
            if raw_line[:1] in (" ", "\t"):
                continue
            in_ns_block = False

        if line.startswith("BU_:"):
            nodes = line[4:].split()
            continue

        match = _BO_RE.match(line)
        if match:
            raw_id = _int(match.group("id"))
            frame_id, extended, warning = split_frame_id(raw_id)
            if warning:
                warnings.append(f"line {lineno}: {warning}")
            msg = MessageDef(
                frame_id=frame_id,
                name=match.group("name"),
                length=_int(match.group("len")),
                signals=[],
                is_extended=extended,
                senders=[
                    s for s in match.group("senders").split()
                    if s not in ("Vector__XXX",)
                ],
                line=lineno,
            )
            messages.append(msg)
            current = msg
            key = (frame_id, extended)
            if key in by_key:
                other = by_key[key]
                warnings.append(
                    f"line {lineno}: duplicate frame id 0x{frame_id:X} "
                    f"({'extended' if extended else 'standard'}); "
                    f"{other.name!r} (line {other.line}) and {msg.name!r} "
                    f"both claim it"
                )
            else:
                by_key[key] = msg
            continue

        match = _SG_RE.match(line)
        if match:
            if current is None:
                warnings.append(f"line {lineno}: SG_ outside any BO_ block; ignored")
                continue
            is_mux, ids, is_multiplexed = _parse_mux_token(match.group("mux"))
            sig = SignalDef(
                name=match.group("name"),
                start=_int(match.group("start")),
                length=max(0, _int(match.group("len"))),
                byte_order="little_endian" if match.group("order") == "1" else "big_endian",
                is_signed=match.group("sign") == "-",
                scale=_num(match.group("scale"), 1.0),
                offset=_num(match.group("offset"), 0.0),
                minimum=_optional_num(match.group("min")),
                maximum=_optional_num(match.group("max")),
                unit=match.group("unit"),
                is_multiplexer=is_mux,
                multiplexer_ids=ids,
                # Parent name is resolved after the message is complete;
                # the DBC child syntax does not name its parent.
                multiplexer_signal=":pending:" if is_multiplexed else None,
            )
            if sig.name in current.bookmarks:
                warnings.append(
                    f"line {lineno}: duplicate signal name {sig.name!r} in "
                    f"message {current.name!r}"
                )
            current.signals.append(sig)
            current.bookmarks[sig.name] = Bookmark(line=lineno, text=line)
            continue

        match = _VAL_RE.match(line)
        if match:
            pending_choices.append((
                _int(match.group("id")), match.group("sig"),
                _choice_pairs(match.group("body")), lineno,
            ))
            continue

        match = _CM_SG_RE.match(line)
        if match:
            frame_id = _int(match.group("id"))
            target = _find_signal(messages, frame_id, match.group("sig"))
            if target is None:
                warnings.append(
                    f"line {lineno}: CM_ SG_ for unknown signal "
                    f"{match.group('sig')!r} in frame 0x{frame_id:X}"
                )
            else:
                target.comment = match.group("text")
            continue

        match = _CM_BO_RE.match(line)
        if match:
            frame_id = _int(match.group("id"))
            target = _find_message(messages, frame_id)
            if target is not None:
                target.comment = match.group("text")
            continue

        match = _SIG_VALTYPE_RE.match(line)
        if match:
            if match.group("type") in ("1", "2"):
                float_signals.add((_int(match.group("id")), match.group("sig")))
            continue

        match = _SG_MUL_VAL_RE.match(line)
        if match:
            ranges: list[tuple[int, int]] = []
            for chunk in match.group("ranges").split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                if "-" in chunk:
                    lo, _, hi = chunk.partition("-")
                    ranges.append((_int(lo), _int(hi)))
                else:
                    value = _int(chunk)
                    ranges.append((value, value))
            pending_mulval.append((
                _int(match.group("id")), match.group("sig"),
                match.group("mux"), ranges, lineno,
            ))
            continue

        if line.startswith(_IGNORED_PREFIXES):
            continue
        if line.startswith(("BO_", "SG_", "VAL_", "CM_", "BA_", "SG_MUL_VAL_")):
            warnings.append(f"line {lineno}: could not parse definition: {line[:120]}")
        # Any other line is metadata we do not model.

    _resolve_multiplexer_parents(messages, pending_mulval, warnings)
    _attach_choices(messages, pending_choices, warnings)

    for frame_id, sig_name in sorted(float_signals):
        target = _find_signal(messages, frame_id, sig_name)
        if target is not None:
            warnings.append(
                f"signal {target.name!r} in frame 0x{frame_id:X} is declared "
                f"IEEE float (SIG_VALTYPE_); this build interprets its bits "
                f"as an integer"
            )

    return DbcDatabase(filename=filename, messages=messages, nodes=nodes,
                       warnings=warnings)


def _find_message(messages: list[MessageDef], frame_id: int) -> MessageDef | None:
    for msg in messages:
        if msg.frame_id == (frame_id & 0x1FFFFFFF):
            return msg
    return None


def _find_signal(messages: list[MessageDef], frame_id: int,
                 name: str) -> SignalDef | None:
    for msg in messages:
        if msg.frame_id != (frame_id & 0x1FFFFFFF):
            continue
        for sig in msg.signals:
            if sig.name == name:
                return sig
    return None


def _resolve_multiplexer_parents(
    messages: list[MessageDef],
    pending_mulval: list[tuple[int, str, str, list[tuple[int, int]], int]],
    warnings: list[str],
) -> None:
    explicit: dict[tuple[int, str], tuple[str, list[tuple[int, int]]]] = {}
    for frame_id, sig_name, mux_name, ranges, lineno in pending_mulval:
        explicit[(frame_id & 0x1FFFFFFF, sig_name)] = (mux_name, ranges)

    for msg in messages:
        muxers = [s for s in msg.signals if s.is_multiplexer]
        for sig in msg.signals:
            if sig.multiplexer_signal != ":pending:":
                continue
            key = (msg.frame_id, sig.name)
            if key in explicit:
                mux_name, ranges = explicit[key]
                # Attach even when the named parent does not exist: that
                # is a real definition error and the validator reports it
                # (``multiplexer_missing``), rather than silently turning
                # the signal into an always-applicable one.
                sig.multiplexer_signal = mux_name
                if ranges:
                    ids: list[int] = []
                    for lo, hi in ranges:
                        ids.extend(range(lo, min(hi, lo + 4096) + 1))
                    sig.multiplexer_ids = tuple(ids)
                continue
            if len(muxers) == 1:
                sig.multiplexer_signal = muxers[0].name
                continue
            if not muxers:
                sig.multiplexer_signal = None
                warnings.append(
                    f"message {msg.name!r}: signal {sig.name!r} is declared "
                    f"multiplexed (m{','.join(map(str, sig.multiplexer_ids))}) "
                    f"but the message has no multiplexer signal; it will be "
                    f"treated as always applicable"
                )
                continue
            sig.multiplexer_signal = muxers[0].name
            names = ", ".join(m.name for m in muxers)
            warnings.append(
                f"message {msg.name!r}: signal {sig.name!r} has "
                f"{len(muxers)} possible multiplexer parents ({names}); "
                f"the DBC child syntax does not name its parent and there is "
                f"no SG_MUL_VAL_ entry, so {muxers[0].name!r} is assumed"
            )


def _attach_choices(
    messages: list[MessageDef],
    pending_choices: list[tuple[int, str, dict[int, str], int]],
    warnings: list[str],
) -> None:
    for frame_id, sig_name, choices, lineno in pending_choices:
        target = _find_signal(messages, frame_id, sig_name)
        if target is None:
            warnings.append(
                f"line {lineno}: VAL_ for unknown signal {sig_name!r} "
                f"in frame 0x{frame_id:X}"
            )
            continue
        merged = dict(target.choices)
        merged.update(choices)
        target.choices = merged


def load_dbc(path: str) -> DbcDatabase:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        text = handle.read()
    return parse_dbc_text(text, filename=os.path.basename(path))
