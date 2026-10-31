"""Native desktop widgets; no browser engine and no background timers.

Imported only by the desktop entry point. Network, model and file operations
belong to the controller; these widgets render bounded snapshots.
"""

from __future__ import annotations

from collections import deque
import math
from pathlib import Path

from PySide6.QtCore import QEvent, QRect, QSize, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QScrollArea,
    QPushButton,
    QSplitter,
    QSizePolicy,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)


STYLE = """
QWidget { background: #0d1420; color: #e4eaf3; font-size: 13px; }
QMainWindow, QDialog { background: #0d1420; }
QLabel#eyebrow { color: #7d9dc4; font-size: 11px; }
QLabel#heading { font-size: 24px; font-weight: 600; }
QLabel#status { color: #aac4e4; padding: 7px 0; }
QLabel#muted { color: #8c9db5; }
QFrame#separator { background: #233246; max-height: 1px; }
QPushButton { background: #182538; border: 1px solid #304259; padding: 8px 13px; border-radius: 6px; }
QPushButton:hover { background: #223852; border-color: #4c78ae; }
QPushButton:pressed { background: #28436a; }
QPushButton:disabled { color: #637288; border-color: #243145; }
QPushButton#primary { background: #286de0; border-color: #397cea; color: white; }
QPushButton#primary:hover { background: #367feb; }
QPlainTextEdit, QTextBrowser, QLineEdit { background: #111d2c; border: 1px solid #283c54; border-radius: 6px; padding: 9px; selection-background-color: #286de0; }
QTextBrowser { border: none; padding: 15px; }
QListWidget { background: #0f1927; border: 1px solid #26374c; border-radius: 6px; outline: none; }
QListWidget::item { padding: 11px 9px; border-bottom: 1px solid #1a293c; }
QListWidget::item:selected { background: #1c3e6b; color: #f2f7ff; }
QListWidget::item:hover { background: #182c44; }
QSplitter::handle { background: #243347; width: 1px; }
QScrollBar:vertical { width: 9px; background: #0d1420; }
QScrollBar::handle:vertical { background: #34465f; min-height: 25px; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QMenu { background: #142236; border: 1px solid #304865; padding: 5px; }
QMenu::item { padding: 7px 22px; }
QMenu::item:selected { background: #28486e; }
QTabWidget::pane { border: 1px solid #283c54; border-radius: 6px; }
QTabBar::tab { background: #142236; border: 1px solid #304259; padding: 8px 18px; color: #a7bdd7; }
QTabBar::tab:selected { background: #254e80; color: #ffffff; }
QTableView { background: #111d2c; alternate-background-color: #17263a; color: #e4eaf3; gridline-color: #2c4059; border: 1px solid #283c54; selection-background-color: #286de0; }
QHeaderView::section { background: #1c3350; color: #dce9f8; border: 1px solid #304a67; padding: 6px; }
QComboBox { background: #182538; border: 1px solid #304259; border-radius: 5px; padding: 7px; }
QComboBox QAbstractItemView { background: #142236; color: #e4eaf3; selection-background-color: #286de0; }
QCheckBox { spacing: 8px; }
QToolTip { color: #eef4ff; background: #17283f; border: 1px solid #3b5270; }
"""

STATUS_NAMES = {
    "queued": "排队中",
    "planning": "规划中",
    "running": "执行中",
    "awaiting_input": "等待补充",
    "succeeded": "已完成",
    "partially_succeeded": "部分完成",
    "failed": "未完成",
    "cancelled": "已取消",
    "interrupted": "已中断",
}


def _asset(relative: str) -> Path:
    try:
        from .paths import asset_path

        return asset_path(relative)
    except ImportError:
        return Path(__file__).resolve().parent.parent / relative


def _format_size(value) -> str:
    if not isinstance(value, (int, float)):
        return ""
    if value < 1024:
        return f"{value:.0f} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.1f} MB"


class VoiceTile(QWidget):
    """Compact voice bar, painted only when input or state actually changes."""

    def __init__(self, controller):
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.controller = controller
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(264, 56)
        self.setAccessibleName("JARVIS 生肖羊语音条")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setStyleSheet(STYLE)
        self.caption = "正在启动"
        self.detail = "正在准备本地助理"
        self.error = False
        self.listening = False
        self.audio_level = 0.0
        self._levels = deque([0.0] * 24, maxlen=24)
        self._drag_offset = None
        self._drag_start = None
        self._dragged = False
        ratio = max(1.0, self.devicePixelRatioF())
        pixmap = QPixmap(str(_asset("assets/branding/open-jarvis-sheep.png")))
        self.logo = QPixmap()
        if not pixmap.isNull():
            from PySide6.QtGui import QPainterPath

            # Clip once at construction; painting a live waveform never needs
            # to rescale or re-mask the branding asset.
            self.logo = QPixmap(round(40 * ratio), round(40 * ratio))
            self.logo.fill(Qt.GlobalColor.transparent)
            self.logo.setDevicePixelRatio(ratio)
            logo_painter = QPainter(self.logo)
            logo_painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            logo_painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            circle = QPainterPath()
            circle.addEllipse(0, 0, 40, 40)
            logo_painter.setClipPath(circle)
            logo_painter.drawPixmap(QRect(0, 0, 40, 40), pixmap)
            logo_painter.end()
        self.wave_button = QPushButton(self)
        self.wave_button.setGeometry(54, 6, 158, 44)
        self.wave_button.setCheckable(True)
        self.wave_button.setAutoDefault(False)
        self.wave_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.wave_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.wave_button.setAccessibleDescription(
            "声浪显示实际收音音量；按空格或点击可暂停和继续接听。"
        )
        self.wave_button.setStyleSheet(
            "QPushButton { background: transparent; border: 1px solid transparent; "
            "border-radius: 12px; padding: 0; }"
            "QPushButton:hover { background: rgba(88, 139, 204, 20); }"
            "QPushButton:pressed { background: rgba(88, 139, 204, 35); }"
            "QPushButton:focus { border-color: #719fd5; }"
        )
        self.wave_button.clicked.connect(self._toggle_listening)
        self.wave_button.installEventFilter(self)
        self.expand_button = QPushButton(self)
        icon = QPixmap(round(16 * ratio), round(16 * ratio))
        icon.fill(Qt.GlobalColor.transparent)
        icon.setDevicePixelRatio(ratio)
        arrow = QPainter(icon)
        arrow.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#dce9fa"), 1.7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        arrow.setPen(pen)
        arrow.drawLine(3, 13, 13, 3)
        arrow.drawLine(6, 3, 13, 3)
        arrow.drawLine(13, 3, 13, 10)
        arrow.end()
        self.expand_button.setIcon(QIcon(icon))
        self.expand_button.setIconSize(QSize(16, 16))
        self.expand_button.setGeometry(220, 10, 36, 36)
        self.expand_button.setAutoDefault(False)
        self.expand_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.expand_button.setAccessibleName("展开 JARVIS 任务与结果")
        self.expand_button.setToolTip("查看任务与结果")
        self.expand_button.setStyleSheet(
            "QPushButton { background: #1b304b; border: 1px solid #385779; "
            "border-radius: 18px; padding: 0; }"
            "QPushButton:hover { background: #294969; border-color: #6a94c2; }"
            "QPushButton:pressed { background: #32597e; }"
            "QPushButton:focus { border-color: #8bb9ec; }"
        )
        self.expand_button.clicked.connect(controller.show_results)
        self.expand_button.installEventFilter(self)
        self._sync_listening_action()
        self.setToolTip(
            self.detail + "\n点击声浪暂停/继续 · 展开查看结果 · 拖动移动 · 右键更多操作"
        )
        screen = QApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            self.move(
                area.right() - self.width() - 20, area.bottom() - self.height() - 20
            )

    def _redraw_if_visible(self):
        if self.isVisible():
            self.update()

    def _sync_listening_action(self):
        # A half-duplex TTS pause is different from the user's explicit pause.
        paused = bool(self.controller.user_paused)
        action = "继续聆听" if paused else "暂停聆听"
        if self.wave_button.isChecked() == paused:
            self.wave_button.setChecked(not paused)
        if self.wave_button.accessibleName() != action:
            self.wave_button.setAccessibleName(action)
            self.wave_button.setToolTip(action + " · 拖动移动语音条")

    def _toggle_listening(self):
        self.controller.toggle_pause()
        self._sync_listening_action()

    def set_state(self, caption: str, detail: str, *, error: bool = False):
        self._sync_listening_action()
        current = (caption, detail, error)
        if current == (self.caption, self.detail, self.error):
            return
        self.caption, self.detail, self.error = current
        if error:
            self.reset_audio_level()
        self.setToolTip(
            detail + "\n点击声浪暂停/继续 · 展开查看结果 · 拖动移动 · 右键更多操作"
        )
        self._redraw_if_visible()

    def set_listening(self, listening: bool):
        """The controller supplies the real microphone state, not task status."""
        self._sync_listening_action()
        listening = bool(listening)
        if self.listening == listening:
            return
        self.listening = listening
        if not listening:
            self.reset_audio_level()
        self._redraw_if_visible()

    def set_audio_level(self, level: float, speech: bool = True):
        """Append a real, normalized microphone level; no synthetic animation."""
        try:
            level = float(level)
        except (TypeError, ValueError):
            level = 0.0
        if (
            not math.isfinite(level)
            or not speech
            or not self.listening
            or self.error
            or not self.isVisible()
        ):
            level = 0.0
        level = round(max(0.0, min(1.0, level)) * 32) / 32
        if not level:
            self.reset_audio_level()
            return
        before = tuple(self._levels)
        self.audio_level = level
        self._levels.append(level)
        if tuple(self._levels) != before:
            self._redraw_if_visible()

    def reset_audio_level(self):
        """Pause, silence, errors and hidden windows have a static baseline."""
        if not self.audio_level and not any(self._levels):
            return
        self.audio_level = 0.0
        self._levels.clear()
        self._levels.extend([0.0] * 24)
        self._redraw_if_visible()

    def hideEvent(self, event):
        self.reset_audio_level()
        super().hideEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#d99c4e" if self.error else "#335887"), 1))
        painter.setBrush(QColor(11, 20, 34, 246))
        painter.drawRoundedRect(1, 1, 262, 54, 27, 27)
        if not self.logo.isNull():
            painter.drawPixmap(QRect(8, 8, 40, 40), self.logo)
        else:
            painter.setPen(QColor("#b4cfff"))
            painter.drawText(QRect(8, 8, 40, 40), Qt.AlignmentFlag.AlignCenter, "J")
        painter.setPen(Qt.PenStyle.NoPen)
        color = "#edb263" if self.error else "#5fa0ff" if self.listening else "#69809c"
        painter.setBrush(QColor(color))
        painter.drawEllipse(61, 13, 4, 4)
        painter.setPen(QColor("#dce9fa"))
        font = painter.font()
        font.setPointSize(9)
        painter.setFont(font)
        text = painter.fontMetrics().elidedText(
            self.caption, Qt.TextElideMode.ElideRight, 136
        )
        painter.drawText(QRect(72, 6, 136, 20), Qt.AlignmentFlag.AlignVCenter, text)
        painter.setPen(Qt.PenStyle.NoPen)
        for index, level in enumerate(self._levels):
            height = 2 + round(16 * level)
            painter.setBrush(QColor("#72b4ff" if level else "#3c526e"))
            painter.drawRoundedRect(61 + index * 6, 38 - height // 2, 3, height, 1, 1)

    def _begin_drag(self, event):
        self._drag_start = event.globalPosition().toPoint()
        self._drag_offset = self._drag_start - self.pos()
        self._dragged = False
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def _move_drag(self, event):
        if (
            self._drag_offset is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            point = event.globalPosition().toPoint()
            if (
                point - self._drag_start
            ).manhattanLength() > QApplication.startDragDistance():
                self._dragged = True
            if self._dragged:
                self.move(point - self._drag_offset)
        return self._dragged

    def _finish_drag(self):
        click = self._drag_offset is not None and not self._dragged
        moved = self._dragged
        self._drag_offset = None
        self._drag_start = None
        self._dragged = False
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        if moved:
            self.controller.save_tile_position(self.x(), self.y())
        return click, moved

    def eventFilter(self, watched, event):
        if watched in (self.expand_button, self.wave_button):
            kind = event.type()
            if (
                kind == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
            ):
                self._begin_drag(event)
            elif kind == QEvent.Type.MouseMove and self._move_drag(event):
                watched.setDown(False)
                return True
            elif (
                kind == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
            ):
                _, moved = self._finish_drag()
                if moved:
                    watched.setDown(False)
                    return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._begin_drag(event)

    def mouseMoveEvent(self, event):
        self._move_drag(event)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        click, _ = self._finish_drag()
        if click:
            if self.wave_button.geometry().contains(event.position().toPoint()):
                self._toggle_listening()
            else:
                self.controller.show_results()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction(
            "继续聆听" if self.controller.user_paused else "暂停聆听",
            self.controller.toggle_pause,
        )
        speech = menu.addAction("朗读任务结果")
        speech.setCheckable(True)
        speech.setChecked(self.controller.speech_enabled)
        speech.toggled.connect(self.controller.toggle_speech)
        stop = menu.addAction("停止播报", self.controller.stop_speaking)
        stop.setEnabled(bool(self.controller.speaking))
        menu.addSeparator()
        menu.addAction("查看任务与结果", self.controller.show_results)
        menu.addAction("设置", self.controller.show_settings)
        menu.addSeparator()
        menu.addAction("退出 JARVIS", self.controller.request_quit)
        menu.exec(event.globalPos())


class SafeTextBrowser(QTextBrowser):
    """Render generated reports without loading remote or arbitrary local data."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.anchorClicked.connect(self.open_source)
        self.document().setDefaultStyleSheet(
            "body { line-height: 1.5; } a { color: #6ea8ff; } pre { white-space: pre-wrap; }"
        )

    def loadResource(self, resource_type, name):
        # No embedded images, styles or file reads from model-generated content.
        if resource_type == QTextDocument.ResourceType.ImageResource:
            return QPixmap()
        return None

    @staticmethod
    def open_source(url: QUrl):
        if url.scheme().lower() in {"http", "https"} and url.host():
            QDesktopServices.openUrl(url)


class MessageInput(QPlainTextEdit):
    submitted = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            event.accept()
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class ResultsWindow(QMainWindow):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.task_id = None
        self._event_ids = set()
        self._event_lines = deque(maxlen=100)
        self._body_signature = None
        self._artifacts_signature = None
        self._events_snapshot_signature = None
        self._events_rendered_text = ""
        self._auto_preview_task = None
        self.setWindowTitle("Open JARVIS · 工作结果")
        self.resize(1280, 840)
        self.setStyleSheet(STYLE)
        outer = QWidget()
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        top = QHBoxLayout()
        heading = QLabel("JARVIS  /  工作结果")
        heading.setObjectName("heading")
        top.addWidget(heading, 1)
        for label, callback in [
            ("导入文件", controller.upload_file),
            ("刷新任务", controller.refresh_tasks),
            ("设置", controller.show_settings),
            ("收起  Esc", self.hide),
        ]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            top.addWidget(button)
        layout.addLayout(top)
        self.status = QLabel("从常驻方块说出任务，或在下方输入文字。")
        self.status.setObjectName("status")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        sidebar = QWidget()
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(0, 0, 12, 0)
        label = QLabel("最近任务")
        label.setObjectName("eyebrow")
        side_layout.addWidget(label)
        self.tasks = QListWidget()
        self.tasks.setAccessibleName("最近任务列表")
        self.tasks.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tasks.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.tasks.setWordWrap(False)
        self.tasks.currentItemChanged.connect(self._selected_task)
        side_layout.addWidget(self.tasks, 1)
        self.tasks.setMinimumWidth(200)
        self.tasks.setMaximumWidth(340)
        splitter.addWidget(sidebar)
        report = QWidget()
        report_layout = QVBoxLayout(report)
        report_layout.setContentsMargins(12, 0, 0, 0)
        self.title = QLabel("把下一件事交给 JARVIS")
        self.title.setObjectName("heading")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        self.title.setMinimumWidth(0)
        self.title.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        report_layout.addWidget(self.title)
        action_row = QHBoxLayout()
        self.task_state = QLabel("尚未选择任务")
        self.task_state.setObjectName("muted")
        action_row.addWidget(self.task_state, 1)
        self.cancel_button = QPushButton("取消任务")
        self.cancel_button.clicked.connect(
            lambda: self.controller.cancel_task(self.task_id) if self.task_id else None
        )
        self.cancel_button.setEnabled(False)
        self.resume_button = QPushButton("恢复任务")
        self.resume_button.clicked.connect(
            lambda: self.controller.resume_task(self.task_id) if self.task_id else None
        )
        self.resume_button.setVisible(False)
        action_row.addWidget(self.resume_button)
        action_row.addWidget(self.cancel_button)
        report_layout.addLayout(action_row)
        self.body = SafeTextBrowser()
        self.body.setAccessibleName("任务结果正文")
        self.body.setMarkdown(
            "## 你好，我是 JARVIS\n\n检索资料、分析 CSV、撰写工作文档。\n\n任务会在后台执行，完成后在这里展示实际结果与可下载的文件。"
        )
        self.content_tabs = QTabWidget()
        self.content_tabs.addTab(self.body, "任务结果")
        from .preview_ui import ArtifactPreviewWidget

        self.preview = ArtifactPreviewWidget()
        self.preview.download_requested.connect(controller.download_artifact)
        self.preview.play_requested.connect(controller.play_artifact)
        self.preview_tab = self.content_tabs.addTab(self.preview, "产物预览")
        report_layout.addWidget(self.content_tabs, 1)
        self.artifacts_area = QWidget()
        self.artifacts_layout = QHBoxLayout(self.artifacts_area)
        self.artifacts_layout.setContentsMargins(0, 0, 0, 0)
        self.artifacts_layout.addStretch(1)
        self.artifacts_area.hide()
        report_layout.addWidget(self.artifacts_area)
        self.events_toggle = QPushButton("查看执行记录")
        self.events_toggle.setAccessibleName("展开或收起执行记录")
        self.events_toggle.clicked.connect(self._toggle_events)
        report_layout.addWidget(self.events_toggle)
        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.events.setMaximumBlockCount(100)
        self.events.setMaximumHeight(150)
        self.events.setAccessibleName("任务执行记录")
        self.events.hide()
        report_layout.addWidget(self.events)
        splitter.addWidget(report)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 900])
        layout.addWidget(splitter, 1)
        composer = QHBoxLayout()
        self.input = MessageInput()
        self.input.setPlaceholderText(
            "告诉 JARVIS 你要完成什么；Enter 发送，Shift + Enter 换行"
        )
        self.input.setAccessibleName("任务或补充说明")
        self.input.setMaximumHeight(100)
        self.input.setMinimumHeight(68)
        self.input.submitted.connect(self._submit)
        composer.addWidget(self.input, 1)
        self.send_button = QPushButton("发送任务")
        self.send_button.setObjectName("primary")
        self.send_button.setMinimumHeight(50)
        self.send_button.clicked.connect(self._submit)
        composer.addWidget(self.send_button)
        layout.addLayout(composer)
        hint = QLabel("文字和文件在本机保存；是否使用云端任务模型由你的设置决定。")
        hint.setObjectName("muted")
        layout.addWidget(hint)
        self.setCentralWidget(outer)
        self.escape = QShortcut(QKeySequence("Escape"), self)
        self.escape.activated.connect(self.hide)

    def _submit(self):
        text = self.input.toPlainText().strip()
        if not text:
            return
        if len(text) > 12000:
            self.set_status("任务文字不能超过 12,000 个字符，请缩短后再发送。")
            return
        if self.controller.submit(text) is not False:
            self.input.clear()

    def _selected_task(self, current, previous):
        if current:
            self.controller.select_task(current.data(Qt.ItemDataRole.UserRole))

    def set_status(self, text: str):
        self.status.setText(str(text)[:2000])

    def render_tasks(self, tasks: list[dict]):
        selected = self.task_id
        self.tasks.blockSignals(True)
        self.tasks.clear()
        for task in tasks[:100]:
            task_id = task.get("task_id", task.get("id"))
            if task_id is None:
                continue
            status = STATUS_NAMES.get(task.get("status"), str(task.get("status", "")))
            title = str(
                task.get("title")
                or task.get("message")
                or task.get("instruction")
                or "任务"
            ).replace("\n", " ")
            if len(title) > 42:
                title = title[:41] + "…"
            item = QListWidgetItem(f"{title}\n{status}")
            item.setData(Qt.ItemDataRole.UserRole, task_id)
            item.setToolTip(
                str(task.get("message") or task.get("instruction") or title)[:1000]
            )
            self.tasks.addItem(item)
            if task_id == selected:
                self.tasks.setCurrentItem(item)
        self.tasks.blockSignals(False)

    def render_task(self, task: dict):
        incoming_id = task.get("task_id", task.get("id"))
        changed = incoming_id != self.task_id
        self.task_id = incoming_id
        title = str(
            task.get("title")
            or task.get("message")
            or task.get("instruction")
            or "任务结果"
        ).replace("\n", " ")
        self.title.setText(title[:180])
        status = str(task.get("status", ""))
        self.task_state.setText(STATUS_NAMES.get(status, status))
        self.cancel_button.setEnabled(
            status in {"queued", "planning", "running", "awaiting_input"}
        )
        self.resume_button.setVisible(bool(task.get("held")) and status == "queued")
        self.send_button.setText(
            "补充说明" if status == "awaiting_input" else "发送任务"
        )
        content = (
            task.get("display_text") or task.get("answer") or task.get("result") or ""
        )
        if isinstance(content, dict):
            content = content.get("display_text") or content.get("content") or ""
        if not content:
            if status == "awaiting_input":
                content = task.get("question") or "请在下方输入补充说明。"
            elif status in {"running", "planning", "queued"}:
                content = "任务正在后台处理。可以收起窗口，完成后会通知你。"
            else:
                content = task.get("error") or "这项任务暂无正文，请查看执行记录。"
        content = str(content)
        if status == "interrupted":
            content += "\n\n上次执行已中断，请检查产物再重新安排任务；不会自动重复已经发生的操作。"
        if len(content) > 100000:
            content = (
                content[:100000]
                + "\n\n---\n正文较长，当前仅展示前 100,000 个字符。完整内容请下载产物。"
            )
        if content != self._body_signature:
            previous_scroll = self.body.verticalScrollBar().value()
            self.body.setMarkdown(content)
            self._body_signature = content
            self.body.verticalScrollBar().setValue(0 if changed else previous_scroll)
        if changed:
            self._auto_preview_task = None
            self.content_tabs.setCurrentIndex(0)
            self.preview.loading(None, "产物内容预览")
            self.preview.placeholder.setText("选择产物查看实际文件内容。")
            self.body.verticalScrollBar().setValue(0)
            self._event_lines.clear()
            self._event_ids.clear()
            self._events_snapshot_signature = None
        artifacts = self._set_artifacts(task.get("artifacts") or [])
        if (
            status in {"succeeded", "partially_succeeded", "cancelled", "failed"}
            and artifacts
            and self._auto_preview_task != self.task_id
        ):
            self._auto_preview_task = self.task_id
            first = next((item for item in artifacts if self._can_preview(item)), None)
            if first is not None:
                self.controller.preview_artifact(
                    first.get("artifact_id", first.get("id")),
                    Path(str(first.get("path") or first.get("name") or "产物")).name,
                    automatic=True,
                    task_id=self.task_id,
                )
        if "events" in task:
            events = [
                event
                for event in (task.get("events") or [])[-100:]
                if not event.get("task_id") or event.get("task_id") == self.task_id
            ]
            signature = tuple(
                (event.get("event_id"), self._event_line(event)) for event in events
            )
            if signature != self._events_snapshot_signature:
                self._events_snapshot_signature = signature
                self._event_lines = deque((line for _, line in signature), maxlen=100)
                self._event_ids = {
                    event_id for event_id, _ in signature if event_id is not None
                }
        self._sync_events()

    @staticmethod
    def _can_preview(item):
        return Path(str(item.get("path") or item.get("name") or "")).suffix.lower() in {
            ".docx",
            ".xlsx",
            ".pptx",
            ".mp4",
            ".md",
            ".txt",
            ".csv",
            ".html",
        }

    def show_preview_loading(self, artifact_id, name):
        self.preview.loading(artifact_id, name)
        self.content_tabs.setCurrentIndex(self.preview_tab)

    def show_preview(self, payload):
        self.preview.render(payload)
        self.content_tabs.setCurrentIndex(self.preview_tab)

    def _set_artifacts(self, artifacts):
        artifacts = [
            item
            for item in artifacts
            if not item.get("hidden")
            and not str(item.get("path", "")).lower().endswith(".preview.json")
        ]
        signature = tuple(
            (
                item.get("artifact_id", item.get("id")),
                str(item.get("path") or item.get("name") or "下载文件"),
                item.get("size"),
            )
            for item in artifacts
        )
        if signature == self._artifacts_signature:
            return artifacts
        self._artifacts_signature = signature
        while self.artifacts_layout.count():
            item = self.artifacts_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for artifact in artifacts[:1]:
            artifact_id = artifact.get("artifact_id", artifact.get("id"))
            if artifact_id is None:
                continue
            path = str(artifact.get("path") or artifact.get("name") or "下载文件")
            name = Path(path).name
            previewable = self._can_preview(artifact)
            label = "预览 " if previewable else "↓ "
            button = QPushButton()
            button.setText(
                button.fontMetrics().elidedText(
                    label + name, Qt.TextElideMode.ElideMiddle, 190
                )
            )
            button.setMinimumWidth(0)
            button.setMaximumWidth(220)
            button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            button.setToolTip(path + "\n" + _format_size(artifact.get("size")))
            if previewable:
                button.clicked.connect(
                    lambda checked=False, aid=artifact_id, filename=name: (
                        self.controller.preview_artifact(
                            aid, filename, task_id=self.task_id
                        )
                    )
                )
            else:
                button.clicked.connect(
                    lambda checked=False, aid=artifact_id, filename=name: (
                        self.controller.download_artifact(aid, filename)
                    )
                )
            self.artifacts_layout.addWidget(button)
        if len(artifacts) > 1:
            more = QPushButton(f"更多产物（{len(artifacts) - 1}）")
            more.setMaximumWidth(140)
            menu = QMenu(more)
            for artifact in artifacts[1:]:
                artifact_id = artifact.get("artifact_id", artifact.get("id"))
                if artifact_id is None:
                    continue
                path = str(artifact.get("path") or artifact.get("name") or "下载文件")
                name = Path(path).name
                previewable = self._can_preview(artifact)
                action = menu.addAction(("预览 " if previewable else "下载 ") + name)
                action.setToolTip(path)
                if previewable:
                    action.triggered.connect(
                        lambda checked=False, aid=artifact_id, filename=name: (
                            self.controller.preview_artifact(
                                aid, filename, task_id=self.task_id
                            )
                        )
                    )
                else:
                    action.triggered.connect(
                        lambda checked=False, aid=artifact_id, filename=name: (
                            self.controller.download_artifact(aid, filename)
                        )
                    )
            more.setMenu(menu)
            self.artifacts_layout.addWidget(more)
        self.artifacts_layout.addStretch(1)
        self.artifacts_area.setVisible(bool(artifacts))
        return artifacts

    def append_event(self, envelope: dict):
        task_id = envelope.get("task_id")
        if task_id and self.task_id and task_id != self.task_id:
            return
        event_id = envelope.get("event_id")
        if event_id is not None and event_id in self._event_ids:
            return
        if event_id is not None:
            self._event_ids.add(event_id)
            if len(self._event_ids) > 300:
                self._event_ids = {event_id}
        self._event_lines.append(self._event_line(envelope))
        if self.events.isVisible():
            self._sync_events()

    @staticmethod
    def _event_line(envelope: dict) -> str:
        event_type = str(envelope.get("type", "事件"))
        data = envelope.get("data") or {}
        message = (
            str(data.get("message") or data.get("name") or data.get("status") or "")
            if isinstance(data, dict)
            else ""
        )
        return f"{event_type}  {message[:250]}"

    def _sync_events(self):
        text = "\n".join(self._event_lines)
        if text == self._events_rendered_text:
            return
        scrollbar = self.events.verticalScrollBar()
        previous = scrollbar.value()
        following = previous >= scrollbar.maximum() - 2
        self.events.setPlainText(text)
        self._events_rendered_text = text
        scrollbar.setValue(scrollbar.maximum() if following else previous)

    def _toggle_events(self):
        showing = not self.events.isVisible()
        self.events.setVisible(showing)
        self.events_toggle.setText("收起执行记录" if showing else "查看执行记录")
        if showing:
            self._sync_events()

    def show_result(self):
        # Native maximization keeps macOS in the current desktop Space.
        # Never request Qt's full-screen state when opening a result.
        if self.isFullScreen():
            self.showNormal()
        self.showMaximized()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        event.accept()
        self.hide()


class SettingsDialog(QDialog):
    """Settings are passed back to controller; this widget does not save secrets."""

    model_download_requested = Signal(str)
    model_cancel_requested = Signal(str)

    def __init__(self, preferences: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("JARVIS 设置")
        self.setStyleSheet(STYLE)
        self.resize(650, 700)
        self.setStyleSheet(
            STYLE
            + """
            QLabel#modelStatus { color: #8bb9ff; }
            QPushButton#primary:disabled { background: #1b2b40; border-color: #2c4059; color: #708198; }
            QProgressBar { border: 1px solid #314963; border-radius: 5px; text-align: center; min-height: 21px; background: #0b1726; }
            QProgressBar::chunk { background: #285b9d; border-radius: 4px; }
            QScrollArea { border: none; }
            """
        )
        self._preferences = dict(preferences)
        self.fields = {}
        layout = QVBoxLayout(self)
        heading = QLabel("连接模型与语音")
        heading.setObjectName("heading")
        layout.addWidget(heading)
        description = QLabel(
            "设置保存到本机。服务配置需要重新启动本地服务后生效；执行中的任务结束后再应用。"
        )
        description.setWordWrap(True)
        description.setObjectName("muted")
        layout.addWidget(description)
        from .model_manager_ui import ModelDownloadRow

        self.model_rows = {}
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 10, 0)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        specs = [
            ("OPENAI_BASE_URL", "模型接口", "https://api.openai.com/v1"),
            ("OPENAI_MODEL", "模型名称", "gpt-4o-mini"),
            ("OPENAI_API_KEY", "模型 API Key", ""),
            ("JARVIS_STT_MODEL_DIR", "语音识别模型", ""),
            ("JARVIS_TTS_MODEL_DIR", "语音合成模型", ""),
            ("JARVIS_WAKE_WORD", "指令前缀", "贾维斯"),
            ("JARVIS_MIC_DEVICE", "麦克风设备", ""),
        ]
        for key, label, default in specs:
            if key.endswith("MODEL_DIR"):
                kind = "stt" if key == "JARVIS_STT_MODEL_DIR" else "tts"
                row = ModelDownloadRow(kind)
                row.download_requested.connect(self.model_download_requested.emit)
                row.cancel_requested.connect(self.model_cancel_requested.emit)
                self.model_rows[kind] = row
                form.addRow(label, row)
                continue
            field = QLineEdit(str(preferences.get(key, default) or ""))
            field.setAccessibleName(label)
            if key == "OPENAI_API_KEY":
                field.setEchoMode(QLineEdit.EchoMode.Password)
                field.setPlaceholderText("仅保存在本机配置中")
            elif key == "JARVIS_WAKE_WORD":
                field.setPlaceholderText("留空则直接执行每段有效语音")
            elif key == "JARVIS_MIC_DEVICE":
                field.setPlaceholderText("留空使用系统默认设备，或输入设备索引")
            self.fields[key] = field
            form.addRow(label, field)
        self.speech = QCheckBox("朗读任务结果")
        self.speech.setChecked(bool(preferences.get("UI_SPEECH_ENABLED", True)))
        form.addRow("语音反馈", self.speech)
        self.fullscreen = QCheckBox("任务结束后自动最大化展示结果")
        self.fullscreen.setChecked(bool(preferences.get("UI_AUTO_FULLSCREEN", True)))
        form.addRow("结果窗口", self.fullscreen)
        content_layout.addLayout(form)
        content_layout.addStretch(1)
        hint = QLabel(
            "语音模型由应用下载和保存，来源不可用时自动切换。关闭设置不会中断下载，完成后自动加载。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        content_layout.addWidget(hint)
        self.scroll.setWidget(content)
        layout.addWidget(self.scroll, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存并应用")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def update_model(self, kind: str, status: dict):
        if kind not in self.model_rows:
            raise ValueError("Unknown speech model")
        if not isinstance(status, dict):
            raise TypeError("Model status must be a mapping")
        self.model_rows[kind].update_snapshot(status)

    def values(self) -> dict:
        result = dict(self._preferences)
        result.update({key: field.text().strip() for key, field in self.fields.items()})
        result["UI_SPEECH_ENABLED"] = self.speech.isChecked()
        result["UI_AUTO_FULLSCREEN"] = self.fullscreen.isChecked()
        return result
