# Daemon Streaming Guide

The PPK2 daemon holds the serial port open persistently, avoiding macOS DTR-drop-on-close
which would cut DUT power. Clients connect via Unix socket to control the PPK2 and stream
measurement data.

## Starting the Daemon

```python
from ppk2.daemon import start_daemon
from ppk2.client import DaemonClient
from pathlib import Path

serial_num, pid = start_daemon(port="/dev/cu.usbmodemXXXX")
client = DaemonClient(
    Path(f"~/.local/state/ppk2/{serial_num}.sock").expanduser(),
    serial_num,
)
client.use_source_meter()
client.set_source_voltage(3700)
client.toggle_dut_power(True)
```

## Streaming Measurement

Two approaches depending on whether you need raw samples or just stats.

### Stats-only (simple)

```python
result = client.measure(duration_s=5.0)
print(f"Mean: {result.mean_ua:.1f} uA")
```

### Raw samples (for waveform analysis, trimming, etc.)

```python
import time

client.start_measuring()
time.sleep(5)

# Drain raw bytes (fast, no per-sample Python overhead)
chunks = []
for _ in range(500):
    chunk = client.read_available()
    if chunk:
        chunks.append(chunk)
    else:
        break
    time.sleep(0.005)

client.stop_measuring()

# Parse in bulk after collection
raw = b"".join(chunks)
samples = client.parse_raw(raw)
```

**Why collect raw bytes instead of calling `read_samples()`?**

`read_samples()` parses each 4-byte frame into a Python `Sample` object during collection.
At 100kHz that's 100K object creations/sec, which causes GIL contention if another thread
is doing I/O (e.g., serial communication with the DUT). The raw byte approach defers all
parsing to after measurement, eliminating contention during the time-critical window.

### Threaded collection (for long or variable-duration measurements)

```python
import threading

raw_chunks: list[bytes] = []
collecting = threading.Event()
collecting.set()

def collect():
    while collecting.is_set():
        chunk = client.read_available()
        if chunk:
            raw_chunks.append(chunk)
        else:
            time.sleep(0.001)

client.start_measuring()
thread = threading.Thread(target=collect, daemon=True)
thread.start()

# ... do other work (serial I/O, sleep detection, etc.) ...

collecting.clear()
thread.join(timeout=2.0)
client.stop_measuring()

samples = client.parse_raw(b"".join(raw_chunks))
```

## Exclusive Port Access

The daemon opens the PPK2 serial port with `exclusive=True` (uses `TIOCEXCL` on macOS).
This prevents other applications (e.g., nRF Connect Power Profiler) from opening the same
port concurrently, which would corrupt the measurement stream.

If you need to use the nRF app, stop the daemon first, use the app, then restart the daemon.

## Known Issues and Fixes

### Stale serial bytes before measurement

The PPK2 serial buffer may contain stale bytes from previous commands or measurements.
If these bytes are forwarded as measurement data, they corrupt the parser's 4-byte frame
alignment, producing incorrect ADC values and range indices.

**Fix (implemented):** The daemon drains stale serial data before sending `average_start`
to the PPK2. This ensures the first bytes the reader thread forwards are valid measurement
frames.

**Symptom if broken:** All samples show constant mA-range values (e.g., 24 mA) even when
the DUT is in deep sleep. The parser reports "Significant data loss" at measurement start.
Sample count may exceed the expected count for the measurement duration (phantom frames from
misaligned parsing).

### Socket buffer sizing

At 100 kHz, the PPK2 produces ~400 KB/s of raw data. Two buffers matter:

- **Client receive buffer** (`SO_RCVBUF`). The client sets this to 1 MB in
  `DaemonClient.start_measuring()` so that a slow reader loop has ~2.5 s of
  headroom before the kernel starts dropping bytes.
- **Daemon send buffer** (`SO_SNDBUF`). The daemon sets this to 1 MB on the
  streaming socket. macOS's default is governed by
  `net.local.stream.sendspace`, which is only **8 KB** by default — under
  that default, `sendall()` blocks after ~20 ms of sample data, causing the
  serial reader thread to fall behind during current bursts. We observed
  ~65% sample loss on an unpatched daemon before enlarging the send buffer
  (see commit `e4f8be6`).

  **Symptom if broken:** large "Significant data loss detected (… samples)"
  warnings in the daemon log during bursty workloads (e.g. radio TX),
  corresponding to gaps in the captured waveform. Quiet periods look fine
  because the kernel buffer drains between sends.

If you're on an older macOS build or a platform where `SO_SNDBUF` tuning
doesn't take effect, you can raise `net.local.stream.sendspace` system-wide:

```
sudo sysctl -w net.local.stream.sendspace=1048576
```

The daemon's `sendall()` still blocks if the client buffer is full, which
causes the daemon's serial read to fall behind. The raw-byte collection
approach (above) minimizes this risk by keeping the client's read loop
fast and free of per-sample processing.
