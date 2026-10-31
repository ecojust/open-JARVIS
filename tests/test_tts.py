import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.tts_runtime import TTSService, prepare_text, speech_chunks, synthesize


class TextTests(unittest.TestCase):
    def test_summary_removes_code_links_and_markup_and_limits_length(self):
        text = (
            "# 结果\n**完成** [报告](https://example.com)\n```json\n{secret:1}\n```\n"
        )
        self.assertEqual(prepare_text(text), "结果 完成 报告")
        limited = prepare_text("羊" * 700)
        self.assertEqual(limited.count("羊"), 600)
        self.assertTrue(limited.endswith("完整内容请查看结果窗口。"))

    def test_chunks_preserve_text_and_keep_short_sentences_first(self):
        text = "任务已完成。" + "这是一段很长的任务说明，" * 20
        chunks = speech_chunks(text)
        self.assertEqual(chunks[0], "任务已完成。")
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= 80 for chunk in chunks))

    def test_long_first_sentence_starts_with_natural_clause_without_losing_text(self):
        text = "销售报告已经整理完成，其中包含本季度各地区的收入和销量变化，详细数据和来源已经放在结果窗口中。"
        chunks = speech_chunks(text)
        self.assertEqual(chunks[0], "销售报告已经整理完成，")
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= 80 for chunk in chunks))

    def test_first_clause_optimization_does_not_split_short_phrase_or_numeric_pause(
        self,
    ):
        text = "你好，我是贾维斯。"
        self.assertEqual(speech_chunks(text), [text])
        text = "会议时间已经改为12:30请在开始前准备好材料以及需要展示的项目报告。"
        self.assertEqual(speech_chunks(text), [text])

    def test_invalid_speaker_speed_and_empty_audio_are_rejected(self):
        engine = SimpleNamespace(
            num_speakers=103,
            generate=lambda *a, **k: SimpleNamespace(samples=[], sample_rate=24000),
        )
        for args in [("任务", 103, 1), ("任务", 3, 0), ("", 3, 1)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                synthesize(engine, *args)
        with self.assertRaises(ValueError):
            synthesize(engine, "任务")


class TTSTests(unittest.TestCase):
    def test_idle_retention_reads_environment_and_safely_bounds_invalid_values(self):
        for value, expected in [
            ("45.5", 45.5),
            ("bad", 120),
            ("nan", 120),
            ("inf", 120),
            ("-5", 0),
            ("99999", 3600),
        ]:
            with (
                self.subTest(value=value),
                patch.dict("os.environ", {"JARVIS_TTS_IDLE_SECONDS": value}),
            ):
                service = TTSService(lambda *args: None)
                self.assertEqual(service.cache_seconds, expected)
                service.shutdown()

    def test_configured_idle_timeout_releases_and_reloads_model_instance(self):
        loaded, released, done = [], threading.Event(), threading.Event()

        class Engine:
            num_speakers = 103

            def __init__(self):
                loaded.append(1)

            def generate(self, *args, **kwargs):
                return SimpleNamespace(samples=[0.1], sample_rate=24000)

            def __del__(self):
                released.set()

        def event(name, data):
            if name == "finished":
                done.set()

        with patch.dict("os.environ", {"JARVIS_TTS_IDLE_SECONDS": "0.02"}):
            service = TTSService(
                event, engine_factory=Engine, player=lambda *args: True
            )
        try:
            service.speak("第一次")
            self.assertTrue(done.wait(1))
            self.assertTrue(
                released.wait(1), "configured idle timeout did not release the engine"
            )
            done.clear()
            service.speak("第二次")
            self.assertTrue(done.wait(1))
            self.assertEqual(len(loaded), 2)
        finally:
            service.shutdown()

    def test_cancel_during_native_generation_discards_audio_and_queued_jobs(self):
        began, release, done = threading.Event(), threading.Event(), threading.Event()
        events, played = [], []

        class Engine:
            num_speakers = 103

            def generate(self, text, **kwargs):
                began.set()
                release.wait(2)
                return SimpleNamespace(samples=[0.1], sample_rate=24000)

        def event(name, data):
            events.append((name, data))
            if name == "finished" and data["playback_id"] == "active":
                done.set()

        service = TTSService(
            event,
            engine_factory=Engine,
            player=lambda audio, cancelled, first: played.append(audio) or True,
        )
        try:
            service.speak("第一条", "task1", "active")
            self.assertTrue(began.wait(2))
            service.speak("第二条", "task2", "pending")
            service.stop()
            release.set()
            self.assertTrue(done.wait(2))
            self.assertEqual(played, [])
            finished = {
                data["playback_id"]: data for name, data in events if name == "finished"
            }
            self.assertTrue(finished["active"]["cancelled"])
            self.assertTrue(finished["pending"]["queued"])
            self.assertNotIn(
                "pending",
                [data["playback_id"] for name, data in events if name == "started"],
            )
        finally:
            release.set()
            service.shutdown()

    def test_model_is_lazy_reused_and_first_audio_event_follows_real_player(self):
        events, loads, calls = [], [], []
        done = threading.Event()

        class Engine:
            num_speakers = 103

            def __init__(self):
                loads.append(1)

            def generate(self, text, **kwargs):
                calls.append(text)
                return SimpleNamespace(samples=[0.1], sample_rate=24000)

        def event(name, data):
            events.append((name, data))
            if name == "finished" and data["playback_id"] == "two":
                done.set()

        def player(audio, cancelled, first):
            first()
            return True

        service = TTSService(event, engine_factory=Engine, player=player)
        self.assertEqual(loads, [])
        try:
            service.speak("完成。请查看报告。", "task", "one")
            service.speak("第二个任务完成。", "task2", "two")
            self.assertTrue(done.wait(2))
            self.assertEqual(loads, [1])
            self.assertEqual(calls, ["完成。", "请查看报告。", "第二个任务完成。"])
            first_events = [
                name for name, data in events if data["playback_id"] == "one"
            ]
            self.assertEqual(first_events, ["started", "playing", "finished"])
        finally:
            service.shutdown()

    def test_synthesis_failure_finishes_and_worker_accepts_next_job(self):
        events = []
        done = threading.Event()

        class Engine:
            num_speakers = 103

            def generate(self, text, **kwargs):
                if text == "失败":
                    raise ValueError("mock synthesis failed")
                return SimpleNamespace(samples=[0.1], sample_rate=24000)

        def event(name, data):
            events.append((name, data))
            if name == "finished" and data["playback_id"] == "good":
                done.set()

        service = TTSService(event, engine_factory=Engine, player=lambda *args: True)
        try:
            service.speak("失败", playback_id="bad")
            service.speak("成功", playback_id="good")
            self.assertTrue(done.wait(2))
            bad = [
                (name, data) for name, data in events if data["playback_id"] == "bad"
            ]
            self.assertEqual(
                [name for name, _ in bad], ["started", "error", "finished"]
            )
            self.assertTrue(bad[-1][1]["failed"])
        finally:
            service.shutdown()

    def test_queue_is_bounded_and_closed_service_rejects_work(self):
        began, release = threading.Event(), threading.Event()

        class Engine:
            num_speakers = 103

            def generate(self, *args, **kwargs):
                began.set()
                release.wait(2)
                return SimpleNamespace(samples=[0.1], sample_rate=24000)

        service = TTSService(
            lambda *args: None,
            engine_factory=Engine,
            player=lambda *args: True,
            max_pending=1,
        )
        try:
            service.speak("第一条", playback_id="one")
            self.assertTrue(began.wait(2))
            service.speak("第二条", playback_id="two")
            with self.assertRaises(ValueError):
                service.speak("第三条")
        finally:
            release.set()
            service.shutdown()
        with self.assertRaises(RuntimeError):
            service.speak("已经关闭")

    def test_prewarm_loads_without_playback_or_speaking_state(self):
        ready = threading.Event()
        finished = threading.Event()
        events, loads = [], []

        class Engine:
            num_speakers = 103

            def __init__(self):
                loads.append(1)

            def generate(self, *args, **kwargs):
                return SimpleNamespace(samples=[0.1], sample_rate=24000)

        def event(name, data):
            events.append(name)
            if name == "ready":
                ready.set()
            if name == "finished":
                finished.set()

        service = TTSService(event, engine_factory=Engine, player=lambda *args: True)
        try:
            service.prewarm()
            self.assertTrue(ready.wait(1))
            self.assertEqual(events, ["ready"])
            service.speak("使用已加载模型")
            self.assertTrue(finished.wait(1))
            self.assertEqual(loads, [1])
        finally:
            service.shutdown()


if __name__ == "__main__":
    unittest.main()
