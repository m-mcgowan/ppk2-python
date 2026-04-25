"""Capture a PPK2 measurement and annotate it with DUT trace events.

The intended workflow for firmware power profiling:

1. DUT firmware emits Chrome JSON scope events over serial, e.g.:
       {"ph":"B","ts":500000,"name":"gps","pid":1,"tid":1}
       {"ph":"E","ts":2000000,"name":"gps","pid":1,"tid":1}
2. Host runs a PPK2 capture via the daemon client and concurrently reads
   DUT serial output.
3. After the capture, parse the serial output and overlay the events as
   synthetic D0-D7 channels on the measurement.
4. Save as ``.ppk2`` (+ ``legend.json`` describing what each channel means).
   The result opens in nRF Connect Power Profiler with the scopes as
   logic channels alongside the current trace.

This example uses a **synthetic** measurement and a canned serial-event
stream so it runs without hardware. The real-hardware skeleton is in the
``real_hardware_skeleton`` function below — it's not executed but shows
the daemon / threading pattern.

Run:
    python examples/capture_with_events.py
"""

from __future__ import annotations

import json
from pathlib import Path

from ppk2.events import parse_serial_events
from ppk2.ppk2file import save_ppk2
from ppk2.synthetic import ProfileBuilder

OUTPUT_DIR = Path(__file__).parent / "output"


# --------------------------------------------------------------------------- #
# Synthetic measurement + canned DUT event stream                             #
# --------------------------------------------------------------------------- #

def make_measurement():
    """Build a synthetic 2-second measurement that looks like a sensor cycle."""
    return (
        ProfileBuilder(seed=42)
        .phase("idle",    current_ua=200,    duration_s=0.2, noise_ua=20)
        .phase("gps",     current_ua=25_000, duration_s=0.6, noise_ua=1500)
        .phase("sensor",  current_ua=4_000,  duration_s=0.3, noise_ua=200)
        .phase("radio",   current_ua=40_000, duration_s=0.3, noise_ua=3000)
        .phase("idle",    current_ua=200,    duration_s=0.6, noise_ua=20)
        .build()
    )


def canned_serial_stream(capture_start_device_us: int) -> str:
    """A DUT could emit this sequence of lines (Chrome JSON + noise)."""
    def b(name, ts_ms):
        return json.dumps({
            "ph": "B",
            "ts": capture_start_device_us + ts_ms * 1000,
            "name": name, "pid": 1, "tid": 1,
        })

    def e(name, ts_ms):
        return json.dumps({
            "ph": "E",
            "ts": capture_start_device_us + ts_ms * 1000,
            "name": name, "pid": 1, "tid": 1,
        })

    lines = [
        "[boot] entering sensor cycle",
        b("boot",   0),
        b("gps",    200),
        e("gps",    800),
        b("sensor", 800),
        e("sensor", 1100),
        b("radio",  1100),
        e("radio",  1400),
        e("boot",   1700),
        "[boot] cycle complete",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Main — parse → apply → save                                                 #
# --------------------------------------------------------------------------- #

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    measurement = make_measurement()

    # Device timestamp at the moment the PPK2 capture began. In a real run,
    # the host records this from the first event it sees, or asks the DUT.
    # Here we pretend the DUT booted 10 seconds before the capture started
    # — i.e. all device `ts` values are offset by this amount.
    capture_start_device_us = 10_000_000

    serial_output = canned_serial_stream(capture_start_device_us)

    # `None` = software-only scope: appears in the embedded events.json
    # and the HTML report's Named-scopes table, but does not toggle a
    # D0–D7 hardware bit (so nRF Connect Power Profiler ignores it).
    channel_map = {"gps": 0, "sensor": 1, "radio": 2, "boot": None}

    # NOTE on time alignment (see docs/dx_notes.md — events.py time alignment):
    # `parse_serial_events` reads the raw device `ts` field. If the DUT has
    # been running before the capture started, every event's timestamp is
    # far past the capture window and `apply()` will warn that events are
    # outside the window. The canonical fix is to subtract the device time
    # at capture-start from every event.
    #
    # For this example we subtract the offset by mutating the timestamps on
    # the mapper's events after parsing — a real consumer would plumb this
    # through `parse_serial_events(..., offset_s=...)` once that option
    # lands.
    mapper = parse_serial_events(serial_output, channel_map)
    offset_s = capture_start_device_us / 1_000_000
    for ev in mapper._events:
        ev.timestamp_s -= offset_s

    # Overlay events onto the measurement as logic channels.
    mapper.apply(measurement)

    # Save the annotated capture. New path: pass scope intervals via the
    # `events` kwarg and they're embedded as events.json inside the .ppk2
    # zip — the HTML report then renders them as a "Named scopes" table
    # without needing a separate sidecar.
    out_ppk2 = OUTPUT_DIR / "annotated.ppk2"
    scopes = mapper.to_scopes(measurement)
    save_ppk2(measurement, out_ppk2, events=scopes)

    # The legacy sidecar still works and stays the right choice if you
    # need the legend in nRF Connect Power Profiler (which can't read
    # the embedded events).
    out_legend = OUTPUT_DIR / "annotated.ppk2.legend.json"
    mapper.save_legend(out_legend)

    print(f"Saved {out_ppk2}")
    print(f"Saved {out_legend}")
    print()
    print("Legend:")
    for ch, name in mapper.legend()["channels"].items():
        print(f"  {ch}: {name}")
    print()
    print(f"Events applied: {len(mapper._events)}")
    print(f"Embedded scopes: {len(scopes)}")
    print(f"Duration:       {measurement.duration_s:.2f} s")
    print(f"Mean current:   {measurement.mean_ua:.0f} µA")


# --------------------------------------------------------------------------- #
# Real-hardware skeleton (not executed — reference only)                       #
# --------------------------------------------------------------------------- #

def real_hardware_skeleton():  # pragma: no cover - reference code, not run
    """Pattern for doing this against real hardware.

    Requires a running ppk2 daemon holding the PPK2 port, and a separate
    serial link to the DUT's debug UART (NOT the PPK2 port).

    Sketch:

        import threading, time
        from ppk2.client import DaemonClient, find_daemon
        import serial

        info = find_daemon()              # or start_daemon(...)
        serial_sn, sock_path = info
        client = DaemonClient(sock_path)
        dut = serial.Serial("/dev/ttyUSB0", 115200, timeout=0.1)

        serial_buf: list[str] = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                line = dut.readline().decode("utf-8", errors="replace")
                if line:
                    serial_buf.append(line)

        # Capture-start device time: read the DUT's current micros() so we
        # can subtract it later. (Protocol-specific — e.g. send a 'ts?'
        # command or use the first event's ts as t=0 via an
        # align_to_first() helper.)
        capture_start_device_us = query_dut_micros(dut)

        threading.Thread(target=reader, daemon=True).start()
        client.toggle_dut_power(True)
        time.sleep(1)                      # settle

        try:
            result = client.measure(duration_s=5.0)
        finally:
            stop.set()
            client.toggle_dut_power(False)

        mapper = parse_serial_events("".join(serial_buf),
                                     {"gps": 0, "sensor": 1, "radio": 2})
        offset_s = capture_start_device_us / 1_000_000
        for ev in mapper._events:
            ev.timestamp_s -= offset_s
        mapper.apply(result)

        save_ppk2(result, "capture.ppk2")
        mapper.save_legend("capture.ppk2.legend.json")
    """


if __name__ == "__main__":
    main()
