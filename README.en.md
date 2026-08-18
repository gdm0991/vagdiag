# VAG Diag — read fault codes from VW, Škoda, SEAT and Audi with a cheap ELM327

Reads diagnostic trouble codes from control modules of VAG group vehicles
through an inexpensive ELM327 adapter. Runs on Windows with no Python
installation and no third-party libraries. The interface opens in a browser.

Originally written to find a broken wire in the parking sensor circuit of
a VW Polo Sedan, when the usual apps only showed the engine and left the
parking module unreachable. Published in case it helps someone else.

[Русская версия описания](README.md)

---

## Features

| Feature | Description |
|---|---|
| Module search | Scans addresses, finds every module that answers |
| Identification | Module name and part number |
| Fault codes | Count, code and **failure type** — open circuit, short, no communication |
| Status breakdown | Currently active, confirmed, previously present — per status flag |
| Clearing | Erases the fault memory of a selected module |
| **Fault monitor** | Live counter for finding a bad sensor by unplugging them one by one |
| **Engine, OBD-II** | Codes, readiness monitors, VIN, freeze frame — works on any car |
| **Live parameters** | RPM, temperatures, load, voltage, logged to file |
| **Sensor wizard** | Step-by-step protocol that builds a code-to-sensor map |
| **Before and after** | State snapshots and a diff: what is gone, left, appeared |
| **Battery and alternator test** | Voltage graph during cranking with an automatic verdict |
| Code descriptions | Offline dictionary of standard OBD-II codes |
| Reports | Plain text with the full adapter exchange, plus a printable page |

### Supported adapters

| Type | Connection | Notes |
|---|---|---|
| ELM327 Wi-Fi | network, usually `192.168.0.10:35000` | the most common one |
| ELM327 USB | COM port | same protocol, steadier link |
| ELM327 Bluetooth | COM port after pairing | convenient with a laptop |
| **USB-CAN (SLCAN)** | COM port | **reads long fault lists in full** |

### What it does not do

- No coding, no adaptations, no changing settings. Everything is read-only
  except explicit fault clearing behind a button.
- No manufacturer database descriptions — you get the code number and the
  standard failure type per ISO 14229-1.
- Cheap adapters cannot read long fault lists in full, see «Limitations».

---

## Quick start

1. Download the archive from Releases and **unpack it into its own folder**
2. Plug the adapter into the OBD socket, usually under the steering wheel
3. **Turn the ignition on, do not start the engine**
4. Connect the computer to the adapter's Wi-Fi network
5. Run `START_GUI.bat` — a browser opens with the interface
6. Click Connect, then Modules → Quick search

Python is bundled, nothing to install, no administrator rights needed.

---

## Limitations

### Cheap ELM327 adapters cannot read long answers

ELM327 firmware handles multi-frame transfers only for the engine address
pair (`0x7E0` → `0x7E8`) hard-wired into it. Body electronics modules reply
with a different offset — for example `0x70A` answers from `0x774` — and the
adapter never requests the continuation of a long answer.

This cannot be worked around with commands. Verified:

| Approach | Result |
|---|---|
| `ATCRA` + `ATFCSH` + `ATFCSD` + `ATFCSM1` | accepted, continuation still not requested |
| `ATCAF0` and sending the flow control frame manually | the adapter prepends its own length byte |
| `ATAR` | forces the «request plus eight» rule |
| `ATCRAXXX` | clones answer `?`, command unsupported |

Proof of the frame being altered: sending `03 22 F1 97` makes the module
read `0x03` — our own length byte — as the service number. A flow control
frame starts with `0x30`, which is itself a protocol byte, so the adapter
prepends another one and the frame becomes meaningless.

**What the program does instead.** The declared length is taken from the
first frame header, so **the fault count is always exact**. The first record
arrives complete, giving its code and failure type. Filtering by status flag
changes the list contents, so different codes end up first — that is how
several codes are extracted instead of one.

**The proper fix is built in.** Choose the «USB-CAN, SLCAN» adapter type
and use a CANable, CANtact or compatible device. It hands over raw CAN
frames while the program assembles ISO-TP and sends flow control itself,
so long lists are read completely. Such an adapter costs about the same
as a regular ELM327.

### Other

- Only UDS over CAN with 11-bit addressing. TP 2.0 (older vehicles)
  is not implemented.
- Cheap adapters stop responding after a few dozen commands. The program
  detects a run of empty answers and restarts the adapter by itself;
  if that does not help, unplug it for ten seconds.
- Some modules do not support reading the name or part number. That is
  normal and does not indicate a fault.

---

## Sources are inside the package

**The program ships with its sources.** Everything in the `app` folder is
plain Python and one markup file. Open them in any editor, change them and
run — nothing to build or compile, Python sits right there in the `python`
folder.

1. Open `app\ui.html` or `app\webui.py` in an editor
2. Change it
3. Close the program window and run `START_GUI.bat` again

`DEV_TOOLS.bat` opens the sources folder, runs the smoke tests and starts
the adapter mock so you can verify changes without a car.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the module map, data flow and
extension points, and [CONTRIBUTING.md](CONTRIBUTING.md) for how to test
changes and what would be most welcome. Both documents are in Russian —
translations are very welcome.

---

## Development

Smoke tests run against a built-in adapter mock, no vehicle required:

```
python tests/test_smoke.py
```

They cover frame parsing, multi-frame assembly, code decoding and a full
exchange with the mock. The same tests run automatically on every change
through GitHub Actions.

---

## Contributing

Reports and improvements are welcome. Especially useful:

- `REPORT_TO_SEND.txt` files from other vehicle models — they reveal which
  addresses and protocols are in use;
- information about adapters that **do** read long answers from body
  electronics modules;
- mappings between code numbers and specific sensors or components.

When reporting a problem, attach `REPORT_TO_SEND.txt`. It contains the
program version, the adapter model and the complete exchange, which is
usually enough to work out what happened.

---

## Warning

The program talks to the vehicle electronics. Although everything except
explicit fault clearing is read-only, the author takes no responsibility
for the consequences of use.

Do not use while driving. Diagnostics is done on a stationary vehicle with
the ignition on and the engine off.

Clearing faults erases the failure history and resets readiness monitors,
which then require several dozen kilometres of mixed driving to complete.
Save a report before clearing — erased records cannot be recovered.

---

## License

MIT. Use it, improve it, share it.

The bundled portable Python is distributed under the PSF license
and included unmodified.
