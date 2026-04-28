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

from . import firmware


def _open_device(
    *,
    port: str | None = None,
    serial: str | None = None,
    transport: str = "auto",
):
    """Open a PPK2 device, routing through daemon or direct as configured.

    ``transport`` selects the routing policy:
        - ``"direct"`` — bypass daemon discovery and open the hardware
          directly via :class:`PPK2Device`. Used when the caller wants
          to talk to the port even if a daemon is running (interlocks
          guard the dangerous cases).
        - ``"daemon"`` — require a running daemon for the resolved
          device; ``SystemExit(1)`` if none is found.
        - ``"auto"`` (default) — prefer an existing daemon, otherwise
          auto-spawn one (Unix) or fall back to direct (Windows).

    Returns either a ``PPK2Device`` (direct) or a ``DaemonClient``;
    both expose the same measurement API and are context-managers.
    """
    from . import daemon as _daemon
    from . import transport as _transport
    from .device import PPK2Device

    if transport == "direct":
        try:
            return PPK2Device.open(port)
        except ConnectionError as e:
            print(f"Error: {e}")
            sys.exit(1)

    # Need a serial number to look up a daemon — resolve the device first.
    try:
        device = _transport.resolve_device(port=port, serial=serial)
    except ConnectionError as e:
        print(f"Error: {e}")
        sys.exit(1)

    info = _daemon.find_daemon(serial=device.serial_number)
    if info is not None:
        from .client import DaemonClient

        sn, sock_path = info
        return DaemonClient(sock_path, sn)

    if transport == "daemon":
        print(
            f"Error: no daemon running for {device.serial_number}",
            file=sys.stderr,
        )
        sys.exit(1)

    # transport == "auto" with no daemon: spawn one on Unix, fall back
    # to direct on Windows (where we don't have fork/launchd integration).
    if sys.platform == "win32":
        try:
            return PPK2Device.open(device.port)
        except ConnectionError as e:
            print(f"Error: {e}")
            sys.exit(1)

    return _autospawn_daemon(device.serial_number)


def _autospawn_daemon(serial: str):
    """Auto-spawn a daemon for ``serial`` and return a DaemonClient.

    Concurrent CLI invocations for the same device race to spawn. A
    per-serial lock file (``<state_dir>/<SERIAL>.spawn.lock``) flocks
    them into a single winner; later contenders re-check ``find_daemon``
    and reuse the spawned daemon's socket.

    DFU pre-check runs before the spawn attempt so a unit stuck in
    bootloader fails fast with a pointer at ``abort-dfu`` rather than
    surfacing a misleading "no PPK2 device" error from resolve_device.
    """
    import fcntl

    import serial.tools.list_ports as _list_ports

    from ._daemon_lib.state import state_dir
    from .client import DaemonClient
    from .daemon import find_daemon, start_daemon

    DFU_VID, DFU_PID = 0x1915, 0x521F
    for p in _list_ports.comports():
        if (p.vid == DFU_VID and p.pid == DFU_PID
                and p.serial_number == serial):
            print(
                f"Error: PPK2 {serial} is in DFU bootloader. Run "
                f"`ppk2 firmware abort-dfu --serial {serial}` first.",
                file=sys.stderr,
            )
            sys.exit(2)

    lock_path = state_dir() / f"{serial}.spawn.lock"
    lock_path.touch()
    with open(lock_path) as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        # Race-check: another contender may have spawned the daemon
        # while we waited for the lock.
        info = find_daemon(serial=serial)
        if info is None:
            sn, _pid = start_daemon(serial=serial)
            info = find_daemon(serial=sn)
            if info is None:
                raise RuntimeError("Auto-spawned daemon disappeared")
        sn, sock_path = info
        return DaemonClient(sock_path, sn)


def cmd_list(args: argparse.Namespace) -> int:
    """List connected PPK2 devices, including per-device daemon state."""
    from ._daemon_lib.service import get_service_manager
    from .daemon import find_daemon
    from .transport import list_ppk2_devices

    devices = list_ppk2_devices()
    if not devices:
        print("No PPK2 devices found.")
        return 1

    svc = get_service_manager()
    fmt = "{sn:10s}  {port:32s}  {daemon}"
    print(fmt.format(sn="SERIAL", port="PORT", daemon="DAEMON"))
    for d in devices:
        info = find_daemon(serial=d.serial_number)
        if info:
            mgmt = (
                "launchd" if (svc and svc.is_installed(d.serial_number))
                else "fork"
            )
            daemon_col = f"running ({mgmt})"
        elif svc and svc.is_installed(d.serial_number):
            daemon_col = "installed (stopped)"
        else:
            daemon_col = "─"
        print(fmt.format(
            sn=d.serial_number, port=d.port[:32], daemon=daemon_col,
        ))
    return 0


def _transport_from_args(args: argparse.Namespace) -> str:
    """Map ``--daemon`` / ``--direct`` argparse flags to a transport mode."""
    if getattr(args, "daemon", False):
        return "daemon"
    if getattr(args, "direct", False):
        return "direct"
    return "auto"


def cmd_power_impl(args, *, transport: str) -> int:
    on = args.state == "on"
    with _open_device(
        port=getattr(args, "port", None),
        serial=getattr(args, "serial", None),
        transport=transport,
    ) as ppk:
        ppk.toggle_dut_power(on)
        print(f"DUT power {'ON' if on else 'OFF'}")
    return 0


def cmd_power(args: argparse.Namespace) -> int:
    """Toggle DUT power on/off."""
    return cmd_power_impl(args, transport=_transport_from_args(args))


def cmd_mode_impl(args, *, transport: str) -> int:
    with _open_device(
        port=getattr(args, "port", None),
        serial=getattr(args, "serial", None),
        transport=transport,
    ) as ppk:
        if args.mode == "source":
            ppk.use_source_meter()
            print("Mode: source meter (PPK2 supplies power)")
        else:
            ppk.use_ampere_meter()
            print("Mode: ampere meter (external power)")
    return 0


def cmd_mode(args: argparse.Namespace) -> int:
    """Set PPK2 measurement mode."""
    return cmd_mode_impl(args, transport=_transport_from_args(args))


def cmd_voltage_impl(args, *, transport: str) -> int:
    with _open_device(
        port=getattr(args, "port", None),
        serial=getattr(args, "serial", None),
        transport=transport,
    ) as ppk:
        ppk.set_source_voltage(args.millivolts)
        print(f"Source voltage: {args.millivolts} mV")
    return 0


def cmd_voltage(args: argparse.Namespace) -> int:
    """Set source voltage in millivolts."""
    return cmd_voltage_impl(args, transport=_transport_from_args(args))


def cmd_measure_impl(args, *, transport: str) -> int:
    from .report import format_current

    with _open_device(
        port=getattr(args, "port", None),
        serial=getattr(args, "serial", None),
        transport=transport,
    ) as ppk:
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


def cmd_measure(args: argparse.Namespace) -> int:
    """Take a quick measurement and print stats."""
    return cmd_measure_impl(args, transport=_transport_from_args(args))


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


def cmd_merge(args: argparse.Namespace) -> int:
    """Merge Chrome Trace JSON with PPK2 power data."""
    from .merge import merge_trace_ppk2

    trace_path = Path(args.trace)
    ppk2_path = Path(args.ppk2)

    if not trace_path.exists():
        print(f"Trace file not found: {args.trace}")
        return 1
    if not ppk2_path.exists():
        print(f"PPK2 file not found: {args.ppk2}")
        return 1

    output = merge_trace_ppk2(
        trace_path, ppk2_path,
        output_path=args.output,
        downsample=args.downsample,
    )
    print(f"Merged trace written to: {output}")
    return 0


def _resolve_serial(args: argparse.Namespace) -> str | None:
    """Resolve `--serial` / `--port` to a serial number for nrfutil."""
    serial = getattr(args, "serial", None)
    port = getattr(args, "port", None)
    if serial:
        return serial
    if port:
        from .transport import resolve_device
        try:
            return resolve_device(port=port).serial_number
        except ConnectionError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(2)
    return None


def cmd_firmware_info(args: argparse.Namespace) -> int:
    """Query and print PPK2 firmware version."""
    try:
        info = firmware.query(serial_number=_resolve_serial(args))
    except firmware.FirmwareToolMissing as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except firmware.FirmwareQueryError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    if getattr(args, "json", False):
        import json as _json
        payload = {
            "serial_number": info.serial_number,
            "bootloader_type": info.bootloader_type,
            "bootloader_version": info.bootloader_version,
            "application_version": info.application_version,
        }
        print(_json.dumps(payload, indent=2))
    else:
        print(f"PPK2 {info.serial_number}:")
        print(f"  application version: {info.application_version}")
        bl_label = " (sDFU)" if "SDFU" in info.bootloader_type else ""
        print(f"  bootloader version:  {info.bootloader_version}{bl_label}")
    return 0


def cmd_firmware_check(args: argparse.Namespace) -> int:
    """Compare device firmware against the known-current version and upstream."""
    serial = _resolve_serial(args)
    try:
        info = firmware.query(serial_number=serial)
    except firmware.FirmwareToolMissing as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except firmware.FirmwareQueryError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    upstream: firmware.UpstreamFirmware | None = None
    upstream_error: str | None = None
    try:
        upstream = firmware.fetch_upstream()
    except firmware.UpstreamFetchError as e:
        upstream_error = str(e)

    up_to_date = info.application_version == firmware.CURRENT_APPLICATION_VERSION

    in_dfu = False
    try:
        in_dfu = firmware.is_in_dfu_mode(info.serial_number)
    except Exception:
        # DFU detection is best-effort — e.g., pyserial edge cases on exotic
        # platforms. Never let it break the main check flow.
        pass

    if getattr(args, "json", False):
        import json as _json
        payload = {
            "device": {
                "serial_number": info.serial_number,
                "bootloader_type": info.bootloader_type,
                "bootloader_version": info.bootloader_version,
                "application_version": info.application_version,
                "in_dfu_mode": in_dfu,
            },
            "upstream": (
                None if upstream is None
                else {
                    "semver": upstream.semver,
                    "commit": upstream.commit,
                    "filename": upstream.filename,
                    "url": upstream.url,
                }
            ),
            "up_to_date": up_to_date,
            "upstream_error": upstream_error,
        }
        print(_json.dumps(payload, indent=2))
    else:
        print(f"PPK2 {info.serial_number}:")
        print(
            f"  application version: {info.application_version}    "
            f"(known current: {firmware.CURRENT_APPLICATION_VERSION})"
        )
        print(f"  bootloader version:  {info.bootloader_version}")
        print()
        if upstream is not None:
            print("Upstream latest (nRF Connect Power Profiler):")
            print(f"  {upstream.semver} (commit {upstream.commit})")
            print(f"  {upstream.filename}")
        else:
            print(f"Upstream: unavailable ({upstream_error})")
        print()
        if up_to_date and upstream is not None:
            print("Status: up to date")
        elif upstream is None:
            pass
        else:
            print("Status: outdated")
        if in_dfu:
            print()
            print(
                "Warning: device is currently in the Open DFU Bootloader "
                "(not the application). Measurement commands will return "
                "garbage until the bootloader exits. Run:"
            )
            print(f"  ppk2 firmware abort-dfu --serial {info.serial_number}")

    if upstream is None:
        return 1
    return 0 if up_to_date else 1


def cmd_firmware_upgrade(args: argparse.Namespace) -> int:
    """Flash firmware onto the PPK2 via nrfutil."""
    if args.hex and args.download:
        print(
            "Error: --hex and --download are mutually exclusive",
            file=sys.stderr,
        )
        sys.exit(2)

    serial = _resolve_serial(args)
    if serial is None:
        from .transport import resolve_device
        try:
            serial = resolve_device().serial_number
        except ConnectionError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2

    # Pre-flash version (best-effort).
    pre_info = None
    try:
        pre_info = firmware.query(serial_number=serial)
    except (firmware.FirmwareQueryError, firmware.FirmwareToolMissing):
        pass

    # Pick hex source.
    if args.hex:
        hex_path = Path(args.hex)
        source_label = "user-supplied"
    else:
        try:
            hex_path, upstream = firmware.download_upstream()
        except firmware.UpstreamFetchError as e:
            print(f"Error: failed to fetch upstream firmware: {e}",
                  file=sys.stderr)
            return 2
        source_label = f"upstream {upstream.semver} (commit {upstream.commit})"
        print(f"Downloaded {hex_path.name} from nRF Connect repo.")

    print(f"Will flash {hex_path.name} ({source_label}) onto PPK2 {serial}.")
    if pre_info is not None:
        print(f"  Current app version: {pre_info.application_version}")
    if not args.yes:
        if not sys.stdin.isatty():
            print("Error: aborted (not a TTY; pass --yes to confirm)",
                  file=sys.stderr)
            return 1
        resp = input("Continue? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("Error: aborted by user", file=sys.stderr)
            return 1

    print("Flashing...")
    try:
        new_info = firmware.upgrade(serial, hex_path)
    except firmware.FirmwareToolMissing as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except firmware.FirmwareUpgradeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    print("Flash complete.")
    print(
        f"PPK2 {new_info.serial_number} is now at app version "
        f"{new_info.application_version} "
        f"(bootloader {new_info.bootloader_version})."
    )
    return 0


def cmd_firmware_abort_dfu(args: argparse.Namespace) -> int:
    """Tell a PPK2 stuck in DFU bootloader to boot the application."""
    serial = _resolve_serial(args)
    if serial is None:
        from .transport import resolve_device
        try:
            serial = resolve_device().serial_number
        except ConnectionError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2

    try:
        firmware.abort_dfu(serial)
    except firmware.FirmwareUpgradeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    print(f"Sent DFU ABORT to PPK2 {serial}; device should reboot into app mode.")
    return 0


def _fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"


def _resolve_lifecycle_targets(args):
    """Resolve --serial/--port/--all to a list of PPK2Port-like objects.

    Used by lifecycle verbs (start/stop) that may operate on a single
    device or every connected/installed PPK2. ``--all`` falls back to
    installed-but-disconnected serials if no devices are currently
    enumerated, so a launchd-installed disconnected unit still shows up.
    """
    from ._daemon_lib.service import get_service_manager
    from .transport import list_ppk2_devices, resolve_device

    if getattr(args, "all", False):
        devices = list_ppk2_devices()
        if not devices:
            svc = get_service_manager()
            if svc:
                devices = [
                    type("D", (), {"serial_number": sn, "port": ""})()
                    for sn in svc.list_installed()
                ]
        if not devices:
            print(
                "No PPK2 devices or installed services found.",
                file=sys.stderr,
            )
            sys.exit(1)
        return devices
    return [resolve_device(
        serial=getattr(args, "serial", None),
        port=getattr(args, "port", None),
    )]


def cmd_daemon_run(args: argparse.Namespace) -> int:
    from .daemon import run_daemon

    try:
        run_daemon(serial=args.serial, port=args.port)
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_daemon_start(args: argparse.Namespace) -> int:
    from ._daemon_lib.service import get_service_manager
    from .daemon import find_daemon, start_daemon

    devices = _resolve_lifecycle_targets(args)
    svc = get_service_manager()
    rc = 0
    for device in devices:
        sn = device.serial_number
        if find_daemon(serial=sn):
            print(f"{sn}: already running")
            continue
        if svc and svc.is_installed(sn):
            svc.start(sn)
            print(f"{sn}: started (service)")
        else:
            try:
                _, pid = start_daemon(serial=sn)
                print(f"{sn}: started (fork, pid={pid})")
            except Exception as e:
                print(f"{sn}: error: {e}", file=sys.stderr)
                rc = 1
    return rc


def cmd_daemon_stop(args: argparse.Namespace) -> int:
    from ._daemon_lib.service import get_service_manager
    from .client import DaemonClient
    from .daemon import find_daemon, list_daemons

    svc = get_service_manager()

    if getattr(args, "all", False):
        targets = list_daemons()
    else:
        info = find_daemon(
            serial=getattr(args, "serial", None),
            port=getattr(args, "port", None),
        )
        targets = [info] if info else []

    if not targets and not (svc and svc.list_installed()):
        print("No daemons running.")
        return 1

    rc = 0
    for sn, sock_path in targets:
        if svc and svc.is_installed(sn):
            svc.stop(sn)
            print(f"{sn}: stopped (service)")
        else:
            try:
                client = DaemonClient(sock_path, sn)
                client.shutdown()
                print(f"{sn}: stopped")
            except Exception as e:
                print(f"{sn}: error stopping: {e}", file=sys.stderr)
                rc = 1
    return rc


def cmd_daemon_status(args: argparse.Namespace) -> int:
    import json as _json

    from ._daemon_lib.service import get_service_manager
    from .client import DaemonClient
    from .daemon import list_daemons

    svc = get_service_manager()

    rows = []
    seen = set()
    for sn, sock_path in list_daemons():
        seen.add(sn)
        managed = "launchd" if (svc and svc.is_installed(sn)) else "fork"
        try:
            with DaemonClient(sock_path, sn) as client:
                state = client.status()
            rows.append({
                "serial": sn,
                "port": state.get("port", ""),
                "running": True,
                "managed": managed,
                "released": state.get("released", False),
                "vdd_mv": state.get("vdd_mv"),
                "mode": state.get("mode"),
                "dut_power": state.get("dut_power", False),
                "measuring": state.get("measuring", False),
                "uptime_s": state.get("uptime_s", 0),
            })
        except Exception:
            rows.append(
                {"serial": sn, "running": False, "managed": managed}
            )

    if svc:
        for sn in svc.list_installed():
            if sn in seen:
                continue
            rows.append(
                {"serial": sn, "running": False, "managed": "launchd"}
            )

    if getattr(args, "json", False):
        print(_json.dumps(rows, indent=2))
        return 0 if rows else 1

    if not rows:
        print("No daemons running or installed.")
        return 1

    fmt = (
        "{serial:10s}  {port:32s}  {power:5s}  "
        "{vdd:9s}  {mode:7s}  {uptime:>7s}  {managed}"
    )
    print(fmt.format(
        serial="SERIAL", port="PORT", power="POWER",
        vdd="VDD", mode="MODE", uptime="UPTIME", managed="MANAGED",
    ))
    for r in rows:
        if r.get("running"):
            print(fmt.format(
                serial=r["serial"],
                port=(r.get("port") or "")[:32],
                power=("ON" if r.get("dut_power") else "OFF"),
                vdd=f"{r.get('vdd_mv', 0)}mV",
                mode=r.get("mode") or "─",
                uptime=_fmt_uptime(r.get("uptime_s", 0)),
                managed=r.get("managed", "─"),
            ))
        else:
            print(fmt.format(
                serial=r["serial"], port="─", power="─",
                vdd="─", mode="─", uptime="─",
                managed=f"{r.get('managed', '─')} (stopped)",
            ))
    return 0


def cmd_daemon_install(args: argparse.Namespace) -> int:
    from ._daemon_lib.service import get_service_manager
    from .daemon import find_daemon

    svc = get_service_manager()
    if svc is None:
        print(
            "Service install not supported on this platform.",
            file=sys.stderr,
        )
        return 2
    devices = _resolve_lifecycle_targets(args)
    rc = 0
    for d in devices:
        sn = d.serial_number
        info = find_daemon(serial=sn)
        if info and not svc.is_installed(sn):
            print(
                f"{sn}: forked daemon already running; "
                f"stop it first (`ppk2 daemon stop --serial {sn}`)",
                file=sys.stderr,
            )
            rc = 1
            continue
        try:
            svc.install(sn)
            print(f"{sn}: installed and started")
        except Exception as e:
            print(f"{sn}: install failed: {e}", file=sys.stderr)
            rc = 1
    return rc


def cmd_daemon_uninstall(args: argparse.Namespace) -> int:
    from ._daemon_lib.service import get_service_manager
    from .transport import resolve_device

    svc = get_service_manager()
    if svc is None:
        print(
            "Service install not supported on this platform.",
            file=sys.stderr,
        )
        return 2

    if getattr(args, "all", False):
        serials = svc.list_installed()
        if not serials:
            print("No installed services.")
            return 0
    else:
        try:
            d = resolve_device(
                serial=getattr(args, "serial", None),
                port=getattr(args, "port", None),
            )
            serials = [d.serial_number]
        except ConnectionError:
            # Uninstall-by-serial works even if the device is unplugged.
            if getattr(args, "serial", None):
                serials = [args.serial]
            else:
                raise

    rc = 0
    for sn in serials:
        try:
            svc.uninstall(sn)
            print(f"{sn}: uninstalled")
        except Exception as e:
            print(f"{sn}: uninstall failed: {e}", file=sys.stderr)
            rc = 1
    return rc


def cmd_daemon_log(args: argparse.Namespace) -> int:
    import os as _os

    from .daemon import _log_path
    from .transport import resolve_device

    try:
        d = resolve_device(
            serial=getattr(args, "serial", None),
            port=getattr(args, "port", None),
        )
    except ConnectionError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    log = _log_path(d.serial_number)
    if not log.exists():
        print(f"No log file: {log}")
        return 1
    if getattr(args, "follow", False):
        _os.execvp("tail", ["tail", "-f", str(log)])
    print(str(log))
    return 0


def cmd_daemon_release(args: argparse.Namespace) -> int:
    from .client import DaemonClient
    from .daemon import find_daemon
    from .transport import resolve_device

    d = resolve_device(
        serial=getattr(args, "serial", None),
        port=getattr(args, "port", None),
    )
    info = find_daemon(serial=d.serial_number)
    if not info:
        print(
            f"No daemon running for {d.serial_number}", file=sys.stderr
        )
        return 1
    sn, sock_path = info
    with DaemonClient(sock_path, sn) as client:
        resp = client._request({"cmd": "release"})
    if not resp.get("ok"):
        print(f"Error: {resp.get('error')}", file=sys.stderr)
        return 1
    print(f"{sn}: released (port closed; daemon still running)")
    return 0


def cmd_daemon_acquire(args: argparse.Namespace) -> int:
    from .client import DaemonClient
    from .daemon import find_daemon
    from .transport import resolve_device

    d = resolve_device(
        serial=getattr(args, "serial", None),
        port=getattr(args, "port", None),
    )
    info = find_daemon(serial=d.serial_number)
    if not info:
        print(
            f"No daemon running for {d.serial_number}", file=sys.stderr
        )
        return 1
    sn, sock_path = info
    with DaemonClient(sock_path, sn) as client:
        resp = client._request({"cmd": "acquire"})
    if not resp.get("ok"):
        print(f"Error: {resp.get('error')}", file=sys.stderr)
        return 1
    print(f"{sn}: acquired (port reopened)")
    return 0


def cmd_daemon_power(args: argparse.Namespace) -> int:
    return cmd_power_impl(args, transport="daemon")


def cmd_daemon_voltage(args: argparse.Namespace) -> int:
    return cmd_voltage_impl(args, transport="daemon")


def cmd_daemon_mode(args: argparse.Namespace) -> int:
    return cmd_mode_impl(args, transport="daemon")


def cmd_daemon_measure(args: argparse.Namespace) -> int:
    return cmd_measure_impl(args, transport="daemon")


def cmd_direct(args: argparse.Namespace) -> int:
    handlers = {
        "power": lambda a: cmd_power_impl(a, transport="direct"),
        "voltage": lambda a: cmd_voltage_impl(a, transport="direct"),
        "mode": lambda a: cmd_mode_impl(a, transport="direct"),
        "measure": lambda a: cmd_measure_impl(a, transport="direct"),
    }
    action = getattr(args, "direct_command", None)
    if not action:
        print(
            "Specify a subcommand: power, voltage, mode, measure",
            file=sys.stderr,
        )
        return 1
    return handlers[action](args)


def cmd_daemon(args: argparse.Namespace) -> int:
    handlers = {
        "run": cmd_daemon_run,
        "start": cmd_daemon_start,
        "stop": cmd_daemon_stop,
        "status": cmd_daemon_status,
        "install": cmd_daemon_install,
        "uninstall": cmd_daemon_uninstall,
        "log": cmd_daemon_log,
        "release": cmd_daemon_release,
        "acquire": cmd_daemon_acquire,
        "power": cmd_daemon_power,
        "voltage": cmd_daemon_voltage,
        "mode": cmd_daemon_mode,
        "measure": cmd_daemon_measure,
    }
    action = getattr(args, "daemon_command", None) or "status"
    return handlers[action](args)


def _cmd_firmware_dispatch(args: argparse.Namespace) -> int:
    action = getattr(args, "firmware_action", None) or "info"
    if action == "info":
        return cmd_firmware_info(args)
    if action == "check":
        return cmd_firmware_check(args)
    if action == "upgrade":
        return cmd_firmware_upgrade(args)
    if action == "abort-dfu":
        return cmd_firmware_abort_dfu(args)
    raise SystemExit(f"Unknown firmware action: {action}")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ppk2",
        description="Nordic PPK2 power profiling tools",
    )
    sub = parser.add_subparsers(dest="command")

    # --daemon / --direct: shared by every base op verb (power/mode/voltage/
    # measure) so users can pin the transport regardless of where the verb
    # is registered. Mutually exclusive — argparse handles the combo error.
    transport_flags = argparse.ArgumentParser(add_help=False)
    _tg = transport_flags.add_mutually_exclusive_group()
    _tg.add_argument(
        "--daemon", action="store_true", default=False,
        help="Require a running daemon (don't auto-spawn)",
    )
    _tg.add_argument(
        "--direct", action="store_true", default=False,
        help="Bypass the daemon, open the serial port directly",
    )

    # --- Device commands ---

    # ppk2 list
    sub.add_parser("list", help="List connected PPK2 devices")

    # ppk2 power
    p_power = sub.add_parser(
        "power", help="Toggle DUT power on/off",
        parents=[transport_flags],
    )
    p_power.add_argument("state", choices=["on", "off"], help="Power state")
    p_power.add_argument("--port", help="Serial port (auto-detect if omitted)")

    # ppk2 mode
    p_mode = sub.add_parser(
        "mode", help="Set measurement mode",
        parents=[transport_flags],
    )
    p_mode.add_argument("mode", choices=["source", "ampere"], help="source=PPK2 powers DUT, ampere=external supply")
    p_mode.add_argument("--port", help="Serial port (auto-detect if omitted)")

    # ppk2 voltage
    p_voltage = sub.add_parser(
        "voltage", help="Set source voltage (mV)",
        parents=[transport_flags],
    )
    p_voltage.add_argument("millivolts", type=int, help="Voltage in millivolts (800-5000)")
    p_voltage.add_argument("--port", help="Serial port (auto-detect if omitted)")

    # ppk2 measure
    p_measure = sub.add_parser(
        "measure", help="Take a measurement",
        parents=[transport_flags],
    )
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
    p_validate.add_argument("--model", default="claude-sonnet-4-6", help="Anthropic model")

    # ppk2 analyze
    p_analyze = sub.add_parser("analyze", help="Analyze a .ppk2 file using Claude")
    p_analyze.add_argument("file", help="Path to .ppk2 file")
    p_analyze.add_argument("--context", help="What the device was doing during recording")
    p_analyze.add_argument("--model", default="claude-sonnet-4-6", help="Anthropic model")

    # ppk2 merge
    p_merge = sub.add_parser("merge", help="Merge Chrome Trace with PPK2 power data")
    p_merge.add_argument("trace", help="Chrome Trace JSON file")
    p_merge.add_argument("ppk2", help=".ppk2 power capture file")
    p_merge.add_argument("-o", "--output", help="Output path (default: <trace>_merged.json)")
    p_merge.add_argument("--downsample", type=int, default=100,
                         help="Take every Nth PPK2 sample (default: 100 → 1 kHz)")

    # ppk2 generate
    p_gen = sub.add_parser("generate", help="Generate synthetic .ppk2 from text description")
    p_gen.add_argument("description", help="Natural language power profile description")
    p_gen.add_argument("-o", "--output", default="profile.ppk2", help="Output .ppk2 file path")
    p_gen.add_argument("--seed", type=int, help="Random seed for reproducible output")
    p_gen.add_argument("--model", default="claude-sonnet-4-6", help="Anthropic model")
    p_gen.add_argument("--open", action="store_true", help="Open in nRF Connect after generating")

    # --- Firmware commands ---
    # Shared common args live on a `parents=` parser so that a value set on the
    # firmware parent (e.g. `ppk2 firmware --serial X check`) is not stomped by
    # the subparser's own default. Using SUPPRESS as the default means argparse
    # only adds an attribute when the flag is actually supplied — so the parent's
    # parse result is preserved when the subparser re-parses.
    fw_common = argparse.ArgumentParser(add_help=False)
    fw_common.add_argument("--serial", default=argparse.SUPPRESS,
                           help="Device serial number")
    fw_common.add_argument("--port", default=argparse.SUPPRESS,
                           help="Serial port (auto-detect if omitted)")
    fw_common.add_argument("--json", action="store_true",
                           default=argparse.SUPPRESS, help="JSON output")

    # ppk2 firmware [info|check|upgrade]
    p_firmware = sub.add_parser(
        "firmware", help="Query or upgrade PPK2 firmware (requires nrfutil)",
        parents=[fw_common],
    )
    firmware_sub = p_firmware.add_subparsers(dest="firmware_action")
    firmware_sub.add_parser("info", help="Show firmware version (default)",
                            parents=[fw_common])
    firmware_sub.add_parser("check", help="Compare device firmware with upstream",
                            parents=[fw_common])
    p_fw_upgrade = firmware_sub.add_parser(
        "upgrade", help="Flash a firmware .hex via nrfutil",
        parents=[fw_common],
    )
    p_fw_upgrade.add_argument("--hex", help="Path to .hex file")
    p_fw_upgrade.add_argument("--download", action="store_true",
                              help="Download latest upstream firmware")
    p_fw_upgrade.add_argument("--yes", action="store_true",
                              help="Skip confirmation prompt")
    firmware_sub.add_parser(
        "abort-dfu",
        help="Tell a PPK2 stuck in DFU bootloader to boot the application",
        parents=[fw_common],
    )

    # ppk2 daemon ...
    p_daemon = sub.add_parser(
        "daemon", help="Manage PPK2 daemon processes"
    )
    daemon_sub = p_daemon.add_subparsers(dest="daemon_command")

    def _daemon_selectors(p):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--serial", help="PPK2 serial number")
        g.add_argument("--port", help="Serial port path")
        g.add_argument(
            "--all", action="store_true",
            help="Operate on all running/installed daemons",
        )

    p_drun = daemon_sub.add_parser(
        "run", help="Run daemon in foreground (no fork; for service managers)"
    )
    p_drun.add_argument("--serial", help="PPK2 serial number")
    p_drun.add_argument("--port", help="Serial port path")

    p_dstart = daemon_sub.add_parser(
        "start", help="Start daemon (service or fork)"
    )
    _daemon_selectors(p_dstart)

    p_dstop = daemon_sub.add_parser(
        "stop", help="Stop a running daemon"
    )
    _daemon_selectors(p_dstop)

    p_dstatus = daemon_sub.add_parser(
        "status", help="Show daemon status"
    )
    _daemon_selectors(p_dstatus)
    p_dstatus.add_argument(
        "--json", action="store_true", help="JSON output"
    )

    p_dinstall = daemon_sub.add_parser(
        "install", help="Install as system service"
    )
    _daemon_selectors(p_dinstall)

    p_duninstall = daemon_sub.add_parser(
        "uninstall", help="Remove system service"
    )
    _daemon_selectors(p_duninstall)

    p_dlog = daemon_sub.add_parser(
        "log", help="Show daemon log path or tail"
    )
    p_dlog.add_argument("--serial", help="PPK2 serial number")
    p_dlog.add_argument("--port", help="Serial port path")
    p_dlog.add_argument(
        "-f", "--follow", action="store_true",
        help="tail -f the log",
    )

    p_drel = daemon_sub.add_parser(
        "release", help="Release the serial port (keep daemon running)"
    )
    p_drel.add_argument("--serial", help="PPK2 serial number")
    p_drel.add_argument("--port", help="Serial port path")

    p_dacq = daemon_sub.add_parser(
        "acquire", help="Reacquire the serial port"
    )
    p_dacq.add_argument("--serial", help="PPK2 serial number")
    p_dacq.add_argument("--port", help="Serial port path")

    # Operational aliases under `ppk2 daemon` — same verbs as the
    # top-level commands but pinned to `transport="daemon"`. Mirrors
    # the layout of `ppk2 direct ...` (Task 21) so users can pick a
    # transport explicitly without remembering --daemon/--direct flags.
    p_dpwr = daemon_sub.add_parser(
        "power", help="Set DUT power (require daemon)"
    )
    p_dpwr.add_argument("state", choices=["on", "off"])
    p_dpwr.add_argument("--port")
    p_dpwr.add_argument("--serial")

    p_dvolt = daemon_sub.add_parser(
        "voltage", help="Set source voltage (require daemon)"
    )
    p_dvolt.add_argument("millivolts", type=int)
    p_dvolt.add_argument("--port")
    p_dvolt.add_argument("--serial")

    p_dmode = daemon_sub.add_parser(
        "mode", help="Set mode (require daemon)"
    )
    p_dmode.add_argument("mode", choices=["source", "ampere"])
    p_dmode.add_argument("--port")
    p_dmode.add_argument("--serial")

    p_dmeas = daemon_sub.add_parser(
        "measure", help="Measure (require daemon)"
    )
    p_dmeas.add_argument("duration", type=float)
    p_dmeas.add_argument("--port")
    p_dmeas.add_argument("--serial")
    p_dmeas.add_argument("--source", type=int, metavar="MV")
    p_dmeas.add_argument("-o", "--output")

    # ppk2 direct ... — operational verbs pinned to transport="direct".
    p_direct = sub.add_parser(
        "direct",
        help="Bypass daemon, open serial port directly",
    )
    direct_sub = p_direct.add_subparsers(dest="direct_command")

    p_xpwr = direct_sub.add_parser("power")
    p_xpwr.add_argument("state", choices=["on", "off"])
    p_xpwr.add_argument("--port")
    p_xpwr.add_argument("--serial")

    p_xvolt = direct_sub.add_parser("voltage")
    p_xvolt.add_argument("millivolts", type=int)
    p_xvolt.add_argument("--port")
    p_xvolt.add_argument("--serial")

    p_xmode = direct_sub.add_parser("mode")
    p_xmode.add_argument("mode", choices=["source", "ampere"])
    p_xmode.add_argument("--port")
    p_xmode.add_argument("--serial")

    p_xmeas = direct_sub.add_parser("measure")
    p_xmeas.add_argument("duration", type=float)
    p_xmeas.add_argument("--port")
    p_xmeas.add_argument("--serial")
    p_xmeas.add_argument("--source", type=int, metavar="MV")
    p_xmeas.add_argument("-o", "--output")

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
        "merge": cmd_merge,
        "firmware": _cmd_firmware_dispatch,
        "daemon": cmd_daemon,
        "direct": cmd_direct,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
