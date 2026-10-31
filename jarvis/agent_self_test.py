"""Explicit, isolated MAF execution check for the packaged worker.

The model responses are fixed fixtures, but the HTTP/SSE exchange, configured
agent branch, delegation, tool invocation and UTF-8 file write are real. This
module is imported only by the explicit self-test role, never by desktop startup.
"""

from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import threading
import time
from unittest.mock import patch

import httpx


_PATH = "reports/agent-self-test.md"
_CONTENT = "# 中文自检报告\n\n你好，贾维斯。"
_PROMPT = f"请写一份中文报告并保存到 {_PATH}。"
_ANSWER = "中文报告已保存。"
_KEY = "local-packaged-self-test-key"


class _CheckFailure(ValueError):
    """A static, non-sensitive diagnostic from this self-test's own assertions."""


def _require(condition, message):
    if not condition:
        raise _CheckFailure(message)


def _tool_call(name, arguments, identifier):
    return {
        "content": None,
        "tool_calls": [
            {
                "id": identifier,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        ],
    }


def _diagnostic(error):
    """Keep missing-module/symbol identities, never provider bodies or secrets."""
    parts, seen = [], set()
    while error is not None and id(error) not in seen and len(parts) < 5:
        seen.add(id(error))
        detail = type(error).__name__
        if isinstance(error, _CheckFailure):
            detail += f": {error}"
        if isinstance(error, ImportError):
            module = getattr(error, "name", None)
            if module and re.fullmatch(r"[A-Za-z0-9_.]+", module):
                detail += f" module={module}"
            symbol = re.search(
                r"cannot import name ['\"]([A-Za-z0-9_]+)['\"]", str(error)
            )
            if symbol:
                detail += f" symbol={symbol[1]}"
        parts.append(detail)
        error = error.__cause__ or error.__context__
    return " <- ".join(parts)


class _ModelServer:
    def __init__(self):
        self.requests = []
        self.failure = None
        self.replies = [
            _tool_call(
                "delegate_writer",
                {"task": f"保存 {_PATH}，内容：{_CONTENT}"},
                "delegate-1",
            ),
            _tool_call("write_file", {"path": _PATH, "content": _CONTENT}, "write-1"),
            {"content": _ANSWER},
            {
                "content": json.dumps(
                    {
                        "status": "succeeded",
                        "display_text": _ANSWER,
                        "speech_text": _ANSWER,
                    },
                    ensure_ascii=False,
                )
            },
        ]
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *_):
                pass

            def do_POST(self):
                try:
                    _require(
                        self.path == "/v1/chat/completions", "Unexpected local API path"
                    )
                    _require(
                        self.headers.get("Authorization") == f"Bearer {_KEY}",
                        "Isolated test key was not used",
                    )
                    length = int(self.headers.get("Content-Length", "0"))
                    _require(0 < length <= 512 * 1024, "Invalid local request size")
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    _require(len(outer.requests) < 4, "Unexpected extra model request")
                    outer.requests.append(body)
                    reply = outer.replies[len(outer.requests) - 1]
                    self.send_response(200)
                    if body.get("stream"):
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        delta = {"role": "assistant", **reply}
                        if "tool_calls" in delta:
                            delta["tool_calls"] = [
                                {"index": index, **item}
                                for index, item in enumerate(delta["tool_calls"])
                            ]
                        for index, chunk in enumerate((delta, {})):
                            payload = {
                                "id": "packaged-agent-test",
                                "object": "chat.completion.chunk",
                                "created": 123,
                                "model": "local-self-test",
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": chunk,
                                        "finish_reason": None
                                        if index == 0
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
                                ).encode("utf-8")
                            )
                        self.wfile.write(b"data: [DONE]\n\n")
                    else:
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        payload = {
                            "id": "packaged-agent-test",
                            "object": "chat.completion",
                            "created": 123,
                            "model": "local-self-test",
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
                        self.wfile.write(
                            json.dumps(payload, ensure_ascii=False).encode("utf-8")
                        )
                    self.wfile.flush()
                except Exception as error:
                    outer.failure = _diagnostic(error)
                    self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05),
            daemon=True,
        )
        self.thread.start()

    @property
    def port(self):
        return self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        _require(not self.thread.is_alive(), "Local test server did not stop")


async def _exercise(directory, server):
    from .agent import Agent
    from .config import Settings
    from .storage import Store
    from .tools import ToolRegistry

    settings = Settings(
        api_key=_KEY,
        base_url=f"http://127.0.0.1:{server.port}/v1",
        model="local-self-test",
        orchestrator="maf",
        task_timeout=15,
        max_model_calls=4,
        max_tool_calls=4,
        workspace=directory / "workspace",
        data_dir=directory / "data",
    )
    store = Store(settings.data_dir)
    store.add_message("user", _PROMPT)
    agent = Agent(settings, store, ToolRegistry(settings.workspace, store))
    async with asyncio.timeout(20):
        events = [event async for event in agent.run(_PROMPT)]
    _require(server.failure is None, "Local model service rejected the exchange")
    _require(len(server.requests) == 4, "MAF did not complete four model requests")
    _require(events and events[-1][0] == "answer", "Missing final agent answer")
    result = events[-1][1]
    _require(result.get("status") == "succeeded", "Configured MAF task failed")
    _require(
        result.get("display_text") == _ANSWER and result.get("speech_text") == _ANSWER,
        "Chinese final answer did not roundtrip",
    )
    _require(
        (settings.workspace / _PATH).read_text(encoding="utf-8") == _CONTENT,
        "Chinese tool output did not roundtrip",
    )
    _require(
        result.get("usage") == {"model_calls": 4, "tool_calls": 1},
        "Wrong model/tool execution counts",
    )
    first, writer, continuation, final = server.requests

    def tool_names(body):
        return {tool["function"]["name"] for tool in body["tools"]}

    _require(
        "delegate_writer" in tool_names(first)
        and "write_file" not in tool_names(first),
        "Coordinator delegation was bypassed",
    )
    _require(
        "write_file" in tool_names(writer)
        and "delegate_writer" not in tool_names(writer),
        "Writer tool scope was not used",
    )
    _require(
        any(
            m.get("role") == "user" and _PROMPT in str(m.get("content"))
            for m in first["messages"]
        ),
        "Chinese user request did not roundtrip",
    )
    _require(
        writer.get("stream") is True and continuation.get("stream") is True,
        "Delegated agent did not use streaming",
    )
    for body, call_id in ((continuation, "write-1"), (final, "delegate-1")):
        _require(
            any(
                m.get("role") == "tool" and m.get("tool_call_id") == call_id
                for m in body["messages"]
            ),
            "Tool result was not returned to the model",
        )
    successful_tools = [
        data
        for event, data in events
        if event == "tool_result"
        and data.get("name") == "write_file"
        and "error" not in data.get("result", {})
    ]
    _require(len(successful_tools) == 1, "Expected one successful real file tool")
    return {
        "model_requests": 4,
        "tool_calls": 1,
        "streaming_requests": 2,
        "chinese_request_roundtrip": True,
        "chinese_file_roundtrip": True,
        "chinese_answer_roundtrip": True,
        "coordinator_writer_delegation": True,
    }


def run_agent_self_test() -> dict:
    started = time.monotonic()
    report = {
        "ok": False,
        "mode": "configured-maf",
        "provider_responses": "explicit local HTTP/SSE fixtures",
        "external_services_used": False,
        "user_configuration_used": False,
    }
    server = None
    try:
        with TemporaryDirectory(prefix="jarvis-agent-self-test-") as temporary:
            server = _ModelServer()
            original_client = httpx.AsyncClient

            async def local_only(request):
                _require(
                    request.url.scheme == "http"
                    and request.url.host == "127.0.0.1"
                    and request.url.port == server.port,
                    "Self-test attempted a non-local model request",
                )

            class LocalClient(original_client):
                def __init__(self, *args, **kwargs):
                    hooks = dict(kwargs.pop("event_hooks", {}))
                    hooks["request"] = [local_only, *hooks.get("request", [])]
                    kwargs.update(event_hooks=hooks, trust_env=False)
                    super().__init__(*args, **kwargs)

            with patch.object(httpx, "AsyncClient", LocalClient):
                report.update(asyncio.run(_exercise(Path(temporary), server)))
            report["ok"] = True
    except Exception as error:
        report["error_type"] = type(error).__name__
        report["diagnostic"] = _diagnostic(error)
    finally:
        if server is not None:
            try:
                server.close()
                report["local_server_stopped"] = True
            except Exception as error:
                report["ok"] = False
                report["local_server_stopped"] = False
                report["diagnostic"] = _diagnostic(error)
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report
