#!/bin/bash
# Start the Gain AI backend server.
# Uses absolute path to venv Python so macOS sandbox doesn't need pyvenv.cfg.
cd "$(dirname "$0")"
export PYTHONPATH="$(pwd)"
exec "$(pwd)/.venv/bin/python3" -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
