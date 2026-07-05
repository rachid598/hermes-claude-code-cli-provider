#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON:-python3}"
"$PYTHON_BIN" - <<'PY'
import socket
import subprocess
import sys

ports = (8765, 8799)
bad = False
for port in ports:
    with socket.socket() as s:
        s.settimeout(0.25)
        try:
            s.connect(('127.0.0.1', port))
        except OSError:
            print(f'port_{port}=free')
        else:
            print(f'port_{port}=listening')
            bad = True

out = subprocess.check_output(['ps', '-eo', 'pid,ppid,cmd'], text=True)
hits = [
    line.strip()
    for line in out.splitlines()
    if 'claude_code_server.py' in line and 'python' in line and 'ps -eo' not in line
]
print('claude_code_server_processes=', len(hits))
for hit in hits:
    print(hit)
    bad = True

if bad:
    sys.exit(1)
PY
