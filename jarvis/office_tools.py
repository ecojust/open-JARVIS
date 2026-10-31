"""Bounded, lazy Office generation and honest structured content previews.

The preview is derived from the same input as the Office document. It is not a
claim to reproduce Microsoft Office's pagination or pixel-exact layout. Heavy
document/image libraries are imported only inside their execution paths.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import unicodedata
from urllib.parse import urlsplit


PREVIEW_NOTICE = "内容预览；实际 Office 分页与排版以下载文件为准。"
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_PREVIEW_BYTES = 2 * 1024 * 1024
MAX_TOTAL_CHARS = 500_000
MAX_SOURCES = 30
MIME_TYPES = {
    "document": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "spreadsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "presentation": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _text(value, label, maximum, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{label}必须是{'非空' if not empty else ''}文本")
    if len(value) > maximum:
        raise ValueError(f"{label}超过 {maximum} 字限制")
    if any(
        (ord(character) < 32 and character not in "\n\r\t")
        or 0xD800 <= ord(character) <= 0xDFFF
        or ord(character) in {0xFFFE, 0xFFFF}
        for character in value
    ):
        raise ValueError(f"{label}包含不支持的控制字符")
    return value.strip() if not empty else value


def _sources(values):
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > MAX_SOURCES:
        raise ValueError(f"来源必须是最多 {MAX_SOURCES} 项的列表")
    result = []
    for source in values:
        if not isinstance(source, dict) or set(source) - {
            "title",
            "url",
            "published_at",
            "accessed_at",
            "note",
        }:
            raise ValueError("来源字段仅支持 title/url/published_at/accessed_at/note")
        title = _text(source.get("title"), "来源标题", 300)
        url = _text(source.get("url"), "来源链接", 2048)
        try:
            parsed = urlsplit(url)
            valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not any(character.isspace() for character in url)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("来源链接必须是不含登录凭据的 HTTP(S) 网页")
        item = {"title": title, "url": url}
        for key in ("published_at", "accessed_at", "note"):
            if key in source:
                item[key] = _text(
                    source[key], key, 1200 if key == "note" else 80, empty=True
                )
        result.append(item)
    return result


def _bounded_payload(payload):
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    if (
        len(encoded) > MAX_TOTAL_CHARS
        or len(encoded.encode("utf-8")) > MAX_PREVIEW_BYTES
    ):
        raise ValueError("文档内容总量超过限制，请拆成多个文件")
    return payload


def _base(kind, title, sources):
    return {
        "schema_version": 1,
        "kind": kind,
        "title": _text(title, "标题", 180),
        "preview_notice": PREVIEW_NOTICE,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sources": _sources(sources),
    }


def _document_payload(title, sections, sources):
    if not isinstance(sections, list) or not 1 <= len(sections) <= 60:
        raise ValueError("Word 需要 1–60 个段落章节")
    payload = _base("document", title, sources)
    payload["sections"] = []
    for section in sections:
        if not isinstance(section, dict) or set(section) - {
            "heading",
            "body",
            "bullets",
        }:
            raise ValueError("章节仅支持 heading/body/bullets")
        bullets = section.get("bullets", [])
        if not isinstance(bullets, list) or len(bullets) > 30:
            raise ValueError("每章最多 30 个要点")
        normalized = {
            "heading": _text(section.get("heading", ""), "章节标题", 240, empty=True),
            "body": _text(section.get("body", ""), "章节正文", 30_000, empty=True),
            "bullets": [_text(item, "章节要点", 2000) for item in bullets],
        }
        if not normalized["body"].strip() and not normalized["bullets"]:
            raise ValueError("章节至少需要正文或要点")
        payload["sections"].append(normalized)
    return _bounded_payload(payload)


def _spreadsheet_payload(title, columns, rows, sources):
    if not isinstance(columns, list) or not 1 <= len(columns) <= 40:
        raise ValueError("Excel 需要 1–40 个列名")
    columns = [_text(column, "列名", 160) for column in columns]
    if len(set(columns)) != len(columns):
        raise ValueError("Excel 列名不能重复")
    if (
        not isinstance(rows, list)
        or len(rows) > 5000
        or len(rows) * len(columns) > 50_000
    ):
        raise ValueError("Excel 最多 5000 行、50000 个数据单元格")
    normalized = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError("每一行的单元格数量必须与列名一致")
        values = []
        for value in row:
            if value is None or isinstance(value, bool):
                values.append(value)
            elif isinstance(value, str):
                values.append(_text(value, "单元格", 4000, empty=True))
            elif isinstance(value, (int, float)):
                if abs(value) > 10**15 or not math.isfinite(value):
                    raise ValueError(
                        "数值必须有限且不超过 Excel 精度范围；较长编号请作为文本传入"
                    )
                values.append(value)
            else:
                raise ValueError("Excel 单元格仅支持文本、数值、开关或空值")
        normalized.append(values)
    payload = _base("spreadsheet", title, sources)
    payload.update(columns=columns, rows=normalized)
    return _bounded_payload(payload)


def _presentation_payload(title, slides, sources):
    if not isinstance(slides, list) or not 1 <= len(slides) <= 24:
        raise ValueError("PPT 需要 1–24 页")
    payload = _base("presentation", title, sources)
    payload["slides"] = []
    for slide in slides:
        if not isinstance(slide, dict) or set(slide) - {
            "title",
            "bullets",
            "notes",
            "sources",
        }:
            raise ValueError("幻灯片仅支持 title/bullets/notes/sources")
        bullets = slide.get("bullets", [])
        if not isinstance(bullets, list) or not 1 <= len(bullets) <= 8:
            raise ValueError("每页需要 1–8 个简短要点")
        bullets = [_text(item, "幻灯片要点", 240) for item in bullets]
        if sum(map(len, bullets)) > 640:
            raise ValueError("单页要点合计不能超过 640 字，请拆页以保持可读性")
        payload["slides"].append(
            {
                "title": _text(slide.get("title"), "幻灯片标题", 100),
                "bullets": bullets,
                "notes": _text(slide.get("notes", ""), "演讲者备注", 4000, empty=True),
                "sources": _sources(slide.get("sources")),
            }
        )
    return _bounded_payload(payload)


def _destination(workspace, path, extension):
    # Reuse the same portable filename policy without importing the registry or
    # its HTTP stack into this module at startup.
    from .tools import _validate_path_components

    root = Path(workspace).resolve()
    if not isinstance(path, str) or not path or len(path) > 450:
        raise ValueError("输出路径必须是工作目录中的相对文件名")
    value = Path(path)
    if value.is_absolute() or ".." in path.split("/") or "\\" in path:
        raise ValueError("只允许工作目录内的相对路径")
    _validate_path_components(path)
    target = root / path
    if target.suffix.lower() != extension:
        raise ValueError(f"输出文件必须使用 {extension} 扩展名")
    for candidate in [target, *target.parents]:
        if candidate == root:
            break
        if candidate.is_symlink():
            raise ValueError("输出路径不能经过符号链接")
    target = target.resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError("输出路径越过工作目录")
    return root, target


def _atomic_bundle(writers, *, overwrite):
    """Stage complete files, then commit; restore pre-existing files on error."""
    if type(overwrite) is not bool:
        raise ValueError("overwrite 必须是开关")
    paths = list(writers)
    parent = paths[0].parent
    if any(path.parent != parent for path in paths):
        raise ValueError("同批文件必须位于同一目录")
    parent.mkdir(parents=True, exist_ok=True)
    for path in paths:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("输出目标必须是普通文件，不能是目录或符号链接")
        if path.exists() and not overwrite:
            raise ValueError("文件或预览已存在；只有明确要求覆盖时才能 overwrite=true")
    with tempfile.TemporaryDirectory(prefix=".jarvis-office-", dir=parent) as directory:
        temporary = Path(directory)
        staged, backups, committed = {}, {}, []
        for index, (path, writer) in enumerate(writers.items()):
            staged[path] = temporary / f"{index}{path.suffix}"
            writer(staged[path])
            if staged[path].stat().st_size > MAX_OUTPUT_BYTES:
                raise ValueError("生成文件超过 16 MiB，请拆分任务")
            # Windows FlushFileBuffers requires a writable file handle.
            with staged[path].open("rb+") as handle:
                os.fsync(handle.fileno())
        try:
            for index, path in enumerate(paths):
                if overwrite:
                    if path.exists():
                        backups[path] = temporary / f"backup-{index}"
                        shutil.copy2(path, backups[path])
                    os.replace(staged[path], path)
                else:
                    # A hard link publishes the fully-written inode atomically
                    # and refuses a concurrently-created existing destination.
                    os.link(staged[path], path)
                committed.append(path)
        except BaseException:
            for path in reversed(committed):
                if path in backups:
                    os.replace(backups[path], path)
                else:
                    path.unlink(missing_ok=True)
            raise


def _write_preview(path, payload):
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_PREVIEW_BYTES:
        raise ValueError("内容预览超过 2 MiB，请拆分任务")
    path.write_bytes(encoded)


def _finish(root, target, payload, writer, *, overwrite):
    preview_path = target.with_name(target.name + ".preview.json")
    _atomic_bundle(
        {preview_path: lambda output: _write_preview(output, payload), target: writer},
        overwrite=overwrite,
    )
    relative = target.relative_to(root).as_posix()
    preview_relative = preview_path.relative_to(root).as_posix()
    kind = payload["kind"]
    summary = {
        "kind": kind,
        "title": payload["title"],
        "preview_notice": PREVIEW_NOTICE,
    }
    if kind == "document":
        summary["section_count"] = len(payload["sections"])
    elif kind == "spreadsheet":
        summary.update(
            row_count=len(payload["rows"]), column_count=len(payload["columns"])
        )
    else:
        summary["slide_count"] = len(payload["slides"])
    return {
        "saved": True,
        "path": relative,
        "size": target.stat().st_size,
        "media_type": MIME_TYPES[kind],
        "preview_path": preview_relative,
        "preview": summary,
        "artifacts": [
            {
                "path": relative,
                "size": target.stat().st_size,
                "media_type": MIME_TYPES[kind],
                "preview_path": preview_relative,
            },
            {
                "path": preview_relative,
                "size": preview_path.stat().st_size,
                "media_type": "application/json",
                "role": "preview_manifest",
                "hidden": True,
            },
        ],
    }


def create_document(workspace, path, *, title, sections, sources=None, overwrite=False):
    """Create a real DOCX and its bounded structured content preview."""
    payload = _document_payload(title, sections, sources)
    root, target = _destination(workspace, path, ".docx")

    def write(output):
        from docx import Document
        from docx.oxml.ns import qn
        from docx.shared import Inches, Pt, RGBColor

        document = Document()
        document.core_properties.title = payload["title"]
        document.core_properties.author = "Open JARVIS"
        normal = document.styles["Normal"]
        normal.font.name = "Microsoft YaHei"
        normal.font.size = Pt(11)
        normal.element.get_or_add_rPr().get_or_add_rFonts().set(
            qn("w:eastAsia"), "Microsoft YaHei"
        )
        normal.paragraph_format.space_after = Pt(7)
        for section in document.sections:
            section.top_margin = section.bottom_margin = Inches(0.75)
        document.add_heading(payload["title"], 0)
        for item in payload["sections"]:
            if item["heading"].strip():
                document.add_heading(item["heading"], 1)
            for paragraph in item["body"].splitlines():
                if paragraph.strip():
                    document.add_paragraph(paragraph)
            for bullet in item["bullets"]:
                document.add_paragraph(bullet, style="List Bullet")
        if payload["sources"]:
            document.add_heading("参考来源", 1)
            for index, source in enumerate(payload["sources"], 1):
                paragraph = document.add_paragraph(
                    f"[{index}] {source['title']}\n{source['url']}"
                )
                paragraph.runs[0].font.color.rgb = RGBColor.from_string("315A9E")
                metadata = []
                if source.get("published_at"):
                    metadata.append("发布日期：" + source["published_at"])
                if source.get("accessed_at"):
                    metadata.append("检索日期：" + source["accessed_at"])
                if source.get("note"):
                    metadata.append(source["note"])
                if metadata:
                    document.add_paragraph("；".join(metadata))
        document.save(output)

    return _finish(root, target, payload, write, overwrite=overwrite)


def _set_excel_value(cell, value):
    cell.value = value
    if isinstance(value, str):
        # Formula-like strings are data unless a dedicated formula capability
        # is explicitly requested. This also protects exported search content.
        cell.data_type = "s"


def create_spreadsheet(
    workspace, path, *, title, columns, rows, sources=None, overwrite=False
):
    """Create a real XLSX, preserving number types and neutralizing formulas."""
    payload = _spreadsheet_payload(title, columns, rows, sources)
    root, target = _destination(workspace, path, ".xlsx")

    def write(output):
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter

        workbook = Workbook()
        workbook.properties.title = payload["title"]
        workbook.properties.creator = "Open JARVIS"
        sheet = workbook.active
        sheet.title = "数据"
        sheet.merge_cells(
            start_row=1, start_column=1, end_row=1, end_column=len(payload["columns"])
        )
        _set_excel_value(sheet.cell(1, 1), payload["title"])
        sheet.cell(1, 1).font = Font(
            name="Microsoft YaHei", size=17, bold=True, color="16243C"
        )
        sheet.row_dimensions[1].height = 30
        sheet.cell(2, 1, "文本按原文保存；数值保持数值类型。来源见单独工作表。")
        for index, column in enumerate(payload["columns"], 1):
            cell = sheet.cell(4, index)
            _set_excel_value(cell, column)
            cell.fill = PatternFill("solid", fgColor="173763")
            cell.font = Font(name="Microsoft YaHei", bold=True, color="FFFFFF")
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        for row_index, row in enumerate(payload["rows"], 5):
            for column_index, value in enumerate(row, 1):
                cell = sheet.cell(row_index, column_index)
                _set_excel_value(cell, value)
                cell.font = Font(name="Microsoft YaHei", size=11)
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                if row_index % 2:
                    cell.fill = PatternFill("solid", fgColor="EEF4FC")
                if isinstance(value, float):
                    cell.number_format = "0.00########"
        sheet.freeze_panes = "A5"
        sheet.auto_filter.ref = f"A4:{get_column_letter(len(payload['columns']))}{max(4, 4 + len(payload['rows']))}"
        for index, heading in enumerate(payload["columns"], 1):
            samples = [
                heading,
                *(str(row[index - 1] or "") for row in payload["rows"][:100]),
            ]
            width = max(
                sum(
                    2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in value
                )
                for value in samples
            )
            sheet.column_dimensions[get_column_letter(index)].width = min(
                65, max(14, width + 2)
            )
        reference = workbook.create_sheet("来源")
        reference.append(["来源标题", "网址", "发布日期", "检索日期", "说明"])
        for row_index, source in enumerate(payload["sources"], 2):
            for column_index, key in enumerate(
                ("title", "url", "published_at", "accessed_at", "note"), 1
            ):
                _set_excel_value(
                    reference.cell(row_index, column_index), source.get(key, "")
                )
            reference.cell(row_index, 2).hyperlink = source["url"]
            reference.cell(row_index, 2).style = "Hyperlink"
        reference.freeze_panes = "A2"
        for name, width in {"A": 42, "B": 70, "C": 22, "D": 22, "E": 60}.items():
            reference.column_dimensions[name].width = width
        for cell in reference[1]:
            cell.font = Font(name="Microsoft YaHei", bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="173763")
        for row in reference.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
        for output_sheet in (sheet, reference):
            output_sheet.page_setup.orientation = "landscape"
            output_sheet.page_setup.paperSize = output_sheet.PAPERSIZE_A4
            output_sheet.page_setup.fitToWidth = 1
            output_sheet.page_setup.fitToHeight = 0
            output_sheet.sheet_properties.pageSetUpPr.fitToPage = True
            output_sheet.print_area = output_sheet.calculate_dimension()
        sheet.print_title_rows = "1:4"
        reference.print_title_rows = "1:1"
        workbook.save(output)

    return _finish(root, target, payload, write, overwrite=overwrite)


def create_presentation(
    workspace, path, *, title, slides, sources=None, overwrite=False
):
    """Create a 16:9 PPTX whose actual speaker notes remain usable for video."""
    payload = _presentation_payload(title, slides, sources)
    root, target = _destination(workspace, path, ".pptx")

    def write(output):
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.util import Inches, Pt

        presentation = Presentation()
        presentation.slide_width = Inches(13.333333)
        presentation.slide_height = Inches(7.5)
        presentation.core_properties.title = payload["title"]
        presentation.core_properties.author = "Open JARVIS"
        for index, item in enumerate(payload["slides"], 1):
            slide = presentation.slides.add_slide(presentation.slide_layouts[5])
            slide.background.fill.solid()
            slide.background.fill.fore_color.rgb = RGBColor.from_string("F8FAFE")
            band = slide.shapes.add_shape(
                MSO_SHAPE.RECTANGLE, 0, 0, presentation.slide_width, Inches(0.13)
            )
            band.fill.solid()
            band.fill.fore_color.rgb = RGBColor.from_string("245EDA")
            band.line.fill.background()
            heading = slide.shapes.title
            heading.left, heading.top, heading.width, heading.height = (
                Inches(0.62),
                Inches(0.43),
                Inches(12.05),
                Inches(1.15),
            )
            heading.text = item["title"]
            for run in heading.text_frame.paragraphs[0].runs:
                run.font.name = "Microsoft YaHei"
                run.font.size = Pt(30 if len(item["title"]) <= 40 else 24)
                run.font.bold = True
                run.font.color.rgb = RGBColor.from_string("172941")
            box = slide.shapes.add_textbox(
                Inches(0.72), Inches(1.7), Inches(11.9), Inches(4.8)
            )
            box.text_frame.word_wrap = True
            box.text_frame.margin_left = box.text_frame.margin_right = Inches(0.03)
            body_size = (
                22
                if sum(map(len, item["bullets"])) <= 320 and len(item["bullets"]) <= 6
                else 18
            )
            for bullet_index, bullet in enumerate(item["bullets"]):
                paragraph = (
                    box.text_frame.paragraphs[0]
                    if bullet_index == 0
                    else box.text_frame.add_paragraph()
                )
                paragraph.text = "• " + bullet
                paragraph.font.name = "Microsoft YaHei"
                paragraph.font.size = Pt(body_size)
                paragraph.font.color.rgb = RGBColor.from_string("263A55")
                paragraph.space_after = Pt(10 if body_size == 22 else 6)
            footer = slide.shapes.add_textbox(
                Inches(0.72), Inches(6.87), Inches(11.0), Inches(0.34)
            )
            paragraph = footer.text_frame.paragraphs[0]
            paragraph.font.name = "Microsoft YaHei"
            paragraph.font.size = Pt(9)
            paragraph.font.color.rgb = RGBColor.from_string("526783")
            sources_for_slide = item["sources"] or payload["sources"]
            label = paragraph.add_run()
            label.text = "参考来源：" if sources_for_slide else "Open JARVIS"
            for source_index, source in enumerate(sources_for_slide, 1):
                run = paragraph.add_run()
                run.text = f" [{source_index}]"
                run.hyperlink.address = source["url"]
            page = slide.shapes.add_textbox(
                Inches(11.85), Inches(6.86), Inches(0.75), Inches(0.34)
            )
            page.text_frame.paragraphs[
                0
            ].text = f"{index:02d} / {len(payload['slides']):02d}"
            page.text_frame.paragraphs[0].font.size = Pt(10)
            slide.notes_slide.notes_text_frame.text = item["notes"]
        presentation.save(output)

    return _finish(root, target, payload, write, overwrite=overwrite)


def _cjk_font(font_path=None):
    from .paths import asset_path

    candidates = (
        [Path(font_path)]
        if font_path
        else [
            asset_path("assets/fonts/NotoSansSC-Regular.otf"),
            Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc",
            Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts/simhei.ttf",
            Path("/System/Library/Fonts/PingFang.ttc"),
            Path("/System/Library/Fonts/STHeiti Medium.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ValueError("未找到可用于中文幻灯片预览的字体，请配置已安装的中文字体文件")


def _wrap(draw, value, font, width):
    lines, current = [], ""
    for character in value:
        if character == "\n":
            lines.append(current)
            current = ""
        elif current and draw.textlength(current + character, font=font) > width:
            lines.append(current)
            current = character
        else:
            current += character
    if current:
        lines.append(current)
    return lines or [""]


def render_presentation_images(manifest, output_dir, *, font_path=None):
    """Render bounded 1280×720 content images for a PPT-based explainer.

    The images use the structured manifest rather than a hidden browser/Office
    process. Their typography can differ from the downloaded PowerPoint file.
    """
    if not isinstance(manifest, dict) or manifest.get("kind") != "presentation":
        raise ValueError("需要 presentation 类型的预览清单")
    payload = _presentation_payload(
        manifest.get("title"), manifest.get("slides"), manifest.get("sources")
    )
    from PIL import Image, ImageDraw, ImageFont

    font_file = str(_cjk_font(font_path))
    directory = Path(output_dir)
    if directory.is_symlink():
        raise ValueError("预览输出目录不能是符号链接")
    directory.mkdir(parents=True, exist_ok=True)
    outputs = [
        directory / f"slide-{index:02d}.png"
        for index in range(1, len(payload["slides"]) + 1)
    ]
    if any(path.exists() or path.is_symlink() for path in outputs):
        raise ValueError("幻灯片预览文件已存在，请使用新目录")
    writers = {}
    for index, (item, target) in enumerate(zip(payload["slides"], outputs), 1):

        def render(output, item=item, index=index):
            image = Image.new("RGB", (1280, 720), "#F8FAFE")
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 1280, 13), fill="#245EDA")
            title_font = ImageFont.truetype(
                font_file, 40 if len(item["title"]) <= 40 else 30
            )
            heading_lines = _wrap(draw, item["title"], title_font, 1140)
            if len(heading_lines) > 3:
                raise ValueError("标题过长，无法生成可读的 16:9 预览")
            for line_index, line in enumerate(heading_lines):
                draw.text(
                    (64, 42 + line_index * 47), line, fill="#172941", font=title_font
                )
            top = max(160, 50 + len(heading_lines) * 47)
            selected = None
            for size in (30, 28, 26, 24, 22, 20):
                font = ImageFont.truetype(font_file, size)
                wrapped = [
                    _wrap(draw, bullet, font, 1085) for bullet in item["bullets"]
                ]
                line_height = size + 10
                needed = sum(len(lines) * line_height + 15 for lines in wrapped)
                if needed <= 635 - top:
                    selected = (font, wrapped, line_height)
                    break
            if selected is None:
                raise ValueError("幻灯片内容过密，无法生成可读预览，请减少单页内容")
            font, wrapped, line_height = selected
            y = top
            for lines in wrapped:
                draw.ellipse((69, y + 15, 77, y + 23), fill="#245EDA")
                for line in lines:
                    draw.text((98, y), line, fill="#263A55", font=font)
                    y += line_height
                y += 15
            small = ImageFont.truetype(font_file, 17)
            references = item["sources"] or payload["sources"]
            if references:
                names = "；".join(source["title"] for source in references[:2])
                label = (
                    "来源："
                    + names[:85]
                    + ("…" if len(names) > 85 or len(references) > 2 else "")
                )
            else:
                label = "Open JARVIS · 内容预览"
            draw.text((64, 669), label, fill="#526783", font=small)
            draw.text(
                (1160, 669),
                f"{index:02d}/{len(payload['slides']):02d}",
                fill="#526783",
                font=small,
            )
            image.save(output, format="PNG", optimize=True)

        writers[target] = render
    _atomic_bundle(writers, overwrite=False)
    return outputs
