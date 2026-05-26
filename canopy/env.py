"""Environment utilities for Canopy.

macOS apps launched from Finder (not a terminal) inherit a stripped-down
PATH from launchd: ``/usr/bin:/bin:/usr/sbin:/sbin``.  This breaks MCP
servers that rely on Homebrew, npm, pipx, etc.  The helpers here recover
 the user's real shell PATH and resolve commands to absolute paths.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Common directories where user tools may live.  These are *fallbacks* — the
# primary source of truth is the user's login-shell PATH.
_FALLBACK_PATHS = [
    "/opt/homebrew/bin",          # Homebrew (Apple Silicon)
    "/usr/local/bin",             # Homebrew (Intel) / local
    "/usr/local/opt/node/bin",    # Homebrew node
    str(Path.home() / ".local/bin"),
    str(Path.home() / ".cargo/bin"),
    str(Path.home() / ".npm-global/bin"),
    str(Path.home() / ".nvm/versions/node/current/bin"),
    str(Path.home() / ".poetry/bin"),
    str(Path.home() / ".config/composer/vendor/bin"),
    str(Path.home() / ".rbenv/shims"),
    str(Path.home() / ".pyenv/shims"),
    str(Path.home() / ".pixi/bin"),
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
]


def _get_shell() -> str:
    """Return the user's login shell or a safe default."""
    return os.environ.get("SHELL", "/bin/zsh")


def get_shell_path() -> str:
    """Query the user's login shell for its ``$PATH``.

    Returns the raw ``PATH`` string (colon-delimited) or an empty string if
    the probe fails.
    """
    shell = _get_shell()
    if not shutil.which(os.path.basename(shell)):
        log.debug("Shell %s not found in current PATH", shell)
        return ""

    try:
        result = subprocess.run(
            [shell, "-l", "-c", "printf '%s' $PATH"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            path = result.stdout.strip()
            if path:
                log.debug("Shell PATH (%s): %s", shell, path)
                return path
    except Exception as exc:
        log.debug("Shell PATH probe failed: %s", exc)

    return ""


def _dedupe_path(path: str) -> str:
    """Remove duplicate and empty entries from a colon-delimited PATH."""
    seen: set[str] = set()
    out: list[str] = []
    for p in path.split(":"):
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return ":".join(out)


def build_mcp_path(
    base_env: Optional[dict[str, str]] = None,
    extra_paths: Optional[list[str]] = None,
) -> str:
    """Build a robust ``PATH`` for MCP server subprocesses.

    1. Uses ``base_env.get("PATH")`` (or ``os.environ``) as the starting point.
    2. If that looks like the macOS launchd default, probes the user's login
       shell for the real PATH and prepends it.
    3. Appends *extra_paths* (or the built-in fallback list) for any entries
       still missing.
    4. Deduplicates while preserving order.
    """
    if base_env is None:
        base_env = os.environ

    current = base_env.get("PATH", "")

    # If we're in a stripped macOS app environment, try to get the shell PATH.
    # Heuristic: the launchd default is very short and only contains system dirs.
    shell_path = ""
    if not current or all(p in current for p in ("/usr/bin", "/bin", "/usr/sbin", "/sbin")):
        if len(current.split(":")) <= 4:
            shell_path = get_shell_path()

    # Build the full ordered list of path segments.
    segments: list[str] = []
    if shell_path:
        segments.extend(shell_path.split(":"))
    if current:
        segments.extend(current.split(":"))

    # Append fallbacks so manually-added entries are still present even if the
    # shell probe fails (e.g. user has a non-login shell or the shell rc
    # doesn't export PATH in non-interactive mode).
    fallbacks = extra_paths or _FALLBACK_PATHS
    for p in fallbacks:
        segments.append(p)

    return _dedupe_path(":".join(segments))


def resolve_command(command: str, path: str) -> str:
    """Resolve *command* to an absolute path using *path*.

    If resolution fails, returns the original *command* unchanged so the
    shell can still attempt a lookup at runtime.
    """
    if not command:
        return command
    if os.path.isabs(command):
        return command

    resolved = shutil.which(command, path=path)
    if resolved:
        log.debug("Resolved %s → %s", command, resolved)
        return resolved

    log.debug("Could not resolve %s in PATH, leaving as-is", command)
    return command


def build_mcp_env(
    base_env: Optional[dict[str, str]] = None,
    overrides: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Build a complete environment dict for an MCP server.

    * ``base_env`` – starting environment (defaults to ``os.environ``).
    * ``overrides`` – per-server env vars from config (e.g. ``{"API_KEY": "x"}``).

    Returns a new dict with the computed ``PATH`` and any overrides applied.
    """
    if base_env is None:
        base_env = dict(os.environ)
    else:
        base_env = dict(base_env)

    # The macOS .app launcher sets PYTHONHOME/PYTHONPATH to the bundled
    # runtime.  These are correct for the canopy process itself but break
    # any external tool that spawns its own Python (uv, pipx, npx, etc.).
    for var in ("PYTHONHOME", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE"):
        base_env.pop(var, None)

    base_env["PATH"] = build_mcp_path(base_env)

    if overrides:
        base_env.update(overrides)
        # If the override included PATH, make sure we still dedupe it.
        if "PATH" in overrides:
            base_env["PATH"] = _dedupe_path(base_env["PATH"])

    return base_env
