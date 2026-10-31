"""Real MAF agents talking to a local OpenAI-compatible test HTTP server."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from jarvis.config import Settings
from jarvis.orchestration import run_maf, _Budget, _parse_final
from jarvis.storage import Store
from jarvis.tools import ToolRegistry
from jarvis.tasks import TaskManager


def final(text="报告已保存。", status="succeeded"):
    return {
        "content": json.dumps(
            {"status": status, "display_text": text, "speech_text": text},
            ensure_ascii=False,
        )
    }


def call(name, arguments, ident="call-1"):
    return {
        "content": None,
        "tool_calls": [
            {
                "id": ident,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        ],
    }


class ModelServer:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.failure = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                outer.requests.append((self.path, request))
                if not outer.replies:
                    self.send_response(500)
                    self.end_headers()
                    return
                reply = outer.replies.pop(0)
                if isinstance(reply, int):
                    self.send_response(reply)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(
                        b'{"error":{"message":"SECRET provider failure","type":"test"}}'
                    )
                    return
                self.send_response(200)
                if request.get("stream"):
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    delta = {"role": "assistant", **reply}
                    if "tool_calls" in delta:
                        delta["tool_calls"] = [
                            {"index": i, **tool}
                            for i, tool in enumerate(delta["tool_calls"])
                        ]
                    chunks = [delta, {}]
                    for i, chunk in enumerate(chunks):
                        payload = {
                            "id": "chat-test",
                            "object": "chat.completion.chunk",
                            "created": 123,
                            "model": "test-model",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": chunk,
                                    "finish_reason": None
                                    if i == 0
                                    else (
                                        "tool_calls"
                                        if reply.get("tool_calls")
                                        else "stop"
                                    ),
                                }
                            ],
                        }
                        self.wfile.write(
                            (
                                "data: "
                                + json.dumps(payload, ensure_ascii=False)
                                + "\n\n"
                            ).encode()
                        )
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    payload = {
                        "id": "chat-test",
                        "object": "chat.completion",
                        "created": 123,
                        "model": "test-model",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", **reply},
                                "finish_reason": "tool_calls"
                                if reply.get("tool_calls")
                                else "stop",
                            }
                        ],
                    }
                    self.wfile.write(json.dumps(payload, ensure_ascii=False).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class MAFIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        try:
            import agent_framework  # noqa: F401
            import agent_framework.openai  # noqa: F401
        except ImportError:
            self.skipTest(
                "Install requirements-agent.txt to run real MAF integration tests"
            )
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(self.root / "data")
        self.registry = ToolRegistry(self.root / "workspace", self.store)
        self.store.add_message("user", "写一份报告，保存到 reports/test.md")

    def setup_model(self, replies, **overrides):
        server = ModelServer(replies)
        self.addCleanup(server.close)
        values = dict(
            Settings(
                api_key="local-test-key",
                base_url=server.url,
                model="test-model",
                workspace=self.root / "workspace",
                data_dir=self.root / "data",
            ).__dict__
        )
        values.update(overrides)
        return server, SimpleNamespace(**values)

    async def test_real_maf_delegates_writer_and_persists_actual_document(self):
        server, settings = self.setup_model(
            [
                call(
                    "delegate_writer",
                    {"task": "保存 reports/test.md，内容：# 真正的报告"},
                ),
                call(
                    "write_file",
                    {"path": "reports/test.md", "content": "# 真正的报告"},
                    "write-1",
                ),
                {"content": "已保存 reports/test.md。"},
                final(),
            ]
        )
        events = [
            event
            async for event in run_maf(
                settings,
                self.store,
                self.registry,
                "写一份报告，保存到 reports/test.md",
            )
        ]
        self.assertEqual(
            (self.root / "workspace/reports/test.md").read_text(), "# 真正的报告"
        )
        self.assertEqual(len(server.requests), 4)
        self.assertTrue(
            all(path == "/v1/chat/completions" for path, _ in server.requests)
        )
        first_tools = {t["function"]["name"] for t in server.requests[0][1]["tools"]}
        self.assertIn("delegate_writer", first_tools)
        self.assertNotIn("write_file", first_tools)
        writer_tools = {t["function"]["name"] for t in server.requests[1][1]["tools"]}
        self.assertEqual(writer_tools, {"write_file", "read_file", "list_files"})
        self.assertTrue(server.requests[1][1]["stream"])
        self.assertIn(
            (
                "tool_start",
                {
                    "name": "write_file",
                    "arguments": {"path": "reports/test.md", "content": "# 真正的报告"},
                },
            ),
            events,
        )
        self.assertEqual(events[-1][1]["status"], "succeeded")
        self.assertEqual(events[-1][1]["usage"], {"model_calls": 4, "tool_calls": 1})

    async def test_budget_covers_nested_agent_requests_and_preserves_completed_write(
        self,
    ):
        server, settings = self.setup_model(
            [
                call("delegate_writer", {"task": "保存报告"}),
                call("write_file", {"path": "saved.md", "content": "already saved"}),
            ],
            max_model_calls=2,
        )
        events = [
            event
            async for event in run_maf(settings, self.store, self.registry, "保存报告")
        ]
        self.assertEqual(len(server.requests), 2)
        self.assertTrue((self.root / "workspace/saved.md").exists())
        self.assertIn("预算", events[-1][1]["content"])
        self.assertEqual(events[-1][1]["status"], "partially_succeeded")

    async def test_awaiting_input_uses_structured_final_protocol(self):
        server, settings = self.setup_model(
            [final("要分析哪个文件？", "awaiting_input")]
        )
        events = [
            event
            async for event in run_maf(settings, self.store, self.registry, "分析一下")
        ]
        self.assertTrue(events[-1][1]["awaiting_input"])
        self.assertEqual(events[-1][1]["speech_text"], "要分析哪个文件？")

    async def test_failed_tool_cannot_be_reported_as_total_success(self):
        server, settings = self.setup_model(
            [
                call("delegate_analyst", {"task": "分析 missing.csv"}),
                call("analyze_csv", {"path": "missing.csv"}),
                {"content": "没有找到文件。"},
                final("全部完成。"),
            ]
        )
        events = [
            event
            async for event in run_maf(
                settings, self.store, self.registry, "分析 missing.csv"
            )
        ]
        self.assertEqual(events[-1][1]["status"], "failed")
        self.assertIn("失败", events[-1][1]["speech_text"])

    async def test_provider_failure_is_visible_without_secret_or_sdk_retry(self):
        server, settings = self.setup_model([401])
        events = [
            event
            async for event in run_maf(settings, self.store, self.registry, "hello")
        ]
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(events[-1][1]["status"], "failed")
        self.assertIn("401", events[-1][1]["content"])
        self.assertNotIn("SECRET", events[-1][1]["content"])

    async def test_cancelled_real_maf_write_is_durably_audited(self):
        server, _ = self.setup_model(
            [
                call("delegate_writer", {"task": "保存报告"}),
                call(
                    "write_file",
                    {"path": "cancelled-write.md", "content": "completed write"},
                ),
            ]
        )
        settings = Settings(
            api_key="local-test-key",
            base_url=server.url,
            model="test-model",
            workspace=self.root / "workspace",
            data_dir=self.root / "data",
        )
        started, release = asyncio.Event(), asyncio.Event()

        class SlowRegistry(ToolRegistry):
            async def execute(self, name, arguments):
                if name == "write_file":
                    started.set()
                    await release.wait()
                return await super().execute(name, arguments)

        manager = TaskManager(
            settings, self.store, SlowRegistry(settings.workspace, self.store)
        )
        await manager.start()
        try:
            task = await manager.submit("保存报告")
            await asyncio.wait_for(started.wait(), 10)
            await manager.cancel(task["task_id"])
            release.set()
            async with asyncio.timeout(10):
                while self.store.get_task(task["task_id"])["status"] != "cancelled":
                    await asyncio.sleep(0.01)
            self.assertEqual(
                (settings.workspace / "cancelled-write.md").read_text(),
                "completed write",
            )
            artifacts = self.store.list_artifacts(task["task_id"])
            self.assertEqual(
                [item["path"] for item in artifacts], ["cancelled-write.md"]
            )
            steps = self.store.list_steps(task["task_id"])
            writes = [step for step in steps if step["name"] == "write_file"]
            self.assertEqual(len(writes), 1)
            self.assertEqual(writes[0]["status"], "succeeded")
            persisted = self.store.get_task(task["task_id"])
            self.assertEqual(persisted["model_calls"], 2)
            self.assertEqual(persisted["tool_calls"], 1)
        finally:
            release.set()
            await manager.close()

    async def test_persisted_model_budget_spans_awaiting_input_turns(self):
        server, _ = self.setup_model([final("需要哪个文件？", "awaiting_input")])
        settings = Settings(
            api_key="local-test-key",
            base_url=server.url,
            model="test-model",
            workspace=self.root / "workspace",
            data_dir=self.root / "data",
            max_model_calls=1,
        )
        manager = TaskManager(settings, self.store, self.registry)
        await manager.start()
        try:
            task = await manager.submit("分析一下")
            async with asyncio.timeout(10):
                while (
                    self.store.get_task(task["task_id"])["status"] != "awaiting_input"
                ):
                    await asyncio.sleep(0.01)
            self.assertEqual(self.store.get_task(task["task_id"])["model_calls"], 1)
            await manager.provide_input(task["task_id"], "example.csv")
            async with asyncio.timeout(10):
                while self.store.get_task(task["task_id"])["status"] not in {
                    "failed",
                    "partially_succeeded",
                }:
                    await asyncio.sleep(0.01)
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(self.store.get_task(task["task_id"])["model_calls"], 1)
        finally:
            await manager.close()

    def test_unstructured_final_does_not_claim_success(self):
        result = _parse_final("我都做好了。", _Budget())
        self.assertEqual(result["status"], "failed")
        self.assertIn("尚不能确认", result["display_text"])


if __name__ == "__main__":
    unittest.main()
