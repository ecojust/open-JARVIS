"""Event-driven localhost HTTP/SSE client for the native Qt shell.

All I/O uses Qt's asynchronous network stack.  No worker thread, polling loop,
browser engine, model imports, or token-bearing URLs are needed.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import quote, urlencode

from PySide6.QtCore import (
    QByteArray,
    QEvent,
    QIODevice,
    QObject,
    QProcess,
    QProcessEnvironment,
    QRect,
    QSaveFile,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkProxy,
    QNetworkReply,
    QNetworkRequest,
)


from PySide6.QtWidgets import QApplication, QDialog, QFileDialog


class TaskClient(QObject):
    event = Signal(object)
    connection = Signal(bool, str)

    def __init__(self, base_url: str, token: str, parent=None):
        super().__init__(parent)
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.network = QNetworkAccessManager(self)
        self.network.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        self._replies = set()
        self._sse = None
        self._stream_buffer = bytearray()
        self._closing = False
        self._listen = False
        self.last_event_id = 0
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._open_events)

    def _request(self, path: str, *, authenticated=True, timeout=15000):
        request = QNetworkRequest(QUrl(self.base_url + path))
        request.setTransferTimeout(timeout)
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.ManualRedirectPolicy,
        )
        request.setRawHeader(b"Accept", b"application/json")
        if authenticated and self.token:
            request.setRawHeader(
                b"Authorization", ("Bearer " + self.token).encode("utf-8")
            )
        return request

    @staticmethod
    def _error(reply, raw: bytes) -> str:
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        try:
            detail = json.loads(raw).get("detail")
            if isinstance(detail, str):
                return detail[:2000]
        except (ValueError, AttributeError):
            pass
        if status:
            return f"本地服务请求失败（HTTP {status}）。"
        return "无法连接本地任务服务，请检查服务状态。"

    def request(
        self,
        method: str,
        path: str,
        payload=None,
        *,
        callback=None,
        error=None,
        authenticated=True,
        timeout=15000,
        raw=None,
        content_type=None,
    ):
        if self._closing:
            return None
        request = self._request(path, authenticated=authenticated, timeout=timeout)
        body = (
            raw
            if raw is not None
            else (
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
                if payload is not None
                else b""
            )
        )
        if content_type or payload is not None:
            request.setHeader(
                QNetworkRequest.KnownHeaders.ContentTypeHeader,
                content_type or "application/json",
            )
        reply = self.network.sendCustomRequest(
            request, method.encode("ascii"), QByteArray(body)
        )
        self._replies.add(reply)

        def finished():
            self._replies.discard(reply)
            try:
                data = bytes(reply.readAll())
                if self._closing:
                    return
                status = (
                    reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
                    or 0
                )
                if (
                    reply.error() != QNetworkReply.NetworkError.NoError
                    or not 200 <= status < 300
                ):
                    if error:
                        error(self._error(reply, data))
                    return
                try:
                    parsed = json.loads(data) if data else {}
                except ValueError:
                    if error:
                        error("本地服务返回了无法识别的数据。")
                    return
                if callback:
                    callback(parsed)
            finally:
                reply.deleteLater()

        reply.finished.connect(finished)
        return reply

    def health(self, callback, error=None):
        return self.request(
            "GET",
            "/api/health",
            callback=callback,
            error=error,
            authenticated=False,
            timeout=1200,
        )

    def start_events(self, after=0):
        if self._closing:
            return
        self.last_event_id = max(self.last_event_id, int(after))
        self._listen = True
        self._open_events()

    def _open_events(self):
        if self._closing or not self._listen or self._sse is not None:
            return
        request = self._request(
            "/api/events?" + urlencode({"after": self.last_event_id}), timeout=0
        )
        request.setRawHeader(b"Accept", b"text/event-stream")
        reply = self.network.get(request)
        self._sse = reply
        self._stream_buffer.clear()
        reply.readyRead.connect(lambda: self._consume(reply))
        reply.finished.connect(lambda: self._events_finished(reply))

    def _consume(self, reply):
        if self._closing or reply is not self._sse:
            return
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if status and status != 200:
            reply.readAll()
            return
        self._stream_buffer.extend(bytes(reply.readAll()))
        if len(self._stream_buffer) > 4 * 1024 * 1024:
            self.connection.emit(False, "本地事件过大，正在重新连接。")
            reply.abort()
            return
        # SSE delimiters are ASCII; decode complete frames so split UTF-8
        # Chinese characters are never lost at network chunk boundaries.
        self._stream_buffer[:] = self._stream_buffer.replace(b"\r\n", b"\n")
        while b"\n\n" in self._stream_buffer:
            block, _, remaining = self._stream_buffer.partition(b"\n\n")
            self._stream_buffer[:] = remaining
            lines = []
            event_id = None
            for line in block.split(b"\n"):
                if line.startswith(b"data:"):
                    lines.append(line[5:].lstrip(b" "))
                elif line.startswith(b"id:"):
                    try:
                        event_id = int(line[3:].strip())
                    except ValueError:
                        pass
            if not lines:
                continue
            try:
                envelope = json.loads(b"\n".join(lines))
                if not isinstance(envelope, dict):
                    raise ValueError("not an object")
                actual_id = int(envelope.get("event_id", event_id or 0))
            except (ValueError, TypeError):
                self.connection.emit(False, "收到格式异常的执行事件，正在重连。")
                reply.abort()
                return
            if actual_id <= self.last_event_id:
                continue
            self.last_event_id = actual_id
            self.connection.emit(True, "执行记录已连接")
            self.event.emit(envelope)

    def _events_finished(self, reply):
        if self._sse is reply:
            self._sse = None
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        reply.deleteLater()
        if self._closing or not self._listen:
            return
        if status in (401, 403):
            self._listen = False
            self.connection.emit(False, "本地服务身份校验失败，请重启客户端。")
            return
        self.connection.emit(False, "执行记录连接中断，正在恢复；后台任务仍继续。")
        self._retry.start(1500)

    def download(self, path: str, destination: Path, callback=None, error=None):
        if self._closing:
            return
        output = QSaveFile(str(destination))
        if not output.open(QIODevice.OpenModeFlag.WriteOnly):
            if error:
                error("无法写入所选文件位置。")
            return
        reply = self.network.get(self._request(path, timeout=30000))
        self._replies.add(reply)
        failed = False

        def chunk():
            nonlocal failed
            status = (
                reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0
            )
            if status == 200:
                data = reply.readAll()
                if output.write(data) != data.size():
                    failed = True
                    reply.abort()

        def finished():
            self._replies.discard(reply)
            try:
                if self._closing:
                    output.cancelWriting()
                    return
                status = (
                    reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
                    or 0
                )
                if (
                    failed
                    or reply.error() != QNetworkReply.NetworkError.NoError
                    or status != 200
                ):
                    output.cancelWriting()
                    if error:
                        error("文件下载失败，已有目标文件未被替换。")
                    return
                chunk()
                if failed or not output.commit():
                    if error:
                        error("无法完成文件保存。")
                    return
                if callback:
                    callback(destination)
            finally:
                reply.deleteLater()

        reply.readyRead.connect(chunk)
        reply.finished.connect(finished)

    def close(self):
        self._closing = True
        self._listen = False
        self._retry.stop()
        if self._sse is not None:
            self._sse.abort()
        for reply in list(self._replies):
            reply.abort()


def _boolean(value, default=True):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"false", "0", "no", "off"}:
            return False
        if lowered in {"true", "1", "yes", "on"}:
            return True
    return default


class DesktopController(QObject):
    """Owns windows and child processes; task execution belongs to the backend."""

    TERMINAL_EVENTS = {
        "task.completed",
        "task.failed",
        "task.cancelled",
        "task.interrupted",
    }
    ACTIVE_STATUSES = {"queued", "planning", "running"}

    def __init__(
        self,
        app,
        *,
        autostart=True,
        client_factory=TaskClient,
        process_factory=QProcess,
    ):
        super().__init__(app)
        from .config import read_preferences
        from .desktop_ui import VoiceTile

        self.app = app
        self._client_factory = client_factory
        self._process_factory = process_factory
        settings_error = None
        try:
            self.preferences = read_preferences()
        except ValueError as exc:
            self.preferences = {}
            settings_error = str(exc)
        self.ready = False
        self.configured = False
        self.closing = False
        self._restarting = False
        self.pending_settings = False
        self.user_paused = False
        self.listening = False
        self.speech_enabled = _boolean(
            os.getenv(
                "UI_SPEECH_ENABLED", self.preferences.get("UI_SPEECH_ENABLED", True)
            )
        )
        self.auto_fullscreen = _boolean(
            os.getenv(
                "UI_AUTO_FULLSCREEN", self.preferences.get("UI_AUTO_FULLSCREEN", True)
            )
        )
        self.speaking = False
        self._playback_id = None
        self._playback_task = None
        self._presented_task = None
        self.awaiting_task = None
        self.tasks = {}
        self.task_order = []
        self.selected_task = None
        self.notifications = deque()
        self._notified_events = deque(maxlen=256)
        self.last_status = "正在启动本地服务…"
        self.errors = {}
        self.results = None
        self.settings_dialog = None
        self.client = None
        self.token = ""
        self.instance_id = ""
        self.base_url = ""
        self.backend_log = ""
        self.audio_log = ""
        self._audio_buffer = bytearray()
        self._probe_pending = False
        self._start_time = 0.0
        self._last_task_refresh = 0.0
        self._downloads = {}
        self._task_refresh = QTimer(self)
        self._task_refresh.setSingleShot(True)
        self._task_refresh.timeout.connect(self.refresh_tasks)
        self._release_view = QTimer(self)
        self._release_view.setSingleShot(True)
        self._release_view.timeout.connect(self._discard_hidden_results)
        self.probe_timer = QTimer(self)
        self.probe_timer.setInterval(250)
        self.probe_timer.timeout.connect(self._probe)
        self.backend = process_factory(self)
        self.backend.readyReadStandardError.connect(self._read_backend_log)
        self.backend.readyReadStandardOutput.connect(self._read_backend_log)
        self.backend.errorOccurred.connect(self._backend_error)
        self.backend.finished.connect(self._backend_finished)
        self.audio = process_factory(self)
        self.audio.readyReadStandardOutput.connect(self._read_audio)
        self.audio.readyReadStandardError.connect(self._read_audio_log)
        self.audio.started.connect(
            lambda: self._send_audio("pause") if self.user_paused else None
        )
        self.audio.errorOccurred.connect(self._audio_process_error)
        self.audio.finished.connect(self._audio_finished)
        self.tile = VoiceTile(self)
        self._restore_tile_position()
        self.tile.show()
        if settings_error:
            self.errors["settings"] = settings_error
        self.update_tile()
        self.app.aboutToQuit.connect(self.cleanup)
        if autostart:
            QTimer.singleShot(0, self.start_backend)

    def _restore_tile_position(self):
        x, y = self.preferences.get("UI_TILE_X"), self.preferences.get("UI_TILE_Y")
        if type(x) is int and type(y) is int:
            proposed = QRect(x, y, self.tile.width(), self.tile.height())
            if any(
                screen.availableGeometry().contains(proposed)
                for screen in QApplication.screens()
            ):
                self.tile.move(x, y)

    def save_tile_position(self, x, y):
        from .config import save_preferences

        self.preferences.update({"UI_TILE_X": x, "UI_TILE_Y": y})
        try:
            save_preferences({"UI_TILE_X": x, "UI_TILE_Y": y})
        except (OSError, ValueError):
            self.notify("窗口位置暂时无法保存。")

    def _environment(self):
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONUNBUFFERED", "1")
        return environment

    def _launch(self, process, role, *args, environment=None):
        from .launcher import child_command

        command = child_command(role, *args)
        process.setProcessEnvironment(environment or self._environment())
        # Resource resolution is handled by paths.py, never by a writable cwd.
        process.start(command[0], command[1:])

    def start_backend(self):
        if self.closing:
            return
        try:
            port = int(os.getenv("JARVIS_DESKTOP_PORT", "8765"))
            if not 1024 <= port <= 65535:
                raise ValueError
        except ValueError:
            self.notify(
                "本地端口必须在 1024–65535 之间，请检查 JARVIS_DESKTOP_PORT。",
                "backend",
            )
            return
        # A bind-only probe mistakes sockets in TIME_WAIT for a live service,
        # preventing settings from restarting the backend on the same port.
        # Detect a real listener; the startup handshake still verifies the
        # fresh instance ID before any authenticated task request is sent.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as check:
            check.settimeout(0.05)
            try:
                occupied = check.connect_ex(("127.0.0.1", port)) == 0
            except OSError:
                occupied = False
        if occupied:
            self.notify(
                f"本地端口 {port} 已被占用。请退出旧的 JARVIS 实例或更换端口；不会连接身份未知的服务。",
                "backend",
            )
            return
        self.ready = False
        self.errors.pop("backend", None)
        self.token = secrets.token_urlsafe(32)
        self.instance_id = uuid.uuid4().hex
        self.base_url = f"http://127.0.0.1:{port}"
        if self.client:
            self.client.close()
            self.client.deleteLater()
        self.client = self._client_factory(self.base_url, self.token, self)
        self.client.event.connect(self.handle_task_event)
        self.client.connection.connect(self._event_connection)
        environment = self._environment()
        environment.insert("JARVIS_INSTANCE_TOKEN", self.token)
        environment.insert("JARVIS_INSTANCE_ID", self.instance_id)
        self._probe_pending = False
        self._start_time = time.monotonic()
        self.last_status = "正在启动本地任务服务…"
        self._launch(
            self.backend, "backend", "--port", str(port), environment=environment
        )
        self.probe_timer.start()
        self.update_tile()

    def _read_backend_log(self):
        data = bytes(self.backend.readAllStandardError()) + bytes(
            self.backend.readAllStandardOutput()
        )
        # Diagnostic bytes stay bounded and are not surfaced as provider errors
        # may include request details; never print authentication headers.
        self.backend_log = (self.backend_log + data.decode("utf-8", errors="replace"))[
            -4000:
        ]

    def _backend_error(self, error):
        if not self.closing and not self._restarting:
            self.probe_timer.stop()
            self.notify(
                "本地任务服务未能启动，请检查运行依赖或重新安装客户端。", "backend"
            )

    def _backend_finished(self, code, status):
        self.ready = False
        self.probe_timer.stop()
        if self.closing or self._restarting:
            return
        if self.client:
            self.client.close()
        self.notify(
            f"本地任务服务已退出（代码 {code}）。任务记录仍保留，可在设置中重新应用配置以重启服务。",
            "backend",
        )

    def _probe(self):
        if self.closing or self.ready or self._probe_pending:
            return
        if time.monotonic() - self._start_time > 30:
            self.probe_timer.stop()
            self.backend.terminate()
            self.notify(
                "本地任务服务启动超时，请检查依赖。语音不会提交到其他服务。", "backend"
            )
            return
        if self.backend.state() != QProcess.ProcessState.Running:
            return
        self._probe_pending = True
        self.client.health(
            self._health_ready, lambda message: setattr(self, "_probe_pending", False)
        )

    def _health_ready(self, payload):
        self._probe_pending = False
        if self.closing or self.ready:
            return
        if (
            not isinstance(payload, dict)
            or payload.get("instance_id") != self.instance_id
        ):
            self.probe_timer.stop()
            self.backend.terminate()
            self.notify("本地服务身份不匹配，已停止连接。", "backend")
            return
        if not payload.get("ready"):
            return
        self.ready = True
        self.probe_timer.stop()
        self.errors.pop("backend", None)
        self.client.start_events(payload.get("last_event_id", 0))
        self.client.request(
            "GET",
            "/api/status",
            callback=self._status_ready,
            error=lambda message: self.notify(message, "backend"),
        )
        self.refresh_tasks()
        self.start_audio()
        self.update_tile()

    def _status_ready(self, data):
        self.configured = bool(data.get("configured"))
        if self.configured:
            self.last_status = "任务模型已配置。说“贾维斯”加上你的任务。"
        else:
            self.last_status = "演示模式：请在设置中配置任务模型。也可先输入 /analyze example-sales.csv 验证流程。"
        self._render_status()
        self.update_tile()

    def _event_connection(self, connected, message):
        if self.closing:
            return
        if connected:
            self.errors.pop("events", None)
        else:
            self.errors["events"] = message
        self.update_tile()

    def start_audio(self):
        if self.closing or self.audio.state() != QProcess.ProcessState.NotRunning:
            return
        self.listening = False
        self._audio_buffer.clear()
        self.errors.pop("audio", None)
        self.errors.pop("stt", None)
        self.last_status = "正在准备本地语音模型…"
        self._launch(self.audio, "audio")
        self.update_tile()

    def _read_audio_log(self):
        self.audio_log = (
            self.audio_log
            + bytes(self.audio.readAllStandardError()).decode("utf-8", errors="replace")
        )[-4000:]

    def _read_audio(self):
        self._audio_buffer.extend(bytes(self.audio.readAllStandardOutput()))
        if len(self._audio_buffer) > 512 * 1024:
            self._audio_buffer.clear()
            self.notify("语音进程返回了异常数据。", "audio")
            return
        while b"\n" in self._audio_buffer:
            line, _, rest = self._audio_buffer.partition(b"\n")
            self._audio_buffer[:] = rest
            if not line.strip():
                continue
            try:
                envelope = json.loads(line)
                if isinstance(envelope, dict) and isinstance(
                    envelope.get("data", {}), dict
                ):
                    self.handle_audio_event(
                        str(envelope.get("type", "")), envelope.get("data", {})
                    )
            except (ValueError, UnicodeError):
                # Native library diagnostics are not protocol events.
                self.audio_log = (
                    self.audio_log + line.decode("utf-8", errors="replace")
                )[-4000:]

    def _send_audio(self, command, **data):
        if self.audio.state() != QProcess.ProcessState.Running:
            return False
        envelope = {"id": uuid.uuid4().hex, "command": command, **data}
        self.audio.write(
            (json.dumps(envelope, ensure_ascii=False) + "\n").encode("utf-8")
        )
        return True

    def _audio_process_error(self, error):
        if not self.closing and not self._restarting:
            self.notify(
                "本地语音进程无法启动，请检查桌面依赖。文字任务仍可使用。", "audio"
            )

    def _audio_finished(self, code, status):
        self.listening = False
        self.speaking = False
        self._playback_id = None
        self._playback_task = None
        if self.closing or self._restarting:
            return
        self.notify(
            f"语音进程已退出（代码 {code}），文字任务仍可使用。可在设置中重新应用配置以恢复。",
            "audio",
        )
        QTimer.singleShot(0, self._drain_notifications)

    def handle_audio_event(self, event, data):
        if self.closing:
            return
        if event == "audio.ready":
            self.listening = bool(data.get("listening", True))
            self.errors.pop("audio", None)
            self.errors.pop("stt", None)
            if self.user_paused:
                self._send_audio("pause")
        elif event == "audio.status":
            if "listening" in data:
                self.listening = bool(data["listening"])
            if not self.speaking and not self._active_tasks():
                self.last_status = str(data.get("message", "持续聆听中"))
        elif event == "stt.final":
            text = str(data.get("text", "")).strip()
            if text and not self.user_paused and not self.speaking:
                self.submit(text)
        elif event == "tts.started":
            if data.get("playback_id") != self._playback_id:
                return
            self.speaking = True
            self.errors.pop("tts", None)
            self.last_status = "正在合成语音反馈…"
        elif event == "tts.playing":
            if data.get("playback_id") != self._playback_id:
                return
            self.last_status = "正在播报任务结果…"
        elif event == "tts.finished":
            if data.get("playback_id") != self._playback_id:
                return
            self._finish_playback()
        elif event == "audio.error":
            component = str(data.get("component", "audio"))
            self.notify(str(data.get("message", "语音组件出现错误。")), component)
            if component in {"audio", "stt", "microphone"}:
                self.listening = False
            if (
                self._playback_id is not None
                and data.get("playback_id") == self._playback_id
            ):
                self._finish_playback()
        self.update_tile()

    def _finish_playback(self):
        self.speaking = False
        self._playback_id = None
        self._playback_task = None
        self._maybe_apply_settings()
        QTimer.singleShot(250, self._drain_notifications)

    def _active_tasks(self):
        return [
            task
            for task in self.tasks.values()
            if task.get("status") in self.ACTIVE_STATUSES and not task.get("held")
        ]

    def submit(self, text):
        text = str(text).strip()
        if not text or len(text) > 12000:
            self.notify("请输入 1–12,000 个字符的任务。")
            return False
        if not self.ready or self.client is None:
            self.notify("任务服务尚未就绪，请稍后重试。")
            return False
        if self.pending_settings:
            self.notify("设置等待现有任务结束后应用，请稍后再发送新任务。")
            return False
        if text.startswith("新任务"):
            text = re.sub(r"^新任务[，,。:：\s]*", "", text).strip()
            self.awaiting_task = None
            self._send_audio("expect_input", enabled=False)
            if not text:
                self.notify("已切换到新任务。请继续说出或输入任务内容。")
                return True
        waiting = self.awaiting_task
        path = (
            f"/api/tasks/{quote(str(waiting), safe='')}/input"
            if waiting
            else "/api/tasks"
        )
        payload = {"message": text}
        if not waiting:
            payload["client_request_id"] = uuid.uuid4().hex
        self.last_status = "正在提交任务…"
        self._render_status()
        self.client.request(
            "POST",
            path,
            payload,
            callback=lambda task: self._task_submitted(task, waiting),
            error=self.notify,
        )
        return True

    def _task_submitted(self, data, waiting):
        task_id = data.get("task_id")
        if waiting and self.awaiting_task == waiting:
            self.awaiting_task = None
            self._send_audio("expect_input", enabled=False)
        if self.speech_enabled:
            self._send_audio("prewarm")
        self.last_status = "任务已提交，正在后台处理。可以继续安排下一项任务。"
        if task_id:
            if not self.speaking:
                self.selected_task = task_id
            self._fetch_task(task_id, callback=self._update_task)
        self.refresh_tasks()
        self.update_tile()
        self._render_status()

    def refresh_tasks(self):
        if not self.ready or self.client is None or self.closing:
            return
        self.client.request(
            "GET", "/api/tasks?limit=100", callback=self._tasks_ready, error=self.notify
        )

    def _tasks_ready(self, payload):
        tasks = payload.get("tasks", []) if isinstance(payload, dict) else []
        self.task_order = []
        for task in tasks[:100]:
            task_id = task.get("task_id", task.get("id"))
            if task_id is not None:
                self.tasks[task_id] = {**self.tasks.get(task_id, {}), **task}
                self.task_order.append(task_id)
        # Bound memory independently of database history.
        keep = set(self.task_order) | {
            self.selected_task,
            self.awaiting_task,
            self._playback_task,
        }
        self.tasks = {key: value for key, value in self.tasks.items() if key in keep}
        self._trim_task_details()
        if self.results and self.results.isVisible():
            self.results.render_tasks([self.tasks[key] for key in self.task_order])
        self._maybe_apply_settings()
        self.update_tile()

    def _fetch_task(self, task_id, callback=None):
        if self.client and self.ready:
            self.client.request(
                "GET",
                "/api/tasks/" + quote(str(task_id), safe=""),
                callback=callback or self._update_task,
                error=self.notify,
            )

    def _update_task(self, task):
        task_id = task.get("task_id", task.get("id"))
        if task_id is None:
            return
        self.tasks[task_id] = {**self.tasks.get(task_id, {}), **task}
        if (
            self.results
            and self.results.isVisible()
            and self.selected_task == task_id
            and not self.speaking
        ):
            self.results.render_task(self.tasks[task_id])
        self.update_tile()

    def _trim_task_details(self):
        """The database owns history; retain heavy details only for active views."""
        keep = {self.selected_task, self._playback_task, self.awaiting_task}
        for task_id, task in self.tasks.items():
            if task_id not in keep:
                for key in ("events", "messages", "steps", "plan"):
                    task.pop(key, None)
                # Full reports are fetched when selected, so history browsing
                # cannot accumulate 100 copies of large generated documents.
                if len(str(task.get("display_text", ""))) > 1000:
                    task.pop("display_text", None)

    def select_task(self, task_id):
        if task_id is None:
            return
        if self.speaking and task_id != self._playback_task:
            self.stop_speaking()
        self.selected_task = task_id
        self._trim_task_details()
        task = self.tasks.get(task_id)
        if task:
            self.awaiting_task = (
                task_id if task.get("status") == "awaiting_input" else None
            )
            self._send_audio(
                "expect_input", enabled=bool(self.awaiting_task), timeout=30
            )
            self._ensure_results().render_task(
                self._presented_task if self.speaking and self._presented_task else task
            )
        self._fetch_task(
            task_id, callback=lambda data: self._selected_ready(task_id, data)
        )

    def _selected_ready(self, requested_id, task):
        # _update_task already refreshes the selected visible view; do not
        # parse the same Markdown/artifacts a second time on every selection.
        self._update_task(task)
        if self.selected_task == requested_id:
            self.awaiting_task = (
                requested_id if task.get("status") == "awaiting_input" else None
            )
            self._send_audio(
                "expect_input", enabled=bool(self.awaiting_task), timeout=30
            )

    def handle_task_event(self, envelope):
        if self.closing or not isinstance(envelope, dict):
            return
        kind = envelope.get("type", "")
        task_id = envelope.get("task_id")
        data = envelope.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        if task_id:
            task = self.tasks.setdefault(task_id, {"task_id": task_id})
            if "status" in data:
                task["status"] = data["status"]
            elif kind == "task.queued":
                task["status"] = "queued"
            elif kind in {"step.started", "tool.started"}:
                task["status"] = "running"
            if kind == "task.awaiting_input":
                task["status"] = "awaiting_input"
        if self.results and self.results.isVisible():
            self.results.append_event(envelope)
        if (
            kind in {"tool.started", "step.started", "task.plan_created"}
            and not self.speaking
        ):
            self.last_status = str(
                data.get("message") or data.get("name") or "正在处理任务…"
            )
        if kind in self.TERMINAL_EVENTS or kind == "task.awaiting_input":
            event_id = envelope.get("event_id")
            if event_id not in self._notified_events:
                self._notified_events.append(event_id)
                status_map = {
                    "task.failed": "failed",
                    "task.cancelled": "cancelled",
                    "task.interrupted": "interrupted",
                    "task.awaiting_input": "awaiting_input",
                }
                if task_id:
                    task = {**self.tasks.get(task_id, {}), **data, "task_id": task_id}
                    task["status"] = data.get(
                        "status", status_map.get(kind, "succeeded")
                    )
                    self.tasks[task_id] = task
                    if task.get("display_text") or task.get("speech_text"):
                        self._queue_notification(task)
                    else:
                        self._fetch_task(task_id, callback=self._queue_notification)
            self._task_refresh.start(100)
        elif kind.startswith("task."):
            if not self._task_refresh.isActive():
                self._task_refresh.start(200)
        self.update_tile()
        self._render_status()

    def _queue_notification(self, task):
        if self.closing:
            return
        self._update_task(task)
        if len(self.notifications) >= 8:
            self.last_status = "另有任务已结束，完整结果保存在任务列表中。"
            self._render_status()
            return
        self.notifications.append(dict(task))
        self._drain_notifications()

    def _drain_notifications(self):
        if self.closing or self.speaking or self._restarting or not self.notifications:
            return
        task = self.notifications.popleft()
        self._presented_task = dict(task)
        task_id = task.get("task_id", task.get("id"))
        self.selected_task = task_id
        self.awaiting_task = task_id if task.get("status") == "awaiting_input" else None
        self._send_audio("expect_input", enabled=False)
        self.last_status = (
            "请补充任务信息。" if self.awaiting_task else "任务已返回结果。"
        )
        if self.auto_fullscreen or (self.results and self.results.isVisible()):
            window = self._ensure_results()
            window.render_tasks(
                [self.tasks[key] for key in self.task_order if key in self.tasks]
            )
            window.render_task(task)
            window.set_status(self.last_status)
            if self.auto_fullscreen:
                window.show_result()
        speech = str(task.get("speech_text") or "").strip()
        if (
            self.speech_enabled
            and speech
            and self.audio.state() == QProcess.ProcessState.Running
        ):
            playback_id = uuid.uuid4().hex
            self._playback_id = playback_id
            self._playback_task = task_id
            self.speaking = True
            if not self._send_audio(
                "speak",
                task_id=task_id,
                playback_id=playback_id,
                text=speech,
                awaiting_input=bool(self.awaiting_task),
            ):
                self._finish_playback()
        else:
            if self.awaiting_task:
                self._send_audio("expect_input", enabled=True, timeout=30)
            QTimer.singleShot(0, self._drain_notifications)
        self.update_tile()

    def toggle_pause(self):
        self.user_paused = not self.user_paused
        self._send_audio("pause" if self.user_paused else "resume")
        self.update_tile()

    def toggle_speech(self, enabled):
        from .config import save_preferences

        self.speech_enabled = bool(enabled)
        self.preferences["UI_SPEECH_ENABLED"] = self.speech_enabled
        save_preferences({"UI_SPEECH_ENABLED": self.speech_enabled})
        if not self.speech_enabled:
            self.stop_speaking()

    def stop_speaking(self):
        self.notifications.clear()
        self._send_audio("stop_speaking")
        self.speaking = False
        self._playback_id = None
        self._playback_task = None
        self.last_status = "播报已停止；任务和文字结果仍然保留。"
        self._maybe_apply_settings()
        self.update_tile()
        self._render_status()

    def _ensure_results(self):
        if self.results is None:
            from .desktop_ui import ResultsWindow

            self.results = ResultsWindow(self)
            self.results.installEventFilter(self)
        return self.results

    def show_results(self):
        window = self._ensure_results()
        self._release_view.stop()
        window.set_status(self._status_text())
        window.render_tasks(
            [self.tasks[key] for key in self.task_order if key in self.tasks]
        )
        window.show_result()
        self.refresh_tasks()
        if self.speaking and self._presented_task:
            window.render_task(self._presented_task)
        elif self.selected_task:
            self.select_task(self.selected_task)

    def eventFilter(self, watched, event):
        if watched is self.results:
            if event.type() == QEvent.Type.Hide and not self.closing:
                self._release_view.start(120000)
            elif event.type() == QEvent.Type.Show:
                self._release_view.stop()
        return super().eventFilter(watched, event)

    def _discard_hidden_results(self):
        if self.results and not self.results.isVisible():
            self.results.deleteLater()
            self.results = None
            for task in self.tasks.values():
                task.pop("events", None)
                task.pop("messages", None)
                task.pop("steps", None)
                task.pop("plan", None)

    def _status_text(self):
        if self.errors:
            return "\n".join(self.errors.values())
        return self.last_status

    def _render_status(self):
        if self.results and self.results.isVisible():
            self.results.set_status(self._status_text())

    def notify(self, message, component=None):
        self.last_status = str(message)
        if component:
            self.errors[component] = str(message)
        self._render_status()
        self.update_tile()

    def update_tile(self):
        if self.closing:
            return
        count = len(self._active_tasks())
        if self.errors:
            caption = "查看提示"
        elif not self.ready:
            caption = "启动中"
        elif self.user_paused:
            caption = "已暂停"
        elif self.speaking:
            caption = "正在反馈"
        elif count:
            caption = "执行任务" + (f" · {count}" if count > 1 else "")
        elif self.awaiting_task:
            caption = "等待补充"
        elif not self.configured:
            caption = "演示模式"
        elif self.listening:
            caption = "正在聆听"
        else:
            caption = "准备语音"
        self.tile.set_state(caption, self._status_text(), error=bool(self.errors))

    def cancel_task(self, task_id):
        if task_id and self.ready:
            self.client.request(
                "POST",
                "/api/tasks/" + quote(str(task_id), safe="") + "/cancel",
                {},
                callback=lambda _: self._fetch_task(task_id),
                error=self.notify,
            )

    def resume_task(self, task_id):
        if task_id and self.ready:
            self.client.request(
                "POST",
                "/api/tasks/" + quote(str(task_id), safe="") + "/resume",
                {},
                callback=lambda _: self.refresh_tasks(),
                error=self.notify,
            )

    def upload_file(self):
        if not self.ready:
            self.notify("本地服务尚未就绪，暂时不能导入文件。")
            return
        filename, _ = QFileDialog.getOpenFileName(
            self._ensure_results(),
            "导入工作文件",
            "",
            "工作文件 (*.csv *.txt *.md *.json)",
        )
        if not filename:
            return
        path = Path(filename)
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                self.notify("文件不能超过 2 MB，请缩小文件后导入。")
                return
            data = path.read_bytes()
        except OSError:
            self.notify("无法读取所选文件。")
            return
        self.client.request(
            "POST",
            "/api/files?" + urlencode({"path": path.name}),
            raw=data,
            content_type="application/octet-stream",
            callback=lambda result: self.notify(
                f"文件已导入：{result.get('path', path.name)}。可以让 JARVIS 读取或分析它。"
            ),
            error=self.notify,
        )

    def download_artifact(self, artifact_id, name):
        if not self.ready:
            return
        filename, _ = QFileDialog.getSaveFileName(
            self._ensure_results(),
            "保存任务产物",
            str(Path.home() / "Downloads" / Path(name).name),
        )
        if filename:
            self.client.download(
                "/api/artifacts/" + quote(str(artifact_id), safe=""),
                Path(filename),
                callback=lambda path: self.notify(f"已保存：{path}"),
                error=self.notify,
            )

    def show_settings(self):
        from .config import read_preferences
        from .desktop_ui import SettingsDialog

        if self.settings_dialog:
            self.settings_dialog.raise_()
            self.settings_dialog.activateWindow()
            return
        try:
            values = dict(read_preferences())
        except ValueError as exc:
            self.notify(str(exc), "settings")
            return
        for key in [
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_MODEL",
            "JARVIS_STT_MODEL_DIR",
            "JARVIS_TTS_MODEL_DIR",
            "JARVIS_WAKE_WORD",
            "JARVIS_MIC_DEVICE",
        ]:
            # Display effective configuration (explicit environment overrides
            # persisted settings), not a different hidden saved value.
            if key in os.environ:
                values[key] = os.environ[key]
        values["UI_SPEECH_ENABLED"] = self.speech_enabled
        values["UI_AUTO_FULLSCREEN"] = self.auto_fullscreen
        dialog = SettingsDialog(
            values,
            self.results if self.results and self.results.isVisible() else self.tile,
        )
        self.settings_dialog = dialog
        dialog.finished.connect(lambda result: self._settings_finished(dialog, result))
        dialog.open()

    def _settings_finished(self, dialog, result):
        from .config import save_preferences

        self.settings_dialog = None
        if result != QDialog.DialogCode.Accepted:
            dialog.deleteLater()
            return
        values = dialog.values()
        url = QUrl(str(values.get("OPENAI_BASE_URL", "")))
        if url.scheme() not in {"http", "https"} or not url.host():
            self.notify("模型接口地址必须是有效的 HTTP(S) 地址，配置未保存。")
            dialog.deleteLater()
            return
        try:
            save_preferences(values)
        except (OSError, ValueError) as exc:
            self.notify(f"设置无法保存：{exc}")
            dialog.deleteLater()
            return
        self.preferences.update(values)
        self.speech_enabled = bool(values.get("UI_SPEECH_ENABLED", True))
        self.auto_fullscreen = bool(values.get("UI_AUTO_FULLSCREEN", True))
        self.pending_settings = True
        if not self.speech_enabled:
            self.stop_speaking()
        self.notify("设置已保存，现有任务和播报结束后会应用。")
        dialog.deleteLater()
        self._maybe_apply_settings()

    def _maybe_apply_settings(self):
        if not self.pending_settings or self.closing or self._restarting:
            return
        if self._active_tasks() or self.speaking or self._downloads:
            return
        self.pending_settings = False
        self._restarting = True
        self.ready = False
        self.probe_timer.stop()
        if self.client:
            self.client.close()
        self._send_audio("shutdown")
        if self.backend.state() != QProcess.ProcessState.NotRunning:
            self.backend.terminate()
        self._restart_deadline = time.monotonic() + 3
        self._wait_restart()

    def _wait_restart(self):
        if self.closing:
            return
        running = [
            process
            for process in (self.backend, self.audio)
            if process.state() != QProcess.ProcessState.NotRunning
        ]
        if running:
            if time.monotonic() > self._restart_deadline + 5:
                self._restarting = False
                self.notify("旧服务未能退出，请退出客户端后重新打开。", "backend")
                return
            if time.monotonic() > self._restart_deadline:
                for process in running:
                    process.kill()
            QTimer.singleShot(50, self._wait_restart)
            return
        self._restarting = False
        self.errors.clear()
        self.notifications.clear()
        self.speaking = False
        self._playback_id = None
        self.start_backend()

    def download_model(self, kind):
        if kind not in {"stt", "tts"} or kind in self._downloads:
            return
        process = self._process_factory(self)
        self._downloads[kind] = process
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        output = bytearray()

        def progress():
            output.extend(bytes(process.readAllStandardOutput()))
            if len(output) > 6000:
                del output[:-6000]
            lines = output.decode("utf-8", errors="replace").strip().splitlines()
            if lines:
                self.notify("语音模型下载：" + lines[-1][:500])

        def finished(code, status):
            progress()
            self._downloads.pop(kind, None)
            process.deleteLater()
            if self.closing:
                return
            if code != 0:
                self.notify(
                    "语音模型下载失败，请检查网络或在设置中指定已下载的模型目录。",
                    "download",
                )
                return
            self.errors.pop("download", None)
            self.notify("语音模型已安装，正在等待当前任务结束后重新加载。")
            self.pending_settings = True
            self._maybe_apply_settings()

        process.readyReadStandardOutput.connect(progress)
        process.finished.connect(finished)

        def start_error(reason):
            if reason == QProcess.ProcessError.FailedToStart:
                self._downloads.pop(kind, None)
                process.deleteLater()
            self.notify("无法启动模型下载进程，请检查运行依赖。", "download")

        process.errorOccurred.connect(start_error)
        self.notify("正在下载语音模型，可继续使用文字任务。")
        self._launch(process, "download-stt" if kind == "stt" else "download-tts")

    def request_quit(self):
        self.cleanup()
        self.app.quit()

    def cleanup(self):
        if self.closing:
            return
        self.closing = True
        self.ready = False
        self.probe_timer.stop()
        self._task_refresh.stop()
        self._release_view.stop()
        if self.client:
            self.client.close()
        self.notifications.clear()
        self._send_audio("shutdown")
        processes = [self.audio, self.backend, *self._downloads.values()]
        self.audio.waitForBytesWritten(150)
        if self.audio.state() != QProcess.ProcessState.NotRunning:
            self.audio.waitForFinished(700)
        for process in processes:
            if process.state() != QProcess.ProcessState.NotRunning:
                process.terminate()
        deadline = time.monotonic() + 2
        for process in processes:
            if process.state() != QProcess.ProcessState.NotRunning:
                process.waitForFinished(
                    max(1, int((deadline - time.monotonic()) * 1000))
                )
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
                process.waitForFinished(300)
        if self.results:
            self.results.hide()
        self.tile.hide()
