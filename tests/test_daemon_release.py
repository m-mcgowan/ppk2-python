"""Tests for the daemon's `release` / `acquire` IPC commands.

`release` closes the PPK2 serial port without killing the daemon process;
`acquire` reopens. Hardware-touching commands are rejected while released.
"""

import pytest

from tests.test_daemon import DaemonTestHarness, _send_recv


@pytest.fixture
def daemon():
    h = DaemonTestHarness()
    sock_path = h.start()
    try:
        yield h, sock_path
    finally:
        h.stop()


def test_release_closes_transport_and_keeps_socket(daemon):
    h, sock_path = daemon
    resp = _send_recv(sock_path, {"cmd": "release"})
    assert resp["ok"] is True
    # Transport closed but the daemon is still serving.
    assert not h.server._transport.is_open
    resp2 = _send_recv(sock_path, {"cmd": "status"})
    assert resp2["ok"] is True
    assert resp2["state"]["released"] is True


def test_hardware_command_rejected_while_released(daemon):
    _, sock_path = daemon
    _send_recv(sock_path, {"cmd": "release"})
    resp = _send_recv(sock_path, {"cmd": "power", "on": True})
    assert resp["ok"] is False
    assert "released" in resp["error"].lower()


def test_acquire_reopens_transport(daemon):
    h, sock_path = daemon
    _send_recv(sock_path, {"cmd": "release"})
    resp = _send_recv(sock_path, {"cmd": "acquire"})
    assert resp["ok"] is True
    assert h.server._transport.is_open
    resp2 = _send_recv(sock_path, {"cmd": "status"})
    assert resp2["state"]["released"] is False


def test_release_when_already_released_is_idempotent(daemon):
    _, sock_path = daemon
    _send_recv(sock_path, {"cmd": "release"})
    resp = _send_recv(sock_path, {"cmd": "release"})
    assert resp["ok"] is True


def test_acquire_when_not_released_is_idempotent(daemon):
    _, sock_path = daemon
    resp = _send_recv(sock_path, {"cmd": "acquire"})
    assert resp["ok"] is True
