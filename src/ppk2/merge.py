"""Merge Chrome Trace JSON with PPK2 power data.

Combines an execution trace (scope events from embedded-tracer) with
PPK2 power measurements, producing a unified Chrome Trace JSON file
that opens in Perfetto UI with scope swim lanes and a power counter graph.

Usage::

    from ppk2.merge import merge_trace_ppk2

    merge_trace_ppk2("trace.json", "capture.ppk2", "merged.json")

CLI::

    ppk2 merge trace.json capture.ppk2 -o merged.json
"""

import json
from pathlib import Path

from .ppk2file import load_ppk2

# Default downsampling rate: 100 kHz → 1 kHz
DEFAULT_DOWNSAMPLE = 100


def merge_trace_ppk2(
    trace_path: str | Path,
    ppk2_path: str | Path,
    output_path: str | Path | None = None,
    downsample: int = DEFAULT_DOWNSAMPLE,
    samples_per_second: int = 100_000,
) -> Path:
    """Merge a Chrome Trace file with PPK2 power data.

    The PPK2 samples are downsampled and inserted as Chrome Trace counter
    events (``ph:"C"``, name: ``current_ua``), time-aligned to the trace's
    first scope event.

    Args:
        trace_path: Path to Chrome Trace JSON file (``{"traceEvents": [...]}``
            or bare array).
        ppk2_path: Path to ``.ppk2`` file.
        output_path: Output path. Defaults to ``<trace_stem>_merged.json``.
        downsample: Take every Nth sample (default 100 → 1 kHz from 100 kHz).
        samples_per_second: PPK2 sampling rate.

    Returns:
        Path to the output file.
    """
    trace_path = Path(trace_path)
    ppk2_path = Path(ppk2_path)

    if output_path is None:
        output_path = trace_path.with_name(f"{trace_path.stem}_merged.json")
    else:
        output_path = Path(output_path)

    # Load trace events
    trace_data = json.loads(trace_path.read_text())
    if isinstance(trace_data, list):
        trace_events = trace_data
        trace_data = {"traceEvents": trace_events}
    else:
        trace_events = trace_data.get("traceEvents", [])

    # Find the trace start time (first B event's timestamp in µs)
    trace_start_us = _find_trace_start(trace_events)

    # Load PPK2 data
    result = load_ppk2(ppk2_path)

    # Generate counter events from PPK2 samples
    counter_events = _ppk2_to_counter_events(
        result, trace_start_us, downsample, samples_per_second
    )

    # Add metadata event for the power track
    metadata_event = {
        "ph": "M",
        "pid": 1,
        "tid": 0,
        "name": "thread_name",
        "args": {"name": "PPK2 Power"},
    }

    # Merge
    trace_data["traceEvents"] = trace_events + [metadata_event] + counter_events

    output_path.write_text(json.dumps(trace_data))
    return output_path


def _find_trace_start(events: list[dict]) -> int:
    """Find the timestamp of the first scope begin event."""
    for event in events:
        if event.get("ph") == "B":
            return event.get("ts", 0)
    # Fallback: first event's timestamp
    if events:
        return events[0].get("ts", 0)
    return 0


def _ppk2_to_counter_events(
    result,
    trace_start_us: int,
    downsample: int,
    samples_per_second: int,
) -> list[dict]:
    """Convert PPK2 samples to Chrome Trace counter events.

    Args:
        result: MeasurementResult from load_ppk2.
        trace_start_us: Trace start timestamp in microseconds.
        downsample: Take every Nth sample.
        samples_per_second: PPK2 sampling rate.

    Returns:
        List of Chrome Trace counter event dicts.
    """
    us_per_sample = 1_000_000 / samples_per_second
    events = []

    for i in range(0, len(result.samples), downsample):
        sample = result.samples[i]
        ts = trace_start_us + int(i * us_per_sample)
        events.append({
            "ph": "C",
            "ts": ts,
            "name": "current_ua",
            "pid": 1,
            "tid": 0,
            "args": {"value": round(sample.current_ua, 1)},
        })

    return events
