"""JSON-lines audio worker; no Qt or task-model dependencies.

stdin carries control objects, stdout carries protocol objects only. Native
model diagnostics go to stderr, including writes directly to file descriptor 1.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
from collections import deque

from .tts_runtime import TTSService
from .voice_runtime import VoiceService


class AudioController:
    def __init__(
        self,
        emit,
        *,
        voice_factory=VoiceService,
        tts_factory=TTSService,
        echo_seconds=0.2,
    ):
        self.emit = emit
        self._lock = threading.RLock()
        self._closed = False
        self._user_paused = False
        self._voice_ready = False
        self._speaking = set()
        self._reply_after_playback = set()
        self._echo_pause = False
        self._resume_generation = 0
        self._followup_generation = 0
        self._resume_timer = None
        self._last_level = {"level": 0.0, "speech": False, "listening": False}
        self._final_utterances = deque(maxlen=64)
        self.echo_seconds = max(0, echo_seconds)
        inference_lock = threading.Lock()
        self.voice = voice_factory(self._voice_event, inference_lock=inference_lock)
        self.tts = tts_factory(self._tts_event, inference_lock=inference_lock)

    def _send(self, event, **data):
        if not self._closed:
            self.emit(event, data)

    def start(self):
        self._send("audio.status", message="正在初始化本地语音…", listening=False)
        self.voice.start()

    def _update_pause(self):
        paused = (
            self._user_paused
            or bool(self._speaking)
            or self._echo_pause
            or self._closed
        )
        self.voice.set_paused(paused)
        if paused:
            self._send_level(listening=False)
        return paused

    def _send_level(self, *, level=0.0, speech=False, listening=False):
        """A tiny transient snapshot; silence produces no repeated IPC traffic."""
        if not listening or not speech:
            level, speech = 0.0, False
        payload = {"level": level, "speech": speech, "listening": listening}
        if level == 0 and payload == self._last_level:
            return
        self._last_level = payload
        self._send("audio.level", **payload)

    def _listening_message(self):
        if self._user_paused:
            return "监听已暂停"
        if self._speaking or self._echo_pause:
            return "播报期间暂停收音"
        if not self._voice_ready:
            return "语音输入尚未就绪"
        if getattr(self.voice, "wake_required", False):
            return f"等待唤醒：请先说“{self.voice.wake_word}”，再说任务。"
        return "正在聆听，说完后会自动发送。"

    def _wake_state(self):
        return {
            "wake_required": bool(getattr(self.voice, "wake_required", False)),
            "wake_word": getattr(self.voice, "wake_word", ""),
        }

    def _send_listening_status(self):
        """Publish control-driven gate changes without waiting for more speech."""
        paused = self._update_pause()
        self._send(
            "audio.status",
            message=self._listening_message(),
            listening=self._voice_ready and not paused,
            user_paused=self._user_paused,
            **self._wake_state(),
        )

    def _voice_event(self, name, data):
        with self._lock:
            if self._closed:
                return
            if name == "level":
                generation = data.get("generation")
                current = getattr(self.voice, "capture_generation", generation)
                if generation != current:
                    return
                listening = (
                    self._voice_ready
                    and data.get("listening") is True
                    and not self._update_pause()
                )
                level = data.get("level", 0.0)
                if type(level) not in (int, float) or (
                    type(level) is float and not math.isfinite(level)
                ):
                    level = 0.0
                self._send_level(
                    level=max(0.0, min(1.0, level)),
                    speech=data.get("speech") is True and level > 0,
                    listening=listening,
                )
            elif name == "transcript":
                generation = data.get("generation")
                current = getattr(self.voice, "capture_generation", generation)
                if (
                    generation is not None and generation != current
                ) or self._update_pause():
                    return
                text = data.get("text")
                if not isinstance(text, str) or not text.strip():
                    return
                final = data.get("final", True) is True
                utterance_id = data.get("utterance_id")
                if utterance_id and utterance_id in self._final_utterances:
                    return
                if final and utterance_id:
                    self._final_utterances.append(utterance_id)
                self._send(
                    "stt.final" if final else "stt.partial",
                    **{
                        "text": text,
                        "final": final,
                        **{
                            key: data[key]
                            for key in ("utterance_id", "generation")
                            if key in data
                        },
                    },
                )
            elif name in {"started", "cancelled"}:
                generation = data.get("generation")
                current = getattr(self.voice, "capture_generation", generation)
                if generation == current and not self._update_pause():
                    self._send("stt." + name, **data)
            elif name == "error":
                self._voice_ready = False
                self._send_level(listening=False)
                self._send(
                    "audio.error",
                    component="stt",
                    message=data.get("message", "语音输入不可用"),
                )
            else:
                if data.get("listening") and not self._voice_ready:
                    self._voice_ready = True
                    self._send(
                        "audio.ready",
                        listening=not self._update_pause(),
                        stt_ready=True,
                        tts_lazy=True,
                        **self._wake_state(),
                    )
                self._send(
                    "audio.status",
                    **{
                        **data,
                        "message": self._listening_message()
                        if data.get("message") == "持续聆听中"
                        else data.get("message", ""),
                        "listening": self._voice_ready and not self._update_pause(),
                        "user_paused": self._user_paused,
                        **self._wake_state(),
                    },
                )

    def _tts_event(self, name, data):
        with self._lock:
            if self._closed:
                return
            playback_id = data.get("playback_id")
            if name == "started":
                self._speaking.add(playback_id)
                self._followup_generation += 1
                self._resume_generation += 1
                self._echo_pause = False
                if self._resume_timer:
                    self._resume_timer.cancel()
                self._update_pause()
                self._send("tts.started", **data)
            elif name == "playing":
                self._send("tts.playing", **data)
            elif name == "ready":
                self._send(
                    "audio.status",
                    message="本地播报模型已加载",
                    tts_model_loaded=True,
                    listening=self._voice_ready and not self._update_pause(),
                    user_paused=self._user_paused,
                    **self._wake_state(),
                )
            elif name == "error":
                self._send("audio.error", component="tts", **data)
            elif name == "finished":
                was_active = playback_id in self._speaking
                self._speaking.discard(playback_id)
                followup = (
                    playback_id in self._reply_after_playback
                    and not data.get("cancelled")
                    and not data.get("failed")
                )
                self._reply_after_playback.discard(playback_id)
                self._send("tts.finished", **data)
                if was_active and not self._speaking:
                    self._schedule_resume(arm_followup=followup)

    def _schedule_resume(self, *, arm_followup=False):
        self._resume_generation += 1
        generation = self._resume_generation
        followup_generation = self._followup_generation
        self._echo_pause = True
        self._update_pause()
        if self._resume_timer:
            self._resume_timer.cancel()

        def finish():
            with self._lock:
                if self._closed or generation != self._resume_generation:
                    return
                self._echo_pause = False
                paused = self._update_pause()
                if (
                    arm_followup
                    and not paused
                    and followup_generation == self._followup_generation
                ):
                    self.voice.arm_followup(30)
                self._send(
                    "audio.status",
                    message=self._listening_message(),
                    listening=self._voice_ready and not paused,
                    user_paused=self._user_paused,
                    **self._wake_state(),
                )

        self._resume_timer = threading.Timer(self.echo_seconds, finish)
        self._resume_timer.daemon = True
        self._resume_timer.start()

    def handle(self, command: dict) -> bool:
        """Return False for shutdown; bad commands report errors and keep running."""
        if not isinstance(command, dict):
            self._send(
                "audio.error", component="ipc", message="音频指令必须是 JSON 对象"
            )
            return True
        request_id = command.get("id")
        action = command.get("command")
        try:
            with self._lock:
                if self._closed:
                    return False
                if action in {"pause", "resume"}:
                    self._user_paused = action == "pause"
                    if self._user_paused:
                        self._reply_after_playback.clear()
                        self._followup_generation += 1
                        self.voice.reset_wake()
                    paused = self._update_pause()
                    self._send(
                        "audio.status",
                        message=self._listening_message(),
                        listening=self._voice_ready and not paused,
                        user_paused=self._user_paused,
                        **self._wake_state(),
                    )
                elif action == "speak":
                    text = command.get("text")
                    if not isinstance(text, str) or len(text) > 12000:
                        raise ValueError("播报内容必须是最多 12000 字的文字")
                    playback_id = self.tts.speak(
                        text,
                        task_id=str(command.get("task_id", ""))[:128],
                        playback_id=command.get("playback_id"),
                    )
                    if command.get("awaiting_input") is True:
                        self._reply_after_playback.add(playback_id)
                    self._send(
                        "audio.ack",
                        id=request_id,
                        command=action,
                        playback_id=playback_id,
                    )
                    return True
                elif action == "stop_speaking":
                    self.tts.stop()
                    self._reply_after_playback.clear()
                    self._followup_generation += 1
                    self.voice.disarm_followup()
                    # Cancellation is checked before every playback callback.
                    # Resume after the tail guard without waiting for a native
                    # synthesis call whose result will now be discarded.
                    if self._speaking:
                        self._speaking.clear()
                        self._schedule_resume()
                    self._send_listening_status()
                elif action == "prewarm":
                    self.tts.prewarm()
                elif action == "expect_input":
                    if command.get("enabled", True) is False:
                        self._reply_after_playback.clear()
                        self._followup_generation += 1
                        self.voice.disarm_followup()
                    else:
                        seconds = command.get("timeout", 30)
                        if type(seconds) not in (int, float) or not 1 <= seconds <= 60:
                            raise ValueError("等待回答时长应位于 1–60 秒")
                        self.voice.arm_followup(seconds)
                    self._send_listening_status()
                elif action == "discard_input":
                    self._reply_after_playback.clear()
                    self._followup_generation += 1
                    self.voice.discard_input()
                    self._send(
                        "stt.discarded",
                        id=request_id,
                        generation=self.voice.capture_generation,
                    )
                    self._send_listening_status()
                elif action == "shutdown":
                    self._send("audio.ack", id=request_id, command=action)
                    return False
                else:
                    raise ValueError("不支持的音频指令")
                self._send("audio.ack", id=request_id, command=action)
        except (TypeError, ValueError, RuntimeError) as exc:
            self._send(
                "audio.error",
                component="ipc",
                id=request_id,
                message=str(exc),
                task_id=command.get("task_id"),
                playback_id=command.get("playback_id"),
            )
        return True

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._resume_generation += 1
            if self._resume_timer:
                self._resume_timer.cancel()
            self._update_pause()
        self.voice.stop()
        self.tts.shutdown()


def main() -> int:
    from .paths import load_environment

    load_environment()
    # Preserve protocol output first, then divert Python/native stdout noise.
    protocol = os.fdopen(
        os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1
    )
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    output_lock = threading.Lock()
    disconnected = threading.Event()

    def emit(name, data):
        if disconnected.is_set():
            return
        try:
            with output_lock:
                protocol.write(
                    json.dumps({"type": name, "data": data}, ensure_ascii=False) + "\n"
                )
                protocol.flush()
        except (BrokenPipeError, OSError):
            disconnected.set()

    controller = AudioController(emit)
    try:
        controller.start()
        while not disconnected.is_set():
            line = sys.stdin.buffer.readline(65537)
            if not line:
                break
            if len(line) > 65536:
                while line and not line.endswith(b"\n"):
                    line = sys.stdin.buffer.readline(65537)
                emit("audio.error", {"component": "ipc", "message": "音频指令过长"})
                continue
            try:
                command = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeError):
                emit("audio.error", {"component": "ipc", "message": "无法解析音频指令"})
                continue
            if not controller.handle(command):
                break
    finally:
        controller.close()
        protocol.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
