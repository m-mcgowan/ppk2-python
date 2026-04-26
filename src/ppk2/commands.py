"""PPK2 command opcodes and encoding.

Protocol reference:
  https://docs.nordicsemi.com/bundle/ug_ppk2/page/UG/ppk/PPK_user_guide_Intro.html
  https://github.com/NordicSemiconductor/pc-nrfconnect-ppk/blob/main/src/constants.ts

Opcode coverage notes (see the inactive block at the bottom of this file):
the upstream PPKCommands enum lists more opcodes than the modern nRF Connect
Power Profiler app actually dispatches. PPK2 triggering, switch-point
hysteresis, and spike filtering are all evaluated host-side in the current
app, so the corresponding device opcodes go unused. We mirror that — the
unused helpers are kept commented out below for reference.
"""

import struct

# --- Active opcodes (verified in upstream and used by this library) ---

AVERAGE_START = 0x06
AVERAGE_STOP = 0x07
DEVICE_RUNNING_SET = 0x0C
REGULATOR_SET = 0x0D
SET_POWER_MODE = 0x11
GET_METADATA = 0x19
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


def set_user_gains(range_idx: int, gain: float) -> bytes:
    return bytes([SET_USER_GAINS, range_idx]) + struct.pack("<f", gain)


# --- Inactive opcodes ---
#
# The opcodes below appear in the upstream `PPKCommands` enum
# (`pc-nrfconnect-ppk/src/constants.ts`) but the current Nordic app does not
# dispatch them — triggering, switch-point hysteresis, and spike filtering
# are all done host-side in the modern app. Whether the PPK2 firmware still
# implements these opcodes is unverified; PPK1 historically supported them.
#
# They are kept here, commented out, so we know what numeric space is
# claimed and so a future maintainer can revive them without re-reading
# the upstream source. If you uncomment one, please confirm the PPK2
# firmware actually accepts it before relying on it.
#
# TRIGGER_SET = 0x01           # 2-byte BE level (uA) — host-side in modern app
# TRIGGER_WINDOW_SET = 0x03    # 1-byte window — host-side in modern app
# TRIGGER_SINGLE_SET = 0x05    # one-shot trigger — host-side in modern app
# SWITCH_POINT_DOWN = 0x0E     # 1-byte range hysteresis — host-side in modern app
# SWITCH_POINT_UP = 0x0F       # 1-byte range hysteresis — host-side in modern app
# RES_USER_SET = 0x12          # user-resistor calibration — superseded by SET_USER_GAINS (0x25)
# SPIKE_FILTERING_ON = 0x15    # device-side spike filter — modern app filters host-side
# SPIKE_FILTERING_OFF = 0x16   # device-side spike filter — modern app filters host-side
# RESET = 0x20                 # soft reset — never dispatched by modern app
#
# def trigger_set(level_ua: int) -> bytes:
#     return bytes([TRIGGER_SET, level_ua >> 8, level_ua & 0xFF])
#
# def trigger_window_set(window: int) -> bytes:
#     return bytes([TRIGGER_WINDOW_SET, window])
#
# def trigger_single_set() -> bytes:
#     return bytes([TRIGGER_SINGLE_SET])
#
# def switch_point_down(value: int) -> bytes:
#     return bytes([SWITCH_POINT_DOWN, value])
#
# def switch_point_up(value: int) -> bytes:
#     return bytes([SWITCH_POINT_UP, value])
#
# def spike_filtering_on() -> bytes:
#     return bytes([SPIKE_FILTERING_ON])
#
# def spike_filtering_off() -> bytes:
#     return bytes([SPIKE_FILTERING_OFF])
#
# def reset() -> bytes:
#     return bytes([RESET])
