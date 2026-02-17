"""Types for PPK2 measurements and calibration."""

from dataclasses import dataclass, field


@dataclass
class Modifiers:
    """Per-device calibration modifiers. 5 ranges x 7 modifier types."""

    r: list[float] = field(
        default_factory=lambda: [1031.64, 101.65, 10.15, 0.94, 0.043]
    )
    gs: list[float] = field(default_factory=lambda: [1.0, 1.0, 1.0, 1.0, 1.0])
    gi: list[float] = field(default_factory=lambda: [1.0, 1.0, 1.0, 1.0, 1.0])
    o: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0, 0.0])
    s: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0, 0.0])
    i: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0, 0.0])
    ug: list[float] = field(default_factory=lambda: [1.0, 1.0, 1.0, 1.0, 1.0])

    def update_from_metadata(self, meta: dict) -> None:
        """Update modifiers from parsed metadata dict (lowercase keys)."""
        for key in ("r", "gs", "gi", "o", "s", "i", "ug"):
            arr = getattr(self, key)
            for idx in range(5):
                val = meta.get(f"{key}{idx}")
                if val is not None:
                    arr[idx] = float(val)


@dataclass
class Sample:
    """A single PPK2 measurement sample."""

    current_ua: float
    range: int
    logic: int  # D0-D7 as 8-bit bitmask
    counter: int


@dataclass
class MeasurementResult:
    """Statistics from a measurement window."""

    samples: list[Sample]
    duration_s: float = 0.0
    sample_count: int = 0
    lost_samples: int = 0

    @property
    def mean_ua(self) -> float:
        values = [s.current_ua for s in self.samples if s.current_ua is not None]
        return sum(values) / len(values) if values else 0.0

    @property
    def min_ua(self) -> float:
        values = [s.current_ua for s in self.samples if s.current_ua is not None]
        return min(values) if values else 0.0

    @property
    def max_ua(self) -> float:
        values = [s.current_ua for s in self.samples if s.current_ua is not None]
        return max(values) if values else 0.0

    @property
    def peak_ma(self) -> float:
        return self.max_ua / 1000.0

    @property
    def p99_ua(self) -> float:
        values = sorted(
            s.current_ua for s in self.samples if s.current_ua is not None
        )
        if not values:
            return 0.0
        idx = int(len(values) * 0.99)
        return values[min(idx, len(values) - 1)]
