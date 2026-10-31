"""Lazy native previews of parsed artifact contents, without a browser engine."""

from __future__ import annotations

import base64
from html import escape

from PySide6.QtCore import (
    QAbstractTableModel,
    QByteArray,
    QBuffer,
    QIODevice,
    QModelIndex,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QFontDatabase, QFontMetrics, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)


_FONT_INITIALIZED = False


def _register_preview_font():
    """Load the bundled CJK font once, after the lightweight tile is visible."""
    global _FONT_INITIALIZED
    if _FONT_INITIALIZED:
        return
    _FONT_INITIALIZED = True
    from .paths import asset_path

    path = asset_path("assets/fonts/NotoSansSC-Regular.otf")
    if not path.is_file():
        return
    identifier = QFontDatabase.addApplicationFont(str(path))
    families = (
        QFontDatabase.applicationFontFamilies(identifier) if identifier >= 0 else []
    )
    app = QApplication.instance()
    if families and app is not None:
        font = app.font()
        font.setFamilies(
            [families[0], *[name for name in font.families() if name != families[0]]]
        )
        app.setFont(font)


class PreviewTableModel(QAbstractTableModel):
    """Bounded, read-only cells; no widgets or formulas are executed per cell."""

    def __init__(self, columns, rows, parent=None):
        super().__init__(parent)
        self.columns = [str(value)[:200] for value in columns[:64]]
        self.rows = [
            list(row[:64]) for row in rows[:200] if isinstance(row, (list, tuple))
        ]
        width = min(64, max([len(self.columns), *(len(row) for row in self.rows)]))
        self.columns += [str(index + 1) for index in range(len(self.columns), width)]

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role not in (
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.ToolTipRole,
        ):
            return None
        row = self.rows[index.row()]
        value = row[index.column()] if index.column() < len(row) else ""
        return "" if value is None else str(value)[:4000]

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.columns[section] if section < len(self.columns) else ""
        return str(section + 1)


class PreviewPoster(QLabel):
    """Keep a decoded cover image; rescale only when the preview is resized."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._original = None
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

    def minimumSizeHint(self):
        return QSize(0, 0)

    def sizeHint(self):
        return QSize(0, 0)

    def setPixmap(self, pixmap):
        self._original = pixmap
        self._resize_image()

    def clear(self):
        self._original = None
        super().clear()

    def _resize_image(self):
        if self._original is not None:
            super().setPixmap(
                self._original.scaled(
                    max(1, self.width() - 4),
                    max(1, self.height() - 4),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_image()


class ArtifactPreviewWidget(QWidget):
    play_requested = Signal(object, str)
    download_requested = Signal(object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        _register_preview_font()
        # Importing the launcher still does not load this module or any Qt class.
        from .desktop_ui import SafeTextBrowser

        self.setObjectName("artifactPreview")
        self.setStyleSheet(
            """
            QWidget#artifactPreview { background: #ffffff; border-radius: 14px; }
            QLabel { background: transparent; color: #1d1d1f; }
            QLabel#heading { font-size: 18px; font-weight: 600; }
            QLabel#muted { color: #6e6e73; }
            QTextBrowser, QPlainTextEdit { background: #ffffff; color: #1d1d1f;
                border: 1px solid #e5e5ea; border-radius: 10px; padding: 16px;
                selection-background-color: #dcecff; selection-color: #1d1d1f; }
            QTableView { background: #ffffff; alternate-background-color: #f7f8fa;
                color: #1d1d1f; gridline-color: #e5e5ea; border: 1px solid #e5e5ea;
                border-radius: 10px; selection-background-color: #dcecff;
                selection-color: #1d1d1f; }
            QHeaderView::section { background: #f5f5f7; color: #515154;
                border: none; border-bottom: 1px solid #e5e5ea; padding: 9px; }
            QTableCornerButton::section { background: #f5f5f7; border: none; }
            QPushButton { background: #f5f5f7; color: #1d1d1f; border: 1px solid #e1e1e6;
                border-radius: 8px; padding: 7px 13px; }
            QPushButton:hover { background: #ececf0; border-color: #d1d1d6; }
            QPushButton:pressed { background: #e5e5ea; }
            QPushButton:disabled { color: #a0a0a6; background: #f7f7f9; border-color: #eeeeF1; }
            QPushButton#primary { background: #007aff; color: white; border-color: #007aff; }
            QPushButton#primary:hover { background: #006ee6; border-color: #006ee6; }
            QPushButton#primary:disabled { background: #dcecff; color: #87a8cf; border-color: #dcecff; }
            QComboBox { background: #f5f5f7; color: #1d1d1f; border: 1px solid #e1e1e6;
                border-radius: 8px; padding: 7px 10px; }
            QComboBox QAbstractItemView { background: #ffffff; color: #1d1d1f;
                selection-background-color: #dcecff; selection-color: #1d1d1f; }
            """
        )
        self.artifact_id = None
        self.name = ""
        self.payload = {}
        self._sheets = []
        self._slides = []
        self._slide_index = 0
        self.table_model = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        self.title = QLabel("文件预览")
        self.title.setObjectName("heading")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        self.title.setMinimumWidth(0)
        self.title.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        heading.addWidget(self.title, 1)
        self.download = QPushButton("保存文件")
        self.download.setToolTip("将原始文件保存到你选择的位置")
        self.download.setEnabled(False)
        self.download.clicked.connect(
            lambda: self.download_requested.emit(self.artifact_id, self.name)
        )
        heading.addWidget(self.download)
        layout.addLayout(heading)
        self.notice = QLabel("选择生成的文件，即可在这里查看内容。")
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        self.notice.setObjectName("muted")
        layout.addWidget(self.notice)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.placeholder = QLabel("生成的文件会显示在这里")
        self.placeholder.setObjectName("muted")
        self.placeholder.setTextFormat(Qt.TextFormat.PlainText)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.stack.addWidget(self.placeholder)
        self.document = SafeTextBrowser()
        self.document.setAccessibleName("实际文件正文预览")
        self.stack.addWidget(self.document)

        sheet_page = QWidget()
        sheet_layout = QVBoxLayout(sheet_page)
        sheet_layout.setContentsMargins(0, 0, 0, 0)
        self.sheet_selector = QComboBox()
        self.sheet_selector.setAccessibleName("选择工作表")
        self.sheet_selector.currentIndexChanged.connect(self._show_sheet)
        sheet_layout.addWidget(self.sheet_selector)
        self.table = QTableView()
        self.table.setAccessibleName("电子表格实际单元格预览")
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setDefaultSectionSize(165)
        self.table.horizontalHeader().setStretchLastSection(False)
        sheet_layout.addWidget(self.table, 1)
        self.sheet_page = sheet_page
        self.stack.addWidget(sheet_page)

        slide_page = QWidget()
        slide_layout = QVBoxLayout(slide_page)
        slide_layout.setContentsMargins(0, 0, 0, 0)
        navigation = QHBoxLayout()
        self.previous = QPushButton("上一页")
        self.next = QPushButton("下一页")
        self.previous.clicked.connect(lambda: self._show_slide(self._slide_index - 1))
        self.next.clicked.connect(lambda: self._show_slide(self._slide_index + 1))
        self.slide_number = QLabel()
        navigation.addWidget(self.previous)
        navigation.addWidget(self.slide_number, 1, Qt.AlignmentFlag.AlignCenter)
        navigation.addWidget(self.next)
        slide_layout.addLayout(navigation)
        self.slide = SafeTextBrowser()
        self.slide.setAccessibleName("PPT 实际幻灯片内容预览")
        self.slide.setStyleSheet(
            "QTextBrowser { background: #ffffff; color: #1d1d1f; "
            "border: 1px solid #e5e5ea; border-radius: 12px; padding: 28px; }"
        )
        self.slide.document().setDefaultStyleSheet(
            "h1 { font-size: 24px; margin-bottom: 18px; } li { font-size: 17px; margin-bottom: 10px; }"
        )
        slide_layout.addWidget(self.slide, 1)
        self.notes_toggle = QPushButton("查看此页讲稿")
        self.notes_toggle.setCheckable(True)
        slide_layout.addWidget(self.notes_toggle)
        self.notes = QPlainTextEdit()
        self.notes.setAccessibleName("PPT 演讲备注")
        self.notes.setReadOnly(True)
        self.notes.setMaximumHeight(120)
        self.notes.setPlaceholderText("此页没有演讲备注。")
        slide_layout.addWidget(self.notes)
        self.notes.hide()
        self.notes_toggle.toggled.connect(self.notes.setVisible)
        self.slide_page = slide_page
        self.stack.addWidget(slide_page)

        video_page = QWidget()
        video_layout = QVBoxLayout(video_page)
        self.poster = PreviewPoster()
        self.poster.setText("MP4 讲解视频")
        self.poster.setTextFormat(Qt.TextFormat.PlainText)
        self.poster.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.poster.setStyleSheet(
            "background: #f5f5f7; color: #6e6e73; border: 1px solid #e5e5ea; "
            "border-radius: 12px; font-size: 26px;"
        )
        video_layout.addWidget(self.poster, 1)
        self.video_info = QLabel()
        self.video_info.setTextFormat(Qt.TextFormat.PlainText)
        self.video_info.setWordWrap(True)
        self.video_info.setMinimumWidth(0)
        self.video_info.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        video_layout.addWidget(self.video_info)
        self.play = QPushButton("播放视频")
        self.play.setToolTip("使用系统默认播放器打开视频")
        self.play.setObjectName("primary")
        self.play.clicked.connect(
            lambda: self.play_requested.emit(self.artifact_id, self.name)
        )
        video_layout.addWidget(self.play)
        self.video_page = video_page
        self.stack.addWidget(video_page)

    def loading(self, artifact_id, name):
        self.artifact_id, self.name = artifact_id, str(name)
        self.title.setText(self.name[:180])
        self.download.setEnabled(False)
        self.notice.setText("正在打开文件…")
        self.placeholder.setText("正在准备预览…")
        self.stack.setCurrentWidget(self.placeholder)
        self.payload = {}
        self._sheets = []
        self._slides = []
        self.document.clear()
        self.slide.clear()
        self.notes.clear()
        self.poster.clear()
        self.table.setModel(None)
        if self.table_model is not None:
            self.table_model.deleteLater()
            self.table_model = None

    def show_error(self, message):
        self.notice.setText("暂时无法预览；原文件仍可下载。")
        self.placeholder.setText(str(message)[:2000])
        self.stack.setCurrentWidget(self.placeholder)
        self.download.setEnabled(self.artifact_id is not None)

    def render(self, payload):
        if not isinstance(payload, dict):
            self.show_error("产物预览格式无效。")
            return
        self.payload = payload
        self.artifact_id = payload.get("artifact_id", self.artifact_id)
        self.name = str(
            payload.get("name") or self.name or payload.get("title") or "产物"
        )
        self.title.setText(str(payload.get("title") or self.name)[:180])
        self.download.setEnabled(self.artifact_id is not None)
        kind = payload.get("kind")
        if kind in {"spreadsheet", "table"}:
            self._sheets = list(payload.get("sheets") or [])[:20]
            self.sheet_selector.blockSignals(True)
            self.sheet_selector.clear()
            self.sheet_selector.addItems(
                [
                    str(item.get("name", f"工作表 {index + 1}"))[:100]
                    for index, item in enumerate(self._sheets)
                ]
            )
            self.sheet_selector.blockSignals(False)
            self.stack.setCurrentWidget(self.sheet_page)
            self._show_sheet(0)
        elif kind in {"presentation", "slides"}:
            self._slides = list(payload.get("slides") or [])[:100]
            self.notice.setText(
                "PowerPoint 演示文稿 · 可逐页查看内容与讲稿，保存文件以查看完整排版。"
            )
            self.stack.setCurrentWidget(self.slide_page)
            self._show_slide(0)
        elif kind == "video":
            self._show_video(payload)
        elif kind in {"markdown", "text", "document", "html"}:
            self.stack.setCurrentWidget(self.document)
            if kind == "markdown":
                self.document.setMarkdown(str(payload.get("content") or "")[:100000])
                self.notice.setText("文章内容预览 · 检查来源和排版后即可复制使用。")
            elif kind == "document":
                paragraphs = list(payload.get("paragraphs") or [])[:1000]
                parts, remaining = [], 100000
                for item in paragraphs:
                    text = str(item)[:remaining]
                    remaining -= len(text)
                    parts.append("<p>" + escape(text) + "</p>")
                    if remaining <= 0:
                        break
                for table in list(payload.get("tables") or [])[:20]:
                    if remaining <= 0:
                        break
                    if isinstance(table, dict):
                        rows = list(table.get("rows", []))
                        if table.get("columns"):
                            rows.insert(0, table["columns"])
                    else:
                        rows = table
                    parts.append("<table border='1' cellspacing='0' cellpadding='5'>")
                    for row in list(rows or [])[:100]:
                        if remaining <= 0:
                            break
                        cells = []
                        for cell in list(row)[:30]:
                            value = str(cell)[: min(4000, remaining)]
                            remaining -= len(value)
                            cells.append("<td>" + escape(value) + "</td>")
                            if remaining <= 0:
                                break
                        parts.append("<tr>" + "".join(cells) + "</tr>")
                    parts.append("</table>")
                self.document.setHtml("".join(parts) or "<p>文件没有可预览的正文。</p>")
                self.notice.setText(
                    "Word 文档 · 可阅读正文和表格，保存文件以查看完整排版。"
                )
            else:
                self.document.setPlainText(str(payload.get("content") or "")[:100000])
                self.notice.setText("文件内容预览。")
            if payload.get("truncated"):
                self.notice.setText(
                    self.notice.text() + " 当前内容已截取，完整内容在原文件中。"
                )
        else:
            self.show_error(
                str(
                    payload.get("message")
                    or "此格式暂不支持原生预览，请下载原文件查看。"
                )
            )

    def _show_sheet(self, index):
        if not 0 <= index < len(self._sheets):
            self.notice.setText("文件没有可预览的工作表。")
            return
        sheet = self._sheets[index]
        self.table.setModel(None)
        if self.table_model is not None:
            self.table_model.deleteLater()
        self.table_model = PreviewTableModel(
            sheet.get("columns") or [], sheet.get("rows") or [], self.table
        )
        self.table.setModel(self.table_model)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(110)
        metrics = QFontMetrics(self.table.font())
        for column, heading in enumerate(self.table_model.columns):
            samples = [heading] + [
                str(row[column])
                if column < len(row) and row[column] is not None
                else ""
                for row in self.table_model.rows[:20]
            ]
            width = max(metrics.horizontalAdvance(value[:40]) + 28 for value in samples)
            date_column = "日期" in heading or all(
                not value
                or (len(value) >= 10 and value[4:5] == "-" and value[7:8] == "-")
                for value in samples[1:]
            )
            self.table.setColumnWidth(
                column, 120 if date_column else min(240, max(120, width))
            )
        rows = self.table_model.rowCount()
        columns = self.table_model.columnCount()
        total_rows = sheet.get("total_rows", rows)
        total_columns = sheet.get("total_columns", columns)
        self.notice.setText(
            f"实际单元格预览：显示 {rows} 行、{columns} 列；文件报告 {total_rows} 行、{total_columns} 列。公式不会在预览中执行。"
        )
        if (
            sheet.get("truncated")
            or rows < len(sheet.get("rows") or [])
            or columns < len(sheet.get("columns") or [])
        ):
            self.notice.setText(
                self.notice.text() + " 已限制展示范围，完整内容请下载。"
            )

    def _show_slide(self, index):
        if not self._slides:
            self.slide.setPlainText("文件没有可预览的幻灯片。")
            self.previous.setEnabled(False)
            self.next.setEnabled(False)
            return
        self._slide_index = max(0, min(index, len(self._slides) - 1))
        slide = self._slides[self._slide_index]
        self.slide_number.setText(f"{self._slide_index + 1} / {len(self._slides)}")
        self.previous.setEnabled(self._slide_index > 0)
        self.next.setEnabled(self._slide_index < len(self._slides) - 1)
        title = str(slide.get("title") or f"第 {self._slide_index + 1} 页")[:500]
        bullets = slide.get("bullets") or []
        if not bullets and slide.get("text"):
            bullets = str(slide["text"]).splitlines()
        content = (
            "<h1>"
            + escape(title)
            + "</h1><ul>"
            + "".join(
                "<li><p>"
                + escape(str(item).removeprefix("• ").removeprefix("· "))[:4000]
                + "</p></li>"
                for item in list(bullets)[:30]
            )
            + "</ul>"
        )
        self.slide.setHtml(content)
        self.notes.setPlainText(str(slide.get("notes") or "")[:12000])

    def _show_video(self, payload):
        self.stack.setCurrentWidget(self.video_page)
        self.poster.clear()
        self.poster.setText("MP4 讲解视频")
        encoded = payload.get("cover_png_base64")
        if isinstance(encoded, str) and len(encoded) < 3_000_000:
            try:
                raw = base64.b64decode(encoded, validate=True)
                buffer = QBuffer()
                buffer.setData(QByteArray(raw))
                buffer.open(QIODevice.OpenModeFlag.ReadOnly)
                reader = QImageReader(buffer, b"png")
                size = reader.size()
                if (
                    size.width() > 0
                    and size.height() > 0
                    and size.width() * size.height() <= 4_000_000
                ):
                    pixmap = QPixmap.fromImage(reader.read())
                    self.poster.setPixmap(
                        pixmap.scaled(
                            960,
                            540,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    )
            except (ValueError, TypeError):
                pass
        duration = payload.get("duration_seconds", payload.get("duration"))
        duration_label = (
            f"{duration:.1f} 秒" if isinstance(duration, (int, float)) else "时长未提供"
        )
        self.video_info.setText(
            f"{self.name}  ·  {duration_label}  ·  {payload.get('width', '?')} × {payload.get('height', '?')}\n点击播放将打开系统默认播放器。"
        )
        self.notice.setText(
            "视频已准备好。点击「播放视频」使用系统播放器打开，也可保存文件。"
        )
        self.play.setEnabled(self.artifact_id is not None)
