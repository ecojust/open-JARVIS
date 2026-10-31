"""End-to-end local API checks; no model credentials or external network required."""

from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from jarvis.agent import Agent, AgentError
from jarvis.app import MAX_UPLOAD_BYTES, create_app
from jarvis.config import Settings


def events(response):
    result = []
    for frame in response.text.split("\n\n"):
        if not frame.strip():
            continue
        lines = frame.splitlines()
        event = next(line[7:] for line in lines if line.startswith("event: "))
        data = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
        result.append((event, data))
    return result


class AppTests(unittest.TestCase):
    @contextmanager
    def app_client(self, **overrides):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            overrides.setdefault("orchestrator", "native")
            settings = Settings(
                workspace=root / "workspace", data_dir=root / "data", **overrides
            )
            app = create_app(settings)
            with TestClient(app, base_url="http://localhost") as client:
                yield client, app, settings

    def test_web_entrypoint_and_referenced_static_assets_are_served(self):
        with self.app_client() as (client, _, _):
            page = client.get("/")
            self.assertEqual(page.status_code, 200)
            self.assertIn("text/html", page.headers["content-type"])
            self.assertIn("/static/app.js", page.text)
            script = client.get("/static/app.js")
            self.assertEqual(script.status_code, 200)
            self.assertIn("javascript", script.headers["content-type"])
            self.assertGreater(len(script.content), 100)

    def test_demo_creates_real_report_and_clear_preserves_artifacts(self):
        with self.app_client() as (client, app, settings):
            response = client.post(
                "/api/chat",
                json={"message": "/write reports/周报.md | # 周报\n完成数据整理。"},
            )
            self.assertEqual(response.status_code, 200)
            output = events(response)
            self.assertEqual(
                [event for event, _ in output],
                ["status", "tool_start", "tool_result", "answer", "done"],
            )
            self.assertNotIn("error", output[2][1]["result"])
            artifact = settings.workspace / "reports/周报.md"
            self.assertEqual(artifact.read_text(), "# 周报\n完成数据整理。")
            download = client.get("/api/files", params={"path": "reports/周报.md"})
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.content, artifact.read_bytes())
            self.assertIn("attachment", download.headers["content-disposition"])
            history = client.get("/api/history").json()["messages"]
            self.assertEqual([m["role"] for m in history], ["user", "assistant"])
            self.assertEqual(client.delete("/api/history").status_code, 200)
            self.assertEqual(client.get("/api/history").json()["messages"], [])
            self.assertTrue(artifact.exists())
            self.assertFalse(app.state.busy)

    def test_model_executes_tool_and_receives_result_before_final_answer(self):
        replies = [
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_report",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {"path": "report.md", "content": "# 真实报告"}
                            ),
                        },
                    }
                ],
            },
            {"content": "已保存 report.md。"},
        ]
        seen_messages = []

        async def complete(_client, messages):
            seen_messages.append(json.loads(json.dumps(messages)))
            return replies.pop(0)

        with self.app_client(api_key="test-key-secret") as (client, _, settings):
            with patch.object(Agent, "_completion", side_effect=complete):
                response = client.post("/api/chat", json={"message": "请写一份报告"})
            output = events(response)
            self.assertFalse(any(event == "error" for event, _ in output), output)
            self.assertEqual(
                (settings.workspace / "report.md").read_text(), "# 真实报告"
            )
            self.assertEqual(seen_messages[0][-1]["content"], "请写一份报告")
            tool_reply = seen_messages[1][-1]
            self.assertEqual(tool_reply["role"], "tool")
            self.assertEqual(tool_reply["tool_call_id"], "call_report")
            self.assertNotIn("error", json.loads(tool_reply["content"]))
            self.assertEqual(
                [data["content"] for event, data in output if event == "answer"],
                ["已保存 report.md。"],
            )

    def test_upload_analysis_download_and_duplicate_protection(self):
        csv = "product,revenue\nA,10\nB,20\n".encode()
        with self.app_client() as (client, _, settings):
            response = client.post(
                "/api/files", params={"path": "sales.csv"}, content=csv
            )
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json(), {"path": "sales.csv", "size": len(csv)})
            duplicate = client.post(
                "/api/files", params={"path": "sales.csv"}, content=b"changed"
            )
            self.assertEqual(duplicate.status_code, 409)
            self.assertEqual((settings.workspace / "sales.csv").read_bytes(), csv)
            output = events(
                client.post("/api/chat", json={"message": "/analyze sales.csv"})
            )
            results = [
                data["result"] for event, data in output if event == "tool_result"
            ]
            self.assertEqual(len(results), 1)
            self.assertNotIn("error", results[0])
            self.assertIn("revenue", json.dumps(results[0]))
            self.assertEqual(
                client.get("/api/files", params={"path": "sales.csv"}).content, csv
            )

    def test_upload_rejects_escape_unsupported_binary_and_oversized_files(self):
        with self.app_client() as (client, _, settings):
            for path in ("../escape.txt", "/tmp/escape.txt"):
                with self.subTest(path=path):
                    self.assertEqual(
                        client.post(
                            "/api/files", params={"path": path}, content=b"x"
                        ).status_code,
                        400,
                    )
            self.assertFalse((settings.workspace.parent / "escape.txt").exists())
            self.assertEqual(
                client.post(
                    "/api/files", params={"path": "script.py"}, content=b"x"
                ).status_code,
                400,
            )
            self.assertEqual(
                client.post(
                    "/api/files", params={"path": "binary.txt"}, content=b"\xff\xfe"
                ).status_code,
                400,
            )
            self.assertEqual(
                client.post(
                    "/api/files",
                    params={"path": "big.csv"},
                    content=b"x" * (MAX_UPLOAD_BYTES + 1),
                ).status_code,
                413,
            )
            self.assertFalse((settings.workspace / "big.csv").exists())
            self.assertEqual(
                client.get(
                    "/api/files", params={"path": "../data/private.txt"}
                ).status_code,
                404,
            )

    def test_foreign_origins_hosts_and_invalid_messages_are_rejected(self):
        with self.app_client() as (client, app, _):
            self.assertEqual(
                client.post(
                    "/api/chat",
                    json={"message": "/time"},
                    headers={"Origin": "https://untrusted.example"},
                ).status_code,
                403,
            )
            self.assertEqual(
                client.delete(
                    "/api/history", headers={"Sec-Fetch-Site": "cross-site"}
                ).status_code,
                403,
            )
            self.assertEqual(
                client.get(
                    "/api/status", headers={"Host": "untrusted.example"}
                ).status_code,
                400,
            )
            for message in ("", "   ", "x" * 12001):
                self.assertEqual(
                    client.post("/api/chat", json={"message": message}).status_code, 422
                )
            app.state.busy = True
            self.assertEqual(
                client.post("/api/chat", json={"message": "/time"}).status_code, 409
            )
            self.assertEqual(client.delete("/api/history").status_code, 409)
            app.state.busy = False
            self.assertEqual(client.get("/api/history").json()["messages"], [])

    def test_model_failure_is_visible_and_releases_busy_state(self):
        with self.app_client(api_key="secret-value") as (client, app, _):
            with patch.object(
                Agent,
                "_completion",
                new=AsyncMock(side_effect=AgentError("模型接口响应超时")),
            ):
                response = client.post("/api/chat", json={"message": "生成报告"})
            output = events(response)
            self.assertEqual(
                [data["message"] for event, data in output if event == "error"],
                ["模型接口响应超时"],
            )
            self.assertFalse(any(event == "answer" for event, _ in output))
            self.assertEqual(output[-1][0], "done")
            self.assertFalse(app.state.busy)
            self.assertEqual(
                client.get("/api/history").json()["messages"][-1]["content"],
                "模型接口响应超时",
            )

    def test_status_is_honest_and_does_not_leak_keys(self):
        with self.app_client() as (client, _, _):
            status = client.get("/api/status").json()
            self.assertFalse(status["configured"])
            output = events(
                client.post("/api/chat", json={"message": "给我查资料并写报告"})
            )
            self.assertFalse(any(event == "tool_start" for event, _ in output))
            answer = next(
                data["content"] for event, data in output if event == "answer"
            )
            self.assertIn("演示模式", answer)
        with self.app_client(
            api_key="private-model-secret", brave_api_key="private-search-secret"
        ) as (client, _, _):
            response = client.get("/api/status")
            self.assertTrue(response.json()["configured"])
            self.assertNotIn("private-model-secret", response.text)
            self.assertNotIn("private-search-secret", response.text)

    def wait_for_task(self, client, task_id, headers=None):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            response = client.get("/api/tasks/" + task_id, headers=headers)
            self.assertEqual(response.status_code, 200)
            detail = response.json()
            if detail["status"] in {
                "succeeded",
                "partially_succeeded",
                "failed",
                "cancelled",
                "interrupted",
                "awaiting_input",
            }:
                return detail
            time.sleep(0.01)
        self.fail("task did not reach a stable state")

    def test_native_task_api_persists_and_downloads_real_artifact(self):
        with self.app_client() as (client, app, settings):
            payload = {
                "message": "/write native-report.md | # Native report",
                "client_request_id": "desktop-123",
            }
            response = client.post("/api/tasks", json=payload)
            self.assertEqual(response.status_code, 202)
            task_id = response.json()["task_id"]
            repeated = client.post("/api/tasks", json=payload)
            self.assertEqual(repeated.json()["task_id"], task_id)
            mismatch = client.post(
                "/api/tasks", json={**payload, "message": "different"}
            )
            self.assertEqual(mismatch.status_code, 409)
            detail = self.wait_for_task(client, task_id)
            self.assertEqual(detail["status"], "succeeded")
            self.assertTrue(detail["display_text"])
            self.assertTrue(detail["speech_text"])
            self.assertEqual(detail["tool_calls"], 1)
            self.assertEqual([step["name"] for step in detail["steps"]], ["write_file"])
            artifact = detail["artifacts"][0]
            downloaded = client.get("/api/artifacts/" + artifact["artifact_id"])
            self.assertEqual(downloaded.status_code, 200)
            self.assertEqual(downloaded.text, "# Native report")
            listed = client.get("/api/tasks").json()["tasks"]
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["task_id"], task_id)
            self.assertNotIn("display_text", listed[0])
            event_ids = [event["event_id"] for event in detail["events"]]
            self.assertEqual(event_ids, sorted(set(event_ids)))
            completed = next(
                event for event in detail["events"] if event["type"] == "task.completed"
            )
            self.assertEqual(
                completed["data"]["artifacts"][0]["artifact_id"],
                artifact["artifact_id"],
            )
            self.assertEqual(
                client.get("/api/health").json()["last_event_id"],
                app.state.store.last_event_id(),
            )

    def test_instance_auth_protects_every_api_except_readiness(self):
        with self.app_client(
            instance_token="private-instance-token", instance_id="instance-123"
        ) as (client, _, _):
            health = client.get("/api/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["instance_id"], "instance-123")
            self.assertNotIn("private-instance-token", health.text)
            for path in [
                "/api/status",
                "/api/tasks",
                "/api/events",
                "/api/files?path=example-sales.csv",
                "/api/history",
            ]:
                self.assertEqual(client.get(path).status_code, 401, path)
            self.assertEqual(
                client.post("/api/tasks", json={"message": "/time"}).status_code, 401
            )
            headers = {"Authorization": "Bearer private-instance-token"}
            self.assertEqual(
                client.get("/api/status", headers=headers).status_code, 200
            )
            response = client.post(
                "/api/tasks", json={"message": "/time"}, headers=headers
            )
            self.assertEqual(response.status_code, 202)
            self.assertEqual(
                self.wait_for_task(client, response.json()["task_id"], headers)[
                    "status"
                ],
                "succeeded",
            )
            self.assertEqual(
                client.post(
                    "/api/tasks",
                    json={"message": "/time"},
                    headers={**headers, "Origin": "https://evil.example"},
                ).status_code,
                403,
            )

    def test_new_task_routes_validate_states_and_missing_ids(self):
        with self.app_client() as (client, _, _):
            self.assertEqual(client.get("/api/tasks/unknown").status_code, 404)
            self.assertEqual(client.post("/api/tasks/unknown/cancel").status_code, 404)
            self.assertEqual(client.get("/api/artifacts/unknown").status_code, 404)
            for message in ["", "  ", "x" * 12001]:
                self.assertEqual(
                    client.post("/api/tasks", json={"message": message}).status_code,
                    422,
                )
            task_id = client.post("/api/tasks", json={"message": "/time"}).json()[
                "task_id"
            ]
            self.wait_for_task(client, task_id)
            self.assertEqual(
                client.post(
                    "/api/tasks/" + task_id + "/input", json={"message": "more"}
                ).status_code,
                409,
            )
            self.assertEqual(
                client.post("/api/tasks/" + task_id + "/resume").status_code, 409
            )
            self.assertEqual(
                client.post("/api/tasks/" + task_id + "/cancel").json()["status"],
                "succeeded",
            )


if __name__ == "__main__":
    unittest.main()
