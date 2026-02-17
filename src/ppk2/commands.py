"""PPK2 command opcodes and encoding.

Protocol reference:
  https://docs.nordicsemi.com/bundle/ug_ppk2/page/UG/ppk/PPK_user_guide_Intro.html
  https://devzone.nordicsemi.com/guides/hardware-design-test-and-measuring/b/nrf5x/posts/ppk_5f00_api
"""

import struct

# Command opcodes
TRIGGER_SET = 0x01
TRIGGER_WINDOW_SET = 0x03
TRIGGER_SINGLE_SET = 0x05
AVERAGE_START = 0x06
AVERAGE_STOP = 0x07
DEVICE_RUNNING_SET = 0x0C
REGULATOR_SET = 0x0D
SWITCH_POINT_DOWN = 0x0E
SWITCH_POINT_UP = 0x0F
SET_POWER_MODE = 0x11
RES_USER_SET = 0x12
SPIKE_FILTERING_ON = 0x15
SPIKE_FILTERING_OFF = 0x16
GET_METADATA = 0x19
RESET = 0x20
SET_USER_GAINS = 0x25


def average_start() -> bytes:
    return bytes([AVERAGE_START])


def average_stop() -> bytes:
    return bytes([AVERAGE_STOP])


def device_running_set(on: bool) -> bytes:
    return bytes([DEVICE_RUNNING_SET, 1 if on else 0])


def regulator_set(vdd_mv: int) -> bytes:
    return bytes([REGULATOR_SET, vdd_mv >> 8, vdd_mv & 0xFF])


def set_power_mode(source_meter: bool) -> bytes:
    return bytes([SET_POWER_MODE, 2 if source_meter else 1])


def get_metadata() -> bytes:
    return bytes([GET_METADATA])


def reset() -> bytes:
    return bytes([RESET])


def set_user_gains(range_idx: int, gain: float) -> bytes:
    return bytes([SET_USER_GAINS, range_idx]) + struct.pack("<f", gain)


def trigger_set(level_ua: int) -> bytes:
    return bytes([TRIGGER_SET, level_ua >> 8, level_ua & 0xFF])


def trigger_window_set(window: int) -> bytes:
    return bytes([TRIGGER_WINDOW_SET, window])


def trigger_single_set() -> bytes:
    return bytes([TRIGGER_SINGLE_SET])


def switch_point_down(value: int) -> bytes:
    return bytes([SWITCH_POINT_DOWN, value])


def switch_point_up(value: int) -> bytes:
    return bytes([SWITCH_POINT_UP, value])


def spike_filtering_on() -> bytes:
    return bytes([SPIKE_FILTERING_ON])


def spike_filtering_off() -> bytes:
    return bytes([SPIKE_FILTERING_OFF])
