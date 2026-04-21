# TODO: PPK2 Daemon CLI & Service Management

## Problem

On macOS, closing the serial port unconditionally drops DTR, which turns off
PPK2 DUT power. The daemon (`src/ppk2/daemon.py`) solves this by holding the
port open and accepting commands via Unix socket. But currently:

- No CLI integration — must start via Python API (`start_daemon()`)
- No service management — dies on reboot, must restart manually
- No `ppk2 daemon` subcommands at all

## Goal

Add `ppk2 daemon` CLI subcommands and platform-aware service install so the
daemon is easy to start, stop, and make persistent across reboots.

**Guiding principle:** The daemon is an implementation detail — like Docker's
daemon is invisible behind `docker run`. The user types `ppk2 power on` and it
just works. If a daemon is needed to hold the port open, it starts
automatically. The `ppk2 daemon` subcommands exist for service management
(install/uninstall) and debugging, not for normal use.

## Current Architecture

### daemon.py (src/ppk2/daemon.py)

**`DaemonServer`** — the core event loop (lines 89-458):
- Opens serial port, reads PPK2 metadata, configures VDD
- Binds Unix socket at `~/.local/state/ppk2/<SERIAL>.sock`
- Selector-based non-blocking I/O for socket + serial
- Handles SIGTERM/SIGINT gracefully (sets `_running = False`)
- Cleanup removes `.sock` and `.pid` files

**`DeviceState`** dataclass (line 66):
- Tracks serial_number, port, mode, vdd_mv, dut_power, measuring, uptime

**`start_daemon(serial, port)`** (line 531):
- Resolves device via `resolve_device()`
- Checks `find_daemon()` — raises RuntimeError if already running
- `os.fork()` — parent waits for socket, child redirects stdio to log and runs server
- Returns `(serial_number, daemon_pid)` to parent

**`find_daemon(serial, port)`** (line 461):
- Searches `~/.local/state/ppk2/*.sock` files
- Matches by serial (direct), port (resolved to serial), or any (first found)
- Verifies PID is alive via `os.kill(pid, 0)`
- Cleans up stale state files

**`list_daemons()`** (line 503):
- Returns all live `(serial, socket_path)` tuples

**State directory:** `~/.local/state/ppk2/`
- `<SERIAL>.sock` — Unix domain socket for IPC
- `<SERIAL>.pid` — daemon process ID
- `<SERIAL>.log` — stdout/stderr log

### client.py (src/ppk2/client.py)

**`DaemonClient`** (line 18):
- Connects to daemon's Unix socket
- JSON-over-socket protocol: `{"cmd": "power", "on": true}\n`
- Commands: power, voltage, mode, measure_start/stop, measure, status, shutdown

**`connect_to_daemon(serial, port)`** (line 252):
- Calls `find_daemon()`, creates `DaemonClient` from socket path
- Auto-discovers if only one daemon running

### cli.py (src/ppk2/cli.py)

Current subcommands: list, power, mode, voltage, measure, open, report, info,
validate, analyze, generate, merge. **No daemon subcommands.**

Device commands use `_open_device()` (line 20) which opens serial directly via
`PPK2Device.open()` — no daemon awareness.

### Reference Implementation: usb-device hub_agent.py

The `usb-device` project has an identical pattern at
`~/e/usb-device/hub_agent.py:455-512`:

```python
LAUNCHD_LABEL = "com.usb-devices.hub-agent"
LAUNCHD_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")
LAUNCHD_LOG = os.path.expanduser("~/Library/Logs/hub-agent.log")

def install_launchd():
    import plistlib
    script_dir = os.path.dirname(os.path.abspath(__file__))
    python_path = os.path.join(script_dir, ".venv", "bin", "python3")
    if not os.path.isfile(python_path):
        python_path = sys.executable

    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [python_path, "-u", ...],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": LAUNCHD_LOG,
        "StandardErrorPath": LAUNCHD_LOG,
    }
    os.makedirs(os.path.dirname(LAUNCHD_PLIST), exist_ok=True)
    with open(LAUNCHD_PLIST, "wb") as f:
        plistlib.dump(plist, f)

    os.system(f"launchctl bootout gui/$(id -u) {LAUNCHD_PLIST} 2>/dev/null")
    os.system(f"launchctl bootstrap gui/$(id -u) {LAUNCHD_PLIST}")

def uninstall_launchd():
    os.system(f"launchctl bootout gui/$(id -u) {LAUNCHD_PLIST} 2>/dev/null")
    os.remove(LAUNCHD_PLIST)
```

The bash wrapper dispatches: `usb-device hub watch|install|uninstall|status|log`

## Design

### CLI Subcommands

```
ppk2 daemon run       [--serial SN] [--port PORT]          # Foreground (service managers call this)
ppk2 daemon start     [--serial SN] [--port PORT] [--all]  # Smart start (service or fork)
ppk2 daemon stop      [--serial SN] [--port PORT] [--all]  # Smart stop
ppk2 daemon status    [--serial SN] [--port PORT] [--json]  # Show all daemons
ppk2 daemon install   [--serial SN] [--port PORT] [--all]  # Install as OS service
ppk2 daemon uninstall [--serial SN] [--port PORT] [--all]  # Remove OS service
ppk2 daemon log       [--serial SN] [--port PORT]          # Tail daemon log
ppk2 daemon release   [--serial SN] [--port PORT]          # Release serial port (keep daemon alive)
ppk2 daemon acquire   [--serial SN] [--port PORT]          # Reacquire serial port, restore state
```

### Port Release/Acquire

Other tools (nRF Connect PPK app, direct ppk2 CLI) need exclusive serial port
access. The daemon can temporarily release the port without shutting down:

- **`release`**: Closes `SerialTransport`, sets `state.released = True`. Daemon
  stays alive (socket still accepts commands). DUT power drops (unavoidable —
  DTR drop on close). Commands that need hardware return
  `{"error": "device released"}` except `acquire`, `status`, `shutdown`.
- **`acquire`**: Reopens serial port via `_connect_device()`, restores saved
  state (mode, vdd, dut_power). Clears `released` flag.

`DeviceState` gains a `released: bool = False` field. The `_dispatch()` method
checks `self._state.released` and rejects hardware commands early.

IPC commands to add to `DaemonServer._dispatch()`:
- `{"cmd": "release"}` → close transport, set released
- `{"cmd": "acquire"}` → reopen transport, restore state

### Device Selection Logic (shared across subcommands)

- `--serial` or `--port` → specific device
- `--all` → all connected PPK2s (via `list_ppk2_devices()`)
- No args → auto-detect if exactly one PPK2; error listing devices if multiple

### `start` Behavior

1. Check `find_daemon(serial)` — error if already running
2. If service installed → `svc.start(serial)` (launchctl bootstrap)
3. Else → `start_daemon(serial)` (fork)

### `stop` Behavior

1. If service-managed → `svc.stop(serial)` (launchctl bootout — prevents KeepAlive restart)
2. Else → `DaemonClient.shutdown()` via socket
3. `--all` iterates `list_daemons()` + `svc.list_installed()`

### `status` Output

```
C9F6358A  /dev/cu.usbmodemC9F6358AC3072  power=ON  vdd=3700mV  mode=source  uptime=2h13m  managed=launchd
AB123456  /dev/cu.usbmodem1234            power=OFF vdd=3300mV  mode=source  uptime=5m     managed=fork
```

## Files to Create

### `src/ppk2/service.py` — Platform-aware service management

```python
"""Platform-aware service management for PPK2 daemons."""

import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path


class ServiceManager(ABC):
    """Abstract interface for OS service management.

    One service per PPK2 device, keyed by serial number.
    """

    @abstractmethod
    def install(self, serial: str) -> None:
        """Install a persistent service for the given PPK2 device."""

    @abstractmethod
    def uninstall(self, serial: str) -> None:
        """Remove the persistent service."""

    @abstractmethod
    def start(self, serial: str) -> None:
        """Start the installed service."""

    @abstractmethod
    def stop(self, serial: str) -> None:
        """Stop the installed service."""

    @abstractmethod
    def is_installed(self, serial: str) -> bool:
        """Check if a service is installed for this device."""

    @abstractmethod
    def is_running(self, serial: str) -> bool:
        """Check if the installed service is currently running."""

    @abstractmethod
    def list_installed(self) -> list[str]:
        """Return serial numbers of all installed services."""

    @abstractmethod
    def log_path(self, serial: str) -> Path:
        """Return the log file path for this device's service."""


class LaunchdManager(ServiceManager):
    """macOS launchd LaunchAgent implementation.

    Naming:
        Label:  com.ppk2.daemon.<SERIAL>
        Plist:  ~/Library/LaunchAgents/com.ppk2.daemon.<SERIAL>.plist
        Log:    ~/.local/state/ppk2/<SERIAL>.log

    ProgramArguments: [python, -u, -m, ppk2, daemon, run, --serial, <SN>]

    Plist properties:
        KeepAlive: True          # auto-restart on crash
        RunAtLoad: True          # start on login
        ThrottleInterval: 10     # min 10s between restarts
    """

    LABEL_PREFIX = "com.ppk2.daemon"
    PLIST_DIR = Path.home() / "Library" / "LaunchAgents"

    def _label(self, serial: str) -> str:
        return f"{self.LABEL_PREFIX}.{serial}"

    def _plist_path(self, serial: str) -> Path:
        return self.PLIST_DIR / f"{self._label(serial)}.plist"

    def _resolve_python(self) -> str:
        """Find the best python to use.

        Prefer the ppk2-python project's venv, fall back to sys.executable.
        """
        # Walk up from this file to find the project root's .venv
        project_root = Path(__file__).resolve().parent.parent.parent
        venv_python = project_root / ".venv" / "bin" / "python3"
        if venv_python.is_file():
            return str(venv_python)
        return sys.executable

    def install(self, serial: str) -> None:
        import plistlib
        from .daemon import _log_path

        python_path = self._resolve_python()
        plist = {
            "Label": self._label(serial),
            "ProgramArguments": [
                python_path, "-u",
                "-m", "ppk2", "daemon", "run",
                "--serial", serial,
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "StandardOutPath": str(_log_path(serial)),
            "StandardErrorPath": str(_log_path(serial)),
        }
        self.PLIST_DIR.mkdir(parents=True, exist_ok=True)
        with open(self._plist_path(serial), "wb") as f:
            plistlib.dump(plist, f)

        label = self._label(serial)
        plist_path = self._plist_path(serial)
        os.system(f"launchctl bootout gui/$(id -u) {plist_path} 2>/dev/null")
        rc = os.system(f"launchctl bootstrap gui/$(id -u) {plist_path}")
        if rc == 0:
            print(f"[ok] Installed and started {label}")
            print(f"     Plist: {plist_path}")
            print(f"     Log:   {_log_path(serial)}")
        else:
            print(f"[error] launchctl bootstrap failed (rc={rc})")

    def uninstall(self, serial: str) -> None:
        plist_path = self._plist_path(serial)
        if not plist_path.exists():
            print(f"Not installed ({plist_path} does not exist).")
            return
        os.system(f"launchctl bootout gui/$(id -u) {plist_path} 2>/dev/null")
        plist_path.unlink()
        print(f"[ok] Uninstalled {self._label(serial)}")

    def start(self, serial: str) -> None:
        plist_path = self._plist_path(serial)
        os.system(f"launchctl bootstrap gui/$(id -u) {plist_path}")

    def stop(self, serial: str) -> None:
        plist_path = self._plist_path(serial)
        os.system(f"launchctl bootout gui/$(id -u) {plist_path} 2>/dev/null")

    def is_installed(self, serial: str) -> bool:
        return self._plist_path(serial).exists()

    def is_running(self, serial: str) -> bool:
        label = self._label(serial)
        rc = os.system(f"launchctl print gui/$(id -u)/{label} >/dev/null 2>&1")
        return rc == 0

    def list_installed(self) -> list[str]:
        serials = []
        prefix = f"{self.LABEL_PREFIX}."
        for p in self.PLIST_DIR.glob(f"{prefix}*.plist"):
            sn = p.stem.removeprefix(prefix)
            serials.append(sn)
        return serials

    def log_path(self, serial: str) -> Path:
        from .daemon import _log_path
        return _log_path(serial)


class SystemdManager(ServiceManager):
    """Linux systemd user unit — stub for future implementation."""

    def install(self, serial): raise NotImplementedError("systemd support not yet implemented")
    def uninstall(self, serial): raise NotImplementedError("systemd support not yet implemented")
    def start(self, serial): raise NotImplementedError("systemd support not yet implemented")
    def stop(self, serial): raise NotImplementedError("systemd support not yet implemented")
    def is_installed(self, serial): return False
    def is_running(self, serial): return False
    def list_installed(self): return []
    def log_path(self, serial): raise NotImplementedError("systemd support not yet implemented")


def get_service_manager() -> ServiceManager | None:
    """Return the appropriate ServiceManager for the current platform."""
    if sys.platform == "darwin":
        return LaunchdManager()
    elif sys.platform == "linux":
        return SystemdManager()
    return None
```

## Files to Modify

### `src/ppk2/daemon.py` — Extract `run_daemon()` from `start_daemon()`

Add a new `run_daemon()` function that runs the daemon in the foreground (no
fork). This is what `ppk2 daemon run` invokes and what service managers call.

```python
def run_daemon(
    serial: str | None = None, port: str | None = None
) -> None:
    """Run a daemon in the foreground (blocking).

    Entry point for:
    - ``ppk2 daemon run`` (service manager mode)
    - Child process of ``start_daemon()`` (fork mode)
    """
    device = resolve_device(serial=serial, port=port)

    existing = find_daemon(serial=device.serial_number)
    if existing:
        raise RuntimeError(
            f"Daemon already running for {device.serial_number}"
        )

    # Write PID file
    _pid_path(device.serial_number).write_text(str(os.getpid()))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    try:
        server = DaemonServer(device)
        server.start()
    except Exception:
        logging.exception("Daemon failed")
        raise
```

Then refactor `start_daemon()` to use it in the child process:

```python
def start_daemon(serial=None, port=None):
    device = resolve_device(serial=serial, port=port)
    existing = find_daemon(serial=device.serial_number)
    if existing:
        raise RuntimeError(f"Daemon already running for {device.serial_number}")

    pid = os.fork()
    if pid > 0:
        # Parent: wait for socket
        sock_path = _sock_path(device.serial_number)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if sock_path.exists():
                return (device.serial_number, pid)
            time.sleep(0.1)
        raise RuntimeError("Daemon failed to start")

    # Child: redirect stdio, then run_daemon
    try:
        os.setsid()
        log_path = _log_path(device.serial_number)
        log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        os.close(log_fd)
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.close(devnull)

        run_daemon(serial=device.serial_number)
    except Exception:
        logging.exception("Daemon failed")
    finally:
        os._exit(0)
```

### `src/ppk2/cli.py` — Add daemon subcommand group

Add to `main()` after existing subcommands:

```python
# ppk2 daemon ...
p_daemon = sub.add_parser("daemon", help="Manage PPK2 daemon processes")
daemon_sub = p_daemon.add_subparsers(dest="daemon_command")

p_run = daemon_sub.add_parser("run", help="Run daemon in foreground")
p_run.add_argument("--serial", help="PPK2 serial number")
p_run.add_argument("--port", help="Serial port")

p_start = daemon_sub.add_parser("start", help="Start daemon (service or fork)")
p_start.add_argument("--serial", help="PPK2 serial number")
p_start.add_argument("--port", help="Serial port")
p_start.add_argument("--all", action="store_true", help="All connected PPK2s")

p_stop = daemon_sub.add_parser("stop", help="Stop a running daemon")
p_stop.add_argument("--serial", help="PPK2 serial number")
p_stop.add_argument("--port", help="Serial port")
p_stop.add_argument("--all", action="store_true", help="Stop all daemons")

p_status = daemon_sub.add_parser("status", help="Show daemon status")
p_status.add_argument("--serial", help="PPK2 serial number")
p_status.add_argument("--port", help="Serial port")
p_status.add_argument("--json", action="store_true", help="JSON output")

p_install = daemon_sub.add_parser("install", help="Install as system service")
p_install.add_argument("--serial", help="PPK2 serial number")
p_install.add_argument("--port", help="Serial port")
p_install.add_argument("--all", action="store_true", help="All connected PPK2s")

p_uninstall = daemon_sub.add_parser("uninstall", help="Remove system service")
p_uninstall.add_argument("--serial", help="PPK2 serial number")
p_uninstall.add_argument("--port", help="Serial port")
p_uninstall.add_argument("--all", action="store_true", help="All installed services")

p_log = daemon_sub.add_parser("log", help="Tail daemon log")
p_log.add_argument("--serial", help="PPK2 serial number")
p_log.add_argument("--port", help="Serial port")
```

Add to handlers dict:
```python
"daemon": cmd_daemon,
```

The `cmd_daemon` dispatcher:
```python
def cmd_daemon(args):
    daemon_handlers = {
        "run": cmd_daemon_run,
        "start": cmd_daemon_start,
        "stop": cmd_daemon_stop,
        "status": cmd_daemon_status,
        "install": cmd_daemon_install,
        "uninstall": cmd_daemon_uninstall,
        "log": cmd_daemon_log,
    }
    if not args.daemon_command:
        # Default to status
        args.daemon_command = "status"
    return daemon_handlers[args.daemon_command](args)
```

### Handler Implementations (in cli.py)

**`cmd_daemon_run`** — foreground, blocking:
```python
def cmd_daemon_run(args):
    from .daemon import run_daemon
    run_daemon(serial=args.serial, port=args.port)
    return 0
```

**`cmd_daemon_start`** — smart start:
```python
def cmd_daemon_start(args):
    from .daemon import find_daemon, start_daemon
    from .service import get_service_manager
    from .transport import list_ppk2_devices, resolve_device

    svc = get_service_manager()
    devices = _resolve_devices(args)  # handles --all, --serial, --port, auto-detect

    for device in devices:
        sn = device.serial_number
        if find_daemon(serial=sn):
            print(f"{sn}: already running")
            continue
        if svc and svc.is_installed(sn):
            svc.start(sn)
            print(f"{sn}: started (service)")
        else:
            sn, pid = start_daemon(serial=sn)
            print(f"{sn}: started (fork, pid={pid})")
    return 0
```

**`cmd_daemon_stop`** — smart stop:
```python
def cmd_daemon_stop(args):
    from .client import DaemonClient
    from .daemon import find_daemon, list_daemons
    from .service import get_service_manager

    svc = get_service_manager()

    if args.all:
        targets = list_daemons()
    else:
        result = find_daemon(serial=getattr(args, 'serial', None),
                             port=getattr(args, 'port', None))
        targets = [result] if result else []

    if not targets:
        print("No daemons running.")
        return 1

    for sn, sock_path in targets:
        if svc and svc.is_installed(sn):
            svc.stop(sn)
            print(f"{sn}: stopped (service)")
        else:
            client = DaemonClient(sock_path, sn)
            client.shutdown()
            print(f"{sn}: stopped")
    return 0
```

**`cmd_daemon_status`** — show all:
```python
def cmd_daemon_status(args):
    from .client import DaemonClient
    from .daemon import list_daemons
    from .service import get_service_manager

    svc = get_service_manager()
    daemons = list_daemons()

    if not daemons:
        # Check for installed-but-stopped services
        if svc:
            installed = svc.list_installed()
            if installed:
                for sn in installed:
                    print(f"{sn}  service=installed  running=no")
                return 0
        print("No daemons running.")
        return 1

    for sn, sock_path in daemons:
        managed = "service" if (svc and svc.is_installed(sn)) else "fork"
        try:
            client = DaemonClient(sock_path, sn)
            state = client.status()
            power = "ON" if state.get("dut_power") else "OFF"
            print(f"{sn}  {state.get('port','')}  "
                  f"power={power}  vdd={state.get('vdd_mv')}mV  "
                  f"mode={state.get('mode')}  "
                  f"uptime={_fmt_uptime(state.get('uptime_s',0))}  "
                  f"managed={managed}")
        except Exception:
            print(f"{sn}  (unreachable)  managed={managed}")
    return 0
```

**`cmd_daemon_log`** — tail log file:
```python
def cmd_daemon_log(args):
    from .daemon import _log_path
    from .transport import resolve_device
    device = resolve_device(serial=args.serial, port=args.port)
    log = _log_path(device.serial_number)
    if not log.exists():
        print(f"No log file: {log}")
        return 1
    os.execvp("tail", ["tail", "-f", str(log)])
```

### Shared helper: `_resolve_devices(args)`

```python
def _resolve_devices(args):
    """Resolve device list from --all, --serial, --port, or auto-detect."""
    from .transport import list_ppk2_devices, resolve_device

    if getattr(args, 'all', False):
        devices = list_ppk2_devices()
        if not devices:
            print("No PPK2 devices found.")
            sys.exit(1)
        return devices
    else:
        return [resolve_device(
            serial=getattr(args, 'serial', None),
            port=getattr(args, 'port', None),
        )]
```

## Tests to Add

### `tests/test_service.py`

- `test_launchd_plist_generation` — mock plistlib.dump, verify plist structure
- `test_launchd_label_format` — verify `com.ppk2.daemon.<SERIAL>` format
- `test_launchd_plist_path` — verify `~/Library/LaunchAgents/` location
- `test_launchd_is_installed` — mock path existence check
- `test_launchd_list_installed` — mock glob of plist dir
- `test_get_service_manager_darwin` — returns LaunchdManager on macOS
- `test_get_service_manager_linux` — returns SystemdManager stub on Linux
- `test_systemd_stub_raises` — verify NotImplementedError

## Implementation Order

1. Create `src/ppk2/service.py` — ABC + LaunchdManager + SystemdManager stub
2. Modify `src/ppk2/daemon.py` — extract `run_daemon()` from `start_daemon()`
3. Modify `src/ppk2/cli.py` — add `ppk2 daemon` subcommand group + handlers
4. Create `tests/test_service.py` — service manager tests
5. Manual test: `ppk2 daemon run` (foreground, Ctrl-C)
6. Manual test: `ppk2 daemon start` / `status` / `stop`
7. Manual test: `ppk2 daemon install` / `status` / `stop` / `start` / `uninstall`
8. Manual test: `ppk2 daemon install --all` with multiple PPK2s

## Future: Transparent Daemon Routing (not this PR)

The design target is that `ppk2 power on` checks for a running daemon first
and routes the command through it. If no daemon, opens serial directly.

The hook point is `_open_device()` in cli.py (line 20):

```python
def _open_device(port):
    # Try daemon first
    try:
        from .client import connect_to_daemon
        return connect_to_daemon(port=port)
    except ConnectionError:
        pass
    # Fall back to direct connection
    from .device import PPK2Device
    return PPK2Device.open(port)
```

This requires `DaemonClient` to implement the same context manager interface
as `PPK2Device` (it mostly does already — may need minor alignment).
