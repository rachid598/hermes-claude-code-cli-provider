#!/usr/bin/env bash
set -euo pipefail

HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"

uninstall_systemd_user() {
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now claude-code-cli-shim.service >/dev/null 2>&1 || true
    rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/claude-code-cli-shim.service"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
  rm -f "$HERMES_HOME_DIR/claude-code-cli-shim.env"
  echo "removed systemd --user service claude-code-cli-shim.service"
}

uninstall_launchd() {
  local plist_path="$HOME/Library/LaunchAgents/com.hermes.claude-code-cli-shim.plist"
  if command -v launchctl >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)" "$plist_path" >/dev/null 2>&1 || true
  fi
  rm -f "$plist_path" "$HERMES_HOME_DIR/claude-code-cli-shim.env"
  echo "removed LaunchAgent com.hermes.claude-code-cli-shim"
}

case "$(uname -s)" in
  Linux) uninstall_systemd_user ;;
  Darwin) uninstall_launchd ;;
  *) echo "error: unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac
