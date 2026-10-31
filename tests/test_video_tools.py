import importlib.util
import json
import math
import multiprocessing
from pathlib import Path
import shutil
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

from jarvis.office_tools import create_presentation
from jarvis.video_tools import (
    _check_alive,
    _narrations,
    _presentation,
    _synthesize_narration,
    create_explainer_video,
    probe_video,
    video_cover_png,
)


def blocked_speech_worker(*args):
    """A deliberately blocked process exercises the same termination boundary."""
    time.sleep(10)


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.slides = [
            {
                "title": "资料来源",
                "bullets": ["依据官方数据编写。"],
                "notes": "这是一段中文讲解稿。",
            },
            {
                "title": "结论与边界",
                "bullets": ["预测不等于事实。"],
                "notes": "请区分预测和已经发生的事件。",
            },
        ]
        create_presentation(
            self.root, "讲解.pptx", title="真实PPT读取", slides=self.slides
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_reads_actual_ppt_notes_and_rejects_mismatched_manifest(self):
        deck = _presentation(self.root / "讲解.pptx")
        self.assertEqual(
            [slide["notes"] for slide in deck["slides"]],
            [slide["notes"] for slide in self.slides],
        )
        self.assertEqual(deck["slides"][0]["bullets"], self.slides[0]["bullets"])
        self.assertEqual(deck["title"], "真实PPT读取")
        bad = self.root / "wrong.json"
        bad.write_text(
            json.dumps({"slides": [{"title": "不相关PPT"}]}), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "不一致"):
            create_explainer_video(
                self.root,
                "out.mp4",
                presentation_path="讲解.pptx",
                manifest_path="wrong.json",
            )
        self.assertFalse((self.root / "out.mp4").exists())

    def test_long_script_is_rejected_without_silently_truncating(self):
        deck = _presentation(self.root / "讲解.pptx")
        deck["slides"][0]["notes"] = "长" * 201
        with self.assertRaisesRegex(ValueError, "超过200字"):
            _narrations(deck)

    def test_missing_model_cannot_report_a_completed_video(self):
        with (
            patch("jarvis.video_tools._ffmpeg_path", return_value="ffmpeg"),
            patch(
                "jarvis.video_tools._synthesize_narration",
                side_effect=ValueError("缺少Kokoro本地模型"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "Kokoro"):
                create_explainer_video(
                    self.root, "out.mp4", presentation_path="讲解.pptx"
                )
        self.assertFalse((self.root / "out.mp4").exists())
        self.assertFalse(list(self.root.glob(".jarvis-video-*")))

    def test_cancel_and_timeout_checkpoints_are_real_failures(self):
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaisesRegex(ValueError, "取消"):
            _check_alive(time.monotonic() + 10, cancelled)
        with self.assertRaisesRegex(ValueError, "时间预算"):
            _check_alive(time.monotonic() - 1)

    def test_cancelling_stops_a_real_speech_worker_process(self):
        before = {process.pid for process in multiprocessing.active_children()}
        cancelled = threading.Event()
        timer = threading.Timer(0.15, cancelled.set)
        timer.start()
        try:
            with (
                patch("jarvis.download_tts_model.model_ready", return_value=True),
                patch("jarvis.video_tools._speech_worker", blocked_speech_worker),
                self.assertRaisesRegex(ValueError, "取消"),
            ):
                _synthesize_narration(
                    ["不会真正合成"], self.root, 3, 1.0, time.monotonic() + 5, cancelled
                )
        finally:
            timer.cancel()
        self.assertEqual(
            {process.pid for process in multiprocessing.active_children()} - before,
            set(),
        )

    @staticmethod
    def fixture_speech(texts, directory, speaker, speed, deadline, cancel_event):
        # Explicit test-tone fixture verifies real muxing and timing only.
        # Actual Kokoro speech is exercised by a separate integration run.
        pages = []
        for index, text in enumerate(texts):
            count, rate = 5773, 24000
            filename = f"speech-{index:02d}.wav"
            samples = b"".join(
                struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / rate)))
                for i in range(count)
            )
            with wave.open(str(directory / filename), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(rate)
                output.writeframes(samples)
            pages.append(
                {
                    "file": filename,
                    "sample_rate": rate,
                    "samples": count,
                    "duration": count / rate,
                    "cues": [{"text": text, "start": 0, "end": count / rate}],
                }
            )
        return {
            "pages": pages,
            "duration": sum(page["duration"] for page in pages),
            "engine": "explicit-test-tone",
        }

    @unittest.skipUnless(
        shutil.which("ffmpeg") or importlib.util.find_spec("imageio_ffmpeg"),
        "FFmpeg unavailable",
    )
    def test_real_h264_aac_container_and_pcm_aligned_subtitles(self):
        with patch(
            "jarvis.video_tools._synthesize_narration", side_effect=self.fixture_speech
        ):
            result = create_explainer_video(
                self.root, "演示.mp4", presentation_path="讲解.pptx"
            )
        metadata = probe_video(self.root / "演示.mp4")
        self.assertEqual(
            (metadata["video_codec"], metadata["audio_codec"]), ("h264", "aac")
        )
        self.assertTrue(metadata["has_audio"])
        self.assertEqual((metadata["width"], metadata["height"]), (1280, 720))
        self.assertFalse(result["published"])
        subtitle = (self.root / "演示.mp4.srt").read_text(encoding="utf-8")
        self.assertIn("00:00:00,250 --> 00:00:00,491", subtitle)
        self.assertIn(self.slides[1]["notes"], subtitle)
        self.assertTrue((self.root / "演示.mp4.poster.png").is_file())
        self.assertEqual(len(result["artifacts"]), 5)
        from PIL import Image
        import io

        # A sidecar can be removed: the cover must come from actual MP4 frames.
        (self.root / "演示.mp4.poster.png").unlink()
        with Image.open(io.BytesIO(video_cover_png(self.root / "演示.mp4"))) as cover:
            self.assertEqual(cover.size, (960, 540))
        # Probe fallback reads actual headers, even without a separate ffprobe.
        original_which = shutil.which
        with patch(
            "jarvis.video_tools.shutil.which",
            side_effect=lambda name: (
                None if name == "ffprobe" else original_which(name)
            ),
        ):
            fallback = probe_video(self.root / "演示.mp4")
        self.assertEqual(fallback["video_codec"], "h264")
        self.assertTrue(fallback["has_audio"])
        with self.assertRaisesRegex(ValueError, "已存在"):
            create_explainer_video(self.root, "演示.mp4", presentation_path="讲解.pptx")


if __name__ == "__main__":
    unittest.main()
