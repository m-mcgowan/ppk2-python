"""Tests for PPK2 metadata and sample parsing."""

import struct

from ppk2.parser import SampleParser, parse_metadata


class TestMetadataParser:
    def test_basic_metadata(self):
        raw = "R0: 1031.64\nR1: 101.65\nVDD: 3700\nMode: 2\nHW: 9\nEND"
        meta = parse_metadata(raw)
        assert meta["r0"] == 1031.64
        assert meta["r1"] == 101.65
        assert meta["vdd"] == 3700
        assert meta["mode"] == 2
        assert meta["hw"] == 9

    def test_nan_handling(self):
        raw = "R0: -nan\nR1: 101.65\nEND"
        meta = parse_metadata(raw)
        assert meta["r0"] is None
        assert meta["r1"] == 101.65

    def test_full_calibration(self):
        lines = []
        for prefix in ("R", "GS", "GI", "O", "S", "I", "UG"):
            for i in range(5):
                val = 1.0 if prefix != "R" else [1031.64, 101.65, 10.15, 0.94, 0.043][i]
                lines.append(f"{prefix}{i}: {val}")
        lines.extend(["VDD: 3000", "Mode: 2", "HW: 10604", "END"])
        raw = "\n".join(lines)
        meta = parse_metadata(raw)
        assert meta["r0"] == 1031.64
        assert meta["r4"] == 0.043
        assert meta["ug4"] == 1.0
        assert meta["hw"] == 10604


def _make_frame(adc: int, range_idx: int, counter: int, logic: int) -> bytes:
    """Pack a single PPK2 measurement frame."""
    raw = (adc & 0x3FFF) | ((range_idx & 0x7) << 14) | ((counter & 0x3F) << 18) | ((logic & 0xFF) << 24)
    return struct.pack("<I", raw)


class TestSampleParser:
    def test_single_frame(self):
        parser = SampleParser()
        frame = _make_frame(adc=1000, range_idx=2, counter=0, logic=0x05)
        results = parser.feed(frame)
        valid = [r for r in results if r is not None]
        assert len(valid) == 1
        adc_raw, range_idx, counter, logic = valid[0]
        assert adc_raw == 1000 * 4  # multiplied by 4
        assert range_idx == 2
        assert counter == 0
        assert logic == 0x05

    def test_multiple_frames(self):
        parser = SampleParser()
        data = b""
        for i in range(10):
            data += _make_frame(adc=500 + i, range_idx=1, counter=i, logic=0)
        results = parser.feed(data)
        valid = [r for r in results if r is not None]
        assert len(valid) == 10
        assert valid[0][0] == 500 * 4
        assert valid[9][0] == 509 * 4

    def test_partial_frame_reassembly(self):
        parser = SampleParser()
        frame = _make_frame(adc=1234, range_idx=3, counter=5, logic=0xFF)

        # Feed first 2 bytes
        results1 = parser.feed(frame[:2])
        assert len([r for r in results1 if r is not None]) == 0

        # Feed remaining 2 bytes
        results2 = parser.feed(frame[2:])
        valid = [r for r in results2 if r is not None]
        assert len(valid) == 1
        assert valid[0][0] == 1234 * 4
        assert valid[0][3] == 0xFF

    def test_digital_channels(self):
        parser = SampleParser()
        # D0=1, D2=1, D7=1 -> 0b10000101 = 0x85
        frame = _make_frame(adc=100, range_idx=0, counter=0, logic=0x85)
        results = parser.feed(frame)
        valid = [r for r in results if r is not None]
        logic = valid[0][3]
        assert logic & 0x01  # D0 high
        assert not (logic & 0x02)  # D1 low
        assert logic & 0x04  # D2 high
        assert logic & 0x80  # D7 high

    def test_counter_wrap(self):
        parser = SampleParser()
        # Counter wraps from 63 to 0
        data = _make_frame(adc=100, range_idx=0, counter=63, logic=0)
        data += _make_frame(adc=100, range_idx=0, counter=0, logic=0)
        results = parser.feed(data)
        valid = [r for r in results if r is not None]
        assert len(valid) == 2
        assert valid[0][2] == 63
        assert valid[1][2] == 0

    def test_adc_max_value(self):
        """14-bit ADC maxes at 0x3FFF = 16383."""
        parser = SampleParser()
        frame = _make_frame(adc=0x3FFF, range_idx=0, counter=0, logic=0)
        results = parser.feed(frame)
        valid = [r for r in results if r is not None]
        assert valid[0][0] == 0x3FFF * 4

    def test_adc_zero(self):
        parser = SampleParser()
        frame = _make_frame(adc=0, range_idx=0, counter=0, logic=0)
        results = parser.feed(frame)
        valid = [r for r in results if r is not None]
        assert valid[0][0] == 0

    def test_all_ranges(self):
        parser = SampleParser()
        data = b""
        for r in range(5):
            data += _make_frame(adc=1000, range_idx=r, counter=r, logic=0)
        results = parser.feed(data)
        valid = [r for r in results if r is not None]
        assert len(valid) == 5
        for i, sample in enumerate(valid):
            assert sample[1] == i  # range matches

    def test_all_digital_channels_high(self):
        parser = SampleParser()
        frame = _make_frame(adc=100, range_idx=0, counter=0, logic=0xFF)
        results = parser.feed(frame)
        valid = [r for r in results if r is not None]
        logic = valid[0][3]
        for bit in range(8):
            assert logic & (1 << bit), f"D{bit} should be high"

    def test_all_digital_channels_low(self):
        parser = SampleParser()
        frame = _make_frame(adc=100, range_idx=0, counter=0, logic=0x00)
        results = parser.feed(frame)
        valid = [r for r in results if r is not None]
        assert valid[0][3] == 0

    def test_large_stream(self):
        """Parse 10000 samples across multiple feed() calls."""
        parser = SampleParser()
        total_valid = 0
        for batch in range(100):
            data = b""
            for i in range(100):
                counter = (batch * 100 + i) & 0x3F
                data += _make_frame(adc=500, range_idx=2, counter=counter, logic=0)
            results = parser.feed(data)
            total_valid += sum(1 for r in results if r is not None)
        assert total_valid == 10000

    def test_reset_clears_state(self):
        parser = SampleParser()
        parser.feed(_make_frame(adc=100, range_idx=0, counter=10, logic=0))
        parser.reset()
        # After reset, any counter should be accepted as first sample
        results = parser.feed(_make_frame(adc=200, range_idx=0, counter=42, logic=0))
        valid = [r for r in results if r is not None]
        assert len(valid) == 1
        assert valid[0][2] == 42

    def test_dataloss_tracking(self):
        parser = SampleParser()
        assert parser.total_dataloss == 0


class TestMetadataEdgeCases:
    def test_extra_whitespace(self):
        raw = "R0: 1031.64\n  R1: 101.65  \nVDD: 3700\nEND"
        meta = parse_metadata(raw)
        assert meta["r0"] == 1031.64

    def test_integer_values(self):
        raw = "VDD: 3700\nMode: 2\nHW: 10604\nEND"
        meta = parse_metadata(raw)
        assert meta["vdd"] == 3700
        assert isinstance(meta["vdd"], int)

    def test_negative_calibration_values(self):
        raw = "O0: -12.5\nO1: -3.2\nEND"
        meta = parse_metadata(raw)
        assert meta["o0"] == -12.5
        assert meta["o1"] == -3.2

    def test_end_with_trailing_whitespace(self):
        raw = "VDD: 3700\nEND  \n"
        meta = parse_metadata(raw)
        assert meta["vdd"] == 3700
