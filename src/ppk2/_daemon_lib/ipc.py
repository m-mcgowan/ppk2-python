"""IPC socket factory + endpoint discovery, platform-aware.

On Unix: ``AF_UNIX`` stream socket bound to ``<state_dir>/<SERIAL>.sock``.
On Windows: ``AF_INET`` stream socket bound to ``127.0.0.1:0`` (OS-assigned
port), with a discovery file ``<state_dir>/<SERIAL>.endpoint`` containing
``127.0.0.1:<port>``.

The returned ``socket.socket`` exposes the same ``recv/sendall/fileno/
setblocking/setsockopt`` surface in either case, so existing selector and
read/write code is unchanged regardless of platform.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

from .state import state_dir


def _sock_path(serial: str) -> Path:
    """Endpoint file for Unix (file IS the socket)."""
    return state_dir() / f"{serial}.sock"


def _endpoint_path(serial: str) -> Path:
    """Endpoint file for Windows (text file with ``host:port``)."""
    return state_dir() / f"{serial}.endpoint"


def endpoint_path(serial: str) -> Path:
    """The endpoint discovery file for this platform."""
    if sys.platform == "win32":
        return _endpoint_path(serial)
    return _sock_path(serial)


def endpoint_glob() -> str:
    """Glob pattern for find-daemon enumeration."""
    return "*.endpoint" if sys.platform == "win32" else "*.sock"


def listener_for(serial: str) -> tuple[socket.socket, Path]:
    """Bind a server socket and return ``(socket, endpoint_file)``.

    The socket is bound and ready for ``listen()`` but not yet listening.
    Caller is responsible for ``setblocking`` / ``listen()`` / cleanup.
    """
    if sys.platform == "win32":
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        endpoint = _endpoint_path(serial)
        endpoint.write_text(f"127.0.0.1:{port}")
        return sock, endpoint

    endpoint = _sock_path(serial)
    if endpoint.exists():
        endpoint.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(endpoint))
    return sock, endpoint


def connect_to(serial: str, *, timeout: float | None = None) -> socket.socket:
    """Connect to a daemon's IPC endpoint for ``serial``.

    Raises:
        FileNotFoundError: No endpoint exists for that serial.
        ConnectionError: Endpoint exists but daemon isn't accepting.
    """
    if sys.platform == "win32":
        ep = _endpoint_path(serial)
        if not ep.exists():
            raise FileNotFoundError(f"No daemon endpoint for {serial}")
        host, port_s = ep.read_text().strip().split(":")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if timeout is not None:
            sock.settimeout(timeout)
        sock.connect((host, int(port_s)))
        return sock

    ep = _sock_path(serial)
    if not ep.exists():
        raise FileNotFoundError(f"No daemon socket for {serial}")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if timeout is not None:
        sock.settimeout(timeout)
    sock.connect(str(ep))
    return sock
