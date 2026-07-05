#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON:-python3}"

"$PYTHON_BIN" -m py_compile \
  __init__.py \
  autostart.py \
  claude_code_server.py \
  prototypes/m0_stream_session.py \
  tests/test_autostart.py \
  tests/test_claude_code_server.py \
  tests/test_provider_plugin_registration.py

"$PYTHON_BIN" -m unittest discover -s tests -v
