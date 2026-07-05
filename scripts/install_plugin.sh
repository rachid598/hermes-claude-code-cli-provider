#!/usr/bin/env bash
set -euo pipefail

# Install this checkout as a Hermes model-provider plugin by symlinking it into
# the selected HERMES_HOME.  Defaults to the active/default Hermes home.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME_DIR/plugins/model-providers"
TARGET="$PLUGIN_DIR/claude-code-cli"

mkdir -p "$PLUGIN_DIR"

if [[ -e "$TARGET" || -L "$TARGET" ]]; then
  if [[ "$(readlink -f "$TARGET" 2>/dev/null || true)" == "$ROOT" ]]; then
    echo "already installed: $TARGET -> $ROOT"
    exit 0
  fi
  echo "error: $TARGET already exists and does not point at this checkout" >&2
  echo "       remove it first or set HERMES_HOME to another profile/home" >&2
  exit 1
fi

ln -s "$ROOT" "$TARGET"
echo "installed: $TARGET -> $ROOT"
echo "next: copy .env.example entries into $HERMES_HOME_DIR/.env as needed, then run hermes setup or hermes model"
