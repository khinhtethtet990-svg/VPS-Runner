#!/bin/sh
set -eu
export DATA_DIR="${DATA_DIR:-/app/data}"
mkdir -p "$DATA_DIR" "$DATA_DIR/projects" "$DATA_DIR/logs" "$DATA_DIR/venvs" "$DATA_DIR/backups"
python --version
if command -v node >/dev/null 2>&1; then node --version; else echo "WARNING: Node.js is unavailable; Node projects cannot run" >&2; fi
if command -v npm >/dev/null 2>&1; then npm --version; else echo "WARNING: npm is unavailable; Node dependency installation cannot run" >&2; fi
if command -v ffmpeg >/dev/null 2>&1; then ffmpeg -version | head -n 1; else echo "WARNING: FFmpeg is unavailable; radio/media projects may fail" >&2; fi
exec python -u finalhost.py
