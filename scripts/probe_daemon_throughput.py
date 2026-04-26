"""Daemon-path throughput probe — exposes the workshop sample-loss bug.

Connects to a running ``ppk2`` daemon's Unix socket, asks for raw
streaming mode, drains bytes as fast as possible for N seconds, and
prints throughput vs the 400 KB/s nominal (100 kHz × 4 B/sample).

This is the probe that exposes ``docs/daemon_reader_loop_issues.md``
issue #1: on Apple Silicon the daemon's reader-thread sleep granularity
caps draining at ~37 % nominal.

Requires a running daemon — start one first with whatever entry point
the host uses (``ppk2_daemon.py start`` on the workshop, or
``python -c 'from ppk2.daemon import start_daemon; start_daemon()'``).

Usage::

    python scripts/probe_daemon_throughput.py
    python scripts/probe_daemon_throughput.py --seconds 30
    python scripts/probe_daemon_throughput.py --busyloop  # no consumer-side sleep
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time

from ppk2.daemon import find_daemon


NOMINAL_BPS = 400_000


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument(
        "--busyloop", action="store_true",
        help="Drain in a tight loop with no consumer-side sleep",
    )
    args = ap.parse_args()

    info = find_daemon()
    if info is None:
        sys.exit("No daemon running. Start one before invoking this probe.")
    serial_num, sock_path = info
    print(f"daemon serial={serial_num} sock={sock_path}")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(sock_path))
    try:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass

        sock.sendall(
            json.dumps({"cmd": "measure_start", "raw": True}).encode() + b"\n"
        )

        # Read one ack line (newline-delimited JSON)
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                sys.exit("daemon closed early before ack")
            data += chunk
        first, _, leftover = data.partition(b"\n")
        ack = json.loads(first.decode("utf-8"))
        if not ack.get("ok"):
            sys.exit(f"measure_start failed: {ack}")

        t0 = time.monotonic()
        deadline = t0 + args.seconds
        total = len(leftover)
        polls = empty = 0

        if args.busyloop:
            sock.settimeout(0.05)
            while time.monotonic() - t0 < args.seconds:
                polls += 1
                try:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                except (socket.timeout, BlockingIOError):
                    empty += 1
            sock.settimeout(None)
        else:
            sock.setblocking(False)
            while time.monotonic() < deadline:
                polls += 1
                try:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                except BlockingIOError:
                    empty += 1
                    time.sleep(0.0005)

        elapsed = time.monotonic() - t0
    finally:
        try:
            sock.setblocking(True)
            sock.sendall(
                json.dumps({"cmd": "measure_stop"}).encode() + b"\n"
            )
        except Exception:
            pass
        sock.close()

    bps = total / elapsed
    pct = bps / NOMINAL_BPS * 100
    print(f"bytes={total} elapsed={elapsed:.3f}s polls={polls} empty={empty}")
    print(
        f"throughput: {bps / 1024:.1f} KB/s = {pct:.1f}% of nominal "
        f"(implied loss = {100 - pct:.1f}%)"
    )


if __name__ == "__main__":
    main()
