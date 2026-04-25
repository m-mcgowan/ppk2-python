# Changelog

All notable changes to this project will be documented in this file.
Follows [Keep a Changelog](https://keepachangelog.com/) conventions.

## [Unreleased]

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

### Changed
- README restructured: feature list now links to per-feature docs in `docs/`; long sections (AI, firmware, GitHub Action, desktop, file format) factored out
- Quick Start leads with basic measurement and the daemon (with explanation of the source-meter DTR-on-close limitation) instead of firmware upgrade
- AI default model bumped from `claude-sonnet-4-5-20250929` to `claude-sonnet-4-6` (`generate_profile`, `analyze_profile`, `validate_profile`, and the `--model` argparse defaults)

### Fixed
- Drain serial buffer fully on reconnect
- Preserve PPK2 state on close
- Handle macOS dual-port enumeration
