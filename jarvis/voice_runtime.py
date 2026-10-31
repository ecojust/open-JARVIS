"""Continuous local microphone capture, VAD segmentation and sherpa-onnx STT.

Audio is kept in bounded memory queues; it is neither uploaded nor recorded.
Optional native dependencies are imported only when the service starts.
"""

import os
import queue
import re
import threading
import time
from collections.abc import Callable

from .voice import Segmenter
from .audio_meter import SpeechMeter


class WakeWordGate:
    def __init__(self, word: str = "贾维斯", clock=time.monotonic):
        self.word = word.strip()
        self.clock = clock
        self.armed_until = 0.0
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
        self.armed_until = 0.0

    def accept(self, text: str) -> tuple[str | None, bool]:
        text = text.strip()
        if not text:
            return None, False
        if self.pattern is None:
            return text, False
        match = self.pattern.match(text)
        if match:
            command = text[match.end() :].strip()
            if command:
                self.reset()
                return command, False
            self.armed_until = self.clock() + 8
            return None, True
        if self.clock() < self.armed_until:
            self.reset()
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
        self._followup_until = 0.0
        self._thread = None
        self._decoder = None
        self._frames = queue.Queue(maxsize=100)
        self._utterances = queue.Queue(maxsize=3)
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
        self._reset_level()
        for queue_ in (self._frames, self._utterances):
            while True:
                try:
                    queue_.get_nowait()
                except queue.Empty:
                    break

    def stop(self):
        self._reset_level()
        self._stop.set()
        self._paused.set()
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

    def _accept_transcript(self, gate, text):
        command, armed = gate.accept(text)
        with self._lock:
            if command:
                self._followup_until = 0.0
            elif not armed and text.strip() and time.monotonic() < self._followup_until:
                self._followup_until = 0.0
                command = text.strip()
        return command, armed

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
                while not self._stop.is_set():
                    try:
                        generation, pcm = self._frames.get(timeout=0.1)
                    except queue.Empty:
                        self._reset_level(listening=True)
                        continue
                    current, paused = self._snapshot()
                    if current != previous_generation:
                        segmenter.reset()
                        self._reset_level(listening=True)
                        previous_generation = current
                    if paused or generation != current:
                        continue
                    speech = vad.is_speech(pcm, 16000)
                    self._meter_frame(pcm, speech, generation)
                    utterance = segmenter.feed(pcm, speech)
                    if utterance:
                        try:
                            self._utterances.put_nowait((generation, utterance))
                        except queue.Full:
                            self._emit(
                                "status",
                                message="语音识别队列已满，请稍后再说。",
                                listening=True,
                            )
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
        previous_generation = -1
        while not self._stop.is_set() and not self._capture_done.is_set():
            try:
                generation, pcm = self._utterances.get(timeout=0.1)
            except queue.Empty:
                continue
            current, paused = self._snapshot()
            if current != previous_generation:
                gate.reset()
                previous_generation = current
            if paused or current != generation:
                continue
            self._emit("status", message="正在识别语音…", listening=True)
            try:
                with self.inference_lock:
                    current, paused = self._snapshot()
                    if paused or current != generation or self._stop.is_set():
                        continue
                    stream = recognizer.create_stream()
                    samples = (
                        np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                    )
                    stream.accept_waveform(16000, samples)
                    recognizer.decode_stream(stream)
                current, paused = self._snapshot()
                if paused or current != generation or self._stop.is_set():
                    continue
                command, armed = self._accept_transcript(gate, stream.result.text)
                if command:
                    self._emit("transcript", text=command)
                elif armed:
                    self._emit(
                        "status", message="我在，请说任务（8 秒内）。", listening=True
                    )
                else:
                    self._emit("status", message="持续聆听中", listening=True)
            except Exception as exc:
                self._emit("error", message=f"本地语音识别失败：{exc}")
