"""Canopy FastAPI server process lifecycle management."""

import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Union

import requests

from .config import LOG_DIR, CanopyConfig

logger = logging.getLogger(__name__)


class ServerStatus(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass
class PortConflict:
    """Returned by start() when chat_port is already in use.

    The app uses ``is_canopy`` to decide between the "Adopt" path
    (a Canopy server is already running — monitor it) and the
    "kill or change port" path (something else holds the port).
    """
    pid: Optional[int]
    is_canopy: bool


def _get_python() -> str:
    """Find bundled python3 next to the running executable, or fall back to sys.executable."""
    exe = Path(sys.executable)
    # In .app bundle: MacOS/Canopy sits next to MacOS/python3
    bundled = exe.parent / "python3"
    if bundled.exists():
        return str(bundled)
    # Dev mode: use sys.executable directly
    return sys.executable


class ServerManager:
    """Manages the Canopy FastAPI server subprocess."""

    def __init__(self, config: CanopyConfig):
        self.config = config
        self.status = ServerStatus.STOPPED
        self._process: Optional[subprocess.Popen] = None
        self._health_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._restart_count = 0
        self._last_stable_time = 0.0
        self._on_status_change = None
        # When True, an external Canopy server holds chat_port and we
        # only monitor its health — we did not spawn it and must not
        # kill it. stop() honours this; the health loop reports ERROR
        # rather than auto-restarting if the external server dies.
        self._adopted = False

    def set_status_callback(self, callback):
        self._on_status_change = callback

    def _update_status(self, new_status: ServerStatus):
        # Once stop() has signalled shutdown, refuse to flip back to
        # RUNNING — the health loop may still be mid-iteration when
        # stop() runs, and we don't want a late check_health() to
        # overwrite the STOPPED status the caller just set.
        if (
            new_status == ServerStatus.RUNNING
            and self._stop_event.is_set()
        ):
            return
        if self.status != new_status:
            self.status = new_status
            if self._on_status_change:
                self._on_status_change(new_status)

    def start(self) -> Union[bool, PortConflict]:
        """Start the Canopy server.

        Returns ``True`` on a clean spawn, ``False`` on a spawn error
        unrelated to port conflict, or a ``PortConflict`` describing
        the existing port owner so the caller can offer Adopt /
        Kill & Restart in the UI.
        """
        if self.status in (ServerStatus.RUNNING, ServerStatus.STARTING):
            return True

        # Pre-flight: refuse to spawn if chat_port is already taken.
        # Two outcomes worth distinguishing for the user:
        #   - It's a Canopy server already → offer to adopt it.
        #   - It's something else → tell them and let them change ports.
        if self._is_port_in_use():
            pid = self._find_port_owner_pid()
            return PortConflict(pid=pid, is_canopy=self._is_canopy_server())

        self._adopted = False
        self._update_status(ServerStatus.STARTING)
        self._stop_event.clear()

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOG_DIR / "server.log"

        python = _get_python()
        args = [python, "-m", "canopy"] + self.config.build_serve_args()

        try:
            log_fh = open(log_file, "a")
            self._process = subprocess.Popen(
                args,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as e:
            logger.error(f"Failed to start server: {e}")
            self._update_status(ServerStatus.ERROR)
            return False

        # Start health check thread
        self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
        self._health_thread.start()

        return True

    # --- Port conflict / adoption ---

    def _is_port_in_use(self) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(("127.0.0.1", self.config.chat_port))
                return True
        except (ConnectionRefusedError, OSError):
            return False

    def _is_canopy_server(self) -> bool:
        """Probe /api/health to confirm the port owner is a Canopy server."""
        return self.check_health()

    def _find_port_owner_pid(self) -> Optional[int]:
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{self.config.chat_port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(result.stdout.strip().splitlines()[0])
        except Exception as e:
            logger.debug(f"lsof failed: {e}")
        return None

    def kill_external(self, pid: int) -> bool:
        """SIGTERM, then SIGKILL after 5s, an external server we don't own."""
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):  # ≤ 5s
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except OSError:
                    return True
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.5)
            return True
        except OSError as e:
            logger.error(f"Failed to kill PID {pid}: {e}")
            return False

    def adopt(self) -> bool:
        """Take over monitoring of an externally-running Canopy server.

        Sets ``_adopted`` so stop() won't kill the foreign process and
        the health loop reports ERROR (not auto-restart) if it dies.
        """
        if not self._is_canopy_server():
            return False

        self._adopted = True
        self._process = None
        self._stop_event.clear()
        self._update_status(ServerStatus.RUNNING)
        self._last_stable_time = time.time()

        self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
        self._health_thread.start()
        logger.info(f"Adopted external Canopy server on port {self.config.chat_port}")
        return True

    def stop(self, timeout: float = 10.0):
        """Stop the server gracefully.

        Adopted servers: we did not start the process, so we only stop
        monitoring it — the external process keeps running.
        """
        if self.status == ServerStatus.STOPPED:
            return

        self._update_status(ServerStatus.STOPPING)
        self._stop_event.set()

        if self._adopted:
            self._adopted = False
            self._restart_count = 0
            self._update_status(ServerStatus.STOPPED)
            return

        if self._process and self._process.poll() is None:
            try:
                os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
                self._process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logger.warning("Server didn't stop gracefully, killing")
                os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
                self._process.wait(timeout=5)
            except Exception as e:
                logger.error(f"Error stopping server: {e}")

        self._process = None
        self._restart_count = 0
        self._update_status(ServerStatus.STOPPED)

    def check_health(self) -> bool:
        """Check if server is responding."""
        try:
            resp = requests.get(
                f"http://127.0.0.1:{self.config.chat_port}/api/health",
                timeout=3,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def _health_loop(self):
        """Background thread: monitor server health."""
        # Wait for server to start
        for _ in range(30):  # 15 seconds max
            if self._stop_event.is_set():
                return
            if self.check_health():
                self._update_status(ServerStatus.RUNNING)
                self._last_stable_time = time.time()
                break
            time.sleep(0.5)
        else:
            # Server didn't start in time
            if self._process and self._process.poll() is not None:
                logger.error("Server process exited during startup")
                self._update_status(ServerStatus.ERROR)
            return

        # Monitor health
        fail_count = 0
        while not self._stop_event.is_set():
            time.sleep(5)
            if self._stop_event.is_set():
                return

            # Check if process is still running
            if self._process and self._process.poll() is not None:
                logger.warning(f"Server process exited (code {self._process.returncode})")
                if self._restart_count < 3:
                    self._restart_count += 1
                    backoff = min(5 * self._restart_count, 20)
                    logger.info(f"Auto-restart {self._restart_count}/3 in {backoff}s")
                    self._update_status(ServerStatus.STARTING)
                    time.sleep(backoff)
                    if not self._stop_event.is_set():
                        self.start()
                else:
                    self._update_status(ServerStatus.ERROR)
                return

            if self.check_health():
                fail_count = 0
                if self.status != ServerStatus.RUNNING:
                    self._update_status(ServerStatus.RUNNING)
                # Reset restart counter after 60s stable
                if time.time() - self._last_stable_time > 60:
                    self._restart_count = 0
                    self._last_stable_time = time.time()
            else:
                fail_count += 1
                if self._adopted:
                    # External server we don't own — don't auto-restart,
                    # just report it stopped responding.
                    logger.warning("Adopted Canopy server stopped responding")
                    self._adopted = False
                    self._update_status(ServerStatus.ERROR)
                    return
                if fail_count >= 3:
                    logger.warning("Server unresponsive (3 consecutive health check failures)")
                    self._update_status(ServerStatus.ERROR)
