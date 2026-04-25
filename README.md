# ppk2-python

Python library and CLI for the Nordic Power Profiler Kit II (PPK2).

Features:
- **Device control** — source/ampere meter, voltage, DUT power, 100kHz measurement
- **File I/O** — save/load `.ppk2` files (nRF Connect Power Profiler compatible)
- **Daemon server** — persistent process holds the PPK2 open so multiple consumers (CLI, scripts, long-running captures) can share a single device without re-enumerating USB
- **Event annotation** — map DUT firmware trace events (Chrome JSON scopes) onto the PPK2's digital channels D0–D7; overlays named scopes on the power trace for visual correlation
- **Reporting** — markdown tables, interactive HTML charts, GitHub Actions annotations
- **Synthetic profiles** — build realistic power profiles programmatically
- **AI integration** — generate, analyze, and validate profiles using Claude
- **Desktop automation** — open `.ppk2` files in nRF Connect via Playwright
- **GitHub Action** — power profiling reports in CI workflows
- **Firmware management** — query, check against upstream, and flash PPK2 firmware over USB (remote-friendly alternative to the nRF Connect GUI)

## Status

[![CI](https://github.com/m-mcgowan/ppk2-python/actions/workflows/ci.yml/badge.svg)](https://github.com/m-mcgowan/ppk2-python/actions/workflows/ci.yml)
[Example reports (live)](https://m-mcgowan.github.io/ppk2-python/) · [Workflow runs](https://github.com/m-mcgowan/ppk2-python/actions)

| Feature | Implementation | Tests | Examples |
|---------|----------------|-------|----------|
| **Device control** | [device.py](src/ppk2/device.py), [transport.py](src/ppk2/transport.py) | [test_device](tests/test_device.py), [test_integration](tests/test_integration.py) | |
| **File I/O** | [ppk2file.py](src/ppk2/ppk2file.py), [conversion.py](src/ppk2/conversion.py), [parser.py](src/ppk2/parser.py) | [test_ppk2file](tests/test_ppk2file.py), [test_parser](tests/test_parser.py), [test_conversion](tests/test_conversion.py) | |
| **Reporting** | [report.py](src/ppk2/report.py) | [test_report](tests/test_report.py) | [generate_reports](examples/generate_reports.py) |
| **Synthetic profiles** | [synthetic.py](src/ppk2/synthetic.py) | [test_synthetic](tests/test_synthetic.py) | |
| **AI integration** | [ai.py](src/ppk2/ai.py) | [test_ai](tests/test_ai.py) | |
| **CLI** | [cli.py](src/ppk2/cli.py), [commands.py](src/ppk2/commands.py) | [test_commands](tests/test_commands.py) | |
| **Daemon server** | [daemon.py](src/ppk2/daemon.py), [client.py](src/ppk2/client.py) | [test_daemon](tests/test_daemon.py) | |
| **Event annotation** | [events.py](src/ppk2/events.py), [merge.py](src/ppk2/merge.py) | [test_events](tests/test_events.py), [test_merge](tests/test_merge.py) | [capture_with_events](examples/capture_with_events.py) |
| **Firmware management** | [firmware.py](src/ppk2/firmware.py) | [test_firmware](tests/test_firmware.py), [test_cli_firmware](tests/test_cli_firmware.py) | |
| **Desktop automation** | [desktop.py](src/ppk2/desktop.py) | — | |
| **GitHub Action** | [action.yml](action.yml), [action_report.py](action_report.py) | — | |

## Installation

```bash
pip install ppk2-python              # core library
pip install ppk2-python[report]      # + plotly HTML charts
pip install ppk2-python[desktop]     # + Playwright for nRF Connect automation
pip install ppk2-python[ai]          # + Anthropic SDK for Claude integration
pip install ppk2-python[all]         # everything
```

### Optional: `nrfutil` (for `ppk2 firmware` commands)

Querying or flashing PPK2 firmware requires Nordic's `nrfutil` with the
`device` and `nrf5sdk-tools` subcommands. On macOS:

```bash
brew install --cask nrfutil
nrfutil install device nrf5sdk-tools
```

On Linux, download `nrfutil` from
<https://www.nordicsemi.com/Products/Development-tools/nrf-util> and run
`nrfutil install device nrf5sdk-tools`. The rest of the library works
without `nrfutil`.

### `ppk2 firmware` usage

```bash
ppk2 firmware                          # show running firmware version
ppk2 firmware check                    # compare against the latest upstream release
ppk2 firmware upgrade --yes            # download latest from nRF Connect repo and flash
ppk2 firmware upgrade --hex fw.hex     # flash a user-supplied hex
```

The upgrade path downloads the hex from the nRF Connect Power Profiler
GitHub repo, wraps it into an unsigned SDFU zip (matching what the GUI
app does), and programs it via the PPK2's USB DFU bootloader. Stop any
running `ppk2` daemon for that device before upgrading.

## Quick Start

### Measure with PPK2 hardware

```python
from ppk2 import PPK2Device, save_ppk2

with PPK2Device.open() as ppk:
    ppk.use_source_meter()
    ppk.set_source_voltage(3700)
    ppk.toggle_dut_power(True)
    result = ppk.measure(duration_s=5.0)
    print(f"Mean: {result.mean_ua:.1f} uA")
    save_ppk2(result, "measurement.ppk2")
```

### Load and inspect a .ppk2 file

```python
from ppk2 import load_ppk2

result = load_ppk2("measurement.ppk2")
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
mapper.apply(measurement)          # adds logic bits to each sample
save_ppk2(measurement, "annotated.ppk2")
mapper.save_legend("annotated.ppk2.legend.json")
```

To make the `.ppk2` file self-contained, embed the scope intervals
inside it. The HTML report will then render them as a "Named scopes"
table without needing the legend sidecar:

```python
save_ppk2(measurement, "annotated.ppk2",
          events=mapper.to_scopes(measurement))
```

Events with timestamps outside the capture window are clamped and a
warning is logged — if you see that warning, your device-clock and
capture-start aren't aligned yet. See
[`examples/capture_with_events.py`](examples/capture_with_events.py) for
a runnable walkthrough including the daemon-client/serial-reader pattern
for real hardware.

## CLI

```
ppk2 info <file.ppk2>                      Show file statistics
ppk2 report <files...> --html report.html   Generate reports
ppk2 open <file.ppk2>                       Open in nRF Connect
ppk2 generate "<description>" -o out.ppk2   Generate from text (requires AI)
ppk2 analyze <file.ppk2>                    Analyze with Claude (requires AI)
ppk2 validate <file.ppk2> --spec "<spec>"   Validate against spec (requires AI)
```

### Examples

```bash
# Quick stats
ppk2 info recording.ppk2

# HTML report with pass/fail thresholds
ppk2 report deep_sleep.ppk2 gps_fix.ppk2 \
    --thresholds '{"deep_sleep": 10, "gps_fix": 50000}' \
    --html report.html

# Generate a synthetic profile from natural language
ppk2 generate "BLE beacon: 3uA deep sleep, wakes every 2s to TX at 8mA for 5ms" \
    -o beacon.ppk2

# Open in nRF Connect for visual comparison
ppk2 open beacon.ppk2

# Analyze a real measurement
ppk2 analyze recording.ppk2 --context "GPS cold fix acquisition test"

# Validate against a specification
ppk2 validate recording.ppk2 \
    --spec "Deep sleep should be 3-5uA. GPS acquisition under 50mA for max 60s. Tracking mode 10-20mA."
```

## AI Integration

The `generate`, `analyze`, and `validate` commands use the Anthropic API (Claude).

### Setup

1. Install the AI extra:
   ```bash
   pip install ppk2-python[ai]
   ```

2. Set your API key:
   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...
   ```

   Get an API key from [console.anthropic.com](https://console.anthropic.com/). You need an Anthropic account with API access (usage is billed per-token).

3. That's it. The CLI commands and Python API will use Claude automatically.

### Python API

```python
from ppk2.ai import generate_profile, analyze_profile, validate_profile
from ppk2 import load_ppk2, save_ppk2

# Generate from description
gen = generate_profile("nRF9160 LTE-M: PSM sleep 3uA, wake to send 200-byte payload")
print(gen.phase_summary())  # see what Claude generated
save_ppk2(gen.profile, "lte_m.ppk2")

# Analyze a recording
result = load_ppk2("recording.ppk2")
analysis = analyze_profile(result, context="Battery-powered wildlife tracker")
print(analysis)

# Validate against spec
validation = validate_profile(
    result,
    spec="Sleep current must be under 5uA. TX burst under 200mA. Total cycle under 30s."
)
print(f"{'PASS' if validation.passed else 'FAIL'}")
print(validation.report)
```

### Model selection

All AI functions accept a `model` parameter (default: `claude-sonnet-4-5-20250929`):

```python
gen = generate_profile("...", model="claude-opus-4-6")
```

CLI:
```bash
ppk2 generate "..." --model claude-opus-4-6
```

### Cost

Typical token usage per call:
- `generate`: ~500 input + ~500 output tokens
- `analyze`: ~2000 input + ~1000 output tokens
- `validate`: ~2500 input + ~1000 output tokens

At Sonnet pricing this is fractions of a cent per call.

## GitHub Action

Use the included action to generate power profiling reports in CI:

```yaml
# .github/workflows/power-profile.yml
name: Power Profile
on: [workflow_dispatch]

jobs:
  report:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      # Download .ppk2 artifacts from bench runner or previous job
      - uses: actions/download-artifact@v4
        with:
          name: power-profiles
          path: profiles/

      - uses: m-mcgowan/ppk2-python@main
        with:
          files: "profiles/*.ppk2"
          thresholds: '{"deep_sleep": 10, "gps_fix": 50000}'
          html-report: "power-report.html"

      - uses: actions/upload-artifact@v4
        with:
          name: power-report
          path: power-report.html
```

The action:
- Loads `.ppk2` files and generates a markdown summary in `$GITHUB_STEP_SUMMARY`
- Emits `::error::` annotations for any threshold failures
- Produces an interactive HTML report with plotly charts
- Sets `passed` output (`true`/`false`) for conditional workflow steps

## .ppk2 File Format

Files are compatible with [nRF Connect Power Profiler](https://www.nordicsemi.com/Products/Development-tools/nRF-Connect-for-Desktop). The format is a ZIP archive containing:

| File | Contents |
|------|----------|
| `session.raw` | 6-byte frames: `Float32LE` current (uA) + `Uint16LE` digital channels |
| `metadata.json` | Sampling rate, start timestamp, format version |
| `minimap.raw` | Downsampled min/max pairs for overview chart |

## Desktop Automation

Open `.ppk2` files in nRF Connect Power Profiler from the command line:

```bash
pip install ppk2-python[desktop]
playwright install    # one-time browser download
ppk2 open recording.ppk2
```

This launches the app via Playwright's Electron support, automatically loading the specified file.

## Development

```bash
git clone https://github.com/m-mcgowan/ppk2-python.git
cd ppk2-python
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -v
```
