# GitHub Action

The repo ships a composite action that turns `.ppk2` artifacts into power
profiling reports inside CI workflows.

## Example workflow

```yaml
# .github/workflows/power-profile.yml
name: Power Profile
on: [workflow_dispatch]

jobs:
  report:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      # Download .ppk2 artifacts from a bench runner or previous job
      - uses: actions/download-artifact@v4
        with:
          name: power-profiles
          path: profiles/

      - uses: m-mcgowan/ppk2-python@main
        with:
          files: "profiles/*.ppk2"
          thresholds: '{"deep_sleep": 10, "gps_fix": 50000}'
          html-report: "power-report.html"

      - uses: actions/upload-artifact@v4
        with:
          name: power-report
          path: power-report.html
```

## What it does

- Loads each `.ppk2` and writes a markdown summary to `$GITHUB_STEP_SUMMARY`.
- Emits `::error::` annotations for any threshold failure so a failed budget
  surfaces on the PR diff.
- Produces an interactive plotly HTML report at the path given by
  `html-report`.
- Sets a `passed` output (`true` / `false`) for downstream conditional steps.

The action is implemented in [`action.yml`](../action.yml) and
[`action_report.py`](../action_report.py). The same code path runs locally
via `ppk2 report` — see the [CLI reference](../README.md#cli) for the
non-CI invocation.
