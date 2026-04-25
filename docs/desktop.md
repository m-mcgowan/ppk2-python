# Desktop Automation

Open `.ppk2` files in [nRF Connect Power
Profiler](https://www.nordicsemi.com/Products/Development-tools/nRF-Connect-for-Desktop)
from the command line. Useful for inspecting captures visually without
clicking through the app's file picker.

## Setup

```bash
pip install ppk2-python[desktop]
playwright install   # one-time browser download
```

## Usage

```bash
ppk2 open recording.ppk2
```

The app launches via Playwright's Electron support, with the specified file
preloaded. By default `ppk2 open` waits for the app to close — pass
`--no-wait` to return immediately.
