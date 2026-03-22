"""Entry point for Canopy chat server."""

import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Canopy — cache-aware chat client for oMLX")
    parser.add_argument("--port", type=int, default=8100, help="Server port (default: 8100)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--db-path", type=str, default=None, help="SQLite database path")
    args = parser.parse_args()

    import uvicorn

    from .server import app

    if args.db_path:
        app.state.db_path = Path(args.db_path)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
