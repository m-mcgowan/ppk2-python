# `.ppk2` File Format

Files written by this library are byte-for-byte compatible with [nRF Connect
Power Profiler](https://www.nordicsemi.com/Products/Development-tools/nRF-Connect-for-Desktop)
— either tool can open the other's output.

A `.ppk2` file is a ZIP archive containing:

| File | Contents |
|------|----------|
| `session.raw` | 6-byte frames: `Float32LE` current (uA) + `Uint16LE` digital channels |
| `metadata.json` | Sampling rate, start timestamp, format version |
| `minimap.raw` | Downsampled min/max pairs for the overview chart |

Optional members written by this library when present:

| File | Contents |
|------|----------|
| `events.json` | Embedded named scopes, used by the HTML report's "Named scopes" table when no legend sidecar is provided |

## Loading

```python
from ppk2 import load_ppk2

result = load_ppk2("recording.ppk2")
print(result.sample_count, result.duration_s)
```

The implementation lives in [`ppk2file.py`](../src/ppk2/ppk2file.py) and the
low-level decoders in [`parser.py`](../src/ppk2/parser.py) and
[`conversion.py`](../src/ppk2/conversion.py).
