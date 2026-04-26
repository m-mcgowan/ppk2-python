"""Raw pyserial throughput probe — bypasses the daemon and the parser.

Sends the minimum sequence to start streaming
(``regulator_set`` + ``average_start``), drains bytes for N seconds, and
prints throughput vs the 400 KB/s nominal (100 kHz × 4 B/sample).

If this hits ~100 % nominal the OS USB-CDC stack is fine. The daemon
and pipeline probes localise loss to one of the higher layers.

Used to diagnose ``docs/daemon_reader_loop_issues.md`` issue #1. Stop
any running daemon first — pyserial opens the port exclusively.

Usage::

    python scripts/probe_raw_throughput.py                    # auto-detect
    python scripts/probe_raw_throughput.py --port /dev/cu...  # explicit
    python scripts/probe_raw_throughput.py --seconds 30       # longer run
"""
from __future__ import annotations

import argparse
import sys
import time

import serial

from ppk2 import commands
from ppk2.transport import list_ppk2_devices


NOMINAL_BPS = 400_000  # 100 kHz * 4 B/sample


def find_default_port() -> str:
    devices = list_ppk2_devices()
    if not devices:
        sys.exit("no PPK2 found")
    return devices[0].port


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="Serial port (auto-detect if omitted)")
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--vdd-mv", type=int, default=3700)
    ap.add_argument(
        "--read-buf", type=int, default=4096,
        help="Bytes per pyserial read (default 4096)",
    )
    args = ap.parse_args()

    port = args.port or find_default_port()
    print(f"port={port} duration={args.seconds:.1f}s read_buf={args.read_buf}")

    s = serial.Serial(port, 115200, timeout=1.0, exclusive=True)
    try:
        s.write(commands.regulator_set(args.vdd_mv))
        time.sleep(0.05)
        # Discard any leftover bytes before starting
        while s.in_waiting:
            s.read(s.in_waiting)

        s.write(commands.average_start())
        t0 = time.monotonic()
        deadline = t0 + args.seconds

        total = polls = empty = max_chunk = 0
        last_nonempty = t0
        max_gap_ms = 0.0

        while (now := time.monotonic()) < deadline:
            polls += 1
            avail = s.in_waiting
            if avail:
                got = len(s.read(min(avail, args.read_buf)))
                total += got
                if got > max_chunk:
                    max_chunk = got
                gap = (now - last_nonempty) * 1000
                if gap > max_gap_ms:
                    max_gap_ms = gap
                last_nonempty = now
            else:
                empty += 1
                time.sleep(0.0005)

        elapsed = time.monotonic() - t0
    finally:
        try:
            s.write(commands.average_stop())
            time.sleep(0.05)
            while s.in_waiting:
                s.read(s.in_waiting)
        except Exception:
            pass
        s.close()

    bps = total / elapsed
    pct = bps / NOMINAL_BPS * 100
    print(f"bytes={total} elapsed={elapsed:.3f}s")
    print(
        f"throughput: {bps / 1024:.1f} KB/s ({bps / 4:.0f} samples/s) "
        f"= {pct:.1f}% of {NOMINAL_BPS // 1024} KB/s nominal "
        f"(implied loss = {100 - pct:.1f}%)"
    )
    print(
        f"polls: {polls}  empty: {empty}  "
        f"max_chunk: {max_chunk} B  max_gap_between_reads: {max_gap_ms:.1f} ms"
    )


if __name__ == "__main__":
    main()
