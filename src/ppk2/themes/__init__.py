"""Report themes — CSS + plotly color palettes."""

from dataclasses import dataclass, field
from pathlib import Path

_THEMES_DIR = Path(__file__).parent

_LIGHT_PLOTLY = dict(
    trace="#00a9ce",
    plot_bg="#fff",
    paper_bg="rgba(0,0,0,0)",
    grid="#eee",
    spike="#999",
    rangeslider_bg="#f0f4f8",
    rangeslider_border="#dee2e6",
    selection_fill="rgba(0,169,206,0.12)",
    selection_line="rgba(0,169,206,0.4)",
    annotation_bg="rgba(255,255,255,0.8)",
    mean_pass="#198754",
    mean_fail="#dc3545",
    threshold="#6c757d",
    histogram="#00a9ce",
    tick_color="#6c757d",
    tick_font="'SF Mono','Fira Code',monospace",
    digital=[
        "#e36209", "#6f42c1", "#0d6efd", "#d63384",
        "#198754", "#ffc107", "#20c997", "#fd7e14",
    ],
)

_DARK_PLOTLY = dict(
    trace="#00e5ff",
    plot_bg="#1e1e2e",
    paper_bg="rgba(0,0,0,0)",
    grid="#333350",
    spike="#666",
    rangeslider_bg="#252540",
    rangeslider_border="#3a3a5c",
    selection_fill="rgba(0,229,255,0.15)",
    selection_line="rgba(0,229,255,0.4)",
    annotation_bg="rgba(30,30,46,0.85)",
    mean_pass="#2dd4bf",
    mean_fail="#fb7185",
    threshold="#8888aa",
    histogram="#00e5ff",
    tick_color="#8888aa",
    tick_font="'SF Mono','Fira Code',monospace",
    digital=[
        "#ff9f43", "#a78bfa", "#38bdf8", "#f472b6",
        "#4ade80", "#fbbf24", "#2dd4bf", "#fb923c",
    ],
)


@dataclass
class PlotlyColors:
    """Colors used in plotly figure construction (not CSS)."""

    trace: str = "#00a9ce"
    plot_bg: str = "#fff"
    paper_bg: str = "rgba(0,0,0,0)"
    grid: str = "#eee"
    spike: str = "#999"
    rangeslider_bg: str = "#f0f4f8"
    rangeslider_border: str = "#dee2e6"
    selection_fill: str = "rgba(0,169,206,0.12)"
    selection_line: str = "rgba(0,169,206,0.4)"
    annotation_bg: str = "rgba(255,255,255,0.8)"
    mean_pass: str = "#198754"
    mean_fail: str = "#dc3545"
    threshold: str = "#6c757d"
    histogram: str = "#00a9ce"
    tick_color: str = "#6c757d"
    tick_font: str = "'SF Mono','Fira Code',monospace"
    digital: list[str] = field(default_factory=lambda: list(_LIGHT_PLOTLY["digital"]))


@dataclass
class Theme:
    """A complete report theme."""

    name: str
    css: str
    plotly: PlotlyColors
    # For auto mode: JS object with both palettes for runtime switching
    plotly_js: str = ""


def _read_css(filename: str) -> str:
    return (_THEMES_DIR / filename).read_text()


def load_theme(name: str = "light") -> Theme:
    """Load a theme by name.

    Args:
        name: "light", "dark", or "auto" (browser chooses via prefers-color-scheme).
    """
    layout_css = _read_css("layout.css")

    if name == "auto":
        light_vars = _read_css("light.css")
        dark_vars = _read_css("dark.css")
        # Wrap dark vars in media query
        dark_media = dark_vars.replace(
            "/* Dark theme",
            "@media (prefers-color-scheme: dark) {\n/* Dark theme",
        ) + "\n}"
        css = light_vars + "\n" + dark_media + "\n" + layout_css

        # Default to light for initial plotly render; JS will switch if dark
        plotly = PlotlyColors(**{k: v for k, v in _LIGHT_PLOTLY.items() if k != "digital"})
        plotly.digital = list(_LIGHT_PLOTLY["digital"])

        import json
        plotly_js = (
            f"window.__ppk2_themes = {{"
            f"light: {json.dumps(_LIGHT_PLOTLY)},"
            f"dark: {json.dumps(_DARK_PLOTLY)}"
            f"}};"
        )

        return Theme(name=name, css=css, plotly=plotly, plotly_js=plotly_js)

    elif name == "dark":
        theme_css = _read_css("dark.css")
        css = theme_css + "\n" + layout_css
        plotly = PlotlyColors(**{k: v for k, v in _DARK_PLOTLY.items() if k != "digital"})
        plotly.digital = list(_DARK_PLOTLY["digital"])
        return Theme(name=name, css=css, plotly=plotly)

    else:  # light
        theme_css = _read_css("light.css")
        css = theme_css + "\n" + layout_css
        plotly = PlotlyColors()
        return Theme(name=name, css=css, plotly=plotly)
