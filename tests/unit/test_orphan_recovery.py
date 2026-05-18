"""
单元测试：P1-17 重启时孤儿任务恢复

验证 `recover_orphaned_tasks(reaped)`:
    - 对每个非终态任务（除 reaped 集合外）：
        * 状态 → TaskStatus.failed
        * 写入 task_failed 事件，error=orphaned_after_worker_restart
    - 已在终态的任务：不重复处理
    - reaper 已处理的任务：跳过（不重复写终态事件）
    - DB 异常时：单条失败不影响后续任务恢复
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from worker.contract.task import TaskMode, TaskRequest, TaskStatus
from worker.orchestrator.recovery import recover_orphaned_tasks
from worker.storage import db as db_module
from worker.storage.repo import (
    get_task,
    insert_task,
    update_task_status,
)


@pytest.fixture
async def temp_db(tmp_path: Path):
    db_file = tmp_path / "orphan_test.db"
    conn = await db_module.init_db(db_file)
    try:
        yield conn
    finally:
        await db_module.close_db()


async def _make_task(db, status: TaskStatus = TaskStatus.pending) -> str:
    req = TaskRequest(mode=TaskMode.direct_execute, messages=[])
    resp = await insert_task(db, req)
    if status != TaskStatus.pending:
        await update_task_status(db, resp.task_id, status)
    return resp.task_id


async def _read_task_events(db, task_id: str) -> list[dict]:
    async with db.execute(
        "SELECT kind, payload_json FROM task_events "
        "WHERE task_id = ? ORDER BY event_id",
        (task_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [{"kind": r["kind"], "payload": json.loads(r["payload_json"])} for r in rows]


async def test_queued_task_recovered_to_failed(temp_db):
    """status='queued' 且无容器：被标记为 failed(orphaned)。"""
    task_id = await _make_task(temp_db, TaskStatus.queued)

    recovered = await recover_orphaned_tasks(reaped_task_ids=[])

    assert task_id in recovered
    task = await get_task(temp_db, task_id)
    assert task is not None
    assert task.status == TaskStatus.failed

    events = await _read_task_events(temp_db, task_id)
    assert events[-1]["kind"] == "task_failed"
    assert events[-1]["payload"]["error"] == "orphaned_after_worker_restart"
    assert events[-1]["payload"]["error_type"] == "WorkerRestartOrphan"


async def test_multiple_non_terminal_states_all_recovered(temp_db):
    """所有非终态任务都会被恢复，不限于 queued。"""
    states = [
        TaskStatus.queued,
        TaskStatus.preparing_workspace,
        TaskStatus.starting_container,
        TaskStatus.executing,
        TaskStatus.awaiting_human,
    ]
    task_ids = [await _make_task(temp_db, s) for s in states]

    recovered = await recover_orphaned_tasks(reaped_task_ids=[])

    assert set(recovered) == set(task_ids)
    for tid in task_ids:
        task = await get_task(temp_db, tid)
        assert task is not None
        assert task.status == TaskStatus.failed


async def test_terminal_tasks_not_touched(temp_db):
    """已终态任务不会被本函数处理。"""
    completed_id = await _make_task(temp_db, TaskStatus.completed)
    failed_id = await _make_task(temp_db, TaskStatus.failed)
    aborted_id = await _make_task(temp_db, TaskStatus.aborted)
    timed_out_id = await _make_task(temp_db, TaskStatus.timed_out)

    recovered = await recover_orphaned_tasks(reaped_task_ids=[])

    assert recovered == []
    # 验证状态没变
    for tid, expected in [
        (completed_id, TaskStatus.completed),
        (failed_id, TaskStatus.failed),
        (aborted_id, TaskStatus.aborted),
        (timed_out_id, TaskStatus.timed_out),
    ]:
        task = await get_task(temp_db, tid)
        assert task is not None
        assert task.status == expected


async def test_reaped_tasks_skipped(temp_db):
    """reaper 已处理过的 task_id 不再被本函数重复写终态。"""
    reaped_id = await _make_task(temp_db, TaskStatus.executing)
    other_id = await _make_task(temp_db, TaskStatus.queued)

    # 模拟 reaper 已经标记 reaped_id 为 failed
    await update_task_status(temp_db, reaped_id, TaskStatus.failed)

    recovered = await recover_orphaned_tasks(reaped_task_ids=[reaped_id])

    # reaped_id 已是终态，不应再出现在 recovered；本函数也不应试图重写
    # other_id 应被恢复
    assert recovered == [other_id]

    # reaped_id 只有 reaper 写的那一条 task_failed（如果有）—— 这里 reaper
    # 是 caller 模拟的，所以无事件；关键是本函数没追加事件
    reaped_events = await _read_task_events(temp_db, reaped_id)
    assert len(reaped_events) == 0


async def test_empty_db_returns_empty(temp_db):
    """空 DB 不报错，返回空列表。"""
    recovered = await recover_orphaned_tasks(reaped_task_ids=[])
    assert recovered == []


async def test_reaped_id_in_reaped_set_but_still_non_terminal(temp_db):
    """边缘场景：reaped_set 包含的 task_id 仍处于非终态（reaper 写终态失败）。
    本函数仍跳过，依赖 reaper 的语义。"""
    task_id = await _make_task(temp_db, TaskStatus.executing)

    recovered = await recover_orphaned_tasks(reaped_task_ids=[task_id])

    assert task_id not in recovered
    # 状态保留为 executing（让 caller 决定如何后续处理）
    task = await get_task(temp_db, task_id)
    assert task is not None
    assert task.status == TaskStatus.executing
