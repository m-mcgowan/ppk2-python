"""AI-powered synthetic profile generation.

Uses Claude to interpret natural language power profile descriptions
and generate realistic .ppk2 files via the ProfileBuilder.

Requires: pip install anthropic

Usage:
    from ppk2.ai import generate_profile

    gen = generate_profile("GPS cold fix: 30mA for 30s, then 15mA tracking for 60s")
    print(gen.phase_summary())  # see what Claude generated
    save_ppk2(gen.profile, "gps_fix.ppk2")

Or via CLI:
    ppk2 generate "BLE beacon: 3uA sleep, wakes every 2s to TX at 8mA for 5ms" -o beacon.ppk2
"""

import json
import re
from dataclasses import dataclass
from textwrap import dedent

from .synthetic import ProfileBuilder
from .types import MeasurementResult

_SYSTEM_PROMPT = dedent("""\
    You are a power profile generator for embedded IoT devices.
    Given a description of a device's power behavior, generate Python code
    that uses ProfileBuilder to create a realistic synthetic power profile.

    Available API:
    - phase(name, current_ua, duration_s, noise_ua=0, noise_type="gaussian")
    - ramp(name, start_ua, end_ua, duration_s, noise_ua=0)
    - spike(current_ua, duration_ms=1, name="spike")
    - periodic_wake(sleep_ua, wake_ua, sleep_s, wake_s, cycles, sleep_noise_ua=0, wake_noise_ua=0)
    - digital(channels)  # 8-bit bitmask for D0-D7

    Current units are ALWAYS in microamps (uA). Convert from mA (* 1000) or nA (/ 1000).
    Common reference points:
    - Deep sleep: 1-10 uA
    - Light sleep: 10-100 uA
    - Idle / standby: 100-5000 uA
    - BLE TX: 5000-15000 uA
    - WiFi TX: 80000-300000 uA
    - GPS acquisition: 20000-50000 uA
    - Cellular (LTE-M): 50000-200000 uA

    Add realistic noise to each phase (typically 5-15% of the mean current).
    Add brief ramps between very different current levels (10-50ms).
    Use digital channels to mark significant events (D0 for radio, D1 for GPS, etc.).

    Respond with ONLY a JSON object containing a "phases" array. Each element is one of:
    {"type": "phase", "name": "...", "current_ua": N, "duration_s": N, "noise_ua": N}
    {"type": "ramp", "name": "...", "start_ua": N, "end_ua": N, "duration_s": N, "noise_ua": N}
    {"type": "spike", "current_ua": N, "duration_ms": N, "name": "..."}
    {"type": "periodic_wake", "sleep_ua": N, "wake_ua": N, "sleep_s": N, "wake_s": N, "cycles": N, "sleep_noise_ua": N, "wake_noise_ua": N}
    {"type": "digital", "channels": N}

    No markdown, no explanation, just the JSON object.
""")


@dataclass
class GenerationResult:
    """Result of AI profile generation, including the phase spec."""
    profile: MeasurementResult
    phases: list[dict]  # the JSON phase list Claude generated

    def phase_summary(self) -> str:
        """Human-readable summary of the generated phases."""
        lines = []
        for p in self.phases:
            t = p.get("type", "?")
            name = p.get("name", "")
            if t == "phase":
                lines.append(f"  {name}: {_fmt(p['current_ua'])} for {p['duration_s']}s")
            elif t == "ramp":
                lines.append(f"  {name}: {_fmt(p['start_ua'])} -> {_fmt(p['end_ua'])} over {p['duration_s']}s")
            elif t == "spike":
                lines.append(f"  {name}: spike to {_fmt(p['current_ua'])} for {p.get('duration_ms', 1)}ms")
            elif t == "periodic_wake":
                lines.append(
                    f"  periodic: {_fmt(p['sleep_ua'])} sleep / {_fmt(p['wake_ua'])} wake"
                    f" x{p['cycles']} ({p['sleep_s']}s/{p['wake_s']}s)"
                )
            elif t == "digital":
                lines.append(f"  digital channels: 0x{p['channels']:02X}")
        return "\n".join(lines)


def _fmt(ua: float) -> str:
    """Format current for phase summary."""
    if ua >= 1000:
        return f"{ua / 1000:.1f}mA"
    return f"{ua:.1f}uA"


def generate_profile(
    description: str,
    model: str = "claude-sonnet-4-5-20250929",
    seed: int | None = None,
) -> GenerationResult:
    """Generate a synthetic power profile from a natural language description.

    Args:
        description: Natural language description of the power profile.
        model: Anthropic model to use.
        seed: Random seed for reproducible noise.

    Returns:
        GenerationResult with the profile and the phase spec Claude generated.

    Raises:
        ImportError: If the anthropic package is not installed.
    """
    try:
        import anthropic
    except ImportError:
        raise ImportError(
            "anthropic package required for AI profile generation. "
            "Install with: pip install anthropic"
        )

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": description}],
    )

    response_text = message.content[0].text
    phases = _parse_response(response_text)
    profile = _build_from_phases(phases, seed=seed)
    return GenerationResult(profile=profile, phases=phases)


_ANALYSIS_PROMPT = dedent("""\
    You are a power profile analyst for embedded IoT devices.
    You will receive statistics and a downsampled current trace from a PPK2 measurement.
    Analyze the power profile and provide:

    1. **Phase identification**: Identify distinct operational phases (sleep, active, TX, RX, etc.)
       with their approximate current levels and durations.
    2. **Anomalies**: Flag any unexpected spikes, dropouts, or irregular patterns.
    3. **Power budget**: Estimate average power consumption and battery life
       (assume 3.7V LiPo, user will specify capacity if needed).
    4. **Optimization suggestions**: Identify potential power savings.

    If the user provides context about what the device is doing, use that to give
    more specific analysis. Be concise and actionable.
""")


def analyze_profile(
    result: MeasurementResult,
    context: str = "",
    model: str = "claude-sonnet-4-5-20250929",
) -> str:
    """Analyze a power profile using Claude.

    Sends summary statistics and a downsampled trace to Claude for
    phase identification, anomaly detection, and optimization suggestions.

    Args:
        result: MeasurementResult to analyze.
        context: Optional description of what the device was doing.
        model: Anthropic model to use.

    Returns:
        Analysis text from Claude.
    """
    try:
        import anthropic
    except ImportError:
        raise ImportError(
            "anthropic package required for AI analysis. "
            "Install with: pip install anthropic"
        )

    # Build a compact representation of the profile
    stats = (
        f"Duration: {result.duration_s:.3f}s\n"
        f"Samples: {result.sample_count:,}\n"
        f"Mean: {result.mean_ua:.2f} uA\n"
        f"Min: {result.min_ua:.2f} uA\n"
        f"Max: {result.max_ua:.2f} uA\n"
        f"P99: {result.p99_ua:.2f} uA\n"
    )

    # Downsample to ~500 points for the LLM context
    trace = _downsample_for_analysis(result)

    prompt = f"## Power Profile Data\n\n### Statistics\n{stats}\n### Trace (downsampled)\n{trace}"
    if context:
        prompt = f"## Device Context\n{context}\n\n{prompt}"

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=4096,
        system=_ANALYSIS_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    return message.content[0].text


def _downsample_for_analysis(result: MeasurementResult, target_points: int = 500) -> str:
    """Downsample a profile to a compact text representation for LLM analysis."""
    samples = result.samples
    if not samples:
        return "(no samples)"

    n = len(samples)
    step = max(1, n // target_points)
    us_per_sample = (result.duration_s * 1_000_000 / n) if n else 0

    lines = ["time_ms,current_ua,digital"]
    for i in range(0, n, step):
        # Take min/max/mean of each bucket for better representation
        bucket = samples[i:i + step]
        currents = [s.current_ua for s in bucket]
        mean_current = sum(currents) / len(currents)
        time_ms = i * us_per_sample / 1000
        logic = bucket[0].logic
        lines.append(f"{time_ms:.2f},{mean_current:.2f},{logic}")

    return "\n".join(lines)


_VALIDATE_PROMPT = dedent("""\
    You are a power profile validator for embedded IoT devices.
    You will receive a power profile specification and actual measurement data.
    Compare the measurement against the spec and report:

    1. **PASS/FAIL** verdict — does the measurement match the specification?
    2. **Phase matching**: For each expected phase, did it appear in the data
       with the right current level (within reasonable tolerance) and approximate duration?
    3. **Violations**: List any specific violations with measured vs expected values.
    4. **Unexpected behavior**: Note any phases or patterns not in the spec.

    Use a tolerance of ±20% on current levels and ±30% on durations unless
    the spec states otherwise. Be precise about what passed and what failed.

    Respond with a structured report. Start with either "PASS" or "FAIL" on the
    first line, followed by details.
""")


@dataclass
class ValidationResult:
    """Result of spec validation."""
    passed: bool
    report: str


def validate_profile(
    result: MeasurementResult,
    spec: str,
    model: str = "claude-sonnet-4-5-20250929",
) -> ValidationResult:
    """Validate a power profile against a natural language specification.

    Args:
        result: MeasurementResult to validate.
        spec: Natural language description of the expected power behavior.
            Example: "Deep sleep at 3-5uA for 10s, then wake to 40-50mA for GPS fix,
            then 15mA tracking for 60s."
        model: Anthropic model to use.

    Returns:
        ValidationResult with pass/fail and detailed report.
    """
    try:
        import anthropic
    except ImportError:
        raise ImportError(
            "anthropic package required for AI validation. "
            "Install with: pip install anthropic"
        )

    stats = (
        f"Duration: {result.duration_s:.3f}s\n"
        f"Samples: {result.sample_count:,}\n"
        f"Mean: {result.mean_ua:.2f} uA\n"
        f"Min: {result.min_ua:.2f} uA\n"
        f"Max: {result.max_ua:.2f} uA\n"
        f"P99: {result.p99_ua:.2f} uA\n"
    )
    trace = _downsample_for_analysis(result)

    prompt = (
        f"## Expected Specification\n{spec}\n\n"
        f"## Actual Measurement\n\n### Statistics\n{stats}\n### Trace (downsampled)\n{trace}"
    )

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=4096,
        system=_VALIDATE_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    report = message.content[0].text
    passed = report.strip().upper().startswith("PASS")
    return ValidationResult(passed=passed, report=report)


def generate_profile_from_phases(phases: list[dict], seed: int | None = None) -> MeasurementResult:
    """Generate a profile from a pre-built phase list (no LLM call).

    Useful for testing or when you want to define phases programmatically
    but still use the JSON format.
    """
    return _build_from_phases(phases, seed=seed)


def _parse_response(text: str) -> list[dict]:
    """Parse the LLM response into a phase list."""
    # Strip markdown code fences if present
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)

    data = json.loads(text)

    if isinstance(data, dict) and "phases" in data:
        return data["phases"]
    if isinstance(data, list):
        return data
    raise ValueError(f"Unexpected response format: {type(data)}")


def _build_from_phases(phases: list[dict], seed: int | None = None) -> MeasurementResult:
    """Build a MeasurementResult from a phase list."""
    builder = ProfileBuilder(seed=seed)

    for p in phases:
        phase_type = p.get("type", "phase")

        if phase_type == "digital":
            builder.digital(p["channels"])

        elif phase_type == "phase":
            builder.phase(
                name=p["name"],
                current_ua=p["current_ua"],
                duration_s=p["duration_s"],
                noise_ua=p.get("noise_ua", 0),
                noise_type=p.get("noise_type", "gaussian"),
            )

        elif phase_type == "ramp":
            builder.ramp(
                name=p["name"],
                start_ua=p["start_ua"],
                end_ua=p["end_ua"],
                duration_s=p["duration_s"],
                noise_ua=p.get("noise_ua", 0),
            )

        elif phase_type == "spike":
            builder.spike(
                current_ua=p["current_ua"],
                duration_ms=p.get("duration_ms", 1.0),
                name=p.get("name", "spike"),
            )

        elif phase_type == "periodic_wake":
            builder.periodic_wake(
                sleep_ua=p["sleep_ua"],
                wake_ua=p["wake_ua"],
                sleep_s=p["sleep_s"],
                wake_s=p["wake_s"],
                cycles=p["cycles"],
                sleep_noise_ua=p.get("sleep_noise_ua", 0),
                wake_noise_ua=p.get("wake_noise_ua", 0),
            )

        else:
            raise ValueError(f"Unknown phase type: {phase_type}")

    return builder.build()
