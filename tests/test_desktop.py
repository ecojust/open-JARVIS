"""Native shell protocol/lifecycle tests; no microphone or model required."""

import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QByteArray, QObject, QProcess, QUrl, Signal
    from PySide6.QtGui import QTextDocument
    from PySide6.QtWidgets import QApplication
    from jarvis.desktop_client import DesktopController, TaskClient
    from jarvis.desktop_ui import SafeTextBrowser

    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


if QT_AVAILABLE:

    class FakeProcess(QProcess):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.fake_state = QProcess.ProcessState.NotRunning
            self.commands = []
            self.launches = []

        def state(self):
            return self.fake_state

        def start(self, program, args):
            self.launches.append((program, args))
            self.fake_state = QProcess.ProcessState.Running
            self.started.emit()

        def write(self, data):
            self.commands.append(json.loads(bytes(data)))
            return len(data)

        def terminate(self):
            self.fake_state = QProcess.ProcessState.NotRunning

        kill = terminate

        def waitForFinished(self, timeout=30000):
            return self.fake_state == QProcess.ProcessState.NotRunning

        def waitForBytesWritten(self, timeout=30000):
            return True

    class FakeClient(QObject):
        event = Signal(object)
        connection = Signal(bool, str)

        def __init__(self, url="http://127.0.0.1:8765", token="test", parent=None):
            super().__init__(parent)
            self.calls = []
            self.closed = False
            self.after = None

        def request(self, method, path, payload=None, **kwargs):
            self.calls.append((method, path, payload, kwargs))

        def start_events(self, after=0):
            self.after = after

        def close(self):
            self.closed = True

    class FakeReply:
        def __init__(self):
            self.data = b""
            self.aborted = False

        def attribute(self, name):
            return 200

        def readAll(self):
            chunk, self.data = self.data, b""
            return QByteArray(chunk)

        def abort(self):
            self.aborted = True


@unittest.skipUnless(QT_AVAILABLE, "PySide6 is an optional desktop dependency")
class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.read = patch("jarvis.config.read_preferences", return_value={})
        self.save = patch("jarvis.config.save_preferences")
        self.read.start()
        self.save.start()
        self.controller = DesktopController(
            self.app,
            autostart=False,
            client_factory=FakeClient,
            process_factory=FakeProcess,
        )
        self.controller.client = FakeClient(parent=self.controller)
        self.controller.auto_fullscreen = False
        self.app.processEvents()

    def tearDown(self):
        self.controller.cleanup()
        self.controller.deleteLater()
        self.app.processEvents()
        self.read.stop()
        self.save.stop()

    def ready(self):
        self.controller.ready = True
        self.controller.configured = True
        self.controller.audio.fake_state = QProcess.ProcessState.Running

    def event(self, number, task_id, status="succeeded", speech="完成了"):
        kind = "task.awaiting_input" if status == "awaiting_input" else "task.completed"
        return {
            "event_id": number,
            "task_id": task_id,
            "type": kind,
            "data": {
                "status": status,
                "display_text": "# 结果 " + task_id,
                "speech_text": speech,
            },
        }

    def test_entry_import_does_not_load_qt_or_models(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import jarvis.desktop; assert 'PySide6' not in sys.modules; assert 'sherpa_onnx' not in sys.modules",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_quit_with_fullscreen_results_exits_first_time(self):
        code = """
import time
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from jarvis.desktop_client import DesktopController
app=QApplication([])
app.setQuitOnLastWindowClosed(False)
controller=DesktopController(app,autostart=False)
controller.show_results()
started=time.monotonic()
QTimer.singleShot(20,controller.request_quit)
QTimer.singleShot(1500,lambda: app.exit(42))
code=app.exec()
assert code==0, code
assert time.monotonic()-started<1, time.monotonic()-started
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            capture_output=True,
            text=True,
            timeout=4,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_explicit_false_environment_disables_speech_and_auto_fullscreen(self):
        with patch.dict(
            os.environ, {"UI_SPEECH_ENABLED": "false", "UI_AUTO_FULLSCREEN": "off"}
        ):
            other = DesktopController(
                self.app,
                autostart=False,
                client_factory=FakeClient,
                process_factory=FakeProcess,
            )
            try:
                self.assertFalse(other.speech_enabled)
                self.assertFalse(other.auto_fullscreen)
            finally:
                other.cleanup()
                other.deleteLater()

    def test_only_tile_is_visible_at_start_and_result_view_is_native_lazy(self):
        self.assertTrue(self.controller.tile.isVisible())
        self.assertFalse(self.controller.tile.logo.isNull())
        self.assertIsNone(self.controller.results)
        self.controller.show_results()
        self.app.processEvents()
        self.assertTrue(self.controller.results.isVisible())
        self.assertTrue(self.controller.results.isFullScreen())
        self.assertFalse(
            any(name.startswith("PySide6.QtWebEngine") for name in sys.modules)
        )
        self.controller.results.hide()
        self.app.processEvents()
        self.assertTrue(self.controller.tile.isVisible())
        self.assertFalse(self.controller.closing)
        self.controller._discard_hidden_results()
        self.assertIsNone(self.controller.results)

    def test_rejected_submission_keeps_native_editor_draft(self):
        self.controller.show_results()
        self.controller.results.input.setPlainText("保存一份报告")
        self.controller.results._submit()
        self.assertEqual(self.controller.results.input.toPlainText(), "保存一份报告")
        self.assertFalse(self.controller.client.calls)

    def test_question_answer_routes_to_same_task_and_new_task_resets_it(self):
        self.ready()
        self.controller.awaiting_task = "waiting-1"
        self.assertTrue(self.controller.submit("明天"))
        call = self.controller.client.calls[-1]
        self.assertEqual(
            call[:3], ("POST", "/api/tasks/waiting-1/input", {"message": "明天"})
        )
        self.controller.submit("新任务：分析销售数据")
        call = self.controller.client.calls[-1]
        self.assertEqual(call[1], "/api/tasks")
        self.assertEqual(call[2]["message"], "分析销售数据")
        self.assertTrue(call[2]["client_request_id"])
        self.assertIsNone(self.controller.awaiting_task)

    def test_notifications_keep_result_and_voice_bound_until_matching_completion(self):
        self.ready()
        self.controller.handle_task_event(self.event(1, "first"))
        first_id = self.controller._playback_id
        self.controller.handle_task_event(self.event(2, "second"))
        self.assertEqual(self.controller.selected_task, "first")
        self.assertEqual(len(self.controller.notifications), 1)
        self.controller.handle_audio_event("tts.finished", {"playback_id": "stale"})
        self.assertEqual(self.controller._playback_id, first_id)
        self.controller.handle_audio_event("tts.finished", {"playback_id": first_id})
        self.controller._drain_notifications()
        self.assertEqual(self.controller.selected_task, "second")
        self.assertNotEqual(self.controller._playback_id, first_id)
        speech_commands = [
            item
            for item in self.controller.audio.commands
            if item["command"] == "speak"
        ]
        self.assertEqual(
            [item["task_id"] for item in speech_commands], ["first", "second"]
        )

    def test_awaiting_question_arms_followup_and_stt_is_routed(self):
        self.ready()
        self.controller.speech_enabled = False
        self.controller.handle_task_event(
            self.event(1, "question", "awaiting_input", "哪一天？")
        )
        self.assertEqual(self.controller.awaiting_task, "question")
        self.assertTrue(
            any(
                item["command"] == "expect_input" and item.get("enabled")
                for item in self.controller.audio.commands
            )
        )
        self.controller.handle_audio_event("stt.final", {"text": "明天"})
        self.assertEqual(
            self.controller.client.calls[-1][1], "/api/tasks/question/input"
        )

    def test_stop_speaking_clears_notifications_without_cancelling_tasks_or_user_pause(
        self,
    ):
        self.ready()
        self.controller.handle_task_event(self.event(1, "first"))
        old_id = self.controller._playback_id
        self.controller.handle_task_event(self.event(2, "second"))
        self.controller.user_paused = True
        self.controller.stop_speaking()
        self.controller.handle_audio_event("tts.finished", {"playback_id": old_id})
        self.assertTrue(self.controller.user_paused)
        self.assertFalse(self.controller.speaking)
        self.assertFalse(self.controller.notifications)
        self.assertFalse(
            any("/cancel" in call[1] for call in self.controller.client.calls)
        )
        self.assertFalse(
            any(item["command"] == "resume" for item in self.controller.audio.commands)
        )

    def test_audio_failure_releases_matching_playback_but_preserves_task_result(self):
        self.ready()
        self.controller.handle_task_event(self.event(1, "first"))
        playback_id = self.controller._playback_id
        self.controller.handle_audio_event(
            "audio.error",
            {"component": "tts", "playback_id": playback_id, "message": "没有输出设备"},
        )
        self.assertFalse(self.controller.speaking)
        self.assertEqual(self.controller.tasks["first"]["status"], "succeeded")
        self.assertIn("没有输出设备", self.controller.tile.toolTip())

    def test_mismatched_backend_identity_is_not_attached(self):
        self.controller.instance_id = "our-instance"
        self.controller._health_ready(
            {"ready": True, "instance_id": "other-instance", "last_event_id": 80}
        )
        self.assertFalse(self.controller.ready)
        self.assertIsNone(self.controller.client.after)
        self.assertIn("身份不匹配", self.controller.errors["backend"])

    def test_generated_markdown_cannot_load_local_or_remote_images(self):
        browser = SafeTextBrowser()
        for source in ["file:///etc/passwd", "https://example.com/tracker.png"]:
            resource = browser.loadResource(
                QTextDocument.ResourceType.ImageResource, QUrl(source)
            )
            self.assertTrue(resource.isNull())
        browser.deleteLater()

    def test_same_report_preserves_scroll_and_does_not_reparse_markdown(self):
        self.controller.show_results()
        window = self.controller.results
        report = {
            "task_id": "same",
            "status": "succeeded",
            "display_text": "\n\n".join(f"段落 {i}" for i in range(100)),
            "artifacts": [{"artifact_id": "a", "path": "report.md"}],
        }
        window.render_task(report)
        self.app.processEvents()
        window.body.verticalScrollBar().setValue(100)
        with patch.object(
            window.body, "setMarkdown", wraps=window.body.setMarkdown
        ) as render:
            window.render_task(dict(report))
            self.assertEqual(render.call_count, 0)
        self.assertEqual(window.body.verticalScrollBar().value(), 100)

    def test_task_detail_cache_releases_large_nonselected_history(self):
        self.controller.selected_task = "current"
        self.controller.tasks = {
            "old": {
                "steps": [{"data": "x" * 100000}],
                "display_text": "x" * 100000,
                "messages": ["large"],
                "status": "succeeded",
            },
            "current": {"steps": [{"data": "needed"}], "display_text": "visible"},
        }
        self.controller._trim_task_details()
        self.assertNotIn("steps", self.controller.tasks["old"])
        self.assertNotIn("display_text", self.controller.tasks["old"])
        self.assertEqual(self.controller.tasks["current"]["display_text"], "visible")

    def test_sse_handles_split_utf8_crlf_and_replay_without_duplicate_events(self):
        client = TaskClient("http://127.0.0.1:8765", "secret-test")
        reply = FakeReply()
        client._sse = reply
        events = []
        client.event.connect(events.append)
        payload = {
            "event_id": 9,
            "task_id": "a",
            "type": "task.completed",
            "data": {"display_text": "中文报告"},
        }
        encoded = (
            "id: 9\r\nevent: task.completed\r\ndata: "
            + json.dumps(payload, ensure_ascii=False)
            + "\r\n\r\n"
        ).encode()
        # One byte at a time also splits Chinese characters and CRLF delimiters.
        for byte in encoded:
            reply.data = bytes([byte])
            client._consume(reply)
        reply.data = encoded
        client._consume(reply)
        self.assertEqual(events, [payload])
        self.assertEqual(client.last_event_id, 9)
        request = client._request("/api/tasks")
        self.assertEqual(
            bytes(request.rawHeader("Authorization")), b"Bearer secret-test"
        )
        self.assertNotIn("secret-test", request.url().toString())
        client._sse = None
        client.close()
        client.deleteLater()


if __name__ == "__main__":
    unittest.main()
