"""Actual PCM level semantics and rate limits; no microphone or model needed."""

import math
import struct
import unittest
from unittest.mock import patch

from jarvis.audio_meter import SpeechMeter


def pcm(amplitude):
    return struct.pack("<480h", *([amplitude] * 480))


class SpeechMeterTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.meter = SpeechMeter(clock=lambda: self.now)

    def test_actual_int16_amplitudes_map_to_finite_distinct_levels(self):
        levels = []
        for value in (64, 512, 4096, 16384, -32768):
            self.now += 0.1
            event = self.meter.feed(pcm(value), True)
            self.assertTrue(event["speech"])
            self.assertTrue(event["listening"])
            self.assertTrue(math.isfinite(event["level"]))
            self.assertGreater(event["level"], 0)
            self.assertLessEqual(event["level"], 1)
            levels.append(event["level"])
        self.assertEqual(levels, sorted(set(levels)))
        self.assertEqual(levels[-1], 1.0)

    def test_speech_events_are_bounded_at_twelve_point_five_hz(self):
        emitted = []
        for index in range(1000):
            self.now = 100.0 + index / 1000
            if self.meter.feed(pcm(4096), True) is not None:
                emitted.append(self.now)
        self.assertLessEqual(len(emitted), 13)
        for previous, current in zip(emitted, emitted[1:]):
            self.assertGreaterEqual(current - previous, 0.08)

    def test_vad_silence_and_zero_pcm_clear_immediately_without_repeated_events(self):
        self.assertTrue(self.meter.feed(pcm(4096), True)["speech"])
        self.now += 0.001
        expected = {"level": 0.0, "speech": False, "listening": True}
        self.assertEqual(self.meter.feed(pcm(4096), False), expected)
        for _ in range(100):
            self.assertIsNone(self.meter.feed(pcm(4096), False))
        self.now += 0.1
        self.assertTrue(self.meter.feed(pcm(4096), True)["speech"])
        self.now += 0.001
        self.assertEqual(self.meter.feed(pcm(0), True), expected)
        self.assertIsNone(self.meter.feed(pcm(0), True))

    def test_pause_and_reset_do_not_allow_stale_level_or_bypass_frequency_cap(self):
        self.assertTrue(self.meter.feed(pcm(4096), True)["speech"])
        self.now += 0.001
        self.assertEqual(
            self.meter.reset(),
            {"level": 0.0, "speech": False, "listening": False},
        )
        self.assertIsNone(self.meter.reset())
        self.assertIsNone(self.meter.feed(pcm(4096), True, listening=False))
        self.assertEqual(
            self.meter.reset(listening=True),
            {"level": 0.0, "speech": False, "listening": True},
        )
        self.assertIsNone(self.meter.feed(pcm(4096), True))
        self.now += 0.1
        self.assertTrue(self.meter.feed(pcm(4096), True)["speech"])

    def test_quiet_input_does_not_import_numpy_and_never_animates_noise(self):
        with patch.dict("sys.modules", {"numpy": None}):
            first = self.meter.feed(pcm(0), False)
            self.assertEqual(first["level"], 0)
            for _ in range(20):
                self.assertIsNone(self.meter.feed(pcm(4096), False))
                self.assertIsNone(self.meter.feed(pcm(0), True))
        # Even a positive VAD decision must not invent a level below -60 dBFS.
        self.assertIsNone(self.meter.feed(pcm(1), True))

    def test_rejects_invalid_or_unbounded_inputs(self):
        for value in (b"", b"\x00", b"\x00" * 1922, bytearray(960), None):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError):
                    self.meter.feed(value, True)
        for speech, listening in ((1, True), (True, 1), (None, True)):
            with self.assertRaises(ValueError):
                self.meter.feed(pcm(1000), speech, listening)
        with self.assertRaises(ValueError):
            self.meter.reset(listening=1)
        for frequency in (0, -1, 16, True, math.nan, math.inf):
            with self.assertRaises(ValueError):
                SpeechMeter(max_hz=frequency)


if __name__ == "__main__":
    unittest.main()
