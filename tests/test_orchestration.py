"""Real MAF agents talking to a local OpenAI-compatible test HTTP server."""

import asyncio
import builtins
from contextlib import contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from jarvis.agent import Agent as NativeAgent
from jarvis.config import Settings
from jarvis.orchestration import OrchestrationError, run_maf, _Budget, _parse_final
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
    def __init__(self, replies, *, require_disabled=False):
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
                if require_disabled and request.get("thinking") != {"type": "disabled"}:
                    reply = 400
                elif not outer.replies:
                    self.send_response(500)
                    self.end_headers()
                    return
                else:
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


@contextmanager
def route_official_deepseek_to(server):
    """Exercise real SDK and HTTP requests without contacting the public API."""
    original_client = httpx.AsyncClient

    async def route(request):
        if request.url.host != "api.deepseek.com":
            raise AssertionError("Unexpected outbound host in DeepSeek protocol test")
        request.url = httpx.URL(server.url).copy_with(path=request.url.path)
        request.headers["Host"] = request.url.netloc.decode()

    class LocalClient(original_client):
        def __init__(self, *args, **kwargs):
            hooks = dict(kwargs.pop("event_hooks", {}))
            hooks["request"] = [*hooks.get("request", []), route]
            kwargs.update(event_hooks=hooks, trust_env=False)
            super().__init__(*args, **kwargs)

    with patch("httpx.AsyncClient", LocalClient):
        yield


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

    async def test_missing_bundled_provider_reports_app_repair_not_pip_commands(self):
        original_import = builtins.__import__

        def missing_provider(name, *args, **kwargs):
            if name == "agent_framework_openai":
                raise ModuleNotFoundError("provider missing", name=name)
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=missing_provider):
            with self.assertRaises(OrchestrationError) as raised:
                _ = [event async for event in run_maf(None, None, None, "你好")]
        self.assertIn("应用内置任务组件加载失败", str(raised.exception))
        self.assertNotIn("pip", str(raised.exception))
        self.assertNotIn("requirements", str(raised.exception))
        self.assertEqual(raised.exception.__cause__.name, "agent_framework_openai")

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
        self.assertEqual(
            writer_tools,
            {
                "write_file",
                "read_file",
                "list_files",
                "get_file_preview",
                "create_document",
                "create_spreadsheet",
                "create_presentation",
            },
        )
        self.assertNotIn("create_explainer_video", writer_tools)
        self.assertNotIn("create_schedule", writer_tools)
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
        self.assertTrue(all("thinking" not in body for _, body in server.requests))

    async def test_deepseek_disables_thinking_for_coordinator_and_writer_tool_rounds(
        self,
    ):
        server = ModelServer(
            [
                call("delegate_writer", {"task": "保存 reports/deepseek.md"}),
                call(
                    "write_file",
                    {"path": "reports/deepseek.md", "content": "# 实际工具结果"},
                    "write-1",
                ),
                {"content": "已保存 reports/deepseek.md。"},
                final(),
            ],
            require_disabled=True,
        )
        self.addCleanup(server.close)
        settings = Settings(
            api_key="local-test-key",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-flash",
            workspace=self.root / "workspace",
            data_dir=self.root / "data",
        )
        with route_official_deepseek_to(server):
            events = [
                event
                async for event in run_maf(
                    settings, self.store, self.registry, "保存报告"
                )
            ]
        self.assertEqual(events[-1][1]["status"], "succeeded", events[-1])
        self.assertEqual(
            (self.root / "workspace/reports/deepseek.md").read_text(),
            "# 实际工具结果",
        )
        self.assertEqual(len(server.requests), 4)
        bodies = [body for _, body in server.requests]
        self.assertTrue(all(b["thinking"] == {"type": "disabled"} for b in bodies))
        self.assertTrue(bodies[1]["stream"])
        self.assertTrue(bodies[2]["stream"])
        self.assertFalse(bodies[0].get("stream", False))
        for index, tool_id in ((2, "write-1"), (3, "call-1")):
            self.assertTrue(
                any(
                    message.get("role") == "tool"
                    and message.get("tool_call_id") == tool_id
                    for message in bodies[index]["messages"]
                )
            )

    async def test_maf_accepts_nested_office_schema_and_creates_real_presentation(self):
        arguments = {
            "path": "reports/actual.pptx",
            "title": "结构化资料",
            "slides": [
                {
                    "title": "实际工具写入",
                    "bullets": ["数值来自输入资料"],
                    "notes": "这是用于验证工具协议的明确测试资料。",
                    "sources": [
                        {"title": "协议测试来源", "url": "https://example.com/source"}
                    ],
                }
            ],
            "sources": [{"title": "协议测试来源", "url": "https://example.com/source"}],
        }
        server, settings = self.setup_model(
            [
                call("delegate_writer", {"task": "生成真正的PPT"}),
                call("create_presentation", arguments),
                {"content": "已生成 reports/actual.pptx。"},
                final("PPT已生成。"),
            ]
        )
        events = [
            event
            async for event in run_maf(
                settings, self.store, self.registry, "生成真正的PPT"
            )
        ]
        from pptx import Presentation

        actual = Presentation(self.root / "workspace/reports/actual.pptx")
        self.assertEqual(actual.slides[0].shapes.title.text, "实际工具写入")
        self.assertIn(
            "明确测试资料", actual.slides[0].notes_slide.notes_text_frame.text
        )
        self.assertEqual(events[-1][1]["status"], "succeeded")
        result = next(
            data["result"]
            for kind, data in events
            if kind == "tool_result" and data["name"] == "create_presentation"
        )
        self.assertTrue(result["saved"])
        self.assertTrue(any(item.get("hidden") for item in result["artifacts"]))
        self.assertEqual(len(server.requests), 4)

    async def test_media_delegation_has_only_presentation_and_video_tools(self):
        server, settings = self.setup_model(
            [
                call("delegate_media", {"task": "检查可用PPT"}),
                call("list_files", {}),
                {"content": "需要先生成PPT。"},
                final("请先生成PPT。", "awaiting_input"),
            ]
        )
        events = [
            event
            async for event in run_maf(
                settings, self.store, self.registry, "将PPT制作成视频"
            )
        ]
        tools = {tool["function"]["name"] for tool in server.requests[1][1]["tools"]}
        self.assertEqual(
            tools, {"list_files", "get_file_preview", "create_explainer_video"}
        )
        self.assertEqual(events[-1][1]["status"], "awaiting_input")

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

    async def test_real_maf_provider_receives_1000_tool_configuration_without_old_clamps(
        self,
    ):
        from agent_framework_openai import OpenAIChatCompletionClient
        from jarvis.orchestration import BudgetExceeded

        budgets, provider_configs = [], []
        original_budget = _Budget

        def budget(**kwargs):
            budgets.append(kwargs)
            return original_budget(**kwargs)

        def provider(**kwargs):
            provider_configs.append(kwargs["function_invocation_configuration"])
            return OpenAIChatCompletionClient(**kwargs)

        server, settings = self.setup_model(
            [final("已完成配置检查")],
            max_tool_calls=1000,
            max_tool_rounds=1001,
            max_model_calls=3001,
        )
        with (
            patch("jarvis.orchestration._Budget", side_effect=budget),
            patch(
                "agent_framework_openai.OpenAIChatCompletionClient",
                side_effect=provider,
            ),
        ):
            output = [
                event
                async for event in run_maf(
                    settings, self.store, self.registry, "检查配置"
                )
            ]
        self.assertEqual(output[-1][1]["status"], "succeeded")
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(budgets, [{"tool_limit": 1000, "model_limit": 3001}])
        self.assertEqual(provider_configs[0]["max_function_calls"], 1000)
        self.assertEqual(provider_configs[0]["max_iterations"], 1001)
        small = original_budget(tool_limit=2)
        await small.tool()
        await small.tool()
        with self.assertRaises(BudgetExceeded):
            await small.tool()
        self.assertEqual(small.tool_calls, 2)
        self.assertIn("2/2", small.exceeded)
        self.assertIn("当前对话", small.exceeded)

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

    async def test_followup_is_a_new_run_with_its_own_budget_and_shared_session(self):
        server, _ = self.setup_model(
            [final("需要哪个文件？", "awaiting_input"), final("已收到文件名。")]
        )
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
            followup = await manager.provide_input(task["task_id"], "example.csv")
            self.assertNotEqual(followup["run_id"], task["run_id"])
            self.assertEqual(followup["session_id"], task["session_id"])
            async with asyncio.timeout(10):
                while self.store.get_task(followup["task_id"])["status"] != "succeeded":
                    await asyncio.sleep(0.01)
            self.assertEqual(len(server.requests), 2)
            previous = self.store.get_task(task["task_id"])
            self.assertEqual(previous["model_calls"], 1)
            self.assertEqual(previous["continued_by_run_id"], followup["run_id"])
            self.assertEqual(self.store.get_task(followup["task_id"])["model_calls"], 1)
            messages = json.dumps(server.requests[1][1]["messages"], ensure_ascii=False)
            self.assertIn("需要哪个文件", messages)
            self.assertIn("example.csv", messages)
        finally:
            await manager.close()

    def test_unstructured_final_does_not_claim_success(self):
        result = _parse_final("我都做好了。", _Budget())
        self.assertEqual(result["status"], "failed")
        self.assertIn("尚不能确认", result["display_text"])


class NativeProviderIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_native_tool_roundtrip_on_deepseek_and_unmodified_proxy(self):
        for official in (True, False):
            with self.subTest(official=official), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                server = ModelServer(
                    [
                        call(
                            "write_file",
                            {"path": "report.md", "content": "# Native 实际结果"},
                        ),
                        {"content": "报告已保存。"},
                    ],
                    require_disabled=official,
                )
                self.addCleanup(server.close)
                settings = Settings(
                    api_key="local-test-key",
                    base_url="https://api.deepseek.com" if official else server.url,
                    model="deepseek-flash",
                    orchestrator="native",
                    workspace=root / "workspace",
                    data_dir=root / "data",
                )
                store = Store(settings.data_dir)
                store.add_message("user", "保存报告")
                agent = NativeAgent(
                    settings, store, ToolRegistry(settings.workspace, store)
                )
                route = (
                    route_official_deepseek_to(server) if official else nullcontext()
                )
                with route:
                    events = [event async for event in agent.run("保存报告")]
                self.assertEqual(events[-1][1]["status"], "succeeded", events[-1])
                self.assertEqual(
                    (settings.workspace / "report.md").read_text(), "# Native 实际结果"
                )
                self.assertEqual(len(server.requests), 2)
                for _, body in server.requests:
                    if official:
                        self.assertEqual(body["thinking"], {"type": "disabled"})
                    else:
                        self.assertNotIn("thinking", body)
                messages = server.requests[1][1]["messages"]
                self.assertEqual(messages[-1]["role"], "tool")
                self.assertEqual(messages[-1]["tool_call_id"], "call-1")
                self.assertNotIn("error", json.loads(messages[-1]["content"]))

    async def test_local_deepseek_simulation_rejects_missing_thinking_option(self):
        server = ModelServer([{"content": "accepted"}], require_disabled=True)
        self.addCleanup(server.close)
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(f"{server.url}/chat/completions", json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(server.replies), 1)


if __name__ == "__main__":
    unittest.main()
