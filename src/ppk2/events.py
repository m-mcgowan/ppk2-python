"""Event annotation for power profiles.

Maps Chrome JSON trace events from DUT firmware to digital channels D0-D7
in .ppk2 files. The host captures Chrome JSON scope events from the DUT
serial output and overlays them onto the measurement samples as synthetic
digital channels.

Usage:
    mapper = EventMapper({
        "gps": 0,          # D0
        "lte_tx": 1,       # D1
        "sensor": 2,       # D2
    })

    # During measurement, capture events from DUT serial
    mapper.event("gps", True, timestamp_s=0.5)    # gps scope began at 0.5s
    mapper.event("gps", False, timestamp_s=2.0)   # gps scope ended at 2.0s
    mapper.event("lte_tx", True, timestamp_s=1.8)

    # After measurement, apply to the sample data
    mapper.apply(result)

    # Save with legend
    save_ppk2(result, "test.ppk2")
    mapper.save_legend("test.ppk2.legend.json")
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from .types import MeasurementResult

SAMPLES_PER_SECOND = 100_000


@dataclass
class _Event:
    """A timestamped event transition."""
    channel_name: str
    channel_bit: int
    high: bool
    timestamp_s: float


@dataclass
class EventMapper:
    """Maps named events to digital channels and applies them to measurements.

    Args:
        channel_map: Mapping of event name to D0-D7 channel number (0-7).
    """
    channel_map: dict[str, int]
    _events: list[_Event] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        for name, ch in self.channel_map.items():
            if not 0 <= ch <= 7:
                raise ValueError(f"Channel must be 0-7, got {ch} for '{name}'")

    def event(self, name: str, high: bool, timestamp_s: float) -> None:
        """Record an event transition.

        Args:
            name: Event name (must be in channel_map).
            high: True for start/high, False for stop/low.
            timestamp_s: Time since measurement start in seconds.
        """
        if name not in self.channel_map:
            raise ValueError(
                f"Unknown event '{name}'. Known: {list(self.channel_map.keys())}"
            )
        self._events.append(_Event(
            channel_name=name,
            channel_bit=self.channel_map[name],
            high=high,
            timestamp_s=timestamp_s,
        ))

    def start(self, name: str, timestamp_s: float) -> None:
        """Shorthand for event(name, True, timestamp_s)."""
        self.event(name, True, timestamp_s)

    def stop(self, name: str, timestamp_s: float) -> None:
        """Shorthand for event(name, False, timestamp_s)."""
        self.event(name, False, timestamp_s)

    def apply(
        self,
        result: MeasurementResult,
        samples_per_second: int = SAMPLES_PER_SECOND,
    ) -> None:
        """Apply recorded events to a MeasurementResult's digital channels.

        Modifies samples in-place, setting the logic bitmask for each sample
        based on which events are active at that point in time.

        Args:
            result: MeasurementResult to annotate.
            samples_per_second: Sampling rate for timestamp-to-index conversion.
        """
        if not self._events:
            return

        # Sort events by timestamp
        sorted_events = sorted(self._events, key=lambda e: e.timestamp_s)

        # Build a timeline of bitmask changes
        # Each entry: (sample_index, bit_to_set, high/low)
        transitions: list[tuple[int, int, bool]] = []
        for ev in sorted_events:
            idx = int(ev.timestamp_s * samples_per_second)
            idx = max(0, min(idx, len(result.samples) - 1))
            transitions.append((idx, ev.channel_bit, ev.high))

        # Apply transitions to samples
        # Start with existing logic state from first sample
        current_mask = result.samples[0].logic if result.samples else 0
        trans_idx = 0

        for i, sample in enumerate(result.samples):
            # Apply all transitions at or before this sample
            while trans_idx < len(transitions) and transitions[trans_idx][0] <= i:
                _, bit, high = transitions[trans_idx]
                if high:
                    current_mask |= (1 << bit)
                else:
                    current_mask &= ~(1 << bit)
                trans_idx += 1

            sample.logic = current_mask

    def legend(self) -> dict:
        """Return the channel legend as a dict.

        Returns:
            {"channels": {"D0": "gps", "D1": "lte_tx", ...}, "events": [...]}
        """
        channels = {}
        for name, ch in self.channel_map.items():
            channels[f"D{ch}"] = name

        events = [
            {
                "name": e.channel_name,
                "channel": f"D{e.channel_bit}",
                "state": "high" if e.high else "low",
                "timestamp_s": e.timestamp_s,
            }
            for e in sorted(self._events, key=lambda e: e.timestamp_s)
        ]

        return {"channels": channels, "events": events}

    def save_legend(self, path: str | Path) -> None:
        """Save the channel legend to a JSON file."""
        Path(path).write_text(json.dumps(self.legend(), indent=2))

    @staticmethod
    def load_legend(path: str | Path) -> dict:
        """Load a channel legend from a JSON file."""
        return json.loads(Path(path).read_text())

    def clear(self) -> None:
        """Clear all recorded events."""
        self._events.clear()


def parse_serial_events(
    serial_output: str,
    channel_map: dict[str, int],
) -> EventMapper:
    """Parse DUT serial output (Chrome JSON trace lines) into an EventMapper.

    Expected format (one event per line, mixed with other output):
        {"ph":"B","ts":500000,"name":"gps","pid":1,"tid":1}
        {"ph":"B","ts":1800000,"name":"lte_tx","pid":1,"tid":1}
        {"ph":"E","ts":2000000,"name":"gps","pid":1,"tid":1}

    The event name comes from the ``name`` field in the JSON. Only events
    whose name appears in ``channel_map`` are recorded.

    Args:
        serial_output: Raw serial text from DUT.
        channel_map: Mapping of event name to D0-D7 channel number.

    Returns:
        EventMapper with parsed events.
    """
    mapper = EventMapper(channel_map)

    for line in serial_output.strip().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue

        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        ph = obj.get("ph")
        name = obj.get("name", "")
        ts_us = obj.get("ts", 0)

        if name not in channel_map:
            continue

        if ph == "B":
            mapper.start(name, ts_us / 1_000_000)
        elif ph == "E":
            mapper.stop(name, ts_us / 1_000_000)

    return mapper
