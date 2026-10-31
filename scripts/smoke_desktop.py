#!/usr/bin/env python3
"""Repeatable native desktop → real backend → SSE → file → fullscreen smoke.

Run ``python scripts/smoke_desktop.py`` after installing desktop requirements.
The default Qt platform is offscreen so this can run on all three CI targets.
This is NOT microphone, loudspeaker, platform-window or packaged-app acceptance.
The supervisor isolates settings/files, disables real audio and cloud inference,
and limits the whole run to fifteen seconds including timeout cleanup.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
RESULT_PREFIX = "JARVIS_SMOKE_RESULT="
WORKER_TIMEOUT_SECONDS = 12
SOFT_TIMEOUT_MS = 8000
EXPECTED_TEXT = "# 桌面端冒烟测试\n\n这份文件由真实本地任务工具生成。"
EXPECTED_NAME = "desktop-smoke.md"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _worker(directory: Path, port: int) -> int:
    """Run Qt in its own process so a blocked event loop cannot hang CI."""
    sys.path.insert(0, str(ROOT))
    report = {
        "passed": False,
        "conditions": {
            "system": platform.system(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "isolated_user_data": True,
            "real_qapplication": True,
            "real_backend_process": True,
            "real_audio_worker_process": True,
            "task_mode": "deterministic demo command",
            "microphone_opened": False,
            "tts_playback_enabled": False,
            "speech_models_loaded": False,
            "cloud_model_used": False,
            "packaged_application_tested": False,
            "platform_hardware_acceptance": False,
        },
        "checks": {},
        "timings_seconds": {},
    }
    controller = None
    started = time.monotonic()
    try:
        from PySide6.QtCore import QProcess, QTimer, qVersion
        from PySide6.QtWidgets import QApplication
        from jarvis.desktop_client import DesktopController

        app = QApplication([])
        app.setApplicationName("Open JARVIS Desktop Smoke")
        app.setOrganizationName("ecojust")
        app.setQuitOnLastWindowClosed(False)
        report["conditions"]["qt"] = qVersion()
        report["conditions"]["qt_platform"] = app.platformName()
        controller = DesktopController(app)
        app.jarvis_controller = controller
        report["checks"]["only_tile_initially"] = (
            controller.tile.isVisible() and controller.results is None
        )
        assert report["checks"]["only_tile_initially"], (
            "result window created at startup"
        )
        assert not controller.speech_enabled, "speech must be disabled for CI"
        observed_types = []
        observed_task = None
        subscribed = False
        submitted = False
        quit_requests = 0
        quitting_at = None
        soft_timed_out = False

        def observed(envelope):
            nonlocal observed_task
            kind = envelope.get("type")
            observed_types.append(kind)
            if kind == "task.completed":
                observed_task = envelope.get("task_id")

        def finish(error=None):
            nonlocal quit_requests, quitting_at
            if quitting_at is not None:
                return
            if error:
                report["error"] = str(error)
            quitting_at = time.monotonic()
            quit_requests += 1
            # Must work on the FIRST quit even when results are fullscreen.
            controller.request_quit()

        def deadline():
            nonlocal soft_timed_out
            soft_timed_out = True
            report["error"] = (
                "first quit did not leave the Qt event loop"
                if quitting_at is not None
                else "desktop pipeline exceeded the 8-second soft deadline"
            )
            if quitting_at is None:
                finish(report["error"])
            # Ensure a quit veto is a failed test, not a second successful quit.
            app.exit(1)

        def poll():
            nonlocal submitted, subscribed
            try:
                if controller.client is not None and not subscribed:
                    controller.client.event.connect(observed)
                    subscribed = True
                if controller.ready and not submitted:
                    assert controller.backend.state() == QProcess.ProcessState.Running
                    # Wait for the real audio IPC worker to exist. Its isolated
                    # missing-model directory prevents it opening a microphone.
                    if controller.audio.state() != QProcess.ProcessState.Running:
                        QTimer.singleShot(25, poll)
                        return
                    report["checks"]["owned_children_started"] = bool(
                        controller.backend.processId() and controller.audio.processId()
                    )
                    assert report["checks"]["owned_children_started"]
                    report["checks"]["backend_identity_verified"] = controller.ready
                    report["timings_seconds"]["backend_ready"] = round(
                        time.monotonic() - started, 3
                    )
                    submitted = True
                    assert controller.submit(
                        f"/write {EXPECTED_NAME} | {EXPECTED_TEXT}"
                    )
                if observed_task is not None:
                    task = controller.tasks.get(observed_task, {})
                    assert task.get("status") == "succeeded", task.get("status")
                    target = directory / "workspace" / EXPECTED_NAME
                    assert target.read_text(encoding="utf-8") == EXPECTED_TEXT
                    assert {"tool.started", "tool.completed", "task.completed"} <= set(
                        observed_types
                    ), observed_types
                    assert controller.client.last_event_id > 0
                    assert controller.results is not None
                    assert controller.results.isVisible()
                    assert controller.results.isFullScreen()
                    assert controller.results.task_id == observed_task
                    report["checks"].update(
                        {
                            "real_api_and_sse": True,
                            "real_file_created": True,
                            "fullscreen_matching_result": True,
                            "no_webengine_loaded": not any(
                                name.startswith("PySide6.QtWebEngine")
                                for name in sys.modules
                            ),
                        }
                    )
                    assert report["checks"]["no_webengine_loaded"]
                    report["timings_seconds"]["result_visible"] = round(
                        time.monotonic() - started, 3
                    )
                    finish()
                    return
                if "backend" in controller.errors:
                    finish(controller.errors["backend"])
                    return
                QTimer.singleShot(25, poll)
            except Exception as exc:
                finish(f"{type(exc).__name__}: {exc}")

        QTimer.singleShot(SOFT_TIMEOUT_MS, deadline)
        QTimer.singleShot(25, poll)
        exit_code = app.exec()
        controller.cleanup()
        report["checks"]["first_quit_exited"] = (
            quit_requests == 1 and not soft_timed_out and exit_code == 0
        )
        report["checks"]["owned_children_stopped"] = all(
            process.state() == QProcess.ProcessState.NotRunning
            for process in (controller.backend, controller.audio)
        )
        if quitting_at is not None:
            report["timings_seconds"]["quit_and_cleanup"] = round(
                time.monotonic() - quitting_at, 3
            )
        required = {
            "only_tile_initially",
            "owned_children_started",
            "backend_identity_verified",
            "real_api_and_sse",
            "real_file_created",
            "fullscreen_matching_result",
            "no_webengine_loaded",
            "first_quit_exited",
            "owned_children_stopped",
        }
        report["passed"] = not report.get("error") and all(
            report["checks"].get(name) for name in required
        )
        if not report["passed"] and "error" not in report:
            report["error"] = "one or more lifecycle assertions failed"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        if controller is not None:
            controller.cleanup()
    report["timings_seconds"]["worker_total"] = round(time.monotonic() - started, 3)
    print(RESULT_PREFIX + json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["passed"] else 1


def _stop_process_tree(process: subprocess.Popen) -> None:
    """Only stop the supervisor's newly created process group/tree."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=1.5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run(qt_platform: str) -> int:
    started = time.monotonic()
    report = {"passed": False, "conditions": {"supervised_timeout_seconds": 15}}
    with tempfile.TemporaryDirectory(prefix="jarvis-desktop-smoke-") as temporary:
        directory = Path(temporary)
        port = _free_port()
        environment = dict(os.environ)
        environment.update(
            {
                "QT_QPA_PLATFORM": qt_platform,
                "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8",
                "JARVIS_CONFIG_FILE": str(directory / "settings.json"),
                "JARVIS_DATA_DIR": str(directory / "data"),
                "JARVIS_WORKSPACE": str(directory / "workspace"),
                "JARVIS_DESKTOP_PORT": str(port),
                "JARVIS_STT_MODEL_DIR": str(directory / "uninstalled-stt"),
                "JARVIS_TTS_MODEL_DIR": str(directory / "uninstalled-tts"),
                "JARVIS_ORCHESTRATOR": "native",
                "OPENAI_API_KEY": "",
                "BRAVE_API_KEY": "",
                "UI_SPEECH_ENABLED": "false",
                "UI_AUTO_FULLSCREEN": "true",
            }
        )
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            str(directory),
            "--port",
            str(port),
        ]
        options = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **options,
        )
        try:
            stdout, stderr = process.communicate(timeout=WORKER_TIMEOUT_SECONDS)
            lines = [
                line for line in stdout.splitlines() if line.startswith(RESULT_PREFIX)
            ]
            if lines:
                report = json.loads(lines[-1][len(RESULT_PREFIX) :])
            else:
                report["error"] = "worker exited without a JSON report"
            report.setdefault("conditions", {})["supervised_timeout_seconds"] = 15
            if process.returncode != 0:
                report["passed"] = False
                report.setdefault("error", f"worker exit code {process.returncode}")
            if not report.get("passed") and stderr:
                report["diagnostic"] = stderr[-1500:]
        except subprocess.TimeoutExpired:
            _stop_process_tree(process)
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
            report["error"] = "hard timeout; owned process tree was terminated"
        except (ValueError, OSError) as exc:
            _stop_process_tree(process)
            report["error"] = f"invalid worker result: {exc}"

        # A completed subprocess must not leave an owned descendant behind.
        if os.name != "nt" and process.poll() is not None:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                pass
            else:
                _stop_process_tree(process)
                report["passed"] = False
                report["error"] = "worker left a descendant process running"
        # TIME_WAIT connections may prevent a fresh bind even after the server
        # and its listening socket are gone. Test for an actual listener instead.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            try:
                listener_alive = probe.connect_ex(("127.0.0.1", port)) == 0
            except OSError:
                listener_alive = False
        report.setdefault("checks", {})["backend_listener_closed"] = not listener_alive
        if listener_alive:
            report["passed"] = False
            report.setdefault("error", "backend listener remains reachable after exit")

    report.setdefault("timings_seconds", {})["supervised_total"] = round(
        time.monotonic() - started, 3
    )
    report["conditions"]["temporary_data_removed"] = not directory.exists()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("passed") else 1


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--qt-platform",
        default="offscreen",
        help="Qt platform plugin (default: offscreen)",
    )
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.worker is not None:
        return _worker(arguments.worker, arguments.port)
    return run(arguments.qt_platform)


if __name__ == "__main__":
    raise SystemExit(main())
