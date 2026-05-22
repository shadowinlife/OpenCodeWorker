"""SDK 单元测试。

测试策略（与 design §13.3 对齐）：

1. 纯单元测试：错误映射、SSE parser、重连逻辑
2. 基于"Worker stub server"的协议测试 —— 这里用 ``httpx.ASGITransport`` 加载
   一个最小 FastAPI app，覆盖完整的 SSE / HITL / artifact 协议路径，但不依
   赖宿主机端口或真实 Worker 状态机。

注意：故意不复用 worker.api.routes 里的真实路由，因为：
- 真实路由依赖 SQLite / Orchestrator / event_bus，单测会变成集成测试
- 这里要验证的是 **SDK 的协议解释**（Last-Event-ID、reconnect、HTTP error
  分类），所以 stub 服务端要"可控地"模拟边界行为
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from worker_sdk import (
    AsyncWorkerClient,
    WorkerClientError,
    WorkerCompatibilityError,
    WorkerConflictError,
    WorkerNotFoundError,
    WorkerSSEError,
    WorkerTaskAborted,
    WorkerTaskFailed,
    WorkerUnauthorizedError,
)
from worker_sdk.compat import is_compatible
from worker_sdk.errors import http_error_for, terminal_error_for
from worker_sdk.sse import _parse_sse_event


# ---------------------------------------------------------------------------
# Stub Worker server
# ---------------------------------------------------------------------------

class StubWorker:
    """可被测试调度的 Worker stub，记录请求并按脚本回放 SSE/HTTP。

    每个测试函数构造一个新的 StubWorker，注入到 ASGITransport 中。
    """

    def __init__(self) -> None:
        self.app = FastAPI()
        self.calls: list[tuple[str, str]] = []
        # 协议级状态：task snapshot、待发的 SSE 事件批次
        self.task: dict[str, object] = {
            "task_id": "stub-task-1",
            "status": "queued",
            "mode": "plan_first",
            "created_at": 0.0,
            "updated_at": 0.0,
        }
        # SSE 行为：每次新连接消费 sse_batches 的下一组事件
        # 一组事件 = list[dict(id, event, data)]，None 表示连接断开（模拟网络中断）
        self.sse_batches: list[list[dict] | None] = []
        # /tasks/{id}/decisions 收到的请求体（用于断言 idempotency_key）
        self.decision_payloads: list[dict] = []
        # artifact 文件路径，下载时直接返回
        self.artifact_file: Path | None = None
        # 控制 /health 返回的版本字符串
        self.health_version = "0.1.0"
        # 控制 create_task 返回的 status code（用于触发 409）
        self.create_status_code = 201
        # 控制 SSE 接到的 Last-Event-ID（验证重连传 cursor）
        self.last_event_id_received: list[str | None] = []

        self._register_routes()

    # ------------------------------------------------------------------ #
    # routes                                                             #
    # ------------------------------------------------------------------ #

    def _register_routes(self) -> None:  # noqa: C901 - test stub, branchy is fine
        app = self.app

        @app.get("/health")
        async def health() -> dict:
            self.calls.append(("GET", "/health"))
            return {"status": "ok", "version": self.health_version}

        @app.post("/tasks", status_code=201)
        async def create_task(request: Request):
            self.calls.append(("POST", "/tasks"))
            if self.create_status_code != 201:
                raise HTTPException(
                    status_code=self.create_status_code,
                    detail="conflict-from-stub",
                )
            body = await request.json()
            task_id = body.get("task_id") or self.task["task_id"]
            snapshot = {**self.task, "task_id": task_id}
            return snapshot

        @app.get("/tasks/{task_id}")
        async def get_task(task_id: str) -> dict:
            self.calls.append(("GET", f"/tasks/{task_id}"))
            if task_id != self.task["task_id"]:
                raise HTTPException(status_code=404, detail="task not found")
            return self.task

        @app.post("/tasks/{task_id}/abort")
        async def abort_task(task_id: str) -> dict:
            self.calls.append(("POST", f"/tasks/{task_id}/abort"))
            if task_id != self.task["task_id"]:
                raise HTTPException(status_code=404, detail="task not found")
            return {"aborted": True, "task_id": task_id}

        @app.get("/tasks/{task_id}/events")
        async def task_events(
            task_id: str,
            last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        ):
            self.calls.append(("GET", f"/tasks/{task_id}/events"))
            self.last_event_id_received.append(last_event_id)
            if not self.sse_batches:
                raise HTTPException(status_code=500, detail="no SSE batch programmed")
            batch = self.sse_batches.pop(0)
            return StreamingResponse(
                _render_sse(batch),
                media_type="text/event-stream",
            )

        @app.post("/tasks/{task_id}/decisions")
        async def submit_decision(task_id: str, request: Request) -> dict:
            self.calls.append(("POST", f"/tasks/{task_id}/decisions"))
            body = await request.json()
            self.decision_payloads.append(body)
            return {"accepted": True, "decision_id": body["decision_id"]}

        @app.get("/tasks/{task_id}/artifacts")
        async def list_artifacts(task_id: str):
            self.calls.append(("GET", f"/tasks/{task_id}/artifacts"))
            if task_id != self.task["task_id"]:
                raise HTTPException(status_code=404, detail="task not found")
            return [
                {
                    "artifact_id": "art-1",
                    "task_id": task_id,
                    "type": "log",
                    "filename": "run.log",
                    "size": 42,
                    "created_at": 0.0,
                    "expires_at": None,
                    "download_url": f"/tasks/{task_id}/artifacts/art-1",
                    "metadata": {"interceptor": "conversations"},
                }
            ]

        @app.get("/tasks/{task_id}/artifacts/{artifact_id}")
        async def download_artifact(task_id: str, artifact_id: str):
            self.calls.append(("GET", f"/tasks/{task_id}/artifacts/{artifact_id}"))
            if self.artifact_file is None:
                # 默认返回一个小 payload，方便 bytes 模式断言
                return PlainTextResponse(
                    content="artifact-bytes",
                    media_type="application/octet-stream",
                )
            return FileResponse(
                path=str(self.artifact_file),
                filename=self.artifact_file.name,
                media_type="application/octet-stream",
            )


async def _render_sse(batch: list[dict] | None) -> AsyncIterator[bytes]:
    """把脚本化的事件批次渲染为 SSE 字节流。

    ``None`` 表示模拟"服务端在 yield 任何事件前关闭连接"——会触发 SDK 的
    transport-error 分支并重连。
    """
    if batch is None:
        # 没有 yield 任何 chunk 直接退出 generator —— 客户端会看到 EOF
        if False:
            yield b""
        return
    for ev in batch:
        line_id = f"id: {ev['id']}\n" if ev.get("id") is not None else ""
        line_event = f"event: {ev['event']}\n" if ev.get("event") else ""
        line_data = f"data: {json.dumps(ev.get('data', {}))}\n"
        chunk = (line_id + line_event + line_data + "\n").encode("utf-8")
        yield chunk
        # 让事件之间出现 await 间隙，更接近真实 SSE 行为
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def stub() -> StubWorker:
    return StubWorker()


@pytest.fixture
async def client(stub: StubWorker) -> AsyncIterator[AsyncWorkerClient]:
    transport = httpx.ASGITransport(app=stub.app)
    async with AsyncWorkerClient(
        base_url="http://stub.test",
        bearer_token="test-token",
        timeout=5.0,
        max_sse_reconnect_attempts=2,
        compatibility_check=False,
        transport=transport,
    ) as sdk:
        yield sdk


# ---------------------------------------------------------------------------
# Pure unit: error mapping, sse parser, compat
# ---------------------------------------------------------------------------

def test_http_error_mapping():
    assert http_error_for(401).__name__ == "WorkerUnauthorizedError"
    assert http_error_for(404).__name__ == "WorkerNotFoundError"
    assert http_error_for(409).__name__ == "WorkerConflictError"
    assert http_error_for(500).__name__ == "WorkerServerError"
    assert http_error_for(503).__name__ == "WorkerServerError"
    assert http_error_for(418).__name__ == "WorkerHTTPError"


def test_terminal_error_mapping():
    assert terminal_error_for("failed") is WorkerTaskFailed
    assert terminal_error_for("aborted") is WorkerTaskAborted
    assert terminal_error_for("completed") is None
    assert terminal_error_for("running") is None


def test_compat_matrix():
    assert is_compatible("0.1.0")
    assert is_compatible("0.1.5")
    assert not is_compatible("0.2.0")
    assert not is_compatible("1.0.0")
    assert not is_compatible("")
    assert not is_compatible("garbage")


def test_sse_parser_skips_missing_id():
    from httpx_sse import ServerSentEvent

    # heartbeat in worker.api.routes uses id=None (not assigned by DB) — should be skipped
    sse = ServerSentEvent(event="heartbeat", data='{"ts": 1.0}', id="", retry=None)
    assert _parse_sse_event(sse) is None


def test_sse_parser_decodes_event():
    from httpx_sse import ServerSentEvent

    sse = ServerSentEvent(
        event="task_started",
        data='{"foo": 1}',
        id="42",
        retry=None,
    )
    event = _parse_sse_event(sse)
    assert event is not None
    assert event.cursor == 42
    assert event.kind == "task_started"
    assert event.payload == {"foo": 1}


# ---------------------------------------------------------------------------
# Integration with stub: task CRUD + headers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_task_returns_handle(client: AsyncWorkerClient, stub: StubWorker):
    handle = await client.create_task({"mode": "plan_first", "messages": []})
    assert handle.task_id == "stub-task-1"
    assert handle.status == "queued"
    assert ("POST", "/tasks") in stub.calls


@pytest.mark.asyncio
async def test_create_task_409_conflict(client: AsyncWorkerClient, stub: StubWorker):
    stub.create_status_code = 409
    with pytest.raises(WorkerConflictError) as exc:
        await client.create_task({"mode": "plan_first"})
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_get_task_404(client: AsyncWorkerClient):
    with pytest.raises(WorkerNotFoundError):
        await client.get_task("non-existent")


@pytest.mark.asyncio
async def test_unauthorized_propagates_as_typed_error():
    """SDK 必须把 401 映射到 WorkerUnauthorizedError，而不是裸 HTTPError。"""
    app = FastAPI()

    @app.post("/tasks", status_code=201)
    async def handler():
        raise HTTPException(status_code=401, detail="missing token")

    async with AsyncWorkerClient(
        base_url="http://stub.test",
        bearer_token="t",
        compatibility_check=False,
        transport=httpx.ASGITransport(app=app),
    ) as sdk:
        with pytest.raises(WorkerUnauthorizedError):
            await sdk.create_task({})


# ---------------------------------------------------------------------------
# Compatibility check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compat_check_rejects_incompatible_worker(stub: StubWorker):
    stub.health_version = "0.2.0"
    transport = httpx.ASGITransport(app=stub.app)
    async with AsyncWorkerClient(
        base_url="http://stub.test",
        bearer_token="t",
        compatibility_check=True,
        transport=transport,
    ) as sdk:
        with pytest.raises(WorkerCompatibilityError):
            await sdk.create_task({})


@pytest.mark.asyncio
async def test_compat_check_passes_and_short_circuits(stub: StubWorker):
    """assert_compatible 只应该被调用一次；后续请求短路。"""
    transport = httpx.ASGITransport(app=stub.app)
    async with AsyncWorkerClient(
        base_url="http://stub.test",
        bearer_token="t",
        compatibility_check=True,
        transport=transport,
    ) as sdk:
        await sdk.create_task({})
        await sdk.create_task({})
    health_calls = [c for c in stub.calls if c == ("GET", "/health")]
    assert len(health_calls) == 1


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_events_yields_until_terminal(
    client: AsyncWorkerClient, stub: StubWorker
):
    stub.sse_batches = [[
        {"id": 1, "event": "task_started", "data": {}},
        {"id": 2, "event": "assistant_delta", "data": {"content": "hi"}},
        {"id": 3, "event": "task_completed", "data": {}},
        # 这条不应该被消费——SDK 看到 terminal 后必须立即终止迭代
        {"id": 4, "event": "should_not_appear", "data": {}},
    ]]
    kinds = [ev.kind async for ev in client.stream_events("stub-task-1")]
    assert kinds == ["task_started", "assistant_delta", "task_completed"]


@pytest.mark.asyncio
async def test_stream_events_skips_heartbeat_by_default(
    client: AsyncWorkerClient, stub: StubWorker
):
    stub.sse_batches = [[
        # heartbeat 在真实 Worker 里没有 id（DB 不写）→ 用空 id 模拟
        {"id": "", "event": "heartbeat", "data": {"ts": 1.0}},
        {"id": 1, "event": "task_started", "data": {}},
        {"id": 2, "event": "heartbeat", "data": {"ts": 2.0}},  # 带 id 的 hb 也要过滤
        {"id": 3, "event": "task_completed", "data": {}},
    ]]
    kinds = [ev.kind async for ev in client.stream_events("stub-task-1")]
    assert kinds == ["task_started", "task_completed"]


@pytest.mark.asyncio
async def test_stream_events_include_heartbeats(
    client: AsyncWorkerClient, stub: StubWorker
):
    stub.sse_batches = [[
        {"id": 1, "event": "heartbeat", "data": {"ts": 1.0}},
        {"id": 2, "event": "task_completed", "data": {}},
    ]]
    kinds = [
        ev.kind
        async for ev in client.stream_events("stub-task-1", include_heartbeats=True)
    ]
    assert kinds == ["heartbeat", "task_completed"]


@pytest.mark.asyncio
async def test_sse_auto_reconnect_uses_last_event_id(
    client: AsyncWorkerClient, stub: StubWorker
):
    """第一次连接喂两条事件然后断开，第二次重连必须带 Last-Event-ID=2。"""
    stub.sse_batches = [
        [
            {"id": 1, "event": "task_started", "data": {}},
            {"id": 2, "event": "assistant_delta", "data": {"content": "hi"}},
        ],
        [
            {"id": 3, "event": "task_completed", "data": {}},
        ],
    ]
    kinds = [ev.kind async for ev in client.stream_events("stub-task-1")]
    assert kinds == ["task_started", "assistant_delta", "task_completed"]
    assert stub.last_event_id_received == [None, "2"]


@pytest.mark.asyncio
async def test_sse_reconnect_exhaustion_raises(stub: StubWorker):
    """超过 max_sse_reconnect_attempts 仍未拿到 terminal → WorkerSSEError。"""
    stub.sse_batches = [
        [],  # 立即 EOF
        [],
        [],  # 第 3 个 batch 是兜底——max_attempts=1 时不应该被消费
    ]
    transport = httpx.ASGITransport(app=stub.app)
    async with AsyncWorkerClient(
        base_url="http://stub.test",
        bearer_token="t",
        max_sse_reconnect_attempts=1,
        compatibility_check=False,
        transport=transport,
    ) as sdk:
        with pytest.raises(WorkerSSEError):
            async for _ in sdk.stream_events("stub-task-1"):
                pass


@pytest.mark.asyncio
async def test_sse_no_auto_reconnect_returns_on_eof(stub: StubWorker):
    """auto_resume=False 时遇到提前断开应该正常结束（而非抛错）。"""
    stub.sse_batches = [
        [{"id": 1, "event": "task_started", "data": {}}],
    ]
    transport = httpx.ASGITransport(app=stub.app)
    async with AsyncWorkerClient(
        base_url="http://stub.test",
        bearer_token="t",
        compatibility_check=False,
        transport=transport,
    ) as sdk:
        kinds = [
            ev.kind async for ev in sdk.stream_events("stub-task-1", auto_resume=False)
        ]
    assert kinds == ["task_started"]


# ---------------------------------------------------------------------------
# wait_until_terminal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wait_until_terminal_returns_result(
    client: AsyncWorkerClient, stub: StubWorker
):
    stub.task["status"] = "completed"
    stub.sse_batches = [[
        {"id": 1, "event": "task_started", "data": {}},
        {"id": 2, "event": "task_completed", "data": {"summary": "ok"}},
    ]]
    result = await client.wait_until_terminal("stub-task-1")
    assert result.final_status == "completed"
    assert result.terminal_event is not None
    assert result.terminal_event.kind == "task_completed"
    assert result.task_snapshot["status"] == "completed"


@pytest.mark.asyncio
async def test_wait_until_terminal_raise_on_failure(
    client: AsyncWorkerClient, stub: StubWorker
):
    stub.task["status"] = "failed"
    stub.sse_batches = [[
        {"id": 1, "event": "task_failed", "data": {"error": {"code": "boom"}}},
    ]]
    with pytest.raises(WorkerTaskFailed) as exc:
        await client.wait_until_terminal("stub-task-1", raise_on_failure=True)
    assert exc.value.final_status == "failed"
    assert exc.value.task_id == "stub-task-1"
    assert exc.value.terminal_event is not None


@pytest.mark.asyncio
async def test_wait_until_terminal_timeout():
    """SDK 等待超时应该抛 WorkerClientError；用一个永不进入终态的 stub 模拟。"""
    app = FastAPI()

    @app.get("/tasks/{task_id}/events")
    async def events_endpoint(task_id: str):
        async def gen():
            # 一直发 keep-alive comment line，永远不推 terminal event
            while True:
                yield b": keep-alive\n\n"
                await asyncio.sleep(0.05)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str):
        return {"task_id": task_id, "status": "executing"}

    transport = httpx.ASGITransport(app=app)
    async with AsyncWorkerClient(
        base_url="http://stub.test",
        bearer_token="t",
        compatibility_check=False,
        transport=transport,
    ) as sdk:
        from worker_sdk.errors import WorkerClientError as _WorkerClientError

        with pytest.raises(_WorkerClientError):
            await sdk.wait_until_terminal("stub-task-1", timeout=0.3)


# ---------------------------------------------------------------------------
# HITL decisions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_submit_decision_generates_idempotency_key(
    client: AsyncWorkerClient, stub: StubWorker
):
    await client.submit_decision(
        "stub-task-1",
        decision_id="d-1",
        choice="approve",
    )
    payload = stub.decision_payloads[-1]
    assert payload["decision_id"] == "d-1"
    assert payload["choice"] == "approve"
    assert payload["idempotency_key"]
    assert len(payload["idempotency_key"]) >= 16


@pytest.mark.asyncio
async def test_submit_decision_preserves_explicit_idempotency_key(
    client: AsyncWorkerClient, stub: StubWorker
):
    await client.submit_decision(
        "stub-task-1",
        decision_id="d-2",
        choice="revise",
        feedback="please add error handling",
        idempotency_key="user-supplied-1",
    )
    payload = stub.decision_payloads[-1]
    assert payload["idempotency_key"] == "user-supplied-1"
    assert payload["feedback"] == "please add error handling"


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_artifacts_parses_metadata(
    client: AsyncWorkerClient, stub: StubWorker
):
    refs = await client.list_artifacts("stub-task-1")
    assert len(refs) == 1
    assert refs[0].artifact_id == "art-1"
    assert refs[0].type == "log"
    assert refs[0].metadata == {"interceptor": "conversations"}


@pytest.mark.asyncio
async def test_download_artifact_bytes(client: AsyncWorkerClient):
    data = await client.download_artifact_bytes("stub-task-1", "art-1")
    assert data == b"artifact-bytes"


@pytest.mark.asyncio
async def test_download_artifact_to_file(
    client: AsyncWorkerClient, stub: StubWorker, tmp_path: Path
):
    source = tmp_path / "src.bin"
    source.write_bytes(b"hello" * 100)
    stub.artifact_file = source

    dest = tmp_path / "subdir" / "out.bin"
    written = await client.download_artifact_to("stub-task-1", "art-1", str(dest))
    assert Path(written).exists()
    assert Path(written).read_bytes() == b"hello" * 100
    # 不能留下 .part 半成品文件
    assert not Path(str(dest) + ".part").exists()


@pytest.mark.asyncio
async def test_download_artifact_to_refuses_overwrite(
    client: AsyncWorkerClient, tmp_path: Path
):
    dest = tmp_path / "exists.bin"
    dest.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        await client.download_artifact_to(
            "stub-task-1", "art-1", str(dest), overwrite=False
        )


def test_validate_artifact_id_rejects_path_traversal():
    with pytest.raises(ValueError):
        AsyncWorkerClient._validate_artifact_id("../etc/passwd")
    with pytest.raises(ValueError):
        AsyncWorkerClient._validate_artifact_id("")
    # legitimate ids are accepted
    AsyncWorkerClient._validate_artifact_id("art-1")
    AsyncWorkerClient._validate_artifact_id("a.b_c-1")


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_and_wait(client: AsyncWorkerClient, stub: StubWorker):
    stub.task["status"] = "completed"
    stub.sse_batches = [[
        {"id": 1, "event": "task_completed", "data": {}},
    ]]
    result = await client.create_and_wait({"mode": "plan_first"})
    assert result.final_status == "completed"


# ---------------------------------------------------------------------------
# Defensive-path coverage (gaps surfaced by /sc:test)
# ---------------------------------------------------------------------------

def test_constructor_rejects_empty_base_url():
    """空 base_url 在构造期就要被拒，避免后续请求拼接出意外路径。"""
    with pytest.raises(ValueError, match="base_url"):
        AsyncWorkerClient(base_url="", bearer_token="t")


def test_constructor_rejects_empty_bearer_token():
    """空 token 立即拒绝；让上游早一步发现配置错误而不是收到一片 401。"""
    with pytest.raises(ValueError, match="bearer_token"):
        AsyncWorkerClient(base_url="http://x", bearer_token="")


@pytest.mark.asyncio
async def test_abort_task_returns_server_response(
    client: AsyncWorkerClient, stub: StubWorker
):
    """abort_task 走 POST /tasks/{id}/abort，必须把服务端响应原样回传。"""
    result = await client.abort_task("stub-task-1")
    assert result == {"aborted": True, "task_id": "stub-task-1"}
    assert ("POST", "/tasks/stub-task-1/abort") in stub.calls


@pytest.mark.asyncio
async def test_submit_decision_passes_patch_through(
    client: AsyncWorkerClient, stub: StubWorker
):
    """patch 是 revise 决策的结构化修订内容，必须原样透传给服务端。"""
    await client.submit_decision(
        "stub-task-1",
        decision_id="d-3",
        choice="revise",
        feedback="tighten plan step 2",
        patch={"plan_step": 2, "replacement": "use mock instead of real DB"},
    )
    payload = stub.decision_payloads[-1]
    assert payload["patch"] == {
        "plan_step": 2,
        "replacement": "use mock instead of real DB",
    }


@pytest.mark.asyncio
async def test_list_artifacts_rejects_non_array_response():
    """服务端契约要求 list_artifacts 返回 JSON array；其它类型应抛 typed error
    而不是裸 AttributeError/TypeError，便于上游捕获。"""
    app = FastAPI()

    @app.get("/tasks/{task_id}/artifacts")
    async def malformed(task_id: str):
        # 返回一个 dict 而不是 list —— 模拟代理改写或服务端 bug
        return {"oops": "not a list"}

    transport = httpx.ASGITransport(app=app)
    async with AsyncWorkerClient(
        base_url="http://stub.test",
        bearer_token="t",
        compatibility_check=False,
        transport=transport,
    ) as sdk:
        with pytest.raises(WorkerClientError, match="expected JSON array"):
            await sdk.list_artifacts("some-task")
