"""Bounded, durable task execution independent of HTTP subscriptions."""

from __future__ import annotations

import asyncio
from collections import deque
import contextlib
from dataclasses import replace
import logging
import mimetypes
import time

from .agent import Agent, AgentError
from .storage import Store, TERMINAL_STATUSES

logger = logging.getLogger(__name__)


class TaskConflict(ValueError):
    pass


class TaskQueueFull(TaskConflict):
    pass


class TaskManager:
    def __init__(self, settings, store: Store, registry, agent_factory=Agent):
        self.settings, self.store, self.registry = settings, store, registry
        self.agent_factory = agent_factory
        self.pending = deque()
        self.active_id = None
        self._active = None
        self._worker = None
        self._wake = asyncio.Event()
        self._changed = asyncio.Condition()
        self._closing = False
        self._cancel_requested = set()
        self._steps = {}
        self._actual_names = {item["function"]["name"] for item in registry.definitions}
        self._journaled = {}
        self._arguments = {}

    @property
    def busy(self):
        return bool(self.active_id or self.pending)

    async def start(self):
        self.store.recover_tasks()
        self.store.expire_waiting()
        self._worker = asyncio.create_task(self._run_queue(), name="jarvis-task-queue")

    async def close(self):
        self._closing = True
        self._wake.set()
        if self._active and not self._active.done():
            self._active.cancel()
        if self._worker:
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        # Leave pending work held: a restart must not silently dispatch it.
        for task_id in self.pending:
            self.store.update_task(
                task_id, "task.held", {"status": "queued", "held": True}, held=True
            )
        self.pending.clear()
        await self._notify()

    async def _notify(self):
        async with self._changed:
            self._changed.notify_all()

    def _capacity(self):
        # The active slot is reserved synchronously at dispatch, otherwise queued
        # requests before the worker runs may consume that first free slot.
        total_limit = int(getattr(self.settings, "max_pending_tasks", 3)) + 1
        if len(self.pending) + (1 if self.active_id else 0) >= total_limit:
            raise TaskQueueFull("任务队列已满，请等待当前任务完成后再提交。")

    async def submit(self, message, client_request_id=None):
        if self._closing:
            raise TaskConflict("服务正在退出，暂不接收新任务。")
        if client_request_id:
            existing = self.store.find_request(client_request_id)
            if existing:
                if existing["message"] != message:
                    raise TaskConflict("同一请求标识不能用于不同指令。")
                return existing
        self._capacity()
        task = self.store.create_task(message, client_request_id)
        self.pending.append(task["task_id"])
        self._wake.set()
        await self._notify()
        return task

    def require(self, task_id):
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    async def provide_input(self, task_id, message):
        if self.store.expire_waiting():
            await self._notify()
        task = self.require(task_id)
        if task["status"] != "awaiting_input":
            raise TaskConflict("该任务当前没有等待补充信息。")
        self._capacity()
        self.store.add_message("user", message, task_id)
        self.store.update_task(
            task_id,
            "task.queued",
            {"status": "queued", "message": message, "resumed": True},
            status="queued",
            held=False,
            error="",
        )
        self.pending.append(task_id)
        self._wake.set()
        await self._notify()
        return self.require(task_id)

    async def resume(self, task_id):
        task = self.require(task_id)
        if task["status"] != "queued" or not task["held"]:
            raise TaskConflict(
                "仅可恢复重启后暂停派发的排队任务；中断任务请核对产物后重新安排。"
            )
        self._capacity()
        self.store.update_task(
            task_id,
            "task.queued",
            {"status": "queued", "held": False, "resumed": True},
            held=False,
        )
        self.pending.append(task_id)
        self._wake.set()
        await self._notify()
        return self.require(task_id)

    async def cancel(self, task_id):
        task = self.require(task_id)
        if task["status"] in TERMINAL_STATUSES:
            return task
        if self.active_id == task_id and self._active:
            if task_id not in self._cancel_requested:
                self._cancel_requested.add(task_id)
                self.store.add_event(
                    task_id,
                    "task.cancel_requested",
                    {"message": "已请求取消；正在完成的工具操作会记录真实结果。"},
                )
                self._active.cancel()
        else:
            with contextlib.suppress(ValueError):
                self.pending.remove(task_id)
            self._finish(
                task_id,
                "cancelled",
                "任务已取消。已完成的操作和产物仍保留。",
                error="用户取消",
            )
        await self._notify()
        result = self.require(task_id)
        result["cancel_requested"] = task_id in self._cancel_requested
        return result

    async def _run_queue(self):
        while not self._closing:
            if not self.pending:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), 60)
                except TimeoutError:
                    if self.store.expire_waiting():
                        await self._notify()
                continue
            task_id = self.pending.popleft()
            task = self.require(task_id)
            if task["status"] != "queued" or task["held"]:
                continue
            self.active_id = task_id
            self._active = asyncio.create_task(
                self._execute(task_id), name=f"jarvis-{task_id}"
            )
            try:
                await self._active
            except asyncio.CancelledError:
                # Cancellation before _execute's first instruction still gets a
                # durable terminal result, rather than leaving queued work lost.
                status = "interrupted" if self._closing else "cancelled"
                if self.require(task_id)["status"] not in TERMINAL_STATUSES:
                    self._finish(
                        task_id, status, "任务已中断，已完成操作仍保留。", error=status
                    )
            finally:
                self._cancel_requested.discard(task_id)
                self.active_id = None
                self._active = None
                await self._notify()

    async def _execute(self, task_id):
        started = time.monotonic()
        task = self.require(task_id)
        prior_elapsed = task["elapsed_seconds"]
        self._steps[task_id] = deque()
        self._journaled[task_id] = deque()
        self._arguments[task_id] = {}
        self.store.update_task(
            task_id,
            "task.planning",
            {"status": "planning", "message": "正在准备任务…"},
            status="planning",
            held=False,
        )
        await self._notify()
        scoped = self.store.scoped(task_id)
        history = scoped.history()
        message = next(
            (item["content"] for item in reversed(history) if item["role"] == "user"),
            task["message"],
        )
        registry = _TaskRegistry(self, task_id, self.registry)
        remaining = replace(
            self.settings,
            max_model_calls=max(0, self.settings.max_model_calls - task["model_calls"]),
            max_tool_calls=max(0, self.settings.max_tool_calls - task["tool_calls"]),
        )
        agent = self.agent_factory(remaining, scoped, registry)
        answered = False
        try:
            timeout = max(
                0.01, float(getattr(self.settings, "task_timeout", 300)) - prior_elapsed
            )
            async with (
                asyncio.timeout(timeout),
                contextlib.aclosing(agent.run(message)) as stream,
            ):
                async for event, data in stream:
                    if self._closing or task_id in self._cancel_requested:
                        raise asyncio.CancelledError
                    # Keep legacy event payloads for the optional web client.
                    self.store.add_event(task_id, "agent." + event, data)
                    if event == "status":
                        self.store.update_task(
                            task_id,
                            "task.progress",
                            {"status": "running", **data},
                            status="running",
                        )
                    elif event in {"plan", "plan_created"}:
                        plan = data.get("steps", data.get("plan", []))
                        self.store.update_task(
                            task_id, "task.plan_created", data, plan=plan
                        )
                    elif event == "usage":
                        if not data.get("persisted"):
                            scoped.record_usage(
                                model_calls_delta=data.get("model_calls_delta", 0),
                                tool_calls_delta=data.get("tool_calls_delta", 0),
                            )
                    elif event == "tool_start":
                        name = str(data.get("name", "工具"))
                        arguments = data.get("arguments", {})
                        if name in self._actual_names:
                            self._arguments[task_id].setdefault(name, deque()).append(
                                arguments
                            )
                        else:
                            step_id, _ = self.store.add_step(task_id, name, arguments)
                            self._steps[task_id].append((name, step_id))
                        if self.require(task_id)["status"] != "running":
                            self.store.update_task(
                                task_id,
                                "task.progress",
                                {"status": "running"},
                                status="running",
                            )
                    elif event == "tool_result":
                        name = str(data.get("name", "工具"))
                        result = data.get("result", {})
                        recorded = next(
                            (
                                item
                                for item in self._journaled[task_id]
                                if item[0] == name and item[1] == result
                            ),
                            None,
                        )
                        if recorded is not None:
                            self._journaled[task_id].remove(recorded)
                            if self._arguments[task_id].get(name):
                                self._arguments[task_id][name].popleft()
                        else:
                            self._record_result(task_id, name, result)
                    elif event == "artifact":
                        self._record_artifact(task_id, data.get("path"))
                    elif event == "error":
                        raise AgentError(data.get("message", "任务执行失败"))
                    elif event == "answer":
                        text = data.get("display_text") or data.get("content") or ""
                        if not isinstance(text, str) or not text.strip():
                            raise AgentError("任务没有返回有效结果。")
                        scoped.add_message("assistant", text)
                        awaiting = (
                            bool(data.get("awaiting_input"))
                            or data.get("status") == "awaiting_input"
                        )
                        if awaiting:
                            self.store.update_task(
                                task_id,
                                "task.awaiting_input",
                                {
                                    "status": "awaiting_input",
                                    "display_text": text,
                                    "speech_text": data.get("speech_text")
                                    or text[:300],
                                },
                                status="awaiting_input",
                                display_text=text,
                                speech_text=data.get("speech_text") or text[:300],
                            )
                        else:
                            steps = self.store.list_steps(task_id)
                            failures = [
                                step for step in steps if step["status"] == "failed"
                            ]
                            status = data.get("status")
                            if status not in {
                                "succeeded",
                                "partially_succeeded",
                                "failed",
                            }:
                                status = (
                                    (
                                        "partially_succeeded"
                                        if any(
                                            step["status"] == "succeeded"
                                            for step in steps
                                        )
                                        else "failed"
                                    )
                                    if failures
                                    else "succeeded"
                                )
                            # A failed actual tool cannot be hidden by a model's
                            # blanket success status. Recovered tool failures can
                            # still be explained in the report as partial work.
                            if failures and status == "succeeded":
                                status = (
                                    "partially_succeeded"
                                    if any(
                                        step["status"] == "succeeded" for step in steps
                                    )
                                    else "failed"
                                )
                            self._finish(
                                task_id,
                                status,
                                text,
                                speech_text=data.get("speech_text"),
                                error="部分工具执行失败" if failures else "",
                            )
                        answered = True
                        await self._notify()
                        break
                    await self._notify()
            if not answered:
                raise AgentError("执行过程已结束，但尚未返回最终结果。")
        except asyncio.CancelledError:
            status = (
                "interrupted"
                if self._closing and task_id not in self._cancel_requested
                else "cancelled"
            )
            text = (
                "服务退出，任务已中断。已完成的操作仍保留，请核对产物后继续。"
                if status == "interrupted"
                else "任务已取消。已完成的操作和产物仍保留。"
            )
            self._finish(
                task_id,
                status,
                text,
                error="服务退出" if status == "interrupted" else "用户取消",
            )
        except TimeoutError:
            self._finish_error(
                task_id, "任务已达到执行时限，已完成的操作和产物仍保留。"
            )
        except AgentError as exc:
            self._finish_error(task_id, str(exc))
        except Exception:
            logger.exception("Task %s failed", task_id)
            self._finish_error(
                task_id, "任务执行出现异常。已完成的操作仍然保留，请检查结果后再试。"
            )
        finally:
            elapsed = prior_elapsed + time.monotonic() - started
            self.store.update_task(
                task_id,
                "task.execution_stopped",
                {"elapsed_seconds": round(elapsed, 3)},
                elapsed_seconds=elapsed,
            )
            self._steps.pop(task_id, None)
            self._journaled.pop(task_id, None)
            self._arguments.pop(task_id, None)
            await self._notify()

    def _record_result(self, task_id, name, result):
        if not isinstance(result, dict):
            result = {"data": result}
        queue = self._steps.setdefault(task_id, deque())
        match = next((item for item in queue if item[0] == name), None)
        if match:
            queue.remove(match)
            self.store.finish_step(match[1], result)
        else:
            arguments = {}
            if self._arguments.get(task_id, {}).get(name):
                arguments = self._arguments[task_id][name].popleft()
            step_id, _ = self.store.add_step(task_id, name, arguments)
            self.store.finish_step(step_id, result)
        if name == "write_file" and not result.get("error"):
            self._record_artifact(task_id, result.get("path"))

    def _record_artifact(self, task_id, path):
        if not isinstance(path, str):
            return
        try:
            target = self.registry.resolve_path(path)
            if target.is_file():
                relative = target.relative_to(self.registry.workspace).as_posix()
                self.store.add_artifact(
                    task_id,
                    relative,
                    target.stat().st_size,
                    mimetypes.guess_type(relative)[0] or "application/octet-stream",
                )
        except (ValueError, OSError):
            logger.warning("Ignored invalid artifact path for task %s", task_id)

    def _finish_error(self, task_id, text):
        successes = any(
            step["status"] == "succeeded" for step in self.store.list_steps(task_id)
        )
        self.store.add_message("assistant", text, task_id)
        self.store.add_event(task_id, "agent.error", {"message": text})
        self._finish(
            task_id, "partially_succeeded" if successes else "failed", text, error=text
        )

    def _finish(self, task_id, status, text, *, speech_text=None, error=""):
        if not speech_text or not isinstance(speech_text, str):
            speech_text = text[:300] + (
                "，完整内容请查看结果窗口。" if len(text) > 300 else ""
            )
        self.store.mark_pending_steps(
            task_id, "cancelled" if status == "cancelled" else "interrupted"
        )
        artifacts = self.store.list_artifacts(task_id)
        steps = self.store.list_steps(task_id)
        data = {
            "status": status,
            "display_text": text,
            "speech_text": speech_text,
            "error": error,
            "artifacts": artifacts,
            "completed_steps": [
                step["step_id"] for step in steps if step["status"] == "succeeded"
            ],
            "unfinished_steps": [
                step["step_id"] for step in steps if step["status"] != "succeeded"
            ],
        }
        event_type = {
            "succeeded": "task.completed",
            "partially_succeeded": "task.completed",
            "failed": "task.failed",
            "cancelled": "task.cancelled",
            "interrupted": "task.interrupted",
        }[status]
        self.store.update_task(
            task_id,
            event_type,
            data,
            status=status,
            held=False,
            display_text=text,
            speech_text=speech_text,
            error=error,
        )

    def details(self, task_id):
        task = self.require(task_id)
        task.update(
            artifacts=self.store.list_artifacts(task_id),
            steps=self.store.list_steps(task_id),
            events=self.store.recent_events(task_id),
            messages=self.store.task_history(task_id),
        )
        task["cancel_requested"] = task_id in self._cancel_requested
        return task

    async def events(self, after=0, task_id=None, stop_at_terminal=False):
        """Replay first, then wait on notification; no polling or tied execution."""
        cursor = after
        while True:
            # Inspect under the condition lock to avoid a notification race.
            async with self._changed:
                batch = self.store.events_after(cursor, task_id)
                if not batch:
                    task = self.store.get_task(task_id) if task_id else None
                    if self._closing or (
                        stop_at_terminal
                        and task
                        and task["status"] in TERMINAL_STATUSES | {"awaiting_input"}
                    ):
                        return
                    try:
                        await asyncio.wait_for(self._changed.wait(), 15)
                    except TimeoutError:
                        pass
                    batch = self.store.events_after(cursor, task_id)
            if not batch:
                yield None  # SSE heartbeat; no database writes.
                continue
            for event in batch:
                cursor = event["event_id"]
                yield event


class _TaskRegistry:
    """Ensure cancellation cannot lose the outcome of an in-flight tool write."""

    def __init__(self, manager, task_id, registry):
        self.manager, self.task_id, self.registry = manager, task_id, registry

    def __getattr__(self, name):
        return getattr(self.registry, name)

    async def execute(self, name, arguments):
        if self.manager._closing or self.task_id in self.manager._cancel_requested:
            raise asyncio.CancelledError
        step_id, _ = self.manager.store.add_step(self.task_id, name, arguments)
        await self.manager._notify()
        operation = asyncio.create_task(self.registry.execute(name, arguments))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(operation)
                break
            except asyncio.CancelledError:
                # A user cancel followed immediately by process shutdown can
                # deliver cancellation more than once. Keep shielding every
                # wait: cancelling the asyncio task does not stop its file-
                # writing worker thread, and we still need its actual outcome.
                cancelled = True
                if operation.cancelled():
                    result = {"error": "操作中断，无法确认工具结果，请核对工作目录。"}
                    break
            except Exception:
                result = {"error": "工具执行异常，请核对工作目录。"}
                break
        # Audit at the actual operation boundary. An SDK producer may outlive
        # its event consumer during cancellation; durable facts cannot depend
        # on the consumer draining a tool_result notification.
        self.manager.store.finish_step(step_id, result)
        self.manager._journaled.setdefault(self.task_id, deque()).append((name, result))
        if name == "write_file" and not result.get("error"):
            self.manager._record_artifact(self.task_id, result.get("path"))
        await self.manager._notify()
        if cancelled:
            raise asyncio.CancelledError
        return result
