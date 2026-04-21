"""Tests for trace + PPK2 merge functionality."""

import json

import pytest

from ppk2.merge import merge_trace_ppk2, _find_trace_start, _ppk2_to_counter_events
from ppk2.ppk2file import save_ppk2
from ppk2.types import MeasurementResult, Sample


def _make_result(n_samples: int = 1000, current_ua: float = 10.0) -> MeasurementResult:
    return MeasurementResult(
        samples=[
            Sample(current_ua=current_ua + i * 0.01, range=0, logic=0, counter=i & 0x3F)
            for i in range(n_samples)
        ],
        duration_s=n_samples / 100_000,
        sample_count=n_samples,
    )


def _make_trace_events() -> list[dict]:
    return [
        {"ph": "M", "pid": 1, "tid": 1, "name": "process_name", "args": {"name": "firmware"}},
        {"ph": "B", "ts": 1000000, "name": "gps_fix", "pid": 1, "tid": 1},
        {"ph": "B", "ts": 1200000, "name": "imu_read", "pid": 1, "tid": 1},
        {"ph": "E", "ts": 1300000, "name": "imu_read", "pid": 1, "tid": 1},
        {"ph": "E", "ts": 2000000, "name": "gps_fix", "pid": 1, "tid": 1},
    ]


class TestFindTraceStart:
    def test_finds_first_b_event(self):
        events = _make_trace_events()
        assert _find_trace_start(events) == 1000000

    def test_skips_metadata_events(self):
        events = [
            {"ph": "M", "ts": 0, "name": "meta"},
            {"ph": "B", "ts": 5000, "name": "start"},
        ]
        assert _find_trace_start(events) == 5000

    def test_empty_events(self):
        assert _find_trace_start([]) == 0

    def test_no_b_event_falls_back(self):
        events = [{"ph": "E", "ts": 3000, "name": "end"}]
        assert _find_trace_start(events) == 3000


class TestPpk2ToCounterEvents:
    def test_basic_downsampling(self):
        result = _make_result(1000, current_ua=100.0)
        events = _ppk2_to_counter_events(result, trace_start_us=0, downsample=100, samples_per_second=100_000)

        # 1000 samples / 100 downsample = 10 counter events
        assert len(events) == 10
        assert all(e["ph"] == "C" for e in events)
        assert all(e["name"] == "current_ua" for e in events)

    def test_timestamps_aligned_to_trace_start(self):
        result = _make_result(200)
        events = _ppk2_to_counter_events(result, trace_start_us=1000000, downsample=100, samples_per_second=100_000)

        # First event at trace start
        assert events[0]["ts"] == 1000000
        # Second event 100 samples later = 1000 µs later
        assert events[1]["ts"] == 1001000

    def test_current_values_preserved(self):
        result = _make_result(100, current_ua=42.5)
        events = _ppk2_to_counter_events(result, trace_start_us=0, downsample=1, samples_per_second=100_000)

        assert events[0]["args"]["value"] == pytest.approx(42.5, abs=0.1)


class TestMergeTracePpk2:
    def test_full_merge(self, tmp_path):
        # Create trace file
        trace_path = tmp_path / "trace.json"
        trace_data = {"traceEvents": _make_trace_events()}
        trace_path.write_text(json.dumps(trace_data))

        # Create PPK2 file
        ppk2_path = tmp_path / "capture.ppk2"
        result = _make_result(1000)
        save_ppk2(result, ppk2_path)

        # Merge
        output = merge_trace_ppk2(trace_path, ppk2_path)

        assert output.exists()
        merged = json.loads(output.read_text())
        events = merged["traceEvents"]

        # Original events + metadata + counter events
        original_count = len(_make_trace_events())
        counter_count = 1000 // 100  # default downsample
        assert len(events) == original_count + 1 + counter_count  # +1 for PPK2 metadata

        # Check counter events present
        counter_events = [e for e in events if e["ph"] == "C" and e["name"] == "current_ua"]
        assert len(counter_events) == counter_count

    def test_custom_output_path(self, tmp_path):
        trace_path = tmp_path / "trace.json"
        trace_path.write_text(json.dumps({"traceEvents": []}))

        ppk2_path = tmp_path / "capture.ppk2"
        save_ppk2(_make_result(100), ppk2_path)

        custom_out = tmp_path / "custom.json"
        output = merge_trace_ppk2(trace_path, ppk2_path, output_path=custom_out)

        assert output == custom_out
        assert output.exists()

    def test_default_output_name(self, tmp_path):
        trace_path = tmp_path / "my_trace.json"
        trace_path.write_text(json.dumps({"traceEvents": []}))

        ppk2_path = tmp_path / "capture.ppk2"
        save_ppk2(_make_result(100), ppk2_path)

        output = merge_trace_ppk2(trace_path, ppk2_path)
        assert output.name == "my_trace_merged.json"

    def test_bare_array_trace(self, tmp_path):
        """Trace file as bare array (no wrapping object)."""
        trace_path = tmp_path / "trace.json"
        trace_path.write_text(json.dumps(_make_trace_events()))

        ppk2_path = tmp_path / "capture.ppk2"
        save_ppk2(_make_result(100), ppk2_path)

        output = merge_trace_ppk2(trace_path, ppk2_path)
        merged = json.loads(output.read_text())
        assert "traceEvents" in merged

    def test_custom_downsample(self, tmp_path):
        trace_path = tmp_path / "trace.json"
        trace_path.write_text(json.dumps({"traceEvents": _make_trace_events()}))

        ppk2_path = tmp_path / "capture.ppk2"
        save_ppk2(_make_result(1000), ppk2_path)

        output = merge_trace_ppk2(trace_path, ppk2_path, downsample=50)
        merged = json.loads(output.read_text())
        counter_events = [e for e in merged["traceEvents"] if e["ph"] == "C" and e["name"] == "current_ua"]
        assert len(counter_events) == 1000 // 50
