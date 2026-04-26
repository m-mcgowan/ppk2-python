"""Measure actual `time.sleep(0.001)` duration on the host.

Background: the daemon's reader thread calls ``time.sleep(0.001)`` between
serial drains. On Apple Silicon the macOS scheduler appears to round short
sleeps up to the next ~9 ms tick, while Intel Macs honour them at ~1.3 ms.
That mismatch is the root cause of the workshop sample-loss issue
documented in ``docs/daemon_reader_loop_issues.md`` (#1).

This probe needs no PPK2 hardware. Run on each host to confirm the
disparity:

    python scripts/probe_sleep_granularity.py            # default n=2000
    python scripts/probe_sleep_granularity.py --n 5000   # longer sample
"""
from __future__ import annotations

import argparse
import time


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--n", type=int, default=2000,
        help="Number of sleeps to time (default 2000)",
    )
    ap.add_argument(
        "--target-ms", type=float, default=1.0,
        help="Sleep target in ms (default 1.0 — matches daemon)",
    )
    args = ap.parse_args()

    target_s = args.target_ms / 1000
    deltas_ms: list[float] = []
    for _ in range(args.n):
        t0 = time.monotonic()
        time.sleep(target_s)
        deltas_ms.append((time.monotonic() - t0) * 1000)
    deltas_ms.sort()

    def pct(p: float) -> float:
        return deltas_ms[int(len(deltas_ms) * p)]

    print(
        f"sleep({args.target_ms}ms) actual ms — n={len(deltas_ms)} "
        f"min={deltas_ms[0]:.2f} median={pct(0.50):.2f} "
        f"p95={pct(0.95):.2f} p99={pct(0.99):.2f} max={deltas_ms[-1]:.2f}"
    )


if __name__ == "__main__":
    main()
