"""
单元测试：P1-10 event_id 并发分配 race fix

修复前：`_consume_sse` 与 `_handle_permission` 并发写事件，可能读到相同
MAX(event_id) → 后写入者撞 UNIQUE(task_id, event_id) → IntegrityError →
queue 误标 task_failed。

修复后：per-task asyncio.Lock 串行化 SELECT MAX + INSERT，并发 insert_event
全部成功且 event_id 单调连续。

验证维度：
    - 同任务 N 个并发写：全部成功，event_id 集合 = {1..N}
    - 不同任务并发写：互不阻塞（锁是 per-task）
    - 终态后 discard_task_locks 释放锁条目
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from worker.contract.event import TaskEventKind
from worker.contract.task import TaskMode, TaskRequest
from worker.storage import db as db_module
from worker.storage import repo as repo_module
from worker.storage.repo import (
    discard_task_locks,
    insert_event,
    insert_task,
)


@pytest.fixture
async def temp_db(tmp_path: Path):
    db_file = tmp_path / "race_test.db"
    conn = await db_module.init_db(db_file)
    try:
        yield conn
    finally:
        await db_module.close_db()


async def _make_task(db) -> str:
    req = TaskRequest(mode=TaskMode.direct_execute, messages=[])
    resp = await insert_task(db, req)
    return resp.task_id


async def test_concurrent_inserts_same_task_no_unique_violation(temp_db):
    """同一任务的 50 个并发 insert_event：全部成功，event_id 1..50 全覆盖。"""
    task_id = await _make_task(temp_db)

    async def write(i: int):
        return await insert_event(
            temp_db, task_id, TaskEventKind.assistant_delta,
            {"i": i},
        )

    N = 50
    results = await asyncio.gather(*(write(i) for i in range(N)))
    event_ids = sorted(e.event_id for e in results)

    # 没有 IntegrityError 抛出代表锁生效
    assert event_ids == list(range(1, N + 1)), (
        f"expected event_ids 1..{N}, got {event_ids}"
    )

    # DB 内确实有 N 行
    async with temp_db.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == N


async def test_concurrent_inserts_different_tasks_independent(temp_db):
    """不同 task_id 的并发写互不阻塞，各自 event_id 从 1 开始。"""
    t1 = await _make_task(temp_db)
    t2 = await _make_task(temp_db)

    async def write(tid: str, i: int):
        return await insert_event(
            temp_db, tid, TaskEventKind.assistant_delta, {"i": i},
        )

    N = 20
    coros = []
    for i in range(N):
        coros.append(write(t1, i))
        coros.append(write(t2, i))

    results = await asyncio.gather(*coros)
    t1_ids = sorted(e.event_id for e in results if e.task_id == t1)
    t2_ids = sorted(e.event_id for e in results if e.task_id == t2)

    assert t1_ids == list(range(1, N + 1))
    assert t2_ids == list(range(1, N + 1))


async def test_discard_task_locks_releases_dict_entry(temp_db):
    """终态调用 discard_task_locks 后 _event_locks 不再保留该 task_id。"""
    task_id = await _make_task(temp_db)
    await insert_event(temp_db, task_id, TaskEventKind.task_started)

    assert task_id in repo_module._event_locks

    discard_task_locks(task_id)
    assert task_id not in repo_module._event_locks

    # 二次 discard 是幂等的（不抛错）
    discard_task_locks(task_id)


async def test_lock_recreated_after_discard(temp_db):
    """discard 后再写事件会重建锁，不影响功能（防御性测试）。"""
    task_id = await _make_task(temp_db)
    await insert_event(temp_db, task_id, TaskEventKind.task_started)
    discard_task_locks(task_id)

    # 应能再次写入（即便正常流程不会发生）
    ev = await insert_event(temp_db, task_id, TaskEventKind.task_completed)
    assert ev.event_id == 2  # 跟随已有 MAX(event_id)+1
