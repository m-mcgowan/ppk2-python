"""Tests for synthetic power profile generation."""

from ppk2.synthetic import ProfileBuilder, _estimate_range


class TestProfileBuilder:
    def test_single_phase(self):
        result = ProfileBuilder(seed=42).phase("sleep", 5.0, 0.01).build()
        assert result.sample_count == 1000  # 0.01s * 100kHz
        assert abs(result.mean_ua - 5.0) < 1.0

    def test_phase_with_noise(self):
        result = ProfileBuilder(seed=42).phase("sleep", 5.0, 0.1, noise_ua=1.0).build()
        # Mean should be close to 5.0 despite noise
        assert abs(result.mean_ua - 5.0) < 0.5
        # Should have some variation
        assert result.max_ua > result.min_ua

    def test_ramp(self):
        result = ProfileBuilder(seed=42).ramp("wakeup", 5.0, 50000.0, 0.01).build()
        assert result.samples[0].current_ua < 100
        assert result.samples[-1].current_ua > 49000

    def test_spike(self):
        result = (
            ProfileBuilder(seed=42)
            .phase("idle", 100.0, 0.01)
            .spike(50000.0, duration_ms=1.0)
            .phase("idle", 100.0, 0.01)
            .build()
        )
        assert result.max_ua > 40000  # spike peak
        assert result.mean_ua < 5000  # mostly idle

    def test_periodic_wake(self):
        result = (
            ProfileBuilder(seed=42)
            .periodic_wake(sleep_ua=5.0, wake_ua=10000.0, sleep_s=0.01, wake_s=0.001, cycles=3)
            .build()
        )
        # 3 cycles * (1000 + 100 samples) = 3300
        assert result.sample_count == 3300

    def test_digital_channels(self):
        result = (
            ProfileBuilder(seed=42)
            .phase("off", 5.0, 0.001)
            .digital(0x01)
            .phase("radio_on", 10000.0, 0.001)
            .digital(0x00)
            .phase("off", 5.0, 0.001)
            .build()
        )
        # First phase: no digital
        assert result.samples[0].logic == 0
        # Middle phase: D0 high
        assert result.samples[150].logic == 0x01
        # Last phase: back to 0
        assert result.samples[-1].logic == 0

    def test_seed_reproducibility(self):
        r1 = ProfileBuilder(seed=123).phase("test", 100.0, 0.01, noise_ua=10.0).build()
        r2 = ProfileBuilder(seed=123).phase("test", 100.0, 0.01, noise_ua=10.0).build()
        assert r1.samples[0].current_ua == r2.samples[0].current_ua
        assert r1.samples[-1].current_ua == r2.samples[-1].current_ua

    def test_different_seeds_differ(self):
        r1 = ProfileBuilder(seed=1).phase("test", 100.0, 0.01, noise_ua=10.0).build()
        r2 = ProfileBuilder(seed=2).phase("test", 100.0, 0.01, noise_ua=10.0).build()
        assert r1.samples[0].current_ua != r2.samples[0].current_ua

    def test_uniform_noise(self):
        result = (
            ProfileBuilder(seed=42)
            .phase("test", 100.0, 0.1, noise_ua=10.0, noise_type="uniform")
            .build()
        )
        # All samples should be within [90, 110]
        for s in result.samples:
            assert 90.0 <= s.current_ua <= 110.0

    def test_no_negative_current(self):
        # Very high noise relative to current — should clamp to 0
        result = ProfileBuilder(seed=42).phase("test", 1.0, 0.01, noise_ua=100.0).build()
        for s in result.samples:
            assert s.current_ua >= 0.0

    def test_duration_correct(self):
        result = ProfileBuilder().phase("a", 10.0, 1.0).phase("b", 20.0, 2.0).build()
        assert abs(result.duration_s - 3.0) < 0.001

    def test_empty_profile(self):
        result = ProfileBuilder().build()
        assert result.sample_count == 0
        assert result.duration_s == 0.0

    def test_chaining(self):
        """Verify fluent API returns self."""
        b = ProfileBuilder(seed=42)
        assert b.phase("a", 1.0, 0.001) is b
        assert b.ramp("b", 1.0, 10.0, 0.001) is b
        assert b.spike(100.0) is b
        assert b.digital(0x01) is b


class TestEstimateRange:
    def test_very_low_current(self):
        assert _estimate_range(1.0) == 0

    def test_low_current(self):
        assert _estimate_range(20.0) == 1

    def test_medium_current(self):
        assert _estimate_range(200.0) == 2

    def test_high_current(self):
        assert _estimate_range(2000.0) == 3

    def test_very_high_current(self):
        assert _estimate_range(50000.0) == 4
