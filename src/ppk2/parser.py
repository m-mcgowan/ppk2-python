"""PPK2 metadata and measurement data parsing.

Protocol reference:
  https://docs.nordicsemi.com/bundle/ug_ppk2/page/UG/ppk/PPK_user_guide_Intro.html
"""

import json
import logging
import struct

logger = logging.getLogger(__name__)

MAX_COUNTER = 0x3F  # 6-bit, 0-63
DATALOSS_THRESHOLD = 500  # ~5ms at 100kHz
NUM_RANGES = 5  # PPK2 has 5 measurement ranges (0-4); bits encode 3, so 5-7 are reserved
INVALID_RANGE_LOG_LIMIT = 10  # cap diagnostic log lines to avoid flooding


def parse_metadata(raw: str) -> dict:
    """Parse PPK2 metadata text response into a dict.

    The PPK2 returns key-value lines like 'R0: 1031.64' terminated by 'END'.
    """
    text = raw.replace("END", "").strip().lower()
    text = text.replace("-nan", "null")
    text = text.replace("\n", ',\n"')
    text = text.replace(": ", '": ')
    return json.loads('{"' + text + "}")


class SampleParser:
    """Parse binary measurement frames from the PPK2.

    Each frame is 4 bytes little-endian:
        Bits 0-13:  ADC value (14 bits)
        Bits 14-16: Measurement range (3 bits)
        Bits 18-23: Sample counter (6 bits)
        Bits 24-31: Digital logic channels (8 bits)
    """

    FRAME_SIZE = 4

    def __init__(self):
        self._remainder = b""
        self._expected_counter: int | None = None
        self._dataloss_counter = 0
        self._invalid_range_counter = 0
        self._invalid_range_logged = 0
        self._corrupted_buffer: list[tuple[int, int, int, int]] = []

    def reset(self) -> None:
        self._remainder = b""
        self._expected_counter = None
        self._dataloss_counter = 0
        self._invalid_range_counter = 0
        self._invalid_range_logged = 0
        self._corrupted_buffer.clear()

    @property
    def total_dataloss(self) -> int:
        return self._dataloss_counter

    @property
    def total_invalid_ranges(self) -> int:
        return self._invalid_range_counter

    def feed(self, data: bytes) -> list[tuple[int, int, int, int] | None]:
        """Feed raw bytes, return parsed samples.

        Returns a list of (adc_raw, range, counter, logic) tuples.
        None entries represent lost samples (placeholder for timestamp alignment).
        """
        data = self._remainder + data
        results: list[tuple[int, int, int, int] | None] = []
        offset = 0

        while offset + self.FRAME_SIZE <= len(data):
            raw = struct.unpack_from("<I", data, offset)[0]
            offset += self.FRAME_SIZE

            adc = (raw & 0x3FFF) * 4
            range_idx = (raw >> 14) & 0x7
            counter = (raw >> 18) & 0x3F
            logic = (raw >> 24) & 0xFF

            if range_idx >= NUM_RANGES:
                # Reserved range bits (5-7) — treat as a lost sample. We do not
                # feed it through counter resync, but we do advance the
                # expected counter so a single corrupt frame does not cascade
                # into a dataloss false-positive.
                self._invalid_range_counter += 1
                if self._invalid_range_logged < INVALID_RANGE_LOG_LIMIT:
                    self._invalid_range_logged += 1
                    logger.warning(
                        "Invalid PPK2 range_idx=%d in frame raw=0x%08x "
                        "(adc=%d counter=%d logic=0x%02x); treating as lost sample",
                        range_idx, raw, adc, counter, logic,
                    )
                results.append(None)
                if self._expected_counter is not None:
                    self._expected_counter = (
                        self._expected_counter + 1
                    ) & MAX_COUNTER
                continue

            sample = (adc, range_idx, counter, logic)
            resolved = self._check_counter(sample)
            results.extend(resolved)

        self._remainder = data[offset:]
        return results

    def _check_counter(
        self, sample: tuple[int, int, int, int]
    ) -> list[tuple[int, int, int, int] | None]:
        """Handle sample counter for data loss detection.

        Buffers up to 4 out-of-sequence samples before declaring loss.
        """
        _, _, counter, _ = sample
        results: list[tuple[int, int, int, int] | None] = []

        if self._expected_counter is None:
            self._expected_counter = counter
        elif (
            self._corrupted_buffer and counter == self._expected_counter
        ):
            # Counter came back into sequence — flush buffered samples
            for buffered in self._corrupted_buffer:
                results.append(buffered)
            self._corrupted_buffer.clear()
        elif len(self._corrupted_buffer) > 4:
            # Too many out-of-sequence — declare data loss
            missing = (
                counter - self._expected_counter + MAX_COUNTER + 1
            ) & MAX_COUNTER
            self._report_dataloss(missing)
            for _ in range(missing):
                results.append(None)
            self._expected_counter = counter
            self._corrupted_buffer.clear()
        elif self._expected_counter != counter:
            self._corrupted_buffer.append(sample)
            self._expected_counter = (self._expected_counter + 1) & MAX_COUNTER
            return [sample]

        self._expected_counter = (self._expected_counter + 1) & MAX_COUNTER
        results.append(sample)
        return results

    def _report_dataloss(self, missing: int) -> None:
        was_below = self._dataloss_counter < DATALOSS_THRESHOLD
        self._dataloss_counter += missing
        if was_below and self._dataloss_counter >= DATALOSS_THRESHOLD:
            logger.error(
                "Significant data loss detected (%d samples). "
                "Check USB connection and host CPU load.",
                self._dataloss_counter,
            )
