#!/usr/bin/env python3
"""Execute six real artifact/desktop scenarios with an explicit scripted agent.

This is tool-workflow acceptance, NOT a claim that an unconfigured language
model understood the commands. Research prose comes from the reviewed input
JSON, while the actual registry searches/reads sources, writes artifacts, runs
Kokoro/FFmpeg, and exposes actual-file previews through HTTP/SSE and native Qt.

python scripts/acceptance_scenarios.py --research research.json --output artifacts/acceptance
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODE = "scripted_tool_workflow"
RESULT_PREFIX = "JARVIS_ACCEPTANCE_RESULT="
SCENARIOS = (
    (
        "word",
        "帮我搜一下最近国际形势，用word总结一下我看看",
        "document",
        "国际形势总结.docx",
    ),
    (
        "excel",
        "帮我搜一下最近国际形势，用excel列举一下",
        "spreadsheet",
        "国际形势清单.xlsx",
    ),
    (
        "ppt",
        "帮我搜一下最近金融趋势，做一个ppt我看看",
        "presentation",
        "近期金融趋势.pptx",
    ),
    ("schedule", "帮我排一个30人的排班计划", "spreadsheet", "30人一周示例排班.xlsx"),
    (
        "video",
        "刚才的ppt你帮我剪一个讲解视频，用于发布b站的",
        "video",
        "金融趋势讲解.mp4",
    ),
    (
        "article",
        "帮我写一个最近金融的总结性文章，后面会用于发布到微信公众号的",
        "markdown",
        "金融趋势公众号文章.md",
    ),
)


def _sources(values):
    result = []
    for source in values:
        title = source.get("title", "来源")
        if source.get("id"):
            title = f"[{source['id']}] {title}"
        item = {"title": title[:300], "url": source["url"]}
        for key in ("published_at", "accessed_at", "note"):
            if source.get(key):
                item[key] = str(source[key])
        result.append(item)
    return result


def _sha256(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _report(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def make_scenario_agent(research):
    class ScenarioAgent:
        """A visibly scripted tool plan; never used as the product's NLP fallback."""

        def __init__(self, settings, store, registry):
            self.settings, self.store, self.registry = settings, store, registry

        async def run(self, message):
            selected = next((row for row in SCENARIOS if row[1] == message), None)
            if selected is None:
                raise ValueError(
                    "验收执行器只接受列明的六条测试指令，不是自然语言模型。"
                )
            name, _, _, filename = selected
            international = research["international"]
            finance = research["financial_presentation_six"]
            sources = (
                international["sources"]
                if name in {"word", "excel"}
                else finance["sources"]
            )
            if name == "article":
                sources = research["wechat_article"]["sources"]
            operations = []
            if name in {"word", "excel", "ppt", "article"}:
                query = (
                    "United Nations international humanitarian situation October 2026"
                    if name in {"word", "excel"}
                    else "global financial trends October 2026 IMF Federal Reserve World Bank"
                )
                operations.append(("search_web", {"query": query}))
                unique = dict.fromkeys(source["url"] for source in sources)
                operations.extend(("fetch_page", {"url": url}) for url in unique)
            if name == "word":
                operations.append(
                    (
                        "create_document",
                        {
                            "path": filename,
                            "title": international["title"],
                            "sections": [
                                {
                                    "heading": row.get("heading", ""),
                                    "body": row.get("body", row.get("text", "")),
                                }
                                for row in international["sections"]
                            ],
                            "sources": _sources(sources),
                        },
                    )
                )
            elif name == "excel":
                operations.append(
                    (
                        "create_spreadsheet",
                        {
                            "path": filename,
                            "title": international["title"],
                            "columns": international["columns"],
                            "rows": international["rows"],
                            "sources": _sources(sources),
                        },
                    )
                )
            elif name == "ppt":
                operations.append(
                    (
                        "create_presentation",
                        {
                            "path": filename,
                            "title": finance["title"],
                            "slides": [
                                {
                                    "title": row["title"],
                                    "bullets": row["bullets"],
                                    "notes": row["notes"],
                                    "sources": _sources(row.get("sources", [])),
                                }
                                for row in finance["slides"]
                            ],
                            "sources": _sources(sources),
                        },
                    )
                )
            elif name == "schedule":
                operations.append(
                    (
                        "create_schedule",
                        {
                            "path": filename,
                            "start_date": "2026-11-02",
                            "days": 7,
                            "employee_count": 30,
                            "min_rest_hours": 12,
                            "max_consecutive_days": 5,
                            "max_shifts_per_employee": 5,
                        },
                    )
                )
            elif name == "video":
                current = self.store.get_task(self.store.task_id)
                context = current.get("parent_context") or {}
                ppt = next(
                    (
                        item
                        for item in context.get("artifacts", [])
                        if str(item.get("path", "")).lower().endswith(".pptx")
                    ),
                    None,
                )
                if not ppt:
                    raise ValueError(
                        "视频任务没有通过持久化父任务引用取得实际 PPT，不能偷用固定文件路径。"
                    )
                operations.append(("get_file_preview", {"path": ppt["path"]}))
                operations.append(
                    (
                        "create_explainer_video",
                        {
                            "path": filename,
                            "presentation_path": ppt["path"],
                        },
                    )
                )
            else:
                operations.append(
                    (
                        "write_file",
                        {
                            "path": filename,
                            "content": research["wechat_article"]["markdown"],
                        },
                    )
                )
            failures, completed = [], []
            for tool, arguments in operations:
                self.store.record_usage(tool_calls_delta=1)
                yield "status", {"message": f"工具链验收（非大模型）：{tool}"}
                yield "tool_start", {"name": tool, "arguments": arguments}
                result = await self.registry.execute(tool, arguments)
                yield "tool_result", {"name": tool, "result": result}
                if result.get("error"):
                    failures.append({"tool": tool, "error": result["error"]})
                else:
                    completed.append(tool)
            status = (
                "partially_succeeded"
                if failures and completed
                else "failed"
                if failures
                else "succeeded"
            )
            special = (
                "\n\n排班使用 30 位匿名员工、2026-11-02 起一周、三班各 6 人、班间至少 12 小时休息、最多连续 5 天及每人 5 班的示例规则，不代表实际人员安排。"
                if name == "schedule"
                else ""
            )
            if name == "video":
                special = "\n\n视频来自承接任务的实际 PPT 和本地 Kokoro 讲解音轨，未发布到 B 站。"
            if name == "article":
                special = "\n\n文章为微信公众号编辑稿，未发布到公众号。"
            display = (
                f"## {filename}\n\n本次为明确的工具链与预览验收；正文使用已核对公开资料的验收输入，未调用自然语言大模型。"
                f"\n\n实际工具：{'、'.join(completed)}。{special}"
            )
            if failures:
                display += "\n\n未完成操作：\n" + "\n".join(
                    f"- {item['tool']}：{item['error']}" for item in failures
                )
            yield (
                "answer",
                {
                    "status": status,
                    "display_text": display,
                    "content": display,
                    "speech_text": "验收结果已显示，请查看实际文件预览。",
                    "awaiting_input": False,
                },
            )

    return ScenarioAgent


def serve(research_path, port):
    import uvicorn
    from jarvis.app import create_app
    from jarvis.config import Settings

    research = json.loads(research_path.read_text(encoding="utf-8"))
    uvicorn.run(
        create_app(Settings.from_env(), agent_factory=make_scenario_agent(research)),
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )


def worker(args):
    from PySide6.QtCore import QProcess, QTimer
    from PySide6.QtWidgets import QApplication
    from jarvis.desktop_client import DesktopController

    started = time.monotonic()
    output = args.output.resolve()
    screenshots = output / "screenshots"
    screenshots.mkdir(parents=True, exist_ok=True)
    workspace = output / "workspace"
    research = json.loads(args.research.read_text(encoding="utf-8"))
    report = {
        "passed": False,
        "mode": MODE,
        "natural_language_agent_execution_verified": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "research_as_of": research.get("coverage_date"),
        "research_sha256": _sha256(args.research),
        "conditions": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "qt_platform": os.environ.get("QT_QPA_PLATFORM"),
            "real_backend_process": True,
            "real_http_sse": True,
            "real_qt_widgets": True,
            "microphone_tested": False,
            "desktop_speech_playback_tested": False,
            "real_video_narration_required": True,
            "audio_worker_enabled": False,
            "published_to_bilibili": False,
            "published_to_wechat": False,
        },
        "cases": [],
        "checks": {},
        "limitations": [
            "Six explicit scripted plans exercise real tools; no language-model key is configured and free-form model planning is not verified.",
            "Offscreen native Qt verifies fullscreen state and actual-file content previews, not physical monitor behavior or Office pixel-perfect rendering.",
            "The schedule is an anonymous example with stated constraints; user-specific staffing rules were not supplied.",
            "The MP4 contains real local Kokoro narration; actual loudspeaker output and external-player launch are not part of this headless run.",
        ],
    }

    class AcceptanceController(DesktopController):
        def _launch(self, process, role, *extra, environment=None):
            if role != "backend":
                return super()._launch(process, role, *extra, environment=environment)
            process.setProcessEnvironment(environment or self._environment())
            process.start(
                sys.executable,
                [
                    str(Path(__file__).resolve()),
                    "--serve",
                    "--research",
                    str(args.research),
                    "--output",
                    str(output),
                    "--port",
                    str(args.port),
                ],
            )

        def start_audio(self):
            # Explicit test scope: no microphone/audio worker; the video tool
            # loads its real model in its own background process.
            self.user_paused = True
            self.listening = False

    app = QApplication([])
    app.setApplicationName("Open JARVIS Artifact Acceptance")
    app.setQuitOnLastWindowClosed(False)
    controller = AcceptanceController(app)
    report["checks"]["tile_only_at_start"] = (
        controller.tile.isVisible() and controller.results is None
    )
    state = {
        "index": 0,
        "sent": False,
        "detail_pending": False,
        "case_started": None,
        "quitting": False,
        "subscribed": False,
        "preview_ready_since": None,
    }
    observed = {}

    def persist():
        _report(output / "report.json", report)

    def finish(error=None):
        if state["quitting"]:
            return
        state["quitting"] = True
        if error:
            report["error"] = str(error)
        controller.request_quit()

    def events(envelope):
        task_id = envelope.get("task_id")
        if task_id:
            observed.setdefault(task_id, []).append(envelope.get("type"))

    def complete_case(task, payload=None, preview_error=None):
        index = state["index"]
        label, command, expected_kind, filename = SCENARIOS[index]
        window = controller.results
        payload = payload or {}
        task_id = task["task_id"]
        record = {
            "index": index + 1,
            "case": label,
            "command": command,
            "task_id": task_id,
            "parent_task_id": task.get("parent_task_id"),
            "status": task["status"],
            "mode": MODE,
            "elapsed_seconds": round(time.monotonic() - state["case_started"], 3),
            "visible": bool(window and window.isVisible()),
            "fullscreen": bool(window and window.isFullScreen()),
            "window_size": [window.width(), window.height()] if window else None,
            "screen_size": (
                [window.screen().size().width(), window.screen().size().height()]
                if window and window.screen()
                else None
            ),
            "preview_kind": payload.get("kind"),
            "preview_source": payload.get("source"),
            "preview_error": preview_error,
            "source_urls_read": [],
            "search_result_count": 0,
            "tool_events": observed.get(task_id, []),
            "tool_steps": [],
            "artifacts": [],
        }
        for step in task.get("steps", []):
            result = step.get("result") or {}
            arguments = step.get("arguments") or {}
            record["tool_steps"].append(
                {
                    "name": step["name"],
                    "status": step["status"],
                    "error": result.get("error"),
                }
            )
            if step["name"] == "fetch_page" and not result.get("error"):
                content = result.get("content", "")
                record["source_urls_read"].append(
                    {
                        "url": result.get("url", arguments.get("url")),
                        "title": result.get("title"),
                        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                        "characters": len(content),
                    }
                )
            if step["name"] == "search_web" and not result.get("error"):
                record["search_result_count"] += len(result.get("results", []))
            if step["name"] == "create_explainer_video":
                record["video_source_presentation"] = arguments.get("presentation_path")
        for artifact in task.get("artifacts", []):
            path = (workspace / artifact["path"]).resolve()
            if not path.is_relative_to(workspace.resolve()) or not path.is_file():
                raise AssertionError("Persisted artifact missing or outside workspace")
            record["artifacts"].append(
                {
                    "path": artifact["path"],
                    "size": path.stat().st_size,
                    "sha256": _sha256(path),
                    "media_type": artifact["media_type"],
                    "hidden": artifact.get("hidden", False),
                }
            )
        record["checks"] = {
            "task_succeeded": task["status"] == "succeeded",
            "expected_file_exists": (workspace / filename).is_file(),
            "actual_file_preview": payload.get("source") == "actual_file"
            and payload.get("kind") == expected_kind,
            "preview_tab_selected": bool(
                window and window.content_tabs.currentWidget() is window.preview
            ),
            "fullscreen_matching_task": bool(
                window
                and window.isFullScreen()
                and window.isVisible()
                and window.task_id == task_id
            ),
            "real_sse_tool_events": {"tool.started", "tool.completed", "task.completed"}
            <= set(record["tool_events"]),
        }
        if label in {"word", "excel", "ppt", "article"}:
            record["checks"]["actual_web_sources_read"] = bool(
                record["source_urls_read"]
            )
            record["checks"]["actual_search_results"] = (
                record["search_result_count"] > 0
            )
        if (
            label == "schedule"
            and (workspace / (filename + ".schedule.json")).is_file()
        ):
            audit = json.loads(
                (workspace / (filename + ".schedule.json")).read_text(encoding="utf-8")
            )
            record["schedule_audit"] = audit["audit"]
            record["schedule_assumptions"] = audit["assumptions"]
            record["checks"]["thirty_people_schedule_audited"] = (
                len(audit["employees"]) == 30
                and audit["anonymous_example"]
                and audit["audit"]["passed"]
            )
        if label == "video":
            ppt_case = report["cases"][2]
            record["checks"]["references_ppt_across_schedule_task"] = (
                task.get("parent_task_id") == ppt_case["task_id"]
                and record.get("video_source_presentation") == SCENARIOS[2][3]
            )
            record["checks"]["actual_video_and_audio_streams"] = (
                payload.get("video_codec") == "h264"
                and payload.get("audio_codec") == "aac"
                and bool(payload.get("has_audio"))
            )
            record["video_probe"] = {
                key: payload.get(key)
                for key in (
                    "duration_seconds",
                    "width",
                    "height",
                    "video_codec",
                    "audio_codec",
                    "has_audio",
                    "verification_source",
                )
            }
        if window and window.isVisible():
            shot = screenshots / f"{index + 1:02d}-{label}.png"
            if not window.grab().save(str(shot)):
                raise AssertionError(
                    "Could not save the actual native window screenshot"
                )
            record["screenshot"] = shot.relative_to(output).as_posix()
        if payload:
            _report(output / "previews" / f"{index + 1:02d}-{label}.json", payload)
        record["passed"] = all(record["checks"].values())
        report["cases"].append(record)
        persist()
        print(
            json.dumps(
                {
                    "event": "scenario_completed",
                    "case": label,
                    "passed": record["passed"],
                    "status": task["status"],
                    "elapsed_seconds": record["elapsed_seconds"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        state.update(
            index=index + 1,
            sent=False,
            detail_pending=False,
            preview_ready_since=None,
        )
        if state["index"] == len(SCENARIOS):
            finish()
        else:
            QTimer.singleShot(100, poll)

    def complete_safely(task, payload=None, preview_error=None):
        # Network callbacks run outside poll's try block. Persist the actual
        # failure and stop our owned backend instead of hanging until timeout.
        try:
            complete_case(task, payload, preview_error)
        except Exception as exc:
            finish(f"{type(exc).__name__}: {exc}")

    def poll():
        if state["quitting"]:
            return
        try:
            if controller.client is not None and not state["subscribed"]:
                controller.client.event.connect(events)
                state["subscribed"] = True
            if "backend" in controller.errors:
                raise AssertionError(controller.errors["backend"])
            if controller.ready and not state["sent"]:
                label, command, _, _ = SCENARIOS[state["index"]]
                state.update(sent=True, case_started=time.monotonic())
                if not controller.submit(command):
                    raise AssertionError("Desktop refused scenario submission")
                print(
                    json.dumps(
                        {
                            "event": "scenario_started",
                            "case": label,
                            "command": command,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            if state["sent"] and not state["detail_pending"]:
                _, command, expected_kind, _ = SCENARIOS[state["index"]]
                current = next(
                    (
                        task
                        for task in controller.tasks.values()
                        if task.get("message") == command
                    ),
                    None,
                )
                if current and current.get("status") in {
                    "succeeded",
                    "partially_succeeded",
                    "failed",
                    "cancelled",
                    "interrupted",
                }:
                    window = controller.results
                    payload = (
                        window.preview.payload
                        if window and window.task_id == current["task_id"]
                        else {}
                    )
                    if (
                        payload.get("source") == "actual_file"
                        and payload.get("kind") == expected_kind
                    ):
                        if state["preview_ready_since"] is None:
                            state["preview_ready_since"] = time.monotonic()
                        if time.monotonic() - state["preview_ready_since"] < 0.25:
                            # Let native layout/resize events settle so the
                            # screenshot and fullscreen check see the final UI.
                            QTimer.singleShot(50, poll)
                            return
                        state["detail_pending"] = True
                        controller.client.request(
                            "GET",
                            "/api/tasks/" + current["task_id"],
                            callback=lambda data, snapshot=dict(payload): (
                                complete_safely(data, snapshot)
                            ),
                            error=finish,
                        )
                        return
                    if (
                        current["status"] not in {"succeeded", "partially_succeeded"}
                        or time.monotonic() - state["case_started"] > 330
                    ):
                        state["detail_pending"] = True
                        controller.client.request(
                            "GET",
                            "/api/tasks/" + current["task_id"],
                            callback=lambda data: complete_safely(
                                data,
                                preview_error="Task failed or the actual-file preview never became ready",
                            ),
                            error=finish,
                        )
                        return
            QTimer.singleShot(50, poll)
        except Exception as exc:
            finish(f"{type(exc).__name__}: {exc}")

    QTimer.singleShot(
        int(args.timeout * 1000),
        lambda: finish("Acceptance exceeded its bounded runtime"),
    )
    QTimer.singleShot(25, poll)
    code = app.exec()
    controller.cleanup()
    report["checks"].update(
        {
            "all_six_submitted_and_observed": len(report["cases"]) == len(SCENARIOS),
            "owned_backend_stopped": controller.backend.state()
            == QProcess.ProcessState.NotRunning,
            "no_webengine_loaded": not any("QtWebEngine" in key for key in sys.modules),
            "qt_exit_code_zero": code == 0,
        }
    )
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["passed"] = (
        not report.get("error")
        and all(report["checks"].values())
        and all(item["passed"] for item in report["cases"])
    )
    persist()
    print(
        RESULT_PREFIX
        + json.dumps(
            {
                "passed": report["passed"],
                "report": str(output / "report.json"),
                "mode": MODE,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if report["passed"] else 1


def supervise(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "workspace").exists() or (output / "report.json").exists():
        raise ValueError(
            "Output already contains an acceptance run; use a new directory to preserve evidence."
        )
    # Keep the explicit, reviewed prose input with the evidence so this run
    # remains reproducible after a temporary research directory is removed.
    research_input = output / "research-input.json"
    research_input.write_bytes(args.research.read_bytes())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    from jarvis.paths import load_environment, model_dir

    load_environment()
    kokoro = model_dir("kokoro")
    environment = dict(os.environ)
    environment.update(
        {
            "QT_QPA_PLATFORM": args.qt_platform,
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "JARVIS_DATA_DIR": str(output / "private-data"),
            "JARVIS_CONFIG_FILE": str(output / "private-data" / "settings.json"),
            "JARVIS_WORKSPACE": str(output / "workspace"),
            "JARVIS_DESKTOP_PORT": str(port),
            "JARVIS_TTS_MODEL_DIR": str(kokoro),
            "JARVIS_STT_MODEL_DIR": str(output / "uninstalled-stt"),
            "JARVIS_TASK_TIMEOUT": "300",
            "JARVIS_MAX_TOOL_CALLS": "24",
            "OPENAI_API_KEY": "",
            "UI_SPEECH_ENABLED": "false",
            "UI_AUTO_FULLSCREEN": "true",
        }
    )
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--research",
        str(research_input),
        "--output",
        str(output),
        "--port",
        str(port),
        "--timeout",
        str(args.timeout),
    ]
    options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    with (output / "desktop.stderr.log").open("w", encoding="utf-8") as diagnostic:
        process = subprocess.Popen(
            command, cwd=ROOT, env=environment, stderr=diagnostic, **options
        )
        try:
            return process.wait(timeout=args.timeout + 20)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    timeout=5,
                    capture_output=True,
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            raise ValueError(
                "Acceptance process exceeded the hard deadline; owned process tree terminated."
            )


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--qt-platform", default="offscreen")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.research = args.research.resolve()
    if not 30 <= args.timeout <= 900:
        parser.error("--timeout must be 30–900 seconds")
    if args.serve:
        serve(args.research, args.port)
        return 0
    return worker(args) if args.worker else supervise(args)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
