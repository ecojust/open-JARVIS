"""Native bar, actual PCM waveform, model buttons and maximized result check.

Developer validation only: reads the committed sample video's real narration.
--verify-installed-models clicks the GUI verification buttons ONLY when both
models are already present, so this check never initiates a large download.
Microphone capture, loudspeaker playback and macOS Spaces are not simulated.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "artifacts/voice-bar-check"
    )
    parser.add_argument("--verify-installed-models", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import webrtcvad
    from jarvis.audio_meter import SpeechMeter
    from jarvis.desktop_client import DesktopController
    from jarvis.model_catalog import model_status
    from jarvis.video_tools import _ffmpeg_path

    report = {
        "microphone_tested": False,
        "speaker_tested": False,
        "macos_spaces_tested": False,
        "waveform_source": "actual PCM decoded from committed Kokoro narration",
        "model_download_test": "not requested",
        "checks": {},
    }
    if args.verify_installed_models:
        assert all(model_status(k)["state"] == "ready" for k in ("stt", "tts")), (
            "Install the models with the client's buttons first; this check does not download missing models."
        )
    sample = ROOT / "docs/acceptance/2026-10-31/files/金融趋势讲解.mp4"
    pcm = subprocess.run(
        [
            _ffmpeg_path(),
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(sample),
            "-t",
            "4",
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            "16000",
            "pipe:1",
        ],
        capture_output=True,
        timeout=15,
        check=True,
    ).stdout
    assert 960 <= len(pcm) <= 16000 * 2 * 5
    with tempfile.TemporaryDirectory(prefix="jarvis-bar-check-") as temporary:
        os.environ["JARVIS_DATA_DIR"] = temporary
        os.environ["JARVIS_CONFIG_FILE"] = str(Path(temporary) / "settings.json")
        os.environ["UI_SPEECH_ENABLED"] = "false"
        os.environ["UI_AUTO_FULLSCREEN"] = "true"
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            os.environ["JARVIS_DESKTOP_PORT"] = str(server.getsockname()[1])
        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        controller = DesktopController(app, autostart=False)

        def until(predicate, timeout=30):
            deadline = time.monotonic() + timeout
            while not predicate():
                app.processEvents()
                if time.monotonic() > deadline:
                    raise TimeoutError("Desktop check did not reach its expected state")
                time.sleep(0.01)
            app.processEvents()

        try:
            app.processEvents()
            controller.tile.grab().save(str(output / "01-bar-idle.png"))
            report["checks"]["bar_only_on_start"] = controller.results is None
            report["bar_size"] = [controller.tile.width(), controller.tile.height()]
            controller.tile.expand_button.click()
            app.processEvents()
            assert (
                controller.results.isMaximized()
                and not controller.results.isFullScreen()
            )
            controller.results.hide()
            controller.handle_audio_event("audio.ready", {"listening": True})
            controller.tile.set_state(
                "语音样例输入", "来自实际中文讲解文件，非麦克风录音"
            )
            stamp = [0.0]
            meter = SpeechMeter(clock=lambda: stamp[0])
            vad = webrtcvad.Vad(2)
            best, events = 0, 0
            for offset in range(0, len(pcm) - 959, 960):
                frame = pcm[offset : offset + 960]
                stamp[0] = offset / 32000
                level = meter.feed(frame, vad.is_speech(frame, 16000))
                if level is not None:
                    controller.handle_audio_event("audio.level", level)
                    events += 1
                    app.processEvents()
                    visible = sum(controller.tile._levels)
                    if visible > best:
                        best = visible
                        controller.tile.grab().save(
                            str(output / "02-real-pcm-wave.png")
                        )
            assert best > 0
            controller.handle_audio_event(
                "audio.level",
                {
                    "level": 0,
                    "speech": False,
                    "listening": False,
                },
            )
            assert controller.tile.audio_level == 0
            report["checks"]["real_pcm_vad_wave_and_reset"] = True
            report["meter_event_count"] = events
            # Keep current work intact while verification processes run.
            controller.tasks["acceptance-hold"] = {"status": "running"}
            controller.show_settings()
            controller.settings_dialog.models_button.click()
            report["checks"]["settings_model_button"] = (
                controller.model_dialog.isVisible()
            )
            if args.verify_installed_models:
                workers = []
                for kind in ("stt", "tts"):
                    controller.model_dialog.cards[kind].download_button.click()
                    workers.append(controller._downloads[kind])
                until(lambda: not controller._downloads)
                assert all(
                    controller._model_states[k]["phase"] == "ready"
                    for k in ("stt", "tts")
                )
                assert controller.pending_settings
                report["model_download_test"] = (
                    "real GUI buttons / real worker processes / existing weight hashes; no new network download"
                )
                report["checks"]["verified_models_defer_reload_until_idle"] = True
            controller.model_dialog.grab().save(str(output / "03-model-manager.png"))
            controller.model_dialog.close()
            controller.settings_dialog.reject()
            controller.tasks.clear()
            if args.verify_installed_models:
                controller._maybe_apply_settings()
            else:
                controller.start_backend()
            until(lambda: controller.ready)
            assert controller.submit(
                "/write client-check.md | 语音条与最大化窗口验收。"
            )
            until(
                lambda: any(
                    t.get("status") == "succeeded" for t in controller.tasks.values()
                )
            )
            until(lambda: controller.results.isVisible())
            until(
                lambda: (
                    controller.results.preview.payload.get("source") == "actual_file"
                )
            )
            assert (
                controller.results.isMaximized()
                and not controller.results.isFullScreen()
            )
            assert (Path(temporary) / "workspace/client-check.md").is_file()
            controller.results.grab().save(str(output / "04-maximized-result.png"))
            report["checks"]["real_backend_result_maximized_not_fullscreen"] = True
            report["checks"][
                "model_reload_requested_after_idle"
            ] = not controller.pending_settings
        finally:
            controller.cleanup()
            app.processEvents()
        report["passed"] = all(report["checks"].values())
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=True))
        return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
