"""Structured tools must make real files through the same audited registry."""

from pathlib import Path
import tempfile
import unittest

from jarvis.storage import Store
from jarvis.tools import ToolRegistry


class WorkToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = ToolRegistry(self.root / "work", Store(self.root / "data"))

    async def test_document_is_real_office_and_preview_reads_its_contents(self):
        result = await self.registry.execute(
            "create_document",
            {
                "path": "reports/核对.docx",
                "title": "真实产物",
                "sections": [
                    {"heading": "结论", "body": "这段文字必须来自实际 Word 文件。"}
                ],
                "sources": [
                    {"title": "协议测试资料", "url": "https://example.com/source"}
                ],
            },
        )
        self.assertNotIn("error", result)
        self.assertTrue(
            (self.registry.workspace / result["path"]).read_bytes().startswith(b"PK")
        )
        preview = await self.registry.execute(
            "get_file_preview", {"path": result["path"]}
        )
        self.assertEqual(preview["source"], "actual_file")
        self.assertIn("这段文字必须来自实际 Word 文件。", preview["paragraphs"])
        self.assertTrue(any(item.get("hidden") for item in result["artifacts"]))

    async def test_invalid_nested_cells_and_fields_are_rejected_before_writing(self):
        valid = {
            "path": "table.xlsx",
            "title": "约束",
            "columns": ["数据"],
            "rows": [[1]],
        }
        for updates in (
            {"rows": [[float("nan")]]},
            {"rows": [[10**400]]},
            {"rows": [[{}]]},
            {
                "sources": [
                    {
                        "title": "不接受嵌套参数",
                        "url": "https://example.com",
                        "shell": "ignored",
                    }
                ]
            },
        ):
            with self.subTest(updates=updates):
                result = await self.registry.execute(
                    "create_spreadsheet", {**valid, **updates}
                )
                self.assertIn("error", result)
        self.assertFalse((self.registry.workspace / "table.xlsx").exists())

    async def test_plain_text_cannot_masquerade_as_office_or_video(self):
        for suffix in ("docx", "xlsx", "pptx", "mp4"):
            name = "fake." + suffix
            result = await self.registry.execute(
                "write_file", {"path": name, "content": "not that format"}
            )
            self.assertIn("error", result)
            self.assertFalse((self.registry.workspace / name).exists())
