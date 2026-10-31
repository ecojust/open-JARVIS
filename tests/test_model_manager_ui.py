"""Download controls reflect real controller state without owning downloads."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QPushButton

    from jarvis.desktop_ui import SettingsDialog

    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


@unittest.skipUnless(QT_AVAILABLE, "PySide6 is an optional desktop dependency")
class ModelManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.dialog = SettingsDialog({})
        self.downloads = []
        self.cancellations = []
        self.dialog.model_download_requested.connect(self.downloads.append)
        self.dialog.model_cancel_requested.connect(self.cancellations.append)
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()

    def test_download_buttons_are_independent_and_duplicate_click_is_blocked(self):
        for kind in ("stt", "tts"):
            self.dialog.update_model(kind, {"phase": "missing"})
        stt, tts = self.dialog.model_rows["stt"], self.dialog.model_rows["tts"]
        QTest.mouseClick(stt.download_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(stt.download_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.downloads, ["stt"])
        self.assertTrue(tts.download_button.isEnabled())
        QTest.mouseClick(tts.download_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.downloads, ["stt", "tts"])

    def test_progress_uses_bytes_then_switches_to_verification_and_ready(self):
        card = self.dialog.model_rows["stt"]
        self.dialog.update_model(
            "stt",
            {
                "phase": "downloading",
                "completed_bytes": 25_000,
                "total_bytes": 100_000,
                "version": "sensevoice-test-revision",
                "directory": "/user/models/sensevoice/test-revision",
                "message": "下载中的测试进度",
            },
        )
        self.assertEqual(card.progress.value(), 250)
        self.assertFalse(card.download_button.isEnabled())
        self.assertTrue(card.cancel_button.isEnabled())
        self.dialog.update_model("stt", {"phase": "verifying"})
        self.assertEqual((card.progress.minimum(), card.progress.maximum()), (0, 0))
        self.assertNotIn("下载中的测试进度", card.detail_label.text())
        self.assertIn("校验", card.detail_label.text())
        self.dialog.update_model("stt", {"phase": "ready"})
        self.assertEqual(card.progress.value(), 1000)
        self.assertEqual(card.download_button.text(), "校验")
        self.assertTrue(card.download_button.isEnabled())
        self.assertTrue(card.cancel_button.isHidden())
        self.assertNotIn("/user/models", card.model_field.text())
        self.assertTrue(card.model_field.isReadOnly())
        self.assertIn("sensevoice-test-revision", card.version_label.text())
        QTest.mouseClick(card.download_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.downloads, ["stt"])

    def test_cancellation_and_retry_are_explicit_and_do_not_affect_other_model(self):
        self.dialog.update_model("stt", {"phase": "downloading"})
        self.dialog.update_model("tts", {"phase": "ready"})
        card = self.dialog.model_rows["stt"]
        QTest.mouseClick(card.cancel_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(card.cancel_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.cancellations, ["stt"])
        self.dialog.update_model("stt", {"phase": "cancelling"})
        self.assertFalse(card.cancel_button.isEnabled())
        self.assertFalse(card.download_button.isEnabled())
        self.dialog.update_model("stt", {"phase": "cancelled"})
        self.assertEqual(card.download_button.text(), "重试")
        QTest.mouseClick(card.download_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.downloads, ["stt"])
        self.assertEqual(self.dialog.model_rows["tts"].status_label.text(), "已就绪")

    def test_source_switch_resets_partial_progress_and_remains_cancellable(self):
        card = self.dialog.model_rows["stt"]
        self.dialog.update_model(
            "stt",
            {
                "phase": "downloading",
                "source": "Hugging Face",
                "source_index": 1,
                "source_count": 2,
                "completed_bytes": 80,
                "total_bytes": 100,
            },
        )
        self.assertEqual(card.progress.value(), 800)
        self.assertEqual(card.source_label.text(), "下载来源：Hugging Face（1/2）")
        self.dialog.update_model(
            "stt",
            {
                "phase": "switching",
                "source": "HF Mirror",
                "source_index": 2,
                "source_count": 2,
                "completed_bytes": 10,
            },
        )
        self.assertEqual(card.status_label.text(), "正在切换来源")
        self.assertEqual(card.progress.value(), 100)
        self.assertEqual(card.source_label.text(), "下载来源：HF Mirror（2/2）")
        self.assertFalse(card.download_button.isEnabled())
        self.assertTrue(card.cancel_button.isEnabled())
        QTest.mouseClick(card.cancel_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.cancellations, ["stt"])
        self.dialog.update_model("stt", {"phase": "cancelled"})
        self.assertTrue(card.source_label.isHidden())
        self.dialog.update_model("stt", {"phase": "checking"})
        self.assertTrue(card.source_label.isHidden())

    def test_closing_and_reopening_does_not_cancel_and_preserves_progress(self):
        self.dialog.update_model(
            "tts",
            {"phase": "downloading", "completed_bytes": 400, "total_bytes": 1000},
        )
        self.dialog.close()
        self.app.processEvents()
        self.assertEqual(self.cancellations, [])
        self.assertEqual(self.downloads, [])
        self.dialog.show()
        self.app.processEvents()
        self.assertEqual(self.dialog.model_rows["tts"].progress.value(), 400)

    def test_incompatibility_failure_and_untrusted_messages_are_visible_plain_text(
        self,
    ):
        card = self.dialog.model_rows["stt"]
        self.dialog.update_model("stt", {"phase": "incompatible"})
        self.assertEqual(card.download_button.text(), "下载")
        self.assertIn("旧文件会保留", card.detail_label.text())
        self.dialog.update_model(
            "stt", {"phase": "failed", "message": "<a href='file:///secret'>失败</a>"}
        )
        self.assertEqual(card.detail_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn("<a ", card.detail_label.text())
        self.assertEqual(card.download_button.text(), "重试")
        self.assertTrue(card.download_button.isEnabled())

    def test_progress_is_bounded_and_unknown_phase_does_not_claim_readiness(self):
        card = self.dialog.model_rows["tts"]
        for completed, total, expected in ((-1, 100, 0), (200, 100, 1000)):
            self.dialog.update_model(
                "tts",
                {
                    "phase": "downloading",
                    "completed_bytes": completed,
                    "total_bytes": total,
                },
            )
            self.assertEqual(card.progress.value(), expected)
        self.dialog.update_model("tts", {"phase": "unexpected"})
        self.assertEqual(card.status_label.text(), "状态待确认")
        self.assertFalse(card.download_button.isEnabled())

    def test_inline_download_has_no_directory_import_or_extra_dialog(self):
        self.assertEqual(set(self.dialog.model_rows), {"stt", "tts"})
        self.assertNotIn("JARVIS_STT_MODEL_DIR", self.dialog.fields)
        self.assertNotIn("JARVIS_TTS_MODEL_DIR", self.dialog.fields)
        self.assertFalse(self.dialog.findChildren(QDialog))
        self.assertFalse(hasattr(self.dialog, "models_button"))
        self.assertFalse(hasattr(self.dialog, "_choose_directory"))
        self.assertNotIn(
            "选择", [b.text() for b in self.dialog.findChildren(QPushButton)]
        )
        for row in self.dialog.model_rows.values():
            self.assertTrue(row.model_field.isReadOnly())
            self.assertFalse(row.model_field.acceptDrops())
            self.assertFalse(row.download_button.autoDefault())
            self.assertFalse(row.cancel_button.autoDefault())
            original = row.model_field.text()
            row.model_field.setFocus()
            QTest.keyClicks(row.model_field, "/tmp/import-model")
            self.assertEqual(row.model_field.text(), original)
        self.dialog.fields["OPENAI_MODEL"].setText("unsaved-model")
        self.dialog.update_model("stt", {"phase": "missing"})
        self.dialog.model_rows["stt"].download_button.click()
        self.assertEqual(self.dialog.fields["OPENAI_MODEL"].text(), "unsaved-model")
        self.assertTrue(self.dialog.isVisible())
        self.assertEqual(self.downloads, ["stt"])

    def test_eta_is_inline_estimated_and_cleared_for_switching_or_verification(self):
        row = self.dialog.model_rows["stt"]
        self.dialog.update_model("stt", {"phase": "downloading", "eta_seconds": None})
        self.assertEqual(row.eta_label.text(), "正在估算剩余时间…")
        self.dialog.update_model("stt", {"eta_seconds": 90})
        self.assertEqual(row.eta_label.text(), "预计剩余 1 分 30 秒")
        self.dialog.update_model("stt", {"phase": "switching"})
        self.assertEqual(row.eta_label.text(), "正在估算剩余时间…")
        self.dialog.update_model("stt", {"phase": "verifying"})
        self.assertTrue(row.eta_label.isHidden())
        self.dialog.update_model(
            "stt", {"phase": "downloading", "eta_seconds": float("nan")}
        )
        self.assertEqual(row.eta_label.text(), "正在估算剩余时间…")
        self.dialog.update_model("stt", {"phase": "ready"})
        self.assertTrue(row.eta_label.isHidden())

    def test_legacy_paths_are_preserved_but_not_offered_as_editable_fields(self):
        preferences = {
            "JARVIS_STT_MODEL_DIR": "/existing/legacy/stt",
            "JARVIS_TTS_MODEL_DIR": "/existing/legacy/tts",
        }
        dialog = SettingsDialog(preferences)
        try:
            values = dialog.values()
            for key, value in preferences.items():
                self.assertEqual(values[key], value)
                self.assertNotIn(key, dialog.fields)
                self.assertFalse(
                    any(
                        field.text() == value
                        for field in dialog.findChildren(QLineEdit)
                    )
                )
        finally:
            dialog.deleteLater()

    def test_small_settings_window_keeps_form_scrollable(self):
        self.dialog.resize(650, 520)
        for kind in ("stt", "tts"):
            self.dialog.update_model(
                kind,
                {
                    "phase": "downloading",
                    "source": "HF Mirror",
                    "eta_seconds": 90,
                    "message": "正在下载模型",
                },
            )
        self.app.processEvents()
        self.assertLessEqual(self.dialog.height(), 520)
        self.assertTrue(self.dialog.scroll.widgetResizable())
        self.assertGreater(self.dialog.scroll.verticalScrollBar().maximum(), 0)

    def test_ready_rows_compact_after_transfer_details_disappear(self):
        self.dialog.update_model("stt", {"phase": "ready"})
        self.dialog.update_model(
            "tts",
            {
                "phase": "downloading",
                "source": "HF Mirror",
                "eta_seconds": 90,
                "message": "正在下载中文播报模型",
            },
        )
        self.app.processEvents()
        self.dialog.update_model("tts", {"phase": "ready"})
        self.app.processEvents()
        # The row's layout request queues its enclosing form's relayout.
        self.app.processEvents()
        rows = list(self.dialog.model_rows.values())
        self.assertLessEqual(abs(rows[0].height() - rows[1].height()), 4)
        for row in rows:
            gap = row.status_label.y() - row.model_field.geometry().bottom()
            self.assertGreaterEqual(gap, 0)
            self.assertLessEqual(gap, 12)

    def test_fallback_transfer_size_overrides_default_archive_size_in_metadata(self):
        row = self.dialog.model_rows["tts"]
        self.dialog.update_model(
            "tts",
            {
                "phase": "downloading",
                "size_bytes": 100 * 1024**2,
                "total_bytes": 200 * 1024**2,
            },
        )
        self.assertIn("200.0 MiB", row.version_label.text())
        self.assertNotIn("100.0 MiB", row.version_label.text())
        self.dialog.update_model(
            "tts", {"phase": "switching", "total_bytes": 250 * 1024**2}
        )
        self.assertIn("250.0 MiB", row.version_label.text())


if __name__ == "__main__":
    unittest.main()
