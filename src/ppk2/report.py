"""Power profiling reports — tables and charts.

Produces:
- Markdown summary tables (for $GITHUB_STEP_SUMMARY or terminal)
- Interactive HTML reports with plotly charts (for CI artifacts or local viewing)
"""

import os
from dataclasses import dataclass
from pathlib import Path

from .types import MeasurementResult, Sample

SAMPLE_PERIOD_US = 10


@dataclass
class ProfileResult:
    """A named measurement with an optional pass/fail threshold."""

    name: str
    result: MeasurementResult
    max_ua: float | None = None  # pass/fail threshold

    @property
    def passed(self) -> bool | None:
        if self.max_ua is None:
            return None
        return self.result.mean_ua <= self.max_ua


def format_current(ua: float) -> str:
    """Format a current value with appropriate units."""
    if ua >= 1000:
        return f"{ua / 1000:.2f} mA"
    if ua >= 1:
        return f"{ua:.1f} uA"
    return f"{ua * 1000:.0f} nA"


def _format_current_html(ua: float) -> str:
    """Format current with unit in <small> tag for stats display."""
    if ua >= 1000:
        return f"{ua / 1000:.2f}<small>mA</small>"
    if ua >= 1:
        return f"{ua:.1f}<small>uA</small>"
    return f"{ua * 1000:.0f}<small>nA</small>"


def _format_time_ppk(ms: float, sep: str = "\n") -> str:
    """Format milliseconds as two-line time: HH:MM:SS / mmm.uuu ms."""
    total_us = int(ms * 1000)
    us = total_us % 1000
    total_ms = total_us // 1000
    ms_part = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    m = (total_s // 60) % 60
    h = total_s // 3600
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms_part:03d}.{us:03d} ms"


def summary_table(results: list[ProfileResult]) -> str:
    """Generate a markdown summary table from test results.

    Suitable for $GITHUB_STEP_SUMMARY or terminal output.
    """
    lines = []
    lines.append("| Test | Mean | P99 | Peak | Threshold | Status |")
    lines.append("|------|------|-----|------|-----------|--------|")

    for tr in results:
        r = tr.result
        mean = format_current(r.mean_ua)
        p99 = format_current(r.p99_ua)
        peak = format_current(r.max_ua)

        if tr.max_ua is not None:
            threshold = format_current(tr.max_ua)
            status = "PASS" if tr.passed else "FAIL"
        else:
            threshold = "-"
            status = "-"

        lines.append(f"| {tr.name} | {mean} | {p99} | {peak} | {threshold} | {status} |")

    if any(tr.result.lost_samples > 0 for tr in results):
        lost_tests = [
            f"{tr.name}: {tr.result.lost_samples}"
            for tr in results
            if tr.result.lost_samples > 0
        ]
        lines.append("")
        lines.append(f"*Data loss detected: {', '.join(lost_tests)}*")

    return "\n".join(lines)


def write_github_summary(results: list[ProfileResult]) -> None:
    """Write summary table to $GITHUB_STEP_SUMMARY if available."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    content = "## Power Profile Results\n\n" + summary_table(results) + "\n"

    with open(summary_path, "a") as f:
        f.write(content)


def github_annotations(results: list[ProfileResult]) -> None:
    """Emit GitHub Actions annotations for failed tests."""
    for tr in results:
        if tr.passed is False:
            print(
                f"::error title=Power budget exceeded: {tr.name}"
                f"::{tr.name}: mean {format_current(tr.result.mean_ua)}"
                f" exceeds threshold {format_current(tr.max_ua)}"
            )


MAX_CHART_POINTS = 10_000  # downsample to this many points for charts


def _downsample(samples: list[Sample], max_points: int = MAX_CHART_POINTS) -> tuple[list[float], list[float], list[Sample]]:
    """Downsample samples for charting, preserving min/max per bucket.

    Returns (times_ms, currents_ua, representative_samples).
    Uses LTTB-like min/max preservation: each bucket emits its min and max
    sample so spikes and dips are visible.
    """
    n = len(samples)
    if n <= max_points:
        times = [i * SAMPLE_PERIOD_US / 1000 for i in range(n)]
        currents = [s.current_ua for s in samples]
        return times, currents, samples

    bucket_size = n / (max_points // 2)  # 2 points per bucket (min + max)
    times: list[float] = []
    currents: list[float] = []
    out_samples: list[Sample] = []

    i = 0
    while i < n:
        end = min(int(i + bucket_size), n)
        bucket = samples[int(i):end]
        if not bucket:
            break

        # Find min and max in this bucket
        min_s = min(bucket, key=lambda s: s.current_ua)
        max_s = max(bucket, key=lambda s: s.current_ua)

        # Emit min first, then max (or vice versa based on position)
        min_idx = int(i) + bucket.index(min_s)
        max_idx = int(i) + bucket.index(max_s)

        for idx, s in sorted([(min_idx, min_s), (max_idx, max_s)]):
            times.append(idx * SAMPLE_PERIOD_US / 1000)
            currents.append(s.current_ua)
            out_samples.append(s)

        i += bucket_size

    return times, currents, out_samples


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    """Convert a hex color like '#e45756' to 'rgba(228,87,86,0.15)'."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def html_report(
    results: list[ProfileResult],
    output_path: str | Path,
    title: str = "Power Profile Report",
    channel_legends: dict[str, dict] | None = None,
    theme: str = "auto",
) -> None:
    """Generate an interactive HTML report with plotly charts.

    Includes:
    - Summary table
    - Current-over-time trace per test
    - Digital channel timeline per test
    - Current histogram per test

    Args:
        theme: "light", "dark", or "auto" (browser chooses).
    """
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        raise ImportError(
            "plotly is required for HTML reports. "
            "Install with: pip install ppk2-python[report]"
        )

    from .themes import load_theme
    th = load_theme(theme)
    pc = th.plotly  # plotly colors shorthand

    output_path = Path(output_path)
    html_parts = []

    # Header + styles (CSS from theme files, inlined)
    html_parts.append(
        f"<html><head><title>{title}</title>\n<style>\n"
        + th.css
        + "\n</style></head><body>"
    )
    html_parts.append(
        f'<div><span class="attribution">Powered by Plotly</span>'
        f'<h1>{title}</h1></div>'
    )

    # Summary cards
    html_parts.append("<h2>Summary</h2>")
    html_parts.append('<div class="summary-cards">')
    for tr in results:
        r = tr.result
        if tr.max_ua is not None:
            status_cls = "pass" if tr.passed else "fail"
            status_icon = "&#x2714;" if tr.passed else "&#x2718;"
            status_text = f'<span style="color:var(--{status_cls})">{status_icon}</span> PASS' if tr.passed else f'<span style="color:var(--{status_cls})">{status_icon}</span> FAIL'
            status_css = f"status-{status_cls}"
            thresh_html = _format_current_html(tr.max_ua)
        else:
            status_cls = ""
            status_text = "-"
            status_css = ""
            thresh_html = "-"
        card_cls = f"summary-card {status_cls}" if status_cls else "summary-card"
        html_parts.append(f'<div class="{card_cls}">')
        html_parts.append(
            f'<div class="stat"><span class="value {status_css}">{status_text}</span>'
            f'<span class="label">result</span></div>'
            f'<div class="stat"><span class="value">{_format_current_html(r.mean_ua)}</span>'
            f'<span class="label">average</span></div>'
            f'<div class="stat"><span class="value">{_format_current_html(r.min_ua)}</span>'
            f'<span class="label">min</span></div>'
            f'<div class="stat"><span class="value">{_format_current_html(r.max_ua)}</span>'
            f'<span class="label">peak</span></div>'
            f'<div class="stat"><span class="value">{thresh_html}</span>'
            f'<span class="label">threshold</span></div>'
        )
        if r.lost_samples > 0:
            html_parts.append(
                f'<div class="stat"><span class="value data-loss">{r.lost_samples}</span>'
                f'<span class="label">lost samples</span></div>'
            )
        html_parts.append('</div>')
    html_parts.append('</div>')

    chart_idx = 0

    # Per-test charts
    for tr in results:
        html_parts.append(f"<h2>{tr.name}</h2>")
        samples = tr.result.samples
        if not samples:
            html_parts.append("<p>No samples collected.</p>")
            continue

        times, currents, ds_samples = _downsample(samples)

        # Identify active digital channels
        lgnd = channel_legends.get(tr.name, {}) if channel_legends else {}
        ch_labels = lgnd.get("channels", {})
        active_channels: list[tuple[int, str]] = []
        if any(s.logic != 0 for s in ds_samples):
            for ch in range(8):
                if any((s.logic >> ch) & 1 for s in ds_samples):
                    label = ch_labels.get(f"D{ch}", f"D{ch}")
                    active_channels.append((ch, label))

        # Build digital channel time ranges for zoom-to buttons
        ch_ranges: dict[str, tuple[float, float]] = {}
        for ch_bit, label in active_channels:
            ch_times = [
                t for t, s in zip(times, ds_samples) if (s.logic >> ch_bit) & 1
            ]
            if ch_times:
                ch_ranges[label] = (ch_times[0], ch_times[-1])

        # Auto-scale y-axis units based on peak value
        peak_ua = max(currents) if currents else 1.0
        if peak_ua >= 1000:
            y_unit = "mA"
            y_scale = 1 / 1000
        elif peak_ua >= 1:
            y_unit = "uA"
            y_scale = 1.0
        else:
            y_unit = "nA"
            y_scale = 1000.0
        y_values = [c * y_scale for c in currents]
        t_max = times[-1] if times else 1.0

        # --- Main current chart (standalone, with rangeslider) ---
        fig = go.Figure()

        # Current trace
        fig.add_trace(
            go.Scatter(
                x=times,
                y=y_values,
                mode="lines",
                name="Current",
                line=dict(width=1, color=pc.trace),
                hovertemplate="<extra></extra>",
            ),
        )

        # Mean and threshold as shapes (not traces) so they don't appear in rangeslider
        mean_scaled = tr.result.mean_ua * y_scale
        ref_vals = [mean_scaled]
        if tr.max_ua is not None:
            thresh_scaled = tr.max_ua * y_scale
            ref_vals.append(thresh_scaled)

        # Y-axis range (in scaled units)
        y_axis_max = max(y_values) * 1.15
        y_axis_max = max(y_axis_max, max(ref_vals) * 1.15)

        # Add reference lines as shapes + annotations
        # Use yref="paper" so shapes don't appear in the rangeslider
        mean_color = pc.mean_pass if tr.passed is not False else pc.mean_fail
        thresh_color = pc.threshold

        # Convert data y to paper y (0=bottom, 1=top of plot area)
        mean_paper_y = mean_scaled / y_axis_max if y_axis_max > 0 else 0

        fig.add_shape(
            type="line", xref="paper", yref="paper",
            x0=0, x1=1,
            y0=mean_paper_y, y1=mean_paper_y,
            line=dict(width=1.5, color=mean_color, dash="dot"),
        )
        fig.add_annotation(
            x=0.99, xref="paper", y=mean_paper_y, yref="paper",
            text=f"Mean: {format_current(tr.result.mean_ua)}",
            showarrow=False, xanchor="right", font=dict(size=11, color=mean_color),
            bgcolor=pc.annotation_bg,
        )

        if tr.max_ua is not None:
            thresh_paper_y = thresh_scaled / y_axis_max if y_axis_max > 0 else 0

            fig.add_shape(
                type="line", xref="paper", yref="paper",
                x0=0, x1=1,
                y0=thresh_paper_y, y1=thresh_paper_y,
                line=dict(width=1.5, color=thresh_color, dash="dash"),
            )
            fig.add_annotation(
                x=0.99, xref="paper", y=thresh_paper_y, yref="paper",
                text=f"Threshold: {format_current(tr.max_ua)}",
                showarrow=False, xanchor="right", font=dict(size=11, color=thresh_color),
                bgcolor=pc.annotation_bg,
            )

        fig.update_yaxes(
            title_text=f"Current ({y_unit})",
            range=[0, y_axis_max],
            fixedrange=True,
            showgrid=True, gridcolor=pc.grid, gridwidth=1,
        )
        fig.update_xaxes(
            range=[0, t_max],
            autorange=False,
            minallowed=0, maxallowed=t_max,
            showticklabels=False,
            showgrid=True, gridcolor=pc.grid, gridwidth=1,
            rangeslider=dict(
                visible=True, thickness=0.22,
                bgcolor=pc.rangeslider_bg, bordercolor=pc.rangeslider_border, borderwidth=1,
                range=[0, t_max],
                autorange=False,
            ),
        )
        fig.update_layout(
            height=475,
            margin=dict(l=60, r=20, t=40, b=45),
            showlegend=True,
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02,
                xanchor="center", x=0.5,
            ),
            modebar_remove=["zoom2d","select2d","lasso2d","pan2d",
                           "zoomIn2d","zoomOut2d","autoScale2d",
                           "resetScale2d","toImage"],
            dragmode="pan",
            hovermode="closest",
            plot_bgcolor=pc.plot_bg,
            paper_bgcolor=pc.paper_bg,
        )

        # Crosshair spike lines
        fig.update_xaxes(
            showspikes=True, spikemode="across", spikethickness=1,
            spikecolor=pc.spike, spikedash="solid", spikesnap="cursor",
        )
        fig.update_yaxes(
            showspikes=True, spikemode="across", spikethickness=1,
            spikecolor=pc.spike, spikedash="solid", spikesnap="cursor",
            mirror="ticks", hoverformat=".2f",
        )

        # --- Digital channels (separate figures per channel) ---
        _digital_colors = pc.digital
        dig_figs: list[tuple[str, str, object]] = []  # (label, color, fig)
        for ch_bit, label in active_channels:
            ch_values = [(s.logic >> ch_bit) & 1 for s in ds_samples]
            color = _digital_colors[ch_bit % len(_digital_colors)]
            dfig = go.Figure()
            dfig.add_trace(
                go.Scatter(
                    x=times, y=ch_values,
                    mode="lines", name=label,
                    line=dict(width=2, color=color, shape="hv"),
                    fill="tozeroy",
                    fillcolor=_hex_to_rgba(color, 0.15),
                    hovertemplate="%{fullData.name}<extra></extra>",
                ),
            )
            dfig.update_yaxes(
                range=[-0.1, 1.3], tickvals=[0, 1], ticktext=["Off", "On"],
                fixedrange=True,
                showgrid=False, zeroline=False,
                tickfont=dict(color=pc.tick_color),
                showspikes=True, spikemode="across", spikethickness=1,
                spikecolor=pc.spike, spikedash="solid", spikesnap="cursor",
            )
            dfig.update_xaxes(
                range=[0, t_max],
                autorange=False,
                minallowed=0, maxallowed=t_max,
                showticklabels=False,
                showgrid=True, gridcolor=pc.grid, zeroline=False,
                showspikes=True, spikemode="across", spikethickness=1,
                spikecolor=pc.spike, spikedash="solid", spikesnap="cursor",
            )
            dfig.update_layout(
                height=50, margin=dict(l=60, r=20, t=0, b=0),
                showlegend=False,
                modebar_remove=["zoom2d","select2d","lasso2d","pan2d",
                               "zoomIn2d","zoomOut2d","autoScale2d",
                               "resetScale2d","toImage"],
                dragmode="pan",
                hovermode="closest",
                plot_bgcolor=pc.plot_bg,
                paper_bgcolor=pc.paper_bg,
            )
            dig_figs.append((label, color, dfig))

        # IDs for this chart
        div_id = f"chart_{chart_idx}"
        stats_id = f"stats_{chart_idx}"
        zoom_fn = f"zoomTo_{chart_idx}"

        def _ts_html(ms: float) -> str:
            total_us = int(ms * 1000)
            us = total_us % 1000
            total_ms = total_us // 1000
            ms_part = total_ms % 1000
            total_s = total_ms // 1000
            s = total_s % 60; m = (total_s // 60) % 60; h = total_s // 3600
            return (f'<span class="ts">{h:02d}:{m:02d}:{s:02d}'
                    f'<span class="sub">{ms_part:03d}.{us:03d} ms</span></span>')

        dur_s = t_max / 1000

        # PPK2-style window stats with zoom presets in title row
        init_charge_mc = tr.result.mean_ua * (t_max / 1000) / 1000
        charge_html = (f"{init_charge_mc:.2f}<small>mC</small>" if init_charge_mc >= 1
                       else f"{init_charge_mc * 1000:.1f}<small>uC</small>")
        dur_html = (f"{dur_s:.3f}<small>s</small>" if dur_s < 60
                    else f"{dur_s / 60:.1f}<small>min</small>")
        # Build zoom preset buttons
        zoom_buttons = ""
        for preset_label, preset_ms in [
            ("1ms", 1), ("5ms", 5), ("10ms", 10), ("50ms", 50),
            ("100ms", 100), ("500ms", 500), ("1s", 1000),
            ("3s", 3000), ("10s", 10000), ("1min", 60000),
        ]:
            if preset_ms <= t_max * 1.5:
                mid = t_max / 2
                x0 = max(0, mid - preset_ms / 2)
                x1 = min(t_max, x0 + preset_ms)
                zoom_buttons += (
                    f'<button onclick="{zoom_fn}({x0:.2f},{x1:.2f})">'
                    f'{preset_label}</button> '
                )
        zoom_buttons += f'<button onclick="{zoom_fn}(0,{t_max:.2f})">All</button>'
        # Stats row: Window + Selection side by side
        sel_id = f"sel_{chart_idx}"
        html_parts.append('<div class="stats-row">')

        # Window stats
        html_parts.append('<div>')
        html_parts.append(
            f'<div class="sel-title-row"><span class="sel-header">Window</span>'
            f'<span>{zoom_buttons}</span>'
            f'</div>'
        )
        html_parts.append(f'<div id="{stats_id}" class="window-stats">')
        html_parts.append(
            '<div class="stat"><span class="value" data-field="mean">'
            f'{_format_current_html(tr.result.mean_ua)}</span>'
            '<span class="label">average</span></div>'
            '<div class="stat"><span class="value" data-field="min">'
            f'{_format_current_html(tr.result.min_ua)}</span>'
            '<span class="label">min</span></div>'
            '<div class="stat"><span class="value" data-field="max">'
            f'{_format_current_html(tr.result.max_ua)}</span>'
            '<span class="label">max</span></div>'
            '<div class="stat"><span class="value" data-field="dur">'
            f'{dur_html}</span>'
            '<span class="label">time</span></div>'
            '<div class="stat"><span class="value" data-field="charge">'
            f'{charge_html}</span>'
            '<span class="label">charge</span></div>'
        )
        html_parts.append("</div></div>")

        # Selection stats
        html_parts.append(f'<div id="{sel_id}" class="sel-wrapper">')
        html_parts.append(
            '<div class="sel-title-row">'
            '<span class="sel-header">Selection</span>'
            '<span data-field="sel-clear" style="visibility:hidden">'
            f'<button onclick="zoomToSelection_{chart_idx}()">Zoom</button> '
            f'<button onclick="clearSelection_{chart_idx}()">Clear</button>'
            '</span></div>'
            '<div class="selection-bar">'
            '<div class="sel-layer" data-field="sel-hint">'
            '<span class="hint">'
            'Shift + drag to select</span>'
            '</div>'
            '<div class="sel-layer" data-field="sel-stats" style="visibility:hidden">'
            '<div class="stat">'
            '<span class="value" data-field="sel-mean">-</span>'
            '<span class="label">average</span></div>'
            '<div class="stat">'
            '<span class="value" data-field="sel-min">-</span>'
            '<span class="label">min</span></div>'
            '<div class="stat">'
            '<span class="value" data-field="sel-max">-</span>'
            '<span class="label">max</span></div>'
            '<div class="stat">'
            '<span class="value" data-field="sel-dur">-</span>'
            '<span class="label">time</span></div>'
            '<div class="stat">'
            '<span class="value" data-field="sel-charge">-</span>'
            '<span class="label">charge</span></div>'
            '</div>'
            '</div>'
        )
        html_parts.append("</div>")

        html_parts.append("</div>")  # close stats-row

        # Window time span (above chart, below legend)
        dur_str = f"{dur_s:.3f} s" if dur_s < 60 else f"{dur_s / 60:.1f} min"
        html_parts.append(
            f'<div class="time-span" id="{div_id}_timespan">'
            f'{_ts_html(0)} <span>\u2014</span> {_ts_html(t_max)}'
            f' <span class="dur">({dur_str})</span>'
            '</div>'
        )

        # Emit main current chart
        chart_html = fig.to_html(
            full_html=False, include_plotlyjs="cdn", div_id=div_id
        )
        ytag_id = f"ytag_{chart_idx}"
        xtag_id = f"xtag_{chart_idx}"
        html_parts.append(
            f'<div style="position:relative">{chart_html}'
            f'<div id="{ytag_id}" class="y-hover-tag"></div>'
            f'<div id="{xtag_id}" class="x-hover-tag"></div></div>'
        )

        # Rangeslider time (below chart, shows full data range)
        rs_span_id = f"{div_id}_rs_timespan"
        html_parts.append(
            f'<div class="time-span" id="{rs_span_id}" style="font-size:0.75em;margin:0 0 4px">'
            f'{_ts_html(0)} <span>\u2014</span> {_ts_html(t_max)}'
            f' <span class="dur">({dur_str})</span>'
            '</div>'
        )

        # Emit digital channel charts as separate section
        dig_div_ids: list[str] = []
        if dig_figs:
            html_parts.append('<div style="margin-top:8px">')
            for i, (label, color, dfig) in enumerate(dig_figs):
                dig_id = f"dig_{chart_idx}_{i}"
                dig_div_ids.append(dig_id)
                pad = 0.0
                t0, t1 = 0.0, t_max
                if label in ch_ranges:
                    t0, t1 = ch_ranges[label]
                    pad = (t1 - t0) * 0.05
                ch_zoom_fn = f"chZoom_{chart_idx}_{i}"
                html_parts.append(
                    f'<div class="sel-title-row" style="margin-top:4px">'
                    f'<span class="sel-header" style="color:{color}">{label}</span>'
                    f'<button title="Zoom to {label} activity (or selection)"'
                    f' onclick="{ch_zoom_fn}()"'
                    f' style="padding:1px 8px;border:1px solid var(--border);'
                    f'border-radius:3px;background:var(--card);cursor:pointer;'
                    f'font-size:11px;color:var(--navy)">Zoom</button>'
                    f'</div>'
                    f'<script>window.{ch_zoom_fn} = function() {{'
                    f' var s0 = window._sel_{chart_idx};'
                    f' if (s0 && s0[0] !== null) {{ {zoom_fn}(s0[0], s0[1]); }}'
                    f' else {{ {zoom_fn}({t0 - pad:.2f},{t1 + pad:.2f}); }}'
                    f' }};</script>'
                )
                dig_ytag = f"dig_ytag_{chart_idx}_{i}"
                dig_xtag = f"dig_xtag_{chart_idx}_{i}"
                html_parts.append(
                    f'<div style="position:relative">'
                    + dfig.to_html(full_html=False, include_plotlyjs=False,
                                   div_id=dig_id)
                    + f'<div id="{dig_ytag}" class="y-hover-tag"'
                    f' style="background:{color};color:#fff"></div>'
                    f'<div id="{dig_xtag}" class="x-hover-tag"'
                    f' style="background:{color};color:#fff"></div>'
                    f'</div>'
                )
            html_parts.append('</div>')

        # Histogram — collapsible
        hist_id = f"hist_{chart_idx}"
        fig_hist = go.Figure()
        hist_fmt = f"Current: %{{x:.2f}} {y_unit}<br>Count: %{{y}}<extra></extra>"
        fig_hist.add_trace(
            go.Histogram(
                x=y_values,
                nbinsx=100,
                name="Current distribution",
                marker_color=pc.histogram,
                hovertemplate=hist_fmt,
            ),
        )
        fig_hist.update_layout(
            title=dict(text="Current Distribution", font=dict(size=14)),
            xaxis_title=f"Current ({y_unit})",
            yaxis_title="Count",
            height=280,
            margin=dict(l=60, r=20, t=40, b=40),
            plot_bgcolor=pc.plot_bg,
            paper_bgcolor=pc.paper_bg,
        )
        html_parts.append(
            f'<div style="text-align:right;margin:6px 0">'
            f'<button style="min-width:110px;'
            f'padding:2px 10px;border:1px solid var(--border);border-radius:3px;'
            f'background:var(--card);cursor:pointer;font-size:11px;color:var(--navy)" '
            f'onclick="var h=document.getElementById(\'{hist_id}\');'
            f'var v=h.style.display===\'none\';h.style.display=v?\'\':\'none\';'
            f'this.textContent=v?\'Hide Histogram\':\'Show Histogram\'">'
            f'Show Histogram</button></div>'
        )
        html_parts.append(
            f'<div id="{hist_id}" style="display:none">'
            + fig_hist.to_html(full_html=False, include_plotlyjs=False)
            + '</div>'
        )

        # JavaScript: window stats, selection stats, shift+drag, y-hover-tag
        import json as _json
        html_parts.append(f"""<script>
(function() {{
    var times = {_json.dumps(times)};
    var currents = {_json.dumps(currents)};
    var el = document.getElementById('{div_id}');
    var statsEl = document.getElementById('{stats_id}');
    var selEl = document.getElementById('{sel_id}');
    var yTag = document.getElementById('{ytag_id}');
    var xTag = document.getElementById('{xtag_id}');
    var selX0 = null, selX1 = null;  // current selection range
    window._sel_{chart_idx} = [null, null];  // expose to channel zoom buttons

    // All chart div IDs (main + digital channels)
    var digIds = {_json.dumps(dig_div_ids)};

    // Zoom function — syncs main chart and all digital channel charts
    window.{zoom_fn} = function(x0, x1) {{
        console.log('[zoom] requested:', x0.toFixed(2), '-', x1.toFixed(2), 'span:', (x1-x0).toFixed(2), 'ms');
        Plotly.relayout('{div_id}', {{ 'xaxis.range': [x0, x1] }});
        digIds.forEach(function(id) {{
            Plotly.relayout(id, {{ 'xaxis.range': [x0, x1] }});
        }});
        setTimeout(function() {{
            var actual = el._fullLayout.xaxis.range;
            console.log('[zoom] actual:', actual[0].toFixed(2), '-', actual[1].toFixed(2), 'span:', (actual[1]-actual[0]).toFixed(2), 'ms');
        }}, 100);
    }};

    window.zoomToSelection_{chart_idx} = function() {{
        if (selX0 !== null && selX1 !== null) {{
            {zoom_fn}(selX0, selX1);
        }}
    }};

    function fmtCurrentPlain(ua) {{
        if (ua >= 1000) return (ua / 1000).toFixed(2) + ' mA';
        if (ua >= 1) return ua.toFixed(1) + ' uA';
        return (ua * 1000).toFixed(0) + ' nA';
    }}

    function fmtTimeTag(ms) {{
        var totalUs = Math.round(ms * 1000);
        var us = totalUs % 1000;
        var totalMs = Math.floor(totalUs / 1000);
        var msP = totalMs % 1000;
        var totalS = Math.floor(totalMs / 1000);
        var s = totalS % 60, m = Math.floor(totalS / 60) % 60, h = Math.floor(totalS / 3600);
        return String(h).padStart(2,'0') + ':' + String(m).padStart(2,'0') + ':' +
               String(s).padStart(2,'0') + '<br>' +
               String(msP).padStart(3,'0') + '.' + String(us).padStart(3,'0') + ' ms';
    }}

    function fmtCurrent(ua) {{
        if (ua >= 1000) return (ua / 1000).toFixed(2) + '<small>mA</small>';
        if (ua >= 1) return ua.toFixed(1) + '<small>uA</small>';
        return (ua * 1000).toFixed(0) + '<small>nA</small>';
    }}

    function fmtDuration(ms) {{
        var s = ms / 1000;
        if (s >= 60) return (s / 60).toFixed(1) + '<small>min</small>';
        return s.toFixed(3) + '<small>s</small>';
    }}

    function fmtCharge(mean_ua, dur_ms) {{
        var mc = mean_ua * (dur_ms / 1000) / 1000;
        if (mc >= 1) return mc.toFixed(2) + '<small>mC</small>';
        return (mc * 1000).toFixed(1) + '<small>uC</small>';
    }}

    function fmtTimePPK(ms) {{
        var totalUs = Math.round(ms * 1000);
        var us = totalUs % 1000;
        var totalMs = Math.floor(totalUs / 1000);
        var msP = totalMs % 1000;
        var totalS = Math.floor(totalMs / 1000);
        var s = totalS % 60, m = Math.floor(totalS / 60) % 60, h = Math.floor(totalS / 3600);
        return '<span class="ts">' + String(h).padStart(2,'0') + ':' + String(m).padStart(2,'0') + ':' +
               String(s).padStart(2,'0') + '<span class="sub">' +
               String(msP).padStart(3,'0') + '.' + String(us).padStart(3,'0') + ' ms</span></span>';
    }}

    function calcStats(x0, x1) {{
        var sum = 0, count = 0, mx = -Infinity, mn = Infinity;
        for (var i = 0; i < times.length; i++) {{
            if (times[i] >= x0 && times[i] <= x1) {{
                sum += currents[i]; count++;
                if (currents[i] > mx) mx = currents[i];
                if (currents[i] < mn) mn = currents[i];
            }}
        }}
        if (count === 0) return null;
        return {{ mean: sum / count, min: mn, max: mx, dur: x1 - x0 }};
    }}

    var rsSpanEl = document.getElementById('{rs_span_id}');

    function fmtDur(ms) {{
        return ms >= 1000 ? (ms / 1000).toFixed(3) + ' s' : ms.toFixed(1) + ' ms';
    }}

    // Custom two-line x-axis tick labels (HH:MM:SS / mmm.uuu ms)
    function fmtTickLabel(ms) {{
        var totalUs = Math.round(ms * 1000);
        var us = Math.abs(totalUs) % 1000;
        var totalMs = Math.floor(Math.abs(totalUs) / 1000);
        var msP = totalMs % 1000;
        var totalS = Math.floor(totalMs / 1000);
        var s = totalS % 60, m = Math.floor(totalS / 60) % 60, h = Math.floor(totalS / 3600);
        return String(h).padStart(2,'0') + ':' + String(m).padStart(2,'0') + ':' +
               String(s).padStart(2,'0') + '<br>' +
               String(msP).padStart(3,'0') + '.' + String(us).padStart(3,'0') + ' ms';
    }}

    function niceTicks(x0, x1, maxTicks) {{
        var span = x1 - x0;
        var rough = span / maxTicks;
        var mag = Math.pow(10, Math.floor(Math.log10(rough)));
        var residual = rough / mag;
        var nice;
        if (residual <= 1.5) nice = 1;
        else if (residual <= 3) nice = 2;
        else if (residual <= 7) nice = 5;
        else nice = 10;
        var step = nice * mag;
        var start = Math.ceil(x0 / step) * step;
        var ticks = [];
        for (var t = start; t <= x1; t += step) {{
            ticks.push(Math.round(t * 1000) / 1000);
        }}
        return ticks;
    }}

    function updateTickLabels(x0, x1) {{
        var ticks = niceTicks(x0, x1, 6);
        var annotations = [];
        // Preserve existing non-tick annotations (mean/threshold labels)
        var existing = el.layout.annotations || [];
        for (var i = 0; i < existing.length; i++) {{
            if (!existing[i]._isTick) annotations.push(existing[i]);
        }}
        for (var j = 0; j < ticks.length; j++) {{
            annotations.push({{
                x: ticks[j], xref: 'x', y: 0, yref: 'paper',
                text: fmtTickLabel(ticks[j]),
                showarrow: false, yanchor: 'top', yshift: 4,
                font: {{ size: 10, color: '{pc.tick_color}', family: "{pc.tick_font}" }},
                _isTick: true
            }});
        }}
        Plotly.relayout('{div_id}', {{ annotations: annotations }});
    }}

    function updateWindowStats(x0, x1) {{
        var s = calcStats(x0, x1);
        if (!s) return;
        statsEl.querySelector('[data-field="mean"]').innerHTML = fmtCurrent(s.mean);
        statsEl.querySelector('[data-field="min"]').innerHTML = fmtCurrent(s.min);
        statsEl.querySelector('[data-field="max"]').innerHTML = fmtCurrent(s.max);
        statsEl.querySelector('[data-field="dur"]').innerHTML = fmtDuration(s.dur);
        statsEl.querySelector('[data-field="charge"]').innerHTML = fmtCharge(s.mean, s.dur);
        var spanEl = document.getElementById('{div_id}_timespan');
        if (spanEl) {{
            spanEl.innerHTML = fmtTimePPK(x0) + ' <span>\\u2014</span> ' + fmtTimePPK(x1) +
                ' <span class="dur">(' + fmtDur(x1 - x0) + ')</span>';
        }}
        // Rangeslider always shows full data range
        if (rsSpanEl) {{
            rsSpanEl.innerHTML = fmtTimePPK(0) + ' <span>\\u2014</span> ' + fmtTimePPK(tMax) +
                ' <span class="dur">(' + fmtDur(tMax) + ')</span>';
        }}
        updateTickLabels(x0, x1);
    }}

    function showSelectionStats(x0, x1) {{
        var s = calcStats(x0, x1);
        if (!s) return;
        selX0 = x0; selX1 = x1;  // store for zoom-to-selection
        window._sel_{chart_idx} = [x0, x1];
        selEl.querySelector('[data-field="sel-hint"]').style.visibility = 'hidden';
        selEl.querySelector('[data-field="sel-clear"]').style.visibility = '';
        selEl.querySelector('[data-field="sel-stats"]').style.visibility = '';
        selEl.querySelector('[data-field="sel-mean"]').innerHTML = fmtCurrent(s.mean);
        selEl.querySelector('[data-field="sel-min"]').innerHTML = fmtCurrent(s.min);
        selEl.querySelector('[data-field="sel-max"]').innerHTML = fmtCurrent(s.max);
        selEl.querySelector('[data-field="sel-dur"]').innerHTML = fmtDuration(s.dur);
        selEl.querySelector('[data-field="sel-charge"]').innerHTML = fmtCharge(s.mean, s.dur);
    }}

    window.clearSelection_{chart_idx} = function() {{
        selX0 = null; selX1 = null;
        window._sel_{chart_idx} = [null, null];
        selEl.querySelector('[data-field="sel-hint"]').style.visibility = '';
        selEl.querySelector('[data-field="sel-clear"]').style.visibility = 'hidden';
        selEl.querySelector('[data-field="sel-stats"]').style.visibility = 'hidden';
        clearSelShape();
    }};

    // Right-side y-hover-tag via mousemove on plot area
    // Binary search to find closest time index
    function findIndex(t) {{
        var lo = 0, hi = times.length - 1;
        while (lo < hi) {{
            var mid = (lo + hi) >> 1;
            if (times[mid] < t) lo = mid + 1; else hi = mid;
        }}
        if (lo > 0 && Math.abs(times[lo-1] - t) < Math.abs(times[lo] - t)) lo--;
        return lo;
    }}

    var wrapper = el.parentElement;
    el.addEventListener('mousemove', function(e) {{
        var plotBox = el.querySelector('.nsewdrag');
        if (!plotBox) return;
        var br = plotBox.getBoundingClientRect();
        var wBr = wrapper.getBoundingClientRect();
        // Only show tags when mouse is over the current trace plot area
        if (e.clientX < br.left || e.clientX > br.right ||
            e.clientY < br.top || e.clientY > br.bottom) {{
            yTag.style.display = 'none';
            xTag.style.display = 'none';
            return;
        }}
        var xaxis = el._fullLayout.xaxis;
        var yaxis = el._fullLayout.yaxis;
        var t = xaxis.p2d(e.clientX - br.left);
        var idx = findIndex(t);
        var ua = currents[idx];

        // Y tag: right side, tracks mouse y-position (crosshair)
        // Show the current value at the data point, positioned at mouse y
        var yPx = e.clientY - wBr.top;
        yTag.style.display = 'block';
        yTag.style.top = yPx + 'px';
        yTag.textContent = fmtCurrentPlain(ua);

        // X tag: top, tracks mouse x-position
        var xPx = e.clientX - wBr.left;
        xTag.style.display = 'block';
        xTag.style.left = xPx + 'px';
        xTag.innerHTML = fmtTimeTag(times[idx]);
    }});
    el.addEventListener('mouseleave', function() {{
        yTag.style.display = 'none';
        xTag.style.display = 'none';
    }});

    var tMax = {t_max};

    // Custom pan: plotly's built-in pan can't be clamped, so we
    // disable dragmode and handle pan via mouse events ourselves.
    var panStart = null, panRange = null;
    var nsew = el.querySelector('.nsewdrag');

    el.addEventListener('mousedown', function(e) {{
        if (e.shiftKey) return;  // shift is for selection
        if (!nsew) return;
        var br = nsew.getBoundingClientRect();
        if (e.clientX < br.left || e.clientX > br.right ||
            e.clientY < br.top || e.clientY > br.bottom) return;
        e.preventDefault();
        e.stopImmediatePropagation();  // block plotly's built-in pan
        panStart = e.clientX;
        var xaxis = el._fullLayout.xaxis;
        panRange = [xaxis.range[0], xaxis.range[1]];
        el.style.cursor = 'grabbing';
    }}, true);

    document.addEventListener('mousemove', function(e) {{
        if (panStart === null) return;
        var br = nsew.getBoundingClientRect();
        var xaxis = el._fullLayout.xaxis;
        var pxDelta = e.clientX - panStart;
        var span = panRange[1] - panRange[0];
        var dataDelta = -pxDelta * span / br.width;
        var x0 = panRange[0] + dataDelta;
        var x1 = panRange[1] + dataDelta;
        // Clamp to data bounds
        if (x0 < 0) {{ x0 = 0; x1 = span; }}
        if (x1 > tMax) {{ x1 = tMax; x0 = tMax - span; }}
        Plotly.relayout('{div_id}', {{ 'xaxis.range': [x0, x1] }});
    }});

    document.addEventListener('mouseup', function() {{
        if (panStart !== null) {{
            panStart = null;
            el.style.cursor = '';
        }}
    }});

    // Sync digital channels + update stats on any relayout (from pan, zoom buttons, rangeslider)
    var syncing = false;
    el.on('plotly_relayout', function(ed) {{
        if (syncing) return;
        // Only sync when x-axis range actually changed (ignore shape-only updates)
        var hasRange = ed['xaxis.range[0]'] !== undefined || ed['xaxis.range'] !== undefined;
        console.log('[relayout] keys:', Object.keys(ed).join(','), 'hasRange:', hasRange);
        if (!hasRange) return;
        var x0 = 0, x1 = tMax;
        if (ed['xaxis.range[0]'] !== undefined) {{
            x0 = ed['xaxis.range[0]']; x1 = ed['xaxis.range[1]'];
        }} else if (ed['xaxis.range'] !== undefined) {{
            x0 = ed['xaxis.range'][0]; x1 = ed['xaxis.range'][1];
        }}
        // Clamp rangeslider final position
        var span = x1 - x0;
        var clamped = false;
        if (x0 < 0) {{ x0 = 0; x1 = Math.min(span, tMax); clamped = true; }}
        if (x1 > tMax) {{ x1 = tMax; x0 = Math.max(0, x1 - span); clamped = true; }}
        if (clamped) {{
            syncing = true;
            Plotly.relayout('{div_id}', {{ 'xaxis.range': [x0, x1] }});
            syncing = false;
        }}
        updateWindowStats(x0, x1);
        digIds.forEach(function(id) {{
            Plotly.relayout(id, {{ 'xaxis.range': [x0, x1] }});
        }});
    }});

    // Shift+drag selection — works on main chart and all digital channel charts
    // Preserve base shapes (mean/threshold lines) on main chart
    var baseShapes = el.layout.shapes ? el.layout.shapes.slice() : [];

    function selShapeObj(x0, x1) {{
        return {{ type:'rect', xref:'x', yref:'paper', x0:x0, x1:x1, y0:0, y1:1,
                  fillcolor:'{pc.selection_fill}', line:{{color:'{pc.selection_line}',width:1}} }};
    }}

    function showSelShape(x0, x1) {{
        var s = selShapeObj(x0, x1);
        Plotly.relayout('{div_id}', {{ shapes: baseShapes.concat([s]) }});
        digIds.forEach(function(id) {{
            Plotly.relayout(id, {{ shapes: [s] }});
        }});
    }}

    function clearSelShape() {{
        Plotly.relayout('{div_id}', {{ shapes: baseShapes }});
        digIds.forEach(function(id) {{
            Plotly.relayout(id, {{ shapes: [] }});
        }});
    }}

    var selStart = null;
    var selSourceEl = null;  // which chart element started the selection

    function attachSelection(chartEl) {{
        chartEl.addEventListener('mousedown', function(e) {{
            if (!e.shiftKey) return;
            var rect = chartEl.querySelector('.nsewdrag');
            if (!rect) return;
            var br = rect.getBoundingClientRect();
            if (e.clientX < br.left || e.clientX > br.right) return;
            e.preventDefault(); e.stopPropagation();
            e.stopImmediatePropagation();
            var xaxis = chartEl._fullLayout.xaxis;
            selStart = xaxis.p2d(e.clientX - br.left);
            selSourceEl = chartEl;
        }}, true);

        chartEl.addEventListener('mousemove', function(e) {{
            if (selStart === null || selSourceEl !== chartEl) return;
            var rect = chartEl.querySelector('.nsewdrag');
            if (!rect) return;
            var br = rect.getBoundingClientRect();
            var xaxis = chartEl._fullLayout.xaxis;
            var cur = xaxis.p2d(Math.max(br.left, Math.min(br.right, e.clientX)) - br.left);
            showSelShape(Math.min(selStart, cur), Math.max(selStart, cur));
        }});

        chartEl.addEventListener('mouseup', function(e) {{
            if (selStart === null || selSourceEl !== chartEl) return;
            var rect = chartEl.querySelector('.nsewdrag');
            if (!rect) return;
            var br = rect.getBoundingClientRect();
            var xaxis = chartEl._fullLayout.xaxis;
            var selEnd = xaxis.p2d(Math.max(br.left, Math.min(br.right, e.clientX)) - br.left);
            var x0 = Math.min(selStart, selEnd);
            var x1 = Math.max(selStart, selEnd);
            selStart = null; selSourceEl = null;
            if (x1 - x0 < 1) return;
            showSelShape(x0, x1);
            showSelectionStats(x0, x1);
        }}, true);
    }}

    function attachDblClick(chartEl) {{
        chartEl.addEventListener('dblclick', function(e) {{
            e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
            if (selX0 !== null && selX1 !== null) {{
                // Check if click is inside selection
                var rect = chartEl.querySelector('.nsewdrag');
                if (rect) {{
                    var br = rect.getBoundingClientRect();
                    var xaxis = chartEl._fullLayout.xaxis;
                    var t = xaxis.p2d(e.clientX - br.left);
                    if (t >= selX0 && t <= selX1) {{
                        {zoom_fn}(selX0, selX1);
                        return;
                    }}
                }}
            }}
            // No selection or clicked outside — zoom to full range
            {zoom_fn}(0, tMax);
        }}, true);
    }}

    // Attach selection + dblclick to main chart
    attachSelection(el);
    attachDblClick(el);

    // Attach selection + dblclick + hover tags to each digital channel chart
    var digLogic = {_json.dumps([[(s.logic >> ch_bit) & 1 for s in ds_samples] for ch_bit, _label in active_channels])};
    digIds.forEach(function(id, idx) {{
        var digEl = document.getElementById(id);
        if (!digEl) return;
        attachSelection(digEl);
        attachDblClick(digEl);

        var chVals = digLogic[idx];
        var dYtag = document.getElementById('dig_ytag_{chart_idx}_' + idx);
        var dXtag = document.getElementById('dig_xtag_{chart_idx}_' + idx);
        if (!dYtag || !dXtag) return;
        var wrapper = digEl.parentElement;
        digEl.addEventListener('mousemove', function(e) {{
            var plotBox = digEl.querySelector('.nsewdrag');
            if (!plotBox) return;
            var br = plotBox.getBoundingClientRect();
            var wBr = wrapper.getBoundingClientRect();
            if (e.clientX < br.left || e.clientX > br.right ||
                e.clientY < br.top || e.clientY > br.bottom) {{
                dYtag.style.display = 'none';
                dXtag.style.display = 'none';
                return;
            }}
            var xaxis = digEl._fullLayout.xaxis;
            var t = xaxis.p2d(e.clientX - br.left);
            var i = findIndex(t);
            var val = chVals[i] ? 'On' : 'Off';
            var yPx = e.clientY - wBr.top;
            dYtag.style.display = 'block';
            dYtag.style.top = yPx + 'px';
            dYtag.textContent = val;
            var xPx = e.clientX - wBr.left;
            dXtag.style.display = 'block';
            dXtag.style.left = xPx + 'px';
            dXtag.innerHTML = fmtTimeTag(times[i]);
        }});
        digEl.addEventListener('mouseleave', function() {{
            dYtag.style.display = 'none';
            dXtag.style.display = 'none';
        }});
    }});

    // Initial tick labels
    updateTickLabels(0, tMax);
}})();
</script>""")

        chart_idx += 1

    html_parts.append("</body></html>")

    output_path.write_text("\n".join(html_parts))


def _markdown_table_to_html(md_table: str) -> str:
    """Convert a simple markdown table to styled HTML."""
    lines = md_table.strip().split("\n")
    html = ['<table class="summary-table">']

    for i, line in enumerate(lines):
        if line.startswith("|---"):
            continue
        if line.startswith("*"):
            cols = html[-1].count("<td") or 6  # fallback
            html.append(
                f'<tr><td colspan="{cols}" class="data-loss">'
                f'{line.strip("*")}</td></tr>'
            )
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if i == 0:
            html.append("<tr>" + "".join(f"<th>{c}</th>" for c in cells) + "</tr>")
        else:
            parts = []
            for c in cells:
                if c == "PASS":
                    parts.append(f'<td class="status-pass">PASS</td>')
                elif c == "FAIL":
                    parts.append(f'<td class="status-fail">FAIL</td>')
                else:
                    parts.append(f"<td>{c}</td>")
            html.append("<tr>" + "".join(parts) + "</tr>")

    html.append("</table>")
    return "\n".join(html)
