from __future__ import annotations

import base64
import contextlib
import json
import os
import pathlib
import re
import stat
import subprocess
import tempfile
import threading
import time
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


class NativeToolCallTests(unittest.TestCase):
    """Hermes-native tool-call emulation: the shim emits OpenAI tool_calls."""

    def _tool_request(self, base: str, *, stream: bool = False, tool_choice="auto") -> request.Request:
        body = {
            "model": "haiku",
            "stream": stream,
            "messages": [{"role": "user", "content": "List my available skills."}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "skill_view",
                    "description": "Load a Hermes skill by name.",
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                },
            }],
            "tool_choice": tool_choice,
        }
        return request.Request(
            base + "/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

    def test_native_tool_call_response_shape_from_fake_cli_json(self):
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import json, sys
prompt = sys.stdin.read()
assert "Available Hermes-native tools" in prompt
assert "skill_view" in prompt
print(json.dumps({
    "result": json.dumps({"tool_calls": [{"name": "skill_view", "arguments": {"name": "hermes-agent"}}]}),
    "usage": {"input_tokens": 4, "output_tokens": 5},
}))
""")
            with temporary_env(CLAUDE_CODE_CLI_BIN=str(fake), CLAUDE_CODE_CLI_TIMEOUT="5"):
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    base = f"http://127.0.0.1:{httpd.server_address[1]}"
                    completion = json.loads(request.urlopen(self._tool_request(base), timeout=5).read().decode("utf-8"))
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=5)

        choice = completion["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        message = choice["message"]
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["content"], "")
        calls = message["tool_calls"]
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["id"].startswith("call_"))
        self.assertEqual(calls[0]["type"], "function")
        self.assertEqual(calls[0]["function"]["name"], "skill_view")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"name": "hermes-agent"})
        self.assertEqual(completion["usage"]["total_tokens"], 9)

    def test_native_tool_call_fallbacks_to_text_on_non_json_result(self):
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"result": "I can answer without a tool.", "usage": {"input_tokens": 1, "output_tokens": 2}}))
""")
            with temporary_env(CLAUDE_CODE_CLI_BIN=str(fake), CLAUDE_CODE_CLI_TIMEOUT="5"):
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    base = f"http://127.0.0.1:{httpd.server_address[1]}"
                    completion = json.loads(request.urlopen(self._tool_request(base), timeout=5).read().decode("utf-8"))
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=5)

        choice = completion["choices"][0]
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(choice["message"]["content"], "I can answer without a tool.")
        self.assertNotIn("tool_calls", choice["message"])

    def test_native_tool_call_stream_request_is_buffered_tool_call_response(self):
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({
    "result": json.dumps({"tool_calls": [{"name": "skill_view", "arguments": {"name": "debugging-workflows"}}]}),
    "usage": {"input_tokens": 3, "output_tokens": 4},
}))
""")
            with temporary_env(CLAUDE_CODE_CLI_BIN=str(fake), CLAUDE_CODE_CLI_TIMEOUT="5"):
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    base = f"http://127.0.0.1:{httpd.server_address[1]}"
                    with request.urlopen(self._tool_request(base, stream=True), timeout=5) as resp:
                        lines: list[str] = []
                        while True:
                            line = resp.readline().decode("utf-8")
                            if not line:
                                break
                            lines.append(line)
                            if line.strip() == "data: [DONE]":
                                break
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=5)

        events = []
        for line in lines:
            if not line.startswith("data: ") or line.strip() == "data: [DONE]":
                continue
            events.append(json.loads(line[len("data: "):]))
        tool_chunks = [
            ev["choices"][0]["delta"]["tool_calls"][0]
            for ev in events
            if ev.get("choices") and ev["choices"][0].get("delta", {}).get("tool_calls")
        ]
        self.assertEqual(len(tool_chunks), 1)
        self.assertEqual(tool_chunks[0]["function"]["name"], "skill_view")
        self.assertEqual(json.loads(tool_chunks[0]["function"]["arguments"]), {"name": "debugging-workflows"})
        self.assertTrue(any(ev.get("choices") and ev["choices"][0].get("finish_reason") == "tool_calls" for ev in events))
        self.assertEqual(lines[-1].strip(), "data: [DONE]")

    def test_extract_native_tool_calls_filters_unknown_names_and_allows_multiple(self):
        tools = [
            {"type": "function", "function": {"name": "skill_view"}},
            {"type": "function", "function": {"name": "todo"}},
        ]
        calls = server.extract_native_tool_calls(json.dumps({"tool_calls": [
            {"name": "skill_view", "arguments": {"name": "hermes-agent"}},
            {"name": "unknown", "arguments": {"x": 1}},
            {"function": {"name": "todo", "arguments": "{\"merge\":true}"}},
        ]}), tools)
        self.assertEqual([c["function"]["name"] for c in calls], ["skill_view", "todo"])
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"name": "hermes-agent"})
        self.assertEqual(json.loads(calls[1]["function"]["arguments"]), {"merge": True})

    def test_tool_choice_none_uses_plain_text_even_when_tools_are_present(self):
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import json, sys
prompt = sys.stdin.read()
print(json.dumps({"result": "has_native_prompt=" + str("Available Hermes-native tools" in prompt)}))
""")
            with temporary_env(CLAUDE_CODE_CLI_BIN=str(fake), CLAUDE_CODE_CLI_TIMEOUT="5"):
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    base = f"http://127.0.0.1:{httpd.server_address[1]}"
                    completion = json.loads(request.urlopen(self._tool_request(base, tool_choice="none"), timeout=5).read().decode("utf-8"))
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=5)
        self.assertEqual(completion["choices"][0]["finish_reason"], "stop")
        self.assertEqual(completion["choices"][0]["message"]["content"], "has_native_prompt=False")

    def test_engine_always_preserves_legacy_claude_code_engine_mode(self):
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import json, sys
prompt = sys.stdin.read()
print(json.dumps({
    "result": "argv=" + " ".join(sys.argv[1:]) + "\\nprompt=" + prompt[-120:],
    "usage": {"output_tokens": 1},
}))
""")
            with temporary_env(CLAUDE_CODE_CLI_BIN=str(fake), CLAUDE_CODE_CLI_TIMEOUT="5",
                               CLAUDE_CODE_CLI_ENGINE="always"):
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    base = f"http://127.0.0.1:{httpd.server_address[1]}"
                    completion = json.loads(request.urlopen(self._tool_request(base), timeout=5).read().decode("utf-8"))
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=5)

        text = completion["choices"][0]["message"]["content"]
        self.assertEqual(completion["choices"][0]["finish_reason"], "stop")
        self.assertIn("--allowedTools", text)
        self.assertIn("carry out the task", text)
        self.assertNotIn("tool_calls", completion["choices"][0]["message"])


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
        # bool is an int subclass - must not leak True/False as "1"/"0".
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


class VisionPassthroughTests(unittest.TestCase):
    """Issue #4: image parts should reach the CLI as bounded temp-file/URL refs."""

    def _data_url(self, payload: bytes = b"fakepng") -> str:
        return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")

    def test_default_flatten_messages_still_omits_images_without_collector(self):
        prompt = server.flatten_messages([{"role": "user", "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": self._data_url()}},
        ]}])
        self.assertIn("[image omitted]", prompt)
        self.assertNotIn("image saved to", prompt)

    def test_image_collector_materializes_base64_and_cleanup_removes_file(self):
        payload = b"PNGDATA"
        with temporary_env(CLAUDE_CODE_CLI_VISION="1", CLAUDE_CODE_CLI_MAX_IMAGES="8",
                           CLAUDE_CODE_CLI_MAX_IMAGE_MB="10"):
            images = server._ImageCollector()
            prompt = server.flatten_messages([{"role": "user", "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": self._data_url(payload)}},
            ]}], images)

        self.assertEqual(len(images.paths), 1)
        img = pathlib.Path(images.paths[0])
        self.assertTrue(img.exists())
        self.assertEqual(img.read_bytes(), payload)
        self.assertIn(str(img), prompt)
        self.assertIn("read this file to view the image", prompt)
        parent = img.parent
        images.cleanup()
        self.assertFalse(img.exists())
        self.assertFalse(parent.exists())

    def test_image_collector_passes_remote_url_without_downloading(self):
        images = server._ImageCollector()
        prompt = server.flatten_messages([{"role": "user", "content": [
            {"type": "image_url", "image_url": "https://example.invalid/cat.png"},
        ]}], images)
        try:
            self.assertIn("[image at https://example.invalid/cat.png", prompt)
            self.assertEqual(images.paths, [])
            self.assertEqual(images.count, 1)
        finally:
            images.cleanup()

    def test_image_limits_and_disabled_mode_degrade_gracefully(self):
        with temporary_env(CLAUDE_CODE_CLI_MAX_IMAGES="1", CLAUDE_CODE_CLI_VISION="1"):
            images = server._ImageCollector()
            first = images.add(self._data_url(b"one"))
            second = images.add(self._data_url(b"two"))
        try:
            self.assertIn("image saved to", first)
            self.assertIn("over the 1-image limit", second)
            self.assertEqual(len(images.paths), 1)
        finally:
            images.cleanup()

        with temporary_env(CLAUDE_CODE_CLI_VISION="0"):
            disabled = server._ImageCollector()
            self.assertEqual(disabled.add(self._data_url()), "[image omitted]")
            self.assertEqual(disabled.paths, [])

    def test_oversized_image_is_omitted_without_temp_file(self):
        with temporary_env(CLAUDE_CODE_CLI_MAX_IMAGE_MB="0.000001"):
            images = server._ImageCollector()
            text = images.add(self._data_url(b"more-than-one-byte"))
        try:
            self.assertIn("exceeds", text)
            self.assertEqual(images.paths, [])
        finally:
            images.cleanup()

    def test_http_request_image_file_is_visible_to_fake_cli_then_cleaned(self):
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import json, os, re, sys
prompt = sys.stdin.read()
m = re.search(r"image saved to (\\S+)", prompt)
path = m.group(1) if m else ""
exists = bool(path and os.path.exists(path))
size = os.path.getsize(path) if exists else -1
print(json.dumps({"result": f"image_exists={exists} image_size={size} image_path={path}", "usage": {"input_tokens": 1, "output_tokens": 1}}))
""")
            payload = b"visible"
            with temporary_env(CLAUDE_CODE_CLI_BIN=str(fake), CLAUDE_CODE_CLI_TIMEOUT="5",
                               CLAUDE_CODE_CLI_VISION="1"):
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    req = request.Request(
                        f"http://127.0.0.1:{httpd.server_address[1]}/v1/chat/completions",
                        data=json.dumps({
                            "model": "haiku",
                            "messages": [{"role": "user", "content": [
                                {"type": "text", "text": "describe"},
                                {"type": "image_url", "image_url": {"url": self._data_url(payload)}},
                            ]}],
                        }).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    completion = json.loads(request.urlopen(req, timeout=5).read().decode("utf-8"))
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=5)

        text = completion["choices"][0]["message"]["content"]
        self.assertIn("image_exists=True", text)
        self.assertIn("image_size=7", text)
        match = re.search(r"image_path=(\S+)", text)
        self.assertIsNotNone(match)
        assert match is not None
        path = match.group(1)
        self.assertFalse(pathlib.Path(path).exists())
        self.assertFalse(pathlib.Path(path).parent.exists())


class RealStreamingTests(unittest.TestCase):
    """Issue #3: stream:true should bridge Claude stream-json events incrementally."""

    def test_stream_argv_uses_stream_json_verbose_and_partial_messages(self):
        with temporary_env(CLAUDE_CODE_CLI_BIN="/tmp/fake-claude"):
            argv = server.build_claude_argv("sonnet", engine=False, stream=True)
        self.assertEqual(argv[:6], ["/tmp/fake-claude", "-p", "--model", "sonnet", "--output-format", "stream-json"])
        self.assertIn("--verbose", argv)
        self.assertIn("--include-partial-messages", argv)

    def test_iter_stream_json_ignores_invalid_lines(self):
        events = list(server.iter_stream_json([
            "not-json\n",
            "{\"type\": \"stream_event\"}\n",
            "[]\n",
            "  {\"type\": \"result\", \"result\": \"ok\"}  \n",
        ]))
        self.assertEqual([ev.get("type") for ev in events], ["stream_event", "result"])

    def test_extract_stream_delta_handles_wrapped_and_direct_shapes(self):
        wrapped = {"type": "stream_event", "event": {
            "type": "content_block_delta", "delta": {"type": "text_delta", "text": "hel"},
        }}
        direct = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}}
        tool_args = {"type": "stream_event", "event": {
            "type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{}"},
        }}
        self.assertEqual(server._extract_stream_delta(wrapped), "hel")
        self.assertEqual(server._extract_stream_delta(direct), "lo")
        self.assertEqual(server._extract_stream_delta(tool_args), "")

    def test_stream_claude_yields_delta_before_final_result(self):
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import json, sys, time
sys.stdin.read()
fmt = sys.argv[sys.argv.index("--output-format") + 1] if "--output-format" in sys.argv else ""
if fmt != "stream-json":
    print("expected stream-json", file=sys.stderr)
    sys.exit(9)
def emit(obj):
    print(json.dumps(obj), flush=True)
emit({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hel"}}})
time.sleep(0.2)
emit({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}})
emit({"type": "result", "result": "hello", "usage": {"input_tokens": 4, "output_tokens": 5}})
""")
            with temporary_env(CLAUDE_CODE_CLI_BIN=str(fake), CLAUDE_CODE_CLI_TIMEOUT="5"):
                gen = server.stream_claude("prompt", "haiku", engine=False)
                started = time.monotonic()
                first = next(gen)
                first_elapsed = time.monotonic() - started
                rest = list(gen)

        self.assertLess(first_elapsed, 1.0)
        self.assertEqual(first, ("delta", "hel"))
        self.assertIn(("delta", "lo"), rest)
        finals = [payload for kind, payload in rest if kind == "final" and isinstance(payload, dict)]
        self.assertEqual(len(finals), 1)
        final = finals[0]
        self.assertEqual(final["text"], "hello")
        self.assertEqual(final["usage"], {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9})

    def test_stream_claude_bare_json_result_falls_back_to_single_delta(self):
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"result": "buffered fallback", "usage": {"input_tokens": 2, "output_tokens": 3}}), flush=True)
""")
            with temporary_env(CLAUDE_CODE_CLI_BIN=str(fake), CLAUDE_CODE_CLI_TIMEOUT="5"):
                events = list(server.stream_claude("prompt", "haiku", engine=False))
        self.assertEqual(events[0], ("delta", "buffered fallback"))
        self.assertEqual(events[-1][0], "final")
        final = events[-1][1]
        self.assertIsInstance(final, dict)
        assert isinstance(final, dict)
        self.assertEqual(final["usage"]["total_tokens"], 5)

    def test_http_stream_response_forwards_incremental_jsonl_and_usage(self):
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
def emit(obj):
    print(json.dumps(obj), flush=True)
emit({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "one"}}})
emit({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "two"}}})
emit({"type": "result", "result": "onetwo", "usage": {"input_tokens": 1, "output_tokens": 2}})
""")
            with temporary_env(CLAUDE_CODE_CLI_BIN=str(fake), CLAUDE_CODE_CLI_TIMEOUT="5",
                               CLAUDE_CODE_CLI_STREAM="1"):
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    req = request.Request(
                        f"http://127.0.0.1:{httpd.server_address[1]}/v1/chat/completions",
                        data=json.dumps({
                            "model": "haiku",
                            "stream": True,
                            "stream_options": {"include_usage": True},
                            "messages": [{"role": "user", "content": "say hi"}],
                        }).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with request.urlopen(req, timeout=5) as resp:
                        lines: list[str] = []
                        while True:
                            line = resp.readline().decode("utf-8")
                            if not line:
                                break
                            lines.append(line)
                            if line.strip() == "data: [DONE]":
                                break
                    body = "".join(lines)
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=5)
        self.assertIn('"content": "one"', body)
        self.assertIn('"content": "two"', body)
        self.assertIn('"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}', body)
        self.assertIn("data: [DONE]", body)


class ProviderErrorHTTPTests(unittest.TestCase):
    """Claude failures must be OpenAI errors, never successful completions."""

    @contextlib.contextmanager
    def _server(self, fake: pathlib.Path, **env):
        values = {
            "CLAUDE_CODE_CLI_BIN": str(fake),
            "CLAUDE_CODE_CLI_TIMEOUT": "5",
            "CLAUDE_CODE_CLI_STREAM": "1",
        }
        values.update(env)
        with temporary_env(**values):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                yield f"http://127.0.0.1:{httpd.server_address[1]}"
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)

    def _request(self, base: str, *, stream: bool = False):
        return request.Request(
            base + "/v1/chat/completions",
            data=json.dumps({
                "model": "sonnet",
                "stream": stream,
                "messages": [{"role": "user", "content": "probe"}],
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

    def _http_error(self, base: str, *, stream: bool = False):
        with self.assertRaises(error.HTTPError) as cm:
            request.urlopen(self._request(base, stream=stream), timeout=5)
        exc = cm.exception
        body = json.loads(exc.read().decode("utf-8"))
        exc.close()
        return exc.code, body

    def test_error_classification_matrix(self):
        cases = [
            ({"api_error_status": 400, "subtype": "invalid_request_error"}, "bad request", 400),
            ({"api_error_status": 401, "subtype": "authentication_error"}, "oauth expired", 401),
            ({"api_error_status": 403}, "subscription forbidden", 403),
            ({"api_error_status": 429}, "usage limit", 429),
            ({"api_error_status": 503}, "temporarily unavailable", 503),
            ({}, "network connection failed", 502),
            ({}, "unknown real failure", 500),
        ]
        for structured, message, expected in cases:
            with self.subTest(message=message):
                info = server._classify_cli_error(message, structured=structured)
                self.assertEqual(info["status"], expected)

    def test_nonzero_exit_is_not_http_200(self):
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import sys
sys.stdin.read()
print("unknown provider failure", file=sys.stderr)
sys.exit(42)
""")
            with self._server(fake) as base:
                status, body = self._http_error(base)
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["code"], "provider_internal_error")
        self.assertNotIn("choices", body)

    def test_structured_auth_error_is_401(self):
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"is_error": True, "api_error_status": 401, "subtype": "authentication_error", "result": "OAuth expired"}))
""")
            with self._server(fake) as base:
                status, body = self._http_error(base)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["type"], "authentication_error")

    def test_structured_rate_limit_is_429(self):
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"is_error": True, "api_error_status": 429, "subtype": "rate_limit_error", "result": "usage limit reached"}))
""")
            with self._server(fake) as base:
                status, body = self._http_error(base)
        self.assertEqual(status, 429)
        self.assertEqual(body["error"]["code"], "rate_limit_exceeded")

    def test_timeout_is_504(self):
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import sys, time
sys.stdin.read()
time.sleep(2)
""")
            with self._server(fake, CLAUDE_CODE_CLI_TIMEOUT="1") as base:
                status, body = self._http_error(base)
        self.assertEqual(status, 504)
        self.assertEqual(body["error"]["type"], "timeout_error")

    def test_temporary_upstream_error_is_503(self):
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"is_error": True, "api_error_status": 503, "result": "backend temporarily unavailable"}))
""")
            with self._server(fake) as base:
                status, body = self._http_error(base)
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["type"], "upstream_error")

    def test_pre_header_sse_error_uses_real_http_status(self):
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"type": "result", "is_error": True, "api_error_status": 429, "result": "quota exceeded"}), flush=True)
""")
            with self._server(fake) as base:
                status, body = self._http_error(base, stream=True)
        self.assertEqual(status, 429)
        self.assertEqual(body["error"]["code"], "rate_limit_exceeded")

    def test_post_header_sse_error_is_not_success_completion(self):
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}}}), flush=True)
print(json.dumps({"type": "result", "is_error": True, "api_error_status": 503, "result": "upstream unavailable"}), flush=True)
""")
            with self._server(fake) as base:
                with request.urlopen(self._request(base, stream=True), timeout=5) as resp:
                    self.assertEqual(resp.status, 200)  # headers were already committed
                    body = resp.read().decode("utf-8")
        self.assertIn('"error"', body)
        self.assertIn('"code": "upstream_unavailable"', body)
        self.assertNotIn('"finish_reason": "stop"', body)
        self.assertNotIn("data: [DONE]", body)

    def test_error_response_redacts_secrets_and_sensitive_paths(self):
        with tempfile.TemporaryDirectory() as td:
            fake = make_fake_claude(pathlib.Path(td), """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"is_error": True, "api_error_status": 401, "result": "Authorization: Bearer secret-token ANTHROPIC_API_KEY=sk-ant-secret /home/alice/private"}))
""")
            with self._server(fake) as base:
                _, body = self._http_error(base)
        rendered = json.dumps(body)
        self.assertNotIn("secret-token", rendered)
        self.assertNotIn("sk-ant-secret", rendered)
        self.assertNotIn("/home/alice", rendered)
        self.assertIn("[REDACTED]", rendered)


class ManagedServiceTests(unittest.TestCase):
    """Issue #2: opt-in service templates and helpers are shipped but not run."""

    def test_service_templates_and_scripts_are_present_and_safe_to_parse(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        install = root / "scripts" / "install-service.sh"
        uninstall = root / "scripts" / "uninstall-service.sh"
        systemd = root / "service" / "systemd" / "claude-code-cli-shim.service"
        launchd = root / "service" / "launchd" / "com.hermes.claude-code-cli-shim.plist"

        for script in (install, uninstall):
            self.assertTrue(script.exists(), script)
            self.assertTrue(os.access(script, os.X_OK), script)
            subprocess.run(["bash", "-n", str(script)], check=True)

        unit = systemd.read_text(encoding="utf-8")
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertIn("EnvironmentFile=-__HERMES_HOME__/claude-code-cli-shim.env", unit)
        self.assertIn("ExecStart=__PLUGIN_DIR__/start.sh", unit)

        plist = launchd.read_text(encoding="utf-8")
        self.assertIn("<key>KeepAlive</key>", plist)
        self.assertIn("__PLUGIN_DIR__/start.sh", plist)
        self.assertIn("__HOST__", plist)
        self.assertIn("__PORT__", plist)


if __name__ == "__main__":
    unittest.main()
