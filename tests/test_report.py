"""Tests for power profiling report generation."""

from ppk2.report import ProfileResult, format_current, summary_table, github_annotations, _markdown_table_to_html
from ppk2.types import MeasurementResult, Sample


def _make_result(mean: float, max_val: float, samples: list[Sample] | None = None) -> MeasurementResult:
    """Create a MeasurementResult with controlled statistics."""
    if samples is None:
        # Create minimal samples that produce the desired mean/max
        samples = [
            Sample(current_ua=mean, range=2, logic=0, counter=0),
            Sample(current_ua=max_val, range=2, logic=0, counter=1),
            Sample(current_ua=2 * mean - max_val, range=2, logic=0, counter=2),
        ]
    return MeasurementResult(samples=samples)


class TestFormatCurrent:
    def test_milliamps(self):
        assert format_current(1500.0) == "1.50 mA"

    def test_milliamps_boundary(self):
        assert format_current(1000.0) == "1.00 mA"

    def test_microamps(self):
        assert format_current(42.3) == "42.3 uA"

    def test_microamps_boundary(self):
        assert format_current(1.0) == "1.0 uA"

    def test_nanoamps(self):
        assert format_current(0.5) == "500 nA"

    def test_nanoamps_small(self):
        assert format_current(0.001) == "1 nA"


class TestProfileResult:
    def test_passed_under_threshold(self):
        r = _make_result(mean=10.0, max_val=20.0)
        tr = ProfileResult(name="sleep", result=r, max_ua=50.0)
        assert tr.passed is True

    def test_failed_over_threshold(self):
        r = _make_result(mean=100.0, max_val=200.0)
        tr = ProfileResult(name="sleep", result=r, max_ua=50.0)
        assert tr.passed is False

    def test_no_threshold(self):
        r = _make_result(mean=100.0, max_val=200.0)
        tr = ProfileResult(name="baseline", result=r)
        assert tr.passed is None


class TestSummaryTable:
    def test_basic_table(self):
        r = _make_result(mean=10.0, max_val=20.0)
        tr = ProfileResult(name="deep_sleep", result=r, max_ua=50.0)
        table = summary_table([tr])
        assert "deep_sleep" in table
        assert "PASS" in table
        assert "50.0 uA" in table

    def test_failing_test(self):
        r = _make_result(mean=100.0, max_val=200.0)
        tr = ProfileResult(name="idle", result=r, max_ua=50.0)
        table = summary_table([tr])
        assert "FAIL" in table

    def test_no_threshold_shows_dash(self):
        r = _make_result(mean=10.0, max_val=20.0)
        tr = ProfileResult(name="baseline", result=r)
        table = summary_table([tr])
        lines = table.split("\n")
        data_line = [l for l in lines if "baseline" in l][0]
        assert "| - |" in data_line

    def test_multiple_results(self):
        results = [
            ProfileResult(name="sleep", result=_make_result(5.0, 10.0), max_ua=20.0),
            ProfileResult(name="idle", result=_make_result(500.0, 1000.0), max_ua=1000.0),
        ]
        table = summary_table(results)
        assert "sleep" in table
        assert "idle" in table

    def test_data_loss_warning(self):
        samples = [Sample(current_ua=10.0, range=2, logic=0, counter=0)]
        r = MeasurementResult(samples=samples, lost_samples=5)
        tr = ProfileResult(name="lossy", result=r)
        table = summary_table([tr])
        assert "Data loss" in table
        assert "lossy: 5" in table

    def test_no_data_loss_no_warning(self):
        r = _make_result(mean=10.0, max_val=20.0)
        tr = ProfileResult(name="clean", result=r)
        table = summary_table([tr])
        assert "Data loss" not in table


class TestGithubAnnotations(object):
    def test_emits_error_for_failure(self, capsys):
        r = _make_result(mean=100.0, max_val=200.0)
        tr = ProfileResult(name="sleep", result=r, max_ua=50.0)
        github_annotations([tr])
        captured = capsys.readouterr()
        assert "::error" in captured.out
        assert "sleep" in captured.out

    def test_no_output_for_pass(self, capsys):
        r = _make_result(mean=10.0, max_val=20.0)
        tr = ProfileResult(name="sleep", result=r, max_ua=50.0)
        github_annotations([tr])
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_no_output_for_no_threshold(self, capsys):
        r = _make_result(mean=10.0, max_val=20.0)
        tr = ProfileResult(name="baseline", result=r)
        github_annotations([tr])
        captured = capsys.readouterr()
        assert captured.out == ""


class TestMarkdownTableToHtml:
    def test_basic_conversion(self):
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        html = _markdown_table_to_html(md)
        assert "<th>A</th>" in html
        assert "<td>1</td>" in html
        assert "<table" in html

    def test_data_loss_row(self):
        md = "| A |\n|---|\n*some note*"
        html = _markdown_table_to_html(md)
        assert "data-loss" in html
        assert "some note" in html


class TestScopeTable:
    def _result(self, n=1000):
        from ppk2.types import MeasurementResult, Sample
        return MeasurementResult(
            samples=[Sample(current_ua=10.0, range=0, logic=0, counter=i & 0x3F) for i in range(n)],
            duration_s=n / 100_000,
            sample_count=n,
        )

    def test_scope_table_present_when_events_nonempty(self, tmp_path):
        from ppk2.report import ProfileResult, html_report
        from ppk2.types import Scope

        result = self._result()
        result.events = [
            Scope(name="gps.cycle", start_s=0.001, end_s=0.005, channel=0),
            Scope(name="notecard.publish", start_s=0.002, end_s=0.004),
        ]
        out = tmp_path / "r.html"
        html_report([ProfileResult(name="t1", result=result)], out)
        text = out.read_text()
        assert "Named scopes (2)" in text
        assert "gps.cycle" in text
        assert "notecard.publish" in text

    def test_scope_table_absent_when_events_empty(self, tmp_path):
        from ppk2.report import ProfileResult, html_report

        result = self._result()
        out = tmp_path / "r.html"
        html_report([ProfileResult(name="t1", result=result)], out)
        text = out.read_text()
        assert "Named scopes" not in text

    def test_scope_table_renders_channel_or_dash(self, tmp_path):
        from ppk2.report import ProfileResult, html_report
        from ppk2.types import Scope

        result = self._result()
        result.events = [
            Scope(name="gps", start_s=0.0, end_s=0.001, channel=0),
            Scope(name="boot", start_s=0.0, end_s=0.001),  # no channel
        ]
        out = tmp_path / "r.html"
        html_report([ProfileResult(name="t1", result=result)], out)
        text = out.read_text()
        # channel column shows "0" for gps and "—" (em dash) for boot.
        assert ">0<" in text  # channel cell
        assert "&mdash;" in text or "—" in text
