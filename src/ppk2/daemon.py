"""PPK2 daemon — holds serial port open for persistent DUT power.

On macOS, closing the serial port unconditionally drops DTR, causing the
PPK2 to turn off DUT power.  This daemon keeps the port open and accepts
commands over a Unix socket so that CLI invocations and Python scripts
can control the device without losing power state.

One daemon process per PPK2 device, keyed by serial number.

State directory: ``~/.local/state/ppk2/``
  - ``<SERIAL>.sock`` — Unix domain socket
  - ``<SERIAL>.pid``  — daemon PID
  - ``<SERIAL>.log``  — log output
"""

import json
import logging
import os
import selectors
import signal
import socket
import struct
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import commands
from .conversion import SpikeFilter, adc_to_microamps
from .parser import SampleParser, parse_metadata
from .transport import PPK2Port, SerialTransport, resolve_device
from .types import Modifiers

logger = logging.getLogger(__name__)

_BUF_SIZE = 65536


def state_dir() -> Path:
    """Return the daemon state directory, creating it if needed."""
    d = Path.home() / ".local" / "state" / "ppk2"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sock_path(serial: str) -> Path:
    return state_dir() / f"{serial}.sock"


def _pid_path(serial: str) -> Path:
    return state_dir() / f"{serial}.pid"


def _log_path(serial: str) -> Path:
    return state_dir() / f"{serial}.log"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@dataclass
class DeviceState:
    """Tracked state of the managed PPK2 device."""

    serial_number: str = ""
    port: str = ""
    mode: str = "source"
    vdd_mv: int = 3700
    dut_power: bool = False
    measuring: bool = False
    started_at: float = field(default_factory=time.monotonic)

    @property
    def uptime_s(self) -> float:
        return time.monotonic() - self.started_at

    def to_dict(self) -> dict:
        d = asdict(self)
        d["uptime_s"] = round(self.uptime_s, 1)
        del d["started_at"]
        return d


class DaemonServer:
    """Daemon that holds a PPK2 serial port open and serves commands.

    Uses ``selectors`` for non-blocking I/O on the Unix socket and the
    serial port (during measurement streaming).
    """

    def __init__(self, device: PPK2Port):
        self._device = device
        self._transport = SerialTransport(device.port)
        self._modifiers = Modifiers()
        self._spike_filter = SpikeFilter()
        self._parser = SampleParser()
        self._state = DeviceState(
            serial_number=device.serial_number,
            port=device.port,
        )
        self._sel = selectors.DefaultSelector()
        self._server_sock: socket.socket | None = None
        self._running = False
        # Active streaming client (only one at a time)
        self._stream_client: socket.socket | None = None
        self._stream_raw: bool = False

    def start(self) -> None:
        """Open device, bind socket, enter event loop."""
        self._connect_device()
        self._bind_socket()
        self._running = True

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        logger.info(
            "Daemon started: serial=%s port=%s vdd=%d",
            self._state.serial_number,
            self._state.port,
            self._state.vdd_mv,
        )

        try:
            self._event_loop()
        finally:
            self._cleanup()

    def _connect_device(self) -> None:
        """Open transport and read metadata."""
        self._transport.open()

        # Stop any in-progress measurement and drain stale data.
        self._transport.write(commands.average_stop())
        time.sleep(0.1)
        while self._transport.read_available():
            time.sleep(0.05)

        self._transport.write(commands.get_metadata())
        metadata = self._read_metadata()
        self._modifiers.update_from_metadata(metadata)

        vdd = int(metadata.get("vdd", 3700))
        self._state.vdd_mv = vdd
        self._transport.write(commands.regulator_set(vdd))

        mode = metadata.get("mode", 2)
        self._state.mode = "source" if int(mode) == 2 else "ampere"

        logger.info("Device connected: %s", metadata)

    def _read_metadata(self) -> dict:
        text = ""
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            chunk = self._transport.read(1024, timeout=1.0)
            if chunk:
                text += chunk.decode("ascii", errors="replace")
                if "END" in text:
                    return parse_metadata(text)
        raise TimeoutError("Timeout waiting for PPK2 metadata")

    def _bind_socket(self) -> None:
        sock_path = _sock_path(self._state.serial_number)
        # Remove stale socket
        if sock_path.exists():
            sock_path.unlink()

        self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_sock.setblocking(False)
        self._server_sock.bind(str(sock_path))
        self._server_sock.listen(4)
        self._sel.register(
            self._server_sock, selectors.EVENT_READ, self._accept
        )

    def _event_loop(self) -> None:
        while self._running:
            # If streaming, also poll serial and forward data
            if self._stream_client and self._state.measuring:
                self._forward_samples()

            events = self._sel.select(timeout=0.1)
            for key, _ in events:
                key.data(key.fileobj)

    def _accept(self, sock: socket.socket) -> None:
        conn, _ = sock.accept()
        conn.setblocking(False)
        self._sel.register(conn, selectors.EVENT_READ, self._on_client_data)

    def _on_client_data(self, conn: socket.socket) -> None:
        try:
            data = conn.recv(_BUF_SIZE)
        except ConnectionError:
            data = b""

        if not data:
            self._disconnect_client(conn)
            return

        try:
            request = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._send_response(conn, {"ok": False, "error": str(e)})
            return

        self._dispatch(conn, request)

    def _disconnect_client(self, conn: socket.socket) -> None:
        # If this was the streaming client, stop measuring
        if conn is self._stream_client:
            if self._state.measuring:
                self._transport.write(commands.average_stop())
                self._state.measuring = False
            self._stream_client = None
        try:
            self._sel.unregister(conn)
        except (KeyError, ValueError):
            pass
        conn.close()

    def _dispatch(self, conn: socket.socket, req: dict) -> None:
        cmd = req.get("cmd")
        try:
            if cmd == "power":
                on = bool(req.get("on", False))
                self._transport.write(commands.device_running_set(on))
                self._state.dut_power = on
                self._send_response(conn, {
                    "ok": True,
                    "msg": f"DUT power {'ON' if on else 'OFF'}",
                    "state": self._state.to_dict(),
                })

            elif cmd == "voltage":
                mv = int(req["mv"])
                self._transport.write(commands.regulator_set(mv))
                self._state.vdd_mv = mv
                self._send_response(conn, {
                    "ok": True,
                    "state": self._state.to_dict(),
                })

            elif cmd == "mode":
                source = bool(req.get("source", True))
                self._transport.write(
                    commands.set_power_mode(source_meter=source)
                )
                self._state.mode = "source" if source else "ampere"
                self._send_response(conn, {
                    "ok": True,
                    "state": self._state.to_dict(),
                })

            elif cmd == "measure_start":
                if self._stream_client is not None:
                    self._send_response(conn, {
                        "ok": False,
                        "error": "Another client is already streaming",
                    })
                    return
                raw = bool(req.get("raw", False))
                self._stream_client = conn
                self._stream_raw = raw
                self._spike_filter.reset()
                self._parser.reset()
                self._transport.write(commands.average_start())
                self._state.measuring = True
                self._send_response(conn, {"ok": True})

            elif cmd == "measure_stop":
                if conn is not self._stream_client:
                    self._send_response(conn, {
                        "ok": False, "error": "Not the streaming client"
                    })
                    return
                self._transport.write(commands.average_stop())
                self._state.measuring = False
                self._stream_client = None
                self._send_response(conn, {
                    "ok": True,
                    "state": self._state.to_dict(),
                })

            elif cmd == "measure":
                # Convenience: measure for a duration and return stats
                if self._stream_client is not None:
                    self._send_response(conn, {
                        "ok": False,
                        "error": "Another client is already streaming",
                    })
                    return
                duration = float(req.get("duration_s", 1.0))
                spike_filter = bool(req.get("spike_filter", True))
                stats = self._measure_duration(duration, spike_filter)
                self._send_response(conn, {"ok": True, "stats": stats})

            elif cmd == "status":
                self._send_response(conn, {
                    "ok": True,
                    "state": self._state.to_dict(),
                })

            elif cmd == "shutdown":
                self._send_response(conn, {"ok": True, "msg": "Shutting down"})
                self._running = False

            else:
                self._send_response(conn, {
                    "ok": False,
                    "error": f"Unknown command: {cmd}",
                })

        except Exception as e:
            logger.exception("Error handling command %s", cmd)
            try:
                self._send_response(conn, {"ok": False, "error": str(e)})
            except Exception:
                pass

    def _measure_duration(
        self, duration_s: float, spike_filter: bool
    ) -> dict:
        """Measure for a fixed duration, return stats dict."""
        self._spike_filter.reset()
        self._parser.reset()
        self._transport.write(commands.average_start())
        self._state.measuring = True
        time.sleep(0.05)

        samples_ua: list[float] = []
        lost = 0
        deadline = time.monotonic() + duration_s

        while time.monotonic() < deadline:
            raw = self._transport.read_available()
            if not raw:
                time.sleep(0.01)
                continue
            parsed = self._parser.feed(raw)
            for frame in parsed:
                if frame is None:
                    lost += 1
                    continue
                adc_raw, range_idx, counter, logic = frame
                current = adc_to_microamps(
                    adc_raw, range_idx, self._modifiers, self._state.vdd_mv
                )
                if spike_filter:
                    current = self._spike_filter.process(current, range_idx)
                samples_ua.append(current)

        self._transport.write(commands.average_stop())
        self._state.measuring = False

        n = len(samples_ua)
        if n == 0:
            return {
                "sample_count": 0, "lost": lost, "duration_s": duration_s,
                "mean_ua": 0, "min_ua": 0, "max_ua": 0,
            }

        sorted_ua = sorted(samples_ua)
        return {
            "sample_count": n,
            "lost": lost,
            "duration_s": duration_s,
            "mean_ua": round(sum(samples_ua) / n, 1),
            "min_ua": round(sorted_ua[0], 1),
            "max_ua": round(sorted_ua[-1], 1),
            "p99_ua": round(sorted_ua[min(int(n * 0.99), n - 1)], 1),
        }

    def _forward_samples(self) -> None:
        """Read serial data and forward to the streaming client."""
        raw = self._transport.read_available()
        if not raw:
            return

        conn = self._stream_client
        if conn is None:
            return

        try:
            # Temporarily set blocking for sendall — the client socket
            # is registered non-blocking for the selector, but sendall
            # requires blocking mode to send large buffers reliably.
            conn.setblocking(True)
            try:
                if self._stream_raw:
                    # Raw mode: forward bytes as-is
                    conn.sendall(raw)
                else:
                    # JSON mode: decode and send JSON lines
                    parsed = self._parser.feed(raw)
                    lines = []
                    for frame in parsed:
                        if frame is None:
                            continue
                        adc_raw, range_idx, counter, logic = frame
                        current = adc_to_microamps(
                            adc_raw, range_idx, self._modifiers,
                            self._state.vdd_mv,
                        )
                        current = self._spike_filter.process(current, range_idx)
                        lines.append(json.dumps({
                            "ua": round(current, 1),
                            "range": range_idx,
                            "logic": logic,
                        }))
                    if lines:
                        conn.sendall(("\n".join(lines) + "\n").encode())
            finally:
                conn.setblocking(False)
        except (BrokenPipeError, ConnectionError, OSError):
            logger.info("Streaming client disconnected")
            self._disconnect_client(conn)

    def _send_response(self, conn: socket.socket, resp: dict) -> None:
        data = json.dumps(resp).encode() + b"\n"
        conn.sendall(data)

    def _handle_signal(self, signum: int, frame) -> None:
        logger.info("Received signal %d, shutting down", signum)
        self._running = False

    def _cleanup(self) -> None:
        """Close device and remove state files."""
        if self._state.measuring:
            try:
                self._transport.write(commands.average_stop())
            except Exception:
                pass
        if self._transport.is_open:
            self._transport.close()

        sn = self._state.serial_number
        for path in (_sock_path(sn), _pid_path(sn)):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

        if self._server_sock:
            try:
                self._sel.unregister(self._server_sock)
            except (KeyError, ValueError):
                pass
            self._server_sock.close()

        self._sel.close()
        logger.info("Daemon stopped")


def find_daemon(
    serial: str | None = None, port: str | None = None
) -> tuple[str, Path] | None:
    """Check if a daemon is running for the given device.

    Returns ``(serial_number, socket_path)`` if found, else ``None``.
    """
    sd = state_dir()

    if serial:
        pid_file = _pid_path(serial)
        sock_file = _sock_path(serial)
        if sock_file.exists():
            if pid_file.exists():
                pid = int(pid_file.read_text().strip())
                if _pid_alive(pid):
                    return (serial, sock_file)
            # Stale socket — clean up
            _cleanup_stale(serial)
        return None

    if port:
        # Resolve port to serial number via device list
        try:
            device = resolve_device(port=port)
            return find_daemon(serial=device.serial_number)
        except ConnectionError:
            pass
        return None

    # No device specified — look for any running daemon
    for sock_file in sd.glob("*.sock"):
        sn = sock_file.stem
        pid_file = _pid_path(sn)
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            if _pid_alive(pid):
                return (sn, sock_file)
        _cleanup_stale(sn)
    return None


def list_daemons() -> list[tuple[str, Path]]:
    """List all running daemons. Returns list of (serial, socket_path)."""
    sd = state_dir()
    result = []
    for sock_file in sd.glob("*.sock"):
        sn = sock_file.stem
        pid_file = _pid_path(sn)
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
            except ValueError:
                _cleanup_stale(sn)
                continue
            if _pid_alive(pid):
                result.append((sn, sock_file))
                continue
        _cleanup_stale(sn)
    return result


def _cleanup_stale(serial: str) -> None:
    for path in (_sock_path(serial), _pid_path(serial)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def start_daemon(
    serial: str | None = None, port: str | None = None
) -> tuple[str, int]:
    """Start a daemon for the specified device.

    Returns ``(serial_number, daemon_pid)``.

    Raises:
        ConnectionError: Device not found or ambiguous.
        RuntimeError: Daemon already running, or failed to start.
    """
    device = resolve_device(serial=serial, port=port)

    # Check for existing daemon
    existing = find_daemon(serial=device.serial_number)
    if existing:
        raise RuntimeError(
            f"Daemon already running for {device.serial_number}"
        )

    # Fork
    pid = os.fork()
    if pid > 0:
        # Parent: wait for socket to appear
        sock_path = _sock_path(device.serial_number)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if sock_path.exists():
                return (device.serial_number, pid)
            time.sleep(0.1)
        raise RuntimeError("Daemon failed to start (timeout waiting for socket)")

    # Child: become daemon
    try:
        os.setsid()

        # Write PID file
        _pid_path(device.serial_number).write_text(str(os.getpid()))

        # Redirect stdout/stderr to log
        log_path = _log_path(device.serial_number)
        log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        os.close(log_fd)

        # Close stdin
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.close(devnull)

        # Set up logging to stderr (which is now the log file)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            stream=sys.stderr,
        )

        server = DaemonServer(device)
        server.start()
    except Exception:
        logging.exception("Daemon failed")
    finally:
        os._exit(0)
