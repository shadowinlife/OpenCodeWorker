"""
asyncio 任务队列：调度与并发控制。

架构职责：
    此模块是 Worker 进程内的任务调度核心。它维护一个无界 asyncio.Queue，
    由 enqueue_task() 向队列投递 task_id，后台 _worker_loop() 协程持续消费。
    asyncio.Semaphore 限制同时运行的任务数（max_concurrent_tasks）。

Phase 1 行为（stub）：
    _task_executor 尚未注册时，任务会经历以下状态流转后直接完成：
        queued → preparing_workspace → completed
    这使 API 骨架可以在 Phase 2（Orchestrator 实现）之前完整运行和测试。

Phase 2 集成方式：
    from worker.orchestrator.queue import set_executor
    set_executor(my_orchestrator.run_task)
    # run_task(task_id: str) -> None 由 Orchestrator 实现完整的
    # workspace 准备 → 容器启动 → opencode 驱动 → 产物收集 流程

[REVIEW: P1-16] queue 不再代写 `starting_container` 状态/`task_started` 事件。
    队列只负责取队 + semaphore + 终态/指标回收；状态机由 orchestrator 完整驱动，
    避免出现 queue 写 starting_container → orchestrator 又改写 preparing_workspace
    的状态倒退。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from worker.config import get_settings
from worker.contract.event import TaskEventKind
from worker.contract.exceptions import TaskAbortedError, TaskTimedOutError
from worker.contract.task import TaskStatus
from worker.observability import metrics
from worker.orchestrator import event_bus
from worker.storage.db import get_db
from worker.storage.repo import discard_task_locks, insert_event, update_task_status

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

    执行流程：
        1. 等待 semaphore（若并发满则阻塞）
        2. 调用注册的 executor（orchestrator.run_task）；executor 内部
           完整驱动状态机：preparing_workspace → starting_container →
           starting_opencode → ... → completed。
        3. 异常分发到对应终态：
            TaskTimedOutError → timed_out + task_timed_out
            TaskAbortedError  → aborted   + task_aborted
            其它 Exception    → failed    + task_failed

    [REVIEW: P1-16] queue 不再代写状态/起始事件，避免与 orchestrator 双写。
    """
    assert _semaphore is not None, "start_queue_worker() 未被调用"
    async with _semaphore:
        db = await get_db()
        logger.info("task %s: starting execution slot", task_id)

        # P1-11：进入活跃槽位 + 起始时间，用于指标统计
        metrics.inc_active_tasks()
        task_start_monotonic = time.monotonic()

        try:
            try:
                if _task_executor is not None:
                    await _task_executor(task_id)
                else:
                    # ── Phase 1 stub ──────────────────────────────────────
                    # 没有真实执行器时，模拟一个成功完成的任务。
                    # 此分支在 Phase 2 注册 executor 后永远不会执行。
                    logger.warning(
                        "task %s: executor not registered, completing as stub",
                        task_id,
                    )
                    await update_task_status(db, task_id, TaskStatus.completed)
                    await insert_event(db, task_id, TaskEventKind.task_completed)
                # 成功路径：executor 自己写了 task_completed
                metrics.inc_task_count(TaskStatus.completed.value)
                metrics.observe_task_duration(time.monotonic() - task_start_monotonic)
            except TaskTimedOutError as exc:
                logger.warning("task %s timed out: %s", task_id, exc)
                await _write_terminal(
                    task_id,
                    TaskStatus.timed_out,
                    TaskEventKind.task_timed_out,
                    {"timeout_sec": exc.timeout_sec, "message": str(exc)},
                )
                metrics.inc_task_count(TaskStatus.timed_out.value)
                metrics.observe_task_duration(time.monotonic() - task_start_monotonic)
            except TaskAbortedError as exc:
                logger.info("task %s aborted (reason=%s): %s",
                            task_id, exc.reason, exc)
                payload: dict = {"reason": exc.reason, "message": str(exc)}
                if exc.decision_id is not None:
                    payload["decision_id"] = exc.decision_id
                await _write_terminal(
                    task_id,
                    TaskStatus.aborted,
                    TaskEventKind.task_aborted,
                    payload,
                )
                metrics.inc_task_count(TaskStatus.aborted.value)
                metrics.inc_abort_count(exc.reason or "unknown")
                metrics.observe_task_duration(time.monotonic() - task_start_monotonic)
            except Exception as exc:
                logger.exception("task %s execution failed: %s", task_id, exc)
                await _write_terminal(
                    task_id,
                    TaskStatus.failed,
                    TaskEventKind.task_failed,
                    {"error": str(exc), "error_type": type(exc).__name__},
                )
                metrics.inc_task_count(TaskStatus.failed.value)
                metrics.observe_task_duration(time.monotonic() - task_start_monotonic)
        finally:
            # P1-10：成功路径（executor 自己写 task_completed，不走 _write_terminal）
            # 也要释放 per-task event 锁，避免 _event_locks dict 长期增长
            discard_task_locks(task_id)
            # P1-11：退出活跃槽位
            metrics.dec_active_tasks()
            # P1-12：释放 SSE 总线条目（残留订阅者会从终态事件自然退出）
            event_bus.discard(task_id)


async def _write_terminal(
    task_id: str,
    status: TaskStatus,
    event_kind: TaskEventKind,
    payload: dict,
) -> None:
    """写入终态状态 + 终态事件，吞下任何 DB 错误（最后一道防线）。

    P1-10：终态写入完成后释放 per-task event 锁，避免 _event_locks dict 长期增长。
    """
    try:
        db = await get_db()
        await update_task_status(db, task_id, status)
        await insert_event(db, task_id, event_kind, payload)
    except Exception:
        # 连 DB 写入都失败时只能记录日志，不能再抛出（会静默退出）
        logger.exception(
            "task %s: failed to write terminal event %s",
            task_id, event_kind.value,
        )
    finally:
        discard_task_locks(task_id)
