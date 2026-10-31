"""Native session and speech composition behavior, with actual Qt widgets."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QProcess, Qt
    from PySide6.QtWidgets import QApplication, QDialogButtonBox
    from jarvis.desktop_client import DesktopController
    from test_desktop import FakeClient, FakeProcess

    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


@unittest.skipUnless(QT_AVAILABLE, "PySide6 is an optional desktop dependency")
class SessionUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        with patch("jarvis.config.read_preferences", return_value={}):
            self.c = DesktopController(
                self.app, autostart=False, process_factory=FakeProcess
            )
        self.c.client = FakeClient(parent=self.c)
        self.c.ready = True
        self.c.configured = True
        self.c.active_session_id = "session-a"
        self.c.auto_fullscreen = False
        self.c.audio.fake_state = QProcess.ProcessState.Running

    def tearDown(self):
        if self.c.tools_dialog:
            self.c.tools_dialog.close()
        self.c.cleanup()
        self.c.deleteLater()
        self.app.processEvents()

    def speech(self, kind, text="", identifier="utterance-1"):
        self.c.handle_audio_event(
            "stt." + kind,
            {
                "text": text,
                "utterance_id": identifier,
                "generation": 1,
                "final": kind == "final",
            },
        )

    def posts(self):
        return [call for call in self.c.client.calls if call[0] == "POST"]

    def test_wake_wait_is_visible_and_explains_ignored_speech(self):
        window = self.c._ensure_results()
        window.show()
        self.c.handle_audio_event(
            "audio.ready",
            {
                "listening": True,
                "wake_required": True,
                "wake_word": "贾维斯",
            },
        )
        self.assertEqual(self.c.tile.caption, "等待唤醒")
        self.assertIn("先说“贾维斯”", window.input.placeholderText())
        self.assertIn("语音需先唤醒", window.input_hint.text())
        self.c.handle_audio_event(
            "stt.cancelled",
            {
                "utterance_id": "not-awake",
                "generation": 1,
                "reason": "not_addressed",
            },
        )
        self.assertIn("还未唤醒", self.c.last_status)
        self.assertIn("请先说", self.c.last_status)
        self.assertFalse(self.posts())
        self.c.handle_audio_event(
            "audio.status",
            {
                "listening": True,
                "wake_required": False,
                "wake_word": "贾维斯",
                "message": "正在聆听，说完后会自动发送。",
            },
        )
        self.assertEqual(self.c.tile.caption, "正在聆听")
        self.assertNotIn("先说", window.input.placeholderText())
        self.speech("final", "继续安排任务", "after-waking")
        self.assertEqual(self.posts()[-1][2]["message"], "继续安排任务")

    def test_partial_in_input_then_final_auto_sends_once_with_same_session(self):
        window = self.c._ensure_results()
        window.input.setPlainText("尚未发送的手写草稿")
        self.speech("started")
        self.speech("partial", "帮我整理")
        self.assertEqual(window.input.toPlainText(), "尚未发送的手写草稿\n帮我整理")
        self.assertFalse(self.posts())
        self.speech("partial", "帮我整理金融资料")
        self.speech("final", "帮我整理金融资料。")
        self.speech("final", "帮我整理金融资料。")
        self.speech("partial", "过期内容")
        self.assertEqual(len(self.posts()), 1)
        call = self.posts()[0]
        self.assertEqual(call[2]["message"], "帮我整理金融资料。")
        self.assertEqual(call[2]["session_id"], "session-a")
        self.assertIn("帮我整理金融资料。", window.input.toPlainText())
        call[3]["callback"](
            {"task_id": "run-1", "session_id": "session-a", "status": "queued"}
        )
        self.assertEqual(window.input.toPlainText(), "尚未发送的手写草稿")
        self.assertEqual(self.c.selected_task, "run-1")
        self.assertEqual(self.c.active_session_id, "session-a")

    def test_manual_edits_survive_preview_and_failed_voice_send_is_recoverable(self):
        self.speech("partial", "帮我整理")
        window = self.c.results
        window.input.setPlainText("用户正在修改自己的文字")
        self.speech("partial", "帮我整理财务数据")
        self.assertEqual(window.input.toPlainText(), "用户正在修改自己的文字")
        self.speech("final", "帮我整理财务数据。")
        self.posts()[0][3]["error"]("本地服务暂时不可用")
        self.assertEqual(
            window.input.toPlainText(), "用户正在修改自己的文字\n帮我整理财务数据。"
        )
        self.assertIn("文字已保留", window.input_hint.text())

    def test_native_editor_does_not_clear_until_server_accepts(self):
        window = self.c._ensure_results()
        window.input.setPlainText("写报告")
        window._submit()
        window._submit()
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(window.input.toPlainText(), "写报告")
        first_id = self.posts()[0][2]["client_request_id"]
        self.posts()[0][3]["error"]("队列已满")
        self.assertEqual(window.input.toPlainText(), "写报告")
        window._submit()
        self.assertEqual(self.posts()[-1][2]["client_request_id"], first_id)
        self.posts()[-1][3]["callback"]({"task_id": "run-1", "session_id": "session-a"})
        self.assertEqual(window.input.toPlainText(), "")

    def test_explicit_session_switch_discards_old_audio_and_old_list_callback(self):
        window = self.c._ensure_results()
        window.input.setPlainText("保留在旧会话里的草稿")
        self.c.refresh_tasks()
        late_list = self.c.client.calls[-1][3]["callback"]
        self.speech("started", identifier="old-speech")
        self.c.select_session("session-b")
        activate = self.posts()[-1]
        self.assertEqual(activate[1], "/api/sessions/session-b/activate")
        self.assertFalse(window.input.isEnabled())
        activate[3]["callback"]({"session_id": "session-b", "title": "另一对话"})
        self.speech("final", "迟到的旧语音", "old-speech")
        late_list({"tasks": [{"task_id": "old-run", "session_id": "session-a"}]})
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.c.active_session_id, "session-b")
        self.assertEqual(self.c.task_order, [])
        self.assertEqual(window.input.toPlainText(), "")
        self.assertIsNone(self.c.selected_task)
        self.assertIn(
            "discard_input", [item["command"] for item in self.c.audio.commands]
        )
        self.c._activate_session_local("session-a")
        self.assertEqual(window.input.toPlainText(), "保留在旧会话里的草稿")

    def test_only_exact_new_conversation_command_creates_session(self):
        self.speech("final", "帮我写一篇新建对话教程", "normal")
        self.assertEqual(self.posts()[-1][1], "/api/tasks")
        self.speech("final", "新建对话。", "new")
        self.assertEqual(self.posts()[-1][1], "/api/sessions")
        self.assertEqual(self.c.active_session_id, "session-a")
        self.posts()[-1][3]["callback"](
            {"session_id": "session-new", "title": "新对话"}
        )
        self.assertEqual(self.c.active_session_id, "session-new")
        self.assertFalse(
            any(
                call[1] == "/api/tasks" and call[2]["message"] == "新建对话。"
                for call in self.posts()
            )
        )

    def test_background_result_from_another_session_still_speaks_without_switching(
        self,
    ):
        window = self.c._ensure_results()
        window.show()
        self.c.selected_task = "current-run"
        window.task_id = "current-run"
        self.c._queue_notification(
            {
                "task_id": "other-run",
                "session_id": "other-session",
                "status": "awaiting_input",
                "display_text": "哪一天？",
                "speech_text": "哪一天？",
            }
        )
        self.assertEqual(self.c.active_session_id, "session-a")
        self.assertEqual(self.c.selected_task, "current-run")
        self.assertEqual(window.task_id, "current-run")
        self.assertIsNone(self.c._voice_awaiting_task)
        speech = [
            command
            for command in self.c.audio.commands
            if command["command"] == "speak"
        ]
        self.assertEqual(speech[-1]["task_id"], "other-run")
        self.assertFalse(speech[-1]["awaiting_input"])

    def test_continued_run_is_not_a_pending_question_or_cross_session_parent(self):
        window = self.c._ensure_results()
        task = {
            "task_id": "question",
            "session_id": "session-a",
            "message": "排班",
            "status": "awaiting_input",
            "continued_by_run_id": "answer",
        }
        self.c.tasks["question"] = task
        self.c.select_task("question")
        self.assertIsNone(self.c.awaiting_task)
        self.assertEqual(window.task_state.text(), "已补充")
        self.assertFalse(window.cancel_button.isVisible())
        self.c.tasks["other"] = {
            "task_id": "other",
            "session_id": "session-b",
            "status": "succeeded",
        }
        self.c.selected_task = "other"
        self.c.submit("继续")
        self.assertNotIn("parent_task_id", self.posts()[-1][2])
        self.assertEqual(self.posts()[-1][2]["session_id"], "session-a")

    def test_tools_icon_loads_catalog_on_demand_and_filters_plain_text_details(self):
        window = self.c._ensure_results()
        self.assertFalse(any(call[1] == "/api/tools" for call in self.c.client.calls))
        window.tools_button.click()
        request = self.c.client.calls[-1]
        self.assertEqual(request[:2], ("GET", "/api/tools"))
        request[3]["callback"](
            {
                "tools": [
                    {
                        "name": "fetch_page",
                        "title": "读取网页",
                        "category": "资料检索",
                        "agents": ["检索 Agent"],
                        "description": "读取公开网页正文和来源链接。",
                        "effect": "只读网络",
                        "parameters": {
                            "properties": {"url": {"description": "公开网址"}},
                            "required": ["url"],
                        },
                    },
                    {
                        "name": "write_file",
                        "title": "写入文件",
                        "category": "文件",
                        "agents": ["文档 Agent"],
                    },
                ]
            }
        )
        dialog = self.c.tools_dialog
        self.assertEqual(dialog.list.count(), 2)
        dialog.search.setText("读取网页")
        self.assertEqual(dialog.list.count(), 1)
        self.assertIn("url（必填）：公开网址", dialog.details.toPlainText())
        self.assertFalse(window.tools_button.icon().isNull())
        close = dialog.findChild(QDialogButtonBox).button(
            QDialogButtonBox.StandardButton.Close
        )
        self.assertEqual(close.text(), "关闭")
        self.assertEqual(len(self.posts()), 0)

    def test_late_run_details_select_the_matching_sidebar_row_without_refetch(self):
        window = self.c._ensure_results()
        first = {
            "task_id": "first",
            "session_id": "session-a",
            "status": "succeeded",
            "message": "第一轮",
        }
        second = {
            "task_id": "second",
            "session_id": "session-a",
            "status": "succeeded",
            "message": "第二轮",
        }
        window.render_tasks([second, first])
        calls = len(self.c.client.calls)
        window.render_task(second)
        self.assertEqual(
            window.tasks.currentItem().data(Qt.ItemDataRole.UserRole), "second"
        )
        window.render_task(first)
        self.assertEqual(
            window.tasks.currentItem().data(Qt.ItemDataRole.UserRole), "first"
        )
        self.assertEqual(len(self.c.client.calls), calls)

    def test_restoring_history_keeps_startup_tile_only(self):
        self.c._tasks_ready(
            {
                "tasks": [
                    {
                        "task_id": "run-history",
                        "session_id": "session-a",
                        "status": "succeeded",
                    }
                ]
            }
        )
        self.assertIsNone(self.c.results)
        self.assertEqual(self.c.selected_task, "run-history")

    def test_discard_ack_rejects_unseen_old_started_and_outdated_generation(self):
        self.c._activate_session_local("session-b")
        reset_id = self.c._voice_reset_id
        self.speech("started", identifier="old-unseen")
        self.speech("final", "不能发到新会话", "old-unseen")
        self.assertFalse(self.posts())
        self.c.handle_audio_event(
            "stt.discarded", {"id": "outdated-ack", "generation": 3}
        )
        self.assertEqual(self.c._voice_reset_id, reset_id)
        self.c.handle_audio_event("stt.discarded", {"id": reset_id, "generation": 3})
        self.speech("started", identifier="old-after-ack")
        self.speech("final", "旧一代音频仍不可发送", "old-after-ack")
        self.assertFalse(self.posts())
        self.c.handle_audio_event(
            "stt.final",
            {
                "utterance_id": "new-audio",
                "generation": 3,
                "text": "新的语音",
                "final": True,
            },
        )
        self.assertEqual(self.posts()[-1][2]["session_id"], "session-b")

    def test_new_session_failure_keeps_spoken_command_and_does_not_reset_session(self):
        self.speech("final", "新建对话", "new-command")
        self.assertEqual(self.c.results.input.toPlainText(), "新建对话")
        self.posts()[-1][3]["error"]("创建失败")
        self.assertEqual(self.c.active_session_id, "session-a")
        self.assertEqual(self.c.results.input.toPlainText(), "新建对话")

    def test_queued_old_question_cannot_rearm_after_user_has_answered(self):
        self.c.speaking = True
        old = {
            "task_id": "question",
            "session_id": "session-a",
            "status": "awaiting_input",
            "speech_text": "哪一天？",
        }
        self.c._queue_notification(old)
        self.c.handle_task_event(
            {
                "task_id": "question",
                "session_id": "session-a",
                "type": "task.continued",
                "data": {"continued_by_run_id": "new-run"},
            }
        )
        self.c.speaking = False
        self.c._drain_notifications()
        self.assertIsNone(self.c._voice_awaiting_task)
        speaks = [item for item in self.c.audio.commands if item["command"] == "speak"]
        self.assertFalse(speaks[-1]["awaiting_input"])
