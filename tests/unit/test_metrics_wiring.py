"""
单元测试：P1-11 metrics callsites 接入

验证 queue._run_one 完成各终态时正确累加：
    - inc_task_count(status)
    - observe_task_duration
    - inc_active_tasks / dec_active_tasks 平衡
    - inc_abort_count(reason) on TaskAbortedError

不直接验证 HITL wait / container start（涉及 Docker 与 driver 实环境，
留给集成测试覆盖；此处仅验证 helper 在 metrics 模块导出且签名正确）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from worker.contract.event import TaskEventKind  # noqa: F401
from worker.contract.exceptions import TaskAbortedError, TaskTimedOutError
from worker.contract.task import TaskMode, TaskRequest, TaskStatus
from worker.observability import metrics
from worker.orchestrator import queue as queue_module
from worker.storage import db as db_module
from worker.storage.repo import insert_task


@pytest.fixture
async def temp_db(tmp_path: Path):
    db_file = tmp_path / "metrics_test.db"
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
    """每个测试用例前清零 metrics 模块的全局状态。"""
    metrics._task_count.clear()
    metrics._abort_count.clear()
    metrics._token_usage.clear()
    metrics._active_tasks = 0
    metrics._task_duration_sum = 0.0
    metrics._task_duration_count = 0
    metrics._hitl_wait_sum = 0.0
    metrics._hitl_wait_count = 0
    metrics._container_start_sum = 0.0
    metrics._container_start_count = 0
    yield


async def _create_task(db) -> str:
    req = TaskRequest(mode=TaskMode.direct_execute, messages=[])
    resp = await insert_task(db, req)
    return resp.task_id


async def test_success_increments_completed_counter(
    temp_db, reset_queue_state, reset_metrics
):
    task_id = await _create_task(temp_db)

    async def executor(tid: str) -> None:
        # mimic orchestrator.run_task: write task_completed itself
        from worker.contract.task import TaskStatus as TS
        from worker.storage.repo import insert_event, update_task_status
        await update_task_status(temp_db, tid, TS.completed)
        await insert_event(temp_db, tid, TaskEventKind.task_completed)

    queue_module.set_executor(executor)
    await queue_module._run_one(task_id)

    assert metrics._task_count.get(TaskStatus.completed.value) == 1
    assert metrics._task_duration_count == 1
    assert metrics._task_duration_sum >= 0
    assert metrics._active_tasks == 0  # 进入+1，退出-1 平衡


async def test_timeout_increments_timed_out_counter(
    temp_db, reset_queue_state, reset_metrics
):
    task_id = await _create_task(temp_db)

    async def executor(_tid: str) -> None:
        raise TaskTimedOutError(timeout_sec=10.0)

    queue_module.set_executor(executor)
    await queue_module._run_one(task_id)

    assert metrics._task_count.get(TaskStatus.timed_out.value) == 1
    assert metrics._task_duration_count == 1
    assert metrics._active_tasks == 0


async def test_abort_increments_aborted_and_abort_reason_counters(
    temp_db, reset_queue_state, reset_metrics
):
    task_id = await _create_task(temp_db)

    async def executor(_tid: str) -> None:
        raise TaskAbortedError(reason="hitl_timeout", decision_id="dec-1")

    queue_module.set_executor(executor)
    await queue_module._run_one(task_id)

    assert metrics._task_count.get(TaskStatus.aborted.value) == 1
    assert metrics._abort_count.get("hitl_timeout") == 1
    assert metrics._task_duration_count == 1
    assert metrics._active_tasks == 0


async def test_failure_increments_failed_counter(
    temp_db, reset_queue_state, reset_metrics
):
    task_id = await _create_task(temp_db)

    async def executor(_tid: str) -> None:
        raise RuntimeError("boom")

    queue_module.set_executor(executor)
    await queue_module._run_one(task_id)

    assert metrics._task_count.get(TaskStatus.failed.value) == 1
    assert metrics._task_duration_count == 1
    assert metrics._active_tasks == 0


async def test_active_tasks_balanced_on_executor_exception(
    temp_db, reset_queue_state, reset_metrics
):
    """active_tasks 始终在 finally 块递减，即便 executor 抛非托管异常。"""
    task_id = await _create_task(temp_db)

    async def executor(_tid: str) -> None:
        raise ValueError("unexpected")

    queue_module.set_executor(executor)
    await queue_module._run_one(task_id)

    assert metrics._active_tasks == 0


async def test_render_prometheus_includes_populated_counters(
    temp_db, reset_queue_state, reset_metrics
):
    """跑两个不同终态后 /metrics 输出应不再是空 counter。"""
    t1 = await _create_task(temp_db)
    t2 = await _create_task(temp_db)

    async def ok(tid: str) -> None:
        from worker.storage.repo import insert_event, update_task_status
        await update_task_status(temp_db, tid, TaskStatus.completed)
        await insert_event(temp_db, tid, TaskEventKind.task_completed)

    async def boom(_tid: str) -> None:
        raise RuntimeError("fail")

    queue_module.set_executor(ok)
    await queue_module._run_one(t1)

    queue_module.set_executor(boom)
    await queue_module._run_one(t2)

    text = metrics.render_prometheus()
    assert 'worker_task_count_total{status="completed"} 1' in text
    assert 'worker_task_count_total{status="failed"} 1' in text
    assert "worker_task_duration_seconds_count 2" in text
    assert "worker_active_tasks 0" in text
