# Daemon

The PPK2 daemon is a long-lived process that keeps the PPK2's serial port open
and accepts commands from clients over a Unix domain socket. Most users won't
interact with it directly — the Python `DaemonClient` and the (planned) `ppk2
daemon` CLI subcommands are the supported interface.

## Why a daemon?

Two limitations of the PPK2 make a one-shot CLI command awkward in source meter
mode.

### 1. DTR drop cuts DUT power

In **source meter mode** the PPK2 drives the DUT rail itself. The `nrf_pwr`
control firmware ties the rail to the host's USB CDC-ACM presence — closing
the serial port (which on macOS unconditionally drops DTR) is interpreted as
"host went away" and the DUT rail collapses.

That means a sequence like

```bash
ppk2 voltage 3300   # set rail
ppk2 power on       # turn rail on
ppk2 measure 5      # take a reading
```

doesn't behave the way the syntax suggests. Each command opens the port,
issues a single request, and closes — so by the time `ppk2 measure 5` opens
the port for the third time, the rail set up by the first two commands is
already gone, and the DUT has just been brown-out reset.

> **TODO:** link to the nRF DevZone forum threads that document this DTR
> dependence in source meter mode. (User to fill in.)

The daemon side-steps this by keeping a single serial connection open for the
lifetime of the daemon process. Clients talk to the daemon, not directly to
the PPK2, so the rail stays up between commands.

In **ampere meter mode** the PPK2 only measures, and an external supply
powers the DUT — so the DTR-on-close behaviour is harmless and a one-shot
CLI command is fine. The daemon is only required for source mode workflows.

### 2. One process per device

The PPK2 USB serial endpoint accepts one open at a time. If you want a
long-running capture script *and* an ad-hoc `ppk2 power off` from a second
shell, both can't open the port directly. With a daemon, both connect to the
same Unix socket and share the underlying device safely.

## Starting and using a daemon

```python
from pathlib import Path
from ppk2.daemon import start_daemon
from ppk2.client import DaemonClient

serial, pid = start_daemon(port="/dev/cu.usbmodemXXXX")
sock = Path(f"~/.local/state/ppk2/{serial}.sock").expanduser()

with DaemonClient(sock, serial) as client:
    client.use_source_meter()
    client.set_source_voltage(3700)
    client.toggle_dut_power(True)
    result = client.measure(duration_s=5.0)
    print(f"Mean: {result.mean_ua:.1f} uA")
```

`DaemonClient` exposes the same surface as `PPK2Device` (`use_source_meter`,
`set_source_voltage`, `toggle_dut_power`, `measure`, `start_measuring` /
`stop_measuring` / `read_available`, `status`, `shutdown`) so application code
can switch between direct and daemon-backed access without other changes.

`connect_to_daemon()` is the simpler entry point if a daemon is already
running:

```python
from ppk2.client import connect_to_daemon

with connect_to_daemon(serial="C9F6358A") as client:
    client.measure(1.0)
```

It calls `find_daemon()` under the hood and raises `ConnectionError` if no
daemon is running for the requested device.

## Multiple PPK2s

Daemons are keyed by **serial number**, not by host or port. Each daemon
gets its own state directory entries:

```
~/.local/state/ppk2/
  C9F6358A.sock   C9F6358A.pid   C9F6358A.log
  AB123456.sock   AB123456.pid   AB123456.log
```

So multiple PPK2s on the same host run as independent daemons that don't
contend for resources.

```python
from ppk2.transport import list_ppk2_devices
from ppk2.daemon import start_daemon, list_daemons

# Spin up one daemon per connected PPK2
for dev in list_ppk2_devices():
    start_daemon(port=dev.port)

# Inspect what's running
for serial, sock_path in list_daemons():
    print(serial, sock_path)
```

Address a specific device by serial number when connecting:

```python
from ppk2.client import connect_to_daemon

with connect_to_daemon(serial="C9F6358A") as a, \
     connect_to_daemon(serial="AB123456") as b:
    result_a = a.measure(2.0)
    result_b = b.measure(2.0)
```

`connect_to_daemon()` (and `find_daemon()`) accept either `serial=` or
`port=`; with no argument and exactly one daemon running, they auto-select
that daemon.

## Stopping a daemon

```python
client.shutdown()             # ask the daemon to exit cleanly
```

or, externally:

```bash
kill "$(cat ~/.local/state/ppk2/<SERIAL>.pid)"
```

The daemon handles `SIGTERM`/`SIGINT`, closes the serial port, and removes
its `.sock` and `.pid` files.

## State directory

```
~/.local/state/ppk2/<SERIAL>.sock   Unix domain socket — IPC endpoint
~/.local/state/ppk2/<SERIAL>.pid    daemon process ID
~/.local/state/ppk2/<SERIAL>.log    stdout/stderr from the daemon child
```

`find_daemon()` cleans up stale `.sock`/`.pid` pairs whose owning process is
gone, so a crashed daemon doesn't block a fresh start.

## Advanced topics

For raw-sample streaming, GIL-aware capture loops, and macOS socket buffer
tuning, see [`daemon_streaming.md`](daemon_streaming.md).
