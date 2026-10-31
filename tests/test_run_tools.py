"""Paginated run audit history with synthetic large persisted tool outputs."""

import json
from pathlib import Path
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from jarvis.app import create_app
from jarvis.config import Settings


class RunToolHistoryTests(unittest.TestCase):
    def test_large_run_paginates_without_payloads_and_late_failure_is_not_hidden(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings(workspace=root / "workspace", data_dir=root / "data")
            expected_ids = []

            class AuditFixtureAgent:
                def __init__(inner, settings, store, registry):
                    inner.store = store

                async def run(inner, message):
                    store, task_id = inner.store._store, inner.store.task_id
                    # Simulate 1,000 actual tool records plus delegation records;
                    # no model calls or tools execute in this persistence test.
                    stamp = store._now()
                    rows = []
                    for index in range(1205):
                        step_id = f"step_{1205 - index:04d}"
                        expected_ids.append(step_id)
                        rows.append(
                            (
                                step_id,
                                task_id,
                                "current_time" if index < 1000 else "delegate_research",
                                json.dumps({"input": "x" * 1000}),
                                json.dumps({"content": "y" * 1000}),
                                "failed" if index == 1199 else "succeeded",
                                stamp,
                                stamp,
                            )
                        )
                    with store._connection() as db:
                        db.executemany(
                            "INSERT INTO steps VALUES (?,?,?,?,?,?,?,?)", rows
                        )
                    store.add_event(
                        task_id,
                        "tool.completed",
                        {
                            "name": "fetch_page",
                            "result": {"content": "z" * (3 * 1024 * 1024)},
                        },
                    )
                    yield "answer", {"content": "完成", "status": "succeeded"}

            app = create_app(settings, agent_factory=AuditFixtureAgent)
            with TestClient(app, base_url="http://localhost") as client:
                run = client.post(
                    "/api/tasks", json={"message": "生成测试调用记录"}
                ).json()
                task_id = run["task_id"]
                deadline = time.monotonic() + 5
                while app.state.store.get_task(task_id)["status"] not in {
                    "partially_succeeded",
                    "failed",
                    "succeeded",
                }:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.005)
                response = client.get(f"/api/tasks/{task_id}?view=summary")
                detail = response.json()
                self.assertEqual(detail["status"], "partially_succeeded")
                self.assertEqual(detail["steps_total"], 1205)
                self.assertEqual(len(detail["steps"]), 100)
                self.assertTrue(detail["steps_has_more"])
                self.assertLess(len(response.content), 200_000)
                self.assertTrue(
                    all(
                        "arguments" not in row and "result" not in row
                        for row in detail["steps"]
                    )
                )
                self.assertNotIn("z" * 1000, response.text)
                terminal = next(
                    event
                    for event in app.state.store.recent_events(task_id)
                    if event["type"] == "task.completed"
                )
                self.assertEqual(len(terminal["data"]["completed_steps"]), 1204)
                self.assertEqual(
                    terminal["data"]["unfinished_steps"], [expected_ids[1199]]
                )
                collected = []
                for offset in range(0, 1205, 100):
                    page = client.get(
                        f"/api/tasks/{task_id}/steps",
                        params={"offset": offset, "limit": 100},
                    ).json()
                    self.assertEqual(page["total"], 1205)
                    self.assertEqual(page["session_id"], run["session_id"])
                    self.assertGreaterEqual(
                        page["snapshot_event_id"], detail["steps_event_id"]
                    )
                    collected.extend(row["step_id"] for row in page["steps"])
                self.assertEqual(collected, expected_ids)
                self.assertFalse(page["has_more"])
                last = client.get(
                    f"/api/tasks/{task_id}/steps/{expected_ids[-1]}"
                ).json()
                self.assertEqual(last["arguments"], {"input": "x" * 1000})
                self.assertEqual(last["result"], {"content": "y" * 1000})
                self.assertEqual(last["name"], "delegate_research")
                other = app.state.store.create_task("其他轮次")
                self.assertEqual(
                    client.get(
                        f"/api/tasks/{other['task_id']}/steps/{expected_ids[-1]}"
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    client.get(
                        f"/api/tasks/{task_id}/steps", params={"offset": -1}
                    ).status_code,
                    422,
                )
                legacy = client.get(f"/api/tasks/{task_id}").json()
                self.assertEqual(len(legacy["steps"]), 200)
                self.assertIn("arguments", legacy["steps"][0])
                self.assertIn("result", legacy["steps"][0])


if __name__ == "__main__":
    unittest.main()
