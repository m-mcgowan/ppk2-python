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
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .types import MeasurementResult, SAMPLES_PER_SECOND, Scope

logger = logging.getLogger(__name__)


@dataclass
class _Event:
    """A timestamped event transition."""
    channel_name: str
    channel_bit: int | None
    high: bool
    timestamp_s: float


@dataclass
class EventMapper:
    """Maps named events to digital channels and applies them to measurements.

    Args:
        channel_map: Mapping of event name to D0-D7 channel number (0-7),
            or None for software-only scopes that should not drive a
            sample.logic bit but should still be recorded.
    """
    channel_map: dict[str, int | None]
    _events: list[_Event] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        for name, ch in self.channel_map.items():
            if ch is not None and not 0 <= ch <= 7:
                raise ValueError(f"Channel must be 0-7 or None, got {ch} for '{name}'")

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

        # Build a timeline of bitmask changes. Events whose timestamp falls
        # outside the capture window are clamped to the first or last sample;
        # this silently produces plausible-looking-but-wrong legends when the
        # caller forgets to align device-time to capture-time, so emit a
        # warning the first time it happens (and count the rest).
        capture_duration_s = len(result.samples) / samples_per_second
        transitions: list[tuple[int, int, bool]] = []
        n_out_of_range = 0
        first_out_of_range: _Event | None = None
        for ev in sorted_events:
            idx = int(ev.timestamp_s * samples_per_second)
            if idx < 0 or idx >= len(result.samples):
                if first_out_of_range is None:
                    first_out_of_range = ev
                n_out_of_range += 1
            idx = max(0, min(idx, len(result.samples) - 1))
            transitions.append((idx, ev.channel_bit, ev.high))

        if first_out_of_range is not None:
            logger.warning(
                "%d event(s) outside capture window [0, %.3fs]; first was "
                "'%s' at ts=%.3fs — likely a time-alignment issue (device "
                "boot-time vs capture-start-time). Events have been clamped "
                "to the nearest sample.",
                n_out_of_range,
                capture_duration_s,
                first_out_of_range.channel_name,
                first_out_of_range.timestamp_s,
            )

        # Apply transitions to samples
        # Start with existing logic state from first sample
        current_mask = result.samples[0].logic if result.samples else 0
        trans_idx = 0

        for i, sample in enumerate(result.samples):
            # Apply all transitions at or before this sample
            while trans_idx < len(transitions) and transitions[trans_idx][0] <= i:
                _, bit, high = transitions[trans_idx]
                if bit is not None:
                    if high:
                        current_mask |= (1 << bit)
                    else:
                        current_mask &= ~(1 << bit)
                trans_idx += 1

            sample.logic = current_mask

    def to_scopes(self, result: MeasurementResult) -> list[Scope]:
        """Pair B/E transitions into Scope intervals.

        Unterminated scopes (a start with no matching stop) are clamped to
        ``result.duration_s`` and logged as a warning.
        """
        # Walk events in arrival order, pairing each "high" transition with
        # the next "low" transition for the same name. Multiple intervals
        # for the same name produce multiple Scopes.
        sorted_events = sorted(self._events, key=lambda e: e.timestamp_s)
        open_starts: dict[str, _Event] = {}
        scopes: list[Scope] = []

        for ev in sorted_events:
            if ev.high:
                # Treat back-to-back "high" without a "low" as nesting we
                # don't model — close the previous one at this timestamp.
                prev = open_starts.get(ev.channel_name)
                if prev is not None:
                    scopes.append(Scope(
                        name=prev.channel_name,
                        start_s=prev.timestamp_s,
                        end_s=ev.timestamp_s,
                        channel=prev.channel_bit,
                    ))
                open_starts[ev.channel_name] = ev
            else:
                start = open_starts.pop(ev.channel_name, None)
                if start is None:
                    # E without a B — ignore; legend.events still records it.
                    continue
                scopes.append(Scope(
                    name=start.channel_name,
                    start_s=start.timestamp_s,
                    end_s=ev.timestamp_s,
                    channel=start.channel_bit,
                ))

        if open_starts:
            names = sorted(open_starts.keys())
            logger.warning(
                "to_scopes: %d unterminated scope(s) clamped to capture "
                "duration %.3fs: %s",
                len(open_starts), result.duration_s, ", ".join(names),
            )
            for start in open_starts.values():
                scopes.append(Scope(
                    name=start.channel_name,
                    start_s=start.timestamp_s,
                    end_s=result.duration_s,
                    channel=start.channel_bit,
                ))

        scopes.sort(key=lambda s: s.start_s)
        return scopes

    def legend(self) -> dict:
        """Return the channel legend as a dict.

        Returns:
            {"channels": {"D0": "gps", "D1": "lte_tx", ...}, "events": [...]}
        """
        channels = {}
        for name, ch in self.channel_map.items():
            if ch is not None:
                channels[f"D{ch}"] = name

        events = [
            {
                "name": e.channel_name,
                "channel": f"D{e.channel_bit}" if e.channel_bit is not None else None,
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

    # Wrap detection: embedded-trace emits uint32_t µs timestamps that roll
    # over every 2**32 µs ≈ 71.58 min. Only a *large* backwards step counts
    # as a wrap — on dual-core ESP32 events from different cores can arrive
    # at the Serial line with slight backwards skew. Threshold: half the
    # wrap period (2**31 µs ≈ 35.8 min); real wraps are always close to
    # -2**32, jitter is always a few ms.
    # See embedded-trace/docs/design.md#timestamp-wrap.
    _WRAP_US = 1 << 32
    _WRAP_THRESHOLD = 1 << 31

    last_raw_ts: int | None = None
    wrap_count = 0

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
        raw_ts_us = obj.get("ts", 0)

        # Update wrap state on every valid trace line, mapped or not —
        # wrap is a property of the timestamp stream, not of what we map.
        if ph in ("B", "E"):
            if last_raw_ts is not None and raw_ts_us - last_raw_ts < -_WRAP_THRESHOLD:
                wrap_count += 1
                logger.info(
                    "parse_serial_events: timestamp wrap detected "
                    "(raw %d vs previous %d) — wrap count now %d",
                    raw_ts_us, last_raw_ts, wrap_count,
                )
            last_raw_ts = raw_ts_us

        if name not in channel_map:
            continue

        adjusted_ts_us = raw_ts_us + wrap_count * _WRAP_US
        ts_s = adjusted_ts_us / 1_000_000

        if ph == "B":
            mapper.start(name, ts_s)
        elif ph == "E":
            mapper.stop(name, ts_s)

    return mapper
