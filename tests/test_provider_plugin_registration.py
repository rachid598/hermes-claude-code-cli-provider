from __future__ import annotations

import importlib
import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest


class ProviderPluginRegistrationTests(unittest.TestCase):
    def test_registers_as_hermes_user_model_provider_when_hermes_is_importable(self):
        if importlib.util.find_spec("providers") is None:
            self.skipTest("Hermes providers package is not importable; set PYTHONPATH to a hermes-agent checkout")

        repo = pathlib.Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as td:
            hermes_home = pathlib.Path(td) / ".hermes"
            plugin_dir = hermes_home / "plugins" / "model-providers" / "claude-code-cli"
            plugin_dir.parent.mkdir(parents=True)
            plugin_dir.symlink_to(repo, target_is_directory=True)

            old_home = os.environ.get("HERMES_HOME")
            old_autostart = os.environ.get("CLAUDE_CODE_CLI_AUTOSTART")
            os.environ["HERMES_HOME"] = str(hermes_home)
            os.environ["CLAUDE_CODE_CLI_AUTOSTART"] = "0"
            try:
                providers = importlib.import_module("providers")
                providers.__dict__["_REGISTRY"].clear()
                providers.__dict__["_ALIASES"].clear()
                providers.__dict__["_discovered"] = False
                for mod in list(sys.modules):
                    if mod.startswith("_hermes_user_provider"):
                        del sys.modules[mod]

                profile = providers.get_provider_profile("claude-code-cli")
                self.assertIsNotNone(profile)
                assert profile is not None
                self.assertEqual(profile.name, "claude-code-cli")
                self.assertEqual(profile.api_mode, "chat_completions")
                self.assertEqual(profile.base_url, "http://127.0.0.1:8765/v1")
                self.assertEqual(profile.auth_type, "api_key")
                self.assertTrue(profile.supports_vision)
                self.assertEqual(profile.env_vars, ("CLAUDE_CODE_CLI_API_KEY", "CLAUDE_CODE_CLI_BASE_URL"))
                self.assertEqual(profile.fallback_models, ("opus", "sonnet", "haiku"))
                self.assertEqual(profile.default_aux_model, "haiku")
                self.assertIn("claude-code-local", profile.aliases)
                self.assertIs(providers.get_provider_profile("claude-code-local"), profile)
            finally:
                if old_home is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = old_home
                if old_autostart is None:
                    os.environ.pop("CLAUDE_CODE_CLI_AUTOSTART", None)
                else:
                    os.environ["CLAUDE_CODE_CLI_AUTOSTART"] = old_autostart


if __name__ == "__main__":
    unittest.main()
