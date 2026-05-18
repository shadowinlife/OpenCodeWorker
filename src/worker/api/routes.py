"""
Worker HTTP API 路由定义。

端点清单：
    GET  /health                             — 健康检查（公开）
    GET  /ready                              — 就绪检查（DB 可用，公开）
    POST /tasks                              — 提交任务
    GET  /tasks/{task_id}                    — 查询任务状态
    GET  /tasks/{task_id}/events             — SSE 实时事件流（支持 Last-Event-ID）
    POST /tasks/{task_id}/decisions          — 提交 HITL 决策
    POST /tasks/{task_id}/abort              — 中止任务
    GET  /tasks/{task_id}/artifacts          — 列出产物
    GET  /tasks/{task_id}/artifacts/{artifact_id} — 下载产物文件
    DELETE /tasks/{task_id}                  — 删除任务（含产物）

SSE 实现细节：
    - 使用 sse-starlette 的 EventSourceResponse，自动处理 SSE 格式化。
    - 支持 Last-Event-ID 断线重连：重连时先从 DB 补发历史事件，
      再切换为实时事件推送。
    - heartbeat 事件每 sse_heartbeat_sec 秒发送一次，防止代理断连。
    - 任务进入终态后，推完最后一条事件即关闭 SSE 连接。

版本信息：
    当前为 Phase 1 骨架。HITL decisions、artifacts 下载、DELETE 等端点
    已有完整 DB 操作，但 Orchestrator 尚未注入真实的容器逻辑（Phase 2）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from worker.contract.decision import DecisionResponse
from worker.contract.event import TERMINAL_EVENT_KINDS, TaskEventKind
from worker.contract.task import TaskRequest, TaskStatus
from worker.orchestrator.queue import enqueue_task
from worker.storage.db import get_db
from worker.storage.repo import (
    delete_task,
    get_artifact_path,
    get_events_after,
    get_task,
    insert_event,
    insert_task,
    list_artifacts,
    resolve_decision,
    update_task_status,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Worker 版本号，随 pyproject.toml 保持同步（Phase 5 可改为读 importlib.metadata）
_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# 健康 / 就绪检查（公开，无需鉴权）
# ---------------------------------------------------------------------------

@router.get("/health", tags=["probe"])
async def health() -> dict:
    """健康检查，只要进程运行中就返回 200。

    负载均衡器或 Docker healthcheck 可以轮询此端点。
    """
    return {"status": "ok", "version": _VERSION}


@router.get("/ready", tags=["probe"])
async def ready() -> dict:
    """就绪检查，验证 SQLite 连接可用。

    Kubernetes readinessProbe 或负载均衡器可用此端点判断是否可以接受流量。
    DB 不可用时返回 503。
    """
    try:
        db = await get_db()
        # 轻量级探针：执行一条不产生 IO 的 SQL
        async with db.execute("SELECT 1") as cur:
            await cur.fetchone()
        return {"status": "ready", "version": _VERSION}
    except Exception as exc:
        logger.error("readiness check failed: %s", exc)
        raise HTTPException(status_code=503, detail="database unavailable")


# ---------------------------------------------------------------------------
# 任务管理
# ---------------------------------------------------------------------------

@router.post("/tasks", status_code=201, tags=["tasks"])
async def create_task(request: TaskRequest) -> dict:
    """提交一个新的 AI 编程任务。

    成功后返回 201 和任务快照（task_id、status=pending 等）。
    任务会被持久化到 SQLite 并异步入队，调用方应通过
    GET /tasks/{task_id}/events SSE 端点监听进度。

    幂等性：调用方可在 TaskRequest.task_id 中指定 UUID 实现幂等重提交。
    相同 task_id 再次提交时返回 409 Conflict。
    """
    db = await get_db()

    # 检查 task_id 是否已存在（幂等保护）
    existing = await get_task(db, request.task_id)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"task {request.task_id} already exists (status={existing.status})",
        )

    # 持久化任务并写入 task_created 事件
    task_resp = await insert_task(db, request)
    await insert_event(db, task_resp.task_id, TaskEventKind.task_created)

    # 异步入队（写 queued 状态 + task_queued 事件 + 投入 asyncio.Queue）
    await enqueue_task(task_resp.task_id)

    logger.info("task created: %s (mode=%s)", task_resp.task_id, request.mode)
    return task_resp.model_dump()


@router.get("/tasks/{task_id}", tags=["tasks"])
async def get_task_status(task_id: str) -> dict:
    """查询任务当前状态快照。

    返回 TaskResponse 序列化后的 JSON，包含 status、mode、时间戳
    和可选的 container_id / opencode_session_id。
    任务不存在时返回 404。
    """
    db = await get_db()
    task = await get_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    return task.model_dump()


@router.delete("/tasks/{task_id}", status_code=204, tags=["tasks"])
async def remove_task(task_id: str) -> None:
    """硬删除任务及其所有关联数据（事件、决策、产物记录）。

    注意：只删除 DB 记录，不删除产物文件（文件清理由定时任务负责）。
    进行中的任务不能直接删除，需先调用 POST /tasks/{task_id}/abort。
    已删除任务再次请求时返回 404。
    """
    db = await get_db()
    task = await get_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")

    # 进行中的任务禁止直接删除
    non_terminal = {
        TaskStatus.pending, TaskStatus.queued, TaskStatus.preparing_workspace,
        TaskStatus.starting_container, TaskStatus.starting_opencode,
        TaskStatus.planning, TaskStatus.awaiting_human, TaskStatus.revising,
        TaskStatus.executing, TaskStatus.collecting_artifacts,
    }
    if task.status in non_terminal:
        raise HTTPException(
            status_code=409,
            detail=f"task {task_id} is {task.status}, call abort first",
        )

    await delete_task(db, task_id)
    logger.info("task deleted: %s", task_id)


# ---------------------------------------------------------------------------
# SSE 事件流
# ---------------------------------------------------------------------------

@router.get("/tasks/{task_id}/events", tags=["events"])
async def task_events(task_id: str, request: Request):
    """SSE 实时事件流，推送任务生命周期事件。

    断线重连：
        客户端在 Last-Event-ID 请求头中携带上次收到的 cursor 值（event_id）。
        端点先从 DB 补发 event_id > last_cursor 的历史事件，再切换为实时推送。
        若首次连接则 last_cursor=0，从第一条事件开始推送。

    连接终止：
        1. 任务进入终态（completed/failed/aborted）时，推完终态事件后服务端主动关闭。
        2. 客户端主动断开（HTTP 连接关闭），asyncio 取消 generator 协程。

    heartbeat：
        每 sse_heartbeat_sec 秒推送一条 heartbeat 事件（payload={}），
        防止 Nginx/ALB 等代理因无数据而超时断连。
    """
    db = await get_db()
    task = await get_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")

    # 读取 Last-Event-ID 头，用于断线重连的历史事件回放
    last_cursor_str = request.headers.get("Last-Event-ID", "0")
    try:
        last_cursor = int(last_cursor_str)
    except ValueError:
        last_cursor = 0

    from worker.config import get_settings
    heartbeat_interval = get_settings().sse_heartbeat_sec

    async def event_generator() -> AsyncIterator[dict]:
        """生成 SSE 事件的异步生成器。

        先补发历史事件，再事件驱动地等待新事件或 heartbeat。
        遇到终态事件或客户端断开时退出。

        P1-12：实时推送阶段使用 `event_bus.subscribe()` 拿到专属唤醒 Event，
        replace 0.5s 轮询为 `asyncio.wait_for(sub.wait(), timeout=heartbeat)`。
        新事件 → < 1ms 唤醒；无事件 → heartbeat 间隔到时唤醒发心跳。
        """
        from worker.orchestrator import event_bus

        cursor = last_cursor

        # 先检查任务是否已终结——若已终结则直接补发全部历史后关闭
        already_terminal = task.status in {
            TaskStatus.completed, TaskStatus.failed,
            TaskStatus.aborted, TaskStatus.timed_out,
        }

        # P1-12：在 history replay 之前先订阅，避免 replay 与新事件之间的窗口期
        # 错过 notify（即便错过，下次 heartbeat 仍会重新拉 DB 兜底）
        bus = event_bus.get_bus(task_id)
        subscriber = bus.subscribe()

        try:
            # 补发历史事件（event_id > cursor 的部分）
            history = await get_events_after(db, task_id, cursor)
            for ev in history:
                # 检查客户端是否已断开
                if await request.is_disconnected():
                    logger.debug("SSE client disconnected during history replay: %s", task_id)
                    return
                cursor = ev.cursor
                yield _sse_dict(ev)
                # 若补发到终态事件则关闭
                if ev.kind in TERMINAL_EVENT_KINDS:
                    return

            # 若任务已终结且历史已全部补发，关闭
            if already_terminal:
                return

            # 实时推送阶段：事件驱动唤醒 + 定时 heartbeat 兜底
            last_heartbeat = time.monotonic()
            while True:
                if await request.is_disconnected():
                    logger.debug("SSE client disconnected: %s", task_id)
                    return

                # 检查是否有新事件
                new_events = await get_events_after(db, task_id, cursor)
                for ev in new_events:
                    cursor = ev.cursor
                    yield _sse_dict(ev)
                    if ev.kind in TERMINAL_EVENT_KINDS:
                        return

                # 是否需要发 heartbeat
                now = time.monotonic()
                next_heartbeat_in = heartbeat_interval - (now - last_heartbeat)
                if next_heartbeat_in <= 0:
                    yield {
                        "event": "heartbeat",
                        "data": json.dumps({"ts": time.time()}),
                    }
                    last_heartbeat = now
                    next_heartbeat_in = heartbeat_interval

                # P1-12：等待 notify 或下次 heartbeat 到期
                # notify 来自 repo.insert_event 的 event_bus.notify(task_id)
                try:
                    await asyncio.wait_for(
                        subscriber.wait(), timeout=next_heartbeat_in,
                    )
                    subscriber.clear()
                except asyncio.TimeoutError:
                    # heartbeat 兜底兜醒，下个循环检测并发心跳
                    pass
        finally:
            bus.unsubscribe(subscriber)

    return EventSourceResponse(event_generator())


def _sse_dict(ev) -> dict:
    """将 TaskEvent 转为 EventSourceResponse 期望的格式字典。

    格式：
        id:    <cursor>
        event: <kind>
        data:  <payload_json>
    """
    return {
        "id": str(ev.cursor),
        "event": ev.kind.value,
        "data": json.dumps(ev.payload),
    }


# ---------------------------------------------------------------------------
# HITL 决策
# ---------------------------------------------------------------------------

@router.post("/tasks/{task_id}/decisions", tags=["hitl"])
async def submit_decision(task_id: str, response: DecisionResponse) -> dict:
    """提交人工决策，响应挂起的 HITL 请求。

    调用方需在 response.decision_id 中指定需要响应的决策 ID，
    该 ID 来自 hitl_required 事件的 payload.decision_id。

    幂等性：相同的 idempotency_key 不会被重复处理。
    决策不存在或已解析时返回 404 / 409。
    """
    db = await get_db()
    task = await get_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")

    updated = await resolve_decision(db, response.decision_id, response)
    if not updated:
        raise HTTPException(
            status_code=409,
            detail=f"decision {response.decision_id} not found or already resolved",
        )

    # 写入 decision_received 事件（Orchestrator 监听此事件后恢复任务）
    await insert_event(db, task_id, TaskEventKind.decision_received, {
        "decision_id": response.decision_id,
        "choice": response.choice.value,
    })

    logger.info("decision received: task=%s decision=%s choice=%s",
                task_id, response.decision_id, response.choice)
    return {"accepted": True, "decision_id": response.decision_id}


# ---------------------------------------------------------------------------
# 任务中止
# ---------------------------------------------------------------------------

@router.post("/tasks/{task_id}/abort", tags=["tasks"])
async def abort_task(task_id: str) -> dict:
    """主动中止任务。

    将任务状态更新为 aborted 并写入 task_aborted 终态事件。
    若任务已是终态（completed/failed/timed_out/aborted）则返回 409。

    注意：Phase 1 只做状态标记，不发送实际的容器停止信号（Phase 2 实现）。
    """
    db = await get_db()
    task = await get_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")

    terminal = {TaskStatus.completed, TaskStatus.failed,
                TaskStatus.aborted, TaskStatus.timed_out}
    if task.status in terminal:
        raise HTTPException(
            status_code=409,
            detail=f"task {task_id} is already in terminal state {task.status}",
        )

    await update_task_status(db, task_id, TaskStatus.aborted)
    await insert_event(db, task_id, TaskEventKind.task_aborted, {"reason": "user_requested"})
    logger.info("task aborted: %s", task_id)
    return {"aborted": True, "task_id": task_id}


# ---------------------------------------------------------------------------
# 产物
# ---------------------------------------------------------------------------

@router.get("/tasks/{task_id}/artifacts", tags=["artifacts"])
async def list_task_artifacts(task_id: str) -> list:
    """列出任务的所有产物元数据。

    返回 Artifact 列表，不包含文件内容。
    使用 GET /tasks/{task_id}/artifacts/{artifact_id} 下载具体文件。
    """
    db = await get_db()
    task = await get_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")

    artifacts = await list_artifacts(db, task_id)
    # 动态填充 download_url（不持久化到 DB，每次响应时构造）
    base = f"/tasks/{task_id}/artifacts"
    return [
        {**a.model_dump(), "download_url": f"{base}/{a.artifact_id}"}
        for a in artifacts
    ]


@router.get("/tasks/{task_id}/artifacts/{artifact_id}", tags=["artifacts"])
async def download_artifact(task_id: str, artifact_id: str):
    """下载单个产物文件。

    以 FileResponse 流式返回文件内容，避免将大文件加载进内存。
    文件路径由 storage.repo.get_artifact_path() 解析，
    不存在或已过期时返回 404。
    """
    db = await get_db()
    task = await get_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")

    file_path = await get_artifact_path(db, artifact_id, task_id)
    if file_path is None:
        raise HTTPException(
            status_code=404,
            detail=f"artifact {artifact_id} not found for task {task_id}",
        )

    # 安全检查：防止路径穿越——必须在受信任的 artifacts_dir 子树内
    from pathlib import Path
    from worker.config import get_settings
    settings = get_settings()
    artifacts_root = settings.artifacts_dir.resolve()
    resolved = Path(file_path).resolve()
    try:
        resolved.relative_to(artifacts_root)
    except ValueError:
        logger.warning(
            "artifact path escape blocked: artifact=%s task=%s path=%s root=%s",
            artifact_id, task_id, resolved, artifacts_root,
        )
        raise HTTPException(
            status_code=403,
            detail="artifact path is outside the trusted artifacts directory",
        )
    if not resolved.exists():
        raise HTTPException(
            status_code=404,
            detail=f"artifact file missing on disk: {artifact_id}",
        )

    return FileResponse(
        path=str(resolved),
        filename=resolved.name,
        media_type="application/octet-stream",
    )


# ---------------------------------------------------------------------------
# 可观测性：Prometheus metrics
# ---------------------------------------------------------------------------

@router.get("/metrics", tags=["probe"])
async def metrics():
    """Prometheus text format 0.0.4 metrics 端点。

    供 Prometheus scraper 或人工查看。
    返回 Content-Type: text/plain; version=0.0.4; charset=utf-8。
    """
    from fastapi.responses import PlainTextResponse
    from worker.observability.metrics import render_prometheus
    return PlainTextResponse(
        content=render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
