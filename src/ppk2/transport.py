"""Serial transport abstraction for PPK2.

Provides a clean interface that can be backed by pyserial (native)
or potentially Web Serial (browser/WASI) in the future.
"""

import logging
from abc import ABC, abstractmethod

import serial
import serial.tools.list_ports

logger = logging.getLogger(__name__)

# Nordic PPK2 USB identifiers
NORDIC_VID = 0x1915
PPK2_PID = 0xC00A
PPK2_BAUD = 115200


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


def list_ppk2_devices() -> list[str]:
    """Find all connected PPK2 devices by USB VID/PID.

    Returns a list of serial port paths.
    """
    ports = []
    for port in serial.tools.list_ports.comports():
        if port.vid == NORDIC_VID and port.pid == PPK2_PID:
            ports.append(port.device)
    return sorted(ports)
