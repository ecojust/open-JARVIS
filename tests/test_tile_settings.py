"""Voice-bar display settings use actual Qt widgets and isolated preferences."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, QProcess, Qt
    from PySide6.QtWidgets import QApplication

    from jarvis.config import read_preferences
    from jarvis.desktop_client import DesktopController
    from jarvis.desktop_ui import VoiceTile
    from test_desktop import FakeClient, FakeProcess

    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


@unittest.skipUnless(QT_AVAILABLE, "PySide6 is an optional desktop dependency")
class TileSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(
            os.environ,
            {
                "QT_QPA_PLATFORM": "offscreen",
                "JARVIS_CONFIG_FILE": str(Path(temporary.name) / "settings.json"),
                "JARVIS_DATA_DIR": temporary.name,
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.c = self.make_controller()
        self.c.ready = True
        self.c.configured = True
        self.c.listening = True
        self.c.active_session_id = "same-session"
        self.c.backend.fake_state = QProcess.ProcessState.Running
        self.c.audio.fake_state = QProcess.ProcessState.Running

    def make_controller(self):
        controller = DesktopController(
            self.app, autostart=False, process_factory=FakeProcess
        )
        controller.client = FakeClient(parent=controller)
        controller._refresh_model_states = Mock()

        def cleanup():
            controller.cleanup()
            controller.deleteLater()
            self.app.processEvents()

        self.addCleanup(cleanup)
        return controller

    def test_default_is_checked_and_environment_false_is_respected(self):
        self.assertTrue(self.c.tile.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self.c.show_settings()
        self.assertTrue(self.c.settings_dialog.always_on_top.isChecked())
        self.assertEqual(
            self.c.settings_dialog.always_on_top.text(), "桌面语音条始终置顶"
        )
        self.c.settings_dialog.reject()
        with patch.dict(os.environ, {"UI_TILE_ALWAYS_ON_TOP": "false"}):
            other = self.make_controller()
            self.assertFalse(other.tile_always_on_top)
            self.assertFalse(
                other.tile.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
            )

    def test_ui_save_is_immediate_during_task_and_tts_and_survives_restart(self):
        self.c.tasks["run"] = {"status": "running"}
        self.c.speaking = True
        self.c.tile.move(QPoint(42, 64))
        geometry = self.c.tile.geometry()
        results = self.c._ensure_results()
        result_flags = results.windowFlags()
        self.c.show_settings()
        dialog = self.c.settings_dialog
        dialog.always_on_top.setChecked(False)
        with patch.object(self.c, "_maybe_apply_settings") as apply:
            dialog.accept()
        apply.assert_not_called()
        self.assertFalse(self.c.pending_settings)
        self.assertFalse(self.c._restarting)
        self.assertFalse(self.c.tile.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self.assertEqual(self.c.tile.geometry(), geometry)
        self.assertTrue(self.c.tile.isVisible())
        self.assertEqual(results.windowFlags(), result_flags)
        self.assertTrue(self.c.listening)
        self.assertTrue(self.c.speaking)
        self.assertFalse(self.c.user_paused)
        self.assertEqual(self.c.active_session_id, "same-session")
        self.assertFalse(self.c.client.closed)
        self.assertEqual(self.c.audio.commands, [])
        for worker in (self.c.audio, self.c.backend):
            self.assertEqual(worker.state(), QProcess.ProcessState.Running)
            self.assertEqual(worker.launches, [])
        self.assertFalse(read_preferences()["UI_TILE_ALWAYS_ON_TOP"])
        os.environ.pop("UI_TILE_ALWAYS_ON_TOP")
        other = self.make_controller()
        self.assertFalse(other.tile.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        other.show_settings()
        self.assertFalse(other.settings_dialog.always_on_top.isChecked())
        other.settings_dialog.always_on_top.setChecked(True)
        other.settings_dialog.accept()
        self.assertTrue(other.tile.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self.assertTrue(read_preferences()["UI_TILE_ALWAYS_ON_TOP"])

    def test_ui_save_preserves_pending_model_reload_without_starting_it(self):
        self.c.pending_settings = True
        self.c.speech_enabled = False
        self.c.show_settings()
        self.c.settings_dialog.always_on_top.setChecked(False)
        with patch.object(self.c, "_maybe_apply_settings") as apply:
            self.c.settings_dialog.accept()
        self.assertTrue(self.c.pending_settings)
        self.assertFalse(self.c.tile_always_on_top)
        apply.assert_not_called()
        self.assertEqual(self.c.audio.commands, [])

    def test_model_changes_still_schedule_restart_but_tile_changes_apply_now(self):
        self.c.tasks["run"] = {"status": "running"}
        self.c.show_settings()
        self.c.settings_dialog.fields["OPENAI_MODEL"].setText("new-task-model")
        self.c.settings_dialog.always_on_top.setChecked(False)
        self.c.settings_dialog.accept()
        self.assertTrue(self.c.pending_settings)
        self.assertFalse(self.c._restarting)
        self.assertFalse(self.c.tile_always_on_top)
        self.assertFalse(self.c.tile.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self.assertEqual(self.c.audio.commands, [])

    def test_macos_off_uses_normal_window_and_switching_preserves_visibility(self):
        with patch("jarvis.desktop_ui.sys.platform", "darwin"):
            tile = VoiceTile(self.c, always_on_top=False)
            self.addCleanup(tile.deleteLater)
            self.assertEqual(tile.windowType(), Qt.WindowType.Window)
            self.assertFalse(tile.isVisible())
            tile.move(80, 90)
            geometry = tile.geometry()
            tile.set_always_on_top(True)
            self.assertEqual(tile.windowType(), Qt.WindowType.Tool)
            self.assertTrue(tile.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
            self.assertFalse(tile.isVisible())
            self.assertEqual(tile.geometry(), geometry)
            tile.show()
            tile.set_always_on_top(False)
            self.assertEqual(tile.windowType(), Qt.WindowType.Window)
            self.assertFalse(tile.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
            self.assertTrue(tile.isVisible())
            self.assertEqual(tile.geometry(), geometry)
            self.assertTrue(tile.windowFlags() & Qt.WindowType.FramelessWindowHint)
            tile.hide()


if __name__ == "__main__":
    unittest.main()
