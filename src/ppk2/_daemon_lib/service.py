"""Cross-platform per-device service management.

One service per device, keyed by serial number. Each platform manager
shells to its native service supervisor (``launchctl`` / ``systemctl`` /
NSSM).

Parametrising constants (``LABEL_PREFIX``, ``_program_args``) live at
module top so a consumer can override them after import.
"""

from __future__ import annotations

import logging
import os
import plistlib
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path

from .state import state_dir

logger = logging.getLogger(__name__)

LABEL_PREFIX = "com.ppk2.daemon"


def _program_args(serial: str) -> list[str]:
    """Argv tail for ProgramArguments / nssm command (after the python prefix)."""
    return ["-m", "ppk2", "daemon", "run", "--serial", serial]


def _resolve_python() -> str:
    """Find the best Python interpreter to launch the daemon with.

    Prefer the ppk2-python project's ``.venv/bin/python3`` if present
    (mirrors how ``usb-device/hub_agent.py`` does it); fall back to
    ``sys.executable``.
    """
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    venv_python = project_root / ".venv" / "bin" / "python3"
    if venv_python.is_file():
        return str(venv_python)
    return sys.executable


class ServiceManager(ABC):
    """Abstract per-device service manager."""

    @abstractmethod
    def install(self, serial: str) -> None: ...
    @abstractmethod
    def uninstall(self, serial: str) -> None: ...
    @abstractmethod
    def start(self, serial: str) -> None: ...
    @abstractmethod
    def stop(self, serial: str) -> None: ...
    @abstractmethod
    def is_installed(self, serial: str) -> bool: ...
    @abstractmethod
    def is_running(self, serial: str) -> bool: ...
    @abstractmethod
    def list_installed(self) -> list[str]: ...
    @abstractmethod
    def log_path(self, serial: str) -> Path: ...


class LaunchdManager(ServiceManager):
    """macOS launchd LaunchAgent. One plist per device."""

    PLIST_DIR = Path.home() / "Library" / "LaunchAgents"

    def _label(self, serial: str) -> str:
        return f"{LABEL_PREFIX}.{serial}"

    def _plist_path(self, serial: str) -> Path:
        return self.PLIST_DIR / f"{self._label(serial)}.plist"

    def log_path(self, serial: str) -> Path:
        return state_dir() / f"{serial}.log"

    def install(self, serial: str) -> None:
        program_args = [_resolve_python(), "-u", *_program_args(serial)]
        plist = {
            "Label": self._label(serial),
            "ProgramArguments": program_args,
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "StandardOutPath": str(self.log_path(serial)),
            "StandardErrorPath": str(self.log_path(serial)),
        }
        self.PLIST_DIR.mkdir(parents=True, exist_ok=True)
        plist_path = self._plist_path(serial)
        with open(plist_path, "wb") as f:
            plistlib.dump(plist, f)

        domain = f"gui/{os.getuid()}"
        # Idempotent: bootout first (in case it's already loaded), then bootstrap
        subprocess.run(
            ["launchctl", "bootout", domain, str(plist_path)],
            check=False, capture_output=True,
        )
        subprocess.run(
            ["launchctl", "bootstrap", domain, str(plist_path)],
            check=False, capture_output=True,
        )

    def uninstall(self, serial: str) -> None:
        plist_path = self._plist_path(serial)
        if not plist_path.exists():
            return
        domain = f"gui/{os.getuid()}"
        subprocess.run(
            ["launchctl", "bootout", domain, str(plist_path)],
            check=False, capture_output=True,
        )
        plist_path.unlink()

    def start(self, serial: str) -> None:
        plist_path = self._plist_path(serial)
        if not plist_path.exists():
            raise FileNotFoundError(f"Service not installed for {serial}")
        domain = f"gui/{os.getuid()}"
        subprocess.run(
            ["launchctl", "bootstrap", domain, str(plist_path)],
            check=False, capture_output=True,
        )

    def stop(self, serial: str) -> None:
        plist_path = self._plist_path(serial)
        if not plist_path.exists():
            return
        domain = f"gui/{os.getuid()}"
        subprocess.run(
            ["launchctl", "bootout", domain, str(plist_path)],
            check=False, capture_output=True,
        )

    def is_installed(self, serial: str) -> bool:
        return self._plist_path(serial).exists()

    def is_running(self, serial: str) -> bool:
        rc = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{self._label(serial)}"],
            check=False, capture_output=True,
        ).returncode
        return rc == 0

    def list_installed(self) -> list[str]:
        if not self.PLIST_DIR.is_dir():
            return []
        prefix = f"{LABEL_PREFIX}."
        result = []
        for p in self.PLIST_DIR.glob(f"{prefix}*.plist"):
            result.append(p.stem.removeprefix(prefix))
        return result


class SystemdManager(ServiceManager):
    """Linux systemd user-unit stub. Implementation deferred."""

    def install(self, serial: str) -> None:
        raise NotImplementedError("systemd support not yet implemented")
    def uninstall(self, serial: str) -> None:
        raise NotImplementedError("systemd support not yet implemented")
    def start(self, serial: str) -> None:
        raise NotImplementedError("systemd support not yet implemented")
    def stop(self, serial: str) -> None:
        raise NotImplementedError("systemd support not yet implemented")
    def is_installed(self, serial: str) -> bool:
        return False
    def is_running(self, serial: str) -> bool:
        return False
    def list_installed(self) -> list[str]:
        return []
    def log_path(self, serial: str) -> Path:
        return state_dir() / f"{serial}.log"


class WinServiceManager(ServiceManager):
    """Windows NSSM-based service stub. Implementation deferred."""

    def install(self, serial: str) -> None:
        raise NotImplementedError("Windows service support not yet implemented (will use NSSM)")
    def uninstall(self, serial: str) -> None:
        raise NotImplementedError("Windows service support not yet implemented")
    def start(self, serial: str) -> None:
        raise NotImplementedError("Windows service support not yet implemented")
    def stop(self, serial: str) -> None:
        raise NotImplementedError("Windows service support not yet implemented")
    def is_installed(self, serial: str) -> bool:
        return False
    def is_running(self, serial: str) -> bool:
        return False
    def list_installed(self) -> list[str]:
        return []
    def log_path(self, serial: str) -> Path:
        return state_dir() / f"{serial}.log"


def get_service_manager() -> ServiceManager | None:
    """Return the platform-appropriate :class:`ServiceManager`, or None."""
    if sys.platform == "darwin":
        return LaunchdManager()
    if sys.platform == "linux":
        return SystemdManager()
    if sys.platform == "win32":
        return WinServiceManager()
    return None
