"""Bounded previews parsed from actual artifacts, without Office or a browser.

Office sidecars are never the authority for this view. Heavy dependencies are
imported only when the user opens that type of artifact in the result window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from itertools import islice
import math
from pathlib import Path
from xml.etree import ElementTree
import zipfile


MAX_OFFICE_BYTES = 32 * 1024 * 1024
MAX_UNPACKED_BYTES = 64 * 1024 * 1024
MAX_PREVIEW_CHARS = 100_000
MAX_PREVIEW_CELLS = 4_000
OFFICE_NOTICE = "从实际文件读取的内容预览；完整排版以 Office 文件为准。"
_STOP = object()


@dataclass
class _Budget:
    """One shared budget, including repeated shared strings and empty cells."""

    characters: int = MAX_PREVIEW_CHARS
    cells: int = MAX_PREVIEW_CELLS
    clips: int = 0

    @property
    def exhausted(self):
        return self.characters <= 0 or self.cells <= 0

    def text(self, value, limit=4000):
        value = str(value)
        count = min(len(value), max(0, self.characters), limit)
        self.characters -= count
        if count < len(value):
            self.clips += 1
        return value[:count]

    def cell(self, value, limit=4000):
        if self.exhausted:
            self.clips += 1
            return _STOP
        self.cells -= 1
        if value is None:
            return None
        if isinstance(value, str):
            return self.text(value, limit)
        if isinstance(value, (date, datetime, time)):
            return self.text(value.isoformat(), limit)
        if isinstance(value, float) and not math.isfinite(value):
            return self.text(str(value), limit)
        if isinstance(value, (bool, int, float)):
            # Do not return a truncated numeric value as if it were real data.
            cost = len(str(value))
            if cost > self.characters:
                self.characters = 0
                self.clips += 1
                return _STOP
            self.characters -= cost
            return value
        return self.text(value, limit)


def _office_archive(path: Path) -> None:
    if path.stat().st_size > MAX_OFFICE_BYTES:
        raise ValueError("Office 文件超过 32 MiB 预览限制，请下载后查看。")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 5000:
                raise ValueError("Office 文件包含过多内部资源，无法预览。")
            if sum(item.file_size for item in entries) > MAX_UNPACKED_BYTES:
                raise ValueError("Office 文件解压内容超过 64 MiB 预览限制。")
            if len({item.filename for item in entries}) != len(entries):
                raise ValueError("Office 文件包含重复内部资源，无法确定实际内容。")
            if any(item.flag_bits & 1 for item in entries):
                raise ValueError("暂不预览加密的 Office 文件。")
    except zipfile.BadZipFile as exc:
        raise ValueError("文件不是有效的 Office 文档，不能仅靠扩展名预览。") from exc


def _document(path: Path) -> dict:
    from docx import Document

    document = Document(path)
    budget = _Budget()
    title = budget.text(document.core_properties.title or path.stem, 500)
    paragraphs = []
    source_paragraphs = document.paragraphs
    for paragraph in islice(source_paragraphs, 500):
        if budget.exhausted:
            break
        if paragraph.text.strip():
            paragraphs.append(budget.text(paragraph.text, 8000))
    tables = []
    source_tables = document.tables
    truncated = len(source_paragraphs) > 500 or len(source_tables) > 10
    for table in islice(source_tables, 10):
        if budget.exhausted:
            truncated = True
            break
        rows = []
        table_truncated = len(table.rows) > 100
        for row in islice(table.rows, 100):
            if budget.exhausted:
                table_truncated = True
                break
            cells = []
            source_cells = row.cells
            table_truncated = table_truncated or len(source_cells) > 30
            for cell in islice(source_cells, 30):
                value = budget.cell(cell.text, 2000)
                if value is _STOP:
                    table_truncated = True
                    break
                cells.append(value)
            rows.append(cells)
        tables.append(
            {"rows": rows, "total_rows": len(table.rows), "truncated": table_truncated}
        )
        truncated = truncated or table_truncated
    return {
        "kind": "document",
        "title": title,
        "paragraphs": paragraphs,
        "tables": tables,
        "truncated": truncated or budget.clips > 0 or budget.exhausted,
        "notice": OFFICE_NOTICE,
    }


class _FilterTarget:
    """Read the actual worksheet autoFilter without constructing its cell tree."""

    def __init__(self):
        self.reference = None

    def start(self, tag, attributes):
        if tag.rsplit("}", 1)[-1] == "autoFilter":
            self.reference = attributes.get("ref")

    def end(self, tag):
        pass

    def data(self, value):
        pass

    def doctype(self, name, pubid, system):
        raise ValueError("工作表 XML 包含不支持的文档类型声明。")

    def close(self):
        return self.reference


def _sheet_bounds(sheet, archive):
    """Use a real filter range; generated sheets have a title above row 4."""
    from openpyxl.utils.cell import range_boundaries

    target = _FilterTarget()
    parser = ElementTree.XMLParser(target=target)
    # ReadOnlyWorksheet deliberately omits auto_filter. Its recorded OOXML path
    # is obtained from the workbook relationship, never a guessed sheet index.
    source_name = getattr(sheet, "_worksheet_path", None)
    if source_name is not None:
        try:
            with archive.open(source_name) as stream:
                for chunk in iter(lambda: stream.read(64 * 1024), b""):
                    parser.feed(chunk)
            reference = parser.close()
        except (KeyError, ElementTree.ParseError) as exc:
            raise ValueError("工作表 XML 内容不完整，无法确定预览范围。") from exc
    else:
        reference = None
    maximum_row = sheet.max_row or 201
    maximum_column = sheet.max_column or 40
    if reference:
        try:
            first_column, header_row, last_column, last_row = range_boundaries(
                reference
            )
        except (ValueError, TypeError) as exc:
            raise ValueError("工作表筛选范围无效，无法确定表头。") from exc
        if not all(
            isinstance(value, int)
            for value in (first_column, header_row, last_column, last_row)
        ):
            raise ValueError("工作表筛选范围必须包含明确的行列位置。")
        if not (
            1 <= header_row <= min(maximum_row, 1000)
            and header_row <= last_row <= maximum_row
            and 1 <= first_column <= last_column <= maximum_column
        ):
            raise ValueError("工作表筛选范围超出文件数据或表头预览限制。")
        return first_column, header_row, last_column, last_row
    return 1, 1, maximum_column, maximum_row


def _spreadsheet(path: Path) -> dict:
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=False, keep_links=False)
    budget = _Budget()
    title = budget.text(workbook.properties.title or path.stem, 500)
    try:
        sheets = []
        truncated = len(workbook.worksheets) > 8
        with zipfile.ZipFile(path) as archive:
            for sheet in islice(workbook.worksheets, 8):
                if budget.exhausted:
                    truncated = True
                    break
                first_column, header_row, last_column, last_row = _sheet_bounds(
                    sheet, archive
                )
                total_rows = max(0, last_row - header_row)
                total_columns = max(0, last_column - first_column + 1)
                columns, rows = [], []
                clips_before = budget.clips
                name = budget.text(sheet.title, 100)
                sheet_truncated = total_rows > 200 or total_columns > 40
                # The iterator only visits the bounded selected rows and columns.
                iterator = sheet.iter_rows(
                    min_row=header_row,
                    max_row=min(last_row, header_row + 200),
                    min_col=first_column,
                    max_col=min(last_column, first_column + 39),
                    values_only=True,
                )
                for index, row in enumerate(iterator):
                    if budget.exhausted:
                        sheet_truncated = True
                        break
                    values = []
                    for cell in row:
                        value = budget.cell(cell)
                        if value is _STOP:
                            sheet_truncated = True
                            break
                        values.append(value)
                    if index == 0:
                        columns = [
                            str(value) if value is not None else "" for value in values
                        ]
                    else:
                        rows.append(values)
                sheet_truncated = sheet_truncated or budget.clips > clips_before
                sheets.append(
                    {
                        "name": name,
                        "columns": columns,
                        "rows": rows,
                        "total_rows": total_rows,
                        "total_columns": total_columns,
                        "header_row": header_row,
                        "first_column": first_column,
                        "truncated": sheet_truncated,
                    }
                )
                truncated = truncated or sheet_truncated
        return {
            "kind": "spreadsheet",
            "title": title,
            "sheets": sheets,
            "truncated": truncated or budget.clips > 0,
            "notice": "从实际工作簿读取；全体预览最多 100,000 个字符、4,000 个单元格、8 个工作表，每表最多 200 行和 40 列。公式显示原文，不重新计算。",
        }
    finally:
        workbook.close()


def _presentation(path: Path) -> dict:
    from pptx import Presentation

    presentation = Presentation(path)
    budget = _Budget()
    title = budget.text(presentation.core_properties.title or path.stem, 500)
    slides = []
    truncated = len(presentation.slides) > 30
    for index, slide in enumerate(presentation.slides):
        if index >= 30 or budget.exhausted:
            truncated = True
            break
        title_shape = slide.shapes.title
        slide_title = budget.text(
            title_shape.text if title_shape is not None else f"第 {index + 1} 页", 500
        )
        bullets = []
        for shape in slide.shapes:
            if shape == title_shape:
                continue
            if budget.exhausted or len(bullets) >= 60:
                truncated = True
                break
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    if not paragraph.text.strip():
                        continue
                    if budget.exhausted or len(bullets) >= 60:
                        truncated = True
                        break
                    bullets.append(budget.text(paragraph.text, 2000))
            elif shape.has_table:
                for row in islice(shape.table.rows, 20):
                    if budget.exhausted or len(bullets) >= 60:
                        truncated = True
                        break
                    values = []
                    for cell in islice(row.cells, 10):
                        if values:
                            if budget.characters < 3:
                                budget.clips += 1
                                truncated = True
                                break
                            values.append(budget.text(" | ", 3))
                        value = budget.cell(cell.text, 200)
                        if value is _STOP:
                            truncated = True
                            break
                        values.append(value)
                    bullets.append("".join(values))
        notes = ""
        if (
            not budget.exhausted
            and slide.has_notes_slide
            and slide.notes_slide.notes_text_frame
        ):
            notes = budget.text(slide.notes_slide.notes_text_frame.text, 8000)
        slides.append({"title": slide_title, "bullets": bullets, "notes": notes})
    return {
        "kind": "presentation",
        "title": title,
        "slides": slides,
        "slide_count": len(presentation.slides),
        "truncated": truncated or budget.clips > 0 or budget.exhausted,
        "notice": OFFICE_NOTICE,
    }


def build_preview(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise ValueError("找不到要预览的实际文件。")
    suffix = path.suffix.lower()
    try:
        if suffix in {".docx", ".xlsx", ".pptx"}:
            _office_archive(path)
            preview = {
                ".docx": _document,
                ".xlsx": _spreadsheet,
                ".pptx": _presentation,
            }[suffix](path)
        elif suffix == ".mp4":
            import base64
            from subprocess import SubprocessError
            from .video_tools import probe_video, video_cover_png

            preview = {
                "kind": "video",
                "title": path.stem,
                **probe_video(path),
                "notice": "元数据来自实际视频；点击播放后由系统播放器打开本地 MP4。",
            }
            try:
                cover = video_cover_png(path)
                # Base64 must also fit the desktop's 2 MiB JSON response limit.
                if len(cover) > 1024 * 1024:
                    raise ValueError("封面体积超过预览限制")
                preview["cover_png_base64"] = base64.b64encode(cover).decode("ascii")
                preview["notice"] = (
                    "元数据与封面来自实际视频；点击播放后由系统播放器打开本地 MP4。"
                )
            except (ValueError, OSError, SubprocessError):
                preview["cover_error"] = (
                    "视频信息已读取，但封面暂时无法生成。可继续播放原文件。"
                )
        elif suffix in {".md", ".txt", ".csv", ".json", ".srt"}:
            with path.open("rb") as source:
                data = source.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError("文本文件超过 2 MiB 预览限制。")
            text = data.decode("utf-8-sig")
            preview = {
                "kind": "markdown" if suffix == ".md" else "text",
                "title": path.stem,
                "content": text[:MAX_PREVIEW_CHARS],
                "truncated": len(text) > MAX_PREVIEW_CHARS,
                "notice": "从实际保存的文件读取。",
            }
        else:
            raise ValueError("暂不支持该格式的内容预览，请下载后打开。")
    except ImportError as exc:
        raise ValueError(
            "预览组件未安装，请安装 requirements-office.txt 中的依赖。"
        ) from exc
    except (UnicodeError, zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise ValueError("文件内容不完整或格式无效，无法生成预览。") from exc
    return {
        **preview,
        "filename": path.name,
        "size": path.stat().st_size,
        "source": "actual_file",
    }
