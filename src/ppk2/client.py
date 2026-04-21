"""Client for communicating with a PPK2 daemon over Unix socket.

Provides the same public interface as ``PPK2Device`` so that CLI commands
work transparently whether talking to a daemon or directly to hardware.
"""

import json
import socket
from pathlib import Path

from .conversion import SpikeFilter, adc_to_microamps
from .daemon import find_daemon, list_daemons, state_dir
from .parser import SampleParser
from .transport import resolve_device
from .types import MeasurementResult, Modifiers, Sample

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

    def start_measuring(self) -> None:
        """Start streaming measurement data from daemon.

        Uses raw mode — daemon forwards PPK2 serial bytes, client parses
        locally with calibration data received in the ack. This matches
        PPK2Device performance (100kHz sample rate) unlike JSON mode.
        """
        if self._stream_sock is not None:
            raise RuntimeError("Already streaming")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(_RECV_TIMEOUT)
        sock.connect(str(self._socket_path))
        sock.sendall(json.dumps({
            "cmd": "measure_start", "raw": True
        }).encode() + b"\n")
        # Read ack — only parse the first line. In raw mode the daemon
        # may already be forwarding binary sample bytes after the ack,
        # so we must not assume the entire buffer is valid UTF-8.
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(_BUF_SIZE)
            if not chunk:
                break
            data += chunk
        first_line, _, leftover = data.partition(b"\n")
        resp = json.loads(first_line.decode("utf-8"))
        if not resp.get("ok"):
            sock.close()
            raise RuntimeError(resp.get("error", "Failed to start streaming"))

        # Set up local parsing with calibration from daemon
        self._parser = SampleParser()
        self._spike_filter = SpikeFilter()
        self._modifiers = Modifiers()
        mods = resp.get("modifiers")
        if mods:
            for key in ("r", "gs", "gi", "o", "s", "i", "ug"):
                if key in mods:
                    setattr(self._modifiers, key, list(mods[key]))
        self._stream_vdd_mv = resp.get("vdd_mv", self._vdd_mv)

        # Enlarge socket receive buffer — at 100kHz the daemon produces
        # ~400KB/s of raw sample data. A large buffer prevents overflow
        # between read_samples() calls.
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)  # 1 MB
        except OSError:
            pass

        self._stream_sock = sock
        # Feed any leftover bytes that arrived after the ack line
        self._leftover = leftover

    def stop_measuring(self) -> None:
        """Stop streaming measurement."""
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
        """Read raw bytes from the streaming connection (raw mode).

        Drains all available data from the socket buffer to prevent
        overflow at high sample rates.
        """
        if self._stream_sock is None:
            return b""
        chunks: list[bytes] = []
        try:
            self._stream_sock.setblocking(False)
            while True:
                chunk = self._stream_sock.recv(_BUF_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
        except BlockingIOError:
            pass
        finally:
            if self._stream_sock:
                self._stream_sock.setblocking(True)
        return b"".join(chunks)

    def read_samples(self, spike_filter: bool = True) -> list[Sample]:
        """Read and process available samples from the daemon stream.

        Parses raw PPK2 bytes locally using calibration data from the
        daemon, producing Sample objects identical to PPK2Device output.

        Args:
            spike_filter: Apply spike filter for range-switching smoothing.

        Returns:
            List of processed Sample objects (may be empty).
        """
        if self._stream_sock is None:
            return []

        # Collect any leftover from start_measuring ack
        chunks: list[bytes] = []
        leftover = getattr(self, '_leftover', b'')
        if leftover:
            chunks.append(leftover)
            self._leftover = b''

        try:
            self._stream_sock.setblocking(False)
            while True:
                chunk = self._stream_sock.recv(_BUF_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
        except BlockingIOError:
            pass
        finally:
            if self._stream_sock:
                self._stream_sock.setblocking(True)

        if not chunks:
            return []
        raw = b"".join(chunks)

        samples: list[Sample] = []
        parsed = self._parser.feed(raw)
        for frame in parsed:
            if frame is None:
                continue
            adc_raw, range_idx, counter, logic = frame
            current = adc_to_microamps(
                adc_raw, range_idx, self._modifiers, self._stream_vdd_mv
            )
            if spike_filter:
                current = self._spike_filter.process(current, range_idx)
            samples.append(Sample(
                current_ua=current,
                range=range_idx,
                logic=logic,
                counter=counter,
            ))
        return samples

    def parse_raw(
        self, raw: bytes, spike_filter: bool = True
    ) -> list[Sample]:
        """Parse raw PPK2 bytes into Sample objects.

        Use with read_available() for deferred parsing: collect raw bytes
        during measurement, parse in bulk afterwards. This avoids per-sample
        Python overhead during time-critical collection.

        A fresh parser is used each call (no cross-call state). Data loss
        detection works within the provided buffer.

        Args:
            raw: Raw PPK2 measurement bytes (4 bytes per frame).
            spike_filter: Apply spike filter for range-switching smoothing.

        Returns:
            List of Sample objects.
        """
        parser = SampleParser()
        sf = SpikeFilter() if spike_filter else None
        mods = self._modifiers
        vdd = self._stream_vdd_mv

        samples: list[Sample] = []
        for frame in parser.feed(raw):
            if frame is None:
                continue
            adc_raw, range_idx, counter, logic = frame
            if not 0 <= range_idx <= 4:
                continue  # invalid range (stream corruption / out-of-sync)
            current = adc_to_microamps(adc_raw, range_idx, mods, vdd)
            if sf:
                current = sf.process(current, range_idx)
            samples.append(Sample(
                current_ua=current,
                range=range_idx,
                logic=logic,
                counter=counter,
            ))
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
