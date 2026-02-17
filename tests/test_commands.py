"""Tests for PPK2 command encoding."""

import struct

from ppk2.commands import (
    average_start,
    average_stop,
    device_running_set,
    get_metadata,
    regulator_set,
    reset,
    set_power_mode,
    set_user_gains,
)


def test_average_start():
    assert average_start() == bytes([0x06])


def test_average_stop():
    assert average_stop() == bytes([0x07])


def test_device_running_on():
    assert device_running_set(True) == bytes([0x0C, 1])


def test_device_running_off():
    assert device_running_set(False) == bytes([0x0C, 0])


def test_regulator_set_3700mv():
    cmd = regulator_set(3700)
    assert cmd[0] == 0x0D
    assert (cmd[1] << 8 | cmd[2]) == 3700


def test_regulator_set_800mv():
    cmd = regulator_set(800)
    assert cmd == bytes([0x0D, 800 >> 8, 800 & 0xFF])


def test_set_power_mode_source():
    assert set_power_mode(source_meter=True) == bytes([0x11, 2])


def test_set_power_mode_ampere():
    assert set_power_mode(source_meter=False) == bytes([0x11, 1])


def test_get_metadata():
    assert get_metadata() == bytes([0x19])


def test_reset():
    assert reset() == bytes([0x20])


def test_set_user_gains():
    cmd = set_user_gains(2, 1.0)
    assert cmd[0] == 0x25
    assert cmd[1] == 2
    assert struct.unpack("<f", cmd[2:6])[0] == 1.0


def test_set_user_gains_range4():
    cmd = set_user_gains(4, 0.95)
    assert cmd[0] == 0x25
    assert cmd[1] == 4
    assert abs(struct.unpack("<f", cmd[2:6])[0] - 0.95) < 1e-6
