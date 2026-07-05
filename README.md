# hermes-claude-code-cli-provider

[![test](https://github.com/Ouroborosrex/hermes-claude-code-cli-provider/actions/workflows/test.yml/badge.svg)](https://github.com/Ouroborosrex/hermes-claude-code-cli-provider/actions/workflows/test.yml)

A local [Hermes](https://github.com/NousResearch/hermes-agent) inference
**provider** that routes through the **Claude Code CLI** (`claude -p`) instead of
the Anthropic API — the same way the `fusion-consult` skill drives Claude Code as
an advisory worker. No Anthropic API key and no network egress: it reuses your
existing local `claude` login.

It ships as a Hermes **user plugin** plus a tiny OpenAI-compatible shim, so it
adds the provider without editing any bundled `hermes-agent` code and is removed
by deleting one directory.

```
claude-code-cli/
├── __init__.py            registers the ProviderProfile (auto-wires into setup)
├── plugin.yaml            plugin manifest
├── claude_code_server.py  OpenAI-compatible shim that shells out to `claude -p`
├── autostart.py           best-effort profile-aware shim autostart
├── start.sh               launcher for the shim
├── scripts/               install, test, and fake-smoke helpers
├── tests/                 fake-Claude and provider-registration tests
└── README.md              this file
```

## How it works

```
Hermes agent
  └─ chat_completions transport  ──HTTP──▶  claude_code_server.py (127.0.0.1:8765)
                                                  └─ claude -p --output-format json
                                                        └─ returns {"result": "..."}
                                              ◀── OpenAI chat.completion ──┘
```

The profile declares `auth_type="api_key"` with a non-empty `env_vars`, so the
Hermes registry folds it into `CANONICAL_PROVIDERS` (the `hermes setup` /
`hermes model` picker) and `PROVIDER_REGISTRY` (the credential/model flow)
automatically. The transport just sees an ordinary OpenAI-compatible endpoint at
`http://127.0.0.1:8765/v1`; the shim turns each request into a `claude -p`
subprocess.

## Engine mode — use Claude Code as the engine

`claude -p` is itself a complete agent that runs its **own** tool loop. The shim
leans into that: when a request carries tool definitions (an agentic Hermes
turn), it runs Claude Code with its **own** tools enabled (`Read`, `Write`,
`Edit`, `Bash`, `Glob`, `Grep`, …) so it actually does the work — reads/edits
files, runs commands — then returns the result. Requests with no tools (Hermes
auxiliary tasks: title generation, compression) stay text-only.

This is controlled by `CLAUDE_CODE_CLI_ENGINE` (`auto` by default — engine when
tools are present; `always`/`never` to force).

**What it gets you:** a working Claude-Code-powered agent, billed to your normal
Claude Code plan (no API/extra-usage charge).

**What it still is NOT:** the shim returns Claude's **final text**, not
OpenAI-style `tool_calls`. So Hermes sees the *result*, not step-by-step tool
events, and it's Claude Code's own tools doing the work — not Hermes' tools. For
Hermes-orchestrated tool-calling, use the bundled **`anthropic`** provider
instead (note: third-party API use now draws from paid *extra usage*, not your
plan).

> ⚠️ **Interactive Hermes features don't work in engine mode.** Because Claude
> Code owns the loop and runs one autonomous pass per turn, anything that needs
> Hermes to drive an interactive back-and-forth — **browsing/picking skills,
> approval prompts, step-by-step Hermes tool use, multi-turn Hermes pickers** —
> collapses to a one-shot text dump of "what Claude Code could see," with no
> interactivity. Engine mode is for **autonomous one-shot tasks** ("read the
> issues", "edit this file"); for interactive Hermes flows use a tool-calling
> provider. Restoring interactivity on the Claude Code path is the core
> integration tracked in the project epic (the `claude_stream` `api_mode` + its
> permission/approval bridge).

> ⚠️ **Engine mode executes autonomously.** It pre-approves a capable tool set
> (incl. `Bash` and file edits) via `--allowedTools`, so it will modify files
> and run commands without prompting, **inside `CLAUDE_CODE_CLI_CWD`** (default:
> your home directory). For project work, set `CLAUDE_CODE_CLI_CWD` to the repo
> and restart the shim. Restrict the toolset with `CLAUDE_CODE_CLI_ENGINE_TOOLS`
> (e.g. `Read,Grep,Glob` for read-only), or set `CLAUDE_CODE_CLI_ENGINE=never`
> to keep the provider text-only.

## Requirements

- [Hermes](https://github.com/NousResearch/hermes-agent) installed (`hermes` CLI).
- The [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) (`claude`)
  installed and logged in.
- Python 3 (standard library only — the shim has no dependencies).

## Install

Clone (or copy) this repo into your Hermes plugins directory as
`claude-code-cli`:

```bash
git clone https://github.com/Ouroborosrex/hermes-claude-code-cli-provider \
  "${HERMES_HOME:-$HOME/.hermes}/plugins/model-providers/claude-code-cli"
```

For development from a checkout, symlink the current tree instead:

```bash
git clone https://github.com/Ouroborosrex/hermes-claude-code-cli-provider
cd hermes-claude-code-cli-provider
scripts/install_plugin.sh
```

Copy `.env.example` entries into your Hermes `.env` if you want explicit local
settings. `CLAUDE_CODE_CLI_API_KEY=local` is only a placeholder accepted by the
local shim; it is not sent to Anthropic.

## Usage

1. **Start the shim** (keep it running):

   ```bash
   "${HERMES_HOME:-$HOME/.hermes}/plugins/model-providers/claude-code-cli/start.sh"
   ```

   To run it detached so it survives your shell session:

   ```bash
   cd "${HERMES_HOME:-$HOME/.hermes}/plugins/model-providers/claude-code-cli"
   setsid nohup python3 claude_code_server.py > /tmp/claude-code-cli-shim.log 2>&1 < /dev/null &
   ```

   Sanity-check it:

   ```bash
   curl -s http://127.0.0.1:8765/healthz
   curl -s http://127.0.0.1:8765/v1/models
   ```

2. **Select it in Hermes:**

   ```bash
   hermes setup        # → Inference Provider → "Claude Code (local CLI)"
   #   or directly:
   hermes model
   ```

   - When prompted for `CLAUDE_CODE_CLI_API_KEY`, enter any non-empty
     placeholder (e.g. `local`) — the shim ignores it. To skip the prompt
     entirely, export `CLAUDE_CODE_CLI_API_KEY=local` before running setup.
   - Pick a model: `opus`, `sonnet`, or `haiku` (forwarded verbatim to
     `claude --model`). If the shim is running, these are listed from
     `/v1/models`; otherwise just type the name.

> **Auto-start:** when this provider is configured (main model or any auxiliary
> task), the plugin starts the shim for you on first use if it isn't already
> listening — so a reboot no longer silently breaks it. Disable with
> `CLAUDE_CODE_CLI_AUTOSTART=0` and start it manually (step 1). If you ever see
> `APIConnectionError` against `http://127.0.0.1:8765/v1` with autostart off,
> the shim isn't running.
>
> The autostart gate is profile-aware: Hermes sets `HERMES_HOME` before provider
> plugin discovery, so the plugin reads that profile's `.env` directly before
> deciding whether to spawn. A profile-local `CLAUDE_CODE_CLI_AUTOSTART=0` is
> therefore honored even when Hermes has not loaded the profile `.env` into
> `os.environ` yet.

## Configuration

The shim reads these environment variables (all optional):

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_CODE_CLI_HOST` | `127.0.0.1` | Bind host. |
| `CLAUDE_CODE_CLI_PORT` | `8765` | Bind port. Keep in sync with `base_url`. |
| `CLAUDE_CODE_CLI_BIN` | autodetect | Path to the `claude` binary. |
| `CLAUDE_CODE_CLI_MODEL` | `sonnet` | Fallback model when a request omits one. |
| `CLAUDE_CODE_CLI_EFFORT` | `high` | `--effort` value (empty string omits it). |
| `CLAUDE_CODE_CLI_TOOLS` | `""` | Text-mode `--tools` value; empty = no CLI tools. |
| `CLAUDE_CODE_CLI_DISALLOWED_TOOLS` | _unset_ | `--disallowedTools` value (both modes). |
| `CLAUDE_CODE_CLI_MAX_TURNS` | `12` | Text-mode `--max-turns`. |
| `CLAUDE_CODE_CLI_ENGINE` | `auto` | `auto` (engine when request has tools), `always`, or `never`. |
| `CLAUDE_CODE_CLI_ENGINE_TOOLS` | `Read,Write,Edit,Bash,Glob,Grep,WebFetch,WebSearch,TodoWrite` | Tools pre-approved (`--allowedTools`) in engine mode. |
| `CLAUDE_CODE_CLI_ENGINE_MAX_TURNS` | `40` | Engine-mode `--max-turns`. |
| `CLAUDE_CODE_CLI_ENGINE_PERMISSION` | _unset_ | Set to `bypass` to add `--dangerously-skip-permissions`. |
| `CLAUDE_CODE_CLI_CWD` | `$HOME` | Working directory Claude Code operates in (engine mode). |
| `CLAUDE_CODE_CLI_ADD_DIR` | _unset_ | Extra dirs (`--add-dir`), `os.pathsep`-separated. |
| `CLAUDE_CODE_CLI_TIMEOUT` | `600` | Per-request timeout (seconds). |
| `CLAUDE_CODE_CLI_EXTRA_ARGS` | _unset_ | Extra argv appended to every call (shlex-split). |
| `CLAUDE_CODE_CLI_AUTOSTART` | `1` | Auto-start the shim on first use when this provider is configured (`0`/`false` to disable). |

The provider profile also honors two Hermes-side env vars:

- `CLAUDE_CODE_CLI_BASE_URL` — override the endpoint Hermes calls (default
  `http://127.0.0.1:8765/v1`). Set this if you change the shim's host/port.
- `CLAUDE_CODE_CLI_API_KEY` — the placeholder key Hermes stores (ignored by the
  shim).

### Per-request overrides

The env vars above are process-wide defaults. A single request may override
`--effort`, `--max-turns`, and (in engine mode) the tool allow/deny lists
without restarting the shim — so main turns and individual auxiliary tasks can
use different settings concurrently. Any field the request omits falls back to
the env default. Hermes surfaces these via a task's `extra_body` (e.g.
`auxiliary.<task>.extra_body`); the model id already overrides `--model`.

| Request field | Maps to | Notes |
|---|---|---|
| `reasoning_effort` (top-level) or `extra_body.reasoning.effort` or `extra_body.effort` | `--effort` | Top-level wins over `extra_body`. |
| `extra_body.max_turns` or top-level `max_turns` | `--max-turns` | Positive integer (int or digit string); `extra_body` wins. |
| `extra_body.allowed_tools` | `--allowedTools` (engine mode) | CSV string, list of names, or OpenAI tool objects (`{"function":{"name":...}}`); de-duplicated. |
| `extra_body.disallowed_tools` | `--disallowedTools` (both modes) | CSV string or list. |

Parsing is defensive: a malformed or empty field is ignored (falls back to the
env default) rather than failing the request.

### Changing the port

Update both sides so they agree:

```bash
CLAUDE_CODE_CLI_PORT=9000 ./start.sh
export CLAUDE_CODE_CLI_BASE_URL=http://127.0.0.1:9000/v1   # before `hermes model`
```

## Endpoints

- `GET  /healthz`               — liveness probe
- `GET  /v1/models`             — advertises `opus` / `sonnet` / `haiku`
- `POST /v1/chat/completions`   — chat completion (supports `stream: true`)

## Development and tests

The default test path uses a fake `claude` executable, so it does **not** require
Claude Code authentication and does not spend real Claude usage.

```bash
scripts/test.sh
```

For local Hermes-profile validation, use an explicit non-default profile and the
fake smoke script:

```bash
# One-time profile setup example. Do not use `hermes profile use` for this.
hermes profile create claudecli --clone --no-alias \
  --description "Claude Code CLI provider smoke profile"
HERMES_HOME="$HOME/.hermes/profiles/claudecli" scripts/install_plugin.sh

# Configure that profile to call the fake-smoke port.
hermes -p claudecli config set model.provider claude-code-cli
hermes -p claudecli config set model.default haiku
hermes -p claudecli config set model.base_url http://127.0.0.1:8799/v1
hermes -p claudecli config set model.api_key '${CLAUDE_CODE_CLI_API_KEY}'
```

Then run:

```bash
scripts/smoke_fake_hermes_profile.sh claudecli
scripts/check_clean_runtime.sh
```

That smoke starts this repo's shim against a temporary fake `claude` binary,
runs `hermes -p claudecli chat ... --provider claude-code-cli`, asserts the
`FAKE_CLAUDE_PROFILE_SMOKE_OK` sentinel, and tears the shim down. It also guards
against accidentally starting the real default shim on port `8765`.

Real Claude Code integration tests should remain opt-in. Run the fake tests and
fake profile smoke first; only run real `claude -p` probes when you explicitly
intend to exercise a logged-in Claude Code installation.

## Uninstall

```bash
rm -rf "${HERMES_HOME:-$HOME/.hermes}/plugins/model-providers/claude-code-cli"
```

Then re-point your default model away from `claude-code-cli` via `hermes model`.

## License

MIT — see [LICENSE](LICENSE).
