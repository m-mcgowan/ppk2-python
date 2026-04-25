# ppk2-python

Python library and CLI for the Nordic Power Profiler Kit II (PPK2).

## Features

- **Device control** — source/ampere meter, voltage, DUT power, 100kHz measurement
- **File I/O** — save/load `.ppk2` files (nRF Connect Power Profiler compatible) — see [file format](docs/file_format.md)
- **Daemon server** — keeps the PPK2 open so multiple consumers can share one device. Required for source meter workflows on macOS — see [daemon guide](docs/daemon.md)
- **Event annotation** — overlay DUT firmware trace events (Chrome JSON scopes) onto PPK2 digital channels D0–D7 for visual correlation against the power trace
- **Reporting** — markdown tables, interactive HTML charts, GitHub Actions annotations
- **Synthetic profiles** — build realistic profiles programmatically with a phase/ramp builder
- **AI integration** — generate, analyze, and validate profiles using Claude — see [AI integration](docs/ai.md)
- **Desktop automation** — open `.ppk2` files in nRF Connect via Playwright — see [desktop](docs/desktop.md)
- **GitHub Action** — power profiling reports in CI workflows — see [GitHub Action](docs/github_action.md)
- **Firmware management** — query, check against upstream, and flash PPK2 firmware over USB — see [firmware](docs/firmware.md)

## Status

[![CI](https://github.com/m-mcgowan/ppk2-python/actions/workflows/ci.yml/badge.svg)](https://github.com/m-mcgowan/ppk2-python/actions/workflows/ci.yml)

[Live example reports](https://m-mcgowan.github.io/ppk2-python/) are
regenerated on every build.

<!-- TODO: add screenshot of the generated test report at docs/img/report-screenshot.png and uncomment:
![Generated test report](docs/img/report-screenshot.png)
-->


## Installation

```bash
pip install ppk2-python              # core library
pip install ppk2-python[report]      # + plotly HTML charts
pip install ppk2-python[desktop]     # + Playwright for nRF Connect automation
pip install ppk2-python[ai]          # + Anthropic SDK for Claude integration
pip install ppk2-python[all]         # everything
```

## Quick Start

### List connected devices

```bash
ppk2 list
```

### Take a measurement (ampere meter mode — DUT has its own supply)

A one-shot CLI command works fine in ampere meter mode because the PPK2 only
measures; nothing about closing the serial port disturbs the DUT.

```bash
ppk2 measure 5.0 -o capture.ppk2
ppk2 info capture.ppk2
ppk2 report capture.ppk2 --html report.html
```

Or from Python:

```python
from ppk2 import PPK2Device, save_ppk2

with PPK2Device.open() as ppk:
    result = ppk.measure(duration_s=5.0)
    print(f"Mean: {result.mean_ua:.1f} uA")
    save_ppk2(result, "capture.ppk2")
```

### Source meter mode — use the daemon

In source meter mode the PPK2 itself supplies the DUT rail, and closing the
serial port causes the rail to collapse (DTR drop on close → DUT power off).
A sequence of one-shot CLI calls — `voltage`, `power on`, `measure` — would
reset the DUT between every command. Hold the port open with the daemon
instead:

```python
from pathlib import Path
from ppk2.daemon import start_daemon
from ppk2.client import DaemonClient

serial, _ = start_daemon()                   # auto-detects a single PPK2
sock = Path(f"~/.local/state/ppk2/{serial}.sock").expanduser()

with DaemonClient(sock, serial) as client:
    client.use_source_meter()
    client.set_source_voltage(3700)
    client.toggle_dut_power(True)
    result = client.measure(duration_s=5.0)
    print(f"Mean: {result.mean_ua:.1f} uA")
```

`DaemonClient` exposes the same surface as `PPK2Device`, so application code
can switch between direct and daemon-backed access without other changes.

### Multiple PPK2s

Daemons are keyed by serial number. Spin one up per device, then address
each by serial:

```python
from ppk2.transport import list_ppk2_devices
from ppk2.daemon import start_daemon
from ppk2.client import connect_to_daemon

for dev in list_ppk2_devices():
    start_daemon(port=dev.port)

with connect_to_daemon(serial="C9F6358A") as a, \
     connect_to_daemon(serial="AB123456") as b:
    print(a.measure(2.0).mean_ua, b.measure(2.0).mean_ua)
```

See the [daemon guide](docs/daemon.md) for the full lifecycle (startup,
shutdown, multi-device state directory, and the underlying limitations).

### Load and inspect a `.ppk2` file

```python
from ppk2 import load_ppk2

result = load_ppk2("capture.ppk2")
print(f"Samples: {result.sample_count:,}")
print(f"Mean: {result.mean_ua:.1f} uA, Peak: {result.max_ua:.1f} uA")
```

### Build a synthetic profile

```python
from ppk2 import ProfileBuilder, save_ppk2

profile = (
    ProfileBuilder(seed=42)
    .phase("deep_sleep", current_ua=3.5, duration_s=5.0, noise_ua=0.5)
    .ramp("wakeup", start_ua=3.5, end_ua=15_000, duration_s=0.05)
    .phase("radio_tx", current_ua=45_000, duration_s=0.2, noise_ua=2000)
    .ramp("shutdown", start_ua=45_000, end_ua=3.5, duration_s=0.01)
    .phase("deep_sleep", current_ua=3.5, duration_s=5.0, noise_ua=0.5)
    .build()
)
save_ppk2(profile, "synthetic.ppk2")
```

### Annotate a capture with DUT trace events

If your firmware emits Chrome JSON scope events over a serial link
(`{"ph":"B"/"E","ts":...,"name":"..."}`), overlay them as D0–D7 logic
channels on the PPK2 capture:

```python
from ppk2.events import parse_serial_events
from ppk2.ppk2file import save_ppk2

mapper = parse_serial_events(dut_serial_output, {"gps": 0, "radio": 1})
mapper.apply(measurement)
save_ppk2(measurement, "annotated.ppk2",
          events=mapper.to_scopes(measurement))
```

The HTML report renders embedded scopes as a "Named scopes" table without
needing a sidecar legend. Events outside the capture window are clamped and
a warning is logged. See
[`examples/capture_with_events.py`](examples/capture_with_events.py) for a
runnable walkthrough including the daemon-client / serial-reader pattern
for real hardware.

## CLI

```
ppk2 list                                   List connected PPK2 devices
ppk2 power on|off [--port ...]              Toggle DUT power
ppk2 mode source|ampere [--port ...]        Set measurement mode
ppk2 voltage 3300 [--port ...]              Set source voltage (mV)
ppk2 measure 5.0 [-o capture.ppk2]          Measure for N seconds
ppk2 info <file.ppk2>                       Show file statistics
ppk2 report <files...> --html report.html   Generate reports
ppk2 open <file.ppk2>                       Open in nRF Connect
ppk2 generate "<description>" -o out.ppk2   Generate from text (requires AI)
ppk2 analyze <file.ppk2>                    Analyze with Claude (requires AI)
ppk2 validate <file.ppk2> --spec "<spec>"   Validate against spec (requires AI)
ppk2 merge <trace.json> <capture.ppk2>      Merge Chrome trace with PPK2 power
ppk2 firmware [info|check|upgrade|...]      Query or upgrade PPK2 firmware
```

The single-command CLI shapes (`ppk2 power on`, `ppk2 voltage 3300`, etc.)
work directly in ampere meter mode. In source meter mode, drive the PPK2
through the [daemon](docs/daemon.md) so the rail isn't reset between
commands.

### A few worked examples

```bash
# HTML report with pass/fail thresholds
ppk2 report deep_sleep.ppk2 gps_fix.ppk2 \
    --thresholds '{"deep_sleep": 10, "gps_fix": 50000}' \
    --html report.html

# Generate a synthetic profile from natural language
ppk2 generate "BLE beacon: 3uA deep sleep, wakes every 2s to TX at 8mA for 5ms" \
    -o beacon.ppk2

# Open in nRF Connect for visual comparison
ppk2 open beacon.ppk2

# Validate against a specification
ppk2 validate recording.ppk2 \
    --spec "Deep sleep should be 3-5uA. GPS acquisition under 50mA for max 60s."
```

## Development

```bash
git clone https://github.com/m-mcgowan/ppk2-python.git
cd ppk2-python
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -v
```
