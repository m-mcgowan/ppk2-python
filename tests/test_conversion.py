"""Tests for ADC-to-current conversion and spike filter."""

from ppk2.conversion import SpikeFilter, adc_to_microamps
from ppk2.types import Modifiers


class TestAdcConversion:
    def test_zero_adc_default_modifiers(self):
        mods = Modifiers()
        result = adc_to_microamps(0, 0, mods, 3700)
        # With default modifiers (gs=1, gi=1, o=0, s=0, i=0, ug=1):
        # no_gain = 0 * (ADC_MULT / r0) = 0
        # adc = 1 * (0 * (1 * 0 + 1) + (0 * 3.7 + 0)) = 0
        assert result == 0.0

    def test_range0_low_current(self):
        """Range 0 (1031.64 kΩ) — verify conversion produces reasonable value.

        With default modifiers (gi=1.0), the quadratic gain model adds a
        constant term, so the result is larger than a simple V/R calculation.
        Real devices have device-specific calibration that corrects this.
        """
        mods = Modifiers()
        result = adc_to_microamps(8000, 0, mods, 3700)
        assert result > 0

    def test_range4_high_current(self):
        """Range 4 (0.043 kΩ) should measure mA range."""
        mods = Modifiers()
        result = adc_to_microamps(8000, 4, mods, 3700)
        # Should be much larger than range 0
        range0 = adc_to_microamps(8000, 0, mods, 3700)
        assert result > range0 * 100

    def test_user_gain_scales_result(self):
        mods = Modifiers()
        baseline = adc_to_microamps(4000, 2, mods, 3700)

        mods_scaled = Modifiers()
        mods_scaled.ug[2] = 2.0
        scaled = adc_to_microamps(4000, 2, mods_scaled, 3700)

        assert abs(scaled - 2 * baseline) < 1e-6

    def test_offset_shifts_result(self):
        mods = Modifiers()
        baseline = adc_to_microamps(4000, 2, mods, 3700)

        mods_offset = Modifiers()
        mods_offset.o[2] = 100.0
        offset_result = adc_to_microamps(4000, 2, mods_offset, 3700)

        # Higher offset reduces the effective ADC value
        assert offset_result < baseline


class TestSpikeFilter:
    def test_stable_range_passthrough(self):
        """Constant range should pass values through with minimal change."""
        sf = SpikeFilter()
        values = [100.0] * 20
        filtered = [sf.process(v, 2) for v in values]
        # After EMA converges, output should be close to input
        assert abs(filtered[-1] - 100.0) < 1.0

    def test_range_switch_smoothing(self):
        """Range switch should trigger smoothing."""
        sf = SpikeFilter()
        # Establish baseline at range 2
        for _ in range(10):
            sf.process(100.0, 2)

        # Switch to range 3 with a spike
        result = sf.process(500.0, 3)
        # Should be smoothed (closer to rolling avg than raw value)
        assert result < 500.0

    def test_reset_clears_state(self):
        sf = SpikeFilter()
        for _ in range(10):
            sf.process(100.0, 2)

        sf.reset()
        assert sf.rolling_avg is None
        assert sf.prev_range is None
