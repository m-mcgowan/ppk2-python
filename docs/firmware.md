# Firmware Management

Query, check against upstream, and flash PPK2 firmware over USB. This is a
remote-friendly alternative to the nRF Connect GUI — useful from CI, headless
benches, or scripted bring-up. Most users running the desktop app won't need
this.

## Prerequisites

`ppk2 firmware` requires Nordic's `nrfutil` with the `device` and
`nrf5sdk-tools` subcommands.

macOS:

```bash
brew install --cask nrfutil
nrfutil install device nrf5sdk-tools
```

Linux: download `nrfutil` from
<https://www.nordicsemi.com/Products/Development-tools/nrf-util>, then
`nrfutil install device nrf5sdk-tools`. The rest of the library works
without `nrfutil` — only the firmware subcommands depend on it.

## Usage

```bash
ppk2 firmware                          # show running firmware version
ppk2 firmware check                    # compare against the latest upstream release
ppk2 firmware upgrade --yes            # download latest from nRF Connect repo and flash
ppk2 firmware upgrade --hex fw.hex     # flash a user-supplied hex
ppk2 firmware abort-dfu                # boot a PPK2 stuck in the DFU bootloader
```

`upgrade` downloads the hex from the nRF Connect Power Profiler GitHub repo,
wraps it into an unsigned SDFU zip (matching what the GUI app does), and
programs it via the PPK2's USB DFU bootloader.

### Stop the daemon first

If a `ppk2` daemon is holding the PPK2's serial port open, stop it before
upgrading — `nrfutil` needs exclusive access to enter DFU. See the
[daemon guide](daemon.md) for shutdown options.
