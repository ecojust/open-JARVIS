import threading
import unittest

from jarvis.audio_process import AudioController


class FakeVoice:
    def __init__(self, event, **kwargs):
        self.event = event
        self.paused = False
        self.stopped = False
        self.followup = False

    def start(self):
        self.event("status", {"message": "ready", "listening": True})

    def set_paused(self, value):
        self.paused = value

    def stop(self):
        self.stopped = True

    def arm_followup(self, seconds):
        self.followup = True

    def disarm_followup(self):
        self.followup = False


class FakeTTS:
    def __init__(self, event, **kwargs):
        self.event = event
        self.stopped = False
        self.closed = False

    def speak(self, text, task_id="", playback_id=None):
        self.event("started", {"task_id": task_id, "playback_id": playback_id})
        return playback_id

    def stop(self):
        self.stopped = True

    def shutdown(self):
        self.closed = True


class AudioControllerTests(unittest.TestCase):
    def make(self):
        events = []
        changed = threading.Event()

        def event(name, data):
            events.append((name, data))
            if name == "audio.status" and data.get("listening"):
                changed.set()

        controller = AudioController(
            event, voice_factory=FakeVoice, tts_factory=FakeTTS, echo_seconds=0.01
        )
        self.addCleanup(controller.close)
        return controller, events, changed

    def test_user_pause_survives_tts_completion_and_stale_finish_cannot_resume(self):
        controller, events, resumed = self.make()
        controller.start()
        controller.handle(
            {
                "command": "speak",
                "text": "任务完成",
                "task_id": "task",
                "playback_id": "old",
            }
        )
        self.assertTrue(controller.voice.paused)
        controller.handle({"command": "pause"})
        controller.tts.event("finished", {"playback_id": "old", "task_id": "task"})
        resumed.clear()
        self.assertFalse(resumed.wait(0.03))
        self.assertTrue(controller.voice.paused)
        controller.handle({"command": "resume"})
        self.assertFalse(controller.voice.paused)
        controller.handle({"command": "speak", "text": "新回复", "playback_id": "new"})
        controller.tts.event("finished", {"playback_id": "old", "task_id": "task"})
        self.assertTrue(controller.voice.paused)

    def test_tts_failure_releases_temporary_pause_after_tail_guard(self):
        controller, events, resumed = self.make()
        controller.start()
        controller.handle({"command": "speak", "text": "回复", "playback_id": "p1"})
        resumed.clear()
        controller.tts.event(
            "error",
            {"playback_id": "p1", "task_id": "task", "message": "speaker unavailable"},
        )
        controller.tts.event(
            "finished", {"playback_id": "p1", "task_id": "task", "failed": True}
        )
        self.assertTrue(resumed.wait(1))
        self.assertFalse(controller.voice.paused)
        error = next(data for name, data in events if name == "audio.error")
        self.assertEqual(error["component"], "tts")
        self.assertEqual(error["playback_id"], "p1")

    def test_audio_transcript_suppressed_while_paused_and_shutdown_stops_both(self):
        controller, events, resumed = self.make()
        controller.start()
        controller.voice.event("transcript", {"text": "新任务"})
        self.assertEqual(
            [data["text"] for name, data in events if name == "stt.final"], ["新任务"]
        )
        controller.handle({"command": "pause"})
        controller.voice.event("transcript", {"text": "过期结果"})
        self.assertEqual(len([name for name, _ in events if name == "stt.final"]), 1)
        self.assertFalse(controller.handle({"command": "shutdown"}))
        controller.close()
        self.assertTrue(controller.voice.stopped)
        self.assertTrue(controller.tts.closed)
        before = len(events)
        controller.voice.event("transcript", {"text": "退出后"})
        self.assertEqual(len(events), before)

    def test_malformed_commands_report_errors_without_terminating(self):
        controller, events, _ = self.make()
        for command in [
            [],
            {"command": "invalid"},
            {"command": "speak", "text": None},
            {"command": []},
        ]:
            self.assertTrue(controller.handle(command))
        self.assertEqual(len([name for name, _ in events if name == "audio.error"]), 4)

    def test_stop_speaking_resumes_without_waiting_for_stale_native_generation(self):
        controller, events, resumed = self.make()
        controller.start()
        controller.handle({"command": "speak", "text": "旧回复", "playback_id": "old"})
        resumed.clear()
        controller.handle({"command": "stop_speaking"})
        self.assertTrue(resumed.wait(1))
        self.assertFalse(controller.voice.paused)
        controller.handle({"command": "speak", "text": "新回复", "playback_id": "new"})
        controller.tts.event("finished", {"playback_id": "old"})
        self.assertTrue(controller.voice.paused)

    def test_completed_question_arms_one_followup_after_echo_guard(self):
        controller, events, resumed = self.make()
        controller.start()
        controller.handle(
            {
                "command": "speak",
                "text": "是哪一天？",
                "playback_id": "question",
                "awaiting_input": True,
            }
        )
        resumed.clear()
        controller.tts.event("finished", {"playback_id": "question"})
        self.assertTrue(resumed.wait(1))
        self.assertTrue(controller.voice.followup)

    def test_clearing_question_context_disarms_pending_and_active_followup(self):
        controller, events, resumed = self.make()
        controller.start()
        controller.handle({"command": "expect_input", "enabled": True})
        self.assertTrue(controller.voice.followup)
        controller.handle(
            {
                "command": "speak",
                "text": "是哪一天？",
                "playback_id": "q",
                "awaiting_input": True,
            }
        )
        controller.handle({"command": "expect_input", "enabled": False})
        resumed.clear()
        controller.tts.event("finished", {"playback_id": "q"})
        self.assertTrue(resumed.wait(1))
        self.assertFalse(controller.voice.followup)

    def test_disarm_during_echo_tail_invalidates_followup_but_resumes_microphone(self):
        controller, events, resumed = self.make()
        controller.echo_seconds = 0.05
        controller.start()
        controller.handle(
            {
                "command": "speak",
                "text": "是哪一天？",
                "playback_id": "q",
                "awaiting_input": True,
            }
        )
        resumed.clear()
        controller.tts.event("finished", {"playback_id": "q"})
        controller.handle({"command": "expect_input", "enabled": False})
        self.assertTrue(resumed.wait(1))
        self.assertFalse(controller.voice.paused)
        self.assertFalse(controller.voice.followup)

    def test_stop_during_echo_tail_does_not_rearm_finished_question(self):
        controller, events, resumed = self.make()
        controller.echo_seconds = 0.05
        controller.start()
        controller.handle(
            {
                "command": "speak",
                "text": "是哪一天？",
                "playback_id": "q",
                "awaiting_input": True,
            }
        )
        resumed.clear()
        controller.tts.event("finished", {"playback_id": "q"})
        controller.handle({"command": "stop_speaking"})
        self.assertTrue(resumed.wait(1))
        self.assertFalse(controller.voice.paused)
        self.assertFalse(controller.voice.followup)


if __name__ == "__main__":
    unittest.main()
