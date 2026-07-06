#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="${CLAUDE_CODE_CLI_PLUGIN_DIR:-$ROOT}"
HOST="${CLAUDE_CODE_CLI_HOST:-127.0.0.1}"
PORT="${CLAUDE_CODE_CLI_PORT:-8765}"

write_env_file() {
  mkdir -p "$HERMES_HOME_DIR"
  local env_file="$HERMES_HOME_DIR/claude-code-cli-shim.env"
  {
    printf 'HERMES_HOME=%s\n' "$HERMES_HOME_DIR"
    printf 'CLAUDE_CODE_CLI_HOST=%s\n' "$HOST"
    printf 'CLAUDE_CODE_CLI_PORT=%s\n' "$PORT"
    printf 'CLAUDE_CODE_CLI_BASE_URL=http://%s:%s/v1\n' "$HOST" "$PORT"
    printf 'CLAUDE_CODE_CLI_AUTOSTART=0\n'
  } > "$env_file"
  echo "wrote env file: $env_file"
}

install_systemd_user() {
  command -v systemctl >/dev/null 2>&1 || {
    echo "error: systemctl not found; cannot install systemd --user service" >&2
    exit 1
  }
  write_env_file
  local unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  local unit_path="$unit_dir/claude-code-cli-shim.service"
  mkdir -p "$unit_dir"
  sed \
    -e "s#__PLUGIN_DIR__#$PLUGIN_DIR#g" \
    -e "s#__HERMES_HOME__#$HERMES_HOME_DIR#g" \
    "$ROOT/service/systemd/claude-code-cli-shim.service" > "$unit_path"
  systemctl --user daemon-reload
  systemctl --user enable --now claude-code-cli-shim.service
  echo "installed and started systemd --user service: claude-code-cli-shim.service"
  echo "status: systemctl --user status claude-code-cli-shim"
  echo "logs:   journalctl --user -u claude-code-cli-shim -n 50"
}

install_launchd() {
  command -v launchctl >/dev/null 2>&1 || {
    echo "error: launchctl not found; cannot install LaunchAgent" >&2
    exit 1
  }
  write_env_file
  local agents_dir="$HOME/Library/LaunchAgents"
  local logs_dir="$HERMES_HOME_DIR/logs"
  local plist_path="$agents_dir/com.hermes.claude-code-cli-shim.plist"
  mkdir -p "$agents_dir" "$logs_dir"
  sed \
    -e "s#__PLUGIN_DIR__#$PLUGIN_DIR#g" \
    -e "s#__HERMES_HOME__#$HERMES_HOME_DIR#g" \
    -e "s#__HOST__#$HOST#g" \
    -e "s#__PORT__#$PORT#g" \
    -e "s#__LOG_DIR__#$logs_dir#g" \
    "$ROOT/service/launchd/com.hermes.claude-code-cli-shim.plist" > "$plist_path"
  launchctl bootout "gui/$(id -u)" "$plist_path" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$plist_path"
  launchctl enable "gui/$(id -u)/com.hermes.claude-code-cli-shim"
  launchctl kickstart -k "gui/$(id -u)/com.hermes.claude-code-cli-shim"
  echo "installed and started LaunchAgent: $plist_path"
  echo "status: launchctl print gui/$(id -u)/com.hermes.claude-code-cli-shim"
  echo "logs:   tail -n 50 '$logs_dir/claude-code-cli-shim.err.log'"
}

case "$(uname -s)" in
  Linux) install_systemd_user ;;
  Darwin) install_launchd ;;
  *) echo "error: unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac
