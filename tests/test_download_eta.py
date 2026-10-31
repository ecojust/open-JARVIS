"""Download estimates use observed transfer bytes and a controlled clock."""

import unittest

from jarvis.model_download import TransferProgress


class DownloadEtaTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.events = []
        self.progress = TransferProgress(
            lambda phase, **data: self.events.append({"phase": phase, **data}),
            clock=lambda: self.now,
        )

    def report(self, when, completed, *, phase="downloading", total=1000, **data):
        self.now = when
        self.progress(
            phase,
            completed_bytes=completed,
            total_bytes=total,
            **{"source": "Primary", "source_index": 1, **data},
        )
        return self.events[-1]

    def assert_unknown(self, event):
        self.assertIsNone(event["eta_seconds"])
        self.assertIsNone(event["bytes_per_second"])

    def test_initial_warmup_then_estimate_uses_actual_bytes_and_time(self):
        self.assert_unknown(self.report(0, 0, message="starting", force=True))
        self.assertEqual(self.events[-1]["message"], "starting")
        self.assertTrue(self.events[-1]["force"])
        self.assert_unknown(self.report(0.5, 50))
        first = self.report(1, 100)
        self.assertAlmostEqual(first["bytes_per_second"], 100)
        self.assertEqual(first["eta_seconds"], 9)
        next_event = self.report(2, 200)
        self.assertAlmostEqual(next_event["bytes_per_second"], 100)
        self.assertEqual(next_event["eta_seconds"], 8)

    def test_cached_baseline_does_not_count_as_transferred_bytes(self):
        self.assert_unknown(self.report(0, 400))
        event = self.report(1, 500)
        self.assertAlmostEqual(event["bytes_per_second"], 100)
        self.assertEqual(event["eta_seconds"], 5)

    def test_file_verification_preserves_rate_and_connection_gap_slows_estimate(self):
        self.report(0, 0, total=10_000, filename="first")
        self.report(1, 100, total=10_000, filename="first")
        self.assert_unknown(
            self.report(1, 100, phase="verifying", total=10_000, filename="first")
        )
        between_files = self.report(1.2, 100, total=10_000, filename="second")
        self.assertIsNotNone(between_files["eta_seconds"])
        resumed = self.report(2, 200, total=10_000, filename="second")
        self.assertAlmostEqual(resumed["bytes_per_second"], 100)
        self.assertEqual(resumed["eta_seconds"], 98)
        idle = self.report(3, 200, total=10_000, filename="third")
        self.assertLess(idle["bytes_per_second"], resumed["bytes_per_second"])
        self.assertGreater(idle["eta_seconds"], resumed["eta_seconds"])

    def test_source_switch_resets_estimate_and_new_total_has_new_baseline(self):
        self.report(0, 0, total=10_000)
        self.assertEqual(self.report(1, 1000, total=10_000)["eta_seconds"], 9)
        switched = self.report(
            2, 100, phase="switching", total=10_000, source="Mirror", source_index=2
        )
        self.assert_unknown(switched)
        self.assert_unknown(
            self.report(2, 100, total=10_000, source="Mirror", source_index=2)
        )
        self.assert_unknown(
            self.report(2.5, 200, total=10_000, source="Mirror", source_index=2)
        )
        measured = self.report(3, 300, total=10_000, source="Mirror", source_index=2)
        self.assertAlmostEqual(measured["bytes_per_second"], 200)
        self.assertEqual(measured["eta_seconds"], 49)
        self.assert_unknown(
            self.report(3, 0, total=20_000, source="Mirror", source_index=2)
        )
        replacement = self.report(4, 200, total=20_000, source="Mirror", source_index=2)
        self.assertAlmostEqual(replacement["bytes_per_second"], 200)
        self.assertEqual(replacement["eta_seconds"], 99)

    def test_implicit_source_change_or_progress_rollback_starts_new_warmup(self):
        self.report(0, 0)
        self.report(1, 100)
        self.assert_unknown(self.report(2, 150, source="Mirror", source_index=2))
        measured = self.report(3, 250, source="Mirror", source_index=2)
        self.assertAlmostEqual(measured["bytes_per_second"], 100)
        self.assert_unknown(self.report(4, 200, source="Mirror", source_index=2))
        recovered = self.report(5, 300, source="Mirror", source_index=2)
        self.assertAlmostEqual(recovered["bytes_per_second"], 100)
        self.assertEqual(recovered["eta_seconds"], 7)

    def test_eof_and_nontransfer_phases_never_claim_download_time_remaining(self):
        for phase in (
            "checking",
            "installing",
            "ready",
            "failed",
            "cancelled",
            "cancelling",
        ):
            with self.subTest(phase=phase):
                self.report(0, 0, phase="checking")
                self.report(0, 0)
                self.report(1, 100)
                self.assert_unknown(self.report(2, 100, phase=phase))
                self.assert_unknown(self.report(3, 100))
        self.assert_unknown(self.report(4, 1000))
        self.assert_unknown(self.report(4, 1000, phase="verifying"))
        self.assert_unknown(self.report(4, 1000, phase="ready"))

    def test_recent_window_adapts_to_sustained_new_speed_and_memory_is_bounded(self):
        self.report(0, 0, total=100_000)
        for when in range(1, 13):
            self.report(when, when * 100, total=100_000)
        self.assertAlmostEqual(self.events[-1]["bytes_per_second"], 100)
        for when in range(13, 29):
            self.report(when, 1200 + (when - 12) * 1000, total=100_000)
        event = self.events[-1]
        self.assertAlmostEqual(event["bytes_per_second"], 1000)
        self.assertEqual(event["eta_seconds"], 83)
        for tick in range(1, 3001):
            self.report(28 + tick / 100, 17_200 + tick * 10, total=100_000)
        self.assertLessEqual(len(self.progress.samples), 64)
        self.assertAlmostEqual(self.events[-1]["bytes_per_second"], 1000)


if __name__ == "__main__":
    unittest.main()
