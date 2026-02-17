"""Synthetic power profile generation.

Build realistic-looking power profiles from a description of phases,
without needing real PPK2 hardware. Useful for testing, documentation,
and generating reference profiles.

Usage:
    from ppk2.synthetic import ProfileBuilder

    profile = (
        ProfileBuilder()
        .phase("deep_sleep", current_ua=3.5, duration_s=5.0, noise_ua=0.5)
        .ramp("wakeup", start_ua=3.5, end_ua=15_000, duration_s=0.05)
        .phase("radio_tx", current_ua=45_000, duration_s=0.2, noise_ua=2000)
        .phase("idle", current_ua=800, duration_s=1.0, noise_ua=50)
        .spike(current_ua=60_000, duration_ms=2)
        .ramp("shutdown", start_ua=800, end_ua=3.5, duration_s=0.01)
        .phase("deep_sleep", current_ua=3.5, duration_s=5.0, noise_ua=0.5)
        .build()
    )
"""

import math
import random
from dataclasses import dataclass, field

from .types import MeasurementResult, Sample

SAMPLES_PER_SECOND = 100_000


@dataclass
class _Phase:
    """Internal representation of a profile phase."""
    name: str
    samples: list[float]  # current values in uA
    digital: int = 0  # digital channel state during this phase


class ProfileBuilder:
    """Fluent builder for synthetic power profiles.

    All current values are in microamps (uA). Duration in seconds.
    """

    def __init__(self, samples_per_second: int = SAMPLES_PER_SECOND, seed: int | None = None):
        self._sps = samples_per_second
        self._phases: list[_Phase] = []
        self._rng = random.Random(seed)
        self._digital: int = 0  # current digital channel state

    def digital(self, channels: int) -> "ProfileBuilder":
        """Set digital channel state for subsequent phases.

        Args:
            channels: 8-bit bitmask (e.g., 0x01 for D0 high, 0x05 for D0+D2).
        """
        self._digital = channels & 0xFF
        return self

    def phase(
        self,
        name: str,
        current_ua: float,
        duration_s: float,
        noise_ua: float = 0.0,
        noise_type: str = "gaussian",
    ) -> "ProfileBuilder":
        """Add a constant-current phase with optional noise.

        Args:
            name: Phase label (for annotation).
            current_ua: Mean current in microamps.
            duration_s: Phase duration in seconds.
            noise_ua: Noise amplitude (std dev for gaussian, max deviation for uniform).
            noise_type: "gaussian" or "uniform".
        """
        n = int(duration_s * self._sps)
        samples = []
        for _ in range(n):
            noise = self._noise(noise_ua, noise_type)
            samples.append(max(0.0, current_ua + noise))
        self._phases.append(_Phase(name=name, samples=samples, digital=self._digital))
        return self

    def ramp(
        self,
        name: str,
        start_ua: float,
        end_ua: float,
        duration_s: float,
        noise_ua: float = 0.0,
    ) -> "ProfileBuilder":
        """Add a linear ramp between two current levels.

        Args:
            name: Phase label.
            start_ua: Starting current in uA.
            end_ua: Ending current in uA.
            duration_s: Ramp duration in seconds.
            noise_ua: Noise amplitude (gaussian std dev).
        """
        n = max(int(duration_s * self._sps), 2)
        samples = []
        for i in range(n):
            t = i / (n - 1)
            current = start_ua + (end_ua - start_ua) * t
            noise = self._noise(noise_ua, "gaussian")
            samples.append(max(0.0, current + noise))
        self._phases.append(_Phase(name=name, samples=samples, digital=self._digital))
        return self

    def spike(
        self,
        current_ua: float,
        duration_ms: float = 1.0,
        name: str = "spike",
    ) -> "ProfileBuilder":
        """Add a current spike.

        Args:
            current_ua: Peak current of the spike.
            duration_ms: Spike duration in milliseconds.
            name: Phase label.
        """
        n = max(int(duration_ms * self._sps / 1000), 1)
        # Bell curve shape
        samples = []
        for i in range(n):
            t = i / max(n - 1, 1)
            # Gaussian envelope centered at 0.5
            envelope = math.exp(-((t - 0.5) ** 2) / 0.04)
            samples.append(current_ua * envelope)
        self._phases.append(_Phase(name=name, samples=samples, digital=self._digital))
        return self

    def periodic_wake(
        self,
        sleep_ua: float,
        wake_ua: float,
        sleep_s: float,
        wake_s: float,
        cycles: int,
        sleep_noise_ua: float = 0.0,
        wake_noise_ua: float = 0.0,
    ) -> "ProfileBuilder":
        """Add repeating sleep/wake cycles.

        Args:
            sleep_ua: Current during sleep.
            wake_ua: Current during wake.
            sleep_s: Sleep duration per cycle.
            wake_s: Wake duration per cycle.
            cycles: Number of cycles.
            sleep_noise_ua: Noise during sleep.
            wake_noise_ua: Noise during wake.
        """
        for i in range(cycles):
            self.phase(f"sleep_{i}", sleep_ua, sleep_s, sleep_noise_ua)
            self.phase(f"wake_{i}", wake_ua, wake_s, wake_noise_ua)
        return self

    def build(self) -> MeasurementResult:
        """Build the profile into a MeasurementResult."""
        all_samples: list[Sample] = []
        counter = 0

        for phase in self._phases:
            for current in phase.samples:
                all_samples.append(
                    Sample(
                        current_ua=current,
                        range=_estimate_range(current),
                        logic=phase.digital,
                        counter=counter & 0x3F,
                    )
                )
                counter += 1

        duration_s = len(all_samples) / self._sps
        return MeasurementResult(
            samples=all_samples,
            duration_s=duration_s,
            sample_count=len(all_samples),
            lost_samples=0,
        )

    def _noise(self, amplitude: float, noise_type: str) -> float:
        if amplitude <= 0:
            return 0.0
        if noise_type == "uniform":
            return self._rng.uniform(-amplitude, amplitude)
        return self._rng.gauss(0, amplitude)


def _estimate_range(current_ua: float) -> int:
    """Estimate which PPK2 measurement range would be active."""
    if current_ua < 5:
        return 0
    if current_ua < 50:
        return 1
    if current_ua < 500:
        return 2
    if current_ua < 5000:
        return 3
    return 4
