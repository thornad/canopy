"""Configuration for Canopy menubar app."""

import json
import logging
import plistlib
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".canopy"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_DIR = CONFIG_DIR / "logs"
DB_FILE = CONFIG_DIR / "canopy.db"

LAUNCH_AGENT_ID = "com.canopy.app"
LAUNCH_AGENT_DIR = Path.home() / "Library" / "LaunchAgents"
LAUNCH_AGENT_FILE = LAUNCH_AGENT_DIR / f"{LAUNCH_AGENT_ID}.plist"


@dataclass
class CanopyConfig:
    chat_port: int = 8100
    omlx_url: str = "http://localhost:8000"
    omlx_api_key: str = ""
    start_on_launch: bool = True
    launch_at_login: bool = False

    @property
    def is_first_run(self) -> bool:
        return not CONFIG_FILE.exists()

    def save(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls) -> "CanopyConfig":
        if not CONFIG_FILE.exists():
            return cls()
        try:
            data = json.loads(CONFIG_FILE.read_text())
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except Exception:
            return cls()

    def build_serve_args(self) -> list[str]:
        """Build CLI args for `python -m canopy`."""
        return [
            "--port", str(self.chat_port),
            "--host", "127.0.0.1",
            "--db-path", str(DB_FILE),
        ]

    def apply_launch_at_login(self):
        """Create or remove LaunchAgent plist for login item."""
        if self.launch_at_login:
            self._install_launch_agent()
        else:
            self._remove_launch_agent()

    def _install_launch_agent(self):
        """Create a LaunchAgent plist to start Canopy at login."""
        try:
            # Find the app bundle path
            app_path = self._find_app_path()
            if not app_path:
                logger.warning("Cannot find Canopy.app bundle — skipping LaunchAgent")
                return

            LAUNCH_AGENT_DIR.mkdir(parents=True, exist_ok=True)
            plist = {
                "Label": LAUNCH_AGENT_ID,
                "ProgramArguments": ["open", "-a", str(app_path)],
                "RunAtLoad": True,
                "KeepAlive": False,
            }
            with open(LAUNCH_AGENT_FILE, "wb") as f:
                plistlib.dump(plist, f)
            logger.info(f"Installed LaunchAgent: {LAUNCH_AGENT_FILE}")
        except Exception as e:
            logger.error(f"Failed to install LaunchAgent: {e}")

    def _remove_launch_agent(self):
        """Remove the LaunchAgent plist."""
        try:
            if LAUNCH_AGENT_FILE.exists():
                LAUNCH_AGENT_FILE.unlink()
                logger.info(f"Removed LaunchAgent: {LAUNCH_AGENT_FILE}")
        except Exception as e:
            logger.error(f"Failed to remove LaunchAgent: {e}")

    @staticmethod
    def _find_app_path() -> str | None:
        """Find the running Canopy.app bundle path."""
        try:
            from AppKit import NSBundle
            bundle = NSBundle.mainBundle()
            path = bundle.bundlePath()
            if path and path.endswith(".app"):
                return path
        except Exception:
            pass
        # Fallback: check common locations
        for p in ["/Applications/Canopy.app", Path.home() / "Applications" / "Canopy.app"]:
            if Path(p).exists():
                return str(p)
        return None
