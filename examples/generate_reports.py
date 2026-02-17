"""Generate example HTML reports for CI artifacts.

Demonstrates the report generation flow:
1. Load .ppk2 files (here we create synthetic ones as a stand-in)
2. Wrap each in a ProfileResult with a pass/fail threshold
3. Generate HTML reports

In practice the .ppk2 files come from real PPK2 captures:

    ppk2 report capture.ppk2 --html report.html --thresholds '{"capture": 5000}'
"""

from pathlib import Path

from ppk2.ppk2file import load_ppk2, save_ppk2
from ppk2.report import ProfileResult, html_report
from ppk2.synthetic import ProfileBuilder

OUTPUT_DIR = Path(__file__).parent / "output"


def make_ppk2_file(path: Path) -> None:
    """Create a synthetic .ppk2 file simulating an IoT wake cycle."""
    mr = (
        ProfileBuilder(seed=42)
        .phase("deep_sleep", current_ua=3.5, duration_s=0.5, noise_ua=0.5)
        .digital(0x01)
        .ramp("wakeup", start_ua=3.5, end_ua=15_000, duration_s=0.01)
        .phase("gps_acquire", current_ua=15_000, duration_s=0.2, noise_ua=2000)
        .digital(0x03)
        .phase("radio_tx", current_ua=45_000, duration_s=0.1, noise_ua=3000)
        .digital(0x01)
        .ramp("settle", start_ua=45_000, end_ua=800, duration_s=0.01)
        .phase("idle", current_ua=800, duration_s=0.3, noise_ua=50)
        .digital(0)
        .ramp("shutdown", start_ua=800, end_ua=3.5, duration_s=0.005)
        .phase("deep_sleep", current_ua=3.5, duration_s=0.5, noise_ua=0.5)
        .build()
    )
    save_ppk2(mr, path)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Step 1: Create a .ppk2 capture file (in practice, this comes from hardware)
    ppk2_path = OUTPUT_DIR / "wake_cycle.ppk2"
    make_ppk2_file(ppk2_path)
    print(f"Created {ppk2_path}")

    # Step 2: Load and wrap with pass/fail thresholds
    mr = load_ppk2(ppk2_path)
    results = [
        ProfileResult("Wake cycle (pass)", mr, max_ua=50_000),
        ProfileResult("Wake cycle (fail)", mr, max_ua=3_000),
    ]

    channel_legends = {
        "Wake cycle (pass)": {"channels": {"D0": "GPS", "D1": "Radio"}},
        "Wake cycle (fail)": {"channels": {"D0": "GPS", "D1": "Radio"}},
    }

    # Step 3: Generate reports in each theme
    for theme in ("light", "dark", "auto"):
        path = OUTPUT_DIR / f"example_{theme}.html"
        html_report(
            results,
            path,
            title=f"Example Report ({theme} theme)",
            channel_legends=channel_legends,
            theme=theme,
        )
        print(f"Generated {path}")


if __name__ == "__main__":
    main()
