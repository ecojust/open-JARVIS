"""Desktop download lifecycle through real Qt buttons and process signals."""

import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray, QCoreApplication, QEvent, QProcess
from PySide6.QtWidgets import QApplication, QDialog

from jarvis.desktop_client import DesktopController


class DownloadProcess(QProcess):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.running = False
        self.output = b""
        self.launches = []
        self.terminated = False

    def state(self):
        return (
            QProcess.ProcessState.Running
            if self.running
            else QProcess.ProcessState.NotRunning
        )

    def start(self, command, arguments):
        self.launches.append((command, arguments))
        self.running = True
        self.started.emit()

    def readAllStandardOutput(self):
        data, self.output = self.output, b""
        return QByteArray(data)

    def send(self, event, **data):
        payload = json.dumps({"event": event, "data": data}).encode() + b"\n"
        # Exercise split JSON lines, as real QProcess reads can fragment them.
        for chunk in (payload[:9], payload[9:]):
            self.output += chunk
            self.readyReadStandardOutput.emit()

    def finish(self, code=0):
        self.running = False
        self.finished.emit(code, QProcess.ExitStatus.NormalExit)

    def terminate(self):
        self.terminated = True
        self.running = False

    kill = terminate

    def waitForBytesWritten(self, timeout=30000):
        return True

    def waitForFinished(self, timeout=30000):
        return not self.running


class ModelDownloadControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        env = patch.dict(os.environ, {"JARVIS_DATA_DIR": self.temporary.name})
        env.start()
        self.addCleanup(env.stop)
        read = patch("jarvis.config.read_preferences", return_value={})
        read.start()
        self.addCleanup(read.stop)
        self.controller = DesktopController(
            self.app, autostart=False, process_factory=DownloadProcess
        )
        self.statuses = {kind: "missing" for kind in ("stt", "tts")}
        self.controller._model_status = lambda kind: {
            "kind": kind,
            "phase": self.statuses[kind],
            "directory": self.temporary.name,
            "version": "test-contract",
            "message": "模型状态",
        }
        self.controller._maybe_apply_settings = Mock()

    def tearDown(self):
        self.controller.cleanup()
        self.controller.deleteLater()
        self.app.processEvents()

    def test_settings_button_download_progress_close_reopen_and_verified_completion(
        self,
    ):
        self.controller.show_settings()
        dialog = self.controller.settings_dialog
        self.assertTrue(dialog.isVisible())
        dialog.model_rows["stt"].download_button.click()
        worker = self.controller._downloads["stt"]
        self.assertEqual(worker.launches[0][1][-1], "download-stt")
        self.controller.download_model("stt")
        self.assertIs(self.controller._downloads["stt"], worker)
        worker.send(
            "download.progress",
            kind="stt",
            phase="downloading",
            completed_bytes=40,
            total_bytes=100,
        )
        self.assertEqual(dialog.model_rows["stt"].progress.value(), 400)
        dialog.close()
        self.assertIsNone(self.controller.settings_dialog)
        self.assertTrue(worker.running)
        self.controller.show_settings()
        self.assertEqual(
            self.controller.settings_dialog.model_rows["stt"].progress.value(), 400
        )
        worker.send("download.completed", kind="stt", phase="ready")
        self.assertFalse(
            self.controller.settings_dialog.model_rows[
                "stt"
            ].download_button.isEnabled()
        )
        self.statuses["stt"] = "ready"
        worker.finish()
        self.assertEqual(self.controller._model_states["stt"]["phase"], "ready")
        self.assertTrue(self.controller.pending_settings)
        self.controller._maybe_apply_settings.assert_called_once()
        self.assertNotIn("stt", self.controller._downloads)

    def test_exit_zero_without_completed_protocol_cannot_claim_installed(self):
        self.controller.download_model("tts")
        self.statuses["tts"] = "ready"
        self.controller._downloads["tts"].finish()
        self.assertEqual(self.controller._model_states["tts"]["phase"], "failed")
        self.assertFalse(self.controller.pending_settings)

    def test_inline_download_keeps_unsaved_form_and_opens_no_additional_window(self):
        self.controller.show_settings()
        dialog = self.controller.settings_dialog
        dialog.fields["OPENAI_MODEL"].setText("unsaved-model-name")
        visible = {w for w in self.app.topLevelWidgets() if w.isVisible()}
        with patch("jarvis.config.save_preferences") as save:
            dialog.model_rows["stt"].download_button.click()
            self.app.processEvents()
            self.assertIs(self.controller.settings_dialog, dialog)
            self.assertEqual(
                {w for w in self.app.topLevelWidgets() if w.isVisible()}, visible
            )
            self.assertEqual(dialog.fields["OPENAI_MODEL"].text(), "unsaved-model-name")
            self.assertEqual([w for w in visible if isinstance(w, QDialog)], [dialog])
            dialog.reject()
            save.assert_not_called()
        self.assertTrue(self.controller._downloads["stt"].running)

    def test_inline_progress_survives_results_disposal_and_eta_reset(self):
        self.controller.show_results()
        self.controller.show_settings()
        dialog = self.controller.settings_dialog
        self.assertIs(dialog.parent(), self.controller.tile)
        dialog.model_rows["stt"].download_button.click()
        worker = self.controller._downloads["stt"]
        self.controller.results.hide()
        self.controller._discard_hidden_results()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        worker.send(
            "download.progress",
            kind="stt",
            phase="downloading",
            completed_bytes=40,
            total_bytes=100,
            eta_seconds=90,
            bytes_per_second=10,
        )
        row = dialog.model_rows["stt"]
        self.assertEqual(row.progress.value(), 400)
        self.assertIn("1 分", row.eta_label.text())
        worker.send(
            "download.progress",
            kind="stt",
            phase="switching",
            completed_bytes=0,
            eta_seconds=None,
            bytes_per_second=None,
        )
        self.assertNotIn("1 分", row.eta_label.text())
        worker.send("download.progress", kind="stt", phase="verifying")
        self.assertTrue(row.eta_label.isHidden())

    def test_reopening_settings_preserves_failed_download_and_retry(self):
        self.controller.download_model("stt")
        worker = self.controller._downloads["stt"]
        worker.send(
            "download.error", kind="stt", phase="failed", message="全部来源校验失败"
        )
        worker.finish(1)
        self.controller.settings_dialog.reject()
        self.controller.show_settings()
        row = self.controller.settings_dialog.model_rows["stt"]
        self.assertIn("全部来源校验失败", row.detail_label.text())
        self.assertTrue(row.download_button.isEnabled())
        row.download_button.click()
        self.assertIsNot(self.controller._downloads["stt"], worker)

    def test_source_switch_survives_reopening_and_cancel_blocks_late_progress(self):
        self.controller.download_model("stt")
        worker = self.controller._downloads["stt"]
        worker.send(
            "download.progress",
            kind="stt",
            phase="switching",
            source="HF Mirror",
            source_index=2,
            source_count=2,
            completed_bytes=10,
            total_bytes=100,
            message="连接超时，正在切换到备用来源",
        )
        self.controller.settings_dialog.close()
        self.controller.show_settings()
        card = self.controller.settings_dialog.model_rows["stt"]
        self.assertEqual(card.status_label.text(), "正在切换来源")
        self.assertIn("HF Mirror（2/2）", card.source_label.text())
        self.assertEqual(card.progress.value(), 100)
        self.assertTrue(card.cancel_button.isEnabled())
        card.cancel_button.click()
        worker.send(
            "download.progress", kind="stt", phase="downloading", completed_bytes=50
        )
        worker.finish()
        self.assertEqual(self.controller._model_states["stt"]["phase"], "cancelled")
        self.assertFalse(self.controller.pending_settings)
        card.download_button.click()
        self.assertEqual(self.controller._model_states["stt"]["source"], "")
        self.assertTrue(card.source_label.isHidden())

    def test_incompatible_runtime_selection_cannot_claim_completed(self):
        self.controller.download_model("tts")
        worker = self.controller._downloads["tts"]
        worker.send("download.completed", kind="tts", phase="ready")
        self.statuses["tts"] = "incompatible"
        worker.finish()
        self.assertEqual(self.controller._model_states["tts"]["phase"], "failed")
        self.assertFalse(self.controller.pending_settings)

    def test_cancel_blocks_late_success_and_keeps_retry_available(self):
        self.controller.download_model("stt")
        worker = self.controller._downloads["stt"]
        self.controller.settings_dialog.model_rows["stt"].cancel_button.click()
        self.assertTrue(worker.terminated)
        worker.send("download.completed", kind="stt", phase="ready")
        worker.finish()
        self.assertEqual(self.controller._model_states["stt"]["phase"], "cancelled")
        self.assertFalse(self.controller.pending_settings)
        self.assertTrue(
            self.controller.settings_dialog.model_rows[
                "stt"
            ].download_button.isEnabled()
        )
        self.controller.settings_dialog.model_rows["stt"].download_button.click()
        self.assertIsNot(self.controller._downloads["stt"], worker)

    def test_start_failure_has_visible_retry_and_independent_model_buttons(self):
        self.controller.download_model("stt")
        self.assertTrue(
            self.controller.settings_dialog.model_rows[
                "tts"
            ].download_button.isEnabled()
        )
        worker = self.controller._downloads["stt"]
        worker.errorOccurred.emit(QProcess.ProcessError.FailedToStart)
        self.assertNotIn("stt", self.controller._downloads)
        self.assertEqual(self.controller._model_states["stt"]["phase"], "failed")
        self.assertTrue(
            self.controller.settings_dialog.model_rows[
                "stt"
            ].download_button.isEnabled()
        )

    def test_audio_levels_only_update_live_microphone_without_task_refresh(self):
        self.controller.handle_audio_event("audio.ready", {"listening": True})
        self.controller.update_tile = Mock()
        self.controller.submit = Mock()
        self.controller.handle_audio_event(
            "audio.level", {"listening": True, "speech": True, "level": 0.75}
        )
        self.assertEqual(self.controller.tile.audio_level, 0.75)
        self.controller.update_tile.assert_not_called()
        self.controller.submit.assert_not_called()
        self.controller.user_paused = True
        self.controller.handle_audio_event(
            "audio.level", {"listening": True, "speech": True, "level": 0.9}
        )
        self.assertEqual(self.controller.tile.audio_level, 0)

        self.controller.user_paused = False
        self.controller.speaking = True
        self.controller.handle_audio_event(
            "audio.level", {"listening": True, "speech": True, "level": 0.9}
        )
        self.assertEqual(self.controller.tile.audio_level, 0)

    def test_download_warning_keeps_wave_but_microphone_failure_clears_it(self):
        self.controller.handle_audio_event("audio.ready", {"listening": True})
        self.controller.notify("中文播报模型下载失败", "download.tts")
        self.controller.handle_audio_event(
            "audio.level", {"listening": True, "speech": True, "level": 0.75}
        )
        self.assertEqual(self.controller.tile.audio_level, 0.75)
        self.controller.handle_audio_event(
            "audio.error", {"component": "microphone", "message": "输入设备断开"}
        )
        self.assertEqual(self.controller.tile.audio_level, 0)
