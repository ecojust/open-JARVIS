"""Native artifact preview, bounded rendering and contextual follow-up tests."""

import os
import base64
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, Qt, Signal
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QPushButton
    from jarvis.desktop_client import DesktopController
    from jarvis.preview_ui import ArtifactPreviewWidget, PreviewTableModel

    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


if QT_AVAILABLE:

    class PreviewClient(QObject):
        event = Signal(object)
        connection = Signal(bool, str)

        def __init__(self):
            super().__init__()
            self.calls = []
            self.downloads = []

        def request(self, method, path, payload=None, **kwargs):
            self.calls.append((method, path, payload, kwargs))

        def download(self, path, destination, **kwargs):
            self.downloads.append((path, destination, kwargs))

        def close(self):
            pass


@unittest.skipUnless(QT_AVAILABLE, "PySide6 is an optional desktop dependency")
class ArtifactPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        with patch("jarvis.config.read_preferences", return_value={}):
            self.controller = DesktopController(self.app, autostart=False)
        self.controller.client = PreviewClient()
        self.controller.ready = True
        self.controller.speech_enabled = False

    def tearDown(self):
        self.controller.cleanup()
        if self.controller.results:
            self.controller.results.deleteLater()
        self.controller.tile.deleteLater()
        self.controller.deleteLater()
        self.app.processEvents()

    @staticmethod
    def task(task_id, name, suffix, **extra):
        return {
            "task_id": task_id,
            "message": name,
            "status": "succeeded",
            "display_text": "工具已生成实际文件。",
            "speech_text": "已完成。",
            "artifacts": [
                {"artifact_id": task_id + "-file", "path": name + suffix, "size": 100}
            ],
            **extra,
        }

    def test_completion_automatically_previews_actual_artifact_in_maximized_window(
        self,
    ):
        task = self.task("word", "国际形势", ".docx")
        task["artifacts"].append(
            {"artifact_id": "hidden", "path": "report.preview.json", "hidden": True}
        )
        self.controller._queue_notification(task)
        window = self.controller.results
        self.assertTrue(window.isMaximized())
        self.assertFalse(window.isFullScreen())
        self.assertEqual(window.content_tabs.currentWidget(), window.preview)
        request = next(
            call
            for call in self.controller.client.calls
            if call[1].endswith("/preview")
        )
        self.assertEqual(request[:2], ("GET", "/api/artifacts/word-file/preview"))
        request[3]["callback"](
            {"kind": "document", "paragraphs": ["国际形势概要", "来自真实文件的正文。"]}
        )
        self.assertIn("来自真实文件的正文", window.preview.document.toPlainText())
        labels = [
            button.text() for button in window.artifacts_area.findChildren(QPushButton)
        ]
        self.assertEqual(len(labels), 1)
        self.assertIn("预览 国际形势.docx", labels)
        self.assertNotIn("hidden", " ".join(labels))

    def test_late_preview_cannot_replace_the_selected_tasks_result(self):
        first = self.task("first", "文档", ".docx")
        second = self.task("second", "表格", ".xlsx")
        window = self.controller._ensure_results()
        window.render_task(first)
        old_callback = self.controller.client.calls[-1][3]["callback"]
        window.render_task(second)
        new_callback = self.controller.client.calls[-1][3]["callback"]
        old_callback({"kind": "document", "paragraphs": ["过期正文"]})
        self.assertEqual(window.preview.artifact_id, "second-file")
        self.assertNotIn("过期正文", window.preview.document.toPlainText())
        new_callback(
            {
                "kind": "spreadsheet",
                "sheets": [
                    {
                        "name": "排班",
                        "columns": ["员工", "周一"],
                        "rows": [["员工01", "早班"]],
                    }
                ],
            }
        )
        self.assertEqual(
            window.preview.table_model.data(window.preview.table_model.index(0, 0)),
            "员工01",
        )

    def test_preview_error_preserves_summary_and_original_download(self):
        window = self.controller._ensure_results()
        window.render_task(self.task("bad", "文件", ".docx"))
        self.controller.client.calls[-1][3]["error"]("文件内容损坏，不能预览。")
        self.assertIn("损坏", window.preview.placeholder.text())
        self.assertTrue(window.preview.download.isEnabled())
        self.assertIn("生成实际文件", window.body.toPlainText())

    def test_table_is_bounded_readonly_and_preserves_formula_text(self):
        model = PreviewTableModel(
            [str(i) for i in range(80)],
            [['=WEBSERVICE("https://bad")'] * 80 for _ in range(250)],
        )
        self.assertEqual(model.rowCount(), 200)
        self.assertEqual(model.columnCount(), 64)
        cell = model.index(0, 0)
        self.assertEqual(model.data(cell), '=WEBSERVICE("https://bad")')
        self.assertFalse(bool(model.flags(cell) & Qt.ItemFlag.ItemIsEditable))
        model.deleteLater()

    def test_sheet_selection_and_slide_navigation_show_real_content(self):
        preview = ArtifactPreviewWidget()
        preview.render(
            {
                "kind": "spreadsheet",
                "sheets": [
                    {"name": "计划", "columns": ["姓名"], "rows": [["员工01"]]},
                    {"name": "约束", "columns": ["规则"], "rows": [["夜班后休息"]]},
                ],
            }
        )
        preview.sheet_selector.setCurrentIndex(1)
        self.assertEqual(
            preview.table_model.data(preview.table_model.index(0, 0)), "夜班后休息"
        )
        preview.render(
            {
                "kind": "presentation",
                "slides": [
                    {
                        "title": "金融趋势",
                        "bullets": ["有来源的分析"],
                        "notes": "第一段讲解",
                    },
                    {
                        "title": "风险与限制",
                        "bullets": ["区分事实与推断"],
                        "notes": "第二段讲解",
                    },
                ],
            }
        )
        self.assertIn("金融趋势", preview.slide.toPlainText())
        preview.next.click()
        self.assertIn("风险与限制", preview.slide.toPlainText())
        self.assertEqual(preview.notes.toPlainText(), "第二段讲解")
        self.assertFalse(preview.next.isEnabled())
        preview.deleteLater()

    def test_narrow_spreadsheet_keeps_readable_columns_and_horizontal_scroll(self):
        preview = ArtifactPreviewWidget()
        preview.resize(480, 500)
        preview.render(
            {
                "kind": "spreadsheet",
                "sheets": [
                    {
                        "name": "实际数据",
                        "columns": ["议题", "区域", "发布日期", "主要进展", "来源"],
                        "rows": [
                            [
                                "国际形势",
                                "地区",
                                "2026-10-31",
                                "这一列需要可读的文本宽度",
                                "https://example.com/source",
                            ]
                        ],
                    }
                ],
            }
        )
        preview.show()
        self.app.processEvents()
        self.assertTrue(
            all(preview.table.columnWidth(index) >= 110 for index in range(5))
        )
        self.assertGreater(preview.table.columnWidth(3), 120)
        self.assertGreater(preview.table.horizontalScrollBar().maximum(), 0)
        preview.hide()
        preview.deleteLater()

    def test_documents_escape_markup_and_markdown_does_not_load_images(self):
        preview = ArtifactPreviewWidget()
        preview.render(
            {
                "kind": "document",
                "paragraphs": ['<img src="file:///secret.png">', "真实正文"],
            }
        )
        self.assertIn('<img src="file:///secret.png">', preview.document.toPlainText())
        preview.render(
            {
                "kind": "markdown",
                "content": "# 公众号文章\n\n![remote](https://example.com/tracker.png)\n\n正文",
            }
        )
        self.assertIn("公众号文章", preview.document.toPlainText())
        self.assertIn("正文", preview.document.toPlainText())
        preview.deleteLater()

    def test_referenced_ppt_skips_intervening_schedule_and_explicit_new_resets(self):
        ppt = self.task("ppt", "金融趋势", ".pptx")
        schedule = self.task("schedule", "30人排班", ".xlsx")
        self.controller.tasks = {"ppt": ppt, "schedule": schedule}
        self.controller.task_order = ["schedule", "ppt"]
        self.controller.selected_task = "schedule"
        self.assertTrue(
            self.controller.submit("刚才的ppt你帮我剪一个讲解视频，用于发布b站")
        )
        self.assertEqual(self.controller.client.calls[-1][2]["parent_task_id"], "ppt")
        self.assertTrue(self.controller.submit("新任务"))
        self.assertTrue(self.controller.submit("帮我写一篇独立文章"))
        self.assertNotIn("parent_task_id", self.controller.client.calls[-1][2])

    def test_referenced_source_type_precedes_requested_output_type(self):
        ppt = self.task("ppt", "金融趋势", ".pptx")
        video = self.task("video", "金融讲解", ".mp4")
        self.controller.tasks = {"ppt": ppt, "video": video}
        self.controller.task_order = ["video", "ppt"]
        self.controller.selected_task = "ppt"
        self.controller.submit("刚才的视频帮我总结成PPT")
        self.assertEqual(self.controller.client.calls[-1][2]["parent_task_id"], "video")
        self.controller.submit("继续生成视频")
        self.assertEqual(self.controller.client.calls[-1][2]["parent_task_id"], "ppt")

    def test_waiting_input_uses_its_existing_task_without_parent(self):
        self.controller.awaiting_task = "question"
        self.controller.selected_task = "old"
        self.controller.tasks["old"] = self.task("old", "旧文档", ".docx")
        self.controller.submit("安排下一周，早晚班")
        self.assertEqual(
            self.controller.client.calls[-1][1], "/api/tasks/question/input"
        )
        self.assertNotIn("parent_task_id", self.controller.client.calls[-1][2])

    def test_actual_office_exports_are_parsed_then_rendered(self):
        from jarvis.artifact_preview import build_preview
        from jarvis.office_tools import (
            create_document,
            create_presentation,
            create_spreadsheet,
        )

        preview = ArtifactPreviewWidget()
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            create_document(
                workspace,
                "真实文档.docx",
                title="文档验证",
                sections=[{"heading": "内容", "body": "从DOCX读取的实际正文。"}],
            )
            create_spreadsheet(
                workspace,
                "真实表格.xlsx",
                title="表格验证",
                columns=["事件", "说明"],
                rows=[["实际记录", "来自单元格"]],
            )
            create_presentation(
                workspace,
                "真实幻灯片.pptx",
                title="演示验证",
                slides=[
                    {
                        "title": "真实页面",
                        "bullets": ["从PPTX读取"],
                        "notes": "实际讲稿",
                    }
                ],
            )
            for filename in ("真实文档.docx", "真实表格.xlsx", "真实幻灯片.pptx"):
                payload = build_preview(workspace / filename)
                self.assertEqual(payload["source"], "actual_file")
                preview.loading(filename, filename)
                preview.render({**payload, "artifact_id": filename, "name": filename})
                if filename.endswith(".docx"):
                    self.assertIn(
                        "从DOCX读取的实际正文", preview.document.toPlainText()
                    )
                elif filename.endswith(".xlsx"):
                    self.assertEqual(preview.table_model.columns, ["事件", "说明"])
                    self.assertEqual(
                        preview.table_model.data(preview.table_model.index(0, 0)),
                        "实际记录",
                    )
                else:
                    self.assertIn("真实页面", preview.slide.toPlainText())
                    self.assertIn("从PPTX读取", preview.slide.toPlainText())
                    self.assertEqual(preview.notes.toPlainText(), "实际讲稿")
        preview.deleteLater()

    def test_video_cover_and_many_long_artifacts_keep_small_screen_maximized(self):
        long_name = "最近金融趋势总结与宏观风险讲解用于发布B站的完整演示视频.mp4"
        task = self.task("video-small-screen", long_name[:-4], ".mp4")
        task["artifacts"] += [
            {
                "artifact_id": "extra-" + str(index),
                "path": long_name + suffix,
                "size": 200,
            }
            for index, suffix in enumerate(
                (".srt", ".narration.md", ".sources.txt", ".audit.json")
            )
        ]
        self.controller._queue_notification(task)
        window = self.controller.results
        QTest.qWait(50)
        self.assertTrue(window.isMaximized())
        self.assertFalse(window.isFullScreen())
        screen_size = window.screen().geometry().size()
        image = QImage(960, 540, QImage.Format.Format_RGB32)
        image.fill(QColor("#173763"))
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(image.save(buffer, "PNG"))
        buffer.close()
        request = next(
            call
            for call in self.controller.client.calls
            if call[1].endswith("/preview")
        )
        request[3]["callback"](
            {
                "kind": "video",
                "title": long_name,
                "name": long_name,
                "duration_seconds": 99.96,
                "width": 1280,
                "height": 720,
                "source": "actual_file",
                "cover_png_base64": base64.b64encode(bytes(data)).decode("ascii"),
            }
        )
        # Process the asynchronous layout pass after the real PNG is decoded;
        # calling showMaximized again here would hide the original regression.
        QTest.qWait(250)
        self.assertTrue(window.isMaximized())
        self.assertFalse(window.isFullScreen())
        self.assertLessEqual(window.width(), screen_size.width())
        self.assertLessEqual(window.height(), screen_size.height())
        self.assertFalse(window.preview.poster.pixmap().isNull())
        self.assertEqual(window.preview.poster.minimumSizeHint().width(), 0)
        self.assertLessEqual(len(window.artifacts_area.findChildren(QPushButton)), 2)
        self.assertTrue(window.preview.play.isVisible())

    def test_video_plays_authenticated_local_copy_in_system_player(self):
        with patch(
            "PySide6.QtGui.QDesktopServices.openUrl", return_value=True
        ) as opened:
            self.controller.play_artifact("video-id", "金融趋势.mp4")
            route, path, callbacks = self.controller.client.downloads[-1]
            self.assertEqual(route, "/api/artifacts/video-id/media")
            self.assertEqual(callbacks["max_bytes"], 128 * 1024 * 1024)
            self.assertFalse(opened.called)
            path.write_bytes(b"test transport fixture")
            callbacks["callback"](path)
            self.assertTrue(opened.call_args.args[0].isLocalFile())
            self.assertEqual(Path(opened.call_args.args[0].toLocalFile()), path)
            self.assertNotIn("token", opened.call_args.args[0].toString())
            self.controller.play_artifact("video-id", "金融趋势.mp4")
            self.assertEqual(len(self.controller.client.downloads), 1)
        self.controller.cleanup()
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
