#!/usr/bin/env bash
set -euo pipefail

# Fake-Claude end-to-end smoke for this provider repo.
# Starts claude_code_server.py against a temporary fake `claude` executable on a
# private loopback port, then runs Hermes against a named profile with
# per-invocation provider/env overrides. It never requires real Claude Code auth
# and should never start the real /usr/bin/claude shim.

PROFILE="${1:-${HERMES_CLAUDE_CODE_PROFILE:-claudecli}}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON:-python3}"

# Hermes' runtime may prefer the profile's persisted model.base_url over a
# per-process CLAUDE_CODE_CLI_BASE_URL override, so this smoke intentionally
# starts the fake shim on the profile-configured port.  The dev profile created
# by the setup notes uses 8799 to avoid the provider's real default 8765.
PORT="${CLAUDE_CODE_CLI_FAKE_PORT:-8799}"

TMPDIR="$(mktemp -d)"
SERVER_PID=""
cleanup() {
  if [[ -n "${SERVER_PID:-}" ]]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMPDIR"
}
trap cleanup EXIT

FAKE_CLAUDE="$TMPDIR/claude"
cat > "$FAKE_CLAUDE" <<'PY'
#!/usr/bin/env python3
import json
import os
import sys

prompt = sys.stdin.read()
if os.environ.get("FAKE_CLAUDE_ECHO_PROMPT"):
    print(prompt, file=sys.stderr)
print(json.dumps({
    "result": "FAKE_CLAUDE_PROFILE_SMOKE_OK",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}))
PY
chmod +x "$FAKE_CLAUDE"

LOG="$TMPDIR/shim.log"
CLAUDE_CODE_CLI_BIN="$FAKE_CLAUDE" \
CLAUDE_CODE_CLI_PORT="$PORT" \
CLAUDE_CODE_CLI_ENGINE=never \
CLAUDE_CODE_CLI_AUTOSTART=0 \
"$PYTHON_BIN" "$ROOT/claude_code_server.py" >"$LOG" 2>&1 &
SERVER_PID=$!

"$PYTHON_BIN" - "$PORT" <<'PY'
import sys
import time
import urllib.request

port = sys.argv[1]
url = f"http://127.0.0.1:{port}/healthz"
for _ in range(60):
    try:
        with urllib.request.urlopen(url, timeout=0.5) as resp:
            if resp.status == 200:
                sys.exit(0)
    except Exception:
        time.sleep(0.1)
raise SystemExit(f"shim did not become ready: {url}")
PY

set +e
OUTPUT="$(CLAUDE_CODE_CLI_AUTOSTART=0 \
  CLAUDE_CODE_CLI_API_KEY=local \
  CLAUDE_CODE_CLI_BASE_URL="http://127.0.0.1:$PORT/v1" \
  CLAUDE_CODE_CLI_BIN="$FAKE_CLAUDE" \
  hermes -p "$PROFILE" chat -Q --provider claude-code-cli -m haiku \
    -q 'Return the fake Claude smoke sentinel.' 2>&1)"
STATUS=$?
set -e
printf '%s\n' "$OUTPUT"

if [[ $STATUS -ne 0 ]]; then
  echo "hermes smoke failed with exit $STATUS" >&2
  echo "--- shim log ---" >&2
  sed -n '1,120p' "$LOG" >&2 || true
  exit "$STATUS"
fi

if [[ "$OUTPUT" != *"FAKE_CLAUDE_PROFILE_SMOKE_OK"* ]]; then
  echo "missing fake Claude sentinel in Hermes output" >&2
  echo "--- shim log ---" >&2
  sed -n '1,120p' "$LOG" >&2 || true
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
import socket
for port in (8765,):
    with socket.socket() as s:
        s.settimeout(0.25)
        try:
            s.connect(('127.0.0.1', port))
        except OSError:
            continue
        raise SystemExit(f"unexpected listener on real default shim port {port}")
PY

echo "fake Hermes profile smoke passed on profile '$PROFILE' port $PORT"
