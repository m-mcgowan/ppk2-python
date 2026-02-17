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


class TestParseSerialEvents:
    def test_basic_parsing(self):
        serial = """
        T=0.500 GPS_STARTED
        T=1.800 LTE_TX_STARTED
        T=2.000 GPS_STOPPED
        T=3.500 LTE_TX_STOPPED
        """
        mapper = parse_serial_events(serial, {"GPS": 0, "LTE_TX": 1})
        assert len(mapper._events) == 4

    def test_applies_correctly(self):
        serial = "T=0.001 GPS_STARTED\nT=0.005 GPS_STOPPED\n"
        mapper = parse_serial_events(serial, {"GPS": 0})
        result = _make_result(1000)
        mapper.apply(result)

        assert result.samples[50].logic == 0
        assert result.samples[150].logic == 1
        assert result.samples[550].logic == 0

    def test_ignores_unknown_events(self):
        serial = "T=0.001 UNKNOWN_STARTED\nT=0.002 GPS_STARTED\n"
        mapper = parse_serial_events(serial, {"GPS": 0})
        assert len(mapper._events) == 1

    def test_ignores_blank_lines(self):
        serial = "\n\nT=0.001 GPS_STARTED\n\n"
        mapper = parse_serial_events(serial, {"GPS": 0})
        assert len(mapper._events) == 1

    def test_custom_markers(self):
        serial = "T=1.0 GPS_ON\nT=2.0 GPS_OFF\n"
        mapper = parse_serial_events(
            serial, {"GPS": 0}, start_marker="_ON", stop_marker="_OFF"
        )
        assert len(mapper._events) == 2
        assert mapper._events[0].high is True
        assert mapper._events[1].high is False


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
