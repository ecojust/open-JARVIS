"""Replay a local recording through the actual desktop/audio/backend processes.

The production microphone device is replaced only in a dedicated test worker;
STT, VAD, JSON-lines IPC, Qt widgets, HTTP/SSE and persistence remain real.
Requires an existing SenseVoice model and a mono 16 kHz int16 WAV recording.
No microphone, speaker or production model account is used.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def recording(path: Path):
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Use a recording no larger than 2 MiB")
    with wave.open(str(path), "rb") as source:
        if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (
            16000,
            1,
            2,
        ):
            raise ValueError("Expected mono 16 kHz int16 WAV")
        pcm = source.readframes(source.getnframes())
    if not pcm or len(pcm) > 60 * 32000:
        raise ValueError("Use a recording between 0 and 60 seconds")
    pcm += b"\0" * ((-len(pcm)) % 960)
    return pcm


def worker(path: Path):
    pcm = recording(path)

    class ReplayInput:
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]
            self.stop = threading.Event()

        def __enter__(self):
            def replay():
                # Allow the real stdin startup-discard handshake to complete.
                if self.stop.wait(2):
                    return
                started = time.monotonic()
                for offset in range(0, len(pcm), 960):
                    if self.stop.is_set():
                        return
                    self.callback(pcm[offset : offset + 960], 480, None, False)
                    deadline = started + (offset / 960 + 1) * 0.03
                    self.stop.wait(max(0, deadline - time.monotonic()))
                while not self.stop.wait(0.03):
                    self.callback(b"\0" * 960, 480, None, False)

            self.thread = threading.Thread(target=replay, daemon=True)
            self.thread.start()
            return self

        def __exit__(self, *_):
            self.stop.set()
            self.thread.join(timeout=2)

    with patch.dict(
        sys.modules, {"sounddevice": SimpleNamespace(RawInputStream=ReplayInput)}
    ):
        from jarvis.audio_process import main

        return main()


def run(args):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from jarvis.desktop_client import DesktopController
    from jarvis.paths import model_dir

    pcm = recording(args.audio_file)
    model = args.stt_model_dir or model_dir("sensevoice")
    if not (model / "model.int8.onnx").is_file():
        raise ValueError(
            "An existing SenseVoice model is required; no download is performed"
        )
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    started = time.monotonic()
    report = {
        "passed": False,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "scope": "Real DesktopController, separate QProcess audio worker, production AudioController JSON-lines IPC, real VAD/SenseVoice, backend HTTP/SSE and SQLite. Only the audio input device is replaced with timed local recording replay. No physical microphone/speaker, production model API or frozen installer tested.",
        "wake_word": args.wake_word,
        "expected": args.expected,
        "audio_sha256": hashlib.sha256(args.audio_file.read_bytes()).hexdigest(),
        "audio_seconds": len(pcm) / 32000,
        "source_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "jarvis/voice_runtime.py",
                "jarvis/audio_process.py",
                "jarvis/desktop_client.py",
                "jarvis/desktop_ui.py",
                "jarvis/config.py",
                "scripts/acceptance_voice_delivery.py",
            )
        },
        "audio_events": [],
        "input_changes": [],
        "positive_audio_level_events": 0,
        "checks": {},
        "screenshots": [],
    }
    with tempfile.TemporaryDirectory(prefix="jarvis-voice-delivery-") as temporary:
        directory = Path(temporary)
        preferences = {"JARVIS_WAKE_WORD": args.wake_word}
        (directory / "settings.json").write_text(
            json.dumps(preferences, ensure_ascii=False), encoding="utf-8"
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        environment = {
            "JARVIS_CONFIG_FILE": str(directory / "settings.json"),
            "JARVIS_DATA_DIR": str(directory / "data"),
            "JARVIS_WORKSPACE": str(directory / "workspace"),
            "JARVIS_DESKTOP_PORT": str(port),
            "JARVIS_STT_MODEL_DIR": str(model.resolve()),
            "OPENAI_API_KEY": "",
            "BRAVE_API_KEY": "",
            "UI_SPEECH_ENABLED": "false",
            "UI_AUTO_FULLSCREEN": "false",
        }
        with patch.dict(os.environ, environment):
            # Exercise the saved wake word without inheriting a shell override.
            os.environ.pop("JARVIS_WAKE_WORD", None)
            controller = DesktopController(app, autostart=False)
            original_launch = controller._launch
            original_audio = controller.handle_audio_event
            initial_session = [None]
            observed_input = [False]
            finished = [False]
            complete_since = [None]

            def elapsed():
                return round(time.monotonic() - started, 3)

            def launch(process, role, *arguments, environment=None):
                if role == "audio":
                    process.setProcessEnvironment(controller._environment())
                    process.start(
                        sys.executable,
                        [
                            str(Path(__file__).resolve()),
                            "--worker",
                            "--audio-file",
                            str(args.audio_file),
                        ],
                    )
                else:
                    original_launch(process, role, *arguments, environment=environment)

            def audio(event, data):
                if event == "audio.level":
                    if data.get("speech") and data.get("level", 0) > 0:
                        report["positive_audio_level_events"] += 1
                else:
                    report["audio_events"].append(
                        {"event": event, "data": data, "seconds": elapsed()}
                    )
                original_audio(event, data)
                if (
                    event == "stt.cancelled"
                    and data.get("reason") == "not_addressed"
                    and controller.results
                ):
                    report["unaddressed_status_at_cancellation"] = (
                        controller.results.status.text()
                    )
                if event == "stt.partial" and controller.results:
                    text = controller.results.input.toPlainText()
                    if text and data.get("text") in text:
                        report["checks"]["partial_visible_in_real_input"] = True
                        controller.results.grab().save(str(output / "voice-input.png"))
                        if "voice-input.png" not in report["screenshots"]:
                            report["screenshots"].append("voice-input.png")

            controller._launch = launch
            controller.handle_audio_event = audio

            def finish(error=None):
                if finished[0]:
                    return
                finished[0] = True
                if error:
                    report["error"] = str(error)
                report["elapsed_seconds"] = elapsed()
                report["controller_errors"] = dict(controller.errors)
                report["discard_pending"] = controller._voice_reset_id is not None
                report["minimum_generation"] = controller._voice_min_generation
                report["audio_stderr_tail"] = controller.audio_log[-2000:]
                (output / "result.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                controller.cleanup()
                app.quit()

            def poll():
                try:
                    if finished[0]:
                        return
                    if time.monotonic() - started > args.timeout:
                        raise TimeoutError(
                            "Voice delivery did not finish within the acceptance deadline"
                        )
                    if (
                        controller.ready
                        and controller.active_session_id
                        and initial_session[0] is None
                    ):
                        initial_session[0] = controller.active_session_id
                        controller.show_results()
                        controller.results.showNormal()
                        controller.results.resize(1220, 860)
                    if controller.results and not observed_input[0]:
                        observed_input[0] = True
                        controller.results.input.textChanged.connect(
                            lambda: report["input_changes"].append(
                                {
                                    "seconds": elapsed(),
                                    "text": controller.results.input.toPlainText(),
                                }
                            )
                        )
                    finals = [
                        e for e in report["audio_events"] if e["event"] == "stt.final"
                    ]
                    tasks = [
                        t
                        for t in controller.tasks.values()
                        if t.get("status") in {"succeeded", "awaiting_input", "failed"}
                    ]
                    rejected = any(
                        e["event"] == "stt.cancelled"
                        and e["data"].get("reason") == "not_addressed"
                        for e in report["audio_events"]
                    )
                    completed = (
                        bool(finals and tasks)
                        if args.expected == "accepted"
                        else rejected
                    )
                    if not completed:
                        return
                    if complete_since[0] is None:
                        complete_since[0] = time.monotonic()
                    if time.monotonic() - complete_since[0] < 0.8:
                        return
                    with closing(
                        sqlite3.connect(directory / "data/jarvis.sqlite3")
                    ) as database:
                        database.row_factory = sqlite3.Row
                        runs = [
                            dict(row)
                            for row in database.execute(
                                "SELECT task_id,session_id,message,status FROM tasks"
                            )
                        ]
                        session_count = database.execute(
                            "SELECT COUNT(*) FROM sessions"
                        ).fetchone()[0]
                    final_text = finals[0]["data"]["text"] if finals else ""
                    report["run_records"] = runs
                    report["final_text"] = final_text
                    report["partial_count"] = sum(
                        e["event"] == "stt.partial" for e in report["audio_events"]
                    )
                    report["final_count"] = len(finals)
                    report["visible_state"] = {
                        "wake_required": controller.wake_required,
                        "wake_word": controller.wake_word,
                        "listening": controller.listening,
                        "tile_caption": controller.tile.caption,
                        "input_placeholder": controller.results.input.placeholderText(),
                        "input_hint": controller.results.input_hint.text(),
                        "status": controller.last_status,
                    }
                    report["checks"].update(
                        {
                            "actual_discard_ack_received": bool(
                                {
                                    e["data"].get("id")
                                    for e in report["audio_events"]
                                    if e["event"] == "stt.discarded"
                                }
                                & {
                                    e["data"].get("id")
                                    for e in report["audio_events"]
                                    if e["event"] == "audio.ack"
                                    and e["data"].get("command") == "discard_input"
                                }
                            ),
                            "discard_boundary_released": controller._voice_reset_id
                            is None,
                            "real_pcm_waveform_observed": report[
                                "positive_audio_level_events"
                            ]
                            > 0,
                            "same_existing_session": session_count == 1
                            and all(
                                run["session_id"] == initial_session[0] for run in runs
                            ),
                            "no_voice_errors": not controller.errors,
                        }
                    )
                    if args.expected == "accepted":
                        report["checks"].update(
                            {
                                "partial_visible_in_real_input": report["checks"].get(
                                    "partial_visible_in_real_input", False
                                ),
                                "one_final_one_persisted_run": len(finals)
                                == len(runs)
                                == 1,
                                "final_text_matches_run": bool(runs)
                                and runs[0]["message"] == final_text,
                                "partial_never_submitted_as_an_extra_run": report[
                                    "partial_count"
                                ]
                                > 0
                                and len(runs) == 1,
                                "expected_content": not args.expect_text
                                or args.expect_text in final_text,
                            }
                        )
                    else:
                        visible = report["visible_state"]
                        report["checks"].update(
                            {
                                "unaddressed_audio_rejected": rejected,
                                "no_input_or_run_without_wake": not finals
                                and not runs
                                and not report["partial_count"]
                                and not report["input_changes"]
                                and not controller.results.input.toPlainText(),
                                "still_listening_for_configured_wake_word": controller.listening
                                and controller.wake_required
                                and controller.wake_word == args.wake_word,
                                "tile_explains_waiting_for_wake": visible[
                                    "tile_caption"
                                ]
                                == "等待唤醒",
                                "placeholder_explains_wake_word": args.wake_word
                                in visible["input_placeholder"]
                                and "先说" in visible["input_placeholder"]
                                and "唤醒" in visible["input_placeholder"],
                                "input_hint_explains_wake_requirement": "语音需先唤醒"
                                in visible["input_hint"],
                                "status_explains_waiting_for_wake": args.wake_word
                                in visible["status"]
                                and "唤醒" in visible["status"],
                                "unaddressed_audio_explanation_emitted": "还未唤醒"
                                in report.get("unaddressed_status_at_cancellation", ""),
                            }
                        )
                    assert all(report["checks"].values()), report["checks"]
                    report["task_scope"] = (
                        "Task model intentionally unconfigured: natural-language demo response, not autonomous production model execution."
                    )
                    report["passed"] = True
                    screenshot = (
                        "voice-run.png"
                        if args.expected == "accepted"
                        else "waiting-wake.png"
                    )
                    controller.results.grab().save(str(output / screenshot))
                    report["screenshots"].append(screenshot)
                    if args.expected == "wake_required":
                        controller.tile.grab().save(
                            str(output / "waiting-wake-tile.png")
                        )
                        report["screenshots"].append("waiting-wake-tile.png")
                    finish()
                except Exception as error:
                    finish(error)

            timer = QTimer()
            timer.setInterval(25)
            timer.timeout.connect(poll)
            timer.start()
            controller.start_backend()
            app.exec()
            timer.stop()
    print(
        json.dumps(
            {
                key: report.get(key)
                for key in (
                    "passed",
                    "wake_word",
                    "expected",
                    "checks",
                    "partial_count",
                    "final_count",
                    "final_text",
                    "error",
                )
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--audio-file", type=Path, required=True)
    parser.add_argument("--stt-model-dir", type=Path)
    parser.add_argument("--wake-word", default="贾维斯")
    parser.add_argument(
        "--expected", choices=("accepted", "wake_required"), default="accepted"
    )
    parser.add_argument("--expect-text", default="")
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--output", type=Path, default=Path("artifacts/voice-delivery"))
    args = parser.parse_args()
    args.audio_file = args.audio_file.resolve()
    if not 10 <= args.timeout <= 120:
        parser.error("timeout must be between 10 and 120 seconds")
    return worker(args.audio_file) if args.worker else run(args)


if __name__ == "__main__":
    raise SystemExit(main())
