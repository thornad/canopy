"""Entry point for Canopy menubar app with error handling."""

import logging
import os
import sys
import traceback
from pathlib import Path

LOG_DIR = Path.home() / ".canopy" / "logs"


def _show_error_dialog(title: str, message: str):
    """Show error dialog via osascript (works even if AppKit fails)."""
    import subprocess
    escaped = message.replace('"', '\\"').replace("\n", "\\n")
    subprocess.run(
        ["osascript", "-e", f'display dialog "{escaped}" with title "{title}" buttons {{"OK"}} default button "OK"'],
        capture_output=True,
    )


def _write_crash_log(exc_text: str):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    crash_file = LOG_DIR / "crash.log"
    with open(crash_file, "a") as f:
        from datetime import datetime
        f.write(f"\n{'='*60}\n")
        f.write(f"Canopy crash at {datetime.now().isoformat()}\n")
        f.write(f"{'='*60}\n")
        f.write(exc_text)
        f.write("\n")


def main():
    # Setup logging
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "app.log"),
            logging.StreamHandler(),
        ],
    )

    # macOS apps launched from Finder inherit a stripped-down PATH from
    # launchd.  Propagate the user's shell PATH into os.environ so every
    # subprocess (including the Canopy server and its MCP children) sees
    # the same directories a Terminal session would.
    try:
        from canopy.env import build_mcp_path

        enhanced = build_mcp_path()
        if enhanced:
            os.environ["PATH"] = enhanced
            logging.getLogger(__name__).debug("Enhanced PATH: %s", enhanced)
    except Exception:
        pass

    try:
        from .app import main as app_main
        app_main()
    except Exception:
        exc_text = traceback.format_exc()
        _write_crash_log(exc_text)
        _show_error_dialog(
            "Canopy Error",
            f"Canopy encountered an error and needs to close.\n\nSee {LOG_DIR / 'crash.log'} for details.",
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
