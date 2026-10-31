"""Per-run tool records: durable identities, bounded pages and lazy inspection."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QPlainTextEdit, QTableView
    from jarvis.desktop_client import DesktopController
    from test_desktop import FakeClient, FakeProcess

    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


@unittest.skipUnless(QT_AVAILABLE, "PySide6 is an optional desktop dependency")
class RunToolUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        with patch("jarvis.config.read_preferences", return_value={}):
            self.controller = DesktopController(
                self.app, autostart=False, process_factory=FakeProcess
            )
        self.controller.client = FakeClient(parent=self.controller)
        self.controller.ready = True
        self.controller.active_session_id = "session-a"
        self.window = self.controller._ensure_results()
        self.panel = self.window.tool_calls

    def tearDown(self):
        self.controller.cleanup()
        self.controller.deleteLater()
        self.app.processEvents()

    @staticmethod
    def record(identifier, run="run-a", name="fetch_page", status="succeeded", **extra):
        return {
            "step_id": identifier,
            "task_id": run,
            "name": name,
            "status": status,
            "created_at": "2026-10-31T10:00:00.000Z",
            "updated_at": "2026-10-31T10:00:01.000Z",
            **extra,
        }

    def task(self, identifier="run-a", records=None, total=None):
        records = records or []
        return {
            "task_id": identifier,
            "session_id": "session-a",
            "status": "running",
            "message": "检索后生成报告",
            "display_text": "仅主动查看才展示的 Agent 回答",
            "steps": records,
            "steps_total": len(records) if total is None else total,
        }

    def event(
        self, sequence, step, kind="tool.started", run="run-a", name="fetch_page"
    ):
        return {
            "event_id": sequence,
            "task_id": run,
            "step_id": step,
            "type": kind,
            "timestamp": f"2026-10-31T10:00:{sequence + 10:02}.000Z",
            "data": {
                "name": name,
                "arguments": {"url": "https://example.org"},
                "result": {"content": "真实来源摘要"},
            },
        }

    def test_run_summary_does_not_expand_reply_or_inspect_arguments(self):
        records = [
            self.record("delegate", name="delegate_research"),
            self.record(
                "web",
                arguments={"url": "https://example.org"},
                result={"content": "large" * 50000},
            ),
        ]
        self.window.render_task(self.task(records=records))
        self.assertIn("工具调用与委派 · 2 项", self.panel.toggle.text())
        self.assertTrue(self.panel.panel.isHidden())
        self.assertFalse(self.window.reply_visible)
        self.assertEqual(self.window.body.toPlainText(), "")
        self.assertEqual(self.panel.details.toPlainText(), "")
        self.assertEqual(
            self.panel.model.data(self.panel.model.index(0, 1)), "助手委派"
        )
        self.assertNotIn("arguments", self.panel.model.rows[1])
        self.assertNotIn("result", self.panel.model.rows[1])
        self.assertFalse(self.controller.client.calls)

    def test_nested_durable_events_pair_by_step_and_do_not_duplicate_raw_agent_events(
        self,
    ):
        self.window.render_task(self.task())
        events = [
            self.event(1, "delegate", name="delegate_research"),
            self.event(2, "web"),
            self.event(3, "web", "tool.failed"),
            self.event(4, "delegate", "tool.completed", name="delegate_research"),
        ]
        for event in events:
            self.window.append_event(event)
            self.window.append_event(event)
        self.window.append_event(self.event(5, "raw", "agent.tool_start"))
        self.window.append_event(self.event(6, "other", run="run-b"))
        self.assertEqual(self.panel.total, 2)
        states = {row["step_id"]: row["status"] for row in self.panel.model.rows}
        self.assertEqual(states, {"delegate": "succeeded", "web": "failed"})
        self.assertEqual(self.panel.details.toPlainText(), "")

    def test_lazy_pages_and_detail_callbacks_are_scoped_to_run_and_request(self):
        self.window.render_task(self.task(records=[self.record("first")], total=150))
        self.panel.toggle.click()
        first_page = self.controller.client.calls[-1]
        self.assertEqual(first_page[1], "/api/tasks/run-a/steps?offset=0&limit=100")
        first_page[3]["callback"](
            {
                "task_id": "run-a",
                "offset": 0,
                "total": 150,
                "steps": [self.record("first")],
            }
        )
        self.assertEqual(self.panel.details.toPlainText(), "")
        self.panel.table.setCurrentIndex(self.panel.model.index(0, 0))
        detail = self.controller.client.calls[-1]
        self.assertEqual(detail[1], "/api/tasks/run-a/steps/first")
        detail[3]["callback"](
            self.record(
                "first",
                arguments={"url": "https://example.org"},
                result={"content": "网页资料"},
            )
        )
        self.assertIn("网页资料", self.panel.details.toPlainText())
        self.panel.next.click()
        next_page = self.controller.client.calls[-1]
        self.assertIn("offset=100", next_page[1])
        first_page[3]["callback"](
            {
                "task_id": "run-a",
                "offset": 0,
                "total": 150,
                "steps": [self.record("stale")],
            }
        )
        next_page[3]["callback"](
            {
                "task_id": "run-a",
                "offset": 100,
                "total": 150,
                "steps": [self.record("last")],
            }
        )
        self.assertEqual(self.panel.model.rows[0]["step_id"], "last")
        self.window.render_task(
            self.task(
                "run-b", [self.record("different", run="run-b", name="write_file")]
            )
        )
        detail[3]["callback"](
            self.record("first", result={"content": "旧run不能进入新run"})
        )
        next_page[3]["callback"](
            {
                "task_id": "run-a",
                "offset": 100,
                "total": 150,
                "steps": [self.record("last")],
            }
        )
        self.assertEqual(self.panel.run_id, "run-b")
        self.assertEqual(self.panel.model.rows[0]["name"], "write_file")
        self.assertEqual(self.panel.details.toPlainText(), "")
        self.assertTrue(self.panel.panel.isHidden())

    def test_thousands_of_calls_keep_one_small_model_and_one_inspector(self):
        self.window.render_task(
            self.task(
                records=[self.record(f"s-{index}") for index in range(1200)], total=1200
            )
        )
        self.assertEqual(self.panel.model.rowCount(), 100)
        self.assertEqual(len(self.panel.findChildren(QTableView)), 1)
        self.assertEqual(len(self.panel.findChildren(QPlainTextEdit)), 1)
        for index in range(1200):
            self.panel.append_event(self.event(index + 1, f"new-{index}"))
        self.assertEqual(self.panel.model.rowCount(), 100)
        self.assertLessEqual(len(self.panel._live), 100)
        self.assertLessEqual(len(self.panel._event_ids), 300)
        self.assertFalse(self.controller.client.calls)
        self.assertEqual(self.panel.details.toPlainText(), "")

    def test_selected_tool_details_are_bounded_plain_text_and_old_response_is_rejected(
        self,
    ):
        self.window.render_task(
            self.task(records=[self.record("first", status="running")])
        )
        self.panel.toggle.click()
        self.controller.client.calls[-1][3]["callback"](
            {
                "task_id": "run-a",
                "offset": 0,
                "total": 1,
                "steps": [self.record("first", status="running")],
            }
        )
        self.panel.table.setCurrentIndex(self.panel.model.index(0, 0))
        old_request = self.controller.client.calls[-1]
        self.window.append_event(self.event(4, "first", "tool.completed"))
        current_request = self.controller.client.calls[-1]
        old_request[3]["callback"](
            self.record("first", status="running", result={"content": "过期"})
        )
        self.assertNotIn("过期", self.panel.details.toPlainText())
        current_request[3]["callback"](
            self.record(
                "first", result={"content": "<script>不会执行</script>" + "文" * 100000}
            )
        )
        self.assertIn("<script>不会执行</script>", self.panel.details.toPlainText())
        self.assertIn("摘要已截断", self.panel.details.toPlainText())
        self.assertLess(len(self.panel.details.toPlainText()), 13000)
        self.assertEqual(
            self.panel.model.data(
                self.panel.model.index(0, 2), Qt.ItemDataRole.DisplayRole
            ),
            "成功",
        )

    def test_cached_old_task_snapshot_cannot_clear_loaded_page_on_completion(self):
        cached = self.task(records=[])
        cached["snapshot_event_id"] = 1
        self.window.render_task(cached)
        self.panel.toggle.click()
        self.controller.client.calls[-1][3]["callback"](
            {
                "task_id": "run-a",
                "offset": 0,
                "steps": [self.record(f"s-{n}") for n in range(100)],
                "total": 100,
                "snapshot_event_id": 201,
            }
        )
        self.window.render_task({**cached, "status": "succeeded"})
        self.assertEqual(self.panel.model.rowCount(), 100)
        self.assertIn("/steps?", self.controller.client.calls[-1][1])

    def test_delayed_page_keeps_completion_after_live_cache_eviction_and_new_starts(
        self,
    ):
        task = self.task(records=[])
        task["snapshot_event_id"] = 1
        self.window.render_task(task)
        self.panel.toggle.click()
        request = self.controller.client.calls[-1]
        self.window.append_event(self.event(2, "initial", "tool.completed"))
        for n in range(110):
            self.window.append_event(self.event(3 + n * 2, f"new-{n}"))
            self.window.append_event(
                self.event(4 + n * 2, f"new-{n}", "tool.completed")
            )
        self.assertNotIn("initial", self.panel._live)
        request[3]["callback"](
            {
                "task_id": "run-a",
                "offset": 0,
                "snapshot_event_id": 1,
                "total": 1,
                "steps": [self.record("initial", status="running")],
            }
        )
        self.assertEqual(self.panel.model.rows[0]["status"], "succeeded")
        self.assertEqual(self.panel.model.rowCount(), 100)
        self.assertEqual(self.panel.model.rows[1]["step_id"], "new-0")
        self.assertEqual(self.panel.total, 111)
        self.assertEqual(self.panel._pending_updates, {})

    def test_snapshot_watermark_prevents_replayed_started_count_and_cancel_refreshes(
        self,
    ):
        task = self.task(records=[self.record("s", status="running")], total=1001)
        task["snapshot_event_id"] = 5000
        self.window.render_task(task)
        self.window.append_event(self.event(4000, "out-of-page-start"))
        self.assertEqual(self.panel.total, 1001)
        self.panel.toggle.click()
        self.controller.client.calls[-1][3]["callback"](
            {
                "task_id": "run-a",
                "offset": 0,
                "snapshot_event_id": 5000,
                "total": 1001,
                "steps": [self.record("s", status="running")],
            }
        )
        self.window.render_task({**task, "status": "cancelled"})
        request = self.controller.client.calls[-1]
        self.assertIn("/steps?", request[1])
        request[3]["callback"](
            {
                "task_id": "run-a",
                "offset": 0,
                "snapshot_event_id": 5002,
                "total": 1001,
                "steps": [self.record("s", status="cancelled")],
            }
        )
        self.assertEqual(self.panel.model.rows[0]["status"], "cancelled")
        self.assertIn("已取消", self.panel.model.data(self.panel.model.index(0, 2)))

    def test_new_summary_wins_over_older_pending_page_and_invalidates_selected_detail(
        self,
    ):
        task = self.task(records=[self.record("s", status="running")])
        task["snapshot_event_id"] = 100
        self.window.render_task(task)
        self.panel.toggle.click()
        pending_page = self.controller.client.calls[-1]
        self.panel.table.setCurrentIndex(self.panel.model.index(0, 0))
        old_detail = self.controller.client.calls[-1]
        updated = {
            **task,
            "steps": [self.record("s", status="succeeded")],
            "snapshot_event_id": 200,
        }
        self.window.render_task(updated)
        new_detail = self.controller.client.calls[-1]
        self.assertIsNot(old_detail, new_detail)
        pending_page[3]["callback"](
            {
                "task_id": "run-a",
                "offset": 0,
                "total": 1,
                "snapshot_event_id": 100,
                "steps": [self.record("s", status="running")],
            }
        )
        self.assertEqual(self.panel.model.rows[0]["status"], "succeeded")
        old_detail[3]["callback"](
            self.record("s", status="running", result={"content": "过期详情"})
        )
        self.assertNotIn("过期详情", self.panel.details.toPlainText())
        new_detail[3]["callback"](self.record("s", result={"content": "新结果"}))
        self.assertIn("新结果", self.panel.details.toPlainText())

    def test_first_page_summary_watermark_does_not_suppress_second_page_completion(
        self,
    ):
        task = self.task(records=[self.record("first")], total=101)
        task["snapshot_event_id"] = 100
        self.window.render_task(task)
        self.panel.toggle.click()
        self.panel.request_page(100)
        self.controller.client.calls[-1][3]["callback"](
            {
                "task_id": "run-a",
                "offset": 100,
                "total": 101,
                "snapshot_event_id": 100,
                "steps": [self.record("last", status="running")],
            }
        )
        self.window.render_task({**task, "snapshot_event_id": 200})
        self.window.append_event(self.event(150, "last", "tool.completed"))
        self.assertEqual(self.panel.model.rows[0]["status"], "succeeded")
        self.assertEqual(self.panel.total, 101)
