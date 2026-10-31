"""Local Kokoro synthesis and bounded, cancellable audio playback.

Importing this module loads no model, NumPy, or audio driver. A single worker
owns the native TTS instance, and synthesis uses the same lock as local STT.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid


def prepare_text(text: str, maximum: int = 600) -> str:
    """Turn a bounded spoken summary into plain text without reading URLs/code."""
    if not isinstance(text, str):
        raise ValueError("播报内容必须是文字")
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"(?m)^\s*(?:#{1,6}\s*|[-*+]\s+|>\s*)", "", text)
    text = re.sub(r"[`*_~]", "", text)
    text = " ".join(text.split())
    if len(text) > maximum:
        text = text[:maximum].rstrip("，,；;：:") + "。完整内容请查看结果窗口。"
    return text


def speech_chunks(text: str, limit: int = 80) -> list[str]:
    """Prefer a short natural first clause, preserving all spoken content.

    Long first sentences can delay the first audio until the whole sentence is
    synthesized. Split only at an existing pause after useful content; a short
    greeting or a number such as ``12:30`` is not an artificial first segment.
    """
    if limit < 8:
        raise ValueError("语音片段长度过小")
    sentences = re.findall(r"[^。！？!?；;]+[。！？!?；;]?", text)
    chunks = []
    for sentence in sentences:
        sentence = sentence.strip()
        first_limit = min(32, limit)
        if not chunks and len(sentence) > first_limit:
            for index in range(8, first_limit):
                if sentence[index] not in "，,：:":
                    continue
                if sentence[index - 1].isdigit() and sentence[index + 1].isdigit():
                    continue
                chunks.append(sentence[: index + 1].strip())
                sentence = sentence[index + 1 :].strip()
                break
        while len(sentence) > limit:
            split = max(
                sentence.rfind(mark, 0, limit) for mark in ("，", ",", " ", "：", ":")
            )
            split = split + 1 if split >= limit // 3 else limit
            chunks.append(sentence[:split].strip())
            sentence = sentence[split:].strip()
        if sentence:
            chunks.append(sentence)
    return chunks


def build_tts_engine(directory: Path | str | None = None):
    """Build the actual sherpa-onnx Kokoro engine; useful for waveform checks."""
    from .download_tts_model import model_ready

    if directory is None:
        from .paths import model_dir

        directory = model_dir("kokoro")
    directory = Path(directory)
    if not model_ready(directory):
        raise ValueError(
            "Kokoro 语音模型缺失或不兼容。请在客户端设置的语音合成模型一行点击下载或校验。"
        )
    import sherpa_onnx

    threads = max(1, min(8, int(os.getenv("JARVIS_TTS_THREADS", "2"))))
    kokoro = sherpa_onnx.OfflineTtsKokoroModelConfig(
        model=str(directory / "model.onnx"),
        voices=str(directory / "voices.bin"),
        tokens=str(directory / "tokens.txt"),
        lexicon=",".join(
            str(directory / name) for name in ("lexicon-us-en.txt", "lexicon-zh.txt")
        ),
        data_dir=str(directory / "espeak-ng-data"),
        lang="",  # Official bilingual v1.1 configuration; do not force en/zh.
    )
    config = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=kokoro, num_threads=threads, provider="cpu", debug=False
        ),
        rule_fsts=",".join(
            str(directory / name)
            for name in ("date-zh.fst", "number-zh.fst", "phone-zh.fst")
        ),
        max_num_sentences=1,
    )
    if not config.validate():
        raise ValueError("Kokoro 模型配置无效，请重新检查模型目录。")
    return sherpa_onnx.OfflineTts(config)


def synthesize(engine, text: str, speaker: int = 3, speed: float = 1.0):
    """Generate real audio, without accessing a speaker or creating files."""
    if type(speaker) is not int or not 0 <= speaker < engine.num_speakers:
        raise ValueError(f"音色编号无效，允许 0–{engine.num_speakers - 1}")
    if not 0.5 <= speed <= 2.0:
        raise ValueError("语速应位于 0.5–2.0")
    if not text.strip():
        raise ValueError("没有可播报的文字")
    audio = engine.generate(text, sid=speaker, speed=speed)
    if not len(audio.samples) or audio.sample_rate <= 0:
        raise ValueError("语音模型没有生成有效音频")
    return audio


def play_samples(audio, cancelled: threading.Event, on_first_audio=None) -> bool:
    """Play one segment with a private stream, stopping within one callback."""
    import numpy as np
    import sounddevice as sd

    samples = np.asarray(audio.samples, dtype=np.float32).reshape(-1)
    if not samples.size or not np.isfinite(samples).all():
        raise ValueError("语音模型生成了无效采样")
    finished, first = threading.Event(), threading.Event()
    position = 0

    def callback(output, frames, timing, status):
        nonlocal position
        output.fill(0)
        if cancelled.is_set():
            raise sd.CallbackAbort
        count = min(frames, len(samples) - position)
        if count:
            output[:count, 0] = samples[position : position + count]
            position += count
            first.set()
        if position >= len(samples):
            raise sd.CallbackStop

    device = os.getenv("JARVIS_SPEAKER_DEVICE", "").strip()
    device = int(device) if device.isdigit() else (device or None)
    notified = False
    with sd.OutputStream(
        samplerate=audio.sample_rate,
        channels=1,
        dtype="float32",
        blocksize=1024,
        device=device,
        callback=callback,
        finished_callback=finished.set,
    ) as stream:
        while not finished.wait(0.025):
            if first.is_set() and not notified:
                notified = True
                if on_first_audio:
                    on_first_audio()
            if cancelled.is_set():
                stream.abort()
                return False
        if first.is_set() and not notified and on_first_audio:
            on_first_audio()
    return not cancelled.is_set() and position == len(samples)


@dataclass
class _Job:
    playback_id: str
    task_id: str
    text: str
    cancelled: threading.Event = field(default_factory=threading.Event)


class TTSService:
    """One lazy synthesis worker and at most eight pending spoken summaries."""

    def __init__(
        self,
        on_event,
        *,
        inference_lock=None,
        engine_factory=None,
        player=None,
        cache_seconds: float | None = None,
        max_pending: int = 8,
    ):
        self.on_event = on_event
        self.inference_lock = inference_lock or threading.Lock()
        self.engine_factory = engine_factory or build_tts_engine
        self.player = player or play_samples
        # Zero disables retention; invalid configuration keeps a useful default.
        try:
            retention = float(
                os.getenv("JARVIS_TTS_IDLE_SECONDS", "120")
                if cache_seconds is None
                else cache_seconds
            )
        except (TypeError, ValueError):
            retention = 120.0
        self.cache_seconds = (
            min(3600.0, max(0.0, retention)) if math.isfinite(retention) else 120.0
        )
        self.max_pending = max(1, max_pending)
        self._condition = threading.Condition(threading.RLock())
        self._jobs = deque()
        self._active = None
        self._thread = None
        self._closed = False
        self._warm_requested = False

    def _emit(self, name: str, job: _Job, **data):
        if not self._closed:
            self.on_event(
                name, {"playback_id": job.playback_id, "task_id": job.task_id, **data}
            )

    def speak(
        self, text: str, task_id: str = "", playback_id: str | None = None
    ) -> str:
        text = prepare_text(text)
        if not text:
            raise ValueError("没有可播报的文字")
        with self._condition:
            if self._closed:
                raise RuntimeError("语音服务已经关闭")
            if len(self._jobs) >= self.max_pending:
                raise ValueError("待播报队列已满，请稍后再试")
            playback_id = playback_id or uuid.uuid4().hex
            if not isinstance(playback_id, str) or len(playback_id) > 128:
                raise ValueError("播报标识无效")
            if any(item.playback_id == playback_id for item in self._jobs) or (
                self._active and self._active.playback_id == playback_id
            ):
                raise ValueError("播报标识已在使用")
            job = _Job(playback_id, str(task_id), text)
            self._jobs.append(job)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="jarvis-tts", daemon=True
                )
                self._thread.start()
            self._condition.notify()
            return job.playback_id

    def prewarm(self):
        """Load only when useful work has begun; never activates the speaker."""
        with self._condition:
            if self._closed:
                return
            self._warm_requested = True
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="jarvis-tts", daemon=True
                )
                self._thread.start()
            self._condition.notify()

    def stop(self):
        with self._condition:
            if self._active:
                self._active.cancelled.set()
            dropped = list(self._jobs)
            self._jobs.clear()
            self._condition.notify_all()
        for job in dropped:
            self._emit("finished", job, cancelled=True, queued=True)

    def shutdown(self, timeout: float = 3):
        with self._condition:
            self._closed = True
        self.stop()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)

    def _run(self):
        engine = None
        used_at = 0.0
        while True:
            with self._condition:
                while not self._jobs and not self._warm_requested and not self._closed:
                    remaining = (
                        self.cache_seconds - (time.monotonic() - used_at)
                        if engine is not None
                        else None
                    )
                    if remaining is not None and remaining <= 0:
                        # Drop the native instance. OS memory release is measured,
                        # not inferred from this assignment or from Python GC.
                        engine = None
                        remaining = None
                    self._condition.wait(remaining)
                if self._closed:
                    return
                job = self._jobs.popleft() if self._jobs else None
                self._warm_requested = False
                self._active = job
            if job is None:
                try:
                    with self.inference_lock:
                        if engine is None and not self._closed:
                            engine = self.engine_factory()
                    used_at = time.monotonic()
                    if not self._closed:
                        self.on_event("ready", {"loaded": True})
                except Exception as exc:
                    if not self._closed:
                        self.on_event(
                            "error",
                            {"message": f"语音模型预加载失败：{exc}", "prewarm": True},
                        )
                continue
            error = False
            self._emit("started", job)
            try:
                speaker = int(os.getenv("JARVIS_TTS_SPEAKER", "3"))
                speed = float(os.getenv("JARVIS_TTS_SPEED", "1.0"))
                first_audio = False

                def began_playback():
                    nonlocal first_audio
                    if not first_audio and not job.cancelled.is_set():
                        first_audio = True
                        self._emit("playing", job)

                for chunk in speech_chunks(job.text):
                    if job.cancelled.is_set() or self._closed:
                        break
                    with self.inference_lock:
                        if job.cancelled.is_set() or self._closed:
                            break
                        if engine is None:
                            engine = self.engine_factory()
                        audio = synthesize(engine, chunk, speaker, speed)
                    used_at = time.monotonic()
                    if job.cancelled.is_set() or self._closed:
                        break
                    if not self.player(audio, job.cancelled, began_playback):
                        job.cancelled.set()
                        break
            except Exception as exc:
                error = True
                self._emit("error", job, message=f"语音播报失败：{exc}")
            finally:
                with self._condition:
                    self._active = None
                self._emit(
                    "finished", job, cancelled=job.cancelled.is_set(), failed=error
                )
