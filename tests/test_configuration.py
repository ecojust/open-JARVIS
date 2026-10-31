"""Settings must work outside a source checkout and never modify the bundle."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from jarvis import paths
from jarvis.config import Settings, read_preferences, save_preferences
from jarvis.launcher import child_command


class ConfigurationTests(unittest.TestCase):
    def test_preferences_are_atomic_private_and_applied_to_children(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {"JARVIS_CONFIG_FILE": str(Path(directory) / "settings.json")},
                clear=True,
            ),
        ):
            save_preferences(
                {"OPENAI_API_KEY": "example-secret", "UI_SPEECH_ENABLED": False}
            )
            save_preferences({"OPENAI_MODEL": "local-tool-model"})
            saved = read_preferences()
            self.assertEqual(saved["OPENAI_API_KEY"], "example-secret")
            self.assertFalse(saved["UI_SPEECH_ENABLED"])
            self.assertEqual(os.environ["OPENAI_MODEL"], "local-tool-model")
            self.assertEqual(os.environ["UI_SPEECH_ENABLED"], "false")
            self.assertEqual(list(Path(directory).glob(".settings-*")), [])
            if os.name != "nt":
                self.assertEqual(paths.preferences_path().stat().st_mode & 0o777, 0o600)

    def test_credentials_cannot_override_instance_identity_from_preferences(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "JARVIS_CONFIG_FILE": str(Path(directory) / "settings.json"),
                    "OPENAI_MODEL": "explicit",
                },
                clear=True,
            ),
        ):
            paths.preferences_path().write_text(
                json.dumps(
                    {
                        "OPENAI_MODEL": "saved",
                        "OPENAI_API_KEY": "saved-key",
                        "JARVIS_INSTANCE_TOKEN": "stale-token",
                    }
                )
            )
            paths.load_environment()
            self.assertEqual(os.environ["OPENAI_MODEL"], "explicit")
            self.assertEqual(os.environ["OPENAI_API_KEY"], "saved-key")
            self.assertNotIn("JARVIS_INSTANCE_TOKEN", os.environ)
            with self.assertRaises(ValueError):
                save_preferences({"JARVIS_INSTANCE_TOKEN": "persisted-token"})

    def test_frozen_models_are_in_writable_user_data_not_bundle(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"JARVIS_DATA_DIR": directory}, clear=True),
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", "/readonly/bundle", create=True),
        ):
            self.assertEqual(
                paths.asset_path("assets/icon.png"),
                Path("/readonly/bundle/assets/icon.png"),
            )
            self.assertEqual(
                paths.model_dir("kokoro"),
                Path(directory).resolve() / "models" / "kokoro",
            )
            with patch.dict(os.environ, {"JARVIS_STT_MODEL_DIR": "custom-model"}):
                self.assertEqual(
                    paths.model_dir("sensevoice"),
                    Path(directory).resolve() / "custom-model",
                )

    def test_saved_settings_override_dotenv_defaults_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                f"JARVIS_DATA_DIR={directory}\nOPENAI_API_KEY=\nOPENAI_MODEL=dotenv-default\nUI_SPEECH_ENABLED=true\n",
                encoding="utf-8",
            )
            (root / "settings.json").write_text(
                json.dumps(
                    {
                        "OPENAI_API_KEY": "saved-key",
                        "OPENAI_MODEL": "saved-model",
                        "UI_SPEECH_ENABLED": False,
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(paths, "resource_root", return_value=root),
            ):
                paths.load_environment()
                self.assertEqual(os.environ["OPENAI_API_KEY"], "saved-key")
                self.assertEqual(os.environ["OPENAI_MODEL"], "saved-model")
                self.assertEqual(os.environ["UI_SPEECH_ENABLED"], "false")
            with (
                patch.dict(os.environ, {"OPENAI_MODEL": "explicit-env"}, clear=True),
                patch.object(paths, "resource_root", return_value=root),
            ):
                paths.load_environment()
                self.assertEqual(os.environ["OPENAI_MODEL"], "explicit-env")

    def test_native_worker_entrypoints_dont_treat_frozen_app_as_python(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "platform", "win32"),
            patch.object(sys, "executable", "/bundle/open-JARVIS.exe"),
        ):
            self.assertEqual(
                child_command("audio"),
                [
                    str(
                        Path("/bundle/open-JARVIS.exe").with_name(
                            "open-JARVIS-worker.exe"
                        )
                    ),
                    "--role",
                    "audio",
                ],
            )
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "platform", "darwin"),
            patch.object(sys, "executable", "/App/Contents/MacOS/open-JARVIS"),
        ):
            self.assertEqual(
                child_command("backend", "--port", "8765"),
                [
                    str(
                        Path("/App/Contents/MacOS/open-JARVIS").with_name(
                            "open-JARVIS-worker"
                        )
                    ),
                    "--role",
                    "backend",
                    "--port",
                    "8765",
                ],
            )

    def test_defaults_respect_writable_user_directory_and_budget_bounds(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "JARVIS_DATA_DIR": directory,
                    "JARVIS_MAX_MODEL_CALLS": "999",
                    "JARVIS_INSTANCE_TOKEN": "session-token",
                },
                clear=True,
            ),
        ):
            settings = Settings.from_env()
            self.assertEqual(
                settings.workspace, Path(directory).resolve() / "workspace"
            )
            self.assertEqual(settings.data_dir, Path(directory).resolve())
            self.assertEqual(settings.max_model_calls, 999)
            self.assertEqual(settings.max_tool_calls, 1000)
            self.assertEqual(settings.max_tool_rounds, 1001)
            self.assertEqual(settings.instance_token, "session-token")
            self.assertEqual(settings.orchestrator, "maf")

    def test_run_budgets_default_to_1000_tools_and_preserve_lower_explicit_limits(self):
        self.assertEqual(Settings().max_tool_calls, 1000)
        self.assertEqual(Settings().max_tool_rounds, 1001)
        self.assertEqual(Settings().max_model_calls, 3001)
        self.assertEqual(Settings().task_timeout, 300)
        with tempfile.TemporaryDirectory() as directory:
            for values, expected in (
                ({}, (1000, 1001, 3001)),
                (
                    {
                        "JARVIS_MAX_TOOL_CALLS": "3",
                        "JARVIS_MAX_TOOL_ROUNDS": "4",
                        "JARVIS_MAX_MODEL_CALLS": "5",
                    },
                    (3, 4, 5),
                ),
                (
                    {
                        "JARVIS_MAX_TOOL_CALLS": "99999",
                        "JARVIS_MAX_TOOL_ROUNDS": "99999",
                        "JARVIS_MAX_MODEL_CALLS": "99999",
                    },
                    (1000, 1001, 3001),
                ),
            ):
                with patch.dict(
                    os.environ, {"JARVIS_DATA_DIR": directory, **values}, clear=True
                ):
                    settings = Settings.from_env()
                    self.assertEqual(
                        (
                            settings.max_tool_calls,
                            settings.max_tool_rounds,
                            settings.max_model_calls,
                        ),
                        expected,
                    )
                    self.assertEqual(settings.task_timeout, 300)


if __name__ == "__main__":
    unittest.main()
