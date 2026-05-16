"""
单元测试：P0-6 / P0-7 queue._run_one 终态分发

验证 driver 抛出不同异常时，queue 写入对应的终态状态 + 终态事件：
    TaskTimedOutError → TaskStatus.timed_out + task_timed_out
    TaskAbortedError  → TaskStatus.aborted   + task_aborted
    其它 Exception    → TaskStatus.failed    + task_failed
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from worker.contract.event import TaskEventKind
from worker.contract.exceptions import TaskAbortedError, TaskTimedOutError
from worker.contract.task import TaskMode, TaskRequest, TaskStatus
from worker.orchestrator import queue as queue_module
from worker.storage import db as db_module
from worker.storage.repo import get_task, insert_task


@pytest.fixture
async def temp_db(tmp_path: Path):
    """每个测试用例一份隔离的 SQLite 数据库。"""
    db_file = tmp_path / "worker_test.db"
    conn = await db_module.init_db(db_file)
    try:
        yield conn
    finally:
        await db_module.close_db()


async def _create_task(db) -> str:
    """生成一条 task 行，返回 task_id。"""
    req = TaskRequest(mode=TaskMode.direct_execute, messages=[])
    resp = await insert_task(db, req)
    return resp.task_id


async def _read_terminal_event_kinds(db, task_id: str) -> list[str]:
    async with db.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY event_id",
        (task_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [r["kind"] for r in rows]


async def _read_last_event_payload(db, task_id: str) -> dict:
    async with db.execute(
        "SELECT payload_json FROM task_events WHERE task_id = ? "
        "ORDER BY event_id DESC LIMIT 1",
        (task_id,),
    ) as cur:
        row = await cur.fetchone()
    return json.loads(row["payload_json"])


@pytest.fixture
def reset_queue_state():
    """每个测试前后重置 queue 模块的全局 executor / semaphore，避免污染。"""
    import asyncio as _asyncio

    saved_executor = queue_module._task_executor
    saved_semaphore = queue_module._semaphore
    queue_module._semaphore = _asyncio.Semaphore(1)
    yield
    queue_module._task_executor = saved_executor
    queue_module._semaphore = saved_semaphore


async def test_timeout_routes_to_timed_out_terminal(temp_db, reset_queue_state):
    task_id = await _create_task(temp_db)

    async def executor(_tid: str) -> None:
        raise TaskTimedOutError(timeout_sec=42.0)

    queue_module.set_executor(executor)
    await queue_module._run_one(task_id)

    task = await get_task(temp_db, task_id)
    assert task is not None
    assert task.status == TaskStatus.timed_out

    kinds = await _read_terminal_event_kinds(temp_db, task_id)
    assert kinds[-1] == TaskEventKind.task_timed_out.value
    assert TaskEventKind.task_failed.value not in kinds
    assert TaskEventKind.task_aborted.value not in kinds

    payload = await _read_last_event_payload(temp_db, task_id)
    assert payload["timeout_sec"] == 42.0


async def test_abort_routes_to_aborted_terminal(temp_db, reset_queue_state):
    task_id = await _create_task(temp_db)

    async def executor(_tid: str) -> None:
        raise TaskAbortedError(reason="hitl_timeout", decision_id="dec-99")

    queue_module.set_executor(executor)
    await queue_module._run_one(task_id)

    task = await get_task(temp_db, task_id)
    assert task is not None
    assert task.status == TaskStatus.aborted

    kinds = await _read_terminal_event_kinds(temp_db, task_id)
    assert kinds[-1] == TaskEventKind.task_aborted.value
    assert TaskEventKind.task_failed.value not in kinds
    assert TaskEventKind.task_timed_out.value not in kinds

    payload = await _read_last_event_payload(temp_db, task_id)
    assert payload["reason"] == "hitl_timeout"
    assert payload["decision_id"] == "dec-99"


async def test_generic_exception_routes_to_failed(temp_db, reset_queue_state):
    task_id = await _create_task(temp_db)

    async def executor(_tid: str) -> None:
        raise RuntimeError("boom")

    queue_module.set_executor(executor)
    await queue_module._run_one(task_id)

    task = await get_task(temp_db, task_id)
    assert task is not None
    assert task.status == TaskStatus.failed

    kinds = await _read_terminal_event_kinds(temp_db, task_id)
    assert kinds[-1] == TaskEventKind.task_failed.value
    assert TaskEventKind.task_aborted.value not in kinds
    assert TaskEventKind.task_timed_out.value not in kinds

    payload = await _read_last_event_payload(temp_db, task_id)
    assert payload["error_type"] == "RuntimeError"
    assert "boom" in payload["error"]
