from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


def load_autostart_module():
    path = pathlib.Path(__file__).resolve().parents[1] / "autostart.py"
    spec = importlib.util.spec_from_file_location("_test_claude_code_autostart", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load autostart.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_profile(home: pathlib.Path, config: str, env_text: str = "") -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(config, encoding="utf-8")
    (home / ".env").write_text(env_text, encoding="utf-8")


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.old_env = os.environ.copy()
        self.autostart = load_autostart_module()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)

    def test_profile_env_autostart_zero_disables_before_hermes_loads_env(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            write_profile(
                home,
                "model:\n  provider: claude-code-cli\n",
                "CLAUDE_CODE_CLI_AUTOSTART=0\nCLAUDE_CODE_CLI_BASE_URL=http://127.0.0.1:8799/v1\n",
            )
            os.environ.clear()
            os.environ["HERMES_HOME"] = str(home)

            self.assertTrue(self.autostart._provider_in_use())
            self.assertTrue(self.autostart._disabled())
            self.assertEqual(self.autostart._host_port(), ("127.0.0.1", 8799))
            with mock.patch.object(self.autostart, "_spawn") as spawn:
                self.assertFalse(self.autostart.ensure_server_running(wait_seconds=0.01))
            spawn.assert_not_called()

    def test_real_environment_overrides_profile_env(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            write_profile(
                home,
                "model:\n  provider: claude-code-cli\n",
                "CLAUDE_CODE_CLI_AUTOSTART=0\nCLAUDE_CODE_CLI_BASE_URL=http://127.0.0.1:8799/v1\n",
            )
            os.environ.clear()
            os.environ["HERMES_HOME"] = str(home)
            os.environ["CLAUDE_CODE_CLI_AUTOSTART"] = "1"
            os.environ["CLAUDE_CODE_CLI_PORT"] = "18888"

            self.assertFalse(self.autostart._disabled())
            self.assertEqual(self.autostart._host_port(), ("127.0.0.1", 18888))

    def test_spawn_child_env_inherits_profile_claude_code_knobs(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            write_profile(
                home,
                "model:\n  provider: claude-code-cli\n",
                "CLAUDE_CODE_CLI_BIN=/tmp/fake-claude\nCLAUDE_CODE_CLI_PORT=8799\nOTHER_SECRET=do-not-copy\n",
            )
            os.environ.clear()
            os.environ["HERMES_HOME"] = str(home)

            env = self.autostart._child_env("127.0.0.1", 8799)

        self.assertEqual(env["CLAUDE_CODE_CLI_BIN"], "/tmp/fake-claude")
        self.assertEqual(env["CLAUDE_CODE_CLI_PORT"], "8799")
        self.assertEqual(env["CLAUDE_CODE_CLI_NO_AUTOSTART"], "1")
        self.assertNotIn("OTHER_SECRET", env)

    def test_provider_scan_does_not_match_alias_text_outside_provider_field(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            write_profile(
                home,
                "model:\n  provider: custom:fugu\nnotes: claude-code-cli\n",
                "",
            )
            os.environ.clear()
            os.environ["HERMES_HOME"] = str(home)

            self.assertFalse(self.autostart._provider_in_use())

    def test_provider_requested_on_cli_argv_counts_as_in_use(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            write_profile(home, "model:\n  provider: custom:fugu\n", "")
            os.environ.clear()
            os.environ["HERMES_HOME"] = str(home)
            old_argv = sys.argv[:]
            sys.argv = ["hermes", "chat", "--provider", "claude-code-cli", "-m", "haiku"]
            try:
                self.assertTrue(self.autostart._provider_requested_on_argv())
                self.assertTrue(self.autostart._provider_in_use())
            finally:
                sys.argv = old_argv

    def test_provider_requested_on_cli_argv_equals_form_counts_as_in_use(self):
        self.assertTrue(self.autostart._provider_requested_on_argv([
            "hermes", "chat", "--provider=cc-cli", "-q", "hello",
        ]))

    def test_profile_env_honors_export_prefixed_disable_flag(self):
        # python-dotenv accepts `export KEY=VALUE`; the autostart gate must too,
        # or a hand-added `export CLAUDE_CODE_CLI_AUTOSTART=0` would be silently
        # ignored and spawn a real shim.
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            write_profile(
                home,
                "model:\n  provider: claude-code-cli\n",
                "export CLAUDE_CODE_CLI_AUTOSTART=0\nexport CLAUDE_CODE_CLI_BASE_URL=http://127.0.0.1:8799/v1\n",
            )
            os.environ.clear()
            os.environ["HERMES_HOME"] = str(home)

            values = self.autostart._profile_env_values()
            self.assertEqual(values.get("CLAUDE_CODE_CLI_AUTOSTART"), "0")
            self.assertEqual(values.get("CLAUDE_CODE_CLI_BASE_URL"), "http://127.0.0.1:8799/v1")
            self.assertTrue(self.autostart._disabled())
            self.assertEqual(self.autostart._host_port(), ("127.0.0.1", 8799))
            with mock.patch.object(self.autostart, "_is_up", return_value=False):
                with mock.patch.object(self.autostart, "_spawn") as spawn:
                    self.assertFalse(self.autostart.ensure_server_running(wait_seconds=0.01))
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
