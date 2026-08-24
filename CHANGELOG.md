# Changelog

All notable changes to this project will be documented in this file.
Follows [Keep a Changelog](https://keepachangelog.com/) conventions.

## [Unreleased]

### Fixed
- **`status` reported a device that was no longer attached.** The handler
  returned the cached `DeviceState` verbatim, so `vdd_mv` and `dut_power` —
  which are only ever set by *commands* — were presented as though measured.
  A daemon whose PPK2 had been unplugged for weeks still reported a powered
  DUT, and nothing in the design could notice. `status` now also returns
  `connected`, computed live from the transport's open state plus the
  presence of the serial device node (pyserial leaves `is_open` true after a
  USB device physically disappears, so it does not detect an unplug on its
  own). The check is side-effect free — no writes — so `status` can never
  perturb an in-flight measurement.
- **`uptime_s` under-reported on hosts that sleep.** It derives from
  `time.monotonic()`, which does not advance while the machine is suspended,
  so a long-lived daemon on a laptop drifts far from reality (observed: 75
  days old, reporting 47). `status` now also returns `age_s`, true wall-clock
  age, so the two can be compared.
- `ppk2 daemon status` gains a LINK column showing `up` / `GONE`, or `─` when
  talking to a daemon that predates link reporting.

### Added
- Daemon server and client for persistent DUT power management
- Hardware integration tests and device connect sequence fixes
- CLI device commands — list, power, mode, voltage, measure
- Device control — source/ampere meter, voltage, DUT power, 100kHz sampling
- File I/O — save/load .ppk2 files (nRF Connect compatible)
- Reporting — markdown tables, HTML charts, GitHub Actions annotations
- Synthetic profiles — programmatic power profile generation with ProfileBuilder
- AI integration — generate, analyze, and validate profiles using Claude
- Desktop automation — open .ppk2 files in nRF Connect via Playwright
- GitHub Action for CI power profiling reports
- Embedded scope events in `.ppk2` files (`events.json` ZIP entry) — rendered as a "Named scopes" table in HTML reports
- `ppk2 daemon` CLI subcommand group: `start`, `stop`, `status`, `install`, `uninstall`, `run`, `log`, `release`, `acquire`, plus `power`, `voltage`, `mode`, `measure` aliases pinned to the daemon transport.
- `ppk2 direct` namespace for explicit serial-port bypass — same operational verbs (`power`, `voltage`, `mode`, `measure`) pinned to the direct transport.
- Auto-routing on base ops: `ppk2 power`, `ppk2 voltage`, `ppk2 mode`, `ppk2 measure` use the daemon when one is running, auto-spawn one when not (Unix). `--daemon` and `--direct` flags select transport explicitly.
- `ppk2 list` shows daemon state per device (`running (fork|launchd)`, `installed (stopped)`, or `─`).
- macOS `launchctl` integration via `LaunchdManager` for boot-persistent daemons, one per PPK2 (label `com.ppk2.daemon.<SERIAL>`).
- Cross-platform IPC + state-dir + service-management abstractions under `src/ppk2/_daemon_lib/` (`state.py`, `ipc.py`, `service.py`), intended for mechanical extraction to `embedded_bridge.daemon` in a future release. `WinServiceManager` and `SystemdManager` are stubs in this release.
- IPC wire protocol upgraded to embedded-bridge's message protocol (text + length-prefixed binary). Resolves the "raw frame bytes can contain `\n`" hazard that made `measure_stop` ack reads fragile.
- New runtime dependency on `embedded-bridge` for the message protocol primitives.

### Changed
- README restructured: feature list now links to per-feature docs in `docs/`; long sections (AI, firmware, GitHub Action, desktop, file format) factored out
- Quick Start leads with basic measurement and the daemon (with explanation of the source-meter DTR-on-close limitation) instead of firmware upgrade
- AI default model bumped from `claude-sonnet-4-5-20250929` to `claude-sonnet-4-6` (`generate_profile`, `analyze_profile`, `validate_profile`, and the `--model` argparse defaults)
- `ppk2 daemon stop` escalates from graceful IPC shutdown (5 s window) to SIGTERM (2 s window) to SIGKILL when a forked daemon doesn't drain a stuck reader.
- Daemon now fast-exits with a clear `ConnectionError` when its target serial isn't connected, so launchd retry loops for unplugged devices stop spamming the log instead of waiting ~25 s on a metadata-read timeout.
- Daemon errors out with a pointer at `ppk2 firmware abort-dfu` when the target PPK2 enumerates as the DFU bootloader (PID 0x521F) instead of trying to open it and getting garbage. The CLI auto-spawn path mirrors the same pre-check.
- Documentation: `docs/daemon.md` corrected to attribute the close-cuts-rail behaviour to USB CDC-ACM `SET_CONTROL_LINE_STATE` (universal across OSes) rather than macOS-specific DTR-on-close.

### Removed
- Commented out the unused trigger / switch-point / spike-filtering / reset opcodes in `commands.py` (`0x01`, `0x03`, `0x05`, `0x0E`, `0x0F`, `0x12`, `0x15`, `0x16`, `0x20`). Triggering, switch-point hysteresis, and spike filtering are all evaluated host-side in the modern nRF Connect Power Profiler app, and these opcodes were never called in our codebase. Whether the PPK2 firmware still implements them is unverified — the helpers are retained in commented form for reference. `commands.reset` and its test removed

### Fixed
- Daemon no longer self-joins its reader thread on a streaming-client crash (issue #2 in `docs/daemon_reader_loop_issues.md`). Previously, when the streaming consumer was killed, the reader thread caught `BrokenPipeError` and called `_disconnect_client` → `_stop_reader_thread` → `self._reader_thread.join(...)` from inside that very thread, raising `RuntimeError: cannot join current thread` and zombieing the daemon (port held, no forwarding, recovery only via `kill`). `_disconnect_client` now skips the join when called from the reader thread; the loop exits on its own. `_start_reader_thread` recycles a stale handle so a subsequent `measure_start` works. Also tolerates `OSError("Bad file descriptor")` in `_on_client_data` when the selector races the reader's `conn.close()`. Regression test in `tests/test_daemon.py::test_streaming_client_force_disconnect_does_not_self_join`.
- Daemon reader-loop conditional sleep (issue #1 in `docs/daemon_reader_loop_issues.md`). Previously the reader thread called `time.sleep(0.001)` after every drain — but on Apple Silicon that rounds up to a ~9 ms scheduler tick, overflowing the macOS USB-CDC TTY buffer at 400 KB/s and silently dropping ~63 % of samples. `_forward_samples()` now returns whether bytes were drained, and the reader only sleeps when it was idle. Verified on a workshop M1 Mac mini: 38.4 % → 100 % of nominal throughput.
- Drain serial buffer fully on reconnect
- Preserve PPK2 state on close
- Handle macOS dual-port enumeration
