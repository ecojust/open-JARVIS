"""Paged, per-run execution records with lazy, bounded native detail rendering."""

from __future__ import annotations

from collections import deque
import json

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)


PAGE_SIZE = 100
STATUS_LABELS = {
    "running": "执行中",
    "succeeded": "成功",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
}
SUMMARY_KEYS = (
    "step_id",
    "task_id",
    "name",
    "status",
    "created_at",
    "updated_at",
    "_event_id",
    "_started_event_id",
)


def summary(record):
    return {key: record.get(key) for key in SUMMARY_KEYS}


def bounded_value(value, limit=6000):
    """Serialize only enough detail to fill a single visible inspector."""
    encoder = json.JSONEncoder(ensure_ascii=False, indent=2)
    chunks, length = [], 0
    for chunk in encoder.iterencode(value):
        available = limit - length
        if len(chunk) > available:
            chunks.append(chunk[:available])
            return "".join(chunks) + "\n…（摘要已截断）"
        chunks.append(chunk)
        length += len(chunk)
    return "".join(chunks)


class RunToolsModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 3

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
        ):
            return ("工具 / 专业助手", "类型", "状态")[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == 0:
                return str(row.get("name") or "工具")[:180]
            if index.column() == 1:
                return (
                    "助手委派"
                    if str(row.get("name", "")).startswith("delegate_")
                    else "工具调用"
                )
            return STATUS_LABELS.get(row.get("status"), str(row.get("status") or ""))
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 2:
            return QColor(
                {"running": "#007aff", "failed": "#bd303b", "succeeded": "#278252"}.get(
                    row.get("status"), "#6e6e73"
                )
            )
        if role == Qt.ItemDataRole.UserRole:
            return row.get("step_id")
        return None

    def replace(self, rows):
        self.beginResetModel()
        self.rows = [summary(row) for row in rows[:PAGE_SIZE] if isinstance(row, dict)]
        self.endResetModel()

    def update(self, record, *, append=False):
        for index, row in enumerate(self.rows):
            if row["step_id"] == record["step_id"]:
                self.rows[index] = {**row, **record}
                self.dataChanged.emit(self.index(index, 0), self.index(index, 2))
                return True
        if append and len(self.rows) < PAGE_SIZE:
            index = len(self.rows)
            self.beginInsertRows(QModelIndex(), index, index)
            self.rows.append(summary(record))
            self.endInsertRows()
            return True
        return False


class RunToolsWidget(QWidget):
    page_requested = Signal(str, int, int)
    detail_requested = Signal(str, str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.run_id = None
        self.offset = 0
        self.total = 0
        self.page_revision = 0
        self.detail_revision = 0
        self.selected_step = None
        self._open = False
        self._loaded = False
        self._event_ids = deque(maxlen=300)
        self._live = {}
        self._snapshot_event_id = 0
        self._page_snapshot_event_id = 0
        self._last_event_id = 0
        self._pending_page = False
        self._pending_updates = {}
        self._page_overflow = False
        self._task_status = None
        self._recent_starts = deque(maxlen=1024)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.toggle = QPushButton("工具调用与委派 · 尚无记录")
        self.toggle.setObjectName("quiet")
        self.toggle.setAccessibleName("展开或收起本轮工具调用")
        self.toggle.clicked.connect(self.toggle_expanded)
        layout.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.panel = QWidget()
        body = QVBoxLayout(self.panel)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(5)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableView()
        self.model = RunToolsModel(self)
        self.table.setModel(self.model)
        self.table.setAccessibleName("本轮工具调用与委派记录")
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.setStyleSheet(
            "QTableView { background: white; border: 1px solid #e4e4e8; border-radius: 8px; gridline-color: #eeeeF1; selection-background-color: #dce9fb; selection-color: #174c88; } QHeaderView::section { background: #f5f5f7; border: none; padding: 6px; }"
        )
        self.table.selectionModel().currentRowChanged.connect(self._selected)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName("所选工具的参数和结果摘要")
        self.details.setPlaceholderText("点击一项调用，查看参数与结果摘要。")
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(self.details)
        self.splitter.setSizes([430, 430])
        self.splitter.setMinimumHeight(180)
        self.splitter.setMaximumHeight(250)
        body.addWidget(self.splitter)
        footer = QHBoxLayout()
        self.page_label = QLabel()
        self.page_label.setObjectName("muted")
        footer.addWidget(self.page_label, 1)
        self.previous = QPushButton("上一页")
        self.next = QPushButton("下一页")
        self.refresh = QPushButton("刷新记录")
        self.previous.clicked.connect(
            lambda: self.request_page(max(0, self.offset - PAGE_SIZE))
        )
        self.next.clicked.connect(lambda: self.request_page(self.offset + PAGE_SIZE))
        self.refresh.clicked.connect(lambda: self.request_page(self.offset))
        for button in (self.previous, self.next, self.refresh):
            footer.addWidget(button)
        body.addLayout(footer)
        layout.addWidget(self.panel)
        self.panel.hide()
        self.hide()

    def bind_task(self, task):
        identifier = task.get("run_id") or task.get("task_id")
        changed = identifier != self.run_id
        if changed:
            self.run_id = identifier
            self.offset = self.total = 0
            self.page_revision += 1
            self.detail_revision += 1
            self.selected_step = None
            self._open = self._loaded = False
            self._event_ids.clear()
            self._live.clear()
            self._snapshot_event_id = self._last_event_id = (
                self._page_snapshot_event_id
            ) = 0
            self._pending_page = False
            self._pending_updates.clear()
            self._recent_starts.clear()
            self._page_overflow = False
            self._task_status = None
            self.model.replace([])
            self.details.clear()
            self.panel.hide()
        self.setVisible(bool(identifier))
        previous_status = self._task_status
        self._task_status = task.get("status")
        records = task.get("steps")
        watermark = task.get("steps_event_id", task.get("snapshot_event_id"))
        fresh = (
            type(watermark) is int
            and watermark >= max(self._snapshot_event_id, self._last_event_id)
        ) or (changed and watermark is None)
        if isinstance(records, list) and fresh:
            self.total = max(self.total, int(task.get("steps_total", len(records))))
            if self.offset == 0:
                self._replace_rows(records, watermark or 0)
            if type(watermark) is int:
                self._snapshot_event_id = watermark
        self._labels()
        if (
            self._open
            and not changed
            and self._task_status != previous_status
            and self._task_status
            in {
                "succeeded",
                "partially_succeeded",
                "cancelled",
                "failed",
                "interrupted",
            }
        ):
            # Cancel/recovery can finalize steps without per-step SSE events.
            self.request_page(self.offset)

    def _replace_rows(self, rows, watermark, snapshot_total=None):
        selected = self.selected_step
        old_selected = next(
            (row for row in self.model.rows if row.get("step_id") == selected), {}
        )
        overlays = {row["step_id"]: row for row in self.model.rows}
        for mapping in (self._live, self._pending_updates):
            for identifier, row in mapping.items():
                if (row.get("_event_id") or 0) >= (
                    overlays.get(identifier, {}).get("_event_id") or 0
                ):
                    overlays[identifier] = row
        combined = []
        for row in rows[:PAGE_SIZE]:
            if row.get("task_id") != self.run_id:
                continue
            row = {**row, "_event_id": watermark}
            live = overlays.get(row.get("step_id"))
            if live and (live.get("_event_id") or 0) > watermark:
                row = {**row, **live}
            combined.append(row)
        # Starts after a snapshot belong at its end, and only on its last page.
        if (
            snapshot_total is not None
            and self.offset <= snapshot_total < self.offset + PAGE_SIZE
        ):
            existing = {row["step_id"] for row in combined}
            additions = [
                row
                for row in self._pending_updates.values()
                if (row.get("_started_event_id") or 0) > watermark
                and row["step_id"] not in existing
            ]
            additions.sort(key=lambda row: row["_started_event_id"])
            combined.extend(additions[: PAGE_SIZE - len(combined)])
        selection = self.table.selectionModel()
        selection.blockSignals(True)
        self.model.replace(combined)
        if selected:
            for index, row in enumerate(self.model.rows):
                if row.get("step_id") == selected:
                    self.table.setCurrentIndex(self.model.index(index, 0))
                    break
        selection.blockSignals(False)
        self._page_snapshot_event_id = max(self._page_snapshot_event_id, watermark)
        new_selected = next(
            (row for row in self.model.rows if row.get("step_id") == selected), {}
        )
        if (
            self._open
            and selected
            and new_selected
            and any(
                old_selected.get(key) != new_selected.get(key)
                for key in ("status", "updated_at")
            )
        ):
            self.detail_revision += 1
            self.details.setPlainText(
                str(new_selected.get("name", "")) + "\n正在更新参数与结果…"
            )
            self.detail_requested.emit(self.run_id, selected, self.detail_revision)

    def _labels(self):
        latest = next(reversed(self._live.values()), None) if self._live else None
        if latest and (latest.get("_event_id") or 0) <= self._page_snapshot_event_id:
            latest = next(
                (
                    row
                    for row in self.model.rows
                    if row.get("step_id") == latest.get("step_id")
                ),
                None,
            )
        suffix = ""
        if latest:
            suffix = (
                " · "
                + str(latest.get("name") or "工具")[:60]
                + " "
                + STATUS_LABELS.get(latest.get("status"), "")
            )
        self.toggle.setText(
            ("▾ " if self._open else "▸ ")
            + f"工具调用与委派 · {self.total} 项"
            + suffix
        )
        self.toggle.setEnabled(bool(self.run_id))
        end = min(self.offset + len(self.model.rows), self.total)
        self.page_label.setText(
            f"第 {self.offset + 1}–{end} 项，共 {self.total} 项"
            if self.total
            else "本轮尚无工具调用"
        )
        self.previous.setEnabled(self.offset > 0)
        self.next.setEnabled(self.offset + PAGE_SIZE < self.total)

    def toggle_expanded(self):
        self._open = not self._open
        self.panel.setVisible(self._open)
        self._labels()
        if self._open:
            self.request_page(self.offset)

    def showEvent(self, event):
        super().showEvent(event)
        if self._open and self.run_id:
            self.request_page(self.offset)

    def request_page(self, offset):
        if not self.run_id:
            return
        next_offset = max(0, int(offset))
        if next_offset != self.offset:
            self.model.replace([])
            self._page_snapshot_event_id = 0
        self.offset = next_offset
        self.page_revision += 1
        self.detail_revision += 1
        self.selected_step = None
        self.details.clear()
        self._pending_page = True
        self._loaded = False
        self._pending_updates.clear()
        self._page_overflow = False
        self.page_label.setText("正在读取本轮执行记录…")
        self.page_requested.emit(self.run_id, self.offset, self.page_revision)

    def receive_page(self, payload, revision):
        if (
            revision != self.page_revision
            or payload.get("task_id", payload.get("run_id")) != self.run_id
            or payload.get("offset") != self.offset
        ):
            return
        watermark = payload.get("snapshot_event_id", 0)
        if watermark < self._page_snapshot_event_id:
            self.page_error("本轮记录已有更新，可点击刷新读取最新一页。", revision)
            return
        if self._page_overflow and watermark < self._last_event_id:
            self.page_error(
                "执行记录在读取期间大量更新，请点击刷新获取最新状态。", revision
            )
            return
        snapshot_total = int(payload.get("total", 0))
        self._replace_rows(payload.get("steps", []), watermark, snapshot_total)
        after_snapshot = sum(event_id > watermark for event_id in self._recent_starts)
        self.total = max(self.total, snapshot_total + after_snapshot)
        self._snapshot_event_id = max(self._snapshot_event_id, watermark)
        self._pending_page = False
        self._pending_updates.clear()
        self._loaded = True
        self._labels()

    def page_error(self, message, revision):
        if revision == self.page_revision:
            self._pending_page = False
            self._pending_updates.clear()
            self.page_label.setText(str(message)[:200])

    def _selected(self, current, previous):
        if not current.isValid() or current.row() >= len(self.model.rows):
            return
        record = self.model.rows[current.row()]
        self.selected_step = record["step_id"]
        self.detail_revision += 1
        self.details.setPlainText(str(record.get("name", "")) + "\n正在读取参数与结果…")
        self.detail_requested.emit(
            self.run_id, self.selected_step, self.detail_revision
        )

    def receive_detail(self, record, revision):
        if (
            revision != self.detail_revision
            or record.get("task_id") != self.run_id
            or record.get("step_id") != self.selected_step
        ):
            return
        lines = [
            str(record.get("name", "")),
            STATUS_LABELS.get(record.get("status"), str(record.get("status", ""))),
            "",
            "输入参数",
            bounded_value(record.get("arguments", {})),
            "",
            "结果摘要",
        ]
        result = record.get("result")
        lines.append(
            "执行中，结果尚未返回。"
            if result is None and record.get("status") == "running"
            else bounded_value(result)
        )
        self.details.setPlainText("\n".join(lines))

    def detail_error(self, message, revision):
        if revision == self.detail_revision:
            self.details.setPlainText(str(message)[:2000])

    def append_event(self, event):
        if event.get("task_id", event.get("run_id")) != self.run_id or not self.run_id:
            return
        kind = event.get("type")
        if kind not in {"tool.started", "tool.completed", "tool.failed"}:
            return
        identifier = event.get("step_id")
        event_id = event.get("event_id")
        if not identifier or (event_id is not None and event_id in self._event_ids):
            return
        if type(event_id) is int and event_id <= max(
            self._last_event_id, self._page_snapshot_event_id
        ):
            return
        if event_id is not None:
            self._event_ids.append(event_id)
            self._last_event_id = event_id
        data = event.get("data") or {}
        existing = next(
            (row for row in self.model.rows if row["step_id"] == identifier), None
        )
        previous = self._live.get(identifier, existing or {})
        record = {
            **previous,
            "step_id": identifier,
            "task_id": self.run_id,
            "name": data.get("name", previous.get("name", "工具")),
            "status": {
                "tool.started": "running",
                "tool.completed": "succeeded",
                "tool.failed": "failed",
            }[kind],
            "updated_at": event.get("timestamp"),
            "_event_id": event_id,
        }
        if kind == "tool.started":
            record["_started_event_id"] = event_id
        if (
            kind == "tool.started"
            and not previous
            and (type(event_id) is not int or event_id > self._snapshot_event_id)
        ):
            self.total += 1
            if type(event_id) is int:
                self._recent_starts.append(event_id)
        elif not self.total:
            self.total = 1
        if len(self._live) >= PAGE_SIZE and identifier not in self._live:
            self._live.pop(next(iter(self._live)))
        self._live[identifier] = record
        if self._pending_page:
            if len(self._pending_updates) < 1024 or identifier in self._pending_updates:
                self._pending_updates[identifier] = record
            else:
                self._page_overflow = True
        self.model.update(record, append=self.offset == 0 and kind == "tool.started")
        self._labels()
        if self._open and self.selected_step == identifier:
            # Refetch only the actively inspected record, not every run or row.
            self.detail_revision += 1
            self.detail_requested.emit(self.run_id, identifier, self.detail_revision)
