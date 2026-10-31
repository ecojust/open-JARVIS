"""Voice gating remains enabled by default and publishes its actual state."""

import os
import unittest
from unittest.mock import patch

from jarvis.voice_runtime import VoiceService, WakeWordGate


class WakeFeedbackTests(unittest.TestCase):
    def test_old_final_cannot_reengage_after_reset_or_pause(self):
        with (
            patch.dict(os.environ, {"JARVIS_WAKE_WORD": "贾维斯"}),
            patch("jarvis.paths.load_environment"),
        ):
            service = VoiceService(lambda *_: None)
        gate = WakeWordGate(service.wake_word)
        generation, epoch = service.capture_generation, service._wake_epoch
        service.reset_wake()
        self.assertEqual(
            service._accept_transcript(
                gate, "贾维斯，旧任务", generation=generation, wake_epoch=epoch
            ),
            (None, False),
        )
        self.assertFalse(gate.engaged)
        self.assertTrue(service.wake_required)
        epoch = service._wake_epoch
        service.set_paused(True)
        service.set_paused(False)
        self.assertEqual(
            service._accept_transcript(
                gate, "贾维斯，旧录音", generation=generation, wake_epoch=epoch
            ),
            (None, False),
        )
        self.assertFalse(gate.engaged)
        self.assertTrue(service.wake_required)

    def test_followup_expiry_notifies_once_without_new_timer(self):
        events = []
        with (
            patch.dict(os.environ, {"JARVIS_WAKE_WORD": "贾维斯"}),
            patch("jarvis.paths.load_environment"),
        ):
            service = VoiceService(lambda name, data: events.append((name, data)))
        with patch("jarvis.voice_runtime.time.monotonic", return_value=100):
            service.arm_followup(30)
            self.assertFalse(service.wake_required)
            service._expire_followup()
            self.assertFalse(events)
        with patch("jarvis.voice_runtime.time.monotonic", return_value=131):
            service._expire_followup()
            service._expire_followup()
            self.assertTrue(service.wake_required)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "status")

    def test_default_gate_reports_waiting_and_manual_reset(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("jarvis.paths.load_environment"),
        ):
            service = VoiceService(lambda *_: None)
        self.assertEqual(service.wake_word, "贾维斯")
        gate = WakeWordGate(service.wake_word)
        self.assertTrue(service.wake_required)
        self.assertEqual(service._accept_transcript(gate, "普通任务"), (None, False))
        self.assertTrue(service.wake_required)
        service._accept_transcript(gate, "贾维斯，写报告")
        self.assertFalse(service.wake_required)
        service.set_paused(True)
        service.set_paused(False)
        self.assertFalse(service.wake_required)
        service.reset_wake()
        self.assertTrue(service.wake_required)
        service.arm_followup()
        self.assertFalse(service.wake_required)
        service.disarm_followup()
        self.assertTrue(service.wake_required)

    def test_explicit_empty_word_still_disables_gate_without_new_settings(self):
        with (
            patch.dict(os.environ, {"JARVIS_WAKE_WORD": ""}),
            patch("jarvis.paths.load_environment"),
        ):
            service = VoiceService(lambda *_: None)
        self.assertEqual(service.wake_word, "")
        self.assertFalse(service.wake_required)
        self.assertEqual(
            service._accept_transcript(WakeWordGate(service.wake_word), "直接说任务"),
            ("直接说任务", False),
        )
