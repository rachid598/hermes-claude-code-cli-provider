# Claude Code CLI Provider for Hermes

[![CI](https://github.com/Ouroborosrex/hermes-claude-code-cli-provider/actions/workflows/test.yml/badge.svg)](https://github.com/Ouroborosrex/hermes-claude-code-cli-provider/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![Hermes](https://img.shields.io/badge/Hermes-model%20provider-6f42c1.svg)](https://github.com/NousResearch/hermes-agent)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-CLI-orange.svg)](https://docs.claude.com/en/docs/claude-code)
[![API](https://img.shields.io/badge/API-OpenAI%20Chat%20Completions-green.svg)](#endpoints)
[![Platforms](https://img.shields.io/badge/platform-Linux%20%7C%20macOS-lightgrey.svg)](#managed-service)
[![Issues](https://img.shields.io/github/issues/Ouroborosrex/hermes-claude-code-cli-provider.svg)](https://github.com/Ouroborosrex/hermes-claude-code-cli-provider/issues)
[![Last commit](https://img.shields.io/github/last-commit/Ouroborosrex/hermes-claude-code-cli-provider.svg)](https://github.com/Ouroborosrex/hermes-claude-code-cli-provider/commits/main)

A local [Hermes](https://github.com/NousResearch/hermes-agent) model-provider plugin that exposes the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) through an OpenAI-compatible Chat Completions shim. Hermes talks to `http://127.0.0.1:8765/v1`, and the shim runs `claude -p` using the local Claude Code login. No Anthropic API key is stored in Hermes.

Use it for local Claude Code powered chat, advisory work, streaming, vision inputs, and one-shot engine-mode tasks. Hermes-native tool event streaming and interactive permission bridging require deeper Hermes core integration and are tracked separately.

## Features

- User plugin for Hermes. No changes to bundled Hermes code.
- Local OpenAI-compatible `/v1/chat/completions` and `/v1/models` endpoints.
- Optional autostart plus managed `systemd --user` and macOS LaunchAgent support.
- Live Claude Code `stream-json` to OpenAI Server-Sent Events translation.
- Bounded image passthrough for OpenAI `image_url` content parts.
- Engine mode for autonomous one-shot Claude Code tool use.
- Fake-Claude test harness that does not require Claude Code authentication.

## Requirements

- Hermes Agent CLI.
- Claude Code CLI installed and logged in.
- Python 3.11 or newer. CI covers Python 3.11, 3.12, and 3.13.

## Install

Install into the active Hermes home:

```bash
git clone https://github.com/Ouroborosrex/hermes-claude-code-cli-provider \
  "${HERMES_HOME:-$HOME/.hermes}/plugins/model-providers/claude-code-cli"
```

For development from a checkout, use the symlink installer:

```bash
git clone https://github.com/Ouroborosrex/hermes-claude-code-cli-provider
cd hermes-claude-code-cli-provider
scripts/install_plugin.sh
```

Optional: copy entries from `.env.example` into `${HERMES_HOME:-$HOME/.hermes}/.env` if you want explicit local settings.

## Start the shim

```bash
"${HERMES_HOME:-$HOME/.hermes}/plugins/model-providers/claude-code-cli/start.sh"
```

Health checks:

```bash
curl -s http://127.0.0.1:8765/healthz
curl -s http://127.0.0.1:8765/v1/models
```

### Managed service

To keep the shim running across logout or reboot:

```bash
cd "${HERMES_HOME:-$HOME/.hermes}/plugins/model-providers/claude-code-cli"
scripts/install-service.sh
```

- Linux: installs `claude-code-cli-shim.service` as a `systemd --user` unit.
- macOS: installs `com.hermes.claude-code-cli-shim` as a LaunchAgent.
- Remove the service with `scripts/uninstall-service.sh`.

## Configure Hermes

Run the model picker and select `Claude Code (local CLI)`:

```bash
hermes model
```

When prompted for an API key, enter any non-empty placeholder such as `local`. The shim ignores it, but Hermes expects API-key providers to have a value.

You can also set the common environment values before running `hermes model`:

```bash
export CLAUDE_CODE_CLI_API_KEY=local
export CLAUDE_CODE_CLI_BASE_URL=http://127.0.0.1:8765/v1
```

Quick smoke check:

```bash
hermes chat -Q --provider claude-code-cli -m haiku -q "Say hello from Claude Code."
```

## Configuration

Most users only need the defaults. See `.env.example` for the full list.

| Variable | Default | Purpose |
|---|---:|---|
| `CLAUDE_CODE_CLI_BASE_URL` | `http://127.0.0.1:8765/v1` | Endpoint Hermes calls. |
| `CLAUDE_CODE_CLI_API_KEY` | `local` | Placeholder stored by Hermes and ignored by the shim. |
| `CLAUDE_CODE_CLI_HOST` | `127.0.0.1` | Shim bind host. |
| `CLAUDE_CODE_CLI_PORT` | `8765` | Shim bind port. |
| `CLAUDE_CODE_CLI_BIN` | autodetect | Path to the `claude` binary. |
| `CLAUDE_CODE_CLI_MODEL` | `sonnet` | Fallback model. |
| `CLAUDE_CODE_CLI_AUTOSTART` | `1` | Start the shim during provider load when needed. |
| `CLAUDE_CODE_CLI_ENGINE` | `auto` | `auto`, `always`, or `never`. |
| `CLAUDE_CODE_CLI_CWD` | `$HOME` | Working directory for Claude Code engine-mode runs. |
| `CLAUDE_CODE_CLI_ENGINE_TOOLS` | common file and web tools | Tool allowlist for engine mode. |
| `CLAUDE_CODE_CLI_STREAM` | `1` | Use live `stream-json` for streaming requests. |
| `CLAUDE_CODE_CLI_VISION` | `1` | Pass image inputs through when possible. |
| `CLAUDE_CODE_CLI_TIMEOUT` | `600` | Per-request timeout in seconds. |

## Behavior notes

### Engine mode

`claude -p` is a complete agent with its own tool loop. In `auto` mode, the shim enables engine mode when the Hermes request includes tool definitions. Claude Code then performs the work with its local tools and returns final assistant text.

Engine mode can read files, edit files, and run commands in `CLAUDE_CODE_CLI_CWD`. Set that directory deliberately, restrict tools with `CLAUDE_CODE_CLI_ENGINE_TOOLS`, or use `CLAUDE_CODE_CLI_ENGINE=never` for text-only behavior.

### Streaming

For `stream: true`, the shim uses:

```bash
claude -p --output-format stream-json --verbose --include-partial-messages
```

Text deltas are forwarded as OpenAI-compatible Server-Sent Events. If Claude Code returns a single buffered JSON object, the shim falls back to one content chunk followed by `[DONE]`.

### Vision inputs

OpenAI `image_url` parts are handled as follows:

- Data URLs and base64 payloads are written to bounded per-request temp files.
- Remote `http` and `https` URLs are passed through as image references.
- Temp files are removed after the request finishes.
- Limits are controlled by `CLAUDE_CODE_CLI_MAX_IMAGES` and `CLAUDE_CODE_CLI_MAX_IMAGE_MB`.

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/healthz` | Liveness check. |
| `GET` | `/v1/models` | Advertises `opus`, `sonnet`, and `haiku`. |
| `POST` | `/v1/chat/completions` | Chat completion with optional streaming. |

## Development

Default tests use a fake `claude` executable. They do not require Claude Code authentication and do not spend Claude usage.

```bash
scripts/test.sh
```

Optional Hermes integration smoke with a disposable profile:

```bash
scripts/smoke_fake_hermes_profile.sh <profile-name>
scripts/check_clean_runtime.sh
```

Run real Claude Code probes only when you intend to exercise a logged-in Claude Code installation.

## Uninstall

If you installed the managed service, remove it first:

```bash
cd "${HERMES_HOME:-$HOME/.hermes}/plugins/model-providers/claude-code-cli"
scripts/uninstall-service.sh
```

Then remove the plugin:

```bash
rm -rf "${HERMES_HOME:-$HOME/.hermes}/plugins/model-providers/claude-code-cli"
```

Select another Hermes model with `hermes model`.

## License

MIT. See [LICENSE](LICENSE).
