import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from jarvis.office_tools import (
    create_document,
    create_presentation,
    create_spreadsheet,
    render_presentation_images,
)


OFFICE_AVAILABLE = all(
    importlib.util.find_spec(name) for name in ("docx", "openpyxl", "pptx", "PIL")
)
SOURCES = [
    {
        "title": "验证资料，不代表最新新闻",
        "url": "https://example.com/research",
        "published_at": "2026-01-01",
        "note": "测试用来源",
    }
]


@unittest.skipUnless(OFFICE_AVAILABLE, "Office export dependencies are not installed")
class OfficeToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def preview(self, result):
        return json.loads(
            (self.root / result["preview_path"]).read_text(encoding="utf-8")
        )

    def test_import_keeps_heavy_office_and_image_libraries_lazy(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import jarvis.office_tools; assert not any(name in sys.modules for name in ('docx','openpyxl','pptx','PIL','PySide6'))",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_docx_contains_actual_headings_body_and_traceable_sources(self):
        from docx import Document

        result = create_document(
            self.root,
            "reports/国际形势.docx",
            title="国际形势资料总结",
            sections=[
                {
                    "heading": "范围",
                    "body": "这是可核验的文档生成测试。\n不能当作实时新闻。",
                    "bullets": ["保留来源", "明确时间范围"],
                }
            ],
            sources=SOURCES,
        )
        document = Document(self.root / result["path"])
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
        self.assertIn("国际形势资料总结", text)
        self.assertIn("这是可核验的文档生成测试。", text)
        self.assertIn("https://example.com/research", text)
        preview = self.preview(result)
        self.assertEqual(preview["kind"], "document")
        self.assertEqual(
            preview["sections"][0]["bullets"], ["保留来源", "明确时间范围"]
        )
        self.assertIn("内容预览", preview["preview_notice"])
        self.assertTrue(result["artifacts"][1]["hidden"])
        self.assertEqual(result["artifacts"][1]["role"], "preview_manifest")
        self.assertFalse(Path(result["path"]).is_absolute())

    def test_xlsx_reopens_with_numeric_types_and_formula_text_neutralized(self):
        from openpyxl import load_workbook

        rows = [
            ['=HYPERLINK("https://example.com","click")', 3.5, True],
            ["+CMD", -7, None],
        ]
        result = create_spreadsheet(
            self.root,
            "reports/国际形势.xlsx",
            title="数据清单",
            columns=["情况", "数值", "已核验"],
            rows=rows,
            sources=SOURCES,
        )
        workbook = load_workbook(self.root / result["path"], data_only=False)
        try:
            sheet = workbook["数据"]
            self.assertEqual(sheet["A5"].value, rows[0][0])
            self.assertEqual(sheet["A5"].data_type, "s")
            self.assertEqual(sheet["B5"].value, 3.5)
            self.assertEqual(sheet["B5"].data_type, "n")
            self.assertEqual(sheet["C5"].value, True)
            self.assertEqual(sheet.freeze_panes, "A5")
            self.assertEqual(workbook["来源"]["B2"].hyperlink.target, SOURCES[0]["url"])
        finally:
            workbook.close()
        self.assertEqual(self.preview(result)["rows"], rows)

    def test_pptx_has_actual_titles_notes_links_and_matching_manifest(self):
        from pptx import Presentation

        slides = [
            {
                "title": "金融趋势验证",
                "bullets": ["使用可验证资料", "区分事实与分析"],
                "notes": "本页讲稿用于后续真实语音合成。",
                "sources": SOURCES,
            },
            {
                "title": "下一步",
                "bullets": ["取得新资料后更新内容"],
                "notes": "当前不声称掌握最新金融数据。",
            },
        ]
        result = create_presentation(
            self.root,
            "reports/金融趋势.pptx",
            title="金融趋势测试稿",
            slides=slides,
            sources=SOURCES,
        )
        presentation = Presentation(self.root / result["path"])
        self.assertEqual(len(presentation.slides), 2)
        self.assertAlmostEqual(
            presentation.slide_width / presentation.slide_height, 16 / 9, places=5
        )
        for index, slide in enumerate(presentation.slides):
            self.assertEqual(slide.shapes.title.text, slides[index]["title"])
            self.assertEqual(
                slide.notes_slide.notes_text_frame.text, slides[index]["notes"]
            )
        with ZipFile(self.root / result["path"]) as archive:
            relationships = archive.read("ppt/slides/_rels/slide1.xml.rels").decode()
            self.assertIn(SOURCES[0]["url"], relationships)
        preview = self.preview(result)
        self.assertEqual(preview["kind"], "presentation")
        self.assertEqual(preview["slides"][0]["notes"], slides[0]["notes"])
        self.assertEqual(result["preview"]["slide_count"], 2)
        self.assertNotIn("slides", result["preview"])

    def test_same_stem_office_formats_have_distinct_preview_files(self):
        first = create_document(
            self.root, "report.docx", title="报告", sections=[{"body": "正文"}]
        )
        second = create_spreadsheet(
            self.root,
            "report.xlsx",
            title="报告",
            columns=["情况"],
            rows=[["明确资料"]],
        )
        self.assertNotEqual(first["preview_path"], second["preview_path"])
        self.assertTrue((self.root / first["preview_path"]).is_file())
        self.assertTrue((self.root / second["preview_path"]).is_file())

    def test_default_no_overwrite_preserves_original_document_and_preview(self):
        result = create_document(
            self.root, "report.docx", title="初稿", sections=[{"body": "真实原文"}]
        )
        files = {
            path: (self.root / path).read_bytes()
            for path in (result["path"], result["preview_path"])
        }
        with self.assertRaisesRegex(ValueError, "已存在"):
            create_document(
                self.root, "report.docx", title="覆盖", sections=[{"body": "不应写入"}]
            )
        for path, content in files.items():
            self.assertEqual((self.root / path).read_bytes(), content)
        create_document(
            self.root,
            "report.docx",
            title="明确更新",
            sections=[{"body": "新内容"}],
            overwrite=True,
        )
        self.assertEqual(self.preview(result)["title"], "明确更新")

    def test_publish_failure_rolls_back_new_preview_and_leaves_no_partial_docx(self):
        original = os.link
        calls = 0

        def fail_second(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated full destination volume")
            return original(source, destination)

        with patch("jarvis.office_tools.os.link", side_effect=fail_second):
            with self.assertRaises(OSError):
                create_document(
                    self.root, "report.docx", title="报告", sections=[{"body": "正文"}]
                )
        self.assertEqual(list(self.root.iterdir()), [])

    def test_rejects_wrong_extensions_escape_special_names_and_symlink_sidecar(self):
        for path in (
            "../outside.docx",
            "report.txt",
            "NUL.docx",
            "report.docx:stream",
            "folder\\report.docx",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                create_document(
                    self.root, path, title="报告", sections=[{"body": "正文"}]
                )
        outside = self.root / "outside.json"
        outside.write_text("original", encoding="utf-8")
        sidecar = self.root / "report.docx.preview.json"
        try:
            sidecar.symlink_to(outside)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows account lacks symlink privilege")
            raise
        with self.assertRaises(ValueError):
            create_document(
                self.root,
                "report.docx",
                title="报告",
                sections=[{"body": "正文"}],
                overwrite=True,
            )
        self.assertEqual(outside.read_text(encoding="utf-8"), "original")

    def test_bounds_and_invalid_sources_fail_before_creating_outputs(self):
        for value in (math.inf, math.nan, 10**17, {"nested": "object"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                create_spreadsheet(
                    self.root,
                    "report.xlsx",
                    title="数据",
                    columns=["值"],
                    rows=[[value]],
                )
        with self.assertRaises(ValueError):
            create_presentation(
                self.root,
                "report.pptx",
                title="数据",
                slides=[{"title": "过长", "bullets": ["字" * 240] * 8}],
            )
        for url in (
            "javascript:alert(1)",
            "https://user:password@example.com",
            "file:///etc/passwd",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                create_document(
                    self.root,
                    "report.docx",
                    title="数据",
                    sections=[{"body": "正文"}],
                    sources=[{"title": "非法来源", "url": url}],
                )
        self.assertEqual(list(self.root.iterdir()), [])

    def test_renderer_writes_real_chinese_16_by_9_images_or_reports_missing_font(self):
        from PIL import Image
        from jarvis.office_tools import _cjk_font

        result = create_presentation(
            self.root,
            "report.pptx",
            title="内容预览",
            slides=[
                {
                    "title": "金融趋势内容预览",
                    "bullets": [
                        "这是实际输出的中文幻灯片图像",
                        "所用资料的日期与来源需另行核实",
                    ],
                    "notes": "讲稿",
                }
            ],
        )
        try:
            font = _cjk_font()
        except ValueError:
            self.skipTest("Native runner has no supported CJK font")
        paths = render_presentation_images(
            self.preview(result), self.root / "frames", font_path=font
        )
        self.assertEqual(len(paths), 1)
        with Image.open(paths[0]) as image:
            self.assertEqual(image.size, (1280, 720))
            self.assertEqual(image.format, "PNG")
            self.assertGreater(len(image.getcolors(1280 * 720)), 100)
        with self.assertRaisesRegex(ValueError, "字体"):
            render_presentation_images(
                self.preview(result),
                self.root / "missing-font",
                font_path=self.root / "missing.ttf",
            )


if __name__ == "__main__":
    unittest.main()
