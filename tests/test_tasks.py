"""Task scheduling, persistence and cancellation semantics without remote models."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import sqlite3
import unittest

from jarvis.config import Settings
from jarvis.storage import Store
from jarvis.tasks import TaskManager, TaskConflict, TaskQueueFull
from jarvis.tools import ToolRegistry


class TaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = Settings(
            workspace=root / "workspace", data_dir=root / "data", orchestrator="native"
        )
        self.store = Store(self.settings.data_dir)
        self.registry = ToolRegistry(self.settings.workspace, self.store)
        self.managers = []

    async def asyncTearDown(self):
        for manager in self.managers:
            await manager.close()
        self.temp.cleanup()

    async def manager(self, factory, settings=None):
        manager = TaskManager(
            settings or self.settings, self.store, self.registry, factory
        )
        self.managers.append(manager)
        await manager.start()
        return manager

    async def until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.005)

    async def terminal(self, manager, task_id):
        await self.until(
            lambda: (
                manager.require(task_id)["status"]
                in {
                    "succeeded",
                    "partially_succeeded",
                    "failed",
                    "cancelled",
                    "interrupted",
                    "awaiting_input",
                }
            )
        )
        return manager.details(task_id)

    async def test_real_tool_artifact_and_isolated_conversation(self):
        seen = []

        class Agent:
            def __init__(inner, settings, store, registry):
                inner.store, inner.registry = store, registry

            async def run(inner, message):
                seen.append(inner.store.history())
                yield (
                    "tool_start",
                    {
                        "name": "write_file",
                        "arguments": {
                            "path": message + ".md",
                            "content": "# " + message,
                        },
                    },
                )
                result = await inner.registry.execute(
                    "write_file", {"path": message + ".md", "content": "# " + message}
                )
                yield "tool_result", {"name": "write_file", "result": result}
                yield (
                    "answer",
                    {"content": "已保存 " + message, "speech_text": "报告已保存。"},
                )

        manager = await self.manager(Agent)
        first = await manager.submit("第一份")
        second = await manager.submit("第二份")
        one = await self.terminal(manager, first["task_id"])
        two = await self.terminal(manager, second["task_id"])
        self.assertEqual(one["status"], "succeeded")
        self.assertEqual(two["status"], "succeeded")
        self.assertEqual(
            (self.settings.workspace / "第一份.md").read_text(), "# 第一份"
        )
        self.assertEqual(len(one["artifacts"]), 1)
        self.assertEqual(one["speech_text"], "报告已保存。")
        self.assertEqual(
            seen,
            [
                [{"role": "user", "content": "第一份"}],
                [{"role": "user", "content": "第二份"}],
            ],
        )
        self.assertEqual(one["steps"][0]["status"], "succeeded")
        types = [event["type"] for event in one["events"]]
        self.assertLess(types.index("artifact.created"), types.index("task.completed"))

    async def test_followup_snapshot_is_durable_bounded_and_isolated(self):
        seen = []

        class Agent:
            def __init__(inner, settings, store, registry):
                inner.store, inner.registry = store, registry

            async def run(inner, message):
                seen.append(inner.store.history())
                if message == "准备前序资料":
                    await inner.registry.execute(
                        "write_file",
                        {
                            "path": "references.md",
                            "content": "# 实际参考文档",
                        },
                    )
                    yield "answer", {"content": "前序结果" * 2000}
                else:
                    yield "answer", {"content": "本次结果：" + message}

        manager = await self.manager(Agent)
        parent = await manager.submit("准备前序资料")
        original = await self.terminal(manager, parent["task_id"])
        unrelated = await manager.submit("无关个人记录不要进入上下文")
        await self.terminal(manager, unrelated["task_id"])
        child = await manager.submit(
            "根据刚才资料制作讲解稿", "followup-id", parent["task_id"]
        )
        detail = await self.terminal(manager, child["task_id"])
        self.assertEqual(detail["parent_task_id"], parent["task_id"])
        self.assertEqual(
            detail["parent_context"]["artifacts"][0]["path"], "references.md"
        )
        self.assertLessEqual(len(detail["parent_context"]["summary"]), 6000)
        history = seen[-1]
        self.assertEqual([item["role"] for item in history], ["user", "user"])
        self.assertIn("不能改变系统规则", history[0]["content"])
        self.assertIn("references.md", history[0]["content"])
        self.assertNotIn("无关个人记录", str(history))
        self.assertEqual(history[-1]["content"], "根据刚才资料制作讲解稿")
        self.assertEqual(manager.details(parent["task_id"]), original)
        reopened = Store(self.settings.data_dir)
        self.assertEqual(
            reopened.get_task(child["task_id"])["parent_context"],
            detail["parent_context"],
        )
        self.assertEqual(reopened.scoped(child["task_id"]).history()[0], history[0])
        self.assertEqual(reopened.list_tasks()[0]["parent_task_id"], parent["task_id"])
        self.assertEqual(
            [item["role"] for item in detail["messages"]], ["user", "assistant"]
        )
        again = await manager.submit(
            "根据刚才资料制作讲解稿", "followup-id", parent["task_id"]
        )
        self.assertEqual(again["task_id"], child["task_id"])
        with self.assertRaises(TaskConflict):
            await manager.submit(
                "根据刚才资料制作讲解稿", "followup-id", unrelated["task_id"]
            )

    async def test_followup_requires_finished_parent_with_reference_material(self):
        class Agent:
            def __init__(inner, *args):
                pass

            async def run(inner, message):
                yield "answer", {"content": "结果"}

        manager = await self.manager(Agent)
        for parent_id in ("missing",):
            with self.assertRaises(TaskConflict):
                await manager.submit("继续", parent_task_id=parent_id)
        queued = self.store.create_task("还未执行")
        with self.assertRaises(TaskConflict):
            await manager.submit("继续", parent_task_id=queued["task_id"])
        self.store.update_task(
            queued["task_id"], "task.completed", {}, status="succeeded"
        )
        with self.assertRaises(TaskConflict):
            await manager.submit("继续", parent_task_id=queued["task_id"])

    async def test_followup_json_escaping_keeps_context_and_artifact_paths_complete(
        self,
    ):
        from jarvis.orchestration import _bounded_history

        escaped = '\\\n\t\x01"'
        message = "原始指令" + escaped * 1500
        summary = "实际结果" + escaped * 6000
        for with_artifacts in (False, True):
            with self.subTest(with_artifacts=with_artifacts):
                parent = self.store.create_task(message)
                self.store.update_task(
                    parent["task_id"],
                    "task.completed",
                    {},
                    status="succeeded",
                    display_text=summary,
                )
                paths = []
                if with_artifacts:
                    for index in range(32):
                        path = f"reference-{index:02d}-" + "x" * 150 + ".md"
                        target = self.registry.workspace / path
                        target.write_text("真实参考文件", encoding="utf-8")
                        self.store.add_artifact(
                            parent["task_id"],
                            path,
                            target.stat().st_size,
                            "text/markdown",
                        )
                        paths.append(path)
                manager = TaskManager(self.settings, self.store, self.registry)
                self.managers.append(manager)
                child = await manager.submit("继续", parent_task_id=parent["task_id"])
                context = child["parent_context"]
                encoded = json.dumps(context, ensure_ascii=False)
                self.assertLessEqual(len(encoded), 11000)
                self.assertEqual(json.loads(encoded), context)
                for field, original, budget in (
                    ("message", message, 1500),
                    ("summary", summary, 6000),
                ):
                    self.assertTrue(context[field])
                    self.assertTrue(original.startswith(context[field]))
                    self.assertLessEqual(
                        len(json.dumps(context[field], ensure_ascii=False)), budget
                    )
                if with_artifacts:
                    self.assertTrue(context["artifacts_truncated"])
                    self.assertGreater(len(context["artifacts"]), 0)
                    self.assertLess(len(context["artifacts"]), len(paths))
                    self.assertEqual(context["artifacts"][0]["path"], paths[0])
                    self.assertTrue(
                        all(item["path"] in paths for item in context["artifacts"])
                    )
                else:
                    self.assertEqual(context["artifacts"], [])
                    self.assertFalse(context["artifacts_truncated"])
                # Exercise the actual MAF history boundary, not just raw JSON.
                history = _bounded_history(self.store.scoped(child["task_id"]), "继续")
                reference = history[0]["content"]
                self.assertLessEqual(len(reference), 12000)
                self.assertEqual(json.loads(reference.split("\n", 1)[1]), context)
                self.assertEqual(history[-1], {"role": "user", "content": "继续"})

    async def test_multiple_artifacts_validate_actual_files_and_preserve_hidden_role(
        self,
    ):
        original = self.registry.execute

        async def output(name, arguments):
            if name == "make_outputs":
                (self.registry.workspace / "report.md").write_text(
                    "真实正文", encoding="utf-8"
                )
                (self.registry.workspace / "report.preview.json").write_text(
                    "{}", encoding="utf-8"
                )
                return {
                    "artifacts": [
                        {"path": "report.md", "media_type": "text/html"},
                        {
                            "path": "report.preview.json",
                            "hidden": True,
                            "role": "preview_manifest",
                        },
                        {"path": "missing.docx"},
                        {"path": "../outside.md"},
                    ]
                }
            return await original(name, arguments)

        self.registry.execute = output

        class Agent:
            def __init__(inner, settings, store, registry):
                inner.registry = registry

            async def run(inner, message):
                result = await inner.registry.execute("make_outputs", {})
                self.assertIn("error", result)
                yield "answer", {"content": "只保留已验证文件", "status": "succeeded"}

        manager = await self.manager(Agent)
        task = await manager.submit("生成多个输出")
        detail = await self.terminal(manager, task["task_id"])
        self.assertEqual(detail["status"], "partially_succeeded")
        artifacts = {item["path"]: item for item in detail["artifacts"]}
        self.assertEqual(set(artifacts), {"report.md", "report.preview.json"})
        self.assertFalse(artifacts["report.md"]["hidden"])
        self.assertNotEqual(artifacts["report.md"]["media_type"], "text/html")
        self.assertTrue(artifacts["report.preview.json"]["hidden"])
        self.assertEqual(artifacts["report.preview.json"]["role"], "preview_manifest")
        reopened = Store(self.settings.data_dir)
        self.assertTrue(
            reopened.get_artifact(artifacts["report.preview.json"]["artifact_id"])[
                "hidden"
            ]
        )

    async def test_cancel_keeps_all_outputs_from_one_actual_operation(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def output(name, arguments):
            started.set()
            await release.wait()
            for name in ("report.md", "notes.md"):
                (self.registry.workspace / name).write_text("已完成", encoding="utf-8")
            return {"artifacts": [{"path": "report.md"}, {"path": "notes.md"}]}

        self.registry.execute = output

        class Agent:
            def __init__(inner, settings, store, registry):
                inner.registry = registry

            async def run(inner, message):
                await inner.registry.execute("make_outputs", {})
                yield "answer", {"content": "完成"}

        manager = await self.manager(Agent)
        task = await manager.submit("生成资料")
        await started.wait()
        await manager.cancel(task["task_id"])
        release.set()
        detail = await self.terminal(manager, task["task_id"])
        self.assertEqual(detail["status"], "cancelled")
        self.assertEqual(
            {item["path"] for item in detail["artifacts"]}, {"report.md", "notes.md"}
        )
        self.assertEqual(detail["steps"][0]["status"], "succeeded")

    async def test_cancel_signals_cooperative_tool_and_audits_partial_outputs(self):
        started = asyncio.Event()
        stop = threading.Event()
        cancellation_calls = []

        def cancel_active():
            cancellation_calls.append(True)
            stop.set()

        async def output(name, arguments):
            started.set()
            stopped = await asyncio.to_thread(stop.wait, 1)
            self.assertTrue(stopped, "cooperative cancellation was not delivered")
            (self.registry.workspace / "completed-script.md").write_text(
                "已经完成的讲稿", encoding="utf-8"
            )
            return {
                "error": "视频生成已取消",
                "artifacts": [{"path": "completed-script.md"}],
            }

        self.registry.execute = output
        self.registry.cancel_active = cancel_active

        class Agent:
            def __init__(inner, settings, store, registry):
                inner.registry = registry

            async def run(inner, message):
                await inner.registry.execute("render_video", {})
                yield "answer", {"content": "不应执行到这里"}

        manager = await self.manager(Agent)
        task = await manager.submit("生成讲解")
        await started.wait()
        await manager.cancel(task["task_id"])
        detail = await self.terminal(manager, task["task_id"])
        self.assertEqual(cancellation_calls, [True])
        self.assertEqual(detail["status"], "cancelled")
        self.assertEqual(detail["steps"][0]["status"], "failed")
        self.assertEqual(detail["artifacts"][0]["path"], "completed-script.md")

    def test_previous_database_migration_preserves_tasks_and_artifacts(self):
        directory = Path(self.temp.name) / "legacy-data"
        directory.mkdir()
        database = directory / "jarvis.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.executescript("""
                CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY, client_request_id TEXT UNIQUE,
                    message TEXT NOT NULL, status TEXT NOT NULL, held INTEGER NOT NULL DEFAULT 0,
                    display_text TEXT NOT NULL DEFAULT '', speech_text TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', plan TEXT NOT NULL DEFAULT '[]',
                    elapsed_seconds REAL NOT NULL DEFAULT 0,
                    model_calls INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE artifacts (
                    artifact_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, path TEXT NOT NULL,
                    size INTEGER NOT NULL, media_type TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(task_id,path)
                );
                INSERT INTO tasks(task_id,message,status,display_text,created_at,updated_at)
                VALUES ('old-task','之前的任务','succeeded','保留旧结果','2026-01-01','2026-01-01');
                INSERT INTO artifacts VALUES ('old-artifact','old-task','kept.md',12,'text/markdown','2026-01-01');
                PRAGMA user_version=1;
            """)
        migrated = Store(directory)
        task = migrated.get_task("old-task")
        self.assertEqual(task["display_text"], "保留旧结果")
        self.assertIsNone(task["parent_task_id"])
        self.assertEqual(task["parent_context"], {})
        artifact = migrated.get_artifact("old-artifact")
        self.assertEqual(artifact["path"], "kept.md")
        self.assertFalse(artifact["hidden"])
        self.assertEqual(artifact["role"], "primary")
        migrated.add_artifact("old-task", "next.md", 4, "text/markdown")
        self.assertEqual(len(migrated.list_artifacts("old-task")), 2)

    async def test_idempotent_submission_and_bounded_queue(self):
        ready = asyncio.Event()
        release = asyncio.Event()

        class Agent:
            def __init__(inner, *args):
                pass

            async def run(inner, message):
                ready.set()
                await release.wait()
                yield "answer", {"content": message}

        manager = await self.manager(Agent)
        task = await manager.submit("one", "same-request")
        await ready.wait()
        again = await manager.submit("one", "same-request")
        self.assertEqual(task["task_id"], again["task_id"])
        with self.assertRaises(TaskConflict):
            await manager.submit("different", "same-request")
        queued = [await manager.submit(str(n)) for n in range(3)]
        with self.assertRaises(TaskQueueFull):
            await manager.submit("overflow")
        await manager.cancel(queued[1]["task_id"])
        replacement = await manager.submit("replacement")
        self.assertEqual(manager.require(queued[1]["task_id"])["status"], "cancelled")
        release.set()
        await self.terminal(manager, replacement["task_id"])

    async def test_abandoned_event_subscription_does_not_cancel_execution(self):
        release = asyncio.Event()

        class Agent:
            def __init__(inner, *args):
                pass

            async def run(inner, message):
                yield "status", {"message": "工作中"}
                await release.wait()
                yield "answer", {"content": "任务完成"}

        manager = await self.manager(Agent)
        task = await manager.submit("do it")
        stream = manager.events(task_id=task["task_id"])
        first = await anext(stream)
        await stream.aclose()
        self.assertEqual(first["type"], "task.queued")
        release.set()
        detail = await self.terminal(manager, task["task_id"])
        self.assertEqual(detail["status"], "succeeded")
        replay = self.store.events_after(first["event_id"], task["task_id"])
        self.assertTrue(any(event["type"] == "task.completed" for event in replay))
        self.assertTrue(all(event["event_id"] > first["event_id"] for event in replay))

    async def test_awaiting_input_releases_slot_and_preserves_task_history(self):
        seen = []

        class Agent:
            def __init__(inner, settings, store, registry):
                inner.store = store

            async def run(inner, message):
                seen.append(inner.store.history())
                if message == "请写报告":
                    yield "answer", {"content": "主题是什么？", "awaiting_input": True}
                else:
                    yield "answer", {"content": "完成：" + message}

        manager = await self.manager(Agent)
        first = await manager.submit("请写报告")
        waiting = await self.terminal(manager, first["task_id"])
        self.assertEqual(waiting["status"], "awaiting_input")
        second = await manager.submit("另一项工作")
        self.assertEqual(
            (await self.terminal(manager, second["task_id"]))["status"], "succeeded"
        )
        await manager.provide_input(first["task_id"], "销售情况")
        result = await self.terminal(manager, first["task_id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(
            [m["content"] for m in seen[-1]], ["请写报告", "主题是什么？", "销售情况"]
        )
        with self.assertRaises(TaskConflict):
            await manager.provide_input(first["task_id"], "不能再次补充")

    async def test_cancel_waits_for_tool_write_and_preserves_true_outcome(self):
        began = asyncio.Event()
        release = asyncio.Event()
        original = self.registry.execute

        async def delayed(name, args):
            began.set()
            await release.wait()
            return await original(name, args)

        self.registry.execute = delayed

        class Agent:
            def __init__(inner, settings, store, registry):
                inner.registry = registry

            async def run(inner, message):
                args = {"path": "already.md", "content": "saved"}
                yield "tool_start", {"name": "write_file", "arguments": args}
                result = await inner.registry.execute("write_file", args)
                yield "tool_result", {"name": "write_file", "result": result}
                yield "answer", {"content": "done"}

        manager = await self.manager(Agent)
        task = await manager.submit("write")
        await began.wait()
        pending = await manager.cancel(task["task_id"])
        self.assertTrue(pending["cancel_requested"])
        release.set()
        detail = await self.terminal(manager, task["task_id"])
        self.assertEqual(detail["status"], "cancelled")
        self.assertEqual((self.settings.workspace / "already.md").read_text(), "saved")
        self.assertEqual(detail["steps"][0]["status"], "succeeded")
        self.assertEqual(len(detail["artifacts"]), 1)

    async def test_cancel_followed_by_shutdown_still_audits_worker_thread_write(self):
        began = threading.Event()
        release = threading.Event()

        def write():
            began.set()
            if not release.wait(3):
                raise TimeoutError("test worker was not released")
            return self.registry._write_file("late.md", "saved")

        async def execute(name, arguments):
            return await asyncio.to_thread(write)

        self.registry.execute = execute

        class Agent:
            def __init__(inner, settings, store, registry):
                inner.registry = registry

            async def run(inner, message):
                arguments = {"path": "late.md", "content": "saved"}
                yield "tool_start", {"name": "write_file", "arguments": arguments}
                result = await inner.registry.execute("write_file", arguments)
                yield "tool_result", {"name": "write_file", "result": result}
                yield "answer", {"content": "done"}

        manager = await self.manager(Agent)
        task = await manager.submit("write")
        await self.until(began.is_set)
        await manager.cancel(task["task_id"])
        await asyncio.sleep(0)
        closing = asyncio.create_task(manager.close())
        await asyncio.sleep(0)
        release.set()
        await closing
        result = manager.details(task["task_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual((self.settings.workspace / "late.md").read_text(), "saved")
        self.assertEqual(result["steps"][0]["status"], "succeeded")
        self.assertEqual(result["artifacts"][0]["path"], "late.md")

    async def test_restart_marks_running_interrupted_and_holds_queued_tasks(self):
        running = self.store.create_task("running")
        self.store.update_task(
            running["task_id"], "task.planning", {}, status="running"
        )
        queued = self.store.create_task("queued")
        calls = []

        class Agent:
            def __init__(inner, *args):
                pass

            async def run(inner, message):
                calls.append(message)
                yield "answer", {"content": "done"}

        manager = await self.manager(Agent)
        self.assertEqual(manager.require(running["task_id"])["status"], "interrupted")
        self.assertTrue(manager.require(queued["task_id"])["held"])
        await asyncio.sleep(0.02)
        self.assertEqual(calls, [])
        with self.assertRaises(TaskConflict):
            await manager.resume(running["task_id"])
        await manager.resume(queued["task_id"])
        self.assertEqual(
            (await self.terminal(manager, queued["task_id"]))["status"], "succeeded"
        )
        self.assertEqual(calls, ["queued"])

    async def test_model_failure_after_tool_success_is_partial_not_blanket_success(
        self,
    ):
        from jarvis.agent import AgentError

        class Agent:
            def __init__(inner, settings, store, registry):
                inner.registry = registry

            async def run(inner, message):
                args = {"path": "kept.md", "content": "saved"}
                yield "tool_start", {"name": "write_file", "arguments": args}
                yield (
                    "tool_result",
                    {
                        "name": "write_file",
                        "result": await inner.registry.execute("write_file", args),
                    },
                )
                raise AgentError("模型失去连接")

        manager = await self.manager(Agent)
        task = await manager.submit("write")
        detail = await self.terminal(manager, task["task_id"])
        self.assertEqual(detail["status"], "partially_succeeded")
        self.assertEqual(detail["error"], "模型失去连接")
        self.assertEqual(len(detail["artifacts"]), 1)

    async def test_timeout_releases_slot_and_reports_failure(self):
        class Agent:
            def __init__(inner, *args):
                pass

            async def run(inner, message):
                if message == "slow":
                    await asyncio.sleep(10)
                yield "answer", {"content": "done"}

        manager = await self.manager(Agent, replace(self.settings, task_timeout=0.025))
        one = await manager.submit("slow")
        two = await manager.submit("fast")
        self.assertEqual(
            (await self.terminal(manager, one["task_id"]))["status"], "failed"
        )
        self.assertEqual(
            (await self.terminal(manager, two["task_id"]))["status"], "succeeded"
        )
        await self.until(lambda: not manager.busy)

    async def test_shutdown_interrupts_active_and_holds_pending_without_dispatch(self):
        began = asyncio.Event()
        calls = []

        class Agent:
            def __init__(inner, *args):
                pass

            async def run(inner, message):
                calls.append(message)
                began.set()
                await asyncio.Event().wait()
                yield "answer", {"content": "unreachable"}

        manager = await self.manager(Agent)
        active = await manager.submit("active")
        await began.wait()
        queued = await manager.submit("pending")
        await manager.close()
        self.assertEqual(manager.require(active["task_id"])["status"], "interrupted")
        self.assertTrue(manager.require(queued["task_id"])["held"])
        self.assertEqual(calls, ["active"])

    async def test_expired_question_is_cancelled_and_cannot_be_resumed_as_new_input(
        self,
    ):
        task = self.store.create_task("需要信息")
        self.store.update_task(
            task["task_id"], "task.awaiting_input", {}, status="awaiting_input"
        )
        with self.store._connection() as db:
            db.execute(
                "UPDATE tasks SET updated_at='2000-01-01T00:00:00+00:00' WHERE task_id=?",
                (task["task_id"],),
            )

        class Agent:
            def __init__(inner, *args):
                pass

            async def run(inner, message):
                yield "answer", {"content": "should not run"}

        manager = await self.manager(Agent)
        result = manager.details(task["task_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["error"], "等待补充超时")
        self.assertEqual(result["events"][-1]["type"], "task.cancelled")
        with self.assertRaises(TaskConflict):
            await manager.provide_input(task["task_id"], "late response")
