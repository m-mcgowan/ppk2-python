"""Serial transport abstraction for PPK2.

Provides a clean interface that can be backed by pyserial (native)
or potentially Web Serial (browser/WASI) in the future.
"""

import logging
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass

import serial
import serial.tools.list_ports

logger = logging.getLogger(__name__)

# Nordic PPK2 USB identifiers
NORDIC_VID = 0x1915
PPK2_PID = 0xC00A
PPK2_BAUD = 115200


@dataclass
class PPK2Port:
    """Information about a discovered PPK2 device."""

    port: str
    serial_number: str
    location: str


class Transport(ABC):
    """Abstract serial transport interface."""

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def write(self, data: bytes) -> None: ...

    @abstractmethod
    def read(self, size: int, timeout: float | None = None) -> bytes: ...

    @abstractmethod
    def read_available(self) -> bytes: ...

    @property
    @abstractmethod
    def is_open(self) -> bool: ...


class SerialTransport(Transport):
    """pyserial-backed transport for PPK2."""

    def __init__(self, port: str, baud: int = PPK2_BAUD):
        self._port_name = port
        self._baud = baud
        self._serial: serial.Serial | None = None

    def open(self) -> None:
        self._serial = serial.Serial(
            self._port_name, self._baud, timeout=1.0
        )
        logger.info("Opened %s at %d baud", self._port_name, self._baud)

    def close(self) -> None:
        if self._serial and self._serial.is_open:
            self._serial.close()
            logger.info("Closed %s", self._port_name)
        self._serial = None

    def write(self, data: bytes) -> None:
        if not self._serial or not self._serial.is_open:
            raise ConnectionError("Serial port not open")
        self._serial.write(data)

    def read(self, size: int, timeout: float | None = None) -> bytes:
        if not self._serial or not self._serial.is_open:
            raise ConnectionError("Serial port not open")
        old_timeout = self._serial.timeout
        if timeout is not None:
            self._serial.timeout = timeout
        try:
            return self._serial.read(size)
        finally:
            self._serial.timeout = old_timeout

    def read_available(self) -> bytes:
        if not self._serial or not self._serial.is_open:
            raise ConnectionError("Serial port not open")
        available = self._serial.in_waiting
        if available > 0:
            return self._serial.read(available)
        return b""

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open


def list_ppk2_devices() -> list[PPK2Port]:
    """Find all connected PPK2 devices by USB VID/PID.

    The PPK2 enumerates two CDC ACM interfaces per device. The data/command
    port is the first (lower-numbered) interface. Filtering strategy:

    - **Linux**: USB location includes interface number; data port ends in '1'.
    - **macOS**: Both ports share the same location; we group by serial number
      and pick the lowest-numbered /dev/cu.* port per device.
    - **Windows**: Only one port is typically visible; no filtering needed.

    Returns one PPK2Port per physical device.
    """
    # Collect all matching ports
    all_ports: list[tuple[str, str, str]] = []  # (device, serial, location)
    for port in serial.tools.list_ports.comports():
        if port.vid != NORDIC_VID or port.pid != PPK2_PID:
            continue
        all_ports.append((
            port.device,
            (port.serial_number or "")[:8],
            port.location or "",
        ))

    if not all_ports:
        return []

    # On Linux, filter by location ending in '1' (interface number)
    if sys.platform == "linux":
        filtered = [
            (dev, sn, loc) for dev, sn, loc in all_ports
            if loc.endswith("1")
        ]
        if filtered:
            all_ports = filtered

    # On macOS (and as fallback), group by serial number and pick the
    # lowest-numbered port per device (the data/command interface).
    elif len(all_ports) > 1:
        by_serial: dict[str, list[tuple[str, str, str]]] = {}
        for dev, sn, loc in all_ports:
            by_serial.setdefault(sn, []).append((dev, sn, loc))
        all_ports = [
            sorted(group, key=lambda x: x[0])[0]
            for group in by_serial.values()
        ]

    return sorted(
        [PPK2Port(port=dev, serial_number=sn, location=loc)
         for dev, sn, loc in all_ports],
        key=lambda d: d.port,
    )
