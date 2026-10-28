#!/usr/bin/env bash
set -euo pipefail
uv run fastapi dev app/main.py --host 0.0.0.0 --port 8000 --reload-dir app
