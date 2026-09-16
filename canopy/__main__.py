"""Entry point for Canopy chat server."""

import argparse
import os
import signal
import sys
import threading
import time
from pathlib import Path


def watch_parent(parent_pid: int, on_orphaned, interval: float = 2.0) -> threading.Thread:
    """Call ``on_orphaned`` once ``parent_pid`` is no longer our parent.

    The menubar app starts the server in its own session so a Ctrl-C in a
    terminal doesn't reach it, which also means the server outlives the app
    if the app dies without stopping it (crash, force quit, SIGKILL). When
    the parent goes away the server is re-parented, so ``getppid()`` changes.
    """
    def _loop():
        while os.getppid() == parent_pid:
            time.sleep(interval)
        on_orphaned()

    thread = threading.Thread(target=_loop, name="parent-watch", daemon=True)
    thread.start()
    return thread


def main():
    parser = argparse.ArgumentParser(description="Canopy — cache-aware chat client for oMLX")
    parser.add_argument("--port", type=int, default=8100, help="Server port (default: 8100)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--db-path", type=str, default=None, help="SQLite database path")
    parser.add_argument("--parent-pid", type=int, default=None,
                        help="Shut down when this process exits (set by the menubar app)")
    args = parser.parse_args()

    import uvicorn

    from .server import app

    if args.db_path:
        app.state.db_path = Path(args.db_path)

    if args.parent_pid:
        # SIGTERM lets uvicorn shut down gracefully, so the lifespan hook
        # still stops MCP servers and closes the database.
        watch_parent(args.parent_pid, lambda: os.kill(os.getpid(), signal.SIGTERM))

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
