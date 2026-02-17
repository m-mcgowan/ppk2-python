"""Tests for AI profile generation (no LLM calls — tests parsing and building only)."""

import pytest

from ppk2.ai import _parse_response, generate_profile_from_phases


class TestParseResponse:
    def test_json_object_with_phases(self):
        text = '{"phases": [{"type": "phase", "name": "sleep", "current_ua": 5, "duration_s": 1}]}'
        phases = _parse_response(text)
        assert len(phases) == 1
        assert phases[0]["name"] == "sleep"

    def test_json_array(self):
        text = '[{"type": "phase", "name": "idle", "current_ua": 100, "duration_s": 0.5}]'
        phases = _parse_response(text)
        assert len(phases) == 1

    def test_strips_markdown_fences(self):
        text = '```json\n{"phases": [{"type": "phase", "name": "x", "current_ua": 1, "duration_s": 0.1}]}\n```'
        phases = _parse_response(text)
        assert len(phases) == 1

    def test_invalid_json(self):
        with pytest.raises(Exception):
            _parse_response("not json at all")


class TestGenerateFromPhases:
    def test_simple_profile(self):
        phases = [
            {"type": "phase", "name": "sleep", "current_ua": 5, "duration_s": 0.01},
            {"type": "ramp", "name": "wake", "start_ua": 5, "end_ua": 10000, "duration_s": 0.001},
            {"type": "phase", "name": "active", "current_ua": 10000, "duration_s": 0.01},
        ]
        result = generate_profile_from_phases(phases, seed=42)
        assert result.sample_count > 0
        assert result.mean_ua > 5

    def test_with_digital(self):
        phases = [
            {"type": "phase", "name": "off", "current_ua": 5, "duration_s": 0.001},
            {"type": "digital", "channels": 1},
            {"type": "phase", "name": "on", "current_ua": 10000, "duration_s": 0.001},
        ]
        result = generate_profile_from_phases(phases, seed=42)
        assert result.samples[0].logic == 0
        assert result.samples[-1].logic == 1

    def test_with_spike(self):
        phases = [
            {"type": "phase", "name": "idle", "current_ua": 100, "duration_s": 0.01},
            {"type": "spike", "current_ua": 50000, "duration_ms": 1},
            {"type": "phase", "name": "idle", "current_ua": 100, "duration_s": 0.01},
        ]
        result = generate_profile_from_phases(phases, seed=42)
        assert result.max_ua > 40000

    def test_periodic_wake(self):
        phases = [
            {"type": "periodic_wake", "sleep_ua": 3, "wake_ua": 8000,
             "sleep_s": 0.01, "wake_s": 0.001, "cycles": 5},
        ]
        result = generate_profile_from_phases(phases, seed=42)
        assert result.sample_count == 5 * (1000 + 100)

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown phase type"):
            generate_profile_from_phases([{"type": "bogus"}])

    def test_realistic_ble_beacon(self):
        """Simulate what Claude might generate for a BLE beacon description."""
        phases = [
            {"type": "phase", "name": "deep_sleep", "current_ua": 3.5, "duration_s": 0.1, "noise_ua": 0.3},
            {"type": "ramp", "name": "wakeup", "start_ua": 3.5, "end_ua": 5000, "duration_s": 0.001},
            {"type": "digital", "channels": 1},
            {"type": "phase", "name": "radio_init", "current_ua": 5000, "duration_s": 0.002, "noise_ua": 200},
            {"type": "spike", "current_ua": 12000, "duration_ms": 2, "name": "tx_burst"},
            {"type": "digital", "channels": 0},
            {"type": "ramp", "name": "shutdown", "start_ua": 5000, "end_ua": 3.5, "duration_s": 0.001},
            {"type": "phase", "name": "deep_sleep_2", "current_ua": 3.5, "duration_s": 0.1, "noise_ua": 0.3},
        ]
        result = generate_profile_from_phases(phases, seed=42)
        assert result.sample_count > 0
        assert result.min_ua < 10  # deep sleep
        assert result.max_ua > 10000  # TX burst
