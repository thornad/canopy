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
from .server_manager import PortConflict, ServerManager, ServerStatus
from canopy.release_check import select_latest_stable_release
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
        self._updater: Optional[AppUpdater] = None
        self._update_progress_text: str = ""

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
            result = self.server_manager.start()
            if isinstance(result, PortConflict):
                self._handle_port_conflict(result)

        self._build_menu()

    def statusChangedOnMain_(self, _):
        self._build_menu()

    def healthCheck_(self, timer):
        """Periodic health check and menu refresh."""
        if self.server_manager.status == ServerStatus.RUNNING:
            if not self.server_manager.check_health():
                pass  # ServerManager health loop handles this
        self._update_icon()
        self._check_for_updates()

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

        # Update available / in progress
        if self._update_progress_text:
            prog_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                f"⏳ {self._update_progress_text}", None, ""
            )
            prog_item.setEnabled_(False)
            self.menu.addItem_(prog_item)
            self.menu.addItem_(NSMenuItem.separatorItem())
        elif self._update_info:
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
        result = self.server_manager.start()
        if isinstance(result, PortConflict):
            self._handle_port_conflict(result)
        self._build_menu()

    def stopServer_(self, sender):
        self.server_manager.stop()
        self._build_menu()

    def restartServer_(self, sender):
        # Restart = stop, then start. Stop is a no-op for adopted servers,
        # so this also handles "I want to take ownership of an adopted
        # server" — the start that follows will hit PortConflict and let
        # the user pick Adopt vs Kill & Restart.
        self.server_manager.stop()
        result = self.server_manager.start()
        if isinstance(result, PortConflict):
            self._handle_port_conflict(result)
        self._build_menu()

    # --- Port conflict dialog ---

    def _handle_port_conflict(self, conflict: PortConflict) -> None:
        """Prompt the user when chat_port is already taken."""
        from AppKit import NSAlert, NSAlertFirstButtonReturn, NSAlertSecondButtonReturn

        alert = NSAlert.alloc().init()
        port = self.config.chat_port
        pid_info = f" (PID {conflict.pid})" if conflict.pid else ""

        if conflict.is_canopy:
            alert.setMessageText_("Canopy Server Already Running")
            alert.setInformativeText_(
                f"A Canopy server is already running on port {port}{pid_info}.\n\n"
                f"Adopt it (monitor without restarting) or kill it and start a new one."
            )
            alert.addButtonWithTitle_("Adopt")
            alert.addButtonWithTitle_("Kill & Restart")
            alert.addButtonWithTitle_("Cancel")

            response = alert.runModal()
            if response == NSAlertFirstButtonReturn:
                if not self.server_manager.adopt():
                    logger.error("Adopt failed — external server may have stopped")
            elif response == NSAlertSecondButtonReturn:
                if conflict.pid and self.server_manager.kill_external(conflict.pid):
                    result = self.server_manager.start()
                    if isinstance(result, PortConflict):
                        logger.error("Port still in use after kill")
        else:
            alert.setMessageText_(f"Port {port} In Use")
            alert.setInformativeText_(
                f"Port {port} is held by another application{pid_info}.\n\n"
                f"Stop that process, or change Canopy's port in Preferences."
            )
            alert.addButtonWithTitle_("OK")
            alert.runModal()

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
        """Check GitHub Releases for a new stable version (cached 24 hours)."""
        import time

        now = time.time()
        if now - self._last_update_check < 86400:
            return

        try:
            resp = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases",
                params={"per_page": 20},
                timeout=10,
            )
            if resp.status_code == 200:
                data = select_latest_stable_release(resp.json())
                if data is not None:
                    latest = data["tag_name"].lstrip("v")
                    if self._is_newer(latest, __version__):
                        dmg_url = None
                        for asset in data.get("assets", []):
                            if asset.get("name", "").endswith(".dmg"):
                                dmg_url = asset["browser_download_url"]
                                break
                        self._update_info = {
                            "version": latest,
                            "dmg_url": dmg_url,
                            "url": data.get("html_url"),
                            "notes": data.get("body", ""),
                        }
                        logger.info("Update available: %s", latest)
                        self.performSelectorOnMainThread_withObject_waitUntilDone_(
                            "rebuildMenuOnMain:", None, False
                        )
                    else:
                        self._update_info = None
                else:
                    self._update_info = None
            self._last_update_check = now
        except Exception as e:
            logger.debug("Update check failed: %s", e)

    def rebuildMenuOnMain_(self, _):
        self._build_menu()

    @staticmethod
    def _is_newer(latest: str, current: str) -> bool:
        from packaging.version import Version

        try:
            return Version(latest) > Version(current)
        except Exception:
            return False

    def openUpdate_(self, sender):
        """Show confirmation dialog and start auto-update."""
        if not self._update_info:
            return

        if not self._update_info.get("dmg_url"):
            self._open_update_browser()
            return

        from AppKit import NSAlert, NSAlertFirstButtonReturn

        alert = NSAlert.alloc().init()
        alert.setMessageText_(
            f"Update to Canopy {self._update_info['version']}?"
        )
        notes = self._update_info.get("notes", "")
        if len(notes) > 500:
            notes = notes[:500] + "..."
        alert.setInformativeText_(
            f"{notes}\n\n"
            "The update will be downloaded and installed automatically. "
            "The app will restart when ready."
        )
        alert.addButtonWithTitle_("Update")
        alert.addButtonWithTitle_("Cancel")

        if alert.runModal() != NSAlertFirstButtonReturn:
            return

        self._start_auto_update()

    def _open_update_browser(self):
        url = (
            self._update_info.get("url")
            if self._update_info
            else f"https://github.com/{GITHUB_REPO}/releases"
        )
        webbrowser.open(url)

    def _start_auto_update(self):
        app_path = AppUpdater.get_app_bundle_path()
        if not AppUpdater.is_writable(app_path):
            from AppKit import NSAlert, NSAlertFirstButtonReturn

            alert = NSAlert.alloc().init()
            alert.setMessageText_("Cannot Auto-Update")
            alert.setInformativeText_(
                f"Canopy does not have write permission to {app_path.parent}.\n\n"
                "Please download the update manually from GitHub."
            )
            alert.addButtonWithTitle_("Open GitHub")
            alert.addButtonWithTitle_("Cancel")
            if alert.runModal() == NSAlertFirstButtonReturn:
                self._open_update_browser()
            return

        self._updater = AppUpdater(
            dmg_url=self._update_info["dmg_url"],
            version=self._update_info["version"],
            on_progress=self._on_update_progress,
            on_error=self._on_update_error,
            on_ready=self._on_update_ready,
        )
        self._updater.start()
        self._build_menu()

    def _on_update_progress(self, message: str):
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "updateProgressOnMain:", message, False
        )

    def updateProgressOnMain_(self, message):
        self._update_progress_text = message
        self._build_menu()

    def _on_update_error(self, message: str):
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "updateErrorOnMain:", message, False
        )

    def updateErrorOnMain_(self, message):
        self._updater = None
        self._update_progress_text = ""
        self._build_menu()

        from AppKit import NSAlert, NSAlertFirstButtonReturn

        alert = NSAlert.alloc().init()
        alert.setMessageText_("Update Failed")
        alert.setInformativeText_(
            f"{message}\n\n"
            "Would you like to download the update manually?"
        )
        alert.addButtonWithTitle_("Open GitHub")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() == NSAlertFirstButtonReturn:
            self._open_update_browser()

    def _on_update_ready(self):
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "updateReadyOnMain:", None, False
        )

    def updateReadyOnMain_(self, _):
        self._updater = None
        self._update_progress_text = "Installing update..."
        self._build_menu()
        self._perform_update_and_relaunch()

    def _perform_update_and_relaunch(self):
        self.server_manager.stop()
        if self.health_timer:
            self.health_timer.invalidate()
        if AppUpdater.perform_swap_and_relaunch():
            NSApp.terminate_(None)
        else:
            from AppKit import NSAlert

            alert = NSAlert.alloc().init()
            alert.setMessageText_("Update Failed")
            alert.setInformativeText_(
                "Could not find the staged update. Please try again."
            )
            alert.addButtonWithTitle_("OK")
            alert.runModal()


def main():
    app = NSApplication.sharedApplication()
    delegate = CanopyAppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()
