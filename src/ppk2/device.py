"""PPK2 device interface.

Provides high-level control of the Nordic Power Profiler Kit II:
source meter mode, voltage control, DUT power, and current measurement.
"""

import logging
import time

from . import commands
from .conversion import SpikeFilter, adc_to_microamps
from .parser import SampleParser, parse_metadata
from .transport import PPK2Port, SerialTransport, Transport, list_ppk2_devices
from .types import MeasurementResult, Modifiers, Sample

logger = logging.getLogger(__name__)

# Hardware limits
VDD_MIN = 800
VDD_MAX = 5000
SAMPLE_RATE_HZ = 100_000
SAMPLE_PERIOD_US = 10


class PPK2Device:
    """High-level interface to a Nordic PPK2.

    Usage:
        with PPK2Device.open() as ppk:
            ppk.use_source_meter()
            ppk.set_source_voltage(3700)
            ppk.toggle_dut_power(True)
            result = ppk.measure(duration_s=5.0)
            print(f"Mean: {result.mean_ua:.1f} uA")
    """

    def __init__(self, transport: Transport):
        self._transport = transport
        self._modifiers = Modifiers()
        self._vdd_mv = 3700
        self._spike_filter = SpikeFilter()
        self._parser = SampleParser()
        self._is_measuring = False
        self._metadata: dict | None = None

    @classmethod
    def open(cls, port: str | None = None) -> "PPK2Device":
        """Open a PPK2 device.

        Args:
            port: Serial port path. If None, auto-discovers the first PPK2.

        Returns:
            An initialized PPK2Device (use as context manager).
        """
        if port is None:
            devices = list_ppk2_devices()
            if not devices:
                raise ConnectionError("No PPK2 device found")
            port = devices[0].port
            logger.info("Auto-discovered PPK2 at %s", port)

        transport = SerialTransport(port)
        device = cls(transport)
        device._connect()
        return device

    def __enter__(self) -> "PPK2Device":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        """Close the serial connection.

        Leaves the PPK2 in its current state (power, mode, voltage).
        To explicitly power down the DUT before closing, call
        ``toggle_dut_power(False)`` first.
        """
        if self._is_measuring:
            self.stop_measuring()
        if self._transport.is_open:
            self._transport.close()

    @property
    def modifiers(self) -> Modifiers:
        return self._modifiers

    @property
    def metadata(self) -> dict | None:
        return self._metadata

    @property
    def vdd_mv(self) -> int:
        return self._vdd_mv

    # --- Setup commands ---

    def use_source_meter(self) -> None:
        """Set PPK2 to source meter mode (PPK2 provides power to DUT)."""
        self._send(commands.set_power_mode(source_meter=True))

    def use_ampere_meter(self) -> None:
        """Set PPK2 to ampere meter mode (external power supply)."""
        self._send(commands.set_power_mode(source_meter=False))

    def set_source_voltage(self, vdd_mv: int) -> None:
        """Set source voltage in millivolts (800-5000)."""
        if not VDD_MIN <= vdd_mv <= VDD_MAX:
            raise ValueError(
                f"VDD must be {VDD_MIN}-{VDD_MAX} mV, got {vdd_mv}"
            )
        self._vdd_mv = vdd_mv
        self._send(commands.regulator_set(vdd_mv))

    def toggle_dut_power(self, on: bool) -> None:
        """Toggle DUT power on/off (source meter mode only)."""
        self._send(commands.device_running_set(on))

    def set_user_gain(self, range_idx: int, gain: float) -> None:
        """Set user gain for a specific measurement range (0-4)."""
        if not 0 <= range_idx <= 4:
            raise ValueError(f"Range must be 0-4, got {range_idx}")
        self._modifiers.ug[range_idx] = gain
        self._send(commands.set_user_gains(range_idx, gain))

    # --- Measurement ---

    def start_measuring(self) -> None:
        """Begin continuous measurement streaming."""
        self._spike_filter.reset()
        self._parser.reset()
        self._send(commands.average_start())
        self._is_measuring = True

    def stop_measuring(self) -> None:
        """Stop measurement streaming."""
        self._send(commands.average_stop())
        self._is_measuring = False

    def measure(
        self, duration_s: float, spike_filter: bool = True
    ) -> MeasurementResult:
        """Take a measurement for the specified duration.

        Args:
            duration_s: Measurement duration in seconds.
            spike_filter: Apply spike filter for range-switching smoothing.

        Returns:
            MeasurementResult with statistics.
        """
        self.start_measuring()
        time.sleep(0.05)  # let stream stabilize

        samples: list[Sample] = []
        lost = 0
        deadline = time.monotonic() + duration_s

        while time.monotonic() < deadline:
            raw = self._transport.read_available()
            if not raw:
                time.sleep(0.01)
                continue

            parsed = self._parser.feed(raw)
            for frame in parsed:
                if frame is None:
                    lost += 1
                    continue
                adc_raw, range_idx, counter, logic = frame
                current = adc_to_microamps(
                    adc_raw, range_idx, self._modifiers, self._vdd_mv
                )
                if spike_filter:
                    current = self._spike_filter.process(current, range_idx)
                samples.append(
                    Sample(
                        current_ua=current,
                        range=range_idx,
                        logic=logic,
                        counter=counter,
                    )
                )

        self.stop_measuring()

        return MeasurementResult(
            samples=samples,
            duration_s=duration_s,
            sample_count=len(samples),
            lost_samples=lost,
        )

    def wait_for_digital(
        self, channel: int, level: bool, timeout_s: float = 10.0
    ) -> bool:
        """Wait until a digital channel reaches the specified level.

        Args:
            channel: Digital channel number (0-7).
            level: True for high, False for low.
            timeout_s: Maximum wait time.

        Returns:
            True if the level was reached, False on timeout.
        """
        if not 0 <= channel <= 7:
            raise ValueError(f"Channel must be 0-7, got {channel}")

        mask = 1 << channel
        deadline = time.monotonic() + timeout_s

        while time.monotonic() < deadline:
            raw = self._transport.read_available()
            if not raw:
                time.sleep(0.01)
                continue

            parsed = self._parser.feed(raw)
            for frame in parsed:
                if frame is None:
                    continue
                _, _, _, logic = frame
                pin_high = bool(logic & mask)
                if pin_high == level:
                    return True

        return False

    # --- Internal ---

    def _connect(self) -> None:
        """Open transport and read device metadata."""
        self._transport.open()

        # Stop any in-progress measurement and drain stale data.
        # The PPK2 may still be streaming from a previous session.
        self._send(commands.average_stop())
        time.sleep(0.1)
        self._transport.read_available()

        self._send(commands.get_metadata())
        self._metadata = self._read_metadata()
        self._modifiers.update_from_metadata(self._metadata)

        vdd = self._metadata.get("vdd", 3700)
        self._vdd_mv = int(vdd)
        self._send(commands.regulator_set(self._vdd_mv))

        self._validate_gains()

        logger.info(
            "PPK2 connected: VDD=%dmV, mode=%s, HW=%s",
            self._vdd_mv,
            self._metadata.get("mode", "?"),
            self._metadata.get("hw", "?"),
        )

    def _read_metadata(self) -> dict:
        """Read and parse metadata response after GetMetadata command."""
        text = ""
        deadline = time.monotonic() + 5.0

        while time.monotonic() < deadline:
            chunk = self._transport.read(1024, timeout=1.0)
            if chunk:
                text += chunk.decode("ascii", errors="replace")
                if "END" in text:
                    return parse_metadata(text)

        raise TimeoutError("Timeout waiting for PPK2 metadata response")

    def _validate_gains(self) -> None:
        """Reset any out-of-range user gains to 1.0 (matches Nordic behavior)."""
        for idx in range(5):
            gain = self._modifiers.ug[idx]
            if gain < 0.9 or gain > 1.1:
                logger.warning(
                    "User gain[%d] = %.3f out of range, resetting to 1.0", idx, gain
                )
                self.set_user_gain(idx, 1.0)

    def _send(self, cmd: bytes) -> None:
        """Send a command to the PPK2."""
        self._transport.write(cmd)
