import unittest

from jarvis.voice import Segmenter


def frame(value=0):
    return value.to_bytes(2, "little", signed=True) * 480


class SegmenterTests(unittest.TestCase):
    def test_preview_snapshot_keeps_the_final_sentence_and_ignores_short_noise(self):
        segmenter = Segmenter(pre_roll_ms=0)
        for _ in range(9):
            segmenter.feed(frame(7), True)
        self.assertTrue(segmenter.active)
        self.assertIsNone(segmenter.snapshot())
        segmenter.feed(frame(7), True)
        self.assertEqual(segmenter.snapshot(), frame(7) * 10)
        for _ in range(10):
            segmenter.feed(frame(8), True)
        self.assertEqual(segmenter.snapshot(), frame(7) * 10 + frame(8) * 10)
        result = None
        for _ in range(30):
            result = segmenter.feed(frame(), False)
        self.assertEqual(result, frame(7) * 10 + frame(8) * 10 + frame() * 30)
        self.assertFalse(segmenter.active)
        self.assertIsNone(segmenter.snapshot())

    def test_waits_for_full_ending_silence_and_preserves_audio(self):
        segmenter = Segmenter()
        speech = [frame(value) for value in range(1, 11)]
        for pcm in speech:
            self.assertIsNone(segmenter.feed(pcm, True))
        for _ in range(29):
            self.assertIsNone(segmenter.feed(frame(), False))
        self.assertEqual(
            segmenter.feed(frame(), False), b"".join(speech + [frame()] * 30)
        )

    def test_pre_roll_contains_only_last_300_ms_before_speech(self):
        segmenter = Segmenter()
        silence = [frame(value) for value in range(20)]
        for pcm in silence:
            self.assertIsNone(segmenter.feed(pcm, False))
        for _ in range(10):
            segmenter.feed(frame(100), True)
        output = None
        for _ in range(30):
            output = segmenter.feed(frame(), False)
        self.assertEqual(
            output, b"".join(silence[-10:] + [frame(100)] * 10 + [frame()] * 30)
        )

    def test_short_noise_and_uninterrupted_silence_do_not_emit(self):
        segmenter = Segmenter()
        for _ in range(100):
            self.assertIsNone(segmenter.feed(frame(), False))
        for _ in range(9):
            self.assertIsNone(segmenter.feed(frame(10), True))
        for _ in range(60):
            self.assertIsNone(segmenter.feed(frame(), False))

    def test_brief_pause_inside_sentence_does_not_end_utterance(self):
        segmenter = Segmenter(pre_roll_ms=0)
        for speech, count in [(True, 5), (False, 20), (True, 5), (False, 29)]:
            for _ in range(count):
                self.assertIsNone(segmenter.feed(frame(1 if speech else 0), speech))
        result = segmenter.feed(frame(), False)
        self.assertEqual(len(result), 60 * 960)

    def test_continuous_speech_is_split_without_duplication_at_max_duration(self):
        segmenter = Segmenter(pre_roll_ms=0, max_segment_ms=600)
        outputs = []
        source = [frame(i) for i in range(40)]
        for pcm in source:
            result = segmenter.feed(pcm, True)
            if result is not None:
                outputs.append(result)
        self.assertEqual(len(outputs), 2)
        self.assertEqual([len(part) for part in outputs], [20 * 960, 20 * 960])
        self.assertEqual(b"".join(outputs), b"".join(source))

    def test_default_max_duration_never_exceeds_twenty_seconds(self):
        segmenter = Segmenter()
        for _ in range(665):
            self.assertIsNone(segmenter.feed(frame(1), True))
        result = segmenter.feed(frame(1), True)
        self.assertIsNotNone(result)
        self.assertLessEqual(len(result), 16000 * 2 * 20)

    def test_reset_drops_prior_speech_and_pre_roll(self):
        segmenter = Segmenter()
        for _ in range(10):
            segmenter.feed(frame(7), False)
        for _ in range(10):
            segmenter.feed(frame(8), True)
        segmenter.reset()
        for _ in range(10):
            segmenter.feed(frame(9), True)
        result = None
        for _ in range(30):
            result = segmenter.feed(frame(), False)
        self.assertEqual(result, frame(9) * 10 + frame() * 30)

    def test_rejects_wrong_frame_size_format_and_invalid_configuration(self):
        segmenter = Segmenter()
        for pcm, speech in [
            (b"", True),
            (b"\0" * 480, True),
            (bytearray(960), True),
            (frame(), 1),
        ]:
            with self.subTest(length=len(pcm), speech=speech):
                with self.assertRaises(ValueError):
                    segmenter.feed(pcm, speech)
        for kwargs in (
            {"sample_rate": 44100},
            {"frame_ms": 25},
            {"pre_roll_ms": -1},
            {"silence_ms": 0},
            {"min_speech_ms": 0},
            {"max_segment_ms": 300},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    Segmenter(**kwargs)


if __name__ == "__main__":
    unittest.main()
