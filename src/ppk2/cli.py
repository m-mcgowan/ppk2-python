"""PPK2 command-line interface.

Usage:
    ppk2 list                       List connected PPK2 devices
    ppk2 power on|off [--port ...]  Toggle DUT power
    ppk2 mode source|ampere [...]   Set measurement mode
    ppk2 voltage 3300 [--port ...]  Set source voltage (mV)
    ppk2 measure 5.0 [--port ...]   Measure for N seconds
    ppk2 open <file.ppk2>           Open a .ppk2 file in nRF Connect Power Profiler
    ppk2 report <file.ppk2> ...     Generate reports from .ppk2 files
    ppk2 info <file.ppk2>           Show file metadata and statistics
    ppk2 generate "description" -o out.ppk2   Generate synthetic profile from text
"""

import argparse
import sys
from pathlib import Path


def _open_device(port: str | None) -> "PPK2Device":
    """Open a PPK2 device, with user-friendly error messages."""
    from .device import PPK2Device

    try:
        return PPK2Device.open(port)
    except ConnectionError as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_list(args: argparse.Namespace) -> int:
    """List connected PPK2 devices."""
    from .transport import list_ppk2_devices

    devices = list_ppk2_devices()
    if not devices:
        print("No PPK2 devices found.")
        return 1

    for d in devices:
        print(f"{d.port}  serial={d.serial_number}  location={d.location}")
    return 0


def cmd_power(args: argparse.Namespace) -> int:
    """Toggle DUT power on/off."""
    on = args.state == "on"
    with _open_device(args.port) as ppk:
        ppk.toggle_dut_power(on)
        print(f"DUT power {'ON' if on else 'OFF'}")
    return 0


def cmd_mode(args: argparse.Namespace) -> int:
    """Set PPK2 measurement mode."""
    with _open_device(args.port) as ppk:
        if args.mode == "source":
            ppk.use_source_meter()
            print("Mode: source meter (PPK2 supplies power)")
        else:
            ppk.use_ampere_meter()
            print("Mode: ampere meter (external power)")
    return 0


def cmd_voltage(args: argparse.Namespace) -> int:
    """Set source voltage in millivolts."""
    with _open_device(args.port) as ppk:
        ppk.set_source_voltage(args.millivolts)
        print(f"Source voltage: {args.millivolts} mV")
    return 0


def cmd_measure(args: argparse.Namespace) -> int:
    """Take a quick measurement and print stats."""
    from .report import format_current

    with _open_device(args.port) as ppk:
        if args.source:
            ppk.use_source_meter()
            ppk.set_source_voltage(args.source)
            ppk.toggle_dut_power(True)

        result = ppk.measure(args.duration)
        print(f"Duration: {result.duration_s:.3f} s")
        print(f"Samples:  {result.sample_count:,}")
        print(f"Lost:     {result.lost_samples:,}")
        print(f"Mean:     {format_current(result.mean_ua)}")
        print(f"Min:      {format_current(result.min_ua)}")
        print(f"Max:      {format_current(result.max_ua)}")
        print(f"P99:      {format_current(result.p99_ua)}")

        if args.output:
            from .ppk2file import save_ppk2

            save_ppk2(result, args.output)
            print(f"\nSaved: {args.output}")
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    """Open a .ppk2 file in nRF Connect Power Profiler."""
    from .desktop import open_in_nrf_connect

    return open_in_nrf_connect(args.file, wait=not args.no_wait)


def cmd_report(args: argparse.Namespace) -> int:
    """Generate reports from .ppk2 files."""
    import json

    from .ppk2file import load_ppk2
    from .report import ProfileResult, html_report, summary_table

    thresholds: dict[str, float] = {}
    if args.thresholds:
        thresholds = json.loads(args.thresholds)

    results: list[ProfileResult] = []
    for f in args.files:
        path = Path(f)
        if not path.exists():
            print(f"File not found: {f}")
            return 1
        measurement = load_ppk2(path)
        name = path.stem
        max_ua = thresholds.get(name)
        results.append(ProfileResult(name=name, result=measurement, max_ua=max_ua))

    print(summary_table(results))

    if args.html:
        try:
            html_report(results, args.html, title=args.title or "Power Profile Report")
            print(f"\nHTML report: {args.html}")
        except ImportError:
            print("\nplotly required for HTML reports: pip install ppk2-python[report]")

    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    """Generate a synthetic .ppk2 file from a natural language description."""
    try:
        from .ai import generate_profile
    except ImportError:
        print("AI generation requires the anthropic package.")
        print("Install with: pip install anthropic")
        return 1

    from .ppk2file import save_ppk2
    from .report import format_current

    print(f"Generating profile: {args.description}")
    try:
        gen = generate_profile(args.description, model=args.model, seed=args.seed)
    except Exception as e:
        print(f"Generation failed: {e}")
        return 1

    print("\nPhases:")
    print(gen.phase_summary())

    result = gen.profile
    save_ppk2(result, args.output)
    print(f"\nSaved: {args.output}")
    print(f"  Samples:  {result.sample_count:,}")
    print(f"  Duration: {result.duration_s:.3f} s")
    print(f"  Mean:     {format_current(result.mean_ua)}")
    print(f"  Peak:     {format_current(result.max_ua)}")

    if args.open:
        try:
            from .desktop import open_in_nrf_connect
            return open_in_nrf_connect(args.output)
        except ImportError:
            print("Desktop automation requires playwright: pip install ppk2-python[desktop]")

    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate a .ppk2 file against a spec using Claude."""
    try:
        from .ai import validate_profile
    except ImportError:
        print("AI validation requires the anthropic package.")
        print("Install with: pip install anthropic")
        return 1

    from .ppk2file import load_ppk2

    path = Path(args.file)
    if not path.exists():
        print(f"File not found: {args.file}")
        return 1

    # Read spec from argument or file
    spec = args.spec
    if args.spec_file:
        spec = Path(args.spec_file).read_text()

    if not spec:
        print("Provide a spec with --spec or --spec-file")
        return 1

    result = load_ppk2(path)
    print(f"Validating {path.name} against spec...\n")

    validation = validate_profile(result, spec=spec, model=args.model)
    print(validation.report)

    return 0 if validation.passed else 1


def cmd_analyze(args: argparse.Namespace) -> int:
    """Analyze a .ppk2 file using Claude."""
    try:
        from .ai import analyze_profile
    except ImportError:
        print("AI analysis requires the anthropic package.")
        print("Install with: pip install anthropic")
        return 1

    from .ppk2file import load_ppk2

    path = Path(args.file)
    if not path.exists():
        print(f"File not found: {args.file}")
        return 1

    result = load_ppk2(path)
    print(f"Analyzing {path.name} ({result.sample_count:,} samples, {result.duration_s:.3f}s)...\n")

    context = args.context or ""
    analysis = analyze_profile(result, context=context, model=args.model)
    print(analysis)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    """Show .ppk2 file metadata and statistics."""
    from .ppk2file import load_ppk2
    from .report import format_current

    path = Path(args.file)
    if not path.exists():
        print(f"File not found: {args.file}")
        return 1

    result = load_ppk2(path)
    print(f"File:     {path.name}")
    print(f"Samples:  {result.sample_count:,}")
    print(f"Duration: {result.duration_s:.3f} s")
    print(f"Mean:     {format_current(result.mean_ua)}")
    print(f"Min:      {format_current(result.min_ua)}")
    print(f"Max:      {format_current(result.max_ua)}")
    print(f"P99:      {format_current(result.p99_ua)}")

    has_digital = any(s.logic != 0 for s in result.samples)
    if has_digital:
        active = set()
        for s in result.samples:
            for ch in range(8):
                if s.logic & (1 << ch):
                    active.add(ch)
        print(f"Digital:  D{', D'.join(str(c) for c in sorted(active))} active")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ppk2",
        description="Nordic PPK2 power profiling tools",
    )
    sub = parser.add_subparsers(dest="command")

    # --- Device commands ---

    # ppk2 list
    sub.add_parser("list", help="List connected PPK2 devices")

    # ppk2 power
    p_power = sub.add_parser("power", help="Toggle DUT power on/off")
    p_power.add_argument("state", choices=["on", "off"], help="Power state")
    p_power.add_argument("--port", help="Serial port (auto-detect if omitted)")

    # ppk2 mode
    p_mode = sub.add_parser("mode", help="Set measurement mode")
    p_mode.add_argument("mode", choices=["source", "ampere"], help="source=PPK2 powers DUT, ampere=external supply")
    p_mode.add_argument("--port", help="Serial port (auto-detect if omitted)")

    # ppk2 voltage
    p_voltage = sub.add_parser("voltage", help="Set source voltage (mV)")
    p_voltage.add_argument("millivolts", type=int, help="Voltage in millivolts (800-5000)")
    p_voltage.add_argument("--port", help="Serial port (auto-detect if omitted)")

    # ppk2 measure
    p_measure = sub.add_parser("measure", help="Take a measurement")
    p_measure.add_argument("duration", type=float, help="Duration in seconds")
    p_measure.add_argument("--port", help="Serial port (auto-detect if omitted)")
    p_measure.add_argument("--source", type=int, metavar="MV", help="Use source mode at this voltage (mV)")
    p_measure.add_argument("-o", "--output", help="Save to .ppk2 file")

    # --- File commands ---

    # ppk2 open
    p_open = sub.add_parser("open", help="Open .ppk2 in nRF Connect Power Profiler")
    p_open.add_argument("file", help="Path to .ppk2 file")
    p_open.add_argument("--no-wait", action="store_true", help="Don't wait for app to close")

    # ppk2 report
    p_report = sub.add_parser("report", help="Generate reports from .ppk2 files")
    p_report.add_argument("files", nargs="+", help=".ppk2 file paths")
    p_report.add_argument("--thresholds", help="JSON: {\"name\": max_ua, ...}")
    p_report.add_argument("--html", help="Output path for HTML report")
    p_report.add_argument("--title", help="Report title")

    # ppk2 info
    p_info = sub.add_parser("info", help="Show .ppk2 file metadata and statistics")
    p_info.add_argument("file", help="Path to .ppk2 file")

    # ppk2 validate
    p_validate = sub.add_parser("validate", help="Validate a .ppk2 file against a spec using Claude")
    p_validate.add_argument("file", help="Path to .ppk2 file")
    p_validate.add_argument("--spec", help="Expected power behavior (natural language)")
    p_validate.add_argument("--spec-file", help="File containing the spec")
    p_validate.add_argument("--model", default="claude-sonnet-4-5-20250929", help="Anthropic model")

    # ppk2 analyze
    p_analyze = sub.add_parser("analyze", help="Analyze a .ppk2 file using Claude")
    p_analyze.add_argument("file", help="Path to .ppk2 file")
    p_analyze.add_argument("--context", help="What the device was doing during recording")
    p_analyze.add_argument("--model", default="claude-sonnet-4-5-20250929", help="Anthropic model")

    # ppk2 generate
    p_gen = sub.add_parser("generate", help="Generate synthetic .ppk2 from text description")
    p_gen.add_argument("description", help="Natural language power profile description")
    p_gen.add_argument("-o", "--output", default="profile.ppk2", help="Output .ppk2 file path")
    p_gen.add_argument("--seed", type=int, help="Random seed for reproducible output")
    p_gen.add_argument("--model", default="claude-sonnet-4-5-20250929", help="Anthropic model")
    p_gen.add_argument("--open", action="store_true", help="Open in nRF Connect after generating")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 0

    handlers = {
        "list": cmd_list,
        "power": cmd_power,
        "mode": cmd_mode,
        "voltage": cmd_voltage,
        "measure": cmd_measure,
        "open": cmd_open,
        "report": cmd_report,
        "info": cmd_info,
        "analyze": cmd_analyze,
        "validate": cmd_validate,
        "generate": cmd_generate,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
