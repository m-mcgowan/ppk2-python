"""Tests for event annotation."""

import json

import pytest

from ppk2.events import EventMapper, parse_serial_events
from ppk2.report import ProfileResult, html_report
from ppk2.types import MeasurementResult, Sample


def _make_result(n_samples: int = 1000, sps: int = 100_000) -> MeasurementResult:
    return MeasurementResult(
        samples=[
            Sample(current_ua=10.0, range=0, logic=0, counter=i & 0x3F)
            for i in range(n_samples)
        ],
        duration_s=n_samples / sps,
        sample_count=n_samples,
    )


class TestEventMapper:
    def test_single_event(self):
        mapper = EventMapper({"GPS": 0})
        result = _make_result(1000)

        mapper.start("GPS", 0.002)  # sample 200
        mapper.stop("GPS", 0.005)   # sample 500
        mapper.apply(result)

        assert result.samples[0].logic == 0
        assert result.samples[199].logic == 0
        assert result.samples[200].logic == 1  # D0 high
        assert result.samples[400].logic == 1
        assert result.samples[500].logic == 0  # D0 low
        assert result.samples[999].logic == 0

    def test_multiple_channels(self):
        mapper = EventMapper({"GPS": 0, "LTE": 1, "SENSOR": 2})
        result = _make_result(1000)

        mapper.start("GPS", 0.001)
        mapper.start("LTE", 0.003)
        mapper.stop("GPS", 0.005)
        mapper.stop("LTE", 0.008)
        mapper.apply(result)

        # Before any events
        assert result.samples[0].logic == 0
        # Only GPS
        assert result.samples[150].logic == 0x01
        # GPS + LTE
        assert result.samples[350].logic == 0x03
        # Only LTE
        assert result.samples[550].logic == 0x02
        # Nothing
        assert result.samples[850].logic == 0x00

    def test_overlapping_events(self):
        mapper = EventMapper({"A": 0, "B": 1})
        result = _make_result(500)

        mapper.start("A", 0.001)
        mapper.start("B", 0.002)
        mapper.stop("A", 0.003)
        mapper.stop("B", 0.004)
        mapper.apply(result)

        assert result.samples[150].logic == 0x01  # A only
        assert result.samples[250].logic == 0x03  # A + B
        assert result.samples[350].logic == 0x02  # B only
        assert result.samples[450].logic == 0x00  # neither

    def test_event_at_start(self):
        mapper = EventMapper({"X": 0})
        result = _make_result(100)
        mapper.start("X", 0.0)
        mapper.apply(result)
        assert result.samples[0].logic == 1

    def test_event_past_end_clamped(self):
        mapper = EventMapper({"X": 0})
        result = _make_result(100)
        mapper.start("X", 0.0)
        mapper.stop("X", 999.0)  # way past the end — clamped to last index
        mapper.apply(result)
        # All samples except the last should be high; the stop is
        # clamped to the last sample index so it goes low there
        assert all(s.logic == 1 for s in result.samples[:-1])
        assert result.samples[-1].logic == 0

    def test_unknown_event_raises(self):
        mapper = EventMapper({"GPS": 0})
        with pytest.raises(ValueError, match="Unknown event"):
            mapper.start("BOGUS", 0.0)

    def test_invalid_channel_raises(self):
        with pytest.raises(ValueError, match="Channel must be 0-7"):
            EventMapper({"X": 8})

    def test_legend(self):
        mapper = EventMapper({"GPS": 0, "LTE": 1})
        mapper.start("GPS", 0.5)
        mapper.stop("GPS", 1.0)
        mapper.start("LTE", 0.8)

        legend = mapper.legend()
        assert legend["channels"]["D0"] == "GPS"
        assert legend["channels"]["D1"] == "LTE"
        assert len(legend["events"]) == 3

    def test_save_load_legend(self, tmp_path):
        mapper = EventMapper({"GPS": 0})
        mapper.start("GPS", 1.0)
        mapper.stop("GPS", 2.0)

        path = tmp_path / "legend.json"
        mapper.save_legend(path)

        loaded = EventMapper.load_legend(path)
        assert loaded["channels"]["D0"] == "GPS"
        assert len(loaded["events"]) == 2

    def test_clear(self):
        mapper = EventMapper({"X": 0})
        mapper.start("X", 0.0)
        assert len(mapper._events) == 1
        mapper.clear()
        assert len(mapper._events) == 0

    def test_no_events_is_noop(self):
        mapper = EventMapper({"X": 0})
        result = _make_result(100)
        mapper.apply(result)
        assert all(s.logic == 0 for s in result.samples)

    def test_warns_when_event_past_end_is_clamped(self, caplog):
        """Events after the capture window are silently clamped to the last
        sample today, which produces plausible-looking but wrong legends.
        apply() should at least log a warning so the user notices.
        """
        import logging
        mapper = EventMapper({"GPS": 0})
        result = _make_result(1000)   # 0.01 s window at 100 kHz
        mapper.start("GPS", 100.0)    # way outside the window

        with caplog.at_level(logging.WARNING, logger="ppk2.events"):
            mapper.apply(result)

        assert any(
            "GPS" in r.getMessage() and "100.0" in r.getMessage()
            for r in caplog.records
        ), f"expected a warning about clamped event; got: {[r.getMessage() for r in caplog.records]}"

    def test_warns_when_event_before_start_is_clamped(self, caplog):
        import logging
        mapper = EventMapper({"GPS": 0})
        result = _make_result(1000)
        mapper.start("GPS", -5.0)     # negative → clamps to 0

        with caplog.at_level(logging.WARNING, logger="ppk2.events"):
            mapper.apply(result)

        assert any(
            "GPS" in r.getMessage() and "-5" in r.getMessage()
            for r in caplog.records
        ), f"expected a warning about clamped event; got: {[r.getMessage() for r in caplog.records]}"

    def test_in_range_events_do_not_warn(self, caplog):
        import logging
        mapper = EventMapper({"GPS": 0})
        result = _make_result(1000)
        mapper.start("GPS", 0.002)
        mapper.stop("GPS", 0.005)

        with caplog.at_level(logging.WARNING, logger="ppk2.events"):
            mapper.apply(result)

        assert not caplog.records, (
            f"did not expect any warnings for in-range events; "
            f"got: {[r.getMessage() for r in caplog.records]}"
        )


class TestParseSerialEvents:
    def _b(self, name: str, ts_us: int) -> str:
        return json.dumps({"ph": "B", "ts": ts_us, "name": name, "pid": 1, "tid": 1})

    def _e(self, name: str, ts_us: int) -> str:
        return json.dumps({"ph": "E", "ts": ts_us, "name": name, "pid": 1, "tid": 1})

    def test_basic_parsing(self):
        serial = "\n".join([
            self._b("gps", 500000),
            self._b("lte_tx", 1800000),
            self._e("gps", 2000000),
            self._e("lte_tx", 3500000),
        ])
        mapper = parse_serial_events(serial, {"gps": 0, "lte_tx": 1})
        assert len(mapper._events) == 4

    def test_applies_correctly(self):
        serial = self._b("gps", 1000) + "\n" + self._e("gps", 5000) + "\n"
        mapper = parse_serial_events(serial, {"gps": 0})
        result = _make_result(1000)
        mapper.apply(result)

        assert result.samples[50].logic == 0
        assert result.samples[150].logic == 1
        assert result.samples[550].logic == 0

    def test_ignores_unknown_events(self):
        serial = self._b("unknown", 1000) + "\n" + self._b("gps", 2000) + "\n"
        mapper = parse_serial_events(serial, {"gps": 0})
        assert len(mapper._events) == 1

    def test_ignores_blank_lines(self):
        serial = "\n\n" + self._b("gps", 1000) + "\n\n"
        mapper = parse_serial_events(serial, {"gps": 0})
        assert len(mapper._events) == 1

    def test_ignores_non_json_lines(self):
        serial = "Starting peripheral cycle...\n" + self._b("gps", 1000) + "\n"
        mapper = parse_serial_events(serial, {"gps": 0})
        assert len(mapper._events) == 1

    def test_ignores_counter_events(self):
        serial = '{"ph":"C","ts":1000,"name":"heap","pid":1,"args":{"value":1024}}\n'
        serial += self._b("gps", 2000) + "\n"
        mapper = parse_serial_events(serial, {"gps": 0, "heap": 1})
        assert len(mapper._events) == 1
        assert mapper._events[0].high is True

    # ── Timestamp wrap ────────────────────────────────────────────────
    # embedded-trace emits uint32_t µs timestamps; they wrap every
    # 2**32 µs ≈ 71.58 min. parse_serial_events compensates so the
    # resulting EventMapper sees a monotonic timeline.

    _WRAP_US = 1 << 32

    def test_wrap_single_rollover(self):
        pre = self._WRAP_US - 1_000_000    # 1 s before wrap
        post = 500_000                     # 0.5 s after wrap
        serial = self._b("gps", pre) + "\n" + self._e("gps", post) + "\n"

        mapper = parse_serial_events(serial, {"gps": 0})
        ts_values = [e.timestamp_s for e in mapper._events]

        assert ts_values[0] == pytest.approx(pre / 1_000_000)
        assert ts_values[1] == pytest.approx((post + self._WRAP_US) / 1_000_000)
        # Duration across wrap is 1.5 s, not ~-4294 s
        assert ts_values[1] - ts_values[0] == pytest.approx(1.5)

    def test_wrap_multiple_rollovers_accumulate(self):
        # Need each wrap to be a *large* backwards step (> 2**31 µs).
        # Simulate two full rollovers: near-end → near-start → near-end →
        # near-start.
        raw = [
            1_000,                         # start of period 1
            self._WRAP_US - 1_000,         # near end of period 1
            1_000,                         # wrapped → period 2 (wrap 1)
            self._WRAP_US - 1_000,         # near end of period 2
            1_000,                         # wrapped → period 3 (wrap 2)
        ]
        expected_us = [
            1_000,
            self._WRAP_US - 1_000,
            1_000 + self._WRAP_US,
            self._WRAP_US - 1_000 + self._WRAP_US,
            1_000 + 2 * self._WRAP_US,
        ]
        lines = [self._b(f"s{i}", ts) for i, ts in enumerate(raw)]
        channel_map = {f"s{i}": i for i in range(len(raw))}

        mapper = parse_serial_events("\n".join(lines), channel_map)

        observed_us = [e.timestamp_s * 1_000_000 for e in mapper._events]
        for obs, exp in zip(observed_us, expected_us):
            assert obs == pytest.approx(exp)

    def test_wrap_unaffected_by_equal_timestamps(self):
        # Equal ts is not a wrap — common for same-sample events.
        serial = "\n".join([
            self._b("gps", 500_000),
            self._e("gps", 500_000),
        ])
        mapper = parse_serial_events(serial, {"gps": 0})
        ts_values = [e.timestamp_s for e in mapper._events]
        assert all(t == pytest.approx(0.5) for t in ts_values)

    def test_wrap_does_not_trigger_on_ignored_events(self):
        # Only mapped events count toward wrap detection — otherwise the
        # ignored ones would trip spurious wraps. (Actually they're still
        # observed on the device, so they *do* count: wrap detection sees
        # every valid trace line, mapped or not.)
        serial = "\n".join([
            self._b("gps", 1_000),
            self._b("other", self._WRAP_US - 100),   # unmapped, but wraps
            self._b("other", 50),                    # unmapped, post-wrap
            self._b("gps", 1_000_000),               # post-wrap gps event
        ])
        mapper = parse_serial_events(serial, {"gps": 0})
        # Two gps events; the second is post-wrap so must get +2**32 µs.
        assert len(mapper._events) == 2
        assert mapper._events[1].timestamp_s == pytest.approx(
            (1_000_000 + self._WRAP_US) / 1_000_000
        )

    def test_small_backwards_step_is_not_wrap(self):
        # On dual-core ESP32, events from different cores can arrive at the
        # Serial line with slight backwards skew. Threshold: only treat a
        # backwards step > 2**31 µs (~35.8 min) as a true wrap.
        serial = "\n".join([
            self._b("gps", 1_000_500),   # core A
            self._b("imu", 1_000_000),   # core B, 500 µs behind — NOT a wrap
            self._b("gps", 2_000_000),
        ])
        mapper = parse_serial_events(serial, {"gps": 0, "imu": 1})
        # Raw values preserved; no 2**32 added anywhere.
        assert mapper._events[0].timestamp_s == pytest.approx(1.0005)
        assert mapper._events[1].timestamp_s == pytest.approx(1.0)
        assert mapper._events[2].timestamp_s == pytest.approx(2.0)


class TestEventMapperOptionalChannel:
    def test_accepts_none_channel(self):
        # None means "software-only" — recorded but does not toggle a logic bit.
        EventMapper({"gps": 0, "boot": None})  # must not raise

    def test_invalid_channel_still_raises(self):
        with pytest.raises(ValueError):
            EventMapper({"oops": 8})

    def test_apply_skips_logic_for_none_channel(self):
        mapper = EventMapper({"gps": 0, "boot": None})
        result = _make_result(1000)
        mapper.start("gps", 0.002)
        mapper.start("boot", 0.001)
        mapper.stop("gps", 0.005)
        mapper.stop("boot", 0.008)
        mapper.apply(result)

        # gps still toggles D0 the way it always did.
        assert result.samples[200].logic == 1
        assert result.samples[500].logic == 0
        # boot never touches sample.logic — the bitmask only ever shows D0.
        for s in result.samples:
            assert s.logic & ~1 == 0  # only bit 0 is ever set


class TestHtmlReportLegend:
    def test_html_report_labels_digital_channels(self, tmp_path):
        """HTML report uses legend names instead of D0/D1 for channel traces."""
        pytest.importorskip("plotly")

        result = _make_result(500)
        mapper = EventMapper({"GPS": 0, "LTE": 1})
        mapper.start("GPS", 0.001)
        mapper.stop("GPS", 0.003)
        mapper.start("LTE", 0.002)
        mapper.stop("LTE", 0.004)
        mapper.apply(result)

        legends = {"test": mapper.legend()}
        tr = ProfileResult(name="test", result=result)

        out = tmp_path / "report.html"
        html_report([tr], out, channel_legends=legends)
        html = out.read_text()

        # Channel names appear as trace names and y-axis labels
        assert "GPS" in html
        assert "LTE" in html

    def test_html_report_without_legend_uses_d_labels(self, tmp_path):
        """Without legends, digital channels show as D0, D1, etc."""
        pytest.importorskip("plotly")

        result = _make_result(500)
        mapper = EventMapper({"X": 0})
        mapper.start("X", 0.001)
        mapper.stop("X", 0.003)
        mapper.apply(result)

        tr = ProfileResult(name="test", result=result)
        out = tmp_path / "report.html"
        html_report([tr], out)
        html = out.read_text()

        # Without legend, falls back to D0 label
        assert "D0" in html


class TestEventMapperToScopes:
    def test_single_pair_becomes_one_scope(self):
        from ppk2.types import Scope

        mapper = EventMapper({"gps": 0})
        result = _make_result(1000)
        mapper.start("gps", 0.002)
        mapper.stop("gps", 0.005)

        scopes = mapper.to_scopes(result)
        assert len(scopes) == 1
        s = scopes[0]
        assert isinstance(s, Scope)
        assert s.name == "gps"
        assert s.start_s == pytest.approx(0.002)
        assert s.end_s == pytest.approx(0.005)
        assert s.channel == 0

    def test_repeated_pairs_become_multiple_scopes(self):
        mapper = EventMapper({"gps": 0})
        result = _make_result(2000)
        mapper.start("gps", 0.001)
        mapper.stop("gps", 0.003)
        mapper.start("gps", 0.005)
        mapper.stop("gps", 0.009)

        scopes = mapper.to_scopes(result)
        assert len(scopes) == 2
        assert scopes[0].start_s == pytest.approx(0.001)
        assert scopes[0].end_s == pytest.approx(0.003)
        assert scopes[1].start_s == pytest.approx(0.005)
        assert scopes[1].end_s == pytest.approx(0.009)

    def test_unterminated_scope_clamps_to_duration_and_warns(self, caplog):
        import logging

        mapper = EventMapper({"gps": 0})
        result = _make_result(1000)  # duration = 0.01s @ 100kHz
        mapper.start("gps", 0.002)
        # No matching stop.

        with caplog.at_level(logging.WARNING, logger="ppk2.events"):
            scopes = mapper.to_scopes(result)

        assert len(scopes) == 1
        assert scopes[0].start_s == pytest.approx(0.002)
        assert scopes[0].end_s == pytest.approx(result.duration_s)
        assert any("unterminated" in rec.message.lower() for rec in caplog.records)

    def test_none_channel_propagates(self):
        mapper = EventMapper({"boot": None})
        result = _make_result(1000)
        mapper.start("boot", 0.001)
        mapper.stop("boot", 0.004)

        scopes = mapper.to_scopes(result)
        assert len(scopes) == 1
        assert scopes[0].channel is None

    def test_no_events_returns_empty_list(self):
        mapper = EventMapper({"gps": 0})
        result = _make_result(1000)

        assert mapper.to_scopes(result) == []

    def test_unterminated_scope_after_capture_end_does_not_invert(self):
        # If a B arrives after the capture has ended (e.g. firmware
        # ringdown), end_s must never be less than start_s.
        mapper = EventMapper({"gps": 0})
        result = _make_result(1000)  # duration = 0.01s
        mapper.start("gps", 0.5)  # well past capture end

        scopes = mapper.to_scopes(result)
        assert len(scopes) == 1
        assert scopes[0].end_s >= scopes[0].start_s
