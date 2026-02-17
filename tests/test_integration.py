"""Integration tests for PPK2 hardware.

These tests require a physical PPK2 device connected via USB.
Run with: PPK2_VDD_MV=4200 pytest tests/test_integration.py -v

Skipped unless PPK2_VDD_MV is set (ensures intentional hardware testing).

Environment variables:
    PPK2_VDD_MV       Max source voltage in mV (required — e.g. 4200 for LiPo)
    PPK2_VDD_MIN_MV   Min source voltage in mV (default: 3000, typical LiPo cutoff)
    PPK2_PORT         Serial port (auto-discover if unset)
"""

import os
import time

import pytest

from ppk2.device import PPK2Device, SAMPLE_RATE_HZ
from ppk2.transport import NORDIC_VID, PPK2_PID, list_ppk2_devices

# Require explicit opt-in via PPK2_VDD_MV to avoid accidental hardware damage
_vdd_str = os.environ.get("PPK2_VDD_MV")
_devices = list_ppk2_devices()
_port = os.environ.get("PPK2_PORT") or (_devices[0].port if _devices else None)

if _vdd_str is None:
    pytestmark = pytest.mark.skipif(True, reason="PPK2_VDD_MV not set")
    _vdd_mv = 0
elif _port is None:
    pytestmark = pytest.mark.skipif(True, reason="No PPK2 device connected")
    _vdd_mv = 0
else:
    pytestmark = []
    _vdd_mv = int(_vdd_str)

_vdd_min_mv = int(os.environ.get("PPK2_VDD_MIN_MV", "3000"))


@pytest.fixture(scope="module")
def ppk():
    """Open a PPK2 device for the entire test module."""
    device = PPK2Device.open(_port)
    device.use_source_meter()
    device.set_source_voltage(_vdd_mv)
    yield device
    if device._transport.is_open:
        device.toggle_dut_power(False)
        device.close()


class TestDiscovery:
    def test_list_devices_finds_ppk2(self):
        devices = list_ppk2_devices()
        assert len(devices) >= 1
        d = devices[0]
        assert d.port
        assert len(d.serial_number) == 8

    def test_list_devices_filters_auxiliary_port(self):
        """Only one port per physical device should be returned."""
        import serial.tools.list_ports

        all_ppk2 = [
            p for p in serial.tools.list_ports.comports()
            if p.vid == NORDIC_VID and p.pid == PPK2_PID
        ]
        devices = list_ppk2_devices()
        # If there are 2 raw ports but 1 device, filtering works
        if len(all_ppk2) > len(devices):
            assert len(devices) < len(all_ppk2)
        else:
            # Windows or single-port scenario
            assert len(devices) == len(all_ppk2)


class TestConnection:
    def test_connect_reads_metadata(self, ppk):
        assert ppk.metadata is not None
        assert "vdd" in ppk.metadata
        assert "mode" in ppk.metadata
        assert "hw" in ppk.metadata

    def test_metadata_has_calibration(self, ppk):
        for prefix in ("r", "gs", "gi", "o", "s", "i", "ug"):
            for idx in range(5):
                assert f"{prefix}{idx}" in ppk.metadata

    def test_modifiers_populated(self, ppk):
        m = ppk.modifiers
        # Resistor values should be positive and decreasing across ranges
        assert m.r[0] > m.r[1] > m.r[2] > m.r[3] > m.r[4] > 0
        # User gains should be close to 1.0
        for g in m.ug:
            assert 0.9 <= g <= 1.1


class TestPowerControl:
    def test_source_meter_mode(self, ppk):
        ppk.use_source_meter()

    def test_ampere_meter_mode(self, ppk):
        ppk.use_ampere_meter()
        # Switch back to source for remaining tests
        ppk.use_source_meter()

    def test_set_voltage(self, ppk):
        ppk.set_source_voltage(_vdd_mv)
        assert ppk.vdd_mv == _vdd_mv

    def test_set_voltage_out_of_range(self, ppk):
        with pytest.raises(ValueError):
            ppk.set_source_voltage(500)
        with pytest.raises(ValueError):
            ppk.set_source_voltage(6000)

    def test_dut_power_toggle(self, ppk):
        ppk.toggle_dut_power(True)
        time.sleep(0.1)
        ppk.toggle_dut_power(False)


class TestMeasurement:
    """Core measurement tests at full 100 kHz sample rate."""

    def test_short_measurement(self, ppk):
        """1-second capture at full rate."""
        ppk.toggle_dut_power(True)
        time.sleep(0.5)

        result = ppk.measure(duration_s=1.0)

        ppk.toggle_dut_power(False)

        assert result.sample_count > 0
        assert result.duration_s == 1.0
        # At 100 kHz, 1 second should yield ~100k samples
        # Allow 50% tolerance for USB scheduling
        assert result.sample_count > SAMPLE_RATE_HZ * 0.5
        assert result.lost_samples < result.sample_count * 0.01  # <1% loss

    def test_sustained_high_rate_capture(self, ppk):
        """5-second sustained capture — stress test for USB throughput."""
        ppk.toggle_dut_power(True)
        time.sleep(0.5)

        result = ppk.measure(duration_s=5.0)

        ppk.toggle_dut_power(False)

        expected = SAMPLE_RATE_HZ * 5
        assert result.sample_count > expected * 0.9  # >90% capture rate
        assert result.lost_samples < expected * 0.01  # <1% data loss
        assert result.mean_ua > 0
        assert result.max_ua >= result.mean_ua
        assert result.min_ua <= result.mean_ua
        assert result.p99_ua > 0

    def test_max_rate_10_second_capture(self, ppk):
        """10-second capture — full throughput endurance test.

        At 100 kHz x 4 bytes/sample = 400 KB/s sustained USB throughput.
        """
        ppk.toggle_dut_power(True)
        time.sleep(0.5)

        result = ppk.measure(duration_s=10.0)

        ppk.toggle_dut_power(False)

        expected = SAMPLE_RATE_HZ * 10  # 1,000,000 samples
        assert result.sample_count > expected * 0.95  # >95% capture rate
        assert result.lost_samples < expected * 0.005  # <0.5% loss
        print(f"\n  10s capture: {result.sample_count:,} samples, "
              f"{result.lost_samples} lost, "
              f"mean={result.mean_ua:.1f} uA")

    def test_measurement_without_spike_filter(self, ppk):
        ppk.toggle_dut_power(True)
        time.sleep(0.5)

        filtered = ppk.measure(duration_s=1.0, spike_filter=True)
        unfiltered = ppk.measure(duration_s=1.0, spike_filter=False)

        ppk.toggle_dut_power(False)

        assert filtered.sample_count > 0
        assert unfiltered.sample_count > 0
        assert filtered.mean_ua > 0
        assert unfiltered.mean_ua > 0

    def test_measurement_current_range(self, ppk):
        """Verify measured values are physically plausible."""
        ppk.toggle_dut_power(True)
        time.sleep(0.5)

        result = ppk.measure(duration_s=2.0)

        ppk.toggle_dut_power(False)

        # A typical DUT draws between 1 uA and 500 mA
        assert result.mean_ua > 1.0, "Mean current suspiciously low"
        assert result.mean_ua < 500_000, "Mean current suspiciously high"
        assert result.max_ua < 1_000_000, "Peak current exceeds PPK2 range"


class TestMeasurementModes:
    def test_source_mode_with_voltage_sweep(self, ppk):
        """Measure at two voltages — both should produce valid data."""
        results = {}
        ppk.toggle_dut_power(True)

        # Sweep from LiPo cutoff to full charge
        voltages = [_vdd_min_mv, _vdd_mv]
        for mv in voltages:
            ppk.set_source_voltage(mv)
            time.sleep(0.5)
            results[mv] = ppk.measure(duration_s=1.0)

        ppk.toggle_dut_power(False)
        ppk.set_source_voltage(_vdd_mv)

        for mv, r in results.items():
            assert r.sample_count > 0, f"No samples at {mv} mV"
            assert r.mean_ua > 0, f"Zero current at {mv} mV"


class TestStartStop:
    """Test manual start/stop measurement control."""

    def test_manual_start_stop(self, ppk):
        ppk.toggle_dut_power(True)
        time.sleep(0.3)

        ppk.start_measuring()
        time.sleep(0.5)
        ppk.stop_measuring()

        ppk.toggle_dut_power(False)

    def test_repeated_start_stop(self, ppk):
        """Start and stop measurement 5 times in succession."""
        ppk.toggle_dut_power(True)
        time.sleep(0.3)

        for _ in range(5):
            result = ppk.measure(duration_s=0.2)
            assert result.sample_count > 0

        ppk.toggle_dut_power(False)


class TestDigitalChannels:
    def test_digital_channel_values_valid(self, ppk):
        """Digital channel bits should be valid 8-bit values."""
        ppk.toggle_dut_power(True)
        time.sleep(0.3)

        result = ppk.measure(duration_s=0.5)

        ppk.toggle_dut_power(False)

        for s in result.samples[:1000]:
            assert 0 <= s.logic <= 0xFF


class TestSaveCapture:
    def test_measure_and_save(self, ppk, tmp_path):
        """Capture and save to .ppk2 file."""
        from ppk2.ppk2file import load_ppk2, save_ppk2

        ppk.toggle_dut_power(True)
        time.sleep(0.3)

        result = ppk.measure(duration_s=1.0)

        ppk.toggle_dut_power(False)

        out = tmp_path / "test_capture.ppk2"
        save_ppk2(result, str(out))
        assert out.exists()
        assert out.stat().st_size > 0

        # Round-trip: load and verify
        loaded = load_ppk2(str(out))
        assert loaded.sample_count == result.sample_count
        assert abs(loaded.mean_ua - result.mean_ua) < 0.01


class TestReconnect:
    """Test device reconnection. Must run last — closes the module fixture."""

    def test_reconnect_after_close(self, ppk):
        """Close and reopen the device cleanly."""
        ppk.toggle_dut_power(False)
        ppk.close()

        time.sleep(0.5)  # allow USB port to settle
        dev = PPK2Device.open(_port)
        try:
            assert dev.metadata is not None
            dev.use_source_meter()
            dev.set_source_voltage(_vdd_mv)
            dev.toggle_dut_power(True)
            time.sleep(0.3)

            result = dev.measure(duration_s=0.5)
            assert result.sample_count > 0

            dev.toggle_dut_power(False)
        finally:
            dev.close()
