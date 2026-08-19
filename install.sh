#!/usr/bin/env bash
# ============================================================
#  COA Reviewer Web App - macOS / Linux one-shot bootstrap
#  1. Creates a local .venv (if missing)
#  2. Installs Python dependencies + Playwright Chromium
#  3. Launches the Flask web app
#
#  Re-running is safe — the venv is reused and pip will be a
#  no-op for already-installed packages. Override the system
#  Python with PYTHON=python3.12 ./install.sh if needed.
# ============================================================

set -e

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if [ ! -d ".venv" ]; then
    echo
    echo "=== Creating virtual environment in .venv ==="
    "$PYTHON" -m venv .venv
else
    echo
    echo "=== Reusing existing .venv ==="
fi

echo
echo "=== Installing Python packages into .venv ==="
.venv/bin/pip install --upgrade pip

.venv/bin/pip install \
    'flask>=3.0.0' \
    'PyJWT>=2.8.0' \
    'requests>=2.31.0' \
    'playwright>=1.40.0' \
    'pymupdf>=1.23.0' \
    'pyzbar>=0.1.9' \
    'pystray>=0.19.0' \
    'Pillow>=10.0.0' \
    'pytest>=8.0'

echo
echo "=== Installing Playwright Chromium ==="
.venv/bin/python -m playwright install chromium

echo
echo "=== Launching COA Reviewer Web App ==="
echo "Press Ctrl+C to stop the server."
exec .venv/bin/python app.py
