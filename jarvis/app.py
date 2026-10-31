"""Local task API, durable SSE replay and bounded workspace file transfer."""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import hmac
import json
import mimetypes
from pathlib import Path
import shutil

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent import Agent
from .config import ROOT, Settings
from .storage import Store
from .tasks import TaskConflict, TaskManager, TaskQueueFull
from .tools import ToolRegistry

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
UPLOAD_EXTENSIONS = {".csv", ".txt", ".md", ".json"}


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)


class TaskRequest(ChatRequest):
    client_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    parent_task_id: str | None = Field(default=None, min_length=1, max_length=128)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)


class InputRequest(ChatRequest):
    client_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)


class SessionRequest(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=120)


def create_app(settings: Settings | None = None, *, agent_factory=Agent) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        settings.workspace.mkdir(parents=True, exist_ok=True)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        sample = settings.workspace / "example-sales.csv"
        try:
            with sample.open("xb") as destination:
                with (ROOT / "examples" / "example-sales.csv").open("rb") as source:
                    shutil.copyfileobj(source, destination)
        except FileExistsError:
            pass
        store = Store(settings.data_dir)
        registry = ToolRegistry(
            settings.workspace, store, brave_api_key=settings.brave_api_key
        )
        manager = TaskManager(settings, store, registry, agent_factory)
        app.state.store, app.state.registry, app.state.tasks = store, registry, manager
        # Kept for old browser integrations; actual execution belongs to manager.
        app.state.busy = False
        await manager.start()
        try:
            yield
        finally:
            await manager.close()

    app = FastAPI(
        title="open-JARVIS",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"],
    )

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        token = getattr(settings, "instance_token", "")
        if (
            request.url.path.startswith("/api/")
            and request.url.path != "/api/health"
            and token
        ):
            supplied = request.headers.get("authorization", "")
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
                return JSONResponse(
                    {"detail": "本地客户端身份校验失败。"}, status_code=401
                )
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if (origin and origin != expected) or request.headers.get(
                "sec-fetch-site"
            ) == "cross-site":
                return JSONResponse(
                    {"detail": "不允许跨站操作本地助理。"}, status_code=403
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
        )
        return response

    @app.exception_handler(TaskQueueFull)
    async def queue_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=429)

    @app.exception_handler(TaskConflict)
    async def conflict_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    def text_message(payload):
        value = payload.message.strip()
        if not value:
            raise HTTPException(422, "消息不能为空。")
        return value

    def require_task(task_id):
        try:
            return app.state.tasks.require(task_id)
        except KeyError as exc:
            raise HTTPException(404, "找不到该任务。") from exc

    @app.get("/api/health")
    async def health():
        return {
            "ready": True,
            "instance_id": getattr(settings, "instance_id", ""),
            "last_event_id": app.state.store.last_event_id(),
        }

    @app.get("/api/status")
    async def status():
        return {
            "configured": settings.configured,
            "model": settings.model if settings.configured else "演示模式",
            "voice": {"input": "sherpa-onnx", "output": "kokoro"},
            "workspace": str(settings.workspace),
            "orchestrator": settings.orchestrator,
            "last_event_id": app.state.store.last_event_id(),
            "active_task_id": app.state.tasks.active_id,
            "pending_tasks": len(app.state.tasks.pending),
            "active_session_id": app.state.store.current_session()["session_id"],
            "tools": [
                {
                    "name": item["function"]["name"],
                    "description": item["function"]["description"],
                }
                for item in app.state.registry.definitions
            ],
        }

    def require_session(session_id):
        session = app.state.store.get_session(session_id)
        if session is None:
            raise HTTPException(404, "找不到该对话。")
        return session

    @app.get("/api/sessions")
    async def list_sessions(limit: int = Query(100, ge=1, le=200)):
        return {
            "sessions": app.state.store.list_sessions(limit),
            "active_session_id": app.state.store.current_session()["session_id"],
        }

    @app.post("/api/sessions", status_code=201)
    async def new_session(payload: SessionRequest):
        return app.state.store.create_session(payload.title)

    @app.get("/api/sessions/current")
    async def current_session():
        return app.state.store.current_session()

    @app.post("/api/sessions/{session_id}/activate")
    async def activate_session(session_id: str):
        require_session(session_id)
        return app.state.store.activate_session(session_id)

    @app.get("/api/sessions/{session_id}/runs")
    async def session_runs(session_id: str, limit: int = Query(100, ge=1, le=200)):
        require_session(session_id)
        return {
            "session_id": session_id,
            "runs": app.state.store.list_tasks(limit, session_id=session_id),
        }

    @app.get("/api/tools")
    async def tool_catalog():
        return {"tools": app.state.registry.catalog(orchestrator=settings.orchestrator)}

    @app.post("/api/tasks", status_code=202)
    async def submit_task(payload: TaskRequest):
        task = await app.state.tasks.submit(
            text_message(payload),
            payload.client_request_id,
            payload.parent_task_id,
            payload.session_id,
        )
        return {
            "task_id": task["task_id"],
            "run_id": task["run_id"],
            "session_id": task["session_id"],
            "status": task["status"],
            "held": task["held"],
            "parent_task_id": task["parent_task_id"],
        }

    @app.get("/api/tasks")
    async def list_tasks(
        limit: int = Query(100, ge=1, le=200), session_id: str | None = None
    ):
        if session_id:
            require_session(session_id)
        return {"tasks": app.state.store.list_tasks(limit, session_id=session_id)}

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str):
        require_task(task_id)
        return app.state.tasks.details(task_id)

    @app.post("/api/tasks/{task_id}/input", status_code=202)
    async def provide_input(task_id: str, payload: InputRequest):
        require_task(task_id)
        return await app.state.tasks.provide_input(
            task_id,
            text_message(payload),
            payload.client_request_id,
            payload.session_id,
        )

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str):
        require_task(task_id)
        return await app.state.tasks.cancel(task_id)

    @app.post("/api/tasks/{task_id}/resume", status_code=202)
    async def resume_task(task_id: str):
        require_task(task_id)
        return await app.state.tasks.resume(task_id)

    @app.get("/api/events")
    async def stream_events(
        request: Request, after: int = Query(0, ge=0), task_id: str | None = None
    ):
        if task_id:
            require_task(task_id)
        last_id = request.headers.get("last-event-id")
        if last_id:
            try:
                after = max(after, int(last_id))
            except ValueError as exc:
                raise HTTPException(400, "Last-Event-ID 必须是事件编号。") from exc

        async def stream():
            async for event in app.state.tasks.events(after, task_id):
                if await request.is_disconnected():
                    return
                if event is None:
                    yield ": keep-alive\n\n"
                else:
                    yield encode_event(event["type"], event, event["event_id"])

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/history")
    async def history():
        return {"messages": app.state.store.history()}

    @app.delete("/api/history")
    async def clear_history():
        if app.state.tasks.busy or app.state.busy:
            raise HTTPException(409, "任务执行中，请稍后清空对话。")
        app.state.store.clear_history()
        return {"ok": True}

    @app.get("/api/dashboard")
    async def dashboard():
        return app.state.registry.dashboard()

    @app.post("/api/files", status_code=201)
    async def upload_file(request: Request, path: str):
        if Path(path).suffix.lower() not in UPLOAD_EXTENSIONS:
            raise HTTPException(400, "仅支持 CSV、TXT、Markdown 和 JSON 文件。")
        try:
            target = app.state.registry.resolve_path(path)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, "无效的工作目录文件路径。") from exc
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "文件不能超过 2 MB。")
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(400, "请上传 UTF-8 编码的文本文件。") from exc
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as handle:
                handle.write(data)
        except FileExistsError as exc:
            raise HTTPException(409, "同名文件已存在，请重命名后上传。") from exc
        except OSError as exc:
            raise HTTPException(400, "无法保存文件，请检查路径。") from exc
        return {
            "path": target.relative_to(settings.workspace.resolve()).as_posix(),
            "size": len(data),
        }

    def local_file(path):
        try:
            target = app.state.registry.resolve_path(path)
            if not target.is_file():
                raise ValueError("not a file")
            return target
        except (ValueError, OSError) as exc:
            raise HTTPException(404, "找不到该工作目录文件。") from exc

    @app.get("/api/files")
    async def download_file(path: str):
        target = local_file(path)
        return FileResponse(
            target, filename=target.name, media_type="application/octet-stream"
        )

    def artifact_file(artifact_id):
        artifact = app.state.store.get_artifact(artifact_id)
        if not artifact:
            raise HTTPException(404, "找不到该产物。")
        target = local_file(artifact["path"])
        try:
            if target.stat().st_size > 512 * 1024 * 1024:
                raise HTTPException(413, "该产物超过预览和媒体传输大小限制。")
        except OSError as exc:
            raise HTTPException(404, "无法读取该产物。") from exc
        return artifact, target

    @app.get("/api/artifacts/{artifact_id}/preview")
    async def preview_artifact(artifact_id: str):
        artifact, target = artifact_file(artifact_id)
        from .artifact_preview import build_preview

        try:
            preview = await asyncio.to_thread(build_preview, target)
        except (ValueError, OSError) as exc:
            raise HTTPException(
                422, "无法预览该文件，请检查格式或下载后查看。"
            ) from exc
        if not isinstance(preview, dict):
            raise HTTPException(422, "文件预览结果格式异常。")
        return {
            **preview,
            "artifact_id": artifact_id,
            "name": target.name,
            "path": artifact["path"],
            "media_type": mimetypes.guess_type(target.name)[0]
            or artifact["media_type"],
            "media_url": f"/api/artifacts/{artifact_id}/media",
        }

    @app.get("/api/artifacts/{artifact_id}/media")
    async def artifact_media(artifact_id: str):
        artifact, target = artifact_file(artifact_id)
        media_type = mimetypes.guess_type(target.name)[0] or artifact["media_type"]
        inline = media_type in {
            "image/png",
            "image/jpeg",
            "image/webp",
            "image/gif",
            "video/mp4",
            "audio/wav",
            "audio/x-wav",
            "audio/mpeg",
            "audio/ogg",
        }
        return FileResponse(
            target,
            filename=target.name,
            media_type=media_type,
            content_disposition_type="inline" if inline else "attachment",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/artifacts/{artifact_id}")
    async def download_artifact(artifact_id: str):
        artifact = app.state.store.get_artifact(artifact_id)
        if not artifact:
            raise HTTPException(404, "找不到该产物。")
        target = local_file(artifact["path"])
        return FileResponse(
            target, filename=target.name, media_type="application/octet-stream"
        )

    @app.post("/api/chat")
    async def legacy_chat(payload: ChatRequest):
        if app.state.busy:
            raise HTTPException(409, "上一个任务仍在执行，请等待完成。")
        task = await app.state.tasks.submit(text_message(payload))

        async def stream():
            async for event in app.state.tasks.events(
                task_id=task["task_id"], stop_at_terminal=True
            ):
                if event and event["type"] in {
                    "agent.status",
                    "agent.tool_start",
                    "agent.tool_result",
                    "agent.answer",
                    "agent.error",
                }:
                    yield encode_event(event["type"][6:], event["data"])
            yield encode_event("done", {})

        # This stream only observes the managed task. Disconnecting does not
        # cancel it, and native clients use /api/tasks + /api/events instead.
        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    static = ROOT / "static"
    if static.is_dir():
        app.mount("/static", StaticFiles(directory=static), name="static")
        app.mount("/", StaticFiles(directory=static, html=True), name="web")
    return app


def encode_event(event: str, data: dict, event_id: int | None = None) -> str:
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


app = create_app()
