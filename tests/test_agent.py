"""Model protocol and failure checks with an in-memory HTTP transport."""

import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from jarvis.agent import Agent, AgentError
from jarvis.config import Settings
from jarvis.storage import Store
from jarvis.tools import ToolRegistry


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.settings = Settings(
            api_key="test-secret",
            workspace=root / "workspace",
            data_dir=root / "data",
            orchestrator="native",
        )
        self.store = Store(self.settings.data_dir)
        self.registry = ToolRegistry(self.settings.workspace, self.store)
        self.agent = Agent(self.settings, self.store, self.registry)
        self.store.add_message("user", "测试任务")

    def tearDown(self):
        self.directory.cleanup()

    async def test_malformed_arguments_return_tool_error_without_executing(self):
        replies = [
            {
                "tool_calls": [
                    {
                        "id": "bad-args",
                        "type": "function",
                        "function": {"name": "write_file", "arguments": "not json"},
                    }
                ]
            },
            {"content": "参数有误，未保存文件。"},
        ]
        with (
            patch.object(self.agent, "_completion", new=AsyncMock(side_effect=replies)),
            patch.object(self.registry, "execute", new=AsyncMock()) as execute,
        ):
            output = [event async for event in self.agent.run("测试任务")]
        execute.assert_not_awaited()
        result = next(
            data["result"] for event, data in output if event == "tool_result"
        )
        self.assertIn("error", result)
        self.assertEqual(output[-1][1]["content"], "参数有误，未保存文件。")

    async def test_loop_limit_stops_with_incomplete_status(self):
        settings = Settings(
            api_key="test",
            workspace=self.settings.workspace,
            data_dir=self.settings.data_dir,
            max_tool_rounds=1,
            orchestrator="native",
        )
        agent = Agent(settings, self.store, self.registry)
        reply = {
            "tool_calls": [
                {
                    "id": "time",
                    "type": "function",
                    "function": {"name": "current_time", "arguments": "{}"},
                }
            ]
        }
        with patch.object(
            agent, "_completion", new=AsyncMock(return_value=reply)
        ) as complete:
            output = [event async for event in agent.run("测试任务")]
        self.assertEqual(complete.await_count, 1)
        self.assertIn("1 轮执行上限", output[-1][1]["content"])
        self.assertIn("当前对话继续剩余工作", output[-1][1]["content"])

    async def test_native_default_allows_more_than_old_24_tools_and_small_limit_stops(
        self,
    ):
        calls = [
            {
                "id": f"clock-{index}",
                "type": "function",
                "function": {"name": "current_time", "arguments": "{}"},
            }
            for index in range(32)
        ]
        replies = [
            {"tool_calls": calls[:16]},
            {"tool_calls": calls[16:]},
            {"content": "完成32次工具调用"},
        ]
        with patch.object(
            self.agent, "_completion", new=AsyncMock(side_effect=replies)
        ) as complete:
            output = [event async for event in self.agent.run("测试任务")]
        self.assertEqual(complete.await_count, 3)
        self.assertEqual(sum(event == "tool_result" for event, _ in output), 32)
        self.assertEqual(output[-1][1]["status"], "succeeded")
        limited = Agent(
            replace(self.settings, max_tool_calls=2), self.store, self.registry
        )
        with patch.object(
            limited,
            "_completion",
            new=AsyncMock(return_value={"tool_calls": calls[:16]}),
        ) as complete:
            output = [event async for event in limited.run("测试任务")]
        self.assertEqual(complete.await_count, 1)
        self.assertEqual(sum(event == "tool_result" for event, _ in output), 2)
        self.assertEqual(output[-1][1]["status"], "partially_succeeded")
        self.assertIn("2/2", output[-1][1]["content"])

    async def test_protocol_uses_configured_model_and_function_tools(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": "你好"}}]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reply = await self.agent._completion(
                client, [{"role": "user", "content": "你好"}]
            )
        self.assertEqual(reply["content"], "你好")
        self.assertEqual(str(seen[0].url), "https://api.openai.com/v1/chat/completions")
        body = json.loads(seen[0].content)
        self.assertEqual(body["model"], self.settings.model)
        self.assertIn(
            "write_file", [tool["function"]["name"] for tool in body["tools"]]
        )

    async def test_provider_errors_are_actionable_and_do_not_echo_secrets(self):
        for code in (401, 404, 429, 500):
            with self.subTest(code=code):
                transport = httpx.MockTransport(
                    lambda request: httpx.Response(code, text="secret-body=test-secret")
                )
                async with httpx.AsyncClient(transport=transport) as client:
                    with self.assertRaises(AgentError) as raised:
                        await self.agent._completion(client, [])
                self.assertIn(str(code), str(raised.exception))
                self.assertNotIn("test-secret", str(raised.exception))

    async def test_invalid_provider_response_is_reported(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"unexpected": True})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with self.assertRaisesRegex(AgentError, "格式不兼容"):
                await self.agent._completion(client, [])

    async def test_native_does_not_request_model_after_task_budget_is_exhausted(self):
        agent = Agent(
            replace(self.settings, max_model_calls=0), self.store, self.registry
        )
        with patch.object(agent, "_completion", new=AsyncMock()) as complete:
            output = [event async for event in agent.run("测试任务")]
        complete.assert_not_awaited()
        self.assertEqual(output[-1][1]["status"], "failed")
        self.assertIn("预算", output[-1][1]["content"])

    async def test_demo_counts_durable_tool_attempt_and_returns_spoken_summary(self):
        task = self.store.create_task("/write report.md | 已完成的报告")
        scoped = self.store.scoped(task["task_id"])
        agent = Agent(replace(self.settings, api_key=""), scoped, self.registry)
        output = [event async for event in agent.run("/write report.md | 已完成的报告")]
        self.assertTrue((self.settings.workspace / "report.md").exists())
        self.assertEqual(self.store.get_task(task["task_id"])["tool_calls"], 1)
        self.assertIn("文档已保存", output[-1][1]["speech_text"])
        usage = next(data for event, data in output if event == "usage")
        self.assertTrue(usage["persisted"])

    async def test_demo_does_not_write_after_tool_budget_is_exhausted(self):
        agent = Agent(
            replace(self.settings, api_key="", max_tool_calls=0),
            self.store,
            self.registry,
        )
        output = [event async for event in agent.run("/write no.md | forbidden")]
        self.assertFalse((self.settings.workspace / "no.md").exists())
        self.assertEqual(output[-1][1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
