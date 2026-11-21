# Fixtures: what each file is for

Everything under `fixtures/` is loaded by the running app; nothing is
generated at runtime. The DBC files are hand-written to exercise one
behaviour each, and the sample frames are the bytes that make those
behaviours visible.

```
fixtures/
  dbc/
    demo_ecu.dbc      a consistent database (no definition findings)
    conflict_a.dbc    interpretation A: 0x300..0x302
    conflict_b.dbc    interpretation B: same ids, different packing
  frames/
    samples.json      15 scenarios, each a payload plus what to look at
```

---

## `demo_ecu.dbc` — the valid baseline

Five messages, covering every feature the tool must support.

| Id | Name | Length | What it demonstrates |
|---|---|---|---|
| `0x100` | VehicleStatus | 8 | 64 bits covered exactly once; Intel + Motorola + signed + scale + offset + text enum |
| `0x101` | BodyCommand | 8 | root multiplexer `Mode` with branches 1, 2, 3; branch 0 selects nothing |
| `0x102` | DiagNested | 8 | nested multiplexing via `SG_MUL_VAL_`: `Group` → `Sub` → `LeafA`/`LeafB` |
| `0x103` | FreeCounter | 8 | a trivial message used for length experiments |
| `0x18FF50E5` | ChargerStatus | 8 | 29-bit extended frame; both wide signals Motorola |

### `0x100` bit map

```
bit  0..7    Counter       Intel  unsigned          1
bit  8..23   Speed         Intel  unsigned  ×0.01   km/h
bit 24..31   CoolantTemp   Intel  unsigned  −40     degC
bit 32..47   EngineTorque  Motorola signed ×0.5     Nm    (DBC start 39 = MSB)
bit 48..51   AuxFlags      Intel  4 independent flags (text enum on 0/5/15)
bit 52..59   ThrottlePct   Intel  unsigned  ×0.4    %
bit 60..63   LampState     Intel  text enum: Off/On/Blink/Fault
```

`EngineTorque` is the Motorola case: DBC start `39` is the **MSB**, so the
signal walks `39..32` then `47..44` in sawtooth order, i.e. linear bits
`32..39` then `44..47` MSB-first. That is exactly what the bit map draws.

### `0x101` multiplexing

```
Mode=0  no branch          -> every m* signal is not applicable
Mode=1  MotorRpm, MotorCurrent
Mode=2  ChargeVoltage, ChargeTimer
Mode=3  ServiceCode
```

Branch 0 existing but selecting nothing is deliberate. It is the cleanest
case of "the bits are present, the definition says nothing about them".

### `0x102` nested multiplexing

```
Group=1 -> Sub is read
             Sub=2 -> LeafA
             Sub=3 -> LeafB
Group!=1 -> Sub is not read, so LeafA and LeafB are not applicable
```

`LeafA`/`LeafB` are written with the plain `m2`/`m3` marker; the parent is
resolved from the `SG_MUL_VAL_` statements at the bottom of the file.

---

## `conflict_a.dbc` and `conflict_b.dbc` — the same ids, two stories

Both are syntactically valid DBCs. They disagree about what the bytes
mean, and in two places each is internally broken. That is the point:
the tool must locate the conflict, not refuse to open the files.

### `0x300` Powertrain

| Field | A (8 bytes) | B (6 bytes!) |
|---|---|---|
| Rpm | `0\|16` Intel | `0\|16` Intel |
| Torque | `16\|12` Intel signed ×0.5 | `23\|16` **Motorola** signed ×1 |
| Pedal | `28\|8` ×0.4, offset 0 | `32\|8` ×0.5, offset −20 |
| Gear | `36\|4` | `40\|4` |
| OilPressure | `40\|8` ×4 kPa | `44\|4` ×2 kPa |
| Cruise | `48\|1` | `48\|1` — **outside the 6-byte message** |
| Reserved | `49\|15` | `49\|15` — **outside the 6-byte message** |

* **A is consistent; B is not.** B's declared length is 6 bytes, so
  `Cruise` and `Reserved` cannot exist. This is a `definition` finding
  (`signal_out_of_frame`), and a strict toolchain refuses B at load.
* **Both are loadable by CANLab.** A frame recorded at 8 bytes is
  accepted by A and rejected by B (`frame_length_mismatch`), so the same
  bytes show two verdicts side by side.

### `0x301` Thermal

| Selector | A | B |
|---|---|---|
| `Mode=0` | — | — |
| `Mode=1` | `FanDuty` ×0.4 % | `FanDuty` ×0.5 % |
| `Mode=2` | `PumpRpm` **rpm** | `PumpPressure` **bar** ×0.01 |

Same bits, same selector value, different unit and different quantity. The
A/B compare reports `signal_missing_in_left` / `signal_missing_in_right`
plus the scale difference on `FanDuty`.

### `0x302` ConflictOverlap

| Finding | A | B |
|---|---|---|
| `PackVoltage` `0\|16` vs `TorqueRaw` | `12\|16` → **overlap on linear bits 12..15** | `16\|16` → overlap fixed |
| `SigOverrun` `60\|8` | reaches linear bit 67 > 63 | same |

* A is the "two owners of the same bits" case. `definition` reports
  `signals_overlap` with both source lines and the shared bit list;
  cantools refuses even lenient decoding with `unpacking failed`.
* B inherits only the out-of-frame tail. Decoding the same bytes with B
  gives different values for `TorqueRaw` because it moved.
* Both B messages are strict-refused at load for their own reasons
  (B: `Cruise` does not fit; the overlap is gone).

---

## Sample frames (`fixtures/frames/samples.json`)

Each scenario is recorded by the local sender (or imported byte-for-byte
when `raw_hex` is present), then decoded at receive time against every
loaded database.

| Id | What it shows |
|---|---|
| `status-normal` | a clean `0x100` frame: raw / physical / enum all populated |
| `status-negative-edge` | signed boundary: `EngineTorque = -512` is raw `0xC00` |
| `status-overrange` | `EngineTorque = 1010 Nm` > declared 1000: accepted + range warning |
| `mux-drive` | `Mode=1`: drive branch active, charge/service not applicable |
| `mux-charge` | `Mode=2`: charge branch active |
| `mux-miss` | raw `05 11 22 33 44 55 66 01`: **every branch not applicable**, never zero |
| `nested-leafa` | `Group=1, Sub=2`: LeafA decodes, LeafB not applicable |
| `nested-blocked` | raw `55 09 …`: `Group=5` blocks `Sub`, so both leaves are not applicable |
| `extended-charger` | 29-bit `0x18FF50E5`, Motorola voltage/current, text enum |
| `short-frame` | 6 bytes for an 8-byte definition: rejected, three signals located |
| `long-frame` | 10 bytes: extra bytes preserved, definition still says 8 |
| `unknown-id` | `0x103` is not in `conflict_a.dbc`: "no message", not a decode failure |
| `ab-powertrain` | same bytes as `0x300` in A and B; A accepts, B rejects |
| `ab-thermal` | `Mode=2` reads `PumpRpm` (rpm) in A, `PumpPressure` (bar) in B |
| `ab-overlap` | overlap in A; `SigOverrun` unreadable in both |

### Verified expectations

The `expect`-style claims above are asserted in the test suite rather than
only written here:

* `tests/test_decode.py` — not-applicable has no value; signed raw is kept
  alongside unsigned; nested resolution blocks children.
* `tests/test_validators.py` — every definition finding carries its source
  line; frame verdicts separate length, readability and range.
* `tests/test_library_boundary.py` — the three channels stay apart, and
  cantools' three error classes map to the business meaning documented in
  `docs/library-vs-business.md`.
