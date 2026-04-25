# AI Integration

The `generate`, `analyze`, and `validate` commands and their Python
counterparts use the Anthropic API (Claude) to work with `.ppk2` profiles in
natural language.

## Setup

1. Install the AI extra:

   ```bash
   pip install ppk2-python[ai]
   ```

2. Set your API key:

   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...
   ```

   Get an API key from [console.anthropic.com](https://console.anthropic.com/).
   You need an Anthropic account with API access (usage is billed per-token).

3. That's it. The CLI commands and Python API will use Claude automatically.

## CLI

```bash
# Generate a synthetic profile from natural language
ppk2 generate "BLE beacon: 3uA deep sleep, wakes every 2s to TX at 8mA for 5ms" \
    -o beacon.ppk2

# Analyze a recording
ppk2 analyze recording.ppk2 --context "GPS cold fix acquisition test"

# Validate against a specification
ppk2 validate recording.ppk2 \
    --spec "Deep sleep should be 3-5uA. GPS acquisition under 50mA for max 60s. Tracking mode 10-20mA."
```

## Python API

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
    spec="Sleep current must be under 5uA. TX burst under 200mA. Total cycle under 30s.",
)
print(f"{'PASS' if validation.passed else 'FAIL'}")
print(validation.report)
```

## Model selection

All AI functions accept a `model` parameter (see the function signature in
`src/ppk2/ai.py` for the current default). Override on a per-call basis:

```python
gen = generate_profile("...", model="claude-opus-4-7")
```

CLI:

```bash
ppk2 generate "..." --model claude-opus-4-7
```

## Cost

Typical token usage per call:

- `generate`: ~500 input + ~500 output tokens
- `analyze`: ~2000 input + ~1000 output tokens
- `validate`: ~2500 input + ~1000 output tokens

At Sonnet pricing this is fractions of a cent per call.
