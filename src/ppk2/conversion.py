"""ADC-to-current conversion and spike filtering.

Protocol reference:
  https://docs.nordicsemi.com/bundle/ug_ppk2/page/UG/ppk/PPK_user_guide_Intro.html
"""

from .types import Modifiers

# Vref / (2^14 * 4 * 2.5)
ADC_MULT = 1.8 / 163840


def adc_to_microamps(
    adc_raw: int, range_idx: int, modifiers: Modifiers, vdd_mv: int
) -> float:
    """Convert raw ADC value to current in microamps.

    Args:
        adc_raw: 14-bit ADC value, already multiplied by 4.
        range_idx: Measurement range (0-4).
        modifiers: Per-device calibration modifiers.
        vdd_mv: Current source voltage in millivolts.
    """
    r = modifiers.r[range_idx]
    gs = modifiers.gs[range_idx]
    gi = modifiers.gi[range_idx]
    o = modifiers.o[range_idx]
    s = modifiers.s[range_idx]
    i = modifiers.i[range_idx]
    ug = modifiers.ug[range_idx]

    no_gain = (adc_raw - o) * (ADC_MULT / r)
    adc = ug * (no_gain * (gs * no_gain + gi) + (s * (vdd_mv / 1000) + i))
    return adc * 1e6


class SpikeFilter:
    """Smooth range-switching transients using exponential moving average."""

    def __init__(
        self, alpha: float = 0.18, alpha5: float = 0.06, samples: int = 3
    ):
        self.alpha = alpha
        self.alpha5 = alpha5
        self.filter_samples = samples

        self.rolling_avg: float | None = None
        self.rolling_avg4: float | None = None
        self.prev_range: int | None = None
        self.after_spike = 0
        self.consecutive_range_sample = 0

    def reset(self) -> None:
        self.rolling_avg = None
        self.rolling_avg4 = None
        self.prev_range = None
        self.after_spike = 0
        self.consecutive_range_sample = 0

    def process(self, value: float, range_idx: int) -> float:
        """Apply spike filter to a converted current value.

        Call once per sample, in order. Returns the filtered value.
        """
        prev_avg = self.rolling_avg
        prev_avg4 = self.rolling_avg4

        if self.rolling_avg is None:
            self.rolling_avg = value
            self.rolling_avg4 = value
        else:
            self.rolling_avg = (
                self.alpha * value + (1 - self.alpha) * self.rolling_avg
            )
            self.rolling_avg4 = (
                self.alpha5 * value + (1 - self.alpha5) * self.rolling_avg4
            )

        if self.prev_range is None:
            self.prev_range = range_idx

        if self.prev_range != range_idx or self.after_spike > 0:
            if self.prev_range != range_idx:
                self.consecutive_range_sample = 0
                self.after_spike = self.filter_samples
            else:
                self.consecutive_range_sample += 1

            if range_idx == 4:
                if self.consecutive_range_sample < 2:
                    self.rolling_avg4 = prev_avg4
                    self.rolling_avg = prev_avg
                value = self.rolling_avg4  # type: ignore[assignment]
            else:
                value = self.rolling_avg  # type: ignore[assignment]

            self.after_spike -= 1

        self.prev_range = range_idx
        return value
