#!/usr/bin/env python3
"""OpenAI-compatible shim for the local Claude Code CLI (`claude -p`).

This is the backend for the ``claude-code-cli`` Hermes provider. It exposes a
minimal OpenAI Chat Completions surface and, for each request, shells out to::

    claude -p --output-format json --model <model> [--effort ..] [--tools ..] ...

then returns the CLI's ``result`` field as the assistant message — the same way
the ``fusion-consult`` skill drives Claude Code as an advisory worker. No
Anthropic API key and no network egress are involved; everything runs through
your local, already-authenticated Claude Code login.

Endpoints
---------
* ``GET  /healthz``               liveness probe
* ``GET  /v1/models``             advertises the model ids the CLI accepts
* ``POST /v1/chat/completions``   chat completion (supports ``stream: true``)

Caveat
------
`claude -p` is a *complete agent*: it runs its own internal tool loop and
returns final text. This shim therefore returns plain assistant text, never
OpenAI-style ``tool_calls``. It is best for chat / advisory / synthesis use.
For Hermes' native tool-calling loop, use the bundled ``anthropic`` provider.

Configuration (environment variables)
-------------------------------------
    CLAUDE_CODE_CLI_HOST            bind host           (default 127.0.0.1)
    CLAUDE_CODE_CLI_PORT            bind port           (default 8765)
    CLAUDE_CODE_CLI_BIN            path to claude       (default: autodetect)
    CLAUDE_CODE_CLI_MODEL          fallback model       (default sonnet)
    CLAUDE_CODE_CLI_EFFORT         reasoning effort     (default high; empty=omit)
    CLAUDE_CODE_CLI_TOOLS          --tools value        (default ""  => no tools)
    CLAUDE_CODE_CLI_DISALLOWED_TOOLS  --disallowedTools (default unset => omit)
    CLAUDE_CODE_CLI_MAX_TURNS      --max-turns          (default 12)
    CLAUDE_CODE_CLI_TIMEOUT        per-request seconds  (default 600)
    CLAUDE_CODE_CLI_EXTRA_ARGS     extra argv, shlex    (default unset)
    CLAUDE_CODE_CLI_STREAM         real token streaming (default 1; see #3)
    CLAUDE_CODE_CLI_VISION         pass images to claude(default on;  see #4)
    CLAUDE_CODE_CLI_MAX_IMAGES     images per request   (default 8)
    CLAUDE_CODE_CLI_MAX_IMAGE_MB   per-image size cap MB (default 20)

Per-request overrides (OpenAI request body)
-------------------------------------------
A single request may tune effort / max-turns / the tool allowlist without
restarting the server; each field falls back to the env default above when the
request omits it (see ``extract_overrides``):
    extra_body.reasoning.effort   -> --effort   (or top-level reasoning_effort)
    extra_body.max_turns          -> --max-turns
    extra_body.allowed_tools      -> --allowedTools   (engine mode; CSV or list)
    extra_body.disallowed_tools   -> --disallowedTools
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Model ids the shim advertises and forwards verbatim to `claude --model`.
ADVERTISED_MODELS = ("opus", "sonnet", "haiku")


# --------------------------------------------------------------------------- #
# Configuration helpers
# --------------------------------------------------------------------------- #
def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name)
    return default if val is None else val


def build_subprocess_env() -> dict[str, str]:
    """Return an env with user-local CLI paths restored.

    Mirrors fusion_runner.build_subprocess_env: cron/gateway-launched parents
    can have a minimal PATH that lacks ~/.local/npm/bin and nvm's node, which
    breaks `claude`'s `/usr/bin/env node` shebang even when the binary resolves.
    """
    env = os.environ.copy()
    home = pathlib.Path.home()
    candidates: list[pathlib.Path] = [home / ".local/npm/bin", home / ".local/bin"]

    nvm_versions = home / ".nvm/versions/node"
    if nvm_versions.exists():
        try:
            node_bins = [p for p in nvm_versions.glob("*/bin") if (p / "node").exists()]
            node_bins.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            candidates.extend(node_bins)
        except OSError:
            pass

    candidates.extend(
        pathlib.Path(p)
        for p in ("/usr/local/bin", "/usr/bin", "/bin", "/usr/local/sbin", "/usr/sbin", "/sbin")
    )

    parts: list[str] = []
    for p in candidates:
        s = str(p)
        if p.exists() and s not in parts:
            parts.append(s)
    for s in (env.get("PATH") or "").split(os.pathsep):
        if s and s not in parts:
            parts.append(s)
    env["PATH"] = os.pathsep.join(parts)
    return env


def resolve_claude_bin() -> str:
    """Resolve the claude binary: explicit env > PATH > common locations."""
    explicit = _env("CLAUDE_CODE_CLI_BIN").strip()
    if explicit:
        return explicit
    found = shutil.which("claude", path=build_subprocess_env().get("PATH"))
    if found:
        return found
    for cand in ("/usr/bin/claude", "/usr/local/bin/claude",
                 str(pathlib.Path.home() / ".local/npm/bin/claude")):
        if pathlib.Path(cand).exists():
            return cand
    return "claude"  # last resort; will surface a clear "not found" error


# --------------------------------------------------------------------------- #
# Prompt construction + CLI invocation
# --------------------------------------------------------------------------- #
# Vision passthrough (#4): claude -p is blind to inline OpenAI image parts, so
# we materialize base64 `data:` images to temp files and reference their paths
# in the prompt (Claude Code's Read tool can open image files in engine mode).
# Remote URLs are passed through as text references — the shim intentionally
# performs no network egress, so it never downloads them itself.
_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[\w.+-]+/[\w.+-]+)?(?P<b64>;base64)?,(?P<data>.*)$", re.DOTALL
)
_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/svg+xml": ".svg",
    "image/heic": ".heic",
    "image/heif": ".heif",
}


def _vision_enabled() -> bool:
    return _env("CLAUDE_CODE_CLI_VISION", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _max_images() -> int:
    try:
        return max(0, int(_env("CLAUDE_CODE_CLI_MAX_IMAGES", "8")))
    except ValueError:
        return 8


def _max_image_bytes() -> int:
    try:
        mb = float(_env("CLAUDE_CODE_CLI_MAX_IMAGE_MB", "20"))
    except ValueError:
        mb = 20.0
    return int(max(0.0, mb) * 1024 * 1024)


def _image_url_from_part(part: dict) -> str | None:
    """Extract the URL string from an OpenAI ``image_url`` content part.

    Handles both the object form ``{"image_url": {"url": "..."}}`` and the
    shorthand ``{"image_url": "..."}``.
    """
    iu = part.get("image_url")
    if isinstance(iu, dict):
        url = iu.get("url")
        return url if isinstance(url, str) and url.strip() else None
    if isinstance(iu, str) and iu.strip():
        return iu
    return None


class _ImageCollector:
    """Materialize image parts to temp files, bounded by count and size.

    ``add(url)`` returns the prompt text that should stand in for the image and,
    for decodable ``data:`` URLs, writes a temp file whose path is referenced in
    that text. Call :meth:`cleanup` after the CLI invocation returns to delete
    the temp files. When vision is disabled it degrades to ``[image omitted]``,
    preserving the pre-#4 behavior.
    """

    def __init__(self) -> None:
        self.enabled = _vision_enabled()
        self.max_images = _max_images()
        self.max_bytes = _max_image_bytes()
        self.count = 0
        self.paths: list[str] = []
        self._dir: str | None = None

    def _tmpdir(self) -> str:
        if self._dir is None:
            self._dir = tempfile.mkdtemp(prefix="claude-code-cli-img-")
        return self._dir

    def add(self, url: str) -> str:
        if not self.enabled:
            return "[image omitted]"
        if self.count >= self.max_images:
            return f"[image omitted: over the {self.max_images}-image limit]"
        url = url.strip()
        m = _DATA_URL_RE.match(url)
        if m and m.group("b64"):
            try:
                raw = base64.b64decode(m.group("data"), validate=False)
            except Exception:
                return "[image omitted: undecodable data URL]"
            if not raw:
                return "[image omitted: empty image]"
            if self.max_bytes and len(raw) > self.max_bytes:
                return f"[image omitted: exceeds {self.max_bytes // (1024 * 1024)}MB cap]"
            mime = (m.group("mime") or "image/png").lower()
            ext = _MIME_EXT.get(mime, ".img")
            path = os.path.join(self._tmpdir(), f"image_{self.count:02d}{ext}")
            try:
                with open(path, "wb") as fh:
                    fh.write(raw)
            except OSError as exc:
                return f"[image omitted: {exc}]"
            self.count += 1
            self.paths.append(path)
            return f"[image saved to {path} — read this file to view the image]"
        # Non-data reference: pass it through as text so an engine-mode agent can
        # fetch it (WebFetch) if it chooses. The shim itself does not download.
        if url.startswith(("http://", "https://", "file://", "/")):
            self.count += 1
            return f"[image at {url} — fetch this URL/path to view the image]"
        return "[image omitted: unsupported image reference]"

    def cleanup(self) -> None:
        for p in self.paths:
            try:
                os.remove(p)
            except OSError:
                pass
        if self._dir:
            try:
                os.rmdir(self._dir)
            except OSError:
                pass
        self.paths = []
        self._dir = None


def _content_to_text(content, images: _ImageCollector | None = None) -> str:
    """Flatten OpenAI message content (str or list-of-parts) to plain text.

    When ``images`` is provided, ``image_url`` parts are materialized/referenced
    through the collector (#4); otherwise they degrade to ``[image omitted]`` so
    text-only callers keep their original behavior.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    chunks.append(part["text"])
                elif part.get("type") == "image_url" or "image_url" in part:
                    url = _image_url_from_part(part) if images is not None else None
                    if images is not None and url:
                        chunks.append(images.add(url))
                    else:
                        chunks.append("[image omitted]")
            elif isinstance(part, str):
                chunks.append(part)
        return "\n".join(chunks)
    if content is None:
        return ""
    return str(content)


def flatten_messages(messages: list[dict], images: _ImageCollector | None = None) -> str:
    """Render an OpenAI message list into a single prompt for `claude -p`.

    System/developer messages become a leading SYSTEM block; the conversation
    is rendered as labelled turns. Tool messages are folded in as context so
    nothing in the history is silently dropped. When an ``images`` collector is
    passed, inline images are materialized/referenced instead of dropped (#4).
    """
    system_blocks: list[str] = []
    convo: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = (msg.get("role") or "user").lower()
        text = _content_to_text(msg.get("content"), images)
        if role in ("system", "developer"):
            if text.strip():
                system_blocks.append(text)
        elif role == "assistant":
            # Surface any tool calls the assistant previously made as context.
            tool_calls = msg.get("tool_calls") or []
            call_desc = ""
            if tool_calls:
                names = [
                    (tc.get("function") or {}).get("name", "tool")
                    for tc in tool_calls if isinstance(tc, dict)
                ]
                call_desc = f" [requested tools: {', '.join(n for n in names if n)}]"
            convo.append(f"ASSISTANT: {text}{call_desc}".rstrip())
        elif role == "tool":
            convo.append(f"TOOL RESULT: {text}")
        else:
            convo.append(f"USER: {text}")

    parts: list[str] = []
    if system_blocks:
        parts.append("SYSTEM INSTRUCTIONS:\n" + "\n\n".join(system_blocks))
    if convo:
        parts.append("CONVERSATION:\n" + "\n\n".join(convo))
    parts.append(
        "Respond as the assistant to the latest user turn. "
        "Return only the response text."
    )
    return "\n\n".join(parts).strip()


# Tools pre-approved in engine mode so `claude -p` can run them without an
# interactive permission prompt (it can't prompt in print mode). Override with
# CLAUDE_CODE_CLI_ENGINE_TOOLS.
DEFAULT_ENGINE_TOOLS = "Read,Write,Edit,Bash,Glob,Grep,WebFetch,WebSearch,TodoWrite"


def _coerce_tool_list(value) -> str:
    """Coerce an allow/deny tools value into claude's CSV form.

    Accepts a CSV string (returned trimmed) or a list. List items may be plain
    tool-name strings or OpenAI ``{"type":"function","function":{"name":...}}``
    tool objects; the function name is extracted from the latter. Returns ``""``
    when nothing usable is present.
    """
    if isinstance(value, str):
        return ",".join(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            if isinstance(item, str):
                name = item.strip()
            elif isinstance(item, dict):
                fn = item.get("function")
                if isinstance(fn, dict):
                    name = str(fn.get("name") or "").strip()
                else:
                    name = str(item.get("name") or "").strip()
            else:
                name = ""
            if name and name not in names:
                names.append(name)
        return ",".join(names)
    return ""


def extract_overrides(body: object) -> dict:
    """Pull per-request CLI overrides out of an OpenAI request body.

    Only keys that are actually present (and non-empty) are returned, so callers
    can treat a missing key as "fall back to the env default". This is parsed
    defensively — a malformed field is ignored rather than raising, keeping a
    single bad request from breaking the shim. Recognised inputs:

    * ``reasoning_effort`` (top-level) or ``extra_body.reasoning.effort``
      or ``extra_body.effort``                       -> ``effort``
    * ``extra_body.max_turns`` / top-level ``max_turns`` (int or str) -> ``max_turns``
    * ``extra_body.allowed_tools`` (CSV/list/tool-objects)  -> ``allowed_tools``
    * ``extra_body.disallowed_tools`` (CSV/list)            -> ``disallowed_tools``
    """
    if not isinstance(body, dict):
        return {}
    extra = body.get("extra_body")
    if not isinstance(extra, dict):
        extra = {}

    out: dict = {}

    # --- effort: top-level reasoning_effort, then extra_body.reasoning.effort ---
    effort = body.get("reasoning_effort")
    if not (isinstance(effort, str) and effort.strip()):
        reasoning = extra.get("reasoning")
        if isinstance(reasoning, dict):
            effort = reasoning.get("effort")
        else:
            effort = extra.get("reasoning_effort") or extra.get("effort")
    if isinstance(effort, str) and effort.strip():
        out["effort"] = effort.strip()

    # --- max_turns: extra_body first, then top-level; accept int or str ---
    max_turns = extra.get("max_turns")
    if max_turns is None:
        max_turns = body.get("max_turns")
    if isinstance(max_turns, bool):
        max_turns = None  # guard: bool is an int subclass
    if isinstance(max_turns, int) and max_turns > 0:
        out["max_turns"] = str(max_turns)
    elif isinstance(max_turns, str) and max_turns.strip().isdigit() and int(max_turns.strip()) > 0:
        out["max_turns"] = max_turns.strip()

    # --- tool allow / deny lists (engine mode) ---
    allowed = _coerce_tool_list(extra.get("allowed_tools"))
    if allowed:
        out["allowed_tools"] = allowed
    disallowed = _coerce_tool_list(extra.get("disallowed_tools"))
    if disallowed:
        out["disallowed_tools"] = disallowed

    return out


def build_claude_argv(model: str, engine: bool, overrides: dict | None = None,
                      stream: bool = False) -> list[str]:
    """Assemble the `claude -p` argv from config. Prompt is piped via stdin.

    engine=True  → Claude Code uses its OWN tools to actually do the work
                   (read/edit files, run bash) — "use Claude Code as an engine".
    engine=False → no tools, single turn — a plain text model (aux tasks).

    ``overrides`` (from :func:`extract_overrides`) lets a single request tune
    effort / max-turns / tool lists; any key it omits falls back to the env
    default, so behavior is unchanged when the request carries no overrides.

    ``stream=True`` (issue #3) switches the output format to ``stream-json`` so
    the CLI emits incremental JSONL events instead of one final JSON blob;
    ``--verbose`` is required by the CLI in that mode, and partial (token-level)
    messages are requested unless ``CLAUDE_CODE_CLI_PARTIAL_MESSAGES`` disables
    them (an escape hatch for CLI versions that lack the flag).
    """
    overrides = overrides or {}
    argv = [
        resolve_claude_bin(),
        "-p",
        "--model", model,
    ]
    if stream:
        argv += ["--output-format", "stream-json", "--verbose"]
        if _env("CLAUDE_CODE_CLI_PARTIAL_MESSAGES", "1").strip().lower() not in {
            "0", "false", "no", "off",
        }:
            argv += ["--include-partial-messages"]
    else:
        argv += ["--output-format", "json"]
    argv += ["--no-session-persistence"]

    effort = str(overrides.get("effort") or _env("CLAUDE_CODE_CLI_EFFORT", "high")).strip()
    if effort:
        argv += ["--effort", effort]

    if engine:
        # Let the CLI's own agentic tool loop run. Pre-approve a capable tool set
        # so it executes autonomously instead of blocking on permission prompts.
        tools = str(
            overrides.get("allowed_tools")
            or _env("CLAUDE_CODE_CLI_ENGINE_TOOLS", DEFAULT_ENGINE_TOOLS)
        ).strip()
        if tools:
            argv += ["--allowedTools", tools]
        if _env("CLAUDE_CODE_CLI_ENGINE_PERMISSION", "").strip().lower() in {
            "bypass", "skip", "dangerous", "yolo",
        }:
            argv += ["--dangerously-skip-permissions"]
        disallowed = str(
            overrides.get("disallowed_tools")
            or _env("CLAUDE_CODE_CLI_DISALLOWED_TOOLS", "")
        ).strip()
        if disallowed:
            argv += ["--disallowedTools", disallowed]
        add_dir = _env("CLAUDE_CODE_CLI_ADD_DIR", "").strip()
        if add_dir:
            argv += ["--add-dir", *add_dir.split(os.pathsep)]
        max_turns = str(
            overrides.get("max_turns")
            or _env("CLAUDE_CODE_CLI_ENGINE_MAX_TURNS", "40")
        ).strip()
        if max_turns:
            argv += ["--max-turns", max_turns]
    else:
        # Disable the CLI's own tool loop so it behaves as a pure text model
        # (Hermes auxiliary tasks: title-gen, compression, etc.).
        argv += ["--tools", _env("CLAUDE_CODE_CLI_TOOLS", "")]
        disallowed = str(
            overrides.get("disallowed_tools")
            or _env("CLAUDE_CODE_CLI_DISALLOWED_TOOLS", "")
        ).strip()
        if disallowed:
            argv += ["--disallowedTools", disallowed]
        max_turns = str(
            overrides.get("max_turns")
            or _env("CLAUDE_CODE_CLI_MAX_TURNS", "12")
        ).strip()
        if max_turns:
            argv += ["--max-turns", max_turns]

    extra = _env("CLAUDE_CODE_CLI_EXTRA_ARGS", "").strip()
    if extra:
        try:
            argv += shlex.split(extra)
        except ValueError:
            pass
    return argv


def _engine_cwd() -> str:
    """Working directory Claude Code operates in during engine mode."""
    raw = _env("CLAUDE_CODE_CLI_CWD", "").strip()
    cwd = pathlib.Path(raw).expanduser() if raw else pathlib.Path.home()
    return str(cwd) if cwd.is_dir() else str(pathlib.Path.home())


def _extract_json_object(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def run_claude(prompt: str, model: str, engine: bool = False, overrides: dict | None = None) -> dict:
    """Invoke `claude -p` and return {text, usage, error}.

    `usage` is {prompt_tokens, completion_tokens, total_tokens}. On any failure
    `error` is a human-readable string and `text` carries the same message so
    streaming clients still see something. In engine mode the CLI runs its own
    tools in CLAUDE_CODE_CLI_CWD and may take much longer.
    """
    argv = build_claude_argv(model, engine, overrides)
    default_timeout = "1200" if engine else "600"
    try:
        timeout = int(_env("CLAUDE_CODE_CLI_TIMEOUT", default_timeout))
    except ValueError:
        timeout = int(default_timeout)
    cwd = _engine_cwd() if engine else None

    try:
        proc = subprocess.run(
            argv,
            input=prompt,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=build_subprocess_env(),
            cwd=cwd,
            check=False,
        )
    except FileNotFoundError:
        msg = f"claude binary not found (tried {argv[0]!r}); set CLAUDE_CODE_CLI_BIN"
        return {"text": f"[claude-code-cli error] {msg}", "usage": _zero_usage(), "error": msg}
    except subprocess.TimeoutExpired:
        msg = f"claude timed out after {timeout}s"
        return {"text": f"[claude-code-cli error] {msg}", "usage": _zero_usage(), "error": msg}
    except Exception as exc:  # defensive: never crash the request thread
        msg = f"claude invocation failed: {exc!r}"
        return {"text": f"[claude-code-cli error] {msg}", "usage": _zero_usage(), "error": msg}

    stdout, stderr = proc.stdout or "", proc.stderr or ""
    parsed = _extract_json_object(stdout)

    if parsed is not None:
        if parsed.get("is_error"):
            msg = str(parsed.get("result") or parsed.get("error") or "claude reported is_error")
            return {"text": f"[claude-code-cli error] {msg}", "usage": _map_usage(parsed.get("usage")), "error": msg}
        result = parsed.get("result")
        if not isinstance(result, str):
            result = parsed.get("content")
        if isinstance(result, str) and result.strip():
            return {"text": result.strip(), "usage": _map_usage(parsed.get("usage")), "error": ""}

    # Non-zero exit with no parseable result → surface stderr/stdout tail.
    if proc.returncode != 0:
        tail = (stderr.strip() or stdout.strip())[-1000:]
        msg = f"claude exited {proc.returncode}: {tail}"
        return {"text": f"[claude-code-cli error] {msg}", "usage": _zero_usage(), "error": msg}

    # Exit 0 but unparseable → degrade to raw stdout as the completion text.
    fallback = stdout.strip()
    if fallback:
        return {"text": fallback, "usage": _zero_usage(), "error": ""}
    msg = "claude returned no output"
    return {"text": f"[claude-code-cli error] {msg}", "usage": _zero_usage(), "error": msg}


def _zero_usage() -> dict:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _map_usage(usage) -> dict:
    if not isinstance(usage, dict):
        return _zero_usage()
    prompt = (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
    )
    completion = int(usage.get("output_tokens", 0) or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


# --------------------------------------------------------------------------- #
# Real token streaming (#3): `claude -p --output-format stream-json`
# --------------------------------------------------------------------------- #
def _real_streaming_enabled() -> bool:
    """Whether ``stream:true`` requests use incremental stream-json (#3).

    Default on ("auto"); set CLAUDE_CODE_CLI_STREAM=off to force the legacy
    run-to-completion-then-one-chunk behavior.
    """
    return _env("CLAUDE_CODE_CLI_STREAM", "auto").strip().lower() not in {
        "0", "false", "no", "off", "buffered", "sync",
    }


def iter_stream_json(lines):
    """Yield JSON object events from a Claude Code stream-json line source.

    The CLI writes one JSON object per line, but stderr/progress wrappers or
    future CLI versions may add blank or non-JSON lines. Invalid lines are
    ignored instead of failing the request, matching the shim's defensive JSON
    handling elsewhere.
    """
    for raw in lines:
        line = str(raw).strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _extract_stream_delta(event: dict) -> str:
    """Pull an assistant *text* delta out of a stream-json event.

    Defensive against schema drift: accepts Claude Code's wrapped
    ``{"type":"stream_event","event":...}`` records as well as direct
    Anthropic-style ``content_block_delta`` records. Only text deltas are
    forwarded; tool-argument ``input_json_delta`` events and everything else
    yield ``""``.
    """
    inner = event.get("event") if event.get("type") == "stream_event" else event
    if not isinstance(inner, dict):
        return ""
    itype = inner.get("type")
    if itype == "content_block_delta":
        delta = inner.get("delta")
        if isinstance(delta, dict):
            dtype = delta.get("type")
            text = delta.get("text")
            if isinstance(text, str) and dtype in ("text_delta", None):
                return text
    elif itype == "content_block_start":
        block = inner.get("content_block")
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                return text
    return ""


def _extract_assistant_message_text(event: dict) -> str:
    """Concatenate text blocks from a complete ``assistant`` message event."""
    msg = event.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _extract_result_event(event: dict):
    """Return ``(text, usage, error)`` for a terminal event, else ``None``.

    Recognizes the stream-json ``{"type":"result",...}`` event *and*, defensively,
    a bare ``{"result": ..., "usage": ...}`` blob — some CLI versions ignore
    ``--output-format stream-json`` and emit a single JSON object, and we still
    want a usable final result + usage in that case.
    """
    is_result_type = event.get("type") == "result"
    bare_result = event.get("type") is None and isinstance(event.get("result"), str)
    if not (is_result_type or bare_result):
        return None
    if event.get("is_error"):
        msg = str(event.get("result") or event.get("error") or "claude reported is_error")
        return ("", _map_usage(event.get("usage")), msg)
    text = event.get("result")
    text = text if isinstance(text, str) else str(event.get("content") or "")
    return (text, _map_usage(event.get("usage")), "")


def _kill_proc(proc) -> None:
    if proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass


def stream_claude(prompt: str, model: str, engine: bool = False,
                  overrides: dict | None = None):
    """Generator yielding ``("delta", text)`` as tokens arrive, then exactly one
    ``("final", {"text","usage","error"})``.

    Invokes ``claude -p --output-format stream-json`` and forwards assistant-text
    deltas the moment the CLI emits them, so time-to-first-token is the first
    token rather than the whole generation (issue #3). It never raises: spawn
    failures, timeouts, and unparseable output are surfaced through the terminal
    ``final`` outcome, mirroring :func:`run_claude`. The prompt is fed on a
    thread and stderr drained on a thread to avoid pipe deadlocks.
    """
    argv = build_claude_argv(model, engine, overrides, stream=True)
    default_timeout = "1200" if engine else "600"
    try:
        timeout = int(_env("CLAUDE_CODE_CLI_TIMEOUT", default_timeout))
    except ValueError:
        timeout = int(default_timeout)
    cwd = _engine_cwd() if engine else None

    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=build_subprocess_env(),
            cwd=cwd,
            bufsize=1,
        )
    except FileNotFoundError:
        msg = f"claude binary not found (tried {argv[0]!r}); set CLAUDE_CODE_CLI_BIN"
        yield ("final", {"text": f"[claude-code-cli error] {msg}", "usage": _zero_usage(), "error": msg})
        return
    except Exception as exc:  # defensive: never crash the request thread
        msg = f"claude invocation failed: {exc!r}"
        yield ("final", {"text": f"[claude-code-cli error] {msg}", "usage": _zero_usage(), "error": msg})
        return

    def _feed():
        try:
            if proc.stdin:
                proc.stdin.write(prompt)
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    stderr_chunks: list[str] = []

    def _drain_stderr():
        try:
            if proc.stderr:
                for line in proc.stderr:
                    stderr_chunks.append(line)
        except Exception:
            pass

    timed_out = {"flag": False}

    def _watchdog():
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.2)
        if proc.poll() is None:
            timed_out["flag"] = True
            _kill_proc(proc)

    feeder = threading.Thread(target=_feed, daemon=True)
    err_thread = threading.Thread(target=_drain_stderr, daemon=True)
    watch = threading.Thread(target=_watchdog, daemon=True)
    feeder.start()
    err_thread.start()
    watch.start()

    emitted_any = False
    emitted_parts: list[str] = []
    assistant_text = ""
    result_text = ""
    result_usage = _zero_usage()
    result_error = ""
    saw_result = False

    try:
        for event in iter_stream_json(proc.stdout or ()):  # watchdog kills on timeout
            etype = event.get("type")
            if etype in {"stream_event", "content_block_delta", "content_block_start"}:
                delta = _extract_stream_delta(event)
                if delta:
                    emitted_any = True
                    emitted_parts.append(delta)
                    yield ("delta", delta)
            elif etype == "assistant":
                assistant_text = _extract_assistant_message_text(event)
                if assistant_text and not emitted_any:
                    # No partial deltas (older CLI / flag off): emit the whole
                    # assistant message once so the client still streams.
                    emitted_any = True
                    emitted_parts.append(assistant_text)
                    yield ("delta", assistant_text)
            else:
                parsed = _extract_result_event(event)
                if parsed is not None:
                    result_text, result_usage, result_error = parsed
                    saw_result = True
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            _kill_proc(proc)
        feeder.join(timeout=1)
        err_thread.join(timeout=1)
        watch.join(timeout=1)
        for pipe in (proc.stdout, proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass

    emitted_text = "".join(emitted_parts)
    stderr_tail = "".join(stderr_chunks).strip()[-1000:]

    if timed_out["flag"]:
        msg = f"claude timed out after {timeout}s"
        text = emitted_text or f"[claude-code-cli error] {msg}"
        yield ("final", {"text": text, "usage": result_usage, "error": msg})
        return

    if result_error:
        if not emitted_any:
            err_line = f"[claude-code-cli error] {result_error}"
            emitted_text = err_line
            yield ("delta", err_line)
        yield ("final", {"text": emitted_text, "usage": result_usage, "error": result_error})
        return

    final_text = result_text or emitted_text or assistant_text
    if not emitted_any and final_text:
        # Never streamed (e.g. a bare-json CLI) but we have text → emit once.
        yield ("delta", final_text)

    if not final_text and not saw_result:
        rc = proc.returncode
        if rc not in (0, None):
            msg = f"claude exited {rc}: {stderr_tail}" if stderr_tail else f"claude exited {rc}"
        else:
            msg = "claude returned no output"
        err_line = f"[claude-code-cli error] {msg}"
        yield ("delta", err_line)
        yield ("final", {"text": err_line, "usage": _zero_usage(), "error": msg})
        return

    yield ("final", {"text": final_text, "usage": result_usage, "error": ""})


# --------------------------------------------------------------------------- #
# OpenAI response shaping
# --------------------------------------------------------------------------- #
def _completion_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def build_completion(model: str, text: str, usage: dict) -> dict:
    return {
        "id": _completion_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": usage,
    }


def build_stream_chunks(model: str, text: str, usage: dict, include_usage: bool):
    cid, created = _completion_id(), int(time.time())

    def chunk(delta, finish=None, usage_obj=None):
        payload = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if usage_obj is not None:
            payload["usage"] = usage_obj
        return payload

    yield chunk({"role": "assistant"})
    if text:
        yield chunk({"content": text})
    yield chunk({}, finish="stop")
    if include_usage:
        # OpenAI emits a trailing usage-only chunk with an empty choices list.
        yield {
            "id": cid, "object": "chat.completion.chunk", "created": created,
            "model": model, "choices": [], "usage": usage,
        }


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "claude-code-cli-shim/1.0"

    # ---- low-level write helpers ----
    def _send_json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str, etype: str = "invalid_request_error") -> None:
        self._send_json(status, {"error": {"message": message, "type": etype}})

    def log_message(self, fmt, *args):  # quieter, single-line logs to stderr
        sys.stderr.write("[claude-code-cli] " + (fmt % args) + "\n")

    # ---- routing ----
    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("/healthz", "/health", "/v1/healthz"):
            self._send_json(200, {"status": "ok", "bin": resolve_claude_bin()})
        elif path == "/v1/models":
            now = int(time.time())
            self._send_json(200, {
                "object": "list",
                "data": [
                    {"id": m, "object": "model", "created": now, "owned_by": "claude-code-cli"}
                    for m in ADVERTISED_MODELS
                ],
            })
        else:
            self._send_error(404, f"unknown path: {self.path}", "not_found")

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path != "/v1/chat/completions":
            self._send_error(404, f"unknown path: {self.path}", "not_found")
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_error(400, f"invalid JSON body: {exc}")
            return

        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            self._send_error(400, "'messages' must be a non-empty array")
            return

        model = str(body.get("model") or _env("CLAUDE_CODE_CLI_MODEL", "sonnet")).strip() or "sonnet"
        stream = bool(body.get("stream"))
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

        # Engine mode: let Claude Code use its own tools to do real work.
        # "auto" (default) turns it on when the request carries tool definitions
        # (an agentic Hermes turn); aux completions (no tools) stay text-only.
        mode = _env("CLAUDE_CODE_CLI_ENGINE", "auto").strip().lower()
        if mode in {"always", "on", "1", "true", "yes"}:
            engine = True
        elif mode in {"never", "off", "0", "false", "no"}:
            engine = False
        else:
            _tools = body.get("tools")
            engine = isinstance(_tools, list) and len(_tools) > 0

        images = _ImageCollector()
        try:
            prompt = flatten_messages(messages, images)
            if engine:
                prompt += (
                    "\n\nYou are operating as an autonomous coding engine with your own tools "
                    "(Read, Write, Edit, Bash, Glob, Grep, etc.) in your working directory. Use "
                    "them to actually carry out the task — read/modify files, run commands — then "
                    "report what you did. Do not ask for permission; act."
                )
            overrides = extract_overrides(body)
            started = time.time()

            if stream and _real_streaming_enabled():
                outcome = self._stream_live_response(model, prompt, engine, overrides, include_usage)
            else:
                outcome = run_claude(prompt, model, engine, overrides)
                if stream:
                    self._stream_response(model, outcome, include_usage)
                else:
                    self._send_json(200, build_completion(model, outcome["text"], outcome["usage"]))

            self.log_message(
                "model=%s engine=%s stream=%s live_stream=%s images=%s effort=%s %.1fs %s",
                model, engine, stream, bool(stream and _real_streaming_enabled()), images.count,
                overrides.get("effort") or _env("CLAUDE_CODE_CLI_EFFORT", "high"),
                time.time() - started, "error" if outcome["error"] else "ok",
            )
        finally:
            images.cleanup()

    def _write_sse(self, payload: dict | str) -> None:
        if isinstance(payload, str):
            data = payload.encode("utf-8")
        else:
            data = json.dumps(payload).encode("utf-8")
        self.wfile.write(b"data: " + data + b"\n\n")
        self.wfile.flush()

    def _stream_response(self, model: str, outcome: dict, include_usage: bool) -> None:
        """Legacy buffered stream response: one content chunk after completion."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            for chunk in build_stream_chunks(model, outcome["text"], outcome["usage"], include_usage):
                self._write_sse(chunk)
            self._write_sse("[DONE]")
        except (BrokenPipeError, ConnectionResetError):
            pass  # client hung up mid-stream

    def _stream_live_response(self, model: str, prompt: str, engine: bool,
                              overrides: dict, include_usage: bool) -> dict:
        """SSE bridge for real `stream-json` deltas (#3)."""
        cid, created = _completion_id(), int(time.time())

        def chunk(delta, finish=None, usage_obj=None):
            payload = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            if usage_obj is not None:
                payload["usage"] = usage_obj
            return payload

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        outcome = {"text": "", "usage": _zero_usage(), "error": "stream ended before final event"}
        try:
            self._write_sse(chunk({"role": "assistant"}))
            for kind, payload in stream_claude(prompt, model, engine, overrides):
                if kind == "delta":
                    self._write_sse(chunk({"content": payload}))
                elif kind == "final" and isinstance(payload, dict):
                    outcome = payload
                    break
            self._write_sse(chunk({}, finish="stop"))
            if include_usage:
                self._write_sse({
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [],
                    "usage": outcome["usage"],
                })
            self._write_sse("[DONE]")
        except (BrokenPipeError, ConnectionResetError):
            pass  # client hung up mid-stream
        return outcome


def main() -> int:
    host = _env("CLAUDE_CODE_CLI_HOST", "127.0.0.1")
    try:
        port = int(_env("CLAUDE_CODE_CLI_PORT", "8765"))
    except ValueError:
        port = 8765

    bin_path = resolve_claude_bin()
    if shutil.which(bin_path) is None and not pathlib.Path(bin_path).exists():
        sys.stderr.write(
            f"[claude-code-cli] WARNING: claude binary {bin_path!r} not found on PATH; "
            "requests will fail until CLAUDE_CODE_CLI_BIN points at a valid `claude`.\n"
        )

    httpd = ThreadingHTTPServer((host, port), Handler)
    sys.stderr.write(
        f"[claude-code-cli] serving OpenAI-compatible Claude Code CLI on "
        f"http://{host}:{port}/v1  (claude={bin_path})\n"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[claude-code-cli] shutting down\n")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
