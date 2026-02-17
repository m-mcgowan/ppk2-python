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

from .types import MeasurementResult, Sample

FRAME_SIZE = 6  # 4 bytes float32 + 2 bytes uint16
FORMAT_VERSION = 2
SAMPLES_PER_SECOND = 100_000
MINIMAP_MAX_ELEMENTS = 10_000


def save_ppk2(
    result: MeasurementResult,
    output_path: str | Path,
    start_time_ms: int | None = None,
    samples_per_second: int = SAMPLES_PER_SECOND,
) -> None:
    """Save a MeasurementResult as a .ppk2 file.

    Args:
        result: Measurement data to save.
        output_path: Path for the .ppk2 file.
        start_time_ms: Unix epoch timestamp in milliseconds. Defaults to now.
        samples_per_second: Sampling rate. Defaults to 100000.
    """
    if start_time_ms is None:
        start_time_ms = int(time.time() * 1000)

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


def load_ppk2(input_path: str | Path) -> MeasurementResult:
    """Load a .ppk2 file into a MeasurementResult.

    Args:
        input_path: Path to the .ppk2 file.

    Returns:
        MeasurementResult with samples and metadata.
    """
    input_path = Path(input_path)

    with zipfile.ZipFile(input_path, "r") as zf:
        session_data = zf.read("session.raw")
        metadata_json = json.loads(zf.read("metadata.json"))

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
                range=0,  # range not stored in .ppk2
                logic=logic,
                counter=i & 0x3F,
            )
        )

    duration_s = n_samples / samples_per_second if samples_per_second else 0.0

    return MeasurementResult(
        samples=samples,
        duration_s=duration_s,
        sample_count=n_samples,
        lost_samples=0,
    )


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
