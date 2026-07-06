#!/usr/bin/env python3
"""End-to-end smoke for Hermes-native tool calls through this provider.

The smoke is self-contained:
1. Create a temporary HERMES_HOME.
2. Symlink this working tree into plugins/model-providers/claude-code-cli.
3. Start this working tree's shim against a fake claude executable.
4. Run a real Hermes CLI turn with the todo tool enabled.
5. Assert Hermes receives a tool_calls response, executes todo, sends TOOL RESULT
   back to the shim, and receives a final sentinel.

It does not require Claude Code authentication and does not touch the user's real
Hermes home. Set HERMES_BIN=/path/to/hermes if hermes is not on PATH.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
PYTHON = sys.executable
SENTINEL = "HERMES_NATIVE_TOOL_LOOP_OK"


def find_hermes() -> pathlib.Path | None:
    explicit = os.environ.get("HERMES_BIN", "").strip()
    if explicit:
        path = pathlib.Path(explicit).expanduser()
        return path if path.exists() else None
    found = shutil.which("hermes")
    if found:
        return pathlib.Path(found)
    candidates = [
        pathlib.Path.home() / ".hermes/hermes-agent/venv/bin/hermes",
        pathlib.Path.home() / ".local/bin/hermes",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_health(port: int) -> None:
    url = f"http://127.0.0.1:{port}/healthz"
    deadline = time.time() + 15
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # pragma: no cover - diagnostic path
            last = exc
            time.sleep(0.1)
    raise RuntimeError(f"shim did not become ready at {url}: {last}")


def write_fake_claude(path: pathlib.Path, fake_log: pathlib.Path) -> None:
    path.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json
        import sys

        prompt = sys.stdin.read()
        with open({str(fake_log)!r}, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({{"argv": sys.argv[1:], "prompt": prompt}}, ensure_ascii=False) + "\\n")
        usage = {{"input_tokens": 7, "output_tokens": 3}}
        if "TOOL RESULT:" in prompt:
            print(json.dumps({{"result": {SENTINEL!r}, "usage": usage}}))
        elif "Available Hermes-native tools" in prompt and "\\\"name\\\": \\\"todo\\\"" in prompt:
            tool_request = {{
                "tool_calls": [{{
                    "name": "todo",
                    "arguments": {{
                        "todos": [{{
                            "id": "smoke",
                            "content": "native tool smoke",
                            "status": "completed",
                        }}]
                    }},
                }}]
            }}
            print(json.dumps({{"result": json.dumps(tool_request), "usage": usage}}))
        else:
            print(json.dumps({{"result": "NO_NATIVE_TOOL_PROMPT", "usage": usage}}))
    """))
    path.chmod(0o755)


def write_isolated_hermes_home(home: pathlib.Path, plugin_target: pathlib.Path, fake: pathlib.Path, port: int) -> None:
    plugin_dir = home / "plugins" / "model-providers"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "claude-code-cli").symlink_to(plugin_target, target_is_directory=True)
    (home / "skills").mkdir(parents=True)
    (home / "memories").mkdir(parents=True)
    (home / "logs").mkdir(parents=True)
    (home / ".env").write_text(textwrap.dedent(f"""\
        CLAUDE_CODE_CLI_API_KEY=local
        CLAUDE_CODE_CLI_BASE_URL=http://127.0.0.1:{port}/v1
        CLAUDE_CODE_CLI_AUTOSTART=0
        CLAUDE_CODE_CLI_BIN={fake}
        CLAUDE_CODE_CLI_NATIVE_TOOLS=1
        CLAUDE_CODE_CLI_ENGINE=auto
        CLAUDE_CODE_CLI_STREAM=1
    """))
    (home / "config.yaml").write_text(textwrap.dedent("""\
        model:
          provider: claude-code-cli
          default: haiku
          context_length: 200000
        display:
          streaming: false
        platform_toolsets:
          cli:
            - todo
    """))


def run_smoke(hermes: pathlib.Path, tmp: pathlib.Path) -> int:
    port = free_port()
    fake = tmp / "claude"
    fake_log = tmp / "fake-prompts.jsonl"
    server_log = tmp / "shim.log"
    home = tmp / "hermes_home"

    write_fake_claude(fake, fake_log)
    write_isolated_hermes_home(home, ROOT, fake, port)

    server_env = os.environ.copy()
    server_env.update({
        "CLAUDE_CODE_CLI_BIN": str(fake),
        "CLAUDE_CODE_CLI_PORT": str(port),
        "CLAUDE_CODE_CLI_AUTOSTART": "0",
        "CLAUDE_CODE_CLI_ENGINE": "auto",
        "CLAUDE_CODE_CLI_NATIVE_TOOLS": "1",
        "CLAUDE_CODE_CLI_STREAM": "1",
    })
    with server_log.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [PYTHON, str(ROOT / "claude_code_server.py")],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=server_env,
        )

    try:
        wait_health(port)
        env = os.environ.copy()
        env.update({
            "PATH": os.environ.get("PATH", "") + os.pathsep + str(hermes.parent),
            "HERMES_HOME": str(home),
            "CLAUDE_CODE_CLI_API_KEY": "local",
            "CLAUDE_CODE_CLI_BASE_URL": f"http://127.0.0.1:{port}/v1",
            "CLAUDE_CODE_CLI_AUTOSTART": "0",
            "CLAUDE_CODE_CLI_BIN": str(fake),
            "CLAUDE_CODE_CLI_NATIVE_TOOLS": "1",
            "CLAUDE_CODE_CLI_ENGINE": "auto",
            "HERMES_DISABLE_VERSION_CHECK": "1",
            "HERMES_BACKGROUND_NOTIFICATIONS": "0",
            "PYTHONUNBUFFERED": "1",
        })
        cmd = [
            str(hermes),
            "chat",
            "-Q",
            "--provider",
            "claude-code-cli",
            "-m",
            "haiku",
            "-t",
            "todo",
            "-q",
            f"Use the todo tool to record a completed native tool smoke, then answer with {SENTINEL}.",
        ]
        run = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=int(os.environ.get("NATIVE_TOOL_SMOKE_TIMEOUT", "90")),
        )
        print("=== hermes stdout ===")
        print(run.stdout)
        print("=== shim log ===")
        print(server_log.read_text(encoding="utf-8"))
        fake_text = fake_log.read_text(encoding="utf-8") if fake_log.exists() else ""
        if run.returncode != 0:
            print(f"hermes exited {run.returncode}", file=sys.stderr)
            return run.returncode
        if SENTINEL not in run.stdout:
            print("missing final sentinel", file=sys.stderr)
            return 1
        if "Available Hermes-native tools" not in fake_text or "TOOL RESULT:" not in fake_text:
            print("native tool prompt or tool result did not reach fake Claude", file=sys.stderr)
            return 1
        print(f"Hermes native tool-call smoke passed on port {port}")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def main() -> int:
    hermes = find_hermes()
    if hermes is None:
        print("missing Hermes CLI; set HERMES_BIN=/path/to/hermes", file=sys.stderr)
        return 2
    if os.environ.get("NATIVE_TOOL_SMOKE_KEEP_TMP"):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="claude-cli-native-tool-smoke-"))
        print(f"keeping temp dir: {tmp}")
        return run_smoke(hermes, tmp)
    with tempfile.TemporaryDirectory(prefix="claude-cli-native-tool-smoke-") as td:
        return run_smoke(hermes, pathlib.Path(td))


if __name__ == "__main__":
    raise SystemExit(main())
