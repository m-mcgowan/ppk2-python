"""PPK2 firmware query and upgrade via nrfutil.

Shells out to `nrfutil device fw-info` / `nrfutil device program`
for device I/O, and uses the GitHub contents API to discover the
upstream firmware published by the nRF Connect Power Profiler app.

nrfutil is an OPTIONAL runtime dependency. The module raises
FirmwareToolMissing with install instructions when it's not found.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError

GITHUB_CONTENTS_URL = (
    "https://api.github.com/repos/NordicSemiconductor/pc-nrfconnect-ppk/"
    "contents/firmware"
)
_HEX_FILENAME_RE = re.compile(
    r"^pca63100_ppk2_(?P<semver>\d+\.\d+\.\d+)_(?P<commit>[0-9a-f]+)\.hex$"
)

# Bump when we observe a new firmware in the upstream nRF Connect repo.
# Determine by flashing the upstream hex and reading back via `nrfutil fw-info`.
CURRENT_APPLICATION_VERSION = 20300
CURRENT_SEMVER = "1.2.4"
CURRENT_COMMIT = "db16a94"


@dataclass(frozen=True)
class FirmwareInfo:
    """Firmware information reported by nrfutil."""
    serial_number: str
    bootloader_type: str
    bootloader_version: int
    application_version: int
    raw: dict


@dataclass(frozen=True)
class UpstreamFirmware:
    """Latest PPK2 firmware published in the nRF Connect repo."""
    semver: str
    commit: str
    filename: str
    url: str


class FirmwareToolMissing(RuntimeError):
    """nrfutil (or its `device` subcommand) is not installed."""


class FirmwareQueryError(RuntimeError):
    """Failure while querying the device via nrfutil."""


class UpstreamFetchError(RuntimeError):
    """Failure while fetching upstream firmware metadata or files."""


class FirmwareUpgradeError(RuntimeError):
    """Failure during firmware upgrade flow."""


def query(
    serial_number: str | None = None,
    *,
    timeout: float = 10.0,
) -> FirmwareInfo:
    """Query PPK2 firmware via `nrfutil device fw-info --json`.

    If serial_number is None, it is resolved via
    `ppk2.transport.resolve_device()`.

    Raises:
        FirmwareToolMissing: nrfutil or its `device` subcommand is not installed.
        FirmwareQueryError: any other failure (no device, non-zero exit,
            unparseable output).
    """
    if serial_number is None:
        from .transport import resolve_device
        try:
            port = resolve_device()
        except ConnectionError as e:
            raise FirmwareQueryError(str(e)) from e
        serial_number = port.serial_number

    cmd = [
        "nrfutil", "device", "fw-info",
        "--serial-number", serial_number,
        "--traits", "nordicUsb",
        "--json",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as e:
        raise FirmwareToolMissing(_tool_install_hint()) from e

    if result.returncode != 0:
        stderr_lower = (result.stderr or "").lower()
        if "not found" in stderr_lower and "device" in stderr_lower:
            raise FirmwareToolMissing(_tool_install_hint())
        raise FirmwareQueryError(
            f"nrfutil exited {result.returncode}: {result.stderr.strip()}"
        )

    return _parse_fw_info(result.stdout, serial_number)


def _tool_install_hint() -> str:
    return (
        "nrfutil (with the 'device' subcommand) is required for firmware "
        "operations. Install with:\n"
        "    brew install --cask nrfutil\n"
        "    nrfutil install device\n"
        "See https://www.nordicsemi.com/Products/Development-tools/nrf-util"
    )


def _parse_fw_info(stdout: str, serial_number: str) -> FirmwareInfo:
    info_event: dict | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise FirmwareQueryError(f"unparseable nrfutil output: {e}") from e
        if obj.get("type") == "info":
            info_event = obj
    if info_event is None:
        raise FirmwareQueryError("no 'info' event in nrfutil output")

    devices = info_event.get("data", {}).get("devices", [])
    if not devices:
        raise FirmwareQueryError("nrfutil returned no devices")
    device = devices[0]

    image_list = device.get("imageInfoList") or []
    bootloader = next(
        (img for img in image_list
         if img.get("imageType") == "NRFDL_IMAGE_TYPE_BOOTLOADER"),
        None,
    )
    application = next(
        (img for img in image_list
         if img.get("imageType") == "NRFDL_IMAGE_TYPE_APPLICATION"),
        None,
    )
    if bootloader is None or application is None:
        raise FirmwareQueryError(
            "nrfutil output missing bootloader or application image info"
        )

    return FirmwareInfo(
        serial_number=device.get("serialNumber", serial_number),
        bootloader_type=device.get("bootloaderType", ""),
        bootloader_version=int(bootloader["version"]),
        application_version=int(application["version"]),
        raw=device,
    )


def fetch_upstream(*, timeout: float = 10.0) -> UpstreamFirmware:
    """Fetch the latest PPK2 firmware metadata from the nRF Connect repo.

    Raises UpstreamFetchError on any network, parse, or regex error.
    """
    req = urllib.request.Request(
        GITHUB_CONTENTS_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ppk2-python",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except HTTPError as e:
        raise UpstreamFetchError(
            f"GitHub API returned HTTP {e.code}: {e.reason}"
        ) from e
    except (URLError, TimeoutError) as e:
        raise UpstreamFetchError(f"network error fetching upstream: {e}") from e

    try:
        entries = json.loads(body)
    except json.JSONDecodeError as e:
        raise UpstreamFetchError(f"unparseable GitHub API response: {e}") from e

    hex_entry = next(
        (e for e in entries
         if isinstance(e, dict) and e.get("name", "").endswith(".hex")),
        None,
    )
    if hex_entry is None:
        raise UpstreamFetchError("no .hex file found in firmware/ directory")

    filename = hex_entry["name"]
    m = _HEX_FILENAME_RE.match(filename)
    if m is None:
        raise UpstreamFetchError(
            f"upstream hex filename did not match expected pattern: {filename}"
        )

    url = hex_entry.get("download_url") or (
        "https://raw.githubusercontent.com/NordicSemiconductor/"
        f"pc-nrfconnect-ppk/main/firmware/{filename}"
    )

    return UpstreamFirmware(
        semver=m.group("semver"),
        commit=m.group("commit"),
        filename=filename,
        url=url,
    )


def download_upstream(
    dest_dir: Path | None = None,
    *,
    timeout: float = 60.0,
) -> tuple[Path, UpstreamFirmware]:
    """Download the latest upstream .hex to dest_dir (tempdir if None).

    Returns (hex_path, UpstreamFirmware). Raises UpstreamFetchError.
    """
    upstream = fetch_upstream()
    if dest_dir is None:
        dest_dir = Path(tempfile.mkdtemp(prefix="ppk2-firmware-"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / upstream.filename

    try:
        with urllib.request.urlopen(upstream.url, timeout=timeout) as resp:
            data = resp.read()
    except (HTTPError, URLError, TimeoutError) as e:
        raise UpstreamFetchError(f"failed to download {upstream.url}: {e}") from e

    dest_path.write_bytes(data)
    return dest_path, upstream


def _pid_path_exists_and_alive(serial_number: str) -> bool:
    from . import daemon
    pid_file = daemon.pid_path(serial_number)
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return False
    return daemon.pid_alive(pid)


def upgrade(
    serial_number: str,
    hex_path: Path,
    *,
    timeout: float = 180.0,
) -> FirmwareInfo:
    """Flash hex_path onto the PPK2 with the given serial via
    `nrfutil device program`, then query and return the new FirmwareInfo.

    Raises:
        FirmwareUpgradeError: if a daemon is running for this serial,
            if nrfutil fails, or if post-flash verification fails.
        FirmwareToolMissing: if nrfutil is not installed.
    """
    if _pid_path_exists_and_alive(serial_number):
        raise FirmwareUpgradeError(
            f"a ppk2 daemon is running for serial {serial_number}; "
            "stop it (kill the daemon process) before upgrading"
        )

    hex_path = Path(hex_path)
    if not hex_path.is_file():
        raise FirmwareUpgradeError(f"hex file not found: {hex_path}")

    cmd = [
        "nrfutil", "device", "program",
        "--firmware", str(hex_path),
        "--serial-number", serial_number,
        "--traits", "nordicUsb",
        "--options", "reset=RESET_SYSTEM",
        "--json",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as e:
        raise FirmwareToolMissing(_tool_install_hint()) from e

    if result.returncode != 0:
        raise FirmwareUpgradeError(
            f"nrfutil program exited {result.returncode}: {result.stderr.strip()}"
        )

    try:
        return query(serial_number=serial_number)
    except FirmwareQueryError as e:
        raise FirmwareUpgradeError(
            f"flash completed but post-flash verification failed: {e}"
        ) from e
