"""
单元测试：P1-16 队列状态流转不再双写

验证：
    - queue._run_one 不再代写 starting_container 状态
    - queue._run_one 不再代写 task_started 事件
    - 仅注册的 executor（orchestrator.run_task 替身）写状态机
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from worker.contract.event import TaskEventKind
from worker.contract.task import Message, TaskMode, TaskRequest, TaskStatus
from worker.observability import metrics
from worker.orchestrator import queue as queue_module
from worker.storage import db as db_module
from worker.storage.repo import insert_task


@pytest.fixture
async def temp_db(tmp_path: Path):
    db_file = tmp_path / "p1_16.db"
    conn = await db_module.init_db(db_file)
    try:
        yield conn
    finally:
        await db_module.close_db()


@pytest.fixture
def reset_queue_state():
    saved_executor = queue_module._task_executor
    saved_semaphore = queue_module._semaphore
    queue_module._semaphore = asyncio.Semaphore(1)
    yield
    queue_module._task_executor = saved_executor
    queue_module._semaphore = saved_semaphore


@pytest.fixture
def reset_metrics():
    metrics._task_count.clear()
    metrics._abort_count.clear()
    metrics._active_tasks = 0
    metrics._task_duration_sum = 0.0
    metrics._task_duration_count = 0
    yield


async def _create_task(db) -> str:
    req = TaskRequest(
        mode=TaskMode.direct_execute,
        messages=[Message(role="user", content="x")],
    )
    resp = await insert_task(db, req)
    return resp.task_id


async def _read_status(db, task_id: str) -> str:
    async with db.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,),
    ) as cur:
        row = await cur.fetchone()
    return row["status"]


async def _read_event_kinds(db, task_id: str) -> list[str]:
    async with db.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY event_id ASC",
        (task_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [r["kind"] for r in rows]


async def test_queue_does_not_write_starting_container_or_task_started(
    temp_db, reset_queue_state, reset_metrics
):
    """executor 故意不写任何状态；queue 完成后状态应仍是初始 pending、
    且没有 task_started 事件——证明 queue 不再代写。"""
    task_id = await _create_task(temp_db)
    seen: list[str] = []

    async def passive_executor(_tid: str) -> None:
        # 模拟 orchestrator 启动前异常，未写任何状态
        seen.append("called")

    queue_module.set_executor(passive_executor)
    await queue_module._run_one(task_id)

    assert seen == ["called"]
    # P1-16：queue 不再代写 starting_container
    status = await _read_status(temp_db, task_id)
    assert status == TaskStatus.pending.value
    # P1-16：queue 不再代写 task_started
    kinds = await _read_event_kinds(temp_db, task_id)
    assert TaskEventKind.task_started.value not in kinds


async def test_executor_owns_state_machine(temp_db, reset_queue_state, reset_metrics):
    """executor 自行驱动 preparing_workspace → completed。"""
    task_id = await _create_task(temp_db)

    async def executor(tid: str) -> None:
        from worker.storage.repo import insert_event, update_task_status
        await update_task_status(temp_db, tid, TaskStatus.preparing_workspace)
        await insert_event(
            temp_db, tid, TaskEventKind.task_started,
            {"phase": "preparing_workspace"},
        )
        await update_task_status(temp_db, tid, TaskStatus.completed)
        await insert_event(temp_db, tid, TaskEventKind.task_completed)

    queue_module.set_executor(executor)
    await queue_module._run_one(task_id)

    assert await _read_status(temp_db, task_id) == TaskStatus.completed.value
    kinds = await _read_event_kinds(temp_db, task_id)
    # task_started 仅出现一次（来自 executor），不会被 queue 重复
    assert kinds.count(TaskEventKind.task_started.value) == 1
    assert TaskEventKind.task_completed.value in kinds
