#!/usr/bin/env python3
"""GitHub Action entrypoint: load .ppk2 files and generate reports.

Reads configuration from environment variables set by action.yml:
  INPUT_FILES: glob pattern or comma-separated .ppk2 file paths
  INPUT_THRESHOLDS: JSON mapping of test name to max uA
  INPUT_HTML_REPORT: output path for HTML report (empty to skip)
  INPUT_TITLE: report title
"""

import glob
import json
import os
import sys
from pathlib import Path

from ppk2.ppk2file import load_ppk2
from ppk2.report import (
    ProfileResult,
    github_annotations,
    html_report,
    summary_table,
    write_github_summary,
)


def resolve_files(pattern: str) -> list[Path]:
    """Resolve glob patterns and comma-separated paths to a list of files."""
    paths = []
    for part in pattern.split(","):
        part = part.strip()
        if not part:
            continue
        matches = glob.glob(part, recursive=True)
        if matches:
            paths.extend(Path(m) for m in sorted(matches))
        else:
            p = Path(part)
            if p.exists():
                paths.append(p)
            else:
                print(f"::warning::File not found: {part}")
    return paths


def main() -> int:
    files_input = os.environ.get("INPUT_FILES", "")
    thresholds_json = os.environ.get("INPUT_THRESHOLDS", "{}")
    html_path = os.environ.get("INPUT_HTML_REPORT", "")
    title = os.environ.get("INPUT_TITLE", "Power Profile Report")

    thresholds: dict[str, float] = json.loads(thresholds_json)

    files = resolve_files(files_input)
    if not files:
        print("::error::No .ppk2 files found matching the input pattern")
        return 1

    results: list[ProfileResult] = []
    for f in files:
        name = f.stem  # filename without extension as test name
        measurement = load_ppk2(f)
        max_ua = thresholds.get(name)
        results.append(ProfileResult(name=name, result=measurement, max_ua=max_ua))
        print(f"Loaded {name}: {measurement.sample_count} samples, mean={measurement.mean_ua:.1f} uA")

    # Markdown summary to stdout
    table = summary_table(results)
    print()
    print(table)

    # GitHub step summary
    write_github_summary(results)

    # GitHub annotations for failures
    github_annotations(results)

    # HTML report
    if html_path:
        html_report(results, html_path, title=title)
        print(f"\nHTML report written to {html_path}")

    # Set outputs
    all_passed = all(r.passed is not False for r in results)
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"passed={str(all_passed).lower()}\n")
            # Multiline output for summary
            f.write("summary<<EOF\n")
            f.write(table + "\n")
            f.write("EOF\n")

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
