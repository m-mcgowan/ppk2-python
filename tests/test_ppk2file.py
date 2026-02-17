"""Tests for .ppk2 file save/load (nRF Connect format)."""

import json
import struct
import zipfile

from ppk2.ppk2file import FRAME_SIZE, load_ppk2, save_ppk2
from ppk2.types import MeasurementResult, Sample


def _make_samples(n: int, base_ua: float = 100.0, logic: int = 0) -> list[Sample]:
    return [
        Sample(current_ua=base_ua + i * 0.1, range=2, logic=logic, counter=i & 0x3F)
        for i in range(n)
    ]


class TestSavePpk2:
    def test_creates_valid_zip(self, tmp_path):
        samples = _make_samples(100)
        result = MeasurementResult(samples=samples, duration_s=0.001, sample_count=100)
        path = tmp_path / "test.ppk2"
        save_ppk2(result, path)

        assert path.exists()
        with zipfile.ZipFile(path) as zf:
            assert "session.raw" in zf.namelist()
            assert "metadata.json" in zf.namelist()
            assert "minimap.raw" in zf.namelist()

    def test_session_raw_format(self, tmp_path):
        samples = [Sample(current_ua=42.5, range=0, logic=0x85, counter=0)]
        result = MeasurementResult(samples=samples, duration_s=0.00001, sample_count=1)
        path = tmp_path / "test.ppk2"
        save_ppk2(result, path)

        with zipfile.ZipFile(path) as zf:
            raw = zf.read("session.raw")
        assert len(raw) == FRAME_SIZE
        current, logic = struct.unpack("<fH", raw)
        assert abs(current - 42.5) < 0.01
        assert logic == 0x85

    def test_metadata_json(self, tmp_path):
        samples = _make_samples(10)
        result = MeasurementResult(samples=samples)
        path = tmp_path / "test.ppk2"
        save_ppk2(result, path, start_time_ms=1700000000000, samples_per_second=100_000)

        with zipfile.ZipFile(path) as zf:
            meta = json.loads(zf.read("metadata.json"))
        assert meta["formatVersion"] == 2
        assert meta["metadata"]["samplesPerSecond"] == 100_000
        assert meta["metadata"]["startSystemTime"] == 1700000000000

    def test_custom_sample_rate(self, tmp_path):
        samples = _make_samples(10)
        result = MeasurementResult(samples=samples)
        path = tmp_path / "test.ppk2"
        save_ppk2(result, path, samples_per_second=10_000)

        with zipfile.ZipFile(path) as zf:
            meta = json.loads(zf.read("metadata.json"))
        assert meta["metadata"]["samplesPerSecond"] == 10_000


class TestLoadPpk2:
    def test_round_trip(self, tmp_path):
        samples = _make_samples(500, base_ua=50.0, logic=0x03)
        result = MeasurementResult(
            samples=samples, duration_s=0.005, sample_count=500, lost_samples=2
        )
        path = tmp_path / "test.ppk2"
        save_ppk2(result, path)

        loaded = load_ppk2(path)
        assert loaded.sample_count == 500
        assert len(loaded.samples) == 500
        # lost_samples not preserved in .ppk2 format
        assert loaded.lost_samples == 0

    def test_current_values_preserved(self, tmp_path):
        samples = [
            Sample(current_ua=0.001, range=0, logic=0, counter=0),
            Sample(current_ua=1000.5, range=4, logic=0, counter=1),
            Sample(current_ua=50000.0, range=4, logic=0, counter=2),
        ]
        result = MeasurementResult(samples=samples, sample_count=3)
        path = tmp_path / "test.ppk2"
        save_ppk2(result, path)

        loaded = load_ppk2(path)
        for orig, loaded_s in zip(samples, loaded.samples):
            assert abs(orig.current_ua - loaded_s.current_ua) < 0.01

    def test_digital_channels_preserved(self, tmp_path):
        samples = [
            Sample(current_ua=10.0, range=0, logic=0x00, counter=0),
            Sample(current_ua=10.0, range=0, logic=0xFF, counter=1),
            Sample(current_ua=10.0, range=0, logic=0x55, counter=2),
        ]
        result = MeasurementResult(samples=samples, sample_count=3)
        path = tmp_path / "test.ppk2"
        save_ppk2(result, path)

        loaded = load_ppk2(path)
        assert loaded.samples[0].logic == 0x00
        assert loaded.samples[1].logic == 0xFF
        assert loaded.samples[2].logic == 0x55

    def test_duration_computed_from_sample_count(self, tmp_path):
        samples = _make_samples(100_000)  # 1 second at 100kHz
        result = MeasurementResult(samples=samples, sample_count=100_000)
        path = tmp_path / "test.ppk2"
        save_ppk2(result, path, samples_per_second=100_000)

        loaded = load_ppk2(path)
        assert abs(loaded.duration_s - 1.0) < 0.001

    def test_range_not_stored(self, tmp_path):
        """Range info is not part of the .ppk2 format — defaults to 0."""
        samples = [Sample(current_ua=10.0, range=3, logic=0, counter=0)]
        result = MeasurementResult(samples=samples, sample_count=1)
        path = tmp_path / "test.ppk2"
        save_ppk2(result, path)

        loaded = load_ppk2(path)
        assert loaded.samples[0].range == 0  # range not preserved

    def test_statistics_match(self, tmp_path):
        samples = _make_samples(1000, base_ua=100.0)
        result = MeasurementResult(samples=samples, sample_count=1000)
        path = tmp_path / "test.ppk2"
        save_ppk2(result, path)

        loaded = load_ppk2(path)
        assert abs(loaded.mean_ua - result.mean_ua) < 0.1
        assert abs(loaded.max_ua - result.max_ua) < 0.1
        assert abs(loaded.min_ua - result.min_ua) < 0.1


class TestMinimap:
    def test_small_dataset_no_folding(self, tmp_path):
        samples = _make_samples(100)
        result = MeasurementResult(samples=samples, sample_count=100)
        path = tmp_path / "test.ppk2"
        save_ppk2(result, path)

        with zipfile.ZipFile(path) as zf:
            minimap = json.loads(zf.read("minimap.raw"))
        assert minimap["numberOfTimesToFold"] == 0
        assert minimap["data"]["length"] == 100

    def test_large_dataset_folds(self, tmp_path):
        samples = _make_samples(50_000)
        result = MeasurementResult(samples=samples, sample_count=50_000)
        path = tmp_path / "test.ppk2"
        save_ppk2(result, path)

        with zipfile.ZipFile(path) as zf:
            minimap = json.loads(zf.read("minimap.raw"))
        assert minimap["numberOfTimesToFold"] > 0
        assert minimap["data"]["length"] <= 10_000

    def test_minimap_values_in_nanoamps(self, tmp_path):
        samples = [Sample(current_ua=42.0, range=0, logic=0, counter=0)]
        result = MeasurementResult(samples=samples, sample_count=1)
        path = tmp_path / "test.ppk2"
        save_ppk2(result, path)

        with zipfile.ZipFile(path) as zf:
            minimap = json.loads(zf.read("minimap.raw"))
        # 42 uA = 42000 nA
        assert minimap["data"]["min"][0]["y"] == 42000.0

    def test_empty_samples(self, tmp_path):
        result = MeasurementResult(samples=[], sample_count=0)
        path = tmp_path / "test.ppk2"
        save_ppk2(result, path)

        with zipfile.ZipFile(path) as zf:
            minimap = json.loads(zf.read("minimap.raw"))
        assert minimap["data"]["length"] == 0
