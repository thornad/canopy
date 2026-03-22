"""Canopy menubar application using PyObjC."""

import logging
import platform
import webbrowser
from typing import Optional

import objc
import requests
from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSColor,
    NSImage,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSVariableStatusItemLength,
)
from Foundation import NSObject, NSRunLoop, NSDefaultRunLoopMode, NSTimer

from canopy._version import __version__
from .config import CanopyConfig
from .server_manager import ServerManager, ServerStatus
from .updater import AppUpdater, GITHUB_REPO

logger = logging.getLogger(__name__)


class CanopyAppDelegate(NSObject):
    """Main application delegate for Canopy menubar app."""

    def init(self):
        self = objc.super(CanopyAppDelegate, self).init()
        if self is None:
            return None

        self.config = CanopyConfig.load()
        self.server_manager = ServerManager(self.config)
        self.status_item = None
        self.menu = None
        self.health_timer = None

        self._update_info: Optional[dict] = None
        self._last_update_check: float = 0

        return self

    def applicationDidFinishLaunching_(self, notification):
        try:
            self._do_finish_launching()
        except Exception:
            logger.exception("Failed to launch")

    def _do_finish_launching(self):
        # Create status bar item
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        button = self.status_item.button()
        button.setTitle_("🌿")

        # Build menu
        self._build_menu()

        # Status callback
        self.server_manager.set_status_callback(
            lambda s: self.performSelectorOnMainThread_withObject_waitUntilDone_(
                "statusChangedOnMain:", None, False
            )
        )

        # Health check timer (5 seconds)
        self.health_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            5.0, self, "healthCheck:", None, True
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.health_timer, NSDefaultRunLoopMode)

        # Switch to accessory (no dock icon)
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        # Clean up leftover staged update
        AppUpdater.cleanup_staged_app()

        # Check for updates
        self._check_for_updates()

        # Auto-start server
        if self.config.start_on_launch:
            self.server_manager.start()

        self._build_menu()

    def statusChangedOnMain_(self, _):
        self._build_menu()

    def healthCheck_(self, timer):
        """Periodic health check and menu refresh."""
        if self.server_manager.status == ServerStatus.RUNNING:
            if not self.server_manager.check_health():
                pass  # ServerManager health loop handles this
        self._update_icon()

    # --- Menu building ---

    def _build_menu(self):
        self.menu = NSMenu.alloc().init()

        # Status
        status = self.server_manager.status
        status_map = {
            ServerStatus.STOPPED: ("⚫ Stopped", False),
            ServerStatus.STARTING: ("🟡 Starting...", False),
            ServerStatus.RUNNING: ("🟢 Running", False),
            ServerStatus.STOPPING: ("🟡 Stopping...", False),
            ServerStatus.ERROR: ("🔴 Error", False),
        }
        text, _ = status_map.get(status, ("⚫ Unknown", False))
        status_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"Canopy v{__version__} — {text}", None, ""
        )
        status_item.setEnabled_(False)
        self.menu.addItem_(status_item)

        port_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"    Port {self.config.chat_port}", None, ""
        )
        port_item.setEnabled_(False)
        self.menu.addItem_(port_item)

        self.menu.addItem_(NSMenuItem.separatorItem())

        # Update available
        if self._update_info:
            update_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                f"🔔 Update Available ({self._update_info['version']})", "openUpdate:", ""
            )
            update_item.setTarget_(self)
            self.menu.addItem_(update_item)
            self.menu.addItem_(NSMenuItem.separatorItem())

        # Server controls
        if status in (ServerStatus.STOPPED, ServerStatus.ERROR):
            start_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Start Server", "startServer:", ""
            )
            start_item.setTarget_(self)
            self.menu.addItem_(start_item)
        elif status == ServerStatus.RUNNING:
            stop_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Stop Server", "stopServer:", ""
            )
            stop_item.setTarget_(self)
            self.menu.addItem_(stop_item)

            restart_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Restart Server", "restartServer:", ""
            )
            restart_item.setTarget_(self)
            self.menu.addItem_(restart_item)

        self.menu.addItem_(NSMenuItem.separatorItem())

        # Open Chat
        if status == ServerStatus.RUNNING:
            chat_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Open Chat", "openChat:", ""
            )
            chat_item.setTarget_(self)
            self.menu.addItem_(chat_item)
            self.menu.addItem_(NSMenuItem.separatorItem())

        # Settings toggles
        login_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Launch at Login", "toggleLaunchAtLogin:", ""
        )
        login_item.setTarget_(self)
        if self.config.launch_at_login:
            login_item.setState_(1)  # NSOnState
        self.menu.addItem_(login_item)

        autostart_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Start Server on Launch", "toggleStartOnLaunch:", ""
        )
        autostart_item.setTarget_(self)
        if self.config.start_on_launch:
            autostart_item.setState_(1)  # NSOnState
        self.menu.addItem_(autostart_item)

        self.menu.addItem_(NSMenuItem.separatorItem())

        # Quit
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit Canopy", "quitApp:", ""
        )
        quit_item.setTarget_(self)
        self.menu.addItem_(quit_item)

        self.status_item.setMenu_(self.menu)
        self._update_icon()

    def _update_icon(self):
        button = self.status_item.button()
        status = self.server_manager.status
        if status == ServerStatus.RUNNING:
            button.setTitle_("🌲")   # Evergreen — strong green, alive
        elif status in (ServerStatus.STARTING, ServerStatus.STOPPING):
            button.setTitle_("🌱")   # Seedling — transitioning
        else:
            button.setTitle_("🍂")   # Fallen leaf — brown, stopped/dead

    # --- Actions ---

    def startServer_(self, sender):
        self.server_manager.start()
        self._build_menu()

    def stopServer_(self, sender):
        self.server_manager.stop()
        self._build_menu()

    def restartServer_(self, sender):
        self.server_manager.restart()
        self._build_menu()

    def openChat_(self, sender):
        url = f"http://127.0.0.1:{self.config.chat_port}/chat"
        webbrowser.open(url)

    def toggleLaunchAtLogin_(self, sender):
        self.config.launch_at_login = not self.config.launch_at_login
        self.config.save()
        self.config.apply_launch_at_login()
        self._build_menu()

    def toggleStartOnLaunch_(self, sender):
        self.config.start_on_launch = not self.config.start_on_launch
        self.config.save()
        self._build_menu()

    def quitApp_(self, sender):
        self.server_manager.stop()
        NSApp.terminate_(self)

    # --- Update checking ---

    def _check_for_updates(self):
        import time
        now = time.time()
        if now - self._last_update_check < 86400:
            return
        try:
            resp = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                latest = data.get("tag_name", "").lstrip("v")
                if latest and self._is_newer(latest, __version__):
                    dmg_url = None
                    for asset in data.get("assets", []):
                        if asset.get("name", "").endswith(".dmg"):
                            dmg_url = asset["browser_download_url"]
                            break
                    self._update_info = {
                        "version": latest,
                        "dmg_url": dmg_url,
                        "url": data.get("html_url"),
                    }
                    logger.info(f"Update available: {latest}")
                else:
                    self._update_info = None
            self._last_update_check = now
        except Exception as e:
            logger.debug(f"Update check failed: {e}")

    @staticmethod
    def _is_newer(latest: str, current: str) -> bool:
        from packaging.version import Version
        try:
            lv = Version(latest)
            cv = Version(current)
            if lv.is_prerelease:
                return False
            return lv > cv
        except Exception:
            return False

    def openUpdate_(self, sender):
        if not self._update_info:
            return
        if self._update_info.get("url"):
            webbrowser.open(self._update_info["url"])


def main():
    app = NSApplication.sharedApplication()
    delegate = CanopyAppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()
