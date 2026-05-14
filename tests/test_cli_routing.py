"""Tests for the CLI's transport routing chokepoint (`_open_device`)."""

from unittest.mock import MagicMock, patch

import pytest

from ppk2 import cli


def test_open_device_direct_returns_ppk2device(monkeypatch):
    """transport='direct' opens the hardware directly via PPK2Device.open
    when no daemon owns the device. Resolves the device and checks
    find_daemon; with no match the call falls through to PPK2Device.open.
    """
    monkeypatch.setattr("ppk2.daemon.find_daemon", lambda **kw: None)
    monkeypatch.setattr(
        "ppk2.transport.resolve_device",
        lambda **kw: type(
            "D", (), {"serial_number": "SN", "port": "P"}
        )(),
    )
    with patch("ppk2.device.PPK2Device.open") as mock_open:
        mock_open.return_value = MagicMock()
        ppk = cli._open_device(port=None, serial=None, transport="direct")
        assert ppk is mock_open.return_value


def test_open_device_direct_interlocks_against_running_daemon(
    monkeypatch,
):
    """transport='direct' must refuse to bypass a running daemon even
    when no --serial/--port selector was passed. Auto-detect must still
    trigger the interlock — opening the port directly while the daemon
    holds it would race on writes and corrupt streaming.
    """
    monkeypatch.setattr(
        "ppk2.transport.resolve_device",
        lambda **kw: type(
            "D", (), {"serial_number": "SN", "port": "P"}
        )(),
    )
    monkeypatch.setattr(
        "ppk2.daemon.find_daemon",
        lambda **kw: ("SN", "/tmp/SN.sock"),
    )
    with pytest.raises(SystemExit):
        cli._open_device(port=None, serial=None, transport="direct")


def test_open_device_daemon_errors_when_no_daemon(monkeypatch):
    """transport='daemon' must fail-fast (SystemExit) when no daemon is
    running for the resolved device, rather than silently spawning one
    or falling back to direct."""
    monkeypatch.setattr("ppk2.daemon.find_daemon", lambda **kw: None)
    monkeypatch.setattr(
        "ppk2.transport.resolve_device",
        lambda **kw: type(
            "D", (), {"serial_number": "SN", "port": "P"}
        )(),
    )
    with pytest.raises(SystemExit):
        cli._open_device(port=None, serial="SN", transport="daemon")


def test_autospawn_holds_flock_during_spawn(monkeypatch, tmp_path):
    """Concurrent auto-spawn for the same serial must serialise via flock.

    The lock file's existence is the testable side-effect — the second
    branch of find_daemon (which returns the fake info post-spawn) is
    what the with-block returns.
    """
    monkeypatch.setattr(
        "ppk2._daemon_lib.state.state_dir", lambda: tmp_path
    )
    spawn_calls = []
    monkeypatch.setattr(
        "ppk2.daemon.start_daemon",
        lambda **kw: spawn_calls.append(kw) or ("SN", 12345),
    )
    fake_info = ("SN", tmp_path / "SN.sock")
    state = {"calls": 0}

    def fake_find(**kw):
        state["calls"] += 1
        return None if state["calls"] == 1 else fake_info

    monkeypatch.setattr("ppk2.daemon.find_daemon", fake_find)
    monkeypatch.setattr(
        "serial.tools.list_ports.comports", lambda: []
    )
    monkeypatch.setattr(
        "ppk2.client.DaemonClient", lambda *a, **k: MagicMock()
    )

    cli._autospawn_daemon("SN")
    assert len(spawn_calls) == 1
    assert (tmp_path / "SN.spawn.lock").exists()


def test_autospawn_errors_when_device_in_dfu(monkeypatch, tmp_path):
    """DFU pre-check in the CLI mirrors the daemon-side check so the
    user gets a clear pointer at abort-dfu before any spawn attempt."""
    monkeypatch.setattr(
        "ppk2._daemon_lib.state.state_dir", lambda: tmp_path
    )
    fake_dfu = type("P", (), {
        "vid": 0x1915, "pid": 0x521F,
        "device": "/dev/cu.fakeDFU", "serial_number": "DFU_SN",
        "location": "fake",
    })
    monkeypatch.setattr(
        "serial.tools.list_ports.comports", lambda: [fake_dfu]
    )
    with pytest.raises(SystemExit):
        cli._autospawn_daemon("DFU_SN")


def test_open_device_auto_uses_daemon_when_running(monkeypatch, tmp_path):
    """transport='auto' must prefer an existing daemon over a fresh
    direct connect, so concurrent CLI invocations cooperate via a single
    daemon owning the serial port."""
    fake_client = MagicMock()
    monkeypatch.setattr(
        "ppk2.daemon.find_daemon",
        lambda **kw: ("SN", tmp_path / "SN.sock"),
    )
    monkeypatch.setattr(
        "ppk2.transport.resolve_device",
        lambda **kw: type(
            "D", (), {"serial_number": "SN", "port": "P"}
        )(),
    )
    with patch("ppk2.client.DaemonClient", return_value=fake_client):
        result = cli._open_device(
            port=None, serial="SN", transport="auto"
        )
        assert result is fake_client
