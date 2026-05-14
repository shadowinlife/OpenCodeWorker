"""
Stub OpenCode HTTP Server — 集成测试用桩服务。

模拟 opencode serve --port <N> 的行为，避免每次集成测试都调用真实 LLM。

实现的端点：
    GET  /health                             → {"status": "ok"}
    POST /session                            → 创建 session，返回 session dict
    GET  /session/{session_id}               → 返回 session 状态
    POST /session/{session_id}/message       → noReply=True 注入消息（返回消息 dict）
    POST /session/{session_id}/prompt_async  → 触发对话，返回 204（异步推送 SSE）
    POST /session/{session_id}/abort         → 中止 session
    POST /session/{session_id}/permissions/{permission_id}
                                             → 响应权限请求
    GET  /session/{session_id}/diff          → 返回 diff 列表（可配置）
    GET  /session/{session_id}/message       → 返回历史消息列表
    GET  /global/event                       → SSE 事件流（可脚本驱动）

使用方式（pytest fixture）：

    from tests.fixtures.stub_opencode_server import StubOpenCodeServer

    @pytest.fixture
    async def stub_server():
        server = StubOpenCodeServer()
        async with server.run():
            yield server

    async def test_prompt(stub_server):
        client = OpenCodeClient(host="127.0.0.1", port=stub_server.port, password="test")
        session = await client.create_session()
        stub_server.schedule_events(session["id"], [
            {"type": "session.idle", "properties": {"sessionID": session["id"]}},
        ])
        await client.prompt_async(session["id"], [{"type": "text", "text": "Hello"}])
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse


class StubOpenCodeServer:
    """可脚本驱动的 opencode HTTP stub 服务。

    线程安全假设：单进程单 asyncio loop（pytest-asyncio 默认）。
    """

    def __init__(self, password: str = "stub-password") -> None:
        self.password = password
        self.port: int = 0  # 在 run() 后填充

        # 内部状态（测试可直接读写）
        self.sessions: dict[str, dict] = {}
        self.messages: dict[str, list] = defaultdict(list)
        self.diffs: dict[str, list] = defaultdict(list)
        self.permission_responses: dict[str, str] = {}  # permission_id → response

        # SSE 事件队列：key=session_id（全局事件放 "global" key）
        self._event_queues: dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
        # 已连接的 SSE 客户端 queues（用于广播）
        self._sse_subscribers: list[asyncio.Queue] = []

        self._app = self._build_app()
        self._server: uvicorn.Server | None = None

    # ------------------------------------------------------------------
    # 测试辅助 API
    # ------------------------------------------------------------------

    def schedule_events(self, session_id: str, events: list[dict]) -> None:
        """注册将在 prompt_async 后推送到 SSE 流的事件列表。

        事件会在 prompt_async 调用后按顺序推送，每条间隔 20ms。
        """
        for ev in events:
            self._event_queues[session_id].put_nowait(ev)

    def set_diff(self, session_id: str, diff: list) -> None:
        """预设 /diff 端点返回的内容。"""
        self.diffs[session_id] = diff

    def set_permission_response(self, permission_id: str, response: str) -> None:
        """预设 /permissions/:id 接受后 stub 记录的 response 值。"""
        self.permission_responses[permission_id] = response

    # ------------------------------------------------------------------
    # FastAPI 应用构建
    # ------------------------------------------------------------------

    def _build_app(self) -> FastAPI:
        server = self

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            yield

        app = FastAPI(lifespan=lifespan)

        def _check_auth(request: Request) -> None:
            """验证 Basic Auth（username="opencode", password=stub_password）。"""
            import base64
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Basic "):
                raise HTTPException(status_code=401, detail="missing auth")
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                _, pwd = decoded.split(":", 1)
            except Exception:
                raise HTTPException(status_code=401, detail="invalid auth")
            if pwd != server.password:
                raise HTTPException(status_code=401, detail="wrong password")

        @app.get("/health")
        async def health(request: Request):
            _check_auth(request)
            return {"status": "ok"}

        @app.post("/session", status_code=201)
        async def create_session(request: Request):
            _check_auth(request)
            session_id = "ses-" + uuid.uuid4().hex[:12]
            session = {
                "id": session_id,
                "status": "idle",
                "created": time.time(),
                "messages": [],
            }
            server.sessions[session_id] = session
            return session

        @app.get("/session/{session_id}")
        async def get_session(session_id: str, request: Request):
            _check_auth(request)
            s = server.sessions.get(session_id)
            if s is None:
                raise HTTPException(status_code=404, detail="session not found")
            return s

        @app.post("/session/{session_id}/message", status_code=201)
        async def send_message(session_id: str, request: Request):
            _check_auth(request)
            if session_id not in server.sessions:
                raise HTTPException(status_code=404, detail="session not found")
            body = await request.json()
            msg_id = "msg-" + uuid.uuid4().hex[:8]
            msg = {"id": msg_id, "role": "user", "parts": body.get("parts", []), "ts": time.time()}
            server.messages[session_id].append(msg)
            return msg

        @app.post("/session/{session_id}/prompt_async", status_code=204)
        async def prompt_async(session_id: str, request: Request):
            _check_auth(request)
            if session_id not in server.sessions:
                raise HTTPException(status_code=404, detail="session not found")
            server.sessions[session_id]["status"] = "running"
            # 在后台异步推送 scheduled events
            asyncio.create_task(server._push_scheduled_events(session_id))
            return None

        @app.post("/session/{session_id}/abort", status_code=204)
        async def abort_session(session_id: str, request: Request):
            _check_auth(request)
            if session_id not in server.sessions:
                raise HTTPException(status_code=404, detail="session not found")
            server.sessions[session_id]["status"] = "aborted"
            # 推送 session.idle 让 SSE 消费者退出
            await server._broadcast_event({
                "type": "sync",
                "properties": {
                    "syncEvent": {
                        "id": str(uuid.uuid4()),
                        "type": "session.updated",
                        "properties": {"sessionID": session_id, "status": "aborted"},
                    }
                },
            })
            return None

        @app.post("/session/{session_id}/permissions/{permission_id}", status_code=200)
        async def respond_permission(
            session_id: str, permission_id: str, request: Request
        ):
            _check_auth(request)
            if session_id not in server.sessions:
                raise HTTPException(status_code=404, detail="session not found")
            body = await request.json()
            response = body.get("response", "reject")
            if response not in ("once", "always", "reject"):
                raise HTTPException(
                    status_code=400,
                    detail=f"invalid response value: {response!r}; must be once|always|reject",
                )
            server.permission_responses[permission_id] = response
            return {"accepted": True, "permission_id": permission_id}

        @app.get("/session/{session_id}/diff")
        async def get_diff(session_id: str, request: Request):
            _check_auth(request)
            return server.diffs.get(session_id, [])

        @app.get("/session/{session_id}/message")
        async def get_messages(session_id: str, request: Request):
            _check_auth(request)
            return server.messages.get(session_id, [])

        @app.get("/global/event")
        async def global_events(request: Request):
            """SSE /global/event 端点。"""
            _check_auth(request)
            queue: asyncio.Queue = asyncio.Queue()
            server._sse_subscribers.append(queue)

            async def stream() -> AsyncIterator[str]:
                try:
                    while True:
                        if await request.is_disconnected():
                            break
                        try:
                            event = await asyncio.wait_for(queue.get(), timeout=0.5)
                            yield f"data: {json.dumps(event)}\n\n"
                        except asyncio.TimeoutError:
                            # heartbeat
                            yield ": heartbeat\n\n"
                finally:
                    server._sse_subscribers.remove(queue)

            return StreamingResponse(stream(), media_type="text/event-stream")

        return app

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    async def _push_scheduled_events(self, session_id: str) -> None:
        """按顺序推送 schedule_events() 预注册的事件（每条间隔 20ms）。"""
        q = self._event_queues[session_id]
        while not q.empty():
            event = await q.get()
            await asyncio.sleep(0.02)
            await self._broadcast_event(event)

    async def _broadcast_event(self, event: dict) -> None:
        """将事件广播到所有 SSE 连接。"""
        for sub in list(self._sse_subscribers):
            await sub.put(event)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def run(self) -> AsyncIterator["StubOpenCodeServer"]:
        """异步上下文管理器：启动 stub server，yield 后停止。

        端口随机分配（port=0），绑定后可通过 self.port 读取。
        """
        import socket

        # 找一个空闲端口
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]

        config = uvicorn.Config(
            app=self._app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)

        task = asyncio.create_task(self._server.serve())
        # 等待 server 就绪
        deadline = time.monotonic() + 5.0
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("stub opencode server failed to start within 5s")
            await asyncio.sleep(0.05)

        try:
            yield self
        finally:
            self._server.should_exit = True
            await task
