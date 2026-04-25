#!/usr/bin/env bash
# Local mirror of .github/workflows/ci.yml. Run before pushing to catch
# CI failures early; the workflow itself shells out to this script.
#
# Usage:
#   ./ci.sh                  # run everything (install + test + examples + site)
#   ./ci.sh install          # pip install -e .[dev] in .venv
#   ./ci.sh test             # pytest with junitxml=results.xml
#   ./ci.sh test-summary     # print a Markdown summary of results.xml
#   ./ci.sh examples         # python examples/generate_reports.py
#   ./ci.sh build-site       # populate _site/ from examples/output
#   ./ci.sh -h | --help
#
# Legacy compatibility:
#   ./ci.sh --no-install     # skip install, otherwise full run
#   ./ci.sh --quick          # tests only; skip examples + build-site
#
# Environment:
#   CI_PY_VERSION   label used in test-summary header (defaults to running python's version)
#   GITHUB_STEP_SUMMARY   if set (in GitHub Actions), test-summary appends instead of stdout

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32mok\033[0m %s\n' "$*"; }

# --- helpers ---------------------------------------------------------------

ensure_venv() {
    if [[ ! -d .venv ]]; then
        step "Create virtualenv (.venv)"
        python3 -m venv .venv
    fi
    # shellcheck disable=SC1091
    source .venv/bin/activate
}

# --- subcommands -----------------------------------------------------------

cmd_install() {
    ensure_venv
    step "Install package with dev dependencies"
    pip install -q -e ".[dev]"
    ok "dependencies installed"
}

cmd_test() {
    ensure_venv
    step "Run pytest"
    pytest tests/ -v --tb=short --junitxml=results.xml
    ok "tests passed"
}

cmd_test_summary() {
    ensure_venv
    local out="${GITHUB_STEP_SUMMARY:-/dev/stdout}"
    python - <<'PYEOF' >> "$out"
import os
import xml.etree.ElementTree as ET

label = os.environ.get("CI_PY_VERSION") or ".".join(__import__("sys").version.split(".", 2)[:2])
tree = ET.parse("results.xml")
suite = tree.getroot().find("testsuite")
tests = int(suite.get("tests", 0))
failures = int(suite.get("failures", 0))
errors = int(suite.get("errors", 0))
skipped = int(suite.get("skipped", 0))
time_s = float(suite.get("time", 0))
passed = tests - failures - errors - skipped

status = "✅" if failures == 0 and errors == 0 else "❌"
print(f"### {status} Python {label} — {passed}/{tests} passed ({time_s:.1f}s)\n")

if failures > 0 or errors > 0:
    print("| Test | Status | Message |")
    print("|------|--------|---------|")
    for tc in suite.iter("testcase"):
        fail = tc.find("failure")
        err = tc.find("error")
        if fail is not None:
            msg = fail.get("message", "").split("\n")[0][:80]
            print(f"| `{tc.get('name')}` | ❌ Fail | {msg} |")
        elif err is not None:
            msg = err.get("message", "").split("\n")[0][:80]
            print(f"| `{tc.get('name')}` | 💥 Error | {msg} |")
PYEOF
}

cmd_examples() {
    ensure_venv
    step "Generate example reports"
    python examples/generate_reports.py
    ok "examples/output/*.html regenerated"
}

cmd_build_site() {
    step "Build _site/ (GitHub Pages artifact)"
    rm -rf _site
    mkdir -p _site
    cp examples/output/*.html _site/
    cat > _site/index.html <<'INDEXEOF'
<!DOCTYPE html>
<html><head><title>PPK2 Example Reports</title>
<style>
  body { font-family: system-ui; max-width: 600px; margin: 40px auto; padding: 0 20px; }
  a { display: block; margin: 8px 0; }
</style></head>
<body>
  <h1>PPK2 Example Reports</h1>
  <a href="example_light.html">☀️ Light theme</a>
  <a href="example_dark.html">🌙 Dark theme</a>
  <a href="example_auto.html">🔄 Auto theme (follows browser)</a>
</body></html>
INDEXEOF
    ok "_site/ built — open _site/index.html in a browser to preview"
}

cmd_help() {
    sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
}

cmd_all() {
    local install=1 quick=0
    for arg in "$@"; do
        case "$arg" in
            --no-install) install=0 ;;
            --quick) quick=1 ;;
            *) echo "unknown flag: $arg" >&2; exit 2 ;;
        esac
    done
    [[ $install -eq 1 ]] && cmd_install
    cmd_test
    if [[ $quick -eq 0 ]]; then
        cmd_examples
        cmd_build_site
    fi
    printf '\n\033[1;32mAll local CI steps passed.\033[0m\n'
}

# --- dispatch --------------------------------------------------------------

if [[ $# -eq 0 ]]; then
    cmd_all
    exit 0
fi

case "$1" in
    install)        shift; cmd_install ;;
    test)           shift; cmd_test ;;
    test-summary)   shift; cmd_test_summary ;;
    examples)       shift; cmd_examples ;;
    build-site)     shift; cmd_build_site ;;
    -h|--help)      cmd_help ;;
    --no-install|--quick) cmd_all "$@" ;;
    *) echo "unknown subcommand: $1" >&2; cmd_help; exit 2 ;;
esac
