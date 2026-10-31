"""On-demand native model downloads view; no network, disk scans or timers."""

from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
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


class ModelDownloadRow(QWidget):
    """Render one controller-owned snapshot; actions never run downloads here."""

    download_requested = Signal(str)
    cancel_requested = Signal(str)

    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.snapshot = {"phase": "checking"}
        self.setObjectName("modelDownloadRow")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        name, _ = _MODEL_NAMES[kind]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.model_field = QLineEdit(name)
        self.model_field.setReadOnly(True)
        self.model_field.setAcceptDrops(False)
        self.model_field.setAccessibleName(name + " 模型")
        row.addWidget(self.model_field, 1)
        self.download_button = QPushButton("下载")
        self.download_button.setObjectName("primary")
        self.download_button.setAutoDefault(False)
        self.download_button.setAccessibleName("下载或校验 " + name)
        self.download_button.clicked.connect(self._download)
        row.addWidget(self.download_button)
        self.cancel_button = QPushButton("取消下载")
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.setAccessibleName("取消 " + name + " 下载")
        self.cancel_button.clicked.connect(self._cancel)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)
        state_row = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setObjectName("modelStatus")
        state_row.addWidget(self.status_label)
        self.version_label = QLabel()
        self.version_label.setTextFormat(Qt.TextFormat.PlainText)
        self.version_label.setWordWrap(True)
        self.version_label.setObjectName("muted")
        state_row.addWidget(self.version_label, 1)
        layout.addLayout(state_row)
        self.progress = QProgressBar()
        self.progress.setAccessibleName(name + " 下载进度")
        self.progress.setRange(0, 1000)
        self.progress.setFormat("%p%")
        layout.addWidget(self.progress)
        self.eta_label = QLabel()
        self.eta_label.setTextFormat(Qt.TextFormat.PlainText)
        self.eta_label.setObjectName("muted")
        self.eta_label.setWordWrap(True)
        layout.addWidget(self.eta_label)
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
        if values.get("phase") == "switching":
            self.snapshot.pop("eta_seconds", None)
        self.snapshot.update(values)
        phase = self.snapshot.get("phase", "checking")
        known = phase in _PHASE_NAMES
        busy = phase in _BUSY or not known
        completed = _number(self.snapshot.get("completed_bytes"))
        total = _number(self.snapshot.get("total_bytes"))
        file_verification = (
            phase == "verifying"
            and bool(_text(self.snapshot.get("filename")))
            and completed < total
        )
        transfer = phase in {"downloading", "switching"} or file_verification
        self.status_label.setText(_PHASE_NAMES.get(phase, "状态待确认"))
        # Keep the download row still when a short file verification temporarily
        # hides its network ETA. Releasing these slots at the end keeps idle
        # settings compact; an absent estimate must not become a fake countdown.
        for label in (self.eta_label, self.source_label, self.detail_label):
            policy = label.sizePolicy()
            if policy.retainSizeWhenHidden() != busy:
                policy.setRetainSizeWhenHidden(busy)
                label.setSizePolicy(policy)
            # Chinese fallback glyphs can be taller than the base font metrics.
            # The visible status label measures the actual styled line height.
            label.setMinimumHeight(
                max(label.fontMetrics().height(), self.status_label.sizeHint().height())
                if busy else 0
            )
        name = _text(self.snapshot.get("display_name"), 100)
        if name:
            self.model_field.setText(name)
        metadata = []
        version = _text(
            self.snapshot.get("display_version") or self.snapshot.get("version"), 100
        )
        if version:
            metadata.append("版本 " + version)
        transfer_size = _number(self.snapshot.get("total_bytes"))
        size = _size(
            transfer_size
            if transfer and transfer_size
            else self.snapshot.get("size_bytes")
        )
        if size:
            metadata.append("下载大小 " + size)
        self.version_label.setText(" · ".join(metadata))
        self.version_label.setVisible(bool(metadata))
        if busy and transfer and total:
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
        self.progress.setVisible(busy)
        eta = self.snapshot.get("eta_seconds")
        if phase in {"downloading", "switching"}:
            if (
                phase == "downloading"
                and type(eta) in (int, float)
                and math.isfinite(eta)
                and eta >= 0
            ):
                seconds = min(math.ceil(eta), 7 * 24 * 3600)
                hours, remainder = divmod(seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                duration = (
                    f"{hours} 小时 {minutes} 分"
                    if hours
                    else f"{minutes} 分 {seconds} 秒"
                    if minutes
                    else f"{seconds} 秒"
                )
                self.eta_label.setText("预计剩余 " + duration)
            else:
                self.eta_label.setText("正在估算剩余时间…")
        else:
            self.eta_label.clear()
        self.eta_label.setVisible(phase in {"downloading", "switching"})
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
        self.detail_label.setVisible(phase not in {"ready", "missing"})
        label = {
            "ready": "校验",
            "incompatible": "下载",
            "failed": "重试",
            "cancelled": "重试",
        }.get(phase, "下载")
        self.download_button.setText(label)
        self.download_button.setEnabled(not busy)
        self.cancel_button.setVisible(busy)
        self.cancel_button.setEnabled(known and phase != "cancelling")
