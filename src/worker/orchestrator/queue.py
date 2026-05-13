"""
asyncio 任务队列：调度与并发控制。

架构职责：
    此模块是 Worker 进程内的任务调度核心。它维护一个无界 asyncio.Queue，
    由 enqueue_task() 向队列投递 task_id，后台 _worker_loop() 协程持续消费。
    asyncio.Semaphore 限制同时运行的任务数（max_concurrent_tasks）。

Phase 1 行为（stub）：
    _task_executor 尚未注册时，任务会经历以下状态流转后直接完成：
        queued → starting_container → completed
    这使 API 骨架可以在 Phase 2（Orchestrator 实现）之前完整运行和测试。

Phase 2 集成方式：
    from worker.orchestrator.queue import set_executor
    set_executor(my_orchestrator.run_task)
    # run_task(task_id: str) -> None 由 Orchestrator 实现完整的
    # workspace 准备 → 容器启动 → opencode 驱动 → 产物收集 流程
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from worker.config import get_settings
from worker.contract.event import TaskEventKind
from worker.contract.task import TaskStatus
from worker.storage.db import get_db
from worker.storage.repo import insert_event, update_task_status

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 全局状态（进程单例）
# ---------------------------------------------------------------------------

# 无界队列，存放待处理的 task_id 字符串
_queue: asyncio.Queue[str] = asyncio.Queue()

# 并发信号量，在 start_queue_worker() 初始化时按配置值创建
_semaphore: Optional[asyncio.Semaphore] = None

# 任务执行器回调，由 Phase 2 Orchestrator 通过 set_executor() 注入
# 签名：async def execute(task_id: str) -> None
_task_executor: Optional[Callable[[str], Awaitable[None]]] = None


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------

def set_executor(fn: Callable[[str], Awaitable[None]]) -> None:
    """注册任务执行器（Phase 2 Orchestrator 启动时调用）。

    fn 必须是一个接受 task_id（str）的 async 函数，负责完整执行任务，
    包括容器启动、opencode 驱动、产物收集，以及所有状态更新和事件写入。
    fn 内部抛出的异常会被 _run_one() 捕获并写入 task_failed 事件。
    """
    global _task_executor
    _task_executor = fn
    logger.info("task executor registered: %s", fn)


async def enqueue_task(task_id: str) -> None:
    """将 task_id 投入调度队列，并将 DB 中的任务状态更新为 queued。

    此函数在 POST /tasks 成功写库后立即调用，保证任务状态与队列状态一致。
    若进程在 enqueue 和 _worker_loop 消费之间崩溃，重启后需要恢复机制
    （Phase 4 会扫描 DB 中 status='queued' 的任务重新投队）。
    """
    db = await get_db()
    await update_task_status(db, task_id, TaskStatus.queued)
    await insert_event(db, task_id, TaskEventKind.task_queued)
    await _queue.put(task_id)
    logger.info("task %s enqueued (queue_size=%d)", task_id, _queue.qsize())


async def start_queue_worker() -> asyncio.Task:
    """启动后台队列消费协程，在 FastAPI lifespan 开始时调用。

    Returns:
        asyncio.Task: 后台任务句柄，lifespan 退出时可 cancel() 使其停止。
    """
    global _semaphore
    settings = get_settings()
    _semaphore = asyncio.Semaphore(settings.max_concurrent_tasks)
    logger.info(
        "queue worker starting, max_concurrent_tasks=%d",
        settings.max_concurrent_tasks,
    )
    return asyncio.create_task(_worker_loop(), name="queue-worker")


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------

async def _worker_loop() -> None:
    """持续从队列取 task_id 并派发给 _run_one()。

    使用 asyncio.create_task() 非阻塞地启动每个任务，
    Semaphore 在 _run_one() 内部控制实际并发上限。
    """
    while True:
        task_id = await _queue.get()
        # 非阻塞派发：_run_one 会在获取 semaphore 后真正开始执行
        asyncio.create_task(_run_one(task_id), name=f"task-{task_id[:8]}")


async def _run_one(task_id: str) -> None:
    """在 semaphore 槽位内执行单个任务。

    执行流程（Phase 1 stub）：
        1. 等待 semaphore（若并发满则阻塞）
        2. 更新状态为 starting_container，发出 task_started 事件
        3. 若有注册的 executor 则调用；否则走 stub 路径直接完成
        4. 异常时标记 failed 并写 task_failed 事件
    """
    assert _semaphore is not None, "start_queue_worker() 未被调用"
    async with _semaphore:
        db = await get_db()
        logger.info("task %s: starting execution slot", task_id)
        await update_task_status(db, task_id, TaskStatus.starting_container)
        await insert_event(db, task_id, TaskEventKind.task_started)
        try:
            if _task_executor is not None:
                await _task_executor(task_id)
            else:
                # ── Phase 1 stub ──────────────────────────────────────────
                # 没有真实执行器时，模拟一个成功完成的任务。
                # 此分支在 Phase 2 注册 executor 后永远不会执行。
                logger.warning(
                    "task %s: executor not registered, completing as stub", task_id
                )
                await update_task_status(db, task_id, TaskStatus.completed)
                await insert_event(db, task_id, TaskEventKind.task_completed)
        except Exception as exc:
            logger.exception("task %s execution failed: %s", task_id, exc)
            db2 = await get_db()  # 防止 exc 是 DB 连接错误导致 db 不可用
            try:
                await update_task_status(db2, task_id, TaskStatus.failed)
                await insert_event(
                    db2,
                    task_id,
                    TaskEventKind.task_failed,
                    {"error": str(exc), "error_type": type(exc).__name__},
                )
            except Exception:
                # 连 DB 写入都失败时只能记录日志，不能再抛出（会静默退出）
                logger.exception("task %s: failed to write failure event", task_id)
