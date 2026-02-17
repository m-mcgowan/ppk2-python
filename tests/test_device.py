"""End-to-end tests for PPK2Device using MockTransport."""

import pytest

from ppk2.commands import (
    AVERAGE_START,
    AVERAGE_STOP,
    DEVICE_RUNNING_SET,
    GET_METADATA,
    REGULATOR_SET,
    SET_POWER_MODE,
)
from ppk2.device import PPK2Device
from ppk2.mock import MockTransport, make_metadata_response, make_sample_stream


class TestDeviceConnect:
    def test_connect_reads_metadata(self):
        mock = MockTransport()
        device = PPK2Device(mock)
        device._connect()

        assert device.metadata is not None
        assert device.metadata["vdd"] == 3700
        assert device.metadata["mode"] == 2
        assert device.metadata["hw"] == 9

    def test_connect_applies_calibration(self):
        meta = make_metadata_response(r=[1000.0, 100.0, 10.0, 1.0, 0.05])
        mock = MockTransport(metadata=meta)
        device = PPK2Device(mock)
        device._connect()

        assert device.modifiers.r[0] == 1000.0
        assert device.modifiers.r[4] == 0.05

    def test_connect_sets_regulator_from_metadata(self):
        meta = make_metadata_response(vdd=4200)
        mock = MockTransport(metadata=meta)
        device = PPK2Device(mock)
        device._connect()

        assert device.vdd_mv == 4200
        # Check that RegulatorSet was sent
        reg_cmds = [c for c in mock.write_log if c[0] == REGULATOR_SET]
        assert len(reg_cmds) >= 1
        last = reg_cmds[-1]
        assert (last[1] << 8 | last[2]) == 4200

    def test_connect_sends_stop_then_metadata(self):
        mock = MockTransport()
        device = PPK2Device(mock)
        device._connect()

        # First command stops any in-progress measurement, then requests metadata
        assert mock.write_log[0] == bytes([AVERAGE_STOP])
        assert mock.write_log[1] == bytes([GET_METADATA])


class TestDeviceCommands:
    @pytest.fixture
    def device(self):
        mock = MockTransport()
        dev = PPK2Device(mock)
        dev._connect()
        return dev, mock

    def test_use_source_meter(self, device):
        dev, mock = device
        mock.write_log.clear()
        dev.use_source_meter()
        assert mock.write_log[-1] == bytes([SET_POWER_MODE, 2])

    def test_use_ampere_meter(self, device):
        dev, mock = device
        mock.write_log.clear()
        dev.use_ampere_meter()
        assert mock.write_log[-1] == bytes([SET_POWER_MODE, 1])

    def test_set_source_voltage(self, device):
        dev, mock = device
        dev.set_source_voltage(3300)
        assert dev.vdd_mv == 3300
        cmd = mock.write_log[-1]
        assert cmd[0] == REGULATOR_SET
        assert (cmd[1] << 8 | cmd[2]) == 3300

    def test_set_source_voltage_out_of_range(self, device):
        dev, _ = device
        with pytest.raises(ValueError, match="800-5000"):
            dev.set_source_voltage(500)
        with pytest.raises(ValueError, match="800-5000"):
            dev.set_source_voltage(6000)

    def test_toggle_dut_power(self, device):
        dev, mock = device
        dev.toggle_dut_power(True)
        assert mock.write_log[-1] == bytes([DEVICE_RUNNING_SET, 1])
        dev.toggle_dut_power(False)
        assert mock.write_log[-1] == bytes([DEVICE_RUNNING_SET, 0])

    def test_start_stop_measuring(self, device):
        dev, mock = device
        dev.start_measuring()
        assert mock.write_log[-1] == bytes([AVERAGE_START])
        dev.stop_measuring()
        assert mock.write_log[-1] == bytes([AVERAGE_STOP])


class TestDeviceMeasure:
    def test_measure_returns_samples(self):
        mock = MockTransport(sample_adc=500, sample_range=2)
        dev = PPK2Device(mock)
        dev._connect()

        result = dev.measure(duration_s=0.05)

        assert result.sample_count > 0
        assert result.duration_s == 0.05
        assert result.lost_samples == 0
        assert all(s.range == 2 for s in result.samples)

    def test_measure_statistics(self):
        mock = MockTransport(sample_adc=500, sample_range=2)
        dev = PPK2Device(mock)
        dev._connect()

        result = dev.measure(duration_s=0.05)

        # All samples have same ADC, so mean/min/max should be very close
        assert result.mean_ua > 0
        assert result.min_ua > 0
        assert result.max_ua > 0
        assert abs(result.mean_ua - result.min_ua) / result.mean_ua < 0.1

    def test_measure_with_digital_channels(self):
        # D0 high, D2 high = 0b00000101 = 0x05
        mock = MockTransport(sample_adc=500, sample_range=2, sample_logic=0x05)
        dev = PPK2Device(mock)
        dev._connect()

        result = dev.measure(duration_s=0.02)

        assert result.sample_count > 0
        for s in result.samples:
            assert s.logic & 0x01  # D0 high
            assert s.logic & 0x04  # D2 high
            assert not (s.logic & 0x02)  # D1 low

    def test_measure_without_spike_filter(self):
        mock = MockTransport(sample_adc=500, sample_range=2)
        dev = PPK2Device(mock)
        dev._connect()

        filtered = dev.measure(duration_s=0.02, spike_filter=True)
        unfiltered = dev.measure(duration_s=0.02, spike_filter=False)

        # With constant range and ADC, both should converge to similar values
        assert filtered.sample_count > 0
        assert unfiltered.sample_count > 0


class TestDeviceWaitForDigital:
    def test_wait_for_digital_high(self):
        mock = MockTransport(sample_logic=0x01)  # D0 high
        dev = PPK2Device(mock)
        dev._connect()
        dev.start_measuring()

        result = dev.wait_for_digital(channel=0, level=True, timeout_s=1.0)
        assert result is True

    def test_wait_for_digital_low(self):
        mock = MockTransport(sample_logic=0x00)  # all low
        dev = PPK2Device(mock)
        dev._connect()
        dev.start_measuring()

        result = dev.wait_for_digital(channel=0, level=False, timeout_s=1.0)
        assert result is True

    def test_wait_for_digital_timeout(self):
        mock = MockTransport(sample_logic=0x00)  # all low
        dev = PPK2Device(mock)
        dev._connect()
        dev.start_measuring()

        # Wait for D0 high — should timeout since all pins are low
        result = dev.wait_for_digital(channel=0, level=True, timeout_s=0.1)
        assert result is False

    def test_wait_for_digital_invalid_channel(self):
        mock = MockTransport()
        dev = PPK2Device(mock)
        dev._connect()

        with pytest.raises(ValueError, match="0-7"):
            dev.wait_for_digital(channel=8, level=True)


class TestDeviceClose:
    def test_close_stops_measuring(self):
        mock = MockTransport()
        dev = PPK2Device(mock)
        dev._connect()
        dev.start_measuring()

        mock.write_log.clear()
        dev.close()

        opcodes = [c[0] for c in mock.write_log]
        assert AVERAGE_STOP in opcodes
        assert DEVICE_RUNNING_SET in opcodes

    def test_close_turns_off_dut_power(self):
        mock = MockTransport()
        dev = PPK2Device(mock)
        dev._connect()

        mock.write_log.clear()
        dev.close()

        dut_cmds = [c for c in mock.write_log if c[0] == DEVICE_RUNNING_SET]
        assert dut_cmds[-1] == bytes([DEVICE_RUNNING_SET, 0])

    def test_context_manager(self):
        mock = MockTransport()
        dev = PPK2Device(mock)
        dev._connect()

        with dev:
            dev.start_measuring()

        # After exiting context, port should be closed
        assert not mock.is_open
