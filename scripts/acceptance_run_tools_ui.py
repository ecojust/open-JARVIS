"""Real Qt/HTTP/SSE and persisted run-tool UI acceptance.

Two demo commands execute actual file tools. A separately labelled 1,205-step
SQLite audit fixture tests pagination and restart; it does not execute tools.
No external model, microphone, loudspeaker or external web service is used.
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


def run(output):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from jarvis.desktop_client import DesktopController
    from jarvis.storage import Store

    output.mkdir(parents=True, exist_ok=True)
    report = {
        "passed": False,
        "scope": "actual Qt, owned backend, HTTP/SSE, SQLite and two real demo file-tool runs; separately injected synthetic 1205-step audit fixture for pagination; no external model or audio hardware",
        "checks": {},
        "screenshots": [],
    }
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="jarvis-run-tools-") as temporary:
        directory = Path(temporary)
        store = Store(directory / "data")
        fixture = store.create_task(
            "分页压力夹具：1,205 条合成执行记录（未实际调用这些工具）"
        )
        now = Store._now()
        with store._connection() as db:
            rows = []
            for index in range(1205):
                failed = index == 1204
                rows.append(
                    (
                        f"fixture_step_{index:04d}",
                        fixture["task_id"],
                        "delegate_research" if index % 5 == 0 else "fetch_page",
                        json.dumps(
                            {"fixture": True, "index": index}, ensure_ascii=False
                        ),
                        json.dumps(
                            {"error": "这是合成的失败记录，用于验收历史状态。"}
                            if failed
                            else {
                                "fixture": True,
                                "summary": f"分页夹具第 {index + 1} 项",
                            },
                            ensure_ascii=False,
                        ),
                        "failed" if failed else "succeeded",
                        now,
                        now,
                    )
                )
            db.executemany(
                "INSERT INTO steps(step_id,task_id,name,arguments,result,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
        store.update_task(
            fixture["task_id"],
            "task.completed",
            {"status": "partially_succeeded"},
            status="partially_succeeded",
            display_text="这些是明确标记的审计夹具，不代表 1,205 次真实工具执行。",
        )
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
            phase = "ready"
            events = []
            connected = False
            success_run = None
            failure_run = None
            all_controllers = [controller]

            def screenshot(name):
                app.processEvents()
                inspector = controller.results.tool_calls.details
                inspector.verticalScrollBar().setValue(
                    inspector.verticalScrollBar().maximum()
                )
                app.processEvents()
                controller.results.grab().save(str(output / name))
                report["screenshots"].append(name)

            def finish(error=None):
                nonlocal phase
                if phase == "finished":
                    return
                phase = "finished"
                if error:
                    report["error"] = str(error)
                for owned in all_controllers:
                    owned.cleanup()
                app.quit()

            def completed(message, status):
                return next(
                    (
                        task
                        for task in controller.tasks.values()
                        if task.get("message", "").startswith(message)
                        and task.get("status") == status
                    ),
                    None,
                )

            def poll():
                nonlocal phase, connected, controller, success_run, failure_run
                try:
                    if phase == "finished":
                        return
                    if time.monotonic() - started > 30:
                        raise TimeoutError("acceptance exceeded 30 seconds at " + phase)
                    if controller.client is not None and not connected:
                        controller.client.event.connect(events.append)
                        connected = True
                    panel = (
                        controller.results.tool_calls if controller.results else None
                    )
                    if (
                        phase == "ready"
                        and controller.ready
                        and controller.active_session_id
                    ):
                        controller.show_results()
                        controller.results.showNormal()
                        controller.results.resize(1260, 960)
                        controller.submit(
                            "/write audit-demo.md | # 工具记录验收\n\n这份文件由真实工具写入。"
                        )
                        phase = "success"
                    elif phase == "success":
                        task = completed("/write audit-demo.md", "succeeded")
                        if task:
                            success_run = task["task_id"]
                            controller.select_task(success_run)
                            phase = "success-view"
                    elif (
                        phase == "success-view"
                        and panel.run_id == success_run
                        and panel.total == 1
                    ):
                        report["checks"]["real_file_tool_written"] = (
                            directory / "workspace/audit-demo.md"
                        ).is_file()
                        assert not controller.results.reply_visible
                        panel.toggle.click()
                        phase = "success-page"
                    elif (
                        phase == "success-page"
                        and panel._loaded
                        and panel.model.rowCount() == 1
                    ):
                        assert panel.model.rows[0]["name"] == "write_file"
                        assert panel.model.rows[0]["status"] == "succeeded"
                        assert panel.details.toPlainText() == ""
                        report["checks"]["details_are_explicitly_opened"] = True
                        panel.table.setCurrentIndex(panel.model.index(0, 0))
                        phase = "success-detail"
                    elif (
                        phase == "success-detail"
                        and "输入参数" in panel.details.toPlainText()
                        and "audit-demo.md" in panel.details.toPlainText()
                    ):
                        screenshot("real-tool-success.png")
                        report["checks"]["real_success_parameters_and_result"] = True
                        controller.submit("/read missing-audit-file.txt")
                        phase = "failure"
                    elif phase == "failure":
                        task = completed("/read missing-audit-file.txt", "failed")
                        if task:
                            failure_run = task["task_id"]
                            controller.select_task(failure_run)
                            phase = "failure-view"
                    elif (
                        phase == "failure-view"
                        and panel.run_id == failure_run
                        and panel.total == 1
                    ):
                        assert panel.model.rows[0]["name"] == "read_file"
                        assert panel.details.toPlainText() == ""
                        panel.toggle.click()
                        phase = "failure-page"
                    elif phase == "failure-page" and panel._loaded:
                        panel.table.setCurrentIndex(panel.model.index(0, 0))
                        phase = "failure-detail"
                    elif (
                        phase == "failure-detail"
                        and "结果摘要" in panel.details.toPlainText()
                        and '"error"' in panel.details.toPlainText()
                    ):
                        screenshot("real-tool-failure.png")
                        report["checks"]["separate_run_real_failure"] = (
                            panel.model.rows[0]["status"] == "failed"
                            and "audit-demo.md" not in panel.details.toPlainText()
                        )
                        report["checks"]["durable_sse_start_and_outcome"] = all(
                            any(
                                event.get("task_id") == identifier
                                and event["type"] == kind
                                for event in events
                            )
                            for identifier, kind in [
                                (success_run, "tool.started"),
                                (success_run, "tool.completed"),
                                (failure_run, "tool.started"),
                                (failure_run, "tool.failed"),
                            ]
                        )
                        controller.select_task(fixture["task_id"])
                        phase = "fixture"
                    elif (
                        phase == "fixture"
                        and panel.run_id == fixture["task_id"]
                        and panel.total == 1205
                    ):
                        assert panel.model.rowCount() == 100
                        assert panel.details.toPlainText() == ""
                        panel.toggle.click()
                        phase = "fixture-first"
                    elif phase == "fixture-first" and panel._loaded:
                        assert panel.model.rows[0]["step_id"] == "fixture_step_0000"
                        panel.next.click()
                        phase = "fixture-second"
                    elif (
                        phase == "fixture-second"
                        and not panel._pending_page
                        and panel.offset == 100
                    ):
                        assert panel.model.rows[0]["step_id"] == "fixture_step_0100"
                        assert panel.model.rowCount() == 100
                        report["checks"]["history_second_page_from_real_http"] = True
                        panel.request_page(1200)
                        phase = "fixture-last"
                    elif (
                        phase == "fixture-last"
                        and not panel._pending_page
                        and panel.offset == 1200
                    ):
                        assert panel.model.rowCount() == 5
                        assert panel.model.rows[-1]["status"] == "failed"
                        panel.table.setCurrentIndex(panel.model.index(4, 0))
                        phase = "fixture-detail"
                    elif (
                        phase == "fixture-detail"
                        and "合成的失败记录" in panel.details.toPlainText()
                    ):
                        screenshot("synthetic-audit-last-page.png")
                        report["checks"]["last_fixture_page_and_failed_detail"] = True
                        report["checks"]["agent_reply_remains_opt_in"] = (
                            not controller.results.reply_visible
                            and controller.results.body.toPlainText() == ""
                        )
                        controller.cleanup()
                        controller = DesktopController(app, autostart=False)
                        controller.start_audio = lambda: None
                        all_controllers.append(controller)
                        connected = False
                        controller.start_backend()
                        phase = "restart"
                    elif (
                        phase == "restart"
                        and controller.ready
                        and controller.active_session_id
                    ):
                        controller.show_results()
                        controller.results.showNormal()
                        controller.results.resize(1260, 960)
                        controller.select_task(fixture["task_id"])
                        phase = "restart-fixture"
                    elif (
                        phase == "restart-fixture"
                        and panel.run_id == fixture["task_id"]
                        and panel.total == 1205
                    ):
                        panel.toggle.click()
                        phase = "restart-page"
                    elif phase == "restart-page" and panel._loaded:
                        assert panel.model.rowCount() == 100
                        assert panel.model.rows[0]["step_id"] == "fixture_step_0000"
                        report["checks"][
                            "backend_and_controller_restart_restores_history"
                        ] = True
                        report["checks"]["same_session_distinct_real_runs"] = (
                            store.get_task(success_run)["session_id"]
                            == store.get_task(failure_run)["session_id"]
                            and success_run != failure_run
                        )
                        report["fixture"] = {
                            "steps": 1205,
                            "actual_tools_executed_by_fixture": 0,
                            "rows_in_ui_at_once": 100,
                            "last_page_rows": 5,
                        }
                        report["passed"] = all(report["checks"].values())
                        finish()
                    if phase != "finished":
                        QTimer.singleShot(25, poll)
                except Exception as error:
                    finish(f"{type(error).__name__}: {error}")

            controller.start_backend()
            QTimer.singleShot(25, poll)
            app.exec()
            for owned in all_controllers:
                owned.cleanup()
                owned.deleteLater()
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
