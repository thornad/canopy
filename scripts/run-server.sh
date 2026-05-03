#!/usr/bin/env bash
# Run the Canopy chat server in dev mode.
# Creates/reuses .venv at repo root and installs canopy editable on first run.
# Args after --setup-only stop after install. All other args are forwarded
# to `python -m canopy` (e.g. --port 9000 --host 0.0.0.0 --db-path foo.db).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$REPO_ROOT/.venv"
PYTHON_BIN="${PYTHON:-python3}"

cd "$REPO_ROOT"

if [[ ! -d "$VENV_DIR" ]]; then
    echo "[setup] creating venv at $VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

if ! python -c "import canopy, uvicorn" >/dev/null 2>&1; then
    echo "[setup] installing canopy + deps (editable)"
    pip install --upgrade pip >/dev/null
    pip install -e .
fi

if [[ "${1:-}" == "--setup-only" ]]; then
    echo "[setup] done, skipping server start"
    exit 0
fi

exec python -m canopy "$@"
