"""Per-user state directory, platform-aware.

Files keyed by device serial number live under :func:`state_dir`:

- ``<SERIAL>.sock``      Unix socket path / endpoint marker
- ``<SERIAL>.endpoint``  Windows TCP endpoint (text: ``host:port``)
- ``<SERIAL>.pid``       Daemon process PID
- ``<SERIAL>.log``       Daemon stdout/stderr log
- ``<SERIAL>.spawn.lock`` Auto-spawn flock file
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Parametrising constant — the single place to change when extracting.
STATE_DIR_NAME = "ppk2"


def state_dir() -> Path:
    """Return the per-user state directory, creating it if needed."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            base = str(Path.home() / "AppData" / "Local")
        d = Path(base) / STATE_DIR_NAME
    else:
        d = Path.home() / ".local" / "state" / STATE_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d
