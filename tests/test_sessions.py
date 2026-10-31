"""Persistent conversation sessions and independent, ordered turn executions."""

import asyncio
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from jarvis.app import create_app
from jarvis.config import Settings
from jarvis.storage import Store
from jarvis.tasks import TaskConflict, TaskManager
from jarvis.tools import ToolRegistry


class SessionStoreTests(unittest.TestCase):
    def test_mixed_prototype_and_task_migration_preserves_execution_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary))
            recent = store.create_task("2026年的任务")
            store.add_message("assistant", "近期回答", recent["task_id"])
            with store._connection() as db:
                db.executemany(
                    "INSERT INTO messages(role,content,created_at) VALUES (?,?,?)",
                    [
                        ("user", "2025年的问题", "2025-01-01"),
                        ("assistant", "旧回答", "2025-01-01"),
                    ],
                )
                db.execute("PRAGMA user_version=2")
            reopened = Store(Path(temporary))
            current = reopened.create_task("现在的问题")
            self.assertEqual(
                [m["content"] for m in reopened.scoped(current["task_id"]).history()],
                ["2025年的问题", "旧回答", "2026年的任务", "近期回答", "现在的问题"],
            )
            self.assertEqual(reopened.list_tasks()[1]["task_id"], recent["task_id"])

    def test_migration_keeps_legacy_records_and_restart_never_creates_a_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            database = directory / "jarvis.sqlite3"
            with closing(sqlite3.connect(database)) as db, db:
                db.executescript("""
                    CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL,
                    content TEXT NOT NULL, created_at TEXT NOT NULL);
                    INSERT INTO messages(role,content,created_at) VALUES ('user','旧输入','2025-01-01');
                    INSERT INTO messages(role,content,created_at) VALUES ('assistant','旧回答','2025-01-01');
                    PRAGMA user_version=1;
                """)
            store = Store(directory)
            initial = store.current_session()
            self.assertEqual(
                store.history(),
                [
                    {"role": "user", "content": "旧输入"},
                    {"role": "assistant", "content": "旧回答"},
                ],
            )
            historical = store.list_tasks(session_id=initial["session_id"])
            self.assertEqual(len(historical), 1)
            self.assertEqual(historical[0]["preview"], "旧回答")
            run = store.create_task("本轮")
            self.assertEqual(
                [item["content"] for item in store.scoped(run["task_id"]).history()],
                ["旧输入", "旧回答", "本轮"],
            )
            store.add_message("assistant", "已回答", run["task_id"])
            store.add_artifact(run["task_id"], "kept.md", 20, "text/markdown")
            second = store.create_session("第二段对话")
            self.assertNotEqual(initial["session_id"], second["session_id"])
            for _ in range(3):
                reopened = Store(directory)
                self.assertEqual(
                    reopened.current_session()["session_id"], second["session_id"]
                )
                self.assertEqual(len(reopened.list_sessions()), 2)
                self.assertEqual(reopened.history(), [])
                self.assertEqual(
                    reopened.get_task(run["task_id"])["session_id"],
                    initial["session_id"],
                )
                self.assertEqual(
                    reopened.get_task(run["task_id"])["run_id"], run["task_id"]
                )
                self.assertEqual(
                    reopened.list_artifacts(run["task_id"])[0]["path"], "kept.md"
                )
                self.assertEqual(
                    reopened.events_after()[0]["session_id"], initial["session_id"]
                )
            reopened.activate_session(initial["session_id"])
            self.assertEqual(
                Store(directory).current_session()["session_id"], initial["session_id"]
            )
            self.assertEqual(len(reopened.history()), 4)

    def test_artifact_context_is_session_scoped_and_bounded_with_large_histories(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary))
            first_session = store.current_session()["session_id"]
            old = store.create_task("财务演示")
            store.add_message("assistant", "A" * 40000, old["task_id"])
            store.add_artifact(
                old["task_id"], "finance.pptx", 1000, "application/presentation"
            )
            other = store.create_session()
            foreign = store.create_task("秘密内容")
            store.add_message("assistant", "秘密回答", foreign["task_id"])
            store.add_artifact(
                foreign["task_id"], "secret.docx", 400, "application/document"
            )
            store.activate_session(first_session)
            for index in range(30):
                run = store.create_task(str(index))
                store.add_message("assistant", "B" * 40000, run["task_id"])
            previous = store.create_task("刚刚的PPT")
            store.add_artifact(
                previous["task_id"], "latest.pptx", 1000, "application/presentation"
            )
            current = store.create_task("用刚才PPT做视频")
            history = store.scoped(current["task_id"]).history()
            content = json.dumps(history, ensure_ascii=False)
            self.assertNotIn("秘密", content)
            self.assertNotIn("secret.docx", content)
            self.assertIn("latest.pptx", content)
            self.assertLessEqual(sum(len(item["content"]) for item in history), 44000)
            self.assertEqual(history[-1]["content"], current["message"])
            self.assertEqual(
                store.list_tasks(session_id=other["session_id"])[0]["task_id"],
                foreign["task_id"],
            )


class SessionRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(workspace=root / "workspace", data_dir=root / "data")
        self.store = Store(self.settings.data_dir)
        self.registry = ToolRegistry(self.settings.workspace, self.store)
        self.manager = None

    async def asyncTearDown(self):
        if self.manager:
            await self.manager.close()
        self.temporary.cleanup()

    async def done(self, task_id):
        async with asyncio.timeout(3):
            while self.store.get_task(task_id)["status"] not in {
                "succeeded",
                "awaiting_input",
                "failed",
            }:
                await asyncio.sleep(0.005)
        return self.store.get_task(task_id)

    async def test_queued_run_sees_previous_answer_but_never_future_or_foreign_inputs(
        self,
    ):
        seen, started, release = {}, asyncio.Event(), asyncio.Event()

        class Agent:
            def __init__(inner, settings, store, registry):
                inner.store = store

            async def run(inner, message):
                if message == "第一问":
                    started.set()
                    await release.wait()
                seen[message] = inner.store.history()
                yield "answer", {"content": message + "的回答"}

        self.manager = TaskManager(
            replace(self.settings, max_pending_tasks=5),
            self.store,
            self.registry,
            Agent,
        )
        await self.manager.start()
        session = self.store.current_session()["session_id"]
        one = await self.manager.submit("第一问")
        await started.wait()
        two = await self.manager.submit("第二问")
        other = self.store.create_session()
        foreign = await self.manager.submit("其他会话内容")
        three = await self.manager.submit("第三问", session_id=session)
        release.set()
        await self.done(three["task_id"])
        self.assertEqual(seen["第一问"], [{"role": "user", "content": "第一问"}])
        self.assertEqual(
            [i["content"] for i in seen["第二问"]], ["第一问", "第一问的回答", "第二问"]
        )
        self.assertEqual(
            [i["content"] for i in seen["第三问"]],
            ["第一问", "第一问的回答", "第二问", "第二问的回答", "第三问"],
        )
        self.assertEqual(
            seen["其他会话内容"], [{"role": "user", "content": "其他会话内容"}]
        )
        self.assertEqual(
            {one["session_id"], two["session_id"], three["session_id"]}, {session}
        )
        self.assertEqual(foreign["session_id"], other["session_id"])
        self.assertEqual(len(self.store.list_sessions()), 2)

    async def test_parent_and_request_ids_cannot_cross_sessions(self):
        class Agent:
            def __init__(inner, *args):
                pass

            async def run(inner, message):
                yield "answer", {"content": "结果"}

        self.manager = TaskManager(self.settings, self.store, self.registry, Agent)
        await self.manager.start()
        one = await self.manager.submit("第一问", "request")
        await self.done(one["task_id"])
        self.store.create_session()
        with self.assertRaises(TaskConflict):
            await self.manager.submit("第一问", "request")
        with self.assertRaises(TaskConflict):
            await self.manager.submit("继续", parent_task_id=one["task_id"])
        same = await self.manager.submit(
            "第一问", "request", session_id=one["session_id"]
        )
        self.assertEqual(one["task_id"], same["task_id"])

    async def test_answering_question_creates_new_run_and_keeps_old_answer_immutable(
        self,
    ):
        class Agent:
            def __init__(inner, *args):
                pass

            async def run(inner, message):
                yield (
                    "answer",
                    {
                        "content": "选哪份文件？" if message == "查资料" else "收到",
                        "status": "awaiting_input"
                        if message == "查资料"
                        else "succeeded",
                    },
                )

        self.manager = TaskManager(self.settings, self.store, self.registry, Agent)
        await self.manager.start()
        first = await self.manager.submit("查资料")
        waiting = await self.done(first["task_id"])
        second = await self.manager.provide_input(
            first["task_id"], "report.csv", "answer-request"
        )
        await self.done(second["task_id"])
        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertEqual(first["session_id"], second["session_id"])
        old = self.store.get_task(first["task_id"])
        self.assertEqual(old["display_text"], waiting["display_text"])
        self.assertEqual(old["continued_by_run_id"], second["run_id"])
        unchanged = await self.manager.cancel(first["task_id"])
        self.assertEqual(unchanged, old)
        self.assertEqual(len(self.store.task_history(first["task_id"])), 2)
        self.assertEqual(len(self.store.task_history(second["task_id"])), 2)
        repeated = await self.manager.provide_input(
            first["task_id"], "report.csv", "answer-request"
        )
        self.assertEqual(repeated["task_id"], second["task_id"])
        with self.store._connection() as db:
            db.execute(
                "UPDATE tasks SET updated_at='2000-01-01' WHERE task_id=?",
                (first["task_id"],),
            )
        self.assertEqual(self.store.expire_waiting(), 0)
        with self.assertRaises(TaskConflict):
            await self.manager.provide_input(first["task_id"], "another.csv")


class SessionAPITests(unittest.TestCase):
    def test_sessions_runs_and_restart_use_explicit_active_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings(workspace=root / "workspace", data_dir=root / "data")
            app = create_app(settings)
            with TestClient(app, base_url="http://localhost") as client:
                first = client.get("/api/sessions/current").json()
                self.assertEqual(first["run_count"], 0)
                run = client.post(
                    "/api/tasks", json={"message": "/time", "client_request_id": "one"}
                ).json()
                self.assertEqual(run["session_id"], first["session_id"])
                self.assertEqual(run["run_id"], run["task_id"])
                self.assertEqual(
                    client.get("/api/status").json()["active_session_id"],
                    first["session_id"],
                )
                second = client.post(
                    "/api/sessions", json={"title": "第二段对话"}
                ).json()
                self.assertNotEqual(first["session_id"], second["session_id"])
                self.assertEqual(
                    client.get(
                        "/api/tasks", params={"session_id": second["session_id"]}
                    ).json()["tasks"],
                    [],
                )
                body = client.get(f"/api/sessions/{first['session_id']}/runs").json()
                self.assertEqual(body["runs"][0]["run_id"], run["run_id"])
                self.assertEqual(body["runs"][0]["session_id"], first["session_id"])
                self.assertEqual(
                    client.get("/api/sessions").json()["active_session_id"],
                    second["session_id"],
                )
                self.assertEqual(
                    client.post("/api/sessions/missing/activate").status_code, 404
                )
                self.assertEqual(
                    client.get("/api/sessions/missing/runs").status_code, 404
                )
                self.assertEqual(
                    client.post(
                        "/api/tasks", json={"message": "hello", "session_id": "missing"}
                    ).status_code,
                    409,
                )
                client.post(f"/api/sessions/{first['session_id']}/activate")
                deadline = time.monotonic() + 3
                while (
                    client.get(f"/api/tasks/{run['task_id']}").json()["status"]
                    != "succeeded"
                ):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.005)
                detail = client.get(f"/api/tasks/{run['task_id']}").json()
                self.assertTrue(
                    all(
                        e["session_id"] == first["session_id"]
                        and e["run_id"] == run["run_id"]
                        for e in detail["events"]
                    )
                )
            with TestClient(
                create_app(settings), base_url="http://localhost"
            ) as client:
                self.assertEqual(
                    client.get("/api/sessions/current").json()["session_id"],
                    first["session_id"],
                )
                self.assertEqual(len(client.get("/api/sessions").json()["sessions"]), 2)
                self.assertEqual(
                    client.get("/api/history").json()["messages"][0]["content"], "/time"
                )


if __name__ == "__main__":
    unittest.main()
