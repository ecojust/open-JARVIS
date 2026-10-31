"""Actual Qt/API/SSE session acceptance, with explicitly replayed STT events.

Does not open audio devices or call an external model. Demo commands create real
files; the transcript is a recorded real SenseVoice result, not microphone input.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def run(output: Path):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from jarvis.desktop_client import DesktopController

    output.mkdir(parents=True, exist_ok=True)
    replay = json.loads(
        (
            ROOT / "docs/validation/session-voice-2026-10-31/voice-replay.json"
        ).read_text(encoding="utf-8")
    )
    report = {
        "passed": False,
        "scope": "real Qt desktop, owned backend, HTTP/SSE and SQLite; replayed recorded STT IPC events; demo tools, no production model or physical audio devices",
        "checks": {},
        "screenshots": [],
    }
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="jarvis-session-acceptance-") as temporary:
        directory = Path(temporary)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        environment = {
            "JARVIS_CONFIG_FILE": str(directory / "settings.json"),
            "JARVIS_DATA_DIR": str(directory / "data"),
            "JARVIS_WORKSPACE": str(directory / "workspace"),
            "JARVIS_DESKTOP_PORT": str(port),
            "OPENAI_API_KEY": "",
            "BRAVE_API_KEY": "",
            "UI_SPEECH_ENABLED": "false",
            "UI_AUTO_FULLSCREEN": "false",
        }
        with patch.dict(os.environ, environment):
            controller = DesktopController(app, autostart=False)
            controller.start_audio = lambda: None
            phase = ["ready"]
            original_session = [None]
            observed = []
            subscribed = [False]
            replayed = [False]
            pending = [False]

            def screenshot(name):
                app.processEvents()
                controller.results.grab().save(str(output / name))
                report["screenshots"].append(name)

            def finish(error=None):
                if phase[0] == "finished":
                    return
                phase[0] = "finished"
                if error:
                    report["error"] = str(error)
                controller.cleanup()
                app.quit()

            def tasks():
                return [
                    task
                    for task in controller.tasks.values()
                    if task.get("session_id") == original_session[0]
                    and task.get("status") in {"succeeded", "awaiting_input"}
                ]

            def poll():
                try:
                    if phase[0] == "finished":
                        return
                    if time.monotonic() - started > 25:
                        raise TimeoutError(
                            "session acceptance exceeded 25 seconds: " + phase[0]
                        )
                    if controller.client is not None and not subscribed[0]:
                        controller.client.event.connect(
                            lambda item: observed.append(item)
                        )
                        subscribed[0] = True
                    if (
                        phase[0] == "ready"
                        and controller.ready
                        and controller.active_session_id
                    ):
                        original_session[0] = controller.active_session_id
                        report["checks"]["persisted_initial_session"] = True
                        controller.show_results()
                        controller.results.showNormal()
                        controller.results.resize(1220, 860)
                        controller.results.input.setPlainText(
                            "/write session-report.md | # 本周工作总结\n\n已完成资料整理，下一步完善财务报告。"
                        )
                        controller.results._submit()
                        phase[0] = "first"
                    elif phase[0] == "first" and len(tasks()) == 1:
                        report["checks"]["real_file_write"] = (
                            directory / "workspace/session-report.md"
                        ).is_file()
                        controller.results.input.setPlainText("/read session-report.md")
                        controller.results._submit()
                        phase[0] = "second"
                    elif phase[0] == "second" and len(tasks()) == 2:
                        report["checks"]["two_runs_one_session"] = (
                            controller.active_session_id == original_session[0]
                        )
                        controller.refresh_sessions()
                        phase[0] = "partial"
                    elif phase[0] == "partial" and not replayed[0]:
                        replayed[0] = True
                        for event in replay["events"]:
                            if event["event"] == "started":
                                controller.handle_audio_event(
                                    "stt.started", event["data"]
                                )
                            elif event["event"] == "transcript" and not event[
                                "data"
                            ].get("final"):
                                controller.handle_audio_event(
                                    "stt.partial", event["data"]
                                )
                        assert len(tasks()) == 2
                        assert "本周工作总结" in controller.results.input.toPlainText()
                        report["checks"]["recorded_partials_visible_not_sent"] = True
                        screenshot("voice-partial.png")
                        final = next(
                            item["data"]
                            for item in replay["events"]
                            if item["event"] == "transcript"
                            and item["data"].get("final")
                        )
                        controller.handle_audio_event("stt.final", final)
                        controller.handle_audio_event("stt.final", final)
                        phase[0] = "final"
                    elif phase[0] == "final" and len(tasks()) == 3:
                        report["voice_run_status"] = (
                            "awaiting_input (demo asks to configure task model)"
                        )
                        report["checks"]["final_auto_sent_once_same_session"] = (
                            controller.active_session_id == original_session[0]
                        )
                        report["checks"]["recorded_final_text_matches_run"] = (
                            sum(
                                task.get("message") == replay["final_text"]
                                for task in tasks()
                            )
                            == 1
                        )
                        controller.refresh_sessions()
                        controller.results.tools_button.click()
                        phase[0] = "tools"
                    elif (
                        phase[0] == "tools"
                        and controller.tools_dialog
                        and controller.tools_dialog.tools
                    ):
                        assert any(
                            item["name"] == "fetch_page"
                            for item in controller.tools_dialog.tools
                        )
                        controller.tools_dialog.search.setText("fetch_page")
                        app.processEvents()
                        controller.tools_dialog.grab().save(
                            str(output / "agent-tools.png")
                        )
                        report["screenshots"].append("agent-tools.png")
                        report["checks"]["real_catalog_loaded_by_toolbar_icon"] = True
                        controller.tools_dialog.close()
                        controller.handle_audio_event(
                            "stt.final",
                            {
                                "text": "新建对话。",
                                "utterance_id": "new-conversation-command",
                                "generation": 0,
                                "final": True,
                            },
                        )
                        phase[0] = "new"
                    elif (
                        phase[0] == "new"
                        and not controller._session_switching
                        and controller.active_session_id != original_session[0]
                    ):
                        report["checks"]["spoken_new_session_only_explicitly"] = True
                        assert controller.selected_task is None
                        controller.refresh_sessions()
                        phase[0] = "empty"
                    elif (
                        phase[0] == "empty" and controller.results.sessions.count() == 2
                    ):
                        screenshot("new-session.png")
                        controller.select_session(original_session[0])
                        phase[0] = "restored"
                    elif (
                        phase[0] == "restored"
                        and controller.active_session_id == original_session[0]
                        and len(controller.task_order) == 3
                        and not pending[0]
                    ):
                        pending[0] = True
                        screenshot("conversation-runs.png")

                        def verify(payload):
                            try:
                                assert len(payload["runs"]) == 3
                                assert all(
                                    run["session_id"] == original_session[0]
                                    for run in payload["runs"]
                                )
                                report["checks"]["server_three_distinct_runs"] = (
                                    len({run["run_id"] for run in payload["runs"]}) == 3
                                )
                                report["checks"]["real_sse_completed"] = (
                                    sum(
                                        item["type"]
                                        in {"task.completed", "task.awaiting_input"}
                                        for item in observed
                                    )
                                    == 3
                                )
                                report["checks"]["restored_conversation"] = True
                                report["passed"] = all(report["checks"].values())
                                finish()
                            except Exception as error:
                                finish(repr(error))

                        controller.client.request(
                            "GET",
                            f"/api/sessions/{original_session[0]}/runs",
                            callback=verify,
                            error=finish,
                        )
                    if phase[0] != "finished":
                        QTimer.singleShot(30, poll)
                except Exception as error:
                    finish(f"{type(error).__name__}: {error}")

            controller.start_backend()
            QTimer.singleShot(30, poll)
            app.exec()
            controller.cleanup()
            controller.deleteLater()
            app.processEvents()
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    (output / "ui-acceptance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(run(parser.parse_args().output))
