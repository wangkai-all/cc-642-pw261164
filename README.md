# CANLab

A local **CAN frame / DBC workbench**. Feed it a DBC, compose or import raw
frames, and see each signal's actual bit span, its raw integer, its
physical value and its text enum — then decode the *same bytes* through a
second DBC and compare.

Everything runs on localhost. The Python side is standard library only;
[cantools](https://github.com/cantools/cantools) is an optional,
deliberately **separate** second opinion (see
[docs/library-vs-business.md](docs/library-vs-business.md)).

---

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # optional: only needed for the cantools panel
.venv/bin/python -m canlab --load demo_ecu.dbc --load conflict_a.dbc --load conflict_b.dbc
```

Then open <http://127.0.0.1:8765/>. Without `--load`, use the *Databases*
panel to load a fixture, upload a `.dbc`, or paste DBC text.

```bash
# no framing changes; load any file on disk instead of a fixture
.venv/bin/python -m canlab --dbc /path/to/vehicle.dbc

# different port / bind address
.venv/bin/python -m canlab --host 127.0.0.1 --port 9000
```

`python -m canlab --help` lists the flags. The server binds to
`127.0.0.1` by default and refuses static-path traversal.

---

## What each tab does

### Sender

* Pick a database, a message and a frame length (0–64 bytes).
* Set the **multiplexer value** and the **pad byte**; the byte preview
  updates from the same encoder that `Send` uses, so the preview cannot
  disagree with what gets stored.
* Every signal row shows its layout (`Intel 8|16` / `Motorola 39|12 ·
  signed ×0.5`), its declared range and its text choices.
* When the selected branch does not cover a signal, the row is disabled
  and labelled **not applicable** — its value is dropped from the request
  rather than encoded as zero.
* **Negative boundary shortcut** fills `minimum` / `most negative raw` /
  `maximum` from the current definition, so signed boundaries can be
  exercised without arithmetic.
* **Frame length** can be shorter or longer than the definition. Short
  frames are refused with one finding per signal whose bits no longer
  exist; *Send even if pre-flight rejects it* records the reason and emits
  exactly the requested length, dropping out-of-frame signals rather than
  inventing zeros.
* The bit map under the preview draws **8 byte rows × 8 bits** (MSB left);
  missing bytes are empty, not applicable bits are cross-hatched.

### Receiver

* Nothing is dropped. Every frame — including rejected ones — keeps its
  bytes, hex, ASCII and requested length.
* *Load sample frames* replays the 15 scenarios in
  `fixtures/frames/samples.json`.
* *Import raw frame* accepts hex directly (`01 02 03 AB CD EF`).
* The detail panel shows: verdict and findings (with source lines), the
  byte grid with the active signal outlined, a per-signal table with
  **raw unsigned / raw signed / physical / unit / text enum / status**, and
  the cantools cross-check in its own panel.

### A/B compare

Pick a recorded frame and two databases. The definition diff lists every
difference for that message (start, length, byte order, sign, scale,
offset, unit, multiplexer parent, missing signals), and the two decode
columns are computed from the same bytes. This is the "same raw frame, two
definitions" view.

### Export

Raw integers, physical values and text enums are **separate column
groups**. Untick *raw* and the CSV/JSON contains no raw columns; the
header comment records which groups travelled. One row per
`frame × database × signal`, so A/B rows line up and a not-applicable row
carries an empty value rather than a fabricated zero.

---

## Layout

```
canlab/
  bits.py            bit-exact Intel/Motorola layout, extraction, encoder
  dbc.py             tolerant, line-aware DBC parser (no library)
  decode.py          raw / signed / physical / enum kept separate
  validators.py      definition findings, frame verdicts, sender pre-flight
  library_oracle.py  cantools load + decode cross-check, kept at arm's length
  session.py         in-memory frames shared by sender and receiver
  export.py          column-group CSV/JSON export
  server.py          stdlib HTTP + JSON API
  static/            vanilla HTML/CSS/JS front end
fixtures/dbc/        demo_ecu.dbc, conflict_a.dbc, conflict_b.dbc
fixtures/frames/     samples.json (15 scenarios)
docs/                library-vs-business.md, fixtures.md
tests/               pytest suite + headless jsdom UI harness
```

The three diagnostic channels are documented in
[docs/library-vs-business.md](docs/library-vs-business.md); the fixture
conflict map is in [docs/fixtures.md](docs/fixtures.md).

---

## The short version of the boundary

| | definition | frame | library |
|---|---|---|---|
| about | the DBC file | one transmission | cantools |
| example | `signals_overlap` at lines 52–53, bits 12–15 | `frame_length_mismatch` 6 ≠ 8, `signal_unreadable` LampState bit 63 | `DecodeError: Wrong data size: 6 instead of 8 bytes` at `database/utils.py:166` |
| effect | reported, database still loads | accepted/rejected, frame always kept | shown verbatim, never converted into a verdict |

A multiplexer branch that is not selected is a fourth state:
`not_applicable`, with no raw and no physical value, and the reason
recorded (`multiplexer Mode=5; this signal is defined only for 2`).
cantools encodes the same rule as a `DecodeError`; the oracle says so in
plain language instead of surfacing it as a fault.

---

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q          # 119 tests
```

The suite covers the bit engine (including an exhaustive parity sweep
against cantools over all start/length/order/sign layouts that fit in 8
bytes), the parser, the decoder's not-applicable semantics, definition and
frame validators, sender pre-flight and encoding, the export column
groups, the HTTP API, and the library/business boundary. Tests that need
cantools skip cleanly when it is not installed.

The UI has a separate headless harness (jsdom) that drives the real page
against a running server:

```bash
.venv/bin/python -m canlab --port 8792 --load demo_ecu.dbc \
    --load conflict_a.dbc --load conflict_b.dbc &
npm --prefix /tmp/uitest install jsdom@24
NODE_PATH=/tmp/uitest/node_modules CANLAB_BASE=http://127.0.0.1:8792 \
    node tests/ui_smoke.js
```
