"""Continuous local microphone capture, VAD segmentation and sherpa-onnx STT.

Audio is kept in bounded memory queues; it is neither uploaded nor recorded.
Optional native dependencies are imported only when the service starts.
"""

import os
import queue
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable

from .voice import Segmenter
from .audio_meter import SpeechMeter


class WakeWordGate:
    def __init__(self, word: str = "贾维斯"):
        self.word = word.strip()
        self.engaged = False
        words = [self.word] if self.word else []
        if self.word == "贾维斯":
            words += ["加维斯", "贾维思", "佳维斯", "jarvis"]
        self.pattern = (
            re.compile(
                r"^(?:你好[，,、\s]*)?(?:"
                + "|".join(re.escape(w) for w in words)
                + r")[，,。.!！?？:：、\s]*",
                re.IGNORECASE,
            )
            if words
            else None
        )

    def reset(self):
        self.engaged = False

    def accept(self, text: str) -> tuple[str | None, bool]:
        return self._accept(text, consume=True)

    def preview(self, text: str) -> tuple[str | None, bool]:
        """Inspect a changing prefix without consuming or extending wake state."""
        return self._accept(text, consume=False)

    def _accept(self, text: str, *, consume: bool) -> tuple[str | None, bool]:
        text = text.strip()
        if not text:
            return None, False
        if self.pattern is None:
            return text, False
        match = self.pattern.match(text)
        if match:
            command = text[match.end() :].strip()
            if consume:
                self.engaged = True
            if command:
                return command, False
            return None, True
        if self.engaged:
            return text, False
        return None, False


class VoiceService:
    def __init__(self, on_event: Callable[[str, dict], None], *, inference_lock=None):
        from .paths import load_environment

        load_environment()
        self.on_event = on_event
        self.inference_lock = inference_lock or threading.Lock()
        self._stop = threading.Event()
        self._capture_done = threading.Event()
        self._paused = threading.Event()
        self._lock = threading.Lock()
        self._generation = 0
        self._wake_epoch = 0
        self._followup_until = 0.0
        self._thread = None
        self._decoder = None
        self._frames = queue.Queue(maxsize=100)
        self._utterances = queue.Queue(maxsize=3)
        self._pending_partial = None
        self._finalizing = set()
        self._completed_utterances = deque(maxlen=64)
        self._decode_wake = threading.Event()
        self._preview_interval = 1.5
        self._preview_ready_at = 0.0
        self._meter = SpeechMeter()

    def _emit(self, name: str, **data):
        if not self._stop.is_set():
            self.on_event(name, data)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        if self._decoder and self._decoder.is_alive():
            self._emit("error", message="上次语音识别尚未结束，请重启语音服务。")
            return
        self._stop.clear()
        self._capture_done.clear()
        self._thread = threading.Thread(
            target=self._run, name="jarvis-microphone", daemon=True
        )
        self._thread.start()

    def set_paused(self, paused: bool):
        with self._lock:
            if paused == self._paused.is_set():
                return
            self._generation += 1
            if paused:
                self._paused.set()
                self._followup_until = 0.0
            else:
                self._paused.clear()
            self._pending_partial = None
            self._finalizing.clear()
        self._discard_queued_audio()
        self._reset_level()

    def _discard_queued_audio(self):
        for queue_ in (self._frames, self._utterances):
            while True:
                try:
                    queue_.get_nowait()
                except queue.Empty:
                    break
        self._decode_wake.set()

    def discard_input(self):
        """Discard speech from the previous conversation without changing pause."""
        with self._lock:
            self._generation += 1
            self._followup_until = 0.0
            self._pending_partial = None
            self._finalizing.clear()
        self._discard_queued_audio()
        self._reset_level()

    def stop(self):
        self._reset_level()
        self._stop.set()
        self._paused.set()
        self._decode_wake.set()
        self.reset_wake()
        for thread in (self._thread, self._decoder):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=2)

    def _snapshot(self):
        with self._lock:
            return self._generation, self._paused.is_set()

    @property
    def capture_generation(self):
        """Identify audio captured before a pause or device discontinuity."""
        return self._snapshot()[0]

    def _publish_level(self, payload, generation):
        if payload is None:
            return
        current, paused = self._snapshot()
        if generation != current or (paused and payload["speech"]):
            return
        self._emit("level", **payload, generation=generation)

    def _reset_level(self, *, listening=False):
        generation, paused = self._snapshot()
        self._publish_level(
            self._meter.reset(listening=listening and not paused), generation
        )

    def _meter_frame(self, pcm, speech, generation):
        """Called only by the capture consumer, never the audio callback."""
        current, paused = self._snapshot()
        if paused or generation != current or self._stop.is_set():
            return
        self._publish_level(self._meter.feed(pcm, speech), generation)

    def arm_followup(self, seconds: float = 30):
        """Accept one answer after an explicitly asked question, not all speech."""
        with self._lock:
            if not self._paused.is_set() and not self._stop.is_set():
                self._followup_until = time.monotonic() + max(1, min(60, seconds))

    def disarm_followup(self):
        with self._lock:
            self._followup_until = 0.0

    def reset_wake(self):
        """Manual pause ends a conversation; echo/session pauses do not."""
        with self._lock:
            self._wake_epoch += 1
            self._followup_until = 0.0
        self._decode_wake.set()

    def _accept_transcript(self, gate, text):
        command, armed = gate.accept(text)
        with self._lock:
            if command:
                self._followup_until = 0.0
            elif not armed and text.strip() and time.monotonic() < self._followup_until:
                self._followup_until = 0.0
                command = text.strip()
        return command, armed

    def _preview_transcript(self, gate, text):
        command, armed = gate.preview(text)
        with self._lock:
            if not command and not armed and time.monotonic() < self._followup_until:
                command = text.strip() or None
        return command

    def _queue_partial(self, generation, utterance_id, pcm):
        with self._lock:
            if (
                generation != self._generation
                or self._paused.is_set()
                or self._stop.is_set()
                or utterance_id in self._finalizing
                or utterance_id in self._completed_utterances
                or not self._utterances.empty()
            ):
                return
            # A slow decoder replaces this one snapshot; it never accumulates
            # obsolete previews or prevents a final utterance being queued.
            self._pending_partial = (generation, pcm, utterance_id)
        self._decode_wake.set()

    def _queue_final(self, generation, utterance_id, pcm):
        with self._lock:
            if (
                generation != self._generation
                or self._paused.is_set()
                or self._stop.is_set()
                or utterance_id in self._finalizing
                or utterance_id in self._completed_utterances
            ):
                return
            if self._pending_partial and self._pending_partial[2] == utterance_id:
                self._pending_partial = None
            try:
                self._utterances.put_nowait((generation, pcm, utterance_id))
            except queue.Full:
                queued = False
            else:
                self._finalizing.add(utterance_id)
                queued = True
        if queued:
            self._decode_wake.set()
        else:
            self._emit(
                "cancelled",
                utterance_id=utterance_id,
                generation=generation,
                reason="queue_full",
            )
            self._emit(
                "status", message="语音识别队列已满，请稍后再说。", listening=True
            )

    def _decode_job(self):
        """Finals take priority; an idle decoder waits on a producer signal."""
        self._decode_wake.clear()
        try:
            item = self._utterances.get_nowait()
        except queue.Empty:
            with self._lock:
                if time.monotonic() < self._preview_ready_at:
                    return None
                item, self._pending_partial = self._pending_partial, None
            return (*item, False) if item else None
        # Retain the legacy final-only queue contract for embedded integrations.
        if len(item) == 2:
            item = (*item, uuid.uuid4().hex)
        return (*item, True)

    def _current_job(self, generation, utterance_id, final):
        with self._lock:
            return (
                generation == self._generation
                and not self._paused.is_set()
                and not self._stop.is_set()
                and utterance_id not in self._completed_utterances
                and (
                    final
                    or (
                        utterance_id not in self._finalizing
                        and self._utterances.empty()
                    )
                )
            )

    def _run(self):
        try:
            import sounddevice as sd
            import sherpa_onnx
            import webrtcvad
            from .model_catalog import inspect_directory
            from .paths import model_dir

            directory = model_dir("sensevoice")
            model = directory / "model.int8.onnx"
            tokens = directory / "tokens.txt"
            compatibility = inspect_directory("stt", directory)
            if compatibility["state"] != "ready":
                self._emit(
                    "error",
                    message=compatibility.get("message")
                    or "语音识别模型未就绪。请在客户端设置的语音识别模型一行点击下载。",
                )
                return
            self._emit("status", message="正在加载本地语音模型…", listening=False)
            with self.inference_lock:
                recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=str(model),
                    tokens=str(tokens),
                    language="zh",
                    use_itn=True,
                    num_threads=max(
                        1, min(8, int(os.getenv("JARVIS_STT_THREADS", "2")))
                    ),
                    provider="cpu",
                    debug=False,
                )
            if self._stop.is_set():
                return
            vad = webrtcvad.Vad(2)
            segmenter = Segmenter()
            device = os.getenv("JARVIS_MIC_DEVICE", "").strip()
            device = int(device) if device.isdigit() else (device or None)

            def callback(indata, frames, timing, status):
                generation, paused = self._snapshot()
                if paused or self._stop.is_set():
                    return
                if status:
                    with self._lock:
                        self._generation += 1
                        generation = self._generation
                try:
                    self._frames.put_nowait((generation, bytes(indata)))
                except queue.Full:
                    # Mark a discontinuity: never concatenate separated audio.
                    with self._lock:
                        self._generation += 1

            with sd.RawInputStream(
                samplerate=16000,
                blocksize=480,
                dtype="int16",
                channels=1,
                device=device,
                callback=callback,
            ):
                self._decoder = threading.Thread(
                    target=self._decode,
                    args=(recognizer,),
                    name="jarvis-stt",
                    daemon=True,
                )
                self._decoder.start()
                self._emit("status", message="持续聆听中", listening=True)
                previous_generation = -1
                utterance_id = None
                next_preview = 0.0
                while not self._stop.is_set():
                    try:
                        generation, pcm = self._frames.get(timeout=0.1)
                    except queue.Empty:
                        self._reset_level(listening=True)
                        continue
                    current, paused = self._snapshot()
                    if current != previous_generation:
                        segmenter.reset()
                        utterance_id = None
                        self._reset_level(listening=True)
                        previous_generation = current
                    if paused or generation != current:
                        continue
                    speech = vad.is_speech(pcm, 16000)
                    self._meter_frame(pcm, speech, generation)
                    if speech and not segmenter.active:
                        utterance_id = uuid.uuid4().hex
                        next_preview = time.monotonic() + self._preview_interval
                        self._emit(
                            "started", utterance_id=utterance_id, generation=generation
                        )
                    utterance = segmenter.feed(pcm, speech)
                    if utterance:
                        self._queue_final(generation, utterance_id, utterance)
                        utterance_id = None
                    elif not segmenter.active and utterance_id:
                        self._emit(
                            "cancelled",
                            utterance_id=utterance_id,
                            generation=generation,
                            reason="short_noise",
                        )
                        utterance_id = None
                    elif speech and time.monotonic() >= next_preview:
                        # Copy on the consumer only, at a bounded and adaptive
                        # rate. Never run inference or copy the whole sentence
                        # from PortAudio's real-time callback.
                        snapshot = segmenter.snapshot()
                        if snapshot:
                            self._queue_partial(generation, utterance_id, snapshot)
                        next_preview = time.monotonic() + self._preview_interval
        except ImportError:
            self._emit(
                "error",
                message="语音组件未能加载，请重新安装最新版客户端。文字任务仍可使用。",
            )
        except Exception as exc:
            self._emit(
                "error",
                message=f"语音输入不可用：{exc}。请检查麦克风权限、输入设备和本地模型。",
            )
        finally:
            self._capture_done.set()
            with self._lock:
                self._generation += 1
            self._reset_level()

    def _decode(self, recognizer):
        import numpy as np

        gate = WakeWordGate(os.getenv("JARVIS_WAKE_WORD", "贾维斯"))
        previous_wake_epoch = -1
        last_preview = None
        while not self._stop.is_set() and not self._capture_done.is_set():
            job = self._decode_job()
            if job is None:
                self._decode_wake.wait(timeout=0.5)
                continue
            generation, pcm, utterance_id, final = job
            with self._lock:
                wake_epoch = self._wake_epoch
            if wake_epoch != previous_wake_epoch:
                gate.reset()
                last_preview = None
                previous_wake_epoch = wake_epoch
            if not self._current_job(generation, utterance_id, final):
                continue
            if final:
                self._emit("status", message="正在识别语音…", listening=True)
            try:
                with self.inference_lock:
                    if not self._current_job(generation, utterance_id, final):
                        continue
                    stream = recognizer.create_stream()
                    samples = (
                        np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                    )
                    stream.accept_waveform(16000, samples)
                    started = time.monotonic()
                    recognizer.decode_stream(stream)
                    if not final:
                        # At most roughly half of a slow CPU's time goes to
                        # repeated prefix decoding; finals are always eligible.
                        elapsed = time.monotonic() - started
                        self._preview_interval = max(1.5, 2 * elapsed)
                        self._preview_ready_at = started + self._preview_interval
                if not self._current_job(generation, utterance_id, final):
                    continue
                if final:
                    command, armed = self._accept_transcript(gate, stream.result.text)
                    if command:
                        self._emit(
                            "transcript",
                            text=command,
                            final=True,
                            utterance_id=utterance_id,
                            generation=generation,
                        )
                    else:
                        self._emit(
                            "cancelled",
                            utterance_id=utterance_id,
                            generation=generation,
                            reason="wake_only" if armed else "not_addressed",
                        )
                        self._emit(
                            "status",
                            message="我在，请说任务；暂停前可以连续对话。"
                            if armed
                            else "持续聆听中",
                            listening=True,
                        )
                else:
                    command = self._preview_transcript(gate, stream.result.text)
                    if command and last_preview != (utterance_id, command):
                        self._emit(
                            "transcript",
                            text=command,
                            final=False,
                            utterance_id=utterance_id,
                            generation=generation,
                        )
                        last_preview = (utterance_id, command)
            except Exception as exc:
                if final:
                    self._emit(
                        "cancelled",
                        utterance_id=utterance_id,
                        generation=generation,
                        reason="decode_error",
                    )
                    self._emit("error", message=f"本地语音识别失败：{exc}")
                # A preview is best effort. Its failure must not disable the
                # microphone or lose the final whole-utterance transcription.
            finally:
                if final:
                    with self._lock:
                        self._finalizing.discard(utterance_id)
                        self._completed_utterances.append(utterance_id)
