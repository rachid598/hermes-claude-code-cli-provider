from __future__ import annotations

import contextlib
import json
import os
import pathlib
import stat
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import error, request

import claude_code_server as server


@contextlib.contextmanager
def temporary_env(**updates: str):
    old = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def make_fake_claude(directory: pathlib.Path, body: str) -> pathlib.Path:
    path = directory / "claude"
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class ClaudeCodeServerUnitTests(unittest.TestCase):
    def test_flatten_messages_preserves_system_conversation_and_tool_context(self):
        prompt = server.flatten_messages([
            {"role": "system", "content": "System rule."},
            {"role": "developer", "content": "Developer rule."},
            {"role": "user", "content": [
                {"type": "text", "text": "hello"},
                {"type": "image_url", "image_url": {"url": "https://example.invalid/img.png"}},
            ]},
            {"role": "assistant", "content": "I will check.", "tool_calls": [
                {"function": {"name": "Read"}},
            ]},
            {"role": "tool", "content": "file contents"},
        ])

        self.assertIn("SYSTEM INSTRUCTIONS:\nSystem rule.\n\nDeveloper rule.", prompt)
        self.assertIn("USER: hello\n[image omitted]", prompt)
        self.assertIn("ASSISTANT: I will check. [requested tools: Read]", prompt)
        self.assertIn("TOOL RESULT: file contents", prompt)
        self.assertTrue(prompt.endswith("Return only the response text."))

    def test_build_claude_argv_text_mode_disables_tools(self):
        with temporary_env(
            CLAUDE_CODE_CLI_BIN="/tmp/fake-claude",
            CLAUDE_CODE_CLI_EFFORT="medium",
            CLAUDE_CODE_CLI_TOOLS="",
            CLAUDE_CODE_CLI_MAX_TURNS="3",
            CLAUDE_CODE_CLI_EXTRA_ARGS="--safe-mode --debug-file /tmp/debug.log",
        ):
            argv = server.build_claude_argv("haiku", engine=False)

        self.assertEqual(argv[:6], [
            "/tmp/fake-claude", "-p", "--model", "haiku", "--output-format", "json",
        ])
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("--effort", argv)
        self.assertIn("medium", argv)
        self.assertIn("--tools", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--max-turns") + 1], "3")
        self.assertIn("--safe-mode", argv)
        self.assertIn("--debug-file", argv)

    def test_build_claude_argv_engine_mode_uses_allowed_tools_and_cwd_knobs(self):
        with temporary_env(
            CLAUDE_CODE_CLI_BIN="/tmp/fake-claude",
            CLAUDE_CODE_CLI_ENGINE_TOOLS="Read,Grep",
            CLAUDE_CODE_CLI_DISALLOWED_TOOLS="Bash",
            CLAUDE_CODE_CLI_ENGINE_MAX_TURNS="7",
            CLAUDE_CODE_CLI_ENGINE_PERMISSION="bypass",
            CLAUDE_CODE_CLI_ADD_DIR="/tmp/a:/tmp/b",
        ):
            argv = server.build_claude_argv("sonnet", engine=True)

        self.assertIn("--allowedTools", argv)
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Grep")
        self.assertIn("--disallowedTools", argv)
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "Bash")
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertIn("--add-dir", argv)
        self.assertIn("/tmp/a", argv)
        self.assertIn("/tmp/b", argv)
        self.assertEqual(argv[argv.index("--max-turns") + 1], "7")

    def test_extract_json_object_accepts_raw_fenced_and_prefixed_json(self):
        raw = server._extract_json_object('{"result":"ok"}')
        fenced = server._extract_json_object('```json\n{"result":"ok"}\n```')
        prefixed = server._extract_json_object('noise {"result":"ok"} tail')

        assert raw is not None
        assert fenced is not None
        assert prefixed is not None
        self.assertEqual(raw["result"], "ok")
        self.assertEqual(fenced["result"], "ok")
        self.assertEqual(prefixed["result"], "ok")
        self.assertIsNone(server._extract_json_object("not json"))

    def test_run_claude_maps_fake_cli_json_result_and_usage(self):
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import json, sys
prompt = sys.stdin.read()
print(json.dumps({
    "result": "fake response to " + prompt[-5:],
    "usage": {"input_tokens": 2, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 5, "output_tokens": 7},
}))
""")
            with temporary_env(CLAUDE_CODE_CLI_BIN=str(fake), CLAUDE_CODE_CLI_TIMEOUT="5"):
                outcome = server.run_claude("hello world", "haiku", engine=False)

        self.assertEqual(outcome["text"], "fake response to world")
        self.assertEqual(outcome["usage"], {"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17})
        self.assertEqual(outcome["error"], "")

    def test_run_claude_surfaces_nonzero_exit_without_crashing(self):
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import sys
print("boom from fake claude", file=sys.stderr)
sys.exit(42)
""")
            with temporary_env(CLAUDE_CODE_CLI_BIN=str(fake), CLAUDE_CODE_CLI_TIMEOUT="5"):
                outcome = server.run_claude("hello", "haiku", engine=False)

        self.assertIn("claude exited 42", outcome["error"])
        self.assertIn("boom from fake claude", outcome["text"])
        self.assertEqual(outcome["usage"], server._zero_usage())

    def test_http_chat_completion_and_stream_work_with_fake_cli(self):
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"result": "hello from fake http", "usage": {"input_tokens": 1, "output_tokens": 2}}))
""")
            with temporary_env(CLAUDE_CODE_CLI_BIN=str(fake), CLAUDE_CODE_CLI_TIMEOUT="5"):
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    base = f"http://127.0.0.1:{httpd.server_address[1]}"
                    health = json.loads(request.urlopen(base + "/healthz", timeout=5).read().decode("utf-8"))
                    self.assertEqual(health["status"], "ok")

                    req = request.Request(
                        base + "/v1/chat/completions",
                        data=json.dumps({
                            "model": "haiku",
                            "messages": [{"role": "user", "content": "say hi"}],
                        }).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    completion = json.loads(request.urlopen(req, timeout=5).read().decode("utf-8"))
                    self.assertEqual(completion["object"], "chat.completion")
                    self.assertEqual(completion["choices"][0]["message"]["content"], "hello from fake http")
                    self.assertEqual(completion["usage"]["total_tokens"], 3)

                    stream_req = request.Request(
                        base + "/v1/chat/completions",
                        data=json.dumps({
                            "model": "haiku",
                            "stream": True,
                            "stream_options": {"include_usage": True},
                            "messages": [{"role": "user", "content": "say hi"}],
                        }).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with request.urlopen(stream_req, timeout=5) as resp:
                        lines: list[str] = []
                        while True:
                            line = resp.readline().decode("utf-8")
                            if not line:
                                break
                            lines.append(line)
                            if line.strip() == "data: [DONE]":
                                break
                    body = "".join(lines)
                    self.assertIn("data:", body)
                    self.assertIn("hello from fake http", body)
                    self.assertIn("data: [DONE]", body)
                    self.assertIn('"usage"', body)
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=5)

    def test_handler_rejects_missing_messages(self):
        # Directly exercising request validation through a tiny server avoids any real Claude call.
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            req = request.Request(
                f"http://127.0.0.1:{httpd.server_address[1]}/v1/chat/completions",
                data=json.dumps({"model": "haiku"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(error.HTTPError) as cm:
                try:
                    request.urlopen(req, timeout=5)
                except error.HTTPError as exc:
                    exc.close()
                    raise
            self.assertEqual(cm.exception.code, 400)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


class PerRequestOverrideTests(unittest.TestCase):
    """Issue #5: a single request can override effort / max-turns / tool lists,
    while an omitted field falls back to the env default."""

    # ---- extract_overrides: effort resolution order ----
    def test_effort_from_top_level_reasoning_effort(self):
        self.assertEqual(
            server.extract_overrides({"reasoning_effort": "low"}).get("effort"), "low"
        )

    def test_effort_from_extra_body_reasoning_effort(self):
        body = {"extra_body": {"reasoning": {"effort": "medium"}}}
        self.assertEqual(server.extract_overrides(body).get("effort"), "medium")

    def test_effort_from_extra_body_flat_effort(self):
        self.assertEqual(
            server.extract_overrides({"extra_body": {"effort": "high"}}).get("effort"),
            "high",
        )

    def test_top_level_effort_wins_over_extra_body(self):
        body = {"reasoning_effort": "low", "extra_body": {"reasoning": {"effort": "high"}}}
        self.assertEqual(server.extract_overrides(body).get("effort"), "low")

    def test_blank_effort_is_ignored(self):
        self.assertNotIn("effort", server.extract_overrides({"reasoning_effort": "  "}))

    # ---- extract_overrides: max_turns typing / boundaries ----
    def test_max_turns_accepts_int_and_digit_string(self):
        self.assertEqual(server.extract_overrides({"extra_body": {"max_turns": 9}})["max_turns"], "9")
        self.assertEqual(server.extract_overrides({"max_turns": "15"})["max_turns"], "15")

    def test_max_turns_extra_body_wins_over_top_level(self):
        body = {"max_turns": 3, "extra_body": {"max_turns": 7}}
        self.assertEqual(server.extract_overrides(body)["max_turns"], "7")

    def test_max_turns_bool_is_rejected(self):
        # bool is an int subclass — must not leak True/False as "1"/"0".
        self.assertNotIn("max_turns", server.extract_overrides({"extra_body": {"max_turns": True}}))

    def test_max_turns_negative_and_nonnumeric_rejected(self):
        self.assertNotIn("max_turns", server.extract_overrides({"extra_body": {"max_turns": "-4"}}))
        self.assertNotIn("max_turns", server.extract_overrides({"extra_body": {"max_turns": -4}}))
        self.assertNotIn("max_turns", server.extract_overrides({"extra_body": {"max_turns": "lots"}}))
        self.assertNotIn("max_turns", server.extract_overrides({"extra_body": {"max_turns": 2.5}}))

    # ---- extract_overrides: tool lists in all shapes ----
    def test_allowed_tools_from_csv_string(self):
        body = {"extra_body": {"allowed_tools": " Read , Bash ,, Grep "}}
        self.assertEqual(server.extract_overrides(body)["allowed_tools"], "Read,Bash,Grep")

    def test_allowed_tools_from_list_of_names(self):
        body = {"extra_body": {"allowed_tools": ["Read", "Bash", "Read"]}}  # dedup
        self.assertEqual(server.extract_overrides(body)["allowed_tools"], "Read,Bash")

    def test_allowed_tools_from_openai_tool_objects(self):
        body = {"extra_body": {"allowed_tools": [
            {"type": "function", "function": {"name": "Read"}},
            {"name": "Grep"},
            {"type": "function", "function": {}},  # nameless → skipped
        ]}}
        self.assertEqual(server.extract_overrides(body)["allowed_tools"], "Read,Grep")

    def test_disallowed_tools_passthrough(self):
        body = {"extra_body": {"disallowed_tools": ["Bash", "Write"]}}
        self.assertEqual(server.extract_overrides(body)["disallowed_tools"], "Bash,Write")

    def test_empty_tool_list_is_ignored(self):
        self.assertNotIn("allowed_tools", server.extract_overrides({"extra_body": {"allowed_tools": []}}))
        self.assertNotIn("allowed_tools", server.extract_overrides({"extra_body": {"allowed_tools": "  "}}))

    # ---- extract_overrides: defensive / malformed inputs never raise ----
    def test_non_dict_body_and_extra_body_return_empty(self):
        self.assertEqual(server.extract_overrides(None), {})
        self.assertEqual(server.extract_overrides([1, 2, 3]), {})
        self.assertEqual(server.extract_overrides({"extra_body": "nope"}), {})
        self.assertEqual(server.extract_overrides({}), {})

    # ---- build_claude_argv: overrides win, omissions fall back to env ----
    def test_override_effort_beats_env_default(self):
        with temporary_env(CLAUDE_CODE_CLI_BIN="/tmp/fake-claude", CLAUDE_CODE_CLI_EFFORT="high"):
            argv = server.build_claude_argv("sonnet", engine=False, overrides={"effort": "low"})
        self.assertEqual(argv[argv.index("--effort") + 1], "low")

    def test_override_max_turns_beats_env_in_both_modes(self):
        with temporary_env(
            CLAUDE_CODE_CLI_BIN="/tmp/fake-claude",
            CLAUDE_CODE_CLI_MAX_TURNS="12",
            CLAUDE_CODE_CLI_ENGINE_MAX_TURNS="40",
        ):
            text_argv = server.build_claude_argv("haiku", engine=False, overrides={"max_turns": "5"})
            eng_argv = server.build_claude_argv("sonnet", engine=True, overrides={"max_turns": "5"})
        self.assertEqual(text_argv[text_argv.index("--max-turns") + 1], "5")
        self.assertEqual(eng_argv[eng_argv.index("--max-turns") + 1], "5")

    def test_override_allowed_and_disallowed_tools_engine_mode(self):
        with temporary_env(
            CLAUDE_CODE_CLI_BIN="/tmp/fake-claude",
            CLAUDE_CODE_CLI_ENGINE_TOOLS="Read,Write,Edit,Bash",
        ):
            argv = server.build_claude_argv(
                "sonnet", engine=True,
                overrides={"allowed_tools": "Read,Grep", "disallowed_tools": "Bash"},
            )
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Grep")
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "Bash")

    def test_empty_overrides_are_byte_identical_to_env_only(self):
        # Backward compatibility: no overrides ⇒ exactly the pre-#5 argv.
        with temporary_env(
            CLAUDE_CODE_CLI_BIN="/tmp/fake-claude",
            CLAUDE_CODE_CLI_EFFORT="high",
            CLAUDE_CODE_CLI_MAX_TURNS="12",
            CLAUDE_CODE_CLI_ENGINE_MAX_TURNS="40",
            CLAUDE_CODE_CLI_ENGINE_TOOLS="Read,Bash",
        ):
            for engine in (False, True):
                self.assertEqual(
                    server.build_claude_argv("sonnet", engine=engine),
                    server.build_claude_argv("sonnet", engine=engine, overrides={}),
                )

    def test_two_requests_different_effort_produce_different_argv(self):
        # Issue #5 acceptance criterion (concurrent requests, distinct effort).
        with temporary_env(CLAUDE_CODE_CLI_BIN="/tmp/fake-claude", CLAUDE_CODE_CLI_EFFORT="high"):
            a = server.build_claude_argv("sonnet", engine=False,
                                         overrides=server.extract_overrides({"reasoning_effort": "low"}))
            b = server.build_claude_argv("sonnet", engine=False,
                                         overrides=server.extract_overrides({"reasoning_effort": "high"}))
            default = server.build_claude_argv("sonnet", engine=False,
                                               overrides=server.extract_overrides({}))
        self.assertEqual(a[a.index("--effort") + 1], "low")
        self.assertEqual(b[b.index("--effort") + 1], "high")
        self.assertNotEqual(a, b)
        self.assertEqual(default[default.index("--effort") + 1], "high")  # env fallback

    def test_run_claude_threads_overrides_into_argv(self):
        # End-to-end: a fake CLI echoes its own argv so we can assert the
        # override actually reached the subprocess, not just build_claude_argv.
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"result": "argv=" + " ".join(sys.argv[1:]), "usage": {"output_tokens": 1}}))
""")
            with temporary_env(
                CLAUDE_CODE_CLI_BIN=str(fake),
                CLAUDE_CODE_CLI_TIMEOUT="5",
                CLAUDE_CODE_CLI_EFFORT="high",
            ):
                outcome = server.run_claude(
                    "hi", "haiku", engine=False, overrides={"effort": "minimal"}
                )
        self.assertIn("--effort minimal", outcome["text"])
        self.assertNotIn("--effort high", outcome["text"])


if __name__ == "__main__":
    unittest.main()
