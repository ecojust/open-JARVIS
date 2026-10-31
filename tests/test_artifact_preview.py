"""Read actual Office content with whole-preview bounds, independent of sidecars."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from jarvis.artifact_preview import MAX_PREVIEW_CELLS, MAX_PREVIEW_CHARS, build_preview
from jarvis.office_tools import create_document, create_presentation, create_spreadsheet


OFFICE_AVAILABLE = all(
    importlib.util.find_spec(name) for name in ("docx", "openpyxl", "pptx")
)


def content_characters(preview):
    total = len(preview.get("title", ""))
    total += sum(map(len, preview.get("paragraphs", [])))
    for table in preview.get("tables", []):
        total += sum(len(str(cell)) for row in table["rows"] for cell in row)
    for sheet in preview.get("sheets", []):
        total += len(sheet["name"])
        total += sum(map(len, sheet["columns"]))
        total += sum(
            len(str(cell)) for row in sheet["rows"] for cell in row if cell is not None
        )
    for slide in preview.get("slides", []):
        total += (
            len(slide["title"]) + len(slide["notes"]) + sum(map(len, slide["bullets"]))
        )
    return total


@unittest.skipUnless(OFFICE_AVAILABLE, "Office preview dependencies are not installed")
class ArtifactPreviewParserTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def test_actual_docx_reopens_with_body_and_tables(self):
        from docx import Document

        result = create_document(
            self.root,
            "report.docx",
            title="真实文档",
            sections=[
                {
                    "heading": "范围",
                    "body": "来自实际文件的正文",
                    "bullets": ["保留来源"],
                }
            ],
        )
        path = self.root / result["path"]
        document = Document(path)
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "项目"
        table.cell(0, 1).text = "数值"
        table.cell(1, 0).text = "记录"
        table.cell(1, 1).text = "42"
        document.save(path)
        preview = build_preview(path)
        self.assertEqual(preview["kind"], "document")
        self.assertEqual(preview["source"], "actual_file")
        self.assertIn("来自实际文件的正文", preview["paragraphs"])
        self.assertEqual(
            preview["tables"][0]["rows"], [["项目", "数值"], ["记录", "42"]]
        )

    def test_generated_xlsx_uses_actual_filter_header_not_title_row(self):
        result = create_spreadsheet(
            self.root,
            "events.xlsx",
            title="国际形势",
            columns=["事件", "发布日期", "说明"],
            rows=[["真实记录", "2026-10-31", "预览测试资料"]],
            sources=[{"title": "验证来源", "url": "https://example.com/source"}],
        )
        preview = build_preview(self.root / result["path"])
        sheet = preview["sheets"][0]
        self.assertEqual(sheet["header_row"], 4)
        self.assertEqual(sheet["columns"], ["事件", "发布日期", "说明"])
        self.assertEqual(sheet["rows"], [["真实记录", "2026-10-31", "预览测试资料"]])
        self.assertEqual(sheet["total_rows"], 1)
        self.assertEqual(preview["sheets"][1]["header_row"], 1)
        self.assertIn("网址", preview["sheets"][1]["columns"])

    def test_filter_bounds_preserve_non_a_column_and_formula_as_text(self):
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet["C3"], sheet["D3"] = "项目", "计算式"
        sheet["C4"], sheet["D4"] = "一条记录", "=SUM(1,2)"
        sheet.auto_filter.ref = "C3:D4"
        path = self.root / "offset.xlsx"
        workbook.save(path)
        preview = build_preview(path)["sheets"][0]
        self.assertEqual(preview["first_column"], 3)
        self.assertEqual(preview["header_row"], 3)
        self.assertEqual(preview["columns"], ["项目", "计算式"])
        self.assertEqual(preview["rows"], [["一条记录", "=SUM(1,2)"]])

    def test_real_pptx_reads_slide_text_and_speaker_notes(self):
        result = create_presentation(
            self.root,
            "slides.pptx",
            title="金融趋势",
            slides=[
                {
                    "title": "第一张实际幻灯片",
                    "bullets": ["事实有来源", "区分预测"],
                    "notes": "这段文字是实际讲稿。",
                }
            ],
        )
        preview = build_preview(self.root / result["path"])
        self.assertEqual(preview["kind"], "presentation")
        self.assertEqual(preview["slides"][0]["title"], "第一张实际幻灯片")
        self.assertTrue(
            any("事实有来源" in value for value in preview["slides"][0]["bullets"])
        )
        self.assertIn("这段文字是实际讲稿", preview["slides"][0]["notes"])

    def test_tampered_sidecar_cannot_change_actual_preview(self):
        result = create_document(
            self.root,
            "original.docx",
            title="真正的标题",
            sections=[
                {"heading": "真实内容", "body": "只读取已保存文档", "bullets": []}
            ],
        )
        (self.root / result["preview_path"]).write_text(
            json.dumps(
                {
                    "kind": "document",
                    "title": "伪造标题",
                    "paragraphs": ["不要显示这个侧车内容"],
                }
            ),
            encoding="utf-8",
        )
        preview = build_preview(self.root / result["path"])
        encoded = json.dumps(preview, ensure_ascii=False)
        self.assertIn("真正的标题", encoded)
        self.assertNotIn("伪造标题", encoded)
        self.assertNotIn("侧车内容", encoded)

    def test_fake_office_extension_is_rejected(self):
        path = self.root / "fake.docx"
        path.write_text("不是 Word 文件", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "有效的 Office"):
            build_preview(path)

    def test_shared_xlsx_string_expansion_is_bounded_across_all_sheets(self):
        from openpyxl import Workbook

        workbook = Workbook()
        workbook.active["A1"] = "value"
        base = self.root / "shared.xlsx"
        workbook.save(base)
        source = "重复的共享文本" * 500
        # A single shared string is referenced thousands of times. The compressed
        # file stays tiny while an unbounded JSON preview would grow by megabytes.
        row_xml = []
        for row in range(1, 202):
            cells = "".join(
                f'<c r="{column}{row}" t="s"><v>0</v></c>'
                for column in ("A", "B", "C", "D", "E", "F", "G", "H", "I", "J")
            )
            row_xml.append(f'<row r="{row}">{cells}</row>')
        sheet_xml = (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:J201"/><sheetData>'
            + "".join(row_xml)
            + "</sheetData></worksheet>"
        ).encode()
        with ZipFile(base) as archive:
            entries = {item.filename: archive.read(item) for item in archive.infolist()}
        entries["xl/worksheets/sheet1.xml"] = sheet_xml
        entries["xl/sharedStrings.xml"] = (
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="2010" uniqueCount="1"><si><t>'
            + source
            + "</t></si></sst>"
        ).encode()
        content_types = entries["[Content_Types].xml"].decode()
        content_types = content_types.replace(
            "</Types>",
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>',
        )
        entries["[Content_Types].xml"] = content_types.encode()
        with ZipFile(base, "w", ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        self.assertLess(base.stat().st_size, 30_000)
        preview = build_preview(base)
        self.assertTrue(preview["truncated"])
        self.assertTrue(preview["sheets"][0]["truncated"])
        self.assertLessEqual(content_characters(preview), MAX_PREVIEW_CHARS)
        self.assertLess(len(json.dumps(preview, ensure_ascii=False).encode()), 400_000)
        self.assertLess(len(preview["sheets"][0]["rows"]), 200)

    def test_empty_cell_count_is_bounded_across_workbook(self):
        from openpyxl import Workbook

        workbook = Workbook()
        workbook.active.title = "first"
        for index in range(8):
            sheet = workbook.active if index == 0 else workbook.create_sheet(str(index))
            sheet.cell(1, 1, "first")
            sheet.cell(201, 40, "last")
        path = self.root / "sparse.xlsx"
        workbook.save(path)
        preview = build_preview(path)
        count = sum(
            len(sheet["columns"]) + sum(len(row) for row in sheet["rows"])
            for sheet in preview["sheets"]
        )
        self.assertLessEqual(count, MAX_PREVIEW_CELLS)
        self.assertTrue(preview["truncated"])

    def test_word_table_content_obeys_one_budget_shared_with_paragraphs(self):
        from docx import Document

        document = Document()
        document.add_paragraph("前言" * 3000)
        for _ in range(3):
            table = document.add_table(rows=10, cols=10)
            for row in table.rows:
                for cell in row.cells:
                    cell.text = "每格重复正文" * 250
        path = self.root / "large-table.docx"
        document.save(path)
        preview = build_preview(path)
        self.assertTrue(preview["truncated"])
        self.assertLessEqual(content_characters(preview), MAX_PREVIEW_CHARS)
        self.assertLess(len(json.dumps(preview, ensure_ascii=False).encode()), 400_000)

    def test_presentation_notes_and_slides_share_global_budget(self):
        from pptx import Presentation

        document = Presentation()
        for index in range(20):
            slide = document.slides.add_slide(document.slide_layouts[5])
            slide.shapes.title.text = f"真实幻灯片 {index + 1}"
            slide.notes_slide.notes_text_frame.text = "实际演讲备注" * 1500
        path = self.root / "many-notes.pptx"
        document.save(path)
        preview = build_preview(path)
        self.assertTrue(preview["truncated"])
        self.assertLessEqual(content_characters(preview), MAX_PREVIEW_CHARS)
        self.assertLess(len(preview["slides"]), preview["slide_count"])

    def test_video_cover_failure_keeps_valid_metadata_but_invalid_media_fails(self):
        path = self.root / "video.mp4"
        path.write_bytes(b"transport fixture; actual media probe is isolated here")
        with (
            patch(
                "jarvis.video_tools.probe_video",
                return_value={"width": 960, "height": 540, "duration_seconds": 10.0},
            ),
            patch(
                "jarvis.video_tools.video_cover_png",
                side_effect=ValueError("cover unavailable"),
            ),
        ):
            preview = build_preview(path)
        self.assertEqual(preview["width"], 960)
        self.assertIn("cover_error", preview)
        self.assertNotIn("cover_png_base64", preview)
        with patch(
            "jarvis.video_tools.probe_video",
            side_effect=ValueError("invalid actual container"),
        ):
            with self.assertRaisesRegex(ValueError, "invalid actual container"):
                build_preview(path)

    def test_worksheet_doctype_is_rejected_without_entity_expansion(self):
        from openpyxl import Workbook

        workbook = Workbook()
        workbook.active["A1"] = "header"
        path = self.root / "doctype.xlsx"
        workbook.save(path)
        with ZipFile(path) as archive:
            entries = {item.filename: archive.read(item) for item in archive.infolist()}
        xml = entries["xl/worksheets/sheet1.xml"]
        entries["xl/worksheets/sheet1.xml"] = (
            b'<!DOCTYPE worksheet [<!ENTITY repeat "unsafe">]>' + xml
        )
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        with self.assertRaisesRegex(ValueError, "文档类型"):
            build_preview(path)


if __name__ == "__main__":
    unittest.main()
