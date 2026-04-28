"""Tests for the CLI's transport routing chokepoint (`_open_device`)."""

from unittest.mock import MagicMock, patch

import pytest

from ppk2 import cli


def test_open_device_direct_returns_ppk2device():
    """transport='direct' must skip daemon discovery entirely and open
    the hardware directly via PPK2Device.open."""
    with patch("ppk2.device.PPK2Device.open") as mock_open:
        mock_open.return_value = MagicMock()
        ppk = cli._open_device(port=None, serial=None, transport="direct")
        assert ppk is mock_open.return_value


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
