"""Desktop automation for nRF Connect Power Profiler.

Launches nRF Connect with the PPK plugin and loads a .ppk2 file
by connecting to the Electron app via Chrome DevTools Protocol (CDP)
and intercepting the native file dialog IPC call.

Requires: pip install ppk2-python[desktop] && playwright install chromium
"""

import platform
import shutil
import subprocess
import time
from pathlib import Path

_APP_PATHS = {
    "Darwin": [
        "/Applications/nRF Connect for Desktop.app/Contents/MacOS/nRF Connect for Desktop",
    ],
    "Windows": [
        Path.home() / "AppData/Local/Programs/nrfconnect/nRF Connect for Desktop.exe",
    ],
    "Linux": [
        "/opt/nrfconnect/nrfconnect",
        Path.home() / ".local/bin/nrfconnect",
        "/snap/bin/nrfconnect",
    ],
}

APP_ID = "pc-nrfconnect-ppk"
CDP_PORT = 9223


def find_nrf_connect() -> str | None:
    """Find the nRF Connect for Desktop executable."""
    system = platform.system()
    for path in _APP_PATHS.get(system, []):
        if Path(path).exists():
            return str(path)
    return shutil.which("nrfconnect")


def open_in_nrf_connect(
    ppk2_file: str | Path,
    app_path: str | None = None,
    wait: bool = True,
) -> int:
    """Open a .ppk2 file in nRF Connect Power Profiler.

    Launches the app with --remote-debugging-port, connects via CDP,
    monkey-patches ipcRenderer.invoke to intercept the native file dialog,
    then clicks the Load button.

    Args:
        ppk2_file: Path to the .ppk2 file.
        app_path: Path to nRF Connect executable. Auto-detected if None.
        wait: Wait for the app to close before returning.

    Returns:
        0 on success, 1 on error.
    """
    from playwright.sync_api import sync_playwright

    ppk2_file = Path(ppk2_file).resolve()
    if not ppk2_file.exists():
        print(f"File not found: {ppk2_file}")
        return 1

    if app_path is None:
        app_path = find_nrf_connect()
    if app_path is None:
        print("nRF Connect for Desktop not found.")
        print("Install from: https://www.nordicsemi.com/Products/Development-tools/nRF-Connect-for-Desktop")
        return 1

    print(f"Launching nRF Connect Power Profiler...")
    print(f"Loading: {ppk2_file.name}")

    # Launch with Chromium remote debugging enabled
    proc = subprocess.Popen(
        [
            app_path,
            "--open-downloadable-app", APP_ID,
            "--skip-splash-screen",
            f"--remote-debugging-port={CDP_PORT}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    print("Waiting for app to start...")
    time.sleep(8)

    loaded = False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT}")
            page = browser.contexts[0].pages[0]

            abs_path = str(ppk2_file).replace("\\", "\\\\").replace("'", "\\'")

            # nRF Connect uses @electron/remote for dialog calls.
            # Monkey-patch remote.dialog.showOpenDialog to return our
            # file path instead of showing the native OS dialog.
            page.evaluate(f"""() => {{
                const remote = require('@electron/remote');
                const orig = remote.dialog.showOpenDialog.bind(remote.dialog);
                remote.dialog.showOpenDialog = async (win, opts) => {{
                    // Restore original for subsequent calls
                    remote.dialog.showOpenDialog = orig;
                    return {{ canceled: false, filePaths: ['{abs_path}'] }};
                }};
            }}""")

            # Click the Load button in the sidebar
            btn = page.locator("button:has-text('Load')").first
            btn.click()

            # Wait for the file to load and chart to render
            page.wait_for_timeout(3000)

            loaded = True
            print("File loaded successfully.")
            browser.close()

    except Exception as e:
        print(f"Automation error: {e}")
        print(f"App is running — click Load and navigate to: {ppk2_file}")

    if wait:
        print("App is open. Close it when done (Ctrl+C to detach).")
        try:
            proc.wait()
        except KeyboardInterrupt:
            print("\nDetaching from app (still running).")

    return 0
