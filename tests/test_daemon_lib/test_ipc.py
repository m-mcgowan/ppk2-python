import socket
import sys
from pathlib import Path

import pytest

from ppk2._daemon_lib import ipc, state


@pytest.fixture(autouse=True)
def isolated_state_dir(monkeypatch, tmp_path_factory):
    """Pin state_dir() at a short tmp path for every test.

    Uses /tmp as the base to stay within the AF_UNIX socket-path limit
    (104 chars on macOS) — pytest's default tmp_path is under
    /private/var/folders/... which is too long.
    """
    import tempfile
    short_tmp = Path(tempfile.mkdtemp(dir="/tmp"))
    monkeypatch.setenv("HOME", str(short_tmp))
    monkeypatch.setenv("LOCALAPPDATA", str(short_tmp / "AppData" / "Local"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    yield
    import shutil
    shutil.rmtree(str(short_tmp), ignore_errors=True)


def test_unix_listener_round_trip(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    sock, endpoint = ipc.listener_for("ABC123")
    sock.listen(1)
    try:
        assert endpoint.exists()
        assert endpoint.suffix == ".sock"
        client = ipc.connect_to("ABC123", timeout=2.0)
        try:
            conn, _ = sock.accept()
            try:
                client.sendall(b"ping")
                assert conn.recv(4) == b"ping"
            finally:
                conn.close()
        finally:
            client.close()
    finally:
        sock.close()


def test_windows_listener_round_trip(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    sock, endpoint = ipc.listener_for("ABC123")
    sock.listen(1)
    try:
        assert endpoint.exists()
        assert endpoint.suffix == ".endpoint"
        host, port = endpoint.read_text().strip().split(":")
        assert host == "127.0.0.1"
        assert int(port) > 0
        client = ipc.connect_to("ABC123", timeout=2.0)
        try:
            conn, _ = sock.accept()
            try:
                client.sendall(b"ping")
                assert conn.recv(4) == b"ping"
            finally:
                conn.close()
        finally:
            client.close()
    finally:
        sock.close()


def test_connect_to_missing_serial_raises(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(FileNotFoundError):
        ipc.connect_to("NONEXIST")


def test_endpoint_glob_unix(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert ipc.endpoint_glob() == "*.sock"


def test_endpoint_glob_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert ipc.endpoint_glob() == "*.endpoint"
