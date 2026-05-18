"""
单元测试：P1-12 SSE 事件驱动推送

修复前：SSE handler 用 0.5s 轮询 DB 检查新事件，造成流式输出抖动 +
HITL 延迟 +500ms +高并发下 DB 抢锁。

修复后：repo.insert_event 写库后调用 event_bus.notify(task_id)，
SSE 订阅者通过 asyncio.Event 立即唤醒（< 1ms）。

验证：
    - subscribe / notify / unsubscribe 基本流程
    - notify 唤醒所有订阅者，且事件可被 clear 后重复使用
    - insert_event 写库后会触发 notify（端到端）
    - discard 释放 bus，残留订阅者不影响 dict 清理
    - 无订阅者时 notify 静默 noop（不创建空 bus）
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from worker.contract.event import TaskEventKind
from worker.contract.task import TaskMode, TaskRequest
from worker.orchestrator import event_bus
from worker.storage import db as db_module
from worker.storage.repo import insert_event, insert_task


@pytest.fixture
async def temp_db(tmp_path: Path):
    db_file = tmp_path / "sse_bus_test.db"
    conn = await db_module.init_db(db_file)
    try:
        yield conn
    finally:
        await db_module.close_db()


@pytest.fixture
def reset_buses():
    event_bus._buses.clear()
    yield
    event_bus._buses.clear()


async def _make_task(db) -> str:
    req = TaskRequest(mode=TaskMode.direct_execute, messages=[])
    resp = await insert_task(db, req)
    return resp.task_id


async def test_subscribe_returns_event_and_registers_subscriber(reset_buses):
    bus = event_bus.get_bus("t1")
    assert bus.subscriber_count == 0

    sub = bus.subscribe()
    assert isinstance(sub, asyncio.Event)
    assert bus.subscriber_count == 1
    assert not sub.is_set()


async def test_notify_wakes_all_subscribers(reset_buses):
    bus = event_bus.get_bus("t1")
    sub_a = bus.subscribe()
    sub_b = bus.subscribe()

    bus.notify()
    assert sub_a.is_set()
    assert sub_b.is_set()


async def test_subscriber_clear_then_re_wait(reset_buses):
    """订阅者醒后 clear，可再次等待下一次 notify。"""
    bus = event_bus.get_bus("t1")
    sub = bus.subscribe()

    bus.notify()
    assert sub.is_set()
    sub.clear()
    assert not sub.is_set()

    # 再次 notify 仍能唤醒
    bus.notify()
    assert sub.is_set()


async def test_unsubscribe_removes_from_bus(reset_buses):
    bus = event_bus.get_bus("t1")
    sub = bus.subscribe()
    assert bus.subscriber_count == 1

    bus.unsubscribe(sub)
    assert bus.subscriber_count == 0

    # 重复 unsubscribe 是幂等的（不抛异常）
    bus.unsubscribe(sub)


async def test_notify_with_no_bus_is_noop(reset_buses):
    """未订阅过的 task_id 调 notify 不会创建空 bus。"""
    event_bus.notify("never-subscribed")
    assert "never-subscribed" not in event_bus._buses
    assert event_bus.active_bus_count() == 0


async def test_discard_releases_bus_dict_entry(reset_buses):
    bus = event_bus.get_bus("t1")
    bus.subscribe()
    assert "t1" in event_bus._buses

    event_bus.discard("t1")
    assert "t1" not in event_bus._buses

    # 二次 discard 是幂等
    event_bus.discard("t1")


async def test_insert_event_triggers_notify_end_to_end(temp_db, reset_buses):
    """端到端：repo.insert_event 写库后 event_bus.notify 会唤醒已订阅的事件。"""
    task_id = await _make_task(temp_db)

    bus = event_bus.get_bus(task_id)
    sub = bus.subscribe()
    assert not sub.is_set()

    await insert_event(temp_db, task_id, TaskEventKind.task_started)

    assert sub.is_set(), "insert_event 后应触发 notify 唤醒订阅者"


async def test_insert_event_wakes_within_one_event_loop_iter(temp_db, reset_buses):
    """订阅者通过 wait_for 等待，insert_event 后毫秒级唤醒（不依赖 sleep）。"""
    task_id = await _make_task(temp_db)
    bus = event_bus.get_bus(task_id)
    sub = bus.subscribe()

    async def writer():
        # 让订阅者先进入 wait
        await asyncio.sleep(0.01)
        await insert_event(temp_db, task_id, TaskEventKind.assistant_delta)

    writer_task = asyncio.create_task(writer())
    # timeout 远大于 writer 的 sleep；事件驱动应在 writer 完成后立即唤醒
    await asyncio.wait_for(sub.wait(), timeout=1.0)
    assert sub.is_set()
    await writer_task


async def test_multiple_subscribers_all_wake_independently(temp_db, reset_buses):
    task_id = await _make_task(temp_db)
    bus = event_bus.get_bus(task_id)
    sub_a = bus.subscribe()
    sub_b = bus.subscribe()
    sub_c = bus.subscribe()

    await insert_event(temp_db, task_id, TaskEventKind.task_started)

    assert sub_a.is_set()
    assert sub_b.is_set()
    assert sub_c.is_set()
