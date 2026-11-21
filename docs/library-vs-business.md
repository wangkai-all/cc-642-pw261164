# Where the library stops and CANLab begins

CANLab uses [cantools](https://github.com/cantools/cantools) as an
independent second opinion. It never uses cantools to decide what a frame
means, what a DBC is allowed to say, or whether a scenario is acceptable.
Those are three different questions, and mixing them is how tools end up
printing `UnsupportedDatabaseFormatError` at someone who just wanted to
look at four bytes.

This document is the contract. The code that implements it lives in three
modules, one per channel:

| Channel | Module | Question it answers |
|---|---|---|
| `library` | `canlab/library_oracle.py` | What did cantools say, and why? |
| `definition` | `canlab/validators.py` (`validate_database`) | Is this DBC internally consistent? |
| `frame` | `canlab/validators.py` (`check_frame`, `validate_for_send`) | Is this particular frame / send request acceptable? |

The API returns all three side by side. The UI renders them in separate
panels. No code path copies a library exception into a business finding or
vice versa.

---

## 1. The library channel

`load_with_cantools()` calls the library **twice**:

```
cantools.database.load_string(text, database_format="dbc", strict=True)
cantools.database.load_string(text, database_format="dbc", strict=False)
```

Each attempt is recorded as a `LibraryDiagnostic` with five fields:

| Field | Meaning |
|---|---|
| `call` | The exact call string, so it can be reproduced |
| `exception` | The exception **class name** (`UnsupportedDatabaseFormatError`, `DecodeError`, …) |
| `message` | The library's own message, unedited |
| `detail` | Where inside cantools it was raised (`database/can/message.py:992`) |
| `business_meaning` | What that same situation means to CANLab |

The strict call answers *"would a standard toolchain accept this file?"*.
The lenient call is what CANLab runs on. When strict fails and lenient
succeeds, that difference is itself reported — it is the documented
strict/lenient split, not an error:

```
strict loading failed but lenient loading succeeded; cantools can model
the database only when its consistency checks are disabled
```

For `fixtures/dbc/conflict_a.dbc` the diagnostics look like this (fields
abridged; this is the JSON the page receives):

```json
{
  "call": "cantools.database.load_string(text, database_format='dbc', strict=True)",
  "exception": "UnsupportedDatabaseFormatError",
  "message": "DBC: \"The signals TorqueRaw and PackVoltage are overlapping in message ConflictOverlap.\"",
  "detail": "raised at database/can/database.py:… The strict loader rejects the whole database, so the offending definition cannot be inspected through cantools' normal API.",
  "business_meaning": "A standard toolchain will refuse this database. CANLab loads it anyway and shows the conflict at its source line; this is a *definition* problem, not a decoding failure."
}
```

Note what is **not** in that object: no CANLab finding code, no
"rejected", no severity. The library channel never asserts a business
verdict.

---

## 2. The definition channel

`validate_database()` is static analysis of the DBC that CANLab parsed
itself. It runs once at load time and its findings are attached to the
loaded database. Each finding is a `Finding` with:

* `code` — a stable identifier (`signals_overlap`, `signal_out_of_frame`,
  `multiplexer_missing`, `signal_zero_scale`, …),
* `severity` — `error` / `warning` / `info`, **CANLab's** judgement,
* `scope` — always `definition` here,
* `location` — message name, message source line, signal name, signal
  source line, sawtooth start, length, byte order, linear bit offsets,
  first/last bit. For an overlap both signals are included (`a` and `b`)
  plus the exact `shared_linear_bits` list.

The definition channel is what makes a conflict **locatable**. For the
overlap in `conflict_a.dbc`:

```
signals_overlap  error
  'PackVoltage' and 'TorqueRaw' in 'ConflictOverlap' share 4 bit(s)
  location.message_line = 51
  location.a = {signal: PackVoltage, signal_line: 52, start: 0, length: 16, …}
  location.b = {signal: TorqueRaw,   signal_line: 53, start: 12, length: 16, …}
  location.shared_linear_bits = [12, 13, 14, 15]
```

Those linear bits are the same coordinates the bit map draws, so the
conflict is visible in the frame as well as in the file.

Crucially, a database with definition findings **still loads and still
decodes**. Definition findings describe the DBC; they do not stop the
receiver from reading bytes, and they do not make a frame "rejected".

---

## 3. The frame channel

Two functions, both pure and both unaware of cantools:

`check_frame(msg, payload, decoded)` runs after every received frame and
produces a `FrameVerdict`:

| Code | Severity | Meaning |
|---|---|---|
| `frame_length_mismatch` | error | DLC ≠ the message's declared length |
| `signal_unreadable` | error | A signal's bits fall outside the received bytes |
| `physical_out_of_declared_range` | warning | Bits are valid; the value violates the DBC range |

`validate_for_send(...)` runs before the sender encodes:

| Code | Severity | Meaning |
|---|---|---|
| `send_length_invalid` | error | Length outside 0..64 |
| `send_length_differs_from_definition` | warning | Allowed on purpose, so lengths can be exercised |
| `send_signal_outside_frame` | error | The signal's bits do not exist at this length |
| `send_signal_not_applicable` | error | The selected multiplexer branch does not define the signal |
| `send_value_below_defined_minimum` | warning | Encodable, but outside the declared range |
| `send_value_above_defined_maximum` | warning | Same, above |

A frame is **accepted** unless it has an `error`-severity finding. Warnings
are data-quality observations, and the frame is still stored with its
bytes.

### "Not applicable" is a fourth state

It is neither a value nor an error. When a multiplexer selects a branch
that a signal does not belong to, the decoder returns:

```json
{
  "name": "ChargeVoltage",
  "status": "not_applicable",
  "reason": "multiplexer Mode=5; this signal is defined only for 2",
  "bit_offsets": [23, 22, …, 8],
  "raw_unsigned": null,
  "raw_signed": null,
  "physical": null,
  "enum_text": null
}
```

The bits still have a *layout* — the bit map cross-hatches them — but no
value. Returning `0` there would be indistinguishable from a real zero.
`check_frame` does not treat it as an error: nothing about the frame is
wrong; that signal simply has no meaning in this frame.

---

## 4. The difficult cases, side by side

This is the part the tool exists to show. Each row is a scenario where the
library and CANLab say different things — and both are right.

### 4.1 A multiplexer value with no branch

Sample `mux-miss`: `0x101`, bytes `05 11 22 33 44 55 66 01`.

| Channel | Output |
|---|---|
| `library` | `DecodeError: expected multiplexer id 0, 1, 2 or 3, but got 5` — raised inside `database/can/message.py`, reported verbatim with its call site |
| `definition` | *(none)* — the DBC is fine |
| `frame` | **accepted**; `Mode` is active (raw 5) and carries a range warning because 5 is outside its declared 0..3; `MotorRpm`, `MotorCurrent`, `ChargeVoltage`, `ChargeTimer`, `ServiceCode` are `not_applicable` with a reason; `Heartbeat` decodes |

CANLab's `business_meaning` for the library error:

> Business condition, not a library failure: the frame selected a
> multiplexer branch that this signal does not cover. CANLab reports the
> signal as not applicable (the bits belong to the selected branch and are
> never read as this signal); cantools encodes the same rule as a
> `DecodeError`.

**The rule:** an "expected multiplexer id" error is a modelling choice by
the library, not evidence that the frame is bad.

### 4.2 A frame length that disagrees with the definition

Sample `short-frame`: `0x100` in 6 bytes.

| Channel | Output |
|---|---|
| `library` | `DecodeError: Wrong data size: 6 instead of 8 bytes` |
| `definition` | *(none — demo_ecu.dbc is consistent)* |
| `frame` | **rejected**: `frame_length_mismatch` at message line 29, `received_length: 6`; `signal_unreadable` for `AuxFlags` (bit 51), `ThrottlePct` (59), `LampState` (63), each with its signal line |

CANLab keeps the six bytes and decodes the signals that fit
(`Counter`, `Speed`, `CoolantTemp`, `EngineTorque`). The library returns
nothing at all because it refuses the whole frame.

**The rule:** a length mismatch is a frame finding. It is reported against
the definition line *and* the received byte count, and the parts that are
readable stay readable.

### 4.3 Overlapping definitions

Database `conflict_a.dbc`, message `0x302`.

| Channel | Output |
|---|---|
| `library` | strict load: `UnsupportedDatabaseFormatError: … TorqueRaw and PackVoltage are overlapping …`; lenient decode of a frame: `DecodeError: unpacking failed` |
| `definition` | `signals_overlap` naming both signals with their lines and the four shared linear bits |
| `frame` | both overlapping signals decode bit-exactly; `SigOverrun` is `signal_unreadable` because bit 67 does not exist in an 8-byte frame |

**The rule:** the library can model neither the database nor the frame.
CANLab lays the bits out anyway, so the conflict can be *seen* and
*located*, and says exactly which definition is impossible.

### 4.4 A value outside its declared range

Sample `status-overrange`: `EngineTorque = 1010 Nm` against a declared
`[-1000, 1000]`.

| Channel | Output |
|---|---|
| `library` | no error — raw values are in range for 12 signed bits |
| `definition` | *(none)* |
| `frame` | **accepted**, with `physical_out_of_declared_range` (warning) at the signal's line, citing raw 2020 and physical 1010.0 |

**The rule:** "the bits are valid, the value is out of range" is a data
quality note, not a decoding failure and not a library error.

---

## 5. Summary of the boundary

```
                    ┌─────────────────────┐
   DBC text ───────►│  canlab/dbc.py       │  tolerant parse, source lines
                    └─────────┬───────────┘
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
     ┌─────────────┐  ┌──────────────┐  ┌──────────────┐
     │ definition  │  │  bit engine  │  │   library    │
     │ validators  │  │  bits.py     │  │ cantools     │
     │ (source     │  │ decode.py    │  │ oracle, kept │
     │  lines)     │  │              │  │ at arm's     │
     └─────────────┘  └──────┬───────┘  │ length       │
                             │          └──────────────┘
                             ▼
                      ┌──────────────┐
                      │    frame     │  length / readability /
                      │  verdicts    │  range, per received frame
                      └──────────────┘
```

* The **definition** channel is about the file. It never mentions frames.
* The **frame** channel is about one transmission. It never mentions
  exceptions.
* The **library** channel is about cantools. It never assigns a verdict.

If you are looking at a message and cannot tell which channel it came
from, the design has failed. In the JSON, `Finding.scope` is `definition`
or `frame`; library diagnostics always carry a `call` and `exception`.
