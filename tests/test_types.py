"""Tests for PPK2 types."""

from ppk2.types import MeasurementResult, Modifiers, Sample


class TestModifiers:
    def test_defaults(self):
        m = Modifiers()
        assert m.r[0] == 1031.64
        assert m.r[4] == 0.043
        assert all(g == 1.0 for g in m.gs)
        assert all(o == 0.0 for o in m.o)

    def test_update_from_metadata(self):
        m = Modifiers()
        meta = {"r0": 1030.0, "gs2": 1.05, "ug4": 0.98}
        m.update_from_metadata(meta)
        assert m.r[0] == 1030.0
        assert m.gs[2] == 1.05
        assert m.ug[4] == 0.98
        # Unchanged values should keep defaults
        assert m.r[1] == 101.65

    def test_update_ignores_missing_keys(self):
        m = Modifiers()
        m.update_from_metadata({})
        assert m.r[0] == 1031.64


class TestMeasurementResult:
    def _make_result(self, values: list[float]) -> MeasurementResult:
        samples = [
            Sample(current_ua=v, range=2, logic=0, counter=i % 64)
            for i, v in enumerate(values)
        ]
        return MeasurementResult(
            samples=samples,
            duration_s=len(values) / 100_000,
            sample_count=len(values),
            lost_samples=0,
        )

    def test_mean(self):
        result = self._make_result([10.0, 20.0, 30.0])
        assert result.mean_ua == 20.0

    def test_min_max(self):
        result = self._make_result([5.0, 15.0, 25.0])
        assert result.min_ua == 5.0
        assert result.max_ua == 25.0

    def test_peak_ma(self):
        result = self._make_result([1000.0, 2000.0])
        assert result.peak_ma == 2.0

    def test_p99(self):
        values = list(range(100))
        result = self._make_result([float(v) for v in values])
        assert result.p99_ua == 99.0

    def test_empty(self):
        result = self._make_result([])
        assert result.mean_ua == 0.0
        assert result.min_ua == 0.0
        assert result.max_ua == 0.0
