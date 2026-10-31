"""Download controls reflect real controller state without owning downloads."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from jarvis.model_manager_ui import ModelManagerDialog

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
        self.dialog = ModelManagerDialog()
        self.downloads = []
        self.cancellations = []
        self.dialog.download_requested.connect(self.downloads.append)
        self.dialog.cancel_requested.connect(self.cancellations.append)
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()

    def test_download_buttons_are_independent_and_duplicate_click_is_blocked(self):
        for kind in ("stt", "tts"):
            self.dialog.update_model(kind, {"phase": "missing"})
        stt, tts = self.dialog.cards["stt"], self.dialog.cards["tts"]
        QTest.mouseClick(stt.download_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(stt.download_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.downloads, ["stt"])
        self.assertTrue(tts.download_button.isEnabled())
        QTest.mouseClick(tts.download_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.downloads, ["stt", "tts"])

    def test_progress_uses_bytes_then_switches_to_verification_and_ready(self):
        card = self.dialog.cards["stt"]
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
        self.assertEqual(card.download_button.text(), "校验模型")
        self.assertTrue(card.download_button.isEnabled())
        self.assertTrue(card.cancel_button.isHidden())
        self.assertEqual(
            card.path_field.text(), "/user/models/sensevoice/test-revision"
        )
        self.assertIn("sensevoice-test-revision", card.version_label.text())
        QTest.mouseClick(card.download_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.downloads, ["stt"])

    def test_cancellation_and_retry_are_explicit_and_do_not_affect_other_model(self):
        self.dialog.update_model("stt", {"phase": "downloading"})
        self.dialog.update_model("tts", {"phase": "ready"})
        card = self.dialog.cards["stt"]
        QTest.mouseClick(card.cancel_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(card.cancel_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.cancellations, ["stt"])
        self.dialog.update_model("stt", {"phase": "cancelling"})
        self.assertFalse(card.cancel_button.isEnabled())
        self.assertFalse(card.download_button.isEnabled())
        self.dialog.update_model("stt", {"phase": "cancelled"})
        self.assertEqual(card.download_button.text(), "重试下载")
        QTest.mouseClick(card.download_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.downloads, ["stt"])
        self.assertEqual(self.dialog.cards["tts"].status_label.text(), "已就绪")

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
        self.assertEqual(self.dialog.cards["tts"].progress.value(), 400)

    def test_incompatibility_failure_and_untrusted_messages_are_visible_plain_text(
        self,
    ):
        card = self.dialog.cards["stt"]
        self.dialog.update_model("stt", {"phase": "incompatible"})
        self.assertEqual(card.download_button.text(), "下载兼容版本")
        self.assertIn("旧文件会保留", card.detail_label.text())
        self.dialog.update_model(
            "stt", {"phase": "failed", "message": "<a href='file:///secret'>失败</a>"}
        )
        self.assertEqual(card.detail_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn("<a ", card.detail_label.text())
        self.assertEqual(card.download_button.text(), "重试下载")
        self.assertTrue(card.download_button.isEnabled())

    def test_progress_is_bounded_and_unknown_phase_does_not_claim_readiness(self):
        card = self.dialog.cards["tts"]
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


if __name__ == "__main__":
    unittest.main()
