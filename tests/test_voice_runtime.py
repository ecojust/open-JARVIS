"""Voice control tests without microphones, native packages, or model downloads."""

import threading
import struct
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jarvis.voice_runtime import VoiceService, WakeWordGate
from jarvis.audio_meter import SpeechMeter


class WakeWordGateTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.gate = WakeWordGate(clock=lambda: self.now)

    def test_command_requires_wake_prefix_and_strips_greeting_punctuation(self):
        self.assertEqual(self.gate.accept("帮我写报告"), (None, False))
        self.assertEqual(self.gate.accept("今天贾维斯帮我写报告"), (None, False))
        self.assertEqual(
            self.gate.accept("你好，贾维斯：帮我写报告"), ("帮我写报告", False)
        )
        self.assertEqual(self.gate.accept("  贾维斯，分析数据  "), ("分析数据", False))

    def test_standalone_wake_arms_exactly_one_command_for_eight_seconds(self):
        self.assertEqual(self.gate.accept("贾维斯！"), (None, True))
        self.now += 7.9
        self.assertEqual(self.gate.accept("生成报告"), ("生成报告", False))
        self.assertEqual(self.gate.accept("再生成一份"), (None, False))
        self.assertEqual(self.gate.accept("贾维斯"), (None, True))
        self.now += 8
        self.assertEqual(self.gate.accept("超时的指令"), (None, False))

    def test_common_transcription_aliases_are_accepted(self):
        for word in ("贾维斯", "加维斯", "贾维思", "佳维斯", "JARVIS", "Jarvis"):
            with self.subTest(word=word):
                self.assertEqual(
                    self.gate.accept(word + "，整理资料"), ("整理资料", False)
                )

    def test_custom_wake_word_and_empty_word_bypass(self):
        gate = WakeWordGate("小助手")
        self.assertEqual(gate.accept("贾维斯，执行任务"), (None, False))
        self.assertEqual(gate.accept("小助手，执行任务"), ("执行任务", False))
        bypass = WakeWordGate("")
        self.assertEqual(bypass.accept("  直接执行任务  "), ("直接执行任务", False))
        self.assertEqual(bypass.accept("  "), (None, False))

    def test_reset_cancels_armed_state_and_empty_text_does_not_consume_it(self):
        self.gate.accept("贾维斯")
        self.assertEqual(self.gate.accept(""), (None, False))
        self.assertEqual(self.gate.accept("任务"), ("任务", False))
        self.gate.accept("贾维斯")
        self.gate.reset()
        self.assertEqual(self.gate.accept("任务"), (None, False))


class _FakeSamples:
    def astype(self, dtype):
        return self

    def __truediv__(self, divisor):
        return self


class VoiceServiceTests(unittest.TestCase):
    def test_capture_callback_only_queues_pcm_and_consumer_reuses_one_vad_decision(
        self,
    ):
        events = []
        decisions = []
        pcm = struct.pack("<480h", *([3000, -3000] * 240))

        def on_event(name, data):
            events.append((name, data))
            if name == "level" and data["speech"]:
                service._stop.set()

        service = VoiceService(on_event)

        class InputStream:
            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]

            def __enter__(self):
                self.callback(pcm, 480, None, False)
                self_callback_levels = [name for name, _ in events if name == "level"]
                if self_callback_levels:
                    raise AssertionError("capture callback performed metering")
                return self

            def __exit__(self, *args):
                return False

        class Vad:
            def __init__(self, mode):
                pass

            def is_speech(self, frame, rate):
                decisions.append((frame, rate))
                return True

        # Simulated microphone/model boundaries make this deterministic. The
        # production capture consumer still reads actual int16 bytes and RMS.
        # Keep NumPy outside the module patch so its native extension is not
        # removed and re-imported by later tests.
        __import__("numpy")
        with (
            patch("jarvis.paths.model_dir", return_value=Path("unused-test-model")),
            patch.dict(
                "sys.modules",
                {
                    "sounddevice": SimpleNamespace(RawInputStream=InputStream),
                    "sherpa_onnx": SimpleNamespace(
                        OfflineRecognizer=SimpleNamespace(
                            from_sense_voice=lambda **kwargs: object()
                        )
                    ),
                    "webrtcvad": SimpleNamespace(Vad=Vad),
                    "jarvis.model_catalog": SimpleNamespace(
                        inspect_directory=lambda *args: {"state": "ready"}
                    ),
                },
            ),
        ):
            service._run()
            service.stop()
        self.assertFalse([data for name, data in events if name == "error"])
        self.assertEqual(decisions, [(pcm, 16000)])
        levels = [data for name, data in events if name == "level" and data["speech"]]
        self.assertEqual(len(levels), 1)
        self.assertGreater(levels[0]["level"], 0)
        self.assertNotIn("pcm", levels[0])

    def test_meter_uses_real_pcm_and_discards_frames_across_pause_and_input_gap(self):
        events = []
        now = [10.0]
        service = VoiceService(lambda name, data: events.append((name, data)))
        service._meter = SpeechMeter(clock=lambda: now[0])
        quiet = struct.pack("<480h", *([800, -800] * 240))
        loud = struct.pack("<480h", *([8000, -8000] * 240))
        service._meter_frame(quiet, True, 0)
        first = events[-1][1]
        self.assertGreater(first["level"], 0)
        now[0] += 0.1
        service._meter_frame(loud, True, 0)
        self.assertGreater(events[-1][1]["level"], first["level"])
        self.assertEqual(events[-1][1]["generation"], 0)

        service.set_paused(True)
        self.assertEqual(events[-1][1]["level"], 0)
        self.assertFalse(events[-1][1]["listening"])
        old_count = len(events)
        service._meter_frame(loud, True, 0)
        self.assertEqual(len(events), old_count)
        service.set_paused(False)
        now[0] += 0.1
        service._meter_frame(loud, True, 0)
        self.assertEqual(len(events), old_count)
        service._meter_frame(loud, True, service.capture_generation)
        self.assertTrue(events[-1][1]["speech"])

        # A device overflow increments the same generation and resets the
        # segmenter/meter in the consumer; old queued frames cannot reanimate.
        prior_generation = service.capture_generation
        with service._lock:
            service._generation += 1
        service._reset_level(listening=True)
        self.assertEqual(events[-1][1]["level"], 0)
        self.assertTrue(events[-1][1]["listening"])
        now[0] += 0.1
        old_count = len(events)
        service._meter_frame(loud, True, prior_generation)
        self.assertEqual(len(events), old_count)
        service._meter_frame(loud, True, service.capture_generation)
        service.stop()
        self.assertEqual(events[-1][1]["level"], 0)
        self.assertFalse(events[-1][1]["listening"])
        old_count = len(events)
        service._meter_frame(loud, True, service.capture_generation)
        self.assertEqual(len(events), old_count)
        self.assertTrue(all(name == "level" for name, _ in events))
        self.assertTrue(all("pcm" not in data for _, data in events))

    def test_followup_accepts_one_plain_answer_and_pause_cancels_it(self):
        service = VoiceService(lambda *args: None)
        gate = WakeWordGate()
        self.assertEqual(service._accept_transcript(gate, "明天"), (None, False))
        service.arm_followup()
        self.assertEqual(service._accept_transcript(gate, "明天"), ("明天", False))
        self.assertEqual(service._accept_transcript(gate, "后天"), (None, False))
        service.arm_followup()
        service.set_paused(True)
        service.set_paused(False)
        self.assertEqual(service._accept_transcript(gate, "后天"), (None, False))

    def test_pause_and_resume_discard_inflight_stale_transcription(self):
        started = threading.Event()
        release = threading.Event()
        received_fresh = threading.Event()
        output = []

        def on_event(name, data):
            output.append((name, data))
            if name == "transcript" and data["text"] == "新的任务":
                received_fresh.set()

        class Recognizer:
            calls = 0

            def create_stream(self):
                return SimpleNamespace(
                    accept_waveform=lambda rate, samples: None,
                    result=SimpleNamespace(text=""),
                )

            def decode_stream(self, stream):
                self.calls += 1
                if self.calls == 1:
                    started.set()
                    release.wait(timeout=3)
                    stream.result.text = "贾维斯，过期任务"
                else:
                    stream.result.text = "贾维斯，新的任务"

        fake_numpy = SimpleNamespace(
            frombuffer=lambda *args, **kwargs: _FakeSamples(),
            int16="int16",
            float32="float32",
        )
        with (
            patch.dict("sys.modules", {"numpy": fake_numpy}),
            patch.dict("os.environ", {"JARVIS_WAKE_WORD": "贾维斯"}),
        ):
            service = VoiceService(on_event)
            service._utterances.put((0, b"\0" * 960))
            worker = threading.Thread(
                target=service._decode, args=(Recognizer(),), daemon=True
            )
            service._decoder = worker
            worker.start()
            try:
                self.assertTrue(started.wait(timeout=2), "decoder did not begin")
                service.set_paused(True)
                service.set_paused(False)
                generation, paused = service._snapshot()
                self.assertFalse(paused)
                service._utterances.put((generation, b"\0" * 960))
                release.set()
                self.assertTrue(
                    received_fresh.wait(timeout=2), "fresh command was not accepted"
                )
            finally:
                release.set()
                service.stop()
            self.assertFalse(worker.is_alive())
        self.assertEqual(
            [data["text"] for name, data in output if name == "transcript"],
            ["新的任务"],
        )

    def test_pause_is_idempotent_and_stopped_service_does_not_emit(self):
        output = []
        service = VoiceService(lambda name, data: output.append((name, data)))
        service.set_paused(True)
        generation, paused = service._snapshot()
        self.assertTrue(paused)
        service.set_paused(True)
        self.assertEqual(service._snapshot(), (generation, True))
        service.set_paused(False)
        self.assertEqual(service._snapshot(), (generation + 1, False))
        service.stop()
        service._emit("transcript", text="不应该发出")
        self.assertEqual(output, [])


if __name__ == "__main__":
    unittest.main()
