"""Save and load .ppk2 files (nRF Connect Power Profiler format).

A .ppk2 file is a ZIP archive containing:
- session.raw: 6-byte frames (Float32LE current_ua + Uint16LE logic)
- metadata.json: sampling rate, start time, format version
- minimap.raw: downsampled min/max pairs for the overview chart
"""

import json
import struct
import time
import zipfile
from io import BytesIO
from pathlib import Path

from .types import MeasurementResult, SAMPLES_PER_SECOND, Sample, Scope

FRAME_SIZE = 6  # 4 bytes float32 + 2 bytes uint16
FORMAT_VERSION = 2
EVENTS_FORMAT_VERSION = 1
MINIMAP_MAX_ELEMENTS = 10_000


def save_ppk2(
    result: MeasurementResult,
    output_path: str | Path,
    start_time_ms: int | None = None,
    samples_per_second: int = SAMPLES_PER_SECOND,
    events: list[Scope] | None = None,
) -> None:
    """Save a MeasurementResult as a .ppk2 file.

    Args:
        result: Measurement data to save.
        output_path: Path for the .ppk2 file.
        start_time_ms: Unix epoch timestamp in milliseconds. Defaults to now.
        samples_per_second: Sampling rate. Defaults to 100000.
        events: Scope intervals to embed as events.json inside the ZIP.
            If None, falls back to result.events. If both are empty, no
            events.json is written and the file is bit-identical to a
            pre-events save.
    """
    if start_time_ms is None:
        start_time_ms = int(time.time() * 1000)
    if events is None:
        events = result.events

    output_path = Path(output_path)

    # Build session.raw — 6 bytes per sample
    session_buf = bytearray(len(result.samples) * FRAME_SIZE)
    for i, s in enumerate(result.samples):
        struct.pack_into("<fH", session_buf, i * FRAME_SIZE, s.current_ua, s.logic)

    # Build metadata.json
    metadata = {
        "metadata": {
            "samplesPerSecond": samples_per_second,
            "startSystemTime": start_time_ms,
        },
        "formatVersion": FORMAT_VERSION,
    }

    # Build minimap.raw — downsampled min/max for overview
    minimap = _build_minimap(result.samples, samples_per_second)

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("session.raw", bytes(session_buf))
        zf.writestr("metadata.json", json.dumps(metadata))
        zf.writestr("minimap.raw", json.dumps(minimap))
        if events:
            zf.writestr("events.json", json.dumps(_events_to_json(events)))


def _events_to_json(events: list[Scope]) -> dict:
    """Serialise a list of Scope to the events.json schema."""
    out_scopes = []
    for s in events:
        item = {
            "name": s.name,
            "start_s": s.start_s,
            "end_s": s.end_s,
        }
        if s.channel is not None:
            item["channel"] = s.channel
        out_scopes.append(item)
    return {"version": EVENTS_FORMAT_VERSION, "scopes": out_scopes}


def load_ppk2(input_path: str | Path) -> MeasurementResult:
    """Load a .ppk2 file into a MeasurementResult.

    Args:
        input_path: Path to the .ppk2 file.

    Returns:
        MeasurementResult with samples, metadata, and (if present) events.

    Raises:
        ValueError: If events.json has an unknown schema version.
    """
    input_path = Path(input_path)

    with zipfile.ZipFile(input_path, "r") as zf:
        session_data = zf.read("session.raw")
        metadata_json = json.loads(zf.read("metadata.json"))
        names = set(zf.namelist())
        events_doc = (
            json.loads(zf.read("events.json")) if "events.json" in names else None
        )

    meta = metadata_json.get("metadata", {})
    samples_per_second = meta.get("samplesPerSecond", SAMPLES_PER_SECOND)

    n_samples = len(session_data) // FRAME_SIZE
    samples: list[Sample] = []

    for i in range(n_samples):
        offset = i * FRAME_SIZE
        current_ua, logic = struct.unpack_from("<fH", session_data, offset)
        samples.append(
            Sample(
                current_ua=current_ua,
                range=0,
                logic=logic,
                counter=i & 0x3F,
            )
        )

    duration_s = n_samples / samples_per_second if samples_per_second else 0.0
    events = _events_from_json(events_doc) if events_doc is not None else []

    return MeasurementResult(
        samples=samples,
        duration_s=duration_s,
        sample_count=n_samples,
        lost_samples=0,
        events=events,
    )


def _events_from_json(doc: dict) -> list[Scope]:
    """Parse the events.json dict, raising on unknown schema version."""
    version = doc.get("version")
    if version != EVENTS_FORMAT_VERSION:
        raise ValueError(
            f"Unknown events.json schema version {version!r} "
            f"(this build understands version {EVENTS_FORMAT_VERSION})"
        )
    return [
        Scope(
            name=s["name"],
            start_s=s["start_s"],
            end_s=s["end_s"],
            channel=s.get("channel"),
        )
        for s in doc.get("scopes", [])
    ]


def _build_minimap(
    samples: list[Sample],
    samples_per_second: int,
) -> dict:
    """Build the minimap folding buffer structure.

    Progressively downsamples to at most MINIMAP_MAX_ELEMENTS min/max pairs.
    """
    if not samples:
        return {
            "lastElementFoldCount": 0,
            "data": {"length": 0, "min": [], "max": []},
            "maxNumberOfElements": MINIMAP_MAX_ELEMENTS,
            "numberOfTimesToFold": 0,
        }

    us_per_sample = 1_000_000 / samples_per_second
    n = len(samples)

    # Determine fold level so we have <= MINIMAP_MAX_ELEMENTS entries
    fold_count = 1
    n_folds = 0
    while n / fold_count > MINIMAP_MAX_ELEMENTS:
        fold_count *= 2
        n_folds += 1

    min_points = []
    max_points = []

    for bucket_start in range(0, n, fold_count):
        bucket_end = min(bucket_start + fold_count, n)
        bucket = samples[bucket_start:bucket_end]

        currents = [s.current_ua for s in bucket]
        min_val = min(currents)
        max_val = max(currents)

        # nRF Connect stores nanoamps in the minimap
        timestamp_us = bucket_start * us_per_sample
        min_points.append({"x": timestamp_us, "y": min_val * 1000})
        max_points.append({"x": timestamp_us, "y": max_val * 1000})

    return {
        "lastElementFoldCount": fold_count,
        "data": {
            "length": len(min_points),
            "min": min_points,
            "max": max_points,
        },
        "maxNumberOfElements": MINIMAP_MAX_ELEMENTS,
        "numberOfTimesToFold": n_folds,
    }
