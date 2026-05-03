#!/usr/bin/env bash
# Build the Canopy macOS .app + DMG via packaging/build.py.
# Forwards args to build.py (e.g. --skip-venv, --dmg-only).
# Requires: macOS arm64, pipx (for venvstacks), Xcode CLT (for `cc`, `iconutil`, `hdiutil`).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON:-python3}"

cd "$REPO_ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "error: build target is macOS only (uname=$(uname -s))" >&2
    exit 1
fi

for cmd in pipx cc iconutil hdiutil; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "error: '$cmd' not found on PATH" >&2
        case "$cmd" in
            pipx) echo "  install with: brew install pipx && pipx ensurepath" >&2 ;;
            cc|iconutil|hdiutil) echo "  install Xcode Command Line Tools: xcode-select --install" >&2 ;;
        esac
        exit 1
    fi
done

exec "$PYTHON_BIN" packaging/build.py "$@"
