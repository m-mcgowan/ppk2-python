"""Client for communicating with a PPK2 daemon over Unix socket.

Provides the same public interface as ``PPK2Device`` so that CLI commands
work transparently whether talking to a daemon or directly to hardware.
"""

import json
import socket
from pathlib import Path

from .daemon import find_daemon, list_daemons, state_dir
from .transport import resolve_device
from .types import MeasurementResult, Sample

_BUF_SIZE = 65536
_RECV_TIMEOUT = 30.0


class DaemonClient:
    """Client that talks to a PPK2 daemon via Unix socket.

    Implements the same methods as ``PPK2Device`` so CLI code doesn't
    need to know which backend is in use.
    """

    def __init__(self, socket_path: Path, serial_number: str):
        self._socket_path = socket_path
        self._serial_number = serial_number
        self._vdd_mv = 3700
        self._metadata: dict | None = None
        # Persistent connection for streaming
        self._stream_sock: socket.socket | None = None

    def __enter__(self) -> "DaemonClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _request(self, req: dict, timeout: float = _RECV_TIMEOUT) -> dict:
        """Send a JSON request to the daemon and return the response."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(str(self._socket_path))
            sock.sendall(json.dumps(req).encode() + b"\n")
            data = b""
            while b"\n" not in data:
                chunk = sock.recv(_BUF_SIZE)
                if not chunk:
                    break
                data += chunk
            resp = json.loads(data.decode("utf-8"))
            # Update local state cache
            if "state" in resp:
                self._vdd_mv = resp["state"].get("vdd_mv", self._vdd_mv)
                self._metadata = resp.get("state")
            return resp
        finally:
            sock.close()

    def _check_ok(self, resp: dict) -> None:
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "Unknown daemon error"))

    # --- Setup commands ---

    def use_source_meter(self) -> None:
        resp = self._request({"cmd": "mode", "source": True})
        self._check_ok(resp)

    def use_ampere_meter(self) -> None:
        resp = self._request({"cmd": "mode", "source": False})
        self._check_ok(resp)

    def set_source_voltage(self, vdd_mv: int) -> None:
        resp = self._request({"cmd": "voltage", "mv": vdd_mv})
        self._check_ok(resp)
        self._vdd_mv = vdd_mv

    def toggle_dut_power(self, on: bool) -> None:
        resp = self._request({"cmd": "power", "on": on})
        self._check_ok(resp)

    # --- Measurement ---

    def measure(
        self, duration_s: float, spike_filter: bool = True
    ) -> MeasurementResult:
        """Measure for a duration via daemon. Returns stats."""
        resp = self._request(
            {
                "cmd": "measure",
                "duration_s": duration_s,
                "spike_filter": spike_filter,
            },
            timeout=duration_s + 10.0,
        )
        self._check_ok(resp)
        stats = resp["stats"]
        # Build a MeasurementResult with no raw samples (stats only)
        return MeasurementResult(
            samples=[],
            duration_s=stats.get("duration_s", duration_s),
            sample_count=stats.get("sample_count", 0),
            lost_samples=stats.get("lost", 0),
            _mean_ua=stats.get("mean_ua"),
            _min_ua=stats.get("min_ua"),
            _max_ua=stats.get("max_ua"),
            _p99_ua=stats.get("p99_ua"),
        )

    def start_measuring(self, raw: bool = False) -> None:
        """Start streaming measurement data from daemon.

        After calling this, use ``read_available()`` (raw mode) or
        ``read_samples()`` (JSON mode) to get data.
        """
        if self._stream_sock is not None:
            raise RuntimeError("Already streaming")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(_RECV_TIMEOUT)
        sock.connect(str(self._socket_path))
        sock.sendall(json.dumps({
            "cmd": "measure_start", "raw": raw
        }).encode() + b"\n")
        # Read ack — only parse the first line.  In raw mode the daemon
        # may already be forwarding binary sample bytes after the ack,
        # so we must not assume the entire buffer is valid UTF-8.
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(_BUF_SIZE)
            if not chunk:
                break
            data += chunk
        first_line, _, _leftover = data.partition(b"\n")
        resp = json.loads(first_line.decode("utf-8"))
        if not resp.get("ok"):
            sock.close()
            raise RuntimeError(resp.get("error", "Failed to start streaming"))
        self._stream_sock = sock

    def stop_measuring(self) -> dict | None:
        """Stop streaming. Returns final stats if available."""
        if self._stream_sock is None:
            return None
        try:
            self._stream_sock.sendall(
                json.dumps({"cmd": "measure_stop"}).encode() + b"\n"
            )
            data = b""
            self._stream_sock.settimeout(5.0)
            while b"\n" not in data:
                chunk = self._stream_sock.recv(_BUF_SIZE)
                if not chunk:
                    break
                data += chunk
            if data:
                return json.loads(data.decode("utf-8"))
        except Exception:
            pass
        finally:
            self._stream_sock.close()
            self._stream_sock = None
        return None

    def read_available(self) -> bytes:
        """Read raw bytes from the streaming connection (raw mode)."""
        if self._stream_sock is None:
            return b""
        try:
            self._stream_sock.setblocking(False)
            data = self._stream_sock.recv(_BUF_SIZE)
            return data
        except BlockingIOError:
            return b""
        finally:
            if self._stream_sock:
                self._stream_sock.setblocking(True)

    def read_samples(self) -> list[dict]:
        """Read decoded JSON sample dicts from the streaming connection."""
        if self._stream_sock is None:
            return []
        try:
            self._stream_sock.setblocking(False)
            data = self._stream_sock.recv(_BUF_SIZE)
        except BlockingIOError:
            return []
        finally:
            if self._stream_sock:
                self._stream_sock.setblocking(True)

        samples = []
        for line in data.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                samples.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return samples

    # --- State ---

    @property
    def metadata(self) -> dict | None:
        if self._metadata is None:
            resp = self._request({"cmd": "status"})
            if resp.get("ok"):
                self._metadata = resp.get("state")
        return self._metadata

    @property
    def vdd_mv(self) -> int:
        return self._vdd_mv

    def status(self) -> dict:
        """Get full daemon status."""
        resp = self._request({"cmd": "status"})
        self._check_ok(resp)
        return resp["state"]

    def close(self, reset: bool = False) -> None:
        """Close client connection.

        If *reset* is True, sends shutdown to the daemon (which will
        close the serial port and drop DUT power).
        """
        if self._stream_sock is not None:
            try:
                self._stream_sock.close()
            except Exception:
                pass
            self._stream_sock = None

        if reset:
            try:
                self._request({"cmd": "shutdown"}, timeout=5.0)
            except Exception:
                pass

    def shutdown(self) -> None:
        """Shut down the daemon."""
        try:
            self._request({"cmd": "shutdown"}, timeout=5.0)
        except Exception:
            pass


def connect_to_daemon(
    serial: str | None = None, port: str | None = None
) -> DaemonClient:
    """Connect to a running daemon.

    Raises:
        ConnectionError: No daemon running for the specified device.
    """
    result = find_daemon(serial=serial, port=port)
    if result is None:
        raise ConnectionError("No PPK2 daemon running")
    sn, sock_path = result
    return DaemonClient(sock_path, sn)
