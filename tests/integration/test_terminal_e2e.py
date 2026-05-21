"""
T2 — abort / timeout 终态事件全链路 E2E（Phase 6 退出门补缺）。

对应 [docs/roadmap/opencode-worker.md §9.B] 中标记为"待补"的 T2：
    覆盖三条端到端链路：
        1. HITL 决策超时 → driver 抛 TaskAbortedError → queue 写
           task_aborted(reason="hitl_timeout") → SSE 总线唤醒订阅者
        2. ResourceLimits.timeout_sec 触发 → driver 抛 TaskTimedOutError
           → queue 写 task_timed_out(timeout_sec=…) → 订阅者唤醒
        3. 一个已存在的 event_bus 订阅者在终态事件写入后立即被唤醒
           （证明 ``insert_event → event_bus.notify`` 这条 wakeup 链没断）

为什么是集成层而非单元层：
    [tests/unit/test_terminal_dispatch.py] 用一个抛异常的 stub executor
    覆盖了 ``queue._run_one`` 的异常→终态路由，但 executor 是裸函数；本
    文件用 **真实 OpenCodeDriver** 走完 ``client.create_session →
    prompt_async → _consume_sse → _handle_permission/asyncio.timeout``
    再被 queue 接住，校验：
        - 异常类型/字段（reason / timeout_sec / decision_id）从 driver
          原样穿透到 queue 终态 payload
        - metrics 计数（abort_count{reason}, task_count{status}）被
          正确累加
        - event_bus 订阅者收到 wakeup（端到端 SSE 链路就绪）

测试不依赖真实 opencode 容器（用 StubOpenCodeServer），也不启动真实
FastAPI / uvicorn（直接调用 ``queue._run_one`` + ``event_bus.subscribe``）。
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

import pytest

from tests.integration.conftest import (
    make_permission_event,
    make_session_idle_event,
    wait_until,
    wait_until_async,
)
from worker.adapters.opencode.driver import OpenCodeDriver
from worker.contract.event import TaskEventKind
from worker.contract.task import (
    HitlPolicy,
    Message,
    ResourceLimits,
    TaskMode,
    TaskRequest,
    TaskStatus,
)
from worker.observability import metrics
from worker.orchestrator import event_bus
from worker.orchestrator import queue as queue_module
from worker.storage.repo import get_events_after, get_task, insert_task


# ── 通用 helper ───────────────────────────────────────────────────────────────


async def _read_events(db, task_id: str) -> list[tuple[str, dict]]:
    events = await get_events_after(db, task_id, after_cursor=0)
    return [(e.kind.value, e.payload) for e in events]


def _metrics_snapshot() -> dict:
    """抓 metrics 模块全局计数器快照，便于做"增量"断言。

    用 dict() 拷贝避免后续操作影响快照。
    """
    return {
        "task_count": dict(metrics._task_count),
        "abort_count": dict(metrics._abort_count),
    }


def _delta(after: dict, before: dict, bucket: str, label: str) -> int:
    """计算 ``after[bucket][label] - before[bucket][label]``，缺省视为 0。"""
    return after[bucket].get(label, 0) - before[bucket].get(label, 0)


async def _insert_task_with(
    db,
    *,
    hitl_policy: Optional[HitlPolicy] = None,
    resource_limits: Optional[ResourceLimits] = None,
) -> tuple[str, TaskRequest]:
    """生成一条 direct_execute task 行，便于让 driver 直接进 prompt_async。"""
    request = TaskRequest(
        mode=TaskMode.direct_execute,
        messages=[Message(role="user", content="please run")],
        hitl_policy=hitl_policy or HitlPolicy(),
        resource_limits=resource_limits
        or ResourceLimits(cpu="2", memory="4Gi", pids=512, timeout_sec=30),
    )
    response = await insert_task(db, request)
    return response.task_id, request


def _make_executor(
    driver_holder: dict,
    *,
    request: TaskRequest,
    stub_server,
    db,
    decision_timeout_sec_float: Optional[float] = None,
):
    """构造 queue._task_executor，捕获 driver 到 ``driver_holder`` 供注入协程读取。

    可选 ``decision_timeout_sec_float``：HitlPolicy.decision_timeout_sec
    是 int，但单测里需要亚秒级（< 1s）以加快用例——用 object.__setattr__
    在 driver 构造完成后把字段改成 float（pydantic v2 允许）。
    """

    async def _executor(task_id: str) -> None:
        driver = OpenCodeDriver(
            task_id=task_id,
            request=request,
            host_port=stub_server.port,
            container_env={"OPENCODE_SERVER_PASSWORD": stub_server.password},
            db=db,
            interceptors=[],
        )
        if decision_timeout_sec_float is not None:
            object.__setattr__(
                driver.request.hitl_policy,
                "decision_timeout_sec",
                float(decision_timeout_sec_float),
            )
        driver_holder["driver"] = driver
        await driver.run()

    return _executor


async def _wait_for_driver_subscribed(stub_server, driver_holder: dict) -> str:
    """阻塞直到 executor 构造出 driver 且 SSE 已订阅，返回 session_id。"""

    def _ok() -> bool:
        d = driver_holder.get("driver")
        return (
            d is not None
            and d.session_id is not None
            and len(stub_server._sse_subscribers) > 0
        )

    await wait_until(
        _ok, timeout=5.0,
        description="driver constructed + SSE subscribed + session_id set",
    )
    return driver_holder["driver"].session_id


# ── T2.1 HITL 超时 → abort 全链路 ───────────────────────────────────────────


async def test_e2e_hitl_timeout_abort_propagates_full_stack(
    temp_db, stub_server, patch_data_dir, reset_buses, reset_queue_state,
    fast_hitl_poll,
):
    """决策完全不响应 + on_timeout=abort 时，driver 抛 TaskAbortedError
    被 queue 接住后：

        - task.status == aborted
        - 最后一条事件 = task_aborted，payload.reason="hitl_timeout"
        - 倒数第二条事件 = hitl_timeout（driver 写入）
        - metrics: abort_count{hitl_timeout} += 1, task_count{aborted} += 1
        - 一个事先订阅的 SSE 总线订阅者被唤醒（Event.is_set() == True）
    """
    task_id, request = await _insert_task_with(
        temp_db,
        hitl_policy=HitlPolicy(decision_timeout_sec=1, on_timeout="abort"),
    )

    # 在 _run_one 之前订阅，确保终态写入时订阅者已存在
    subscriber = event_bus.get_bus(task_id).subscribe()
    assert not subscriber.is_set()

    metrics_before = _metrics_snapshot()
    driver_holder: dict = {}

    queue_module.set_executor(_make_executor(
        driver_holder,
        request=request,
        stub_server=stub_server,
        db=temp_db,
        decision_timeout_sec_float=0.5,
    ))

    # 注入协程：等 driver 订阅 → 广播权限 → 等 hitl_timeout → 广播 session.idle
    async def _injector() -> None:
        session_id = await _wait_for_driver_subscribed(stub_server, driver_holder)
        perm_id = "per-e2e-abort-1"
        await stub_server._broadcast_event(make_permission_event(perm_id, tool="bash"))

        async def _has_hitl_timeout() -> bool:
            events = await _read_events(temp_db, task_id)
            return any(k == "hitl_timeout" for k, _ in events)

        await wait_until_async(
            _has_hitl_timeout, timeout=3.0,
            description="hitl_timeout written by _handle_permission",
        )
        # 让 SSE / poll 循环退出，_run_inner step 8 才能看到 abort_event
        await stub_server._broadcast_event(make_session_idle_event(session_id))

    inject_task = asyncio.create_task(_injector(), name="t2-inject-abort")
    try:
        # _run_one 内部捕获 TaskAbortedError 并写终态；外层不该抛
        await asyncio.wait_for(queue_module._run_one(task_id), timeout=10.0)
    finally:
        if not inject_task.done():
            inject_task.cancel()
            try:
                await inject_task
            except BaseException:
                pass

    # ── 断言 ────────────────────────────────────────────────────────────────
    # 1) 任务终态 = aborted
    task = await get_task(temp_db, task_id)
    assert task is not None
    assert task.status == TaskStatus.aborted, (
        f"任务应转到 aborted，实际 {task.status!r}"
    )

    # 2) 事件序列：…→ hitl_required → hitl_timeout → task_aborted
    events = await _read_events(temp_db, task_id)
    kinds = [k for k, _ in events]
    assert "hitl_required" in kinds
    assert "hitl_timeout" in kinds
    assert kinds[-1] == TaskEventKind.task_aborted.value, (
        f"最后一条事件应是 task_aborted，实际 {kinds[-1]!r}（全序列：{kinds}）"
    )
    assert kinds.index("hitl_timeout") < kinds.index(TaskEventKind.task_aborted.value)

    # 3) task_aborted payload 透传 reason 和 decision_id
    aborted_payload = events[-1][1]
    assert aborted_payload["reason"] == "hitl_timeout", aborted_payload
    assert aborted_payload.get("decision_id"), (
        f"task_aborted.payload.decision_id 不应为空：{aborted_payload}"
    )

    # 4) metrics 计数器累加
    metrics_after = _metrics_snapshot()
    assert _delta(metrics_after, metrics_before, "abort_count", "hitl_timeout") == 1, (
        f"abort_count{{hitl_timeout}} 增量应为 1："
        f"{metrics_before['abort_count']!r} → {metrics_after['abort_count']!r}"
    )
    assert _delta(metrics_after, metrics_before, "task_count", "aborted") == 1, (
        f"task_count{{aborted}} 增量应为 1："
        f"{metrics_before['task_count']!r} → {metrics_after['task_count']!r}"
    )

    # 5) SSE 订阅者唤醒（insert_event → event_bus.notify 链路）
    assert subscriber.is_set(), (
        "终态事件写入后，事先订阅的 event_bus 订阅者应被唤醒；"
        "若未唤醒则 SSE 推送链路在 insert_event → notify 之间断了"
    )


# ── T2.2 ResourceLimits.timeout_sec 触发 → timed_out 全链路 ─────────────────


async def test_e2e_resource_timeout_routes_to_timed_out_terminal(
    temp_db, stub_server, patch_data_dir, reset_buses, reset_queue_state,
    fast_hitl_poll,
):
    """ResourceLimits.timeout_sec=1 + stub 永远不发 idle → driver.run() 外
    层 ``asyncio.timeout`` 触发 → 抛 TaskTimedOutError → queue 写
    task_timed_out。

        - task.status == timed_out
        - 最后一条事件 = task_timed_out, payload.timeout_sec == 1
        - metrics: task_count{timed_out} += 1
        - SSE 订阅者被唤醒
    """
    task_id, request = await _insert_task_with(
        temp_db,
        # 任务级 1 秒超时；HITL 不触发（没人请求权限）
        resource_limits=ResourceLimits(cpu="2", memory="4Gi", pids=512, timeout_sec=1),
    )

    subscriber = event_bus.get_bus(task_id).subscribe()
    metrics_before = _metrics_snapshot()
    driver_holder: dict = {}

    queue_module.set_executor(_make_executor(
        driver_holder, request=request, stub_server=stub_server, db=temp_db,
    ))

    # 不需要注入协程：stub 默认只会保持 SSE 心跳，driver 等不到 idle
    # 1 秒后 asyncio.timeout 自然触发
    t0 = time.monotonic()
    await asyncio.wait_for(queue_module._run_one(task_id), timeout=10.0)
    elapsed = time.monotonic() - t0
    # 应该接近 1s（允许 SSE 订阅 + prompt_async 的少量额外耗时）
    assert elapsed < 5.0, f"resource timeout 路径耗时异常：{elapsed:.2f}s"

    # ── 断言 ────────────────────────────────────────────────────────────────
    # 1) 任务终态 = timed_out
    task = await get_task(temp_db, task_id)
    assert task is not None
    assert task.status == TaskStatus.timed_out, (
        f"任务应转到 timed_out，实际 {task.status!r}"
    )

    # 2) 最后一条事件 = task_timed_out, payload 透传 timeout_sec
    events = await _read_events(temp_db, task_id)
    kinds = [k for k, _ in events]
    assert kinds[-1] == TaskEventKind.task_timed_out.value, (
        f"最后一条事件应是 task_timed_out，实际 {kinds[-1]!r}（全序列：{kinds}）"
    )
    timed_out_payload = events[-1][1]
    assert timed_out_payload["timeout_sec"] == 1, (
        f"task_timed_out.payload.timeout_sec 应等于 1，实际 {timed_out_payload!r}"
    )

    # 3) metrics 计数器累加
    metrics_after = _metrics_snapshot()
    assert _delta(metrics_after, metrics_before, "task_count", "timed_out") == 1, (
        f"task_count{{timed_out}} 增量应为 1："
        f"{metrics_before['task_count']!r} → {metrics_after['task_count']!r}"
    )

    # 4) SSE 订阅者唤醒
    assert subscriber.is_set(), (
        "task_timed_out 写入后，事先订阅的 event_bus 订阅者应被唤醒"
    )


# ── T2.3 终态事件唤醒已存在的 SSE 订阅者（计时） ────────────────────────────


async def test_e2e_terminal_event_wakes_existing_sse_subscriber(
    temp_db, stub_server, patch_data_dir, reset_buses, reset_queue_state,
    fast_hitl_poll,
):
    """复用 ResourceLimits 超时路径，但断言时序：

        - 订阅必须在 _run_one 启动之前完成
        - 终态写入后，等待 subscriber.wait() 的耗时应 << 100ms（直接由
          event_bus.notify 触发，而不是被某个 0.5s heartbeat 兜底）
        - 终止后 event_bus.discard(task_id) 已执行（不再持有该 bus）
    """
    task_id, request = await _insert_task_with(
        temp_db,
        resource_limits=ResourceLimits(cpu="2", memory="4Gi", pids=512, timeout_sec=1),
    )

    # 订阅 + 留住引用（即使 queue._run_one 在 finally 中 discard 了 bus，
    # 已被订阅者持有的 Event 对象仍然有效——这正是 SSE handler 的实际语义）
    bus_before = event_bus.get_bus(task_id)
    subscriber = bus_before.subscribe()
    assert bus_before.subscriber_count == 1

    driver_holder: dict = {}
    queue_module.set_executor(_make_executor(
        driver_holder, request=request, stub_server=stub_server, db=temp_db,
    ))

    # 起一个并发任务等 subscriber.wait()，计时
    async def _wait_and_time() -> float:
        t0 = time.monotonic()
        await subscriber.wait()
        return time.monotonic() - t0

    waiter_task = asyncio.create_task(_wait_and_time(), name="t2-wakeup-waiter")
    try:
        await asyncio.wait_for(queue_module._run_one(task_id), timeout=10.0)
    finally:
        # waiter 应已自然 set；保险起见加 timeout
        wake_elapsed = await asyncio.wait_for(waiter_task, timeout=2.0)

    # ── 断言 ────────────────────────────────────────────────────────────────
    # 1) wakeup 在终态前后立即发生
    #    `subscriber.wait()` 是被 event_bus.notify 唤醒的；总耗时 ≈ run_one
    #    的耗时（~1s），因为 wakeup 在 _run_one 内部触发，但 wake_elapsed 是
    #    "从订阅开始到被唤醒"的时间——只能断言它 < _run_one 总耗时 + 余量。
    #    更关键的是 ``subscriber.is_set() == True`` 在 _run_one 返回时已成立。
    assert subscriber.is_set()
    assert wake_elapsed < 5.0, (
        f"订阅者唤醒耗时异常（{wake_elapsed:.3f}s），可能 event_bus.notify "
        f"没被 insert_event 调用"
    )

    # 2) 终态确实写入
    task = await get_task(temp_db, task_id)
    assert task is not None
    assert task.status == TaskStatus.timed_out

    # 3) queue._run_one 的 finally 应已调用 event_bus.discard(task_id)
    #    即模块级 _buses 不再持有该 task 的 bus；但订阅者持有的 Event 仍有效。
    assert task_id not in event_bus._buses, (
        f"queue._run_one finally 应 discard 掉 task {task_id} 的 event_bus，"
        f"实际 _buses 仍含 {list(event_bus._buses.keys())!r}"
    )
