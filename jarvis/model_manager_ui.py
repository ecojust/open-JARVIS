"""On-demand native model downloads view; no network, disk scans or timers."""

from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


_BUSY = frozenset(
    {"checking", "downloading", "switching", "verifying", "installing", "cancelling"}
)
_PHASE_NAMES = {
    "checking": "正在检查",
    "missing": "尚未下载",
    "incompatible": "需要兼容版本",
    "downloading": "正在下载",
    "switching": "正在切换来源",
    "verifying": "正在校验",
    "installing": "正在安装",
    "ready": "已就绪",
    "failed": "下载未完成",
    "cancelled": "已取消",
    "cancelling": "正在取消",
}
_MODEL_NAMES = {
    "stt": ("SenseVoice", "将你的语音识别为文字，在本机运行。"),
    "tts": ("Kokoro", "为 JARVIS 生成中文语音反馈，在本机运行。"),
}


def _text(value, maximum=500):
    return str(value or "")[:maximum]


def _number(value):
    if type(value) not in (int, float):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return max(0, min(value, 10**13))


def _size(value):
    value = _number(value)
    if not value:
        return ""
    if value >= 1024 * 1024 * 1024:
        return f"{value / 1024**3:.2f} GiB"
    if value >= 1024 * 1024:
        return f"{value / 1024**2:.1f} MiB"
    return f"{value / 1024:.1f} KiB"


class ModelCard(QFrame):
    """Render one controller-owned snapshot; actions never run downloads here."""

    download_requested = Signal(str)
    cancel_requested = Signal(str)

    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.snapshot = {"phase": "checking"}
        self.setObjectName("modelCard")
        name, description = _MODEL_NAMES[kind]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(8)
        title_row = QHBoxLayout()
        self.name_label = QLabel(name)
        self.name_label.setTextFormat(Qt.TextFormat.PlainText)
        self.name_label.setWordWrap(True)
        self.name_label.setObjectName("modelName")
        title_row.addWidget(self.name_label, 1)
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setObjectName("modelStatus")
        title_row.addWidget(self.status_label)
        layout.addLayout(title_row)
        purpose = QLabel(description)
        purpose.setWordWrap(True)
        purpose.setObjectName("muted")
        layout.addWidget(purpose)
        self.version_label = QLabel()
        self.version_label.setTextFormat(Qt.TextFormat.PlainText)
        self.version_label.setWordWrap(True)
        self.version_label.setObjectName("muted")
        layout.addWidget(self.version_label)
        self.progress = QProgressBar()
        self.progress.setAccessibleName(name + " 下载进度")
        self.progress.setRange(0, 1000)
        self.progress.setFormat("%p%")
        layout.addWidget(self.progress)
        self.source_label = QLabel()
        self.source_label.setTextFormat(Qt.TextFormat.PlainText)
        self.source_label.setWordWrap(True)
        self.source_label.setObjectName("muted")
        layout.addWidget(self.source_label)
        self.detail_label = QLabel()
        self.detail_label.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_label.setWordWrap(True)
        self.detail_label.setObjectName("muted")
        layout.addWidget(self.detail_label)
        self.path_field = QLineEdit()
        self.path_field.setReadOnly(True)
        self.path_field.setAccessibleName(name + " 模型保存位置")
        self.path_field.setPlaceholderText("下载完成后显示本机保存位置")
        self.path_field.setToolTip("模型保存位置，可选择复制")
        layout.addWidget(self.path_field)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.cancel_button = QPushButton("取消下载")
        self.cancel_button.setAccessibleName("取消 " + name + " 下载")
        self.cancel_button.clicked.connect(self._cancel)
        actions.addWidget(self.cancel_button)
        self.download_button = QPushButton("下载模型")
        self.download_button.setObjectName("primary")
        self.download_button.setAccessibleName("下载或校验 " + name)
        self.download_button.clicked.connect(self._download)
        actions.addWidget(self.download_button)
        layout.addLayout(actions)
        self.update_snapshot({})

    def _download(self):
        # The controller normally supplies the next snapshot synchronously;
        # disabling here also prevents repeated clicks before that response.
        self.download_button.setEnabled(False)
        self.download_requested.emit(self.kind)

    def _cancel(self):
        self.cancel_button.setEnabled(False)
        self.cancel_requested.emit(self.kind)

    def update_snapshot(self, values):
        if (
            "phase" in values
            and values["phase"] != self.snapshot.get("phase")
            and "message" not in values
        ):
            self.snapshot.pop("message", None)
        self.snapshot.update(values)
        phase = self.snapshot.get("phase", "checking")
        known = phase in _PHASE_NAMES
        busy = phase in _BUSY or not known
        self.status_label.setText(_PHASE_NAMES.get(phase, "状态待确认"))
        name = _text(self.snapshot.get("display_name"), 100)
        if name:
            self.name_label.setText(name)
        metadata = []
        version = _text(
            self.snapshot.get("display_version") or self.snapshot.get("version"), 100
        )
        if version:
            metadata.append("版本 " + version)
        size = _size(self.snapshot.get("size_bytes"))
        if size:
            metadata.append("下载大小 " + size)
        self.version_label.setText(" · ".join(metadata))
        self.version_label.setVisible(bool(metadata))
        directory = _text(self.snapshot.get("directory"), 2000)
        self.path_field.setText(directory)
        self.path_field.setCursorPosition(0)
        self.path_field.setVisible(bool(directory))
        completed = _number(self.snapshot.get("completed_bytes"))
        total = _number(self.snapshot.get("total_bytes"))
        if busy and phase in {"downloading", "switching"} and total:
            self.progress.setRange(0, 1000)
            self.progress.setValue(round(1000 * min(completed, total) / total))
            self.progress.setFormat(
                f"{_size(completed) or '0 MiB'} / {_size(total)} · %p%"
            )
        elif busy:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(1000 if phase == "ready" else 0)
            self.progress.setFormat("已完成" if phase == "ready" else "%p%")
        self.progress.setVisible(busy or phase == "ready")
        source = _text(self.snapshot.get("source"), 100)
        source_index = int(_number(self.snapshot.get("source_index")))
        source_count = int(_number(self.snapshot.get("source_count")))
        source_text = "下载来源：" + source
        if 0 < source_index <= source_count <= 100:
            source_text += f"（{source_index}/{source_count}）"
        self.source_label.setText(source_text)
        self.source_label.setVisible(
            bool(source) and phase in {"downloading", "switching", "verifying"}
        )
        fallback = {
            "checking": "正在确认本机模型状态…",
            "missing": "点击下载后即可使用，不需要运行安装命令。",
            "incompatible": "当前模型与此版本不兼容，请下载受支持的版本。旧文件会保留。",
            "downloading": "下载期间可以继续使用文字任务。",
            "switching": "当前来源暂不可用，正在自动尝试其他来源。",
            "verifying": "正在校验下载文件完整性，请稍候。",
            "installing": "正在准备模型，完成后自动加载。",
            "ready": "已安装；升级客户端后会检查兼容性并复用。",
            "failed": "下载未完成，可以重试。已安装的可用模型会保留。",
            "cancelled": "已停止下载，随时可以重新开始。",
            "cancelling": "正在停止下载，请稍候。",
        }
        self.detail_label.setText(
            _text(self.snapshot.get("message"), 800)
            or fallback.get(phase, "模型状态暂不可用，请稍后重试。")
        )
        label = {
            "ready": "校验模型",
            "incompatible": "下载兼容版本",
            "failed": "重试下载",
            "cancelled": "重试下载",
        }.get(phase, "下载模型")
        self.download_button.setText(label)
        self.download_button.setEnabled(not busy)
        self.cancel_button.setVisible(busy)
        self.cancel_button.setEnabled(known and phase != "cancelling")


class ModelManagerDialog(QDialog):
    """A lazy view. Closing it keeps controller-owned downloads running."""

    download_requested = Signal(str)
    cancel_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        from .desktop_ui import STYLE

        self.setWindowTitle("JARVIS 语音模型")
        self.setStyleSheet(
            STYLE
            + """
            QFrame#modelCard { background: #111e30; border: 1px solid #2c415e; border-radius: 10px; }
            QFrame#modelCard QLabel { background: transparent; }
            QLabel#modelName { font-size: 19px; font-weight: 600; }
            QLabel#modelStatus { color: #8bb9ff; }
            QPushButton#primary:disabled { background: #1b2b40; border-color: #2c4059; color: #708198; }
            QProgressBar { border: 1px solid #314963; border-radius: 5px; text-align: center; min-height: 22px; background: #0b1726; }
            QProgressBar::chunk { background: #285b9d; border-radius: 4px; }
            QScrollArea { border: none; }
            """
        )
        self.resize(610, 690)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        heading = QLabel("下载语音模型")
        heading.setObjectName("heading")
        layout.addWidget(heading)
        introduction = QLabel(
            "分别下载语音识别与中文播报模型，完成后自动加载。来源不可用时自动切换。"
        )
        introduction.setWordWrap(True)
        introduction.setObjectName("muted")
        layout.addWidget(introduction)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 4, 0, 4)
        body_layout.setSpacing(12)
        self.cards = {}
        for kind in ("stt", "tts"):
            card = ModelCard(kind, body)
            card.download_requested.connect(self.download_requested.emit)
            card.cancel_requested.connect(self.cancel_requested.emit)
            self.cards[kind] = card
            body_layout.addWidget(card)
        body_layout.addStretch(1)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        self.persistence_note = QLabel(
            "已下载模型保存在用户目录，升级客户端后会检查兼容性并复用；"
            "不同模型版本独立保存。关闭此窗口不会中断下载。"
        )
        self.persistence_note.setObjectName("muted")
        self.persistence_note.setWordWrap(True)
        layout.addWidget(self.persistence_note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def update_model(self, kind: str, status: dict):
        if kind not in self.cards:
            raise ValueError("Unknown speech model")
        if not isinstance(status, dict):
            raise TypeError("Model status must be a mapping")
        self.cards[kind].update_snapshot(status)
