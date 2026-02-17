"""Mock transport for testing PPK2Device without hardware.

Simulates a PPK2 device that responds to commands with realistic
metadata and measurement data streams.
"""

import struct
import time
from collections import deque

from .commands import AVERAGE_START, AVERAGE_STOP, GET_METADATA
from .transport import Transport


def make_metadata_response(
    vdd: int = 3700,
    mode: int = 2,
    hw: int = 9,
    r: list[float] | None = None,
) -> bytes:
    """Build a metadata text response like a real PPK2 would send."""
    if r is None:
        r = [1031.64, 101.65, 10.15, 0.94, 0.043]
    lines = []
    for i in range(5):
        lines.append(f"R{i}: {r[i]}")
    for prefix, default in [("GS", 1.0), ("GI", 1.0), ("O", 0.0),
                            ("S", 0.0), ("I", 0.0), ("UG", 1.0)]:
        for i in range(5):
            lines.append(f"{prefix}{i}: {default}")
    lines.append(f"VDD: {vdd}")
    lines.append(f"Mode: {mode}")
    lines.append(f"HW: {hw}")
    lines.append("END")
    return "\n".join(lines).encode("ascii")


def make_sample_frame(
    adc: int, range_idx: int, counter: int, logic: int = 0
) -> bytes:
    """Pack a single PPK2 measurement frame (4 bytes LE)."""
    raw = (
        (adc & 0x3FFF)
        | ((range_idx & 0x7) << 14)
        | ((counter & 0x3F) << 18)
        | ((logic & 0xFF) << 24)
    )
    return struct.pack("<I", raw)


def make_sample_stream(
    n_samples: int,
    adc: int = 1000,
    range_idx: int = 2,
    logic: int = 0,
) -> bytes:
    """Build a stream of n measurement frames with incrementing counters."""
    frames = []
    for i in range(n_samples):
        frames.append(make_sample_frame(adc, range_idx, i & 0x3F, logic))
    return b"".join(frames)


class MockTransport(Transport):
    """Mock transport that simulates PPK2 responses.

    Responds to GetMetadata with canned calibration data, and to
    AverageStart by making sample data available via read_available().

    Usage:
        mock = MockTransport()
        device = PPK2Device(mock)
        device._connect()
        # device is now initialized with mock calibration data
    """

    def __init__(
        self,
        metadata: bytes | None = None,
        sample_adc: int = 1000,
        sample_range: int = 2,
        sample_logic: int = 0,
    ):
        self._metadata = metadata or make_metadata_response()
        self._sample_adc = sample_adc
        self._sample_range = sample_range
        self._sample_logic = sample_logic

        self._is_open = False
        self._measuring = False
        self._read_buffer = deque[bytes]()
        self._write_log: list[bytes] = []
        self._sample_counter = 0

    def open(self) -> None:
        self._is_open = True

    def close(self) -> None:
        self._is_open = False
        self._measuring = False

    def write(self, data: bytes) -> None:
        if not self._is_open:
            raise ConnectionError("Not open")
        self._write_log.append(data)

        if data[0] == GET_METADATA:
            self._read_buffer.append(self._metadata)
        elif data[0] == AVERAGE_START:
            self._measuring = True
            self._sample_counter = 0
        elif data[0] == AVERAGE_STOP:
            self._measuring = False

    def read(self, size: int, timeout: float | None = None) -> bytes:
        if not self._is_open:
            raise ConnectionError("Not open")
        if self._read_buffer:
            data = self._read_buffer.popleft()
            return data[:size]
        return b""

    def read_available(self) -> bytes:
        if not self._is_open:
            raise ConnectionError("Not open")

        # Return buffered data first (e.g. metadata leftovers)
        if self._read_buffer:
            return self._read_buffer.popleft()

        # If measuring, generate a batch of samples
        if self._measuring:
            batch_size = 100  # ~1ms of data at 100kHz
            frames = []
            for _ in range(batch_size):
                frames.append(make_sample_frame(
                    self._sample_adc,
                    self._sample_range,
                    self._sample_counter & 0x3F,
                    self._sample_logic,
                ))
                self._sample_counter += 1
            return b"".join(frames)

        return b""

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def write_log(self) -> list[bytes]:
        """All commands sent to the device, in order."""
        return self._write_log

    def inject_samples(self, data: bytes) -> None:
        """Inject raw sample data to be returned by next read_available()."""
        self._read_buffer.append(data)
