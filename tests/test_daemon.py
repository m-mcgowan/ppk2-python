"""Tests for PPK2 daemon server, client, and IPC protocol.

Tests use a real Unix domain socket with MockTransport — no hardware needed.
"""

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ppk2.client import DaemonClient
from ppk2.daemon import (
    DaemonServer,
    DeviceState,
    _cleanup_stale,
    _pid_path,
    _sock_path,
    find_daemon,
    list_daemons,
    state_dir,
)
from ppk2.mock import MockTransport, make_metadata_response
from ppk2.transport import PPK2Port
from ppk2.types import MeasurementResult


# --- Helpers ---


class DaemonTestHarness:
    """Run a DaemonServer in a background thread with MockTransport.

    Patches state_dir to use a temp directory so tests don't interfere
    with each other or with real daemon state.
    """

    def __init__(self, serial: str = "TEST1234", vdd: int = 3700):
        self.serial = serial
        self.vdd = vdd
        self.tmpdir = tempfile.mkdtemp()
        self.device = PPK2Port(
            port="/dev/ttyTEST0",
            serial_number=serial,
            location="1-1",
        )
        self.mock = MockTransport(metadata=make_metadata_response(vdd=vdd))
        self.server: DaemonServer | None = None
        self.thread: threading.Thread | None = None
        self._state_dir_patcher = patch(
            "ppk2.daemon.state_dir", return_value=Path(self.tmpdir)
        )

    def start(self) -> Path:
        """Start daemon in background thread. Returns socket path."""
        self._state_dir_patcher.start()

        # Create server with mock transport
        self.server = DaemonServer(self.device)
        self.server._transport = self.mock
        # Skip real _connect_device — set up state manually
        self.server._connect_device = lambda: None  # type: ignore
        self.server._state = DeviceState(
            serial_number=self.serial,
            port=self.device.port,
            vdd_mv=self.vdd,
            mode="source",
        )
        self.mock.open()

        # Bind socket
        self.server._bind_socket()

        # Run event loop in thread
        self.server._running = True

        def _run():
            try:
                self.server._event_loop()
            finally:
                self.server._cleanup()

        self.thread = threading.Thread(target=_run, daemon=True)
        self.thread.start()

        # Wait for socket
        sock_path = Path(self.tmpdir) / f"{self.serial}.sock"
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if sock_path.exists():
                return sock_path
            time.sleep(0.02)
        raise RuntimeError("Daemon socket not created")

    def stop(self):
        if self.server:
            self.server._running = False
        if self.thread:
            self.thread.join(timeout=3.0)
        self._state_dir_patcher.stop()

    def client(self, sock_path: Path) -> DaemonClient:
        return DaemonClient(sock_path, self.serial)


@pytest.fixture
def harness():
    h = DaemonTestHarness()
    sock_path = h.start()
    yield h, sock_path
    h.stop()


def _send_recv(sock_path: Path, req: dict, timeout: float = 5.0) -> dict:
    """Send a JSON request to the daemon and return the response."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(sock_path))
    try:
        sock.sendall(json.dumps(req).encode() + b"\n")
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode("utf-8"))
    finally:
        sock.close()


# --- DeviceState tests ---


class TestDeviceState:
    def test_to_dict(self):
        s = DeviceState(
            serial_number="ABC12345",
            port="/dev/ttyACM0",
            mode="source",
            vdd_mv=3700,
            dut_power=True,
        )
        d = s.to_dict()
        assert d["serial_number"] == "ABC12345"
        assert d["mode"] == "source"
        assert d["vdd_mv"] == 3700
        assert d["dut_power"] is True
        assert "uptime_s" in d
        assert "started_at" not in d

    def test_uptime_increases(self):
        s = DeviceState()
        t1 = s.uptime_s
        time.sleep(0.05)
        t2 = s.uptime_s
        assert t2 > t1


# --- Protocol tests (raw socket) ---


class TestDaemonProtocol:
    def test_status_command(self, harness):
        h, sock_path = harness
        resp = _send_recv(sock_path, {"cmd": "status"})
        assert resp["ok"] is True
        assert resp["state"]["serial_number"] == "TEST1234"
        assert resp["state"]["vdd_mv"] == 3700

    def test_power_on(self, harness):
        h, sock_path = harness
        resp = _send_recv(sock_path, {"cmd": "power", "on": True})
        assert resp["ok"] is True
        assert resp["state"]["dut_power"] is True

    def test_power_off(self, harness):
        h, sock_path = harness
        _send_recv(sock_path, {"cmd": "power", "on": True})
        resp = _send_recv(sock_path, {"cmd": "power", "on": False})
        assert resp["ok"] is True
        assert resp["state"]["dut_power"] is False

    def test_voltage_set(self, harness):
        h, sock_path = harness
        resp = _send_recv(sock_path, {"cmd": "voltage", "mv": 4200})
        assert resp["ok"] is True
        assert resp["state"]["vdd_mv"] == 4200

    def test_mode_source(self, harness):
        h, sock_path = harness
        resp = _send_recv(sock_path, {"cmd": "mode", "source": True})
        assert resp["ok"] is True
        assert resp["state"]["mode"] == "source"

    def test_mode_ampere(self, harness):
        h, sock_path = harness
        resp = _send_recv(sock_path, {"cmd": "mode", "source": False})
        assert resp["ok"] is True
        assert resp["state"]["mode"] == "ampere"

    def test_unknown_command(self, harness):
        h, sock_path = harness
        resp = _send_recv(sock_path, {"cmd": "bogus"})
        assert resp["ok"] is False
        assert "Unknown" in resp["error"]

    def test_invalid_json(self, harness):
        h, sock_path = harness
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect(str(sock_path))
        try:
            sock.sendall(b"not json\n")
            data = b""
            while b"\n" not in data:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
            resp = json.loads(data.decode("utf-8"))
            assert resp["ok"] is False
        finally:
            sock.close()

    def test_shutdown_stops_daemon(self, harness):
        h, sock_path = harness
        resp = _send_recv(sock_path, {"cmd": "shutdown"})
        assert resp["ok"] is True
        # Wait for thread to exit
        h.thread.join(timeout=3.0)
        assert not h.thread.is_alive()


# --- DaemonClient tests ---


class TestDaemonClient:
    def test_status(self, harness):
        h, sock_path = harness
        client = h.client(sock_path)
        state = client.status()
        assert state["serial_number"] == "TEST1234"
        assert state["vdd_mv"] == 3700

    def test_power_toggle(self, harness):
        h, sock_path = harness
        client = h.client(sock_path)
        client.toggle_dut_power(True)
        state = client.status()
        assert state["dut_power"] is True

        client.toggle_dut_power(False)
        state = client.status()
        assert state["dut_power"] is False

    def test_set_voltage(self, harness):
        h, sock_path = harness
        client = h.client(sock_path)
        client.set_source_voltage(4200)
        assert client.vdd_mv == 4200
        state = client.status()
        assert state["vdd_mv"] == 4200

    def test_use_source_meter(self, harness):
        h, sock_path = harness
        client = h.client(sock_path)
        client.use_source_meter()
        state = client.status()
        assert state["mode"] == "source"

    def test_use_ampere_meter(self, harness):
        h, sock_path = harness
        client = h.client(sock_path)
        client.use_ampere_meter()
        state = client.status()
        assert state["mode"] == "ampere"

    def test_metadata(self, harness):
        h, sock_path = harness
        client = h.client(sock_path)
        m = client.metadata
        assert m is not None
        assert m["serial_number"] == "TEST1234"

    def test_context_manager(self, harness):
        h, sock_path = harness
        with DaemonClient(sock_path, "TEST1234") as client:
            state = client.status()
            assert state["serial_number"] == "TEST1234"

    def test_shutdown(self, harness):
        h, sock_path = harness
        client = h.client(sock_path)
        client.shutdown()
        h.thread.join(timeout=3.0)
        assert not h.thread.is_alive()

    def test_close_with_reset(self, harness):
        h, sock_path = harness
        client = h.client(sock_path)
        client.close(reset=True)
        # Daemon should be shutting down
        h.thread.join(timeout=3.0)
        assert not h.thread.is_alive()


# --- Measurement via daemon ---


class TestDaemonMeasurement:
    def test_measure_returns_stats(self, harness):
        h, sock_path = harness
        # The daemon will use MockTransport which generates samples when measuring
        resp = _send_recv(
            sock_path,
            {"cmd": "measure", "duration_s": 0.1},
            timeout=10.0,
        )
        assert resp["ok"] is True
        stats = resp["stats"]
        assert stats["sample_count"] > 0
        assert stats["mean_ua"] > 0
        assert stats["duration_s"] == 0.1

    def test_client_measure(self, harness):
        h, sock_path = harness
        client = h.client(sock_path)
        result = client.measure(duration_s=0.1)
        assert isinstance(result, MeasurementResult)
        assert result.sample_count > 0
        assert result.mean_ua > 0
        assert result.duration_s == 0.1

    def test_client_measure_precomputed_stats(self, harness):
        h, sock_path = harness
        client = h.client(sock_path)
        result = client.measure(duration_s=0.1)
        # Stats come from daemon (pre-computed), not from raw samples
        assert result.samples == []
        assert result.mean_ua > 0
        assert result.min_ua > 0
        assert result.max_ua > 0

    def test_measure_blocks_second_client(self, harness):
        h, sock_path = harness
        # Start streaming on one connection
        sock1 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock1.settimeout(5.0)
        sock1.connect(str(sock_path))
        sock1.sendall(json.dumps({"cmd": "measure_start"}).encode() + b"\n")
        data = b""
        while b"\n" not in data:
            chunk = sock1.recv(65536)
            if not chunk:
                break
            data += chunk
        # Parse only the first line (ack); rest may be streaming data
        first_line = data.decode().split("\n", 1)[0]
        resp1 = json.loads(first_line)
        assert resp1["ok"] is True

        try:
            # Second client should be rejected
            resp2 = _send_recv(sock_path, {"cmd": "measure_start"})
            assert resp2["ok"] is False
            assert "already streaming" in resp2["error"].lower()
        finally:
            sock1.close()


# --- Streaming tests ---


class TestDaemonStreaming:
    def test_raw_streaming(self, harness):
        h, sock_path = harness
        client = h.client(sock_path)
        client.start_measuring(raw=True)
        time.sleep(0.2)

        data = client.read_available()
        # Should get some raw bytes (4 bytes per sample)
        assert len(data) > 0
        assert len(data) % 4 == 0

        client.stop_measuring()

    def test_json_streaming(self, harness):
        h, sock_path = harness
        client = h.client(sock_path)
        client.start_measuring(raw=False)
        time.sleep(0.2)

        samples = client.read_samples()
        assert len(samples) > 0
        # Each sample should have ua, range, logic keys
        for s in samples[:5]:
            assert "ua" in s
            assert "range" in s
            assert "logic" in s

        client.stop_measuring()

    def test_start_measuring_twice_raises(self, harness):
        h, sock_path = harness
        client = h.client(sock_path)
        client.start_measuring(raw=True)
        with pytest.raises(RuntimeError, match="Already streaming"):
            client.start_measuring(raw=True)
        client.stop_measuring()


# --- State management tests ---


class TestStateDirManagement:
    def test_state_dir_creates_directory(self, tmp_path):
        with patch("ppk2.daemon.Path.home", return_value=tmp_path):
            d = state_dir()
            assert d.exists()
            assert d == tmp_path / ".local" / "state" / "ppk2"

    def test_find_daemon_returns_none_when_no_daemons(self, tmp_path):
        with patch("ppk2.daemon.state_dir", return_value=tmp_path):
            assert find_daemon(serial="NONEXIST") is None
            assert find_daemon() is None

    def test_find_daemon_cleans_stale_socket(self, tmp_path):
        with patch("ppk2.daemon.state_dir", return_value=tmp_path):
            # Create stale PID file pointing to non-existent process
            sn = "STALE123"
            (tmp_path / f"{sn}.sock").write_text("")
            (tmp_path / f"{sn}.pid").write_text("99999999")  # very unlikely PID

            result = find_daemon(serial=sn)
            assert result is None
            # Stale files should be cleaned
            assert not (tmp_path / f"{sn}.sock").exists()
            assert not (tmp_path / f"{sn}.pid").exists()

    def test_list_daemons_empty(self, tmp_path):
        with patch("ppk2.daemon.state_dir", return_value=tmp_path):
            assert list_daemons() == []

    def test_list_daemons_finds_live(self, tmp_path):
        with patch("ppk2.daemon.state_dir", return_value=tmp_path):
            sn = "LIVE1234"
            (tmp_path / f"{sn}.sock").write_text("")
            (tmp_path / f"{sn}.pid").write_text(str(os.getpid()))  # current process = alive

            daemons = list_daemons()
            assert len(daemons) == 1
            assert daemons[0][0] == sn

    def test_cleanup_stale(self, tmp_path):
        with patch("ppk2.daemon.state_dir", return_value=tmp_path):
            sn = "CLEANUP1"
            (tmp_path / f"{sn}.sock").write_text("")
            (tmp_path / f"{sn}.pid").write_text("1")
            _cleanup_stale(sn)
            assert not (tmp_path / f"{sn}.sock").exists()
            assert not (tmp_path / f"{sn}.pid").exists()


# --- MeasurementResult stats-only mode ---


class TestMeasurementResultStatsOnly:
    def test_precomputed_stats(self):
        result = MeasurementResult(
            samples=[],
            duration_s=1.0,
            sample_count=100000,
            _mean_ua=45000.0,
            _min_ua=100.0,
            _max_ua=90000.0,
            _p99_ua=85000.0,
        )
        assert result.mean_ua == 45000.0
        assert result.min_ua == 100.0
        assert result.max_ua == 90000.0
        assert result.p99_ua == 85000.0
        assert result.peak_ma == 90.0

    def test_stats_from_samples_when_no_precomputed(self):
        from ppk2.types import Sample

        samples = [
            Sample(current_ua=100.0, range=2, logic=0, counter=0),
            Sample(current_ua=200.0, range=2, logic=0, counter=1),
            Sample(current_ua=300.0, range=2, logic=0, counter=2),
        ]
        result = MeasurementResult(samples=samples, sample_count=3)
        assert result.mean_ua == 200.0
        assert result.min_ua == 100.0
        assert result.max_ua == 300.0


# --- Multiple concurrent clients ---


class TestConcurrentClients:
    def test_multiple_status_queries(self, harness):
        h, sock_path = harness
        results = []

        def query():
            resp = _send_recv(sock_path, {"cmd": "status"})
            results.append(resp)

        threads = [threading.Thread(target=query) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert len(results) == 5
        for r in results:
            assert r["ok"] is True

    def test_power_commands_from_different_clients(self, harness):
        h, sock_path = harness
        # Client 1 turns power on
        c1 = h.client(sock_path)
        c1.toggle_dut_power(True)

        # Client 2 reads state
        c2 = h.client(sock_path)
        state = c2.status()
        assert state["dut_power"] is True

        # Client 2 turns power off
        c2.toggle_dut_power(False)

        # Client 1 reads updated state
        state = c1.status()
        assert state["dut_power"] is False
