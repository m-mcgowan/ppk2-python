# ppk2-python

Python library and CLI for the Nordic Power Profiler Kit II (PPK2).

Features:
- **Device control** — source/ampere meter, voltage, DUT power, 100kHz measurement
- **File I/O** — save/load `.ppk2` files (nRF Connect Power Profiler compatible)
- **Reporting** — markdown tables, interactive HTML charts, GitHub Actions annotations
- **Synthetic profiles** — build realistic power profiles programmatically
- **AI integration** — generate, analyze, and validate profiles using Claude
- **Desktop automation** — open `.ppk2` files in nRF Connect via Playwright
- **GitHub Action** — power profiling reports in CI workflows

## Installation

```bash
pip install ppk2-python              # core library
pip install ppk2-python[report]      # + plotly HTML charts
pip install ppk2-python[desktop]     # + Playwright for nRF Connect automation
pip install ppk2-python[ai]          # + Anthropic SDK for Claude integration
pip install ppk2-python[all]         # everything
```

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
