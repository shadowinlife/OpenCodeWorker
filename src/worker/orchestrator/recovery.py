"""
P1-17 — Worker 重启时的孤儿任务恢复。

reaper（`sandbox.manager.reap_orphaned_containers`）只清理有容器关联的任务；
而 `status='queued'`（容器未起）/ `'preparing_workspace'`（容器准备中失败）等
无容器的非终态任务在 worker 重启后既不会被入队也不会被标记终态——会永远卡死。

本模块提供兜底扫描：所有非终态任务在重启时统一标 `failed(orphaned)` +
`task_failed` 事件（reason=orphaned_after_worker_restart），与 reaper 协同。

被本函数处理的任务包含：
    - reaper 已经处理过（有容器）的：本函数会跳过，避免重复事件
    - 无容器的孤儿（reaper 漏掉的）：本函数兜底
"""
from __future__ import annotations

import logging
from typing import Iterable

from worker.contract.event import TaskEventKind
from worker.contract.task import TaskStatus
from worker.storage.db import get_db
from worker.storage.repo import (
    insert_event,
    list_non_terminal_tasks,
    update_task_status,
)

logger = logging.getLogger(__name__)


async def recover_orphaned_tasks(reaped_task_ids: Iterable[str]) -> list[str]:
    """扫描非终态任务，将未被 reaper 处理的标记为 failed(orphaned)。

    Args:
        reaped_task_ids: 已被 reaper 处理过的 task_ids（避免重复写终态事件）。

    Returns:
        本函数额外标记的 task_ids 列表（不含 reaper 已处理的）。
    """
    reaped_set = set(reaped_task_ids)
    db = await get_db()
    non_terminal = await list_non_terminal_tasks(db)

    recovered: list[str] = []
    for task_id, _container_id in non_terminal:
        if task_id in reaped_set:
            continue
        try:
            await update_task_status(db, task_id, TaskStatus.failed)
            await insert_event(
                db, task_id, TaskEventKind.task_failed,
                {
                    "error": "orphaned_after_worker_restart",
                    "error_type": "WorkerRestartOrphan",
                },
            )
            recovered.append(task_id)
        except Exception:
            logger.exception(
                "task %s: failed to write orphan terminal event",
                task_id,
            )

    if recovered:
        logger.info(
            "recovered %d orphaned non-terminal task(s) on startup: %s",
            len(recovered), recovered,
        )
    return recovered
