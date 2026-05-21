"""
T1 — HITL 时序集成测试（Phase 6 退出门补缺）。

对应 [docs/roadmap/opencode-worker.md §9.B] 中标记为"待补"的 T1：
    复用 [tests/fixtures/stub_opencode_server.py] 覆盖决策的四类时序边界：
        1. 早到：决策在 driver 首次 poll 之前就到达
        2. 晚到：决策经过多轮 poll 后才到达（但仍在 timeout 内）
        3. 重复：同一 decision_id 提交两次，第二次必须 No-op
        4. 超时：完全不响应 + ``on_timeout=abort`` → driver 抛 TaskAbortedError

为什么是集成层而非单元层：
    [tests/unit/test_hitl_timeout_policy.py] 用 ``DummyClient`` 在单元
    层覆盖了 timeout 策略路由，但所有外部交互都被 mock 掉了。本文件用
    真实 ``OpenCodeClient`` ↔ ``StubOpenCodeServer`` ↔ 真实 SQLite DB ↔
    真实 ``resolve_decision`` 链路重跑一遍，校验：
        - SSE 订阅到 permission 事件能正确触发 ``_handle_permission``
        - 通过 ``resolve_decision`` 写库后 driver 能感知并调用
          ``respond_permission`` 把结果回传给 opencode（stub 侧记录）
        - 三类异常时序与正常路径不会回归

测试不依赖真实 opencode 容器；不调用 queue（终态分发由 T2 覆盖）。
"""
from __future__ import annotations

import asyncio
import json
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
from worker.contract.decision import DecisionChoice, DecisionResponse
from worker.contract.event import TaskEventKind
from worker.contract.exceptions import TaskAbortedError
from worker.contract.task import (
    HitlPolicy,
    Message,
    ResourceLimits,
    TaskMode,
    TaskRequest,
    TaskStatus,
)
from worker.storage.repo import (
    get_events_after,
    get_pending_decision,
    get_task,
    insert_task,
    resolve_decision,
)


# ── 通用 helper ───────────────────────────────────────────────────────────────


async def _make_task_and_driver(
    db,
    stub_server,
    *,
    decision_timeout_sec: float = 1.5,
    on_timeout: str = "abort",
) -> tuple[str, OpenCodeDriver, TaskRequest]:
    """构造一条 task 行 + driver 实例，便于复用。

    direct_execute 模式 + 一条 user 消息（让 prompt_async 有内容），
    resource_limits.timeout_sec=30 保证 asyncio.timeout 不会抢在 HITL
    超时前触发。
    """
    request = TaskRequest(
        mode=TaskMode.direct_execute,
        messages=[Message(role="user", content="please run")],
        hitl_policy=HitlPolicy(
            decision_timeout_sec=int(max(1, decision_timeout_sec)),
            on_timeout=on_timeout,
        ),
        resource_limits=ResourceLimits(cpu="2", memory="4Gi", pids=512, timeout_sec=30),
    )
    # 用 float 覆盖 pydantic 强制 int 后的精度损失（HitlPolicy.decision_timeout_sec
    # 是 int 字段；我们需要亚秒级控制——直接覆盖 driver._wait_for_decision 的输入）
    response = await insert_task(db, request)
    task_id = response.task_id
    driver = OpenCodeDriver(
        task_id=task_id,
        request=request,
        host_port=stub_server.port,
        container_env={"OPENCODE_SERVER_PASSWORD": stub_server.password},
        db=db,
        interceptors=[],
    )
    # 透传 sub-second timeout 给 driver：HitlPolicy.decision_timeout_sec 强制 int，
    # 这里在不改 contract 的前提下，直接 monkeypatch driver._wait_for_decision 的
    # 内层调用方——通过包装 hitl_policy.decision_timeout_sec 的访问。
    # 简单做法：把 HitlPolicy 字段 in-place 改 float。pydantic v2 允许 hack。
    object.__setattr__(driver.request.hitl_policy, "decision_timeout_sec", float(decision_timeout_sec))
    return task_id, driver, request


async def _wait_for_pending_decision(db, task_id: str, *, timeout: float = 3.0) -> str:
    """轮询 DB 直到出现 pending 决策，返回 decision_id。"""
    result: dict = {}

    async def _check() -> bool:
        pd = await get_pending_decision(db, task_id)
        if pd is None:
            return False
        result["id"] = pd.decision_id
        return True

    await wait_until_async(
        _check, timeout=timeout, description="pending decision row appears",
    )
    return result["id"]


async def _read_events(db, task_id: str) -> list[tuple[str, dict]]:
    events = await get_events_after(db, task_id, after_cursor=0)
    return [(e.kind.value, e.payload) for e in events]


async def _read_decision_status(db, decision_id: str) -> Optional[str]:
    async with db.execute(
        "SELECT status FROM decisions WHERE id = ?", (decision_id,)
    ) as cur:
        row = await cur.fetchone()
    return row["status"] if row else None


async def _finish_with_session_idle_and_join(
    stub_server, driver_task: asyncio.Task, session_id: str, *, timeout: float = 5.0,
) -> None:
    """广播 session.idle 让 driver 的 SSE 循环退出，并等待 driver_task 终结。"""
    await stub_server._broadcast_event(make_session_idle_event(session_id))
    await asyncio.wait_for(driver_task, timeout=timeout)


# ── T1.1 决策早到 ─────────────────────────────────────────────────────────────


async def test_hitl_decision_early_resolved_within_first_poll(
    temp_db, stub_server, patch_data_dir, reset_buses, fast_hitl_poll,
):
    """决策在 driver 首次 poll 之前/期间到达——driver 应回 once、任务 completed。

    时序覆盖：发权限 → 立即 resolve(approve) → driver 收到的下一次轮询拿到决策。
    """
    task_id, driver, _ = await _make_task_and_driver(
        temp_db, stub_server, decision_timeout_sec=2.0,
    )
    driver_task = asyncio.create_task(driver.run(), name=f"driver-{task_id[:6]}")

    try:
        # 等 driver 完成 SSE 订阅
        await wait_until(
            lambda: len(stub_server._sse_subscribers) > 0 and driver.session_id is not None,
            description="driver subscribed to SSE + session_id set",
        )
        session_id = driver.session_id

        # 广播权限事件
        perm_id = "per-early-1"
        await stub_server._broadcast_event(make_permission_event(perm_id, tool="bash"))

        # 立即等 pending 决策出现，写 approve 响应
        decision_id = await _wait_for_pending_decision(temp_db, task_id)
        t0 = time.monotonic()
        ok = await resolve_decision(
            temp_db, decision_id,
            DecisionResponse(decision_id=decision_id, choice=DecisionChoice.approve),
        )
        assert ok, "首次 resolve_decision 应该成功"

        # 等 driver 把决策回传给 stub（说明 _handle_permission 收尾完成）
        await wait_until(
            lambda: stub_server.permission_responses.get(perm_id) is not None,
            timeout=2.0,
            description="stub recorded permission response",
        )
        elapsed = time.monotonic() - t0
        # early case：driver 在 << 0.5s 内拿到决策（HITL_POLL_INTERVAL=20ms）
        assert elapsed < 0.5, f"early decision pickup 应该 < 0.5s，实际 {elapsed:.3f}s"

        # 收尾
        await _finish_with_session_idle_and_join(stub_server, driver_task, session_id)
    finally:
        if not driver_task.done():
            driver_task.cancel()
            try:
                await driver_task
            except BaseException:
                pass

    # ── 断言 ────────────────────────────────────────────────────────────────
    # 1) opencode 收到 "once"（approve → once 的映射）
    assert stub_server.permission_responses[perm_id] == "once"

    # 2) DB 决策行已 resolved，事件含 hitl_required + decision_received(once)
    assert await _read_decision_status(temp_db, decision_id) == "resolved"
    events = await _read_events(temp_db, task_id)
    kinds = [k for k, _ in events]
    assert "hitl_required" in kinds
    decision_payload = next(p for k, p in events if k == "decision_received")
    assert decision_payload["choice"] == "once"
    assert decision_payload["permission_id"] == perm_id

    # 3) 任务非 abort（driver 正常退出）；驱动未抛终态异常 → executor 自己写
    #    task_completed 的逻辑在 queue 层；此处只断言 driver_task 不抛
    assert driver_task.exception() is None
    assert not driver._abort_event.is_set()


# ── T1.2 决策晚到 ─────────────────────────────────────────────────────────────


async def test_hitl_decision_late_resolved_close_to_timeout(
    temp_db, stub_server, patch_data_dir, reset_buses, fast_hitl_poll,
):
    """决策在 driver 已 poll 多轮后才到达（但仍在 timeout 内）。

    用 reject 选择，验证：单次 reject 不触发 _REJECT_THRESHOLD（3）→
    driver 应回 reject、任务正常退出。
    """
    task_id, driver, _ = await _make_task_and_driver(
        temp_db, stub_server, decision_timeout_sec=2.0,
    )
    driver_task = asyncio.create_task(driver.run(), name=f"driver-{task_id[:6]}")

    try:
        await wait_until(
            lambda: len(stub_server._sse_subscribers) > 0 and driver.session_id is not None,
            description="driver subscribed to SSE + session_id set",
        )
        session_id = driver.session_id

        perm_id = "per-late-1"
        await stub_server._broadcast_event(make_permission_event(perm_id, tool="write"))

        decision_id = await _wait_for_pending_decision(temp_db, task_id)
        # 故意等 0.6s（30 轮 poll，HITL_POLL_INTERVAL=20ms），但仍 < 2s timeout
        t0 = time.monotonic()
        await asyncio.sleep(0.6)
        ok = await resolve_decision(
            temp_db, decision_id,
            DecisionResponse(decision_id=decision_id, choice=DecisionChoice.reject),
        )
        assert ok

        await wait_until(
            lambda: stub_server.permission_responses.get(perm_id) is not None,
            timeout=2.0,
            description="stub recorded reject response",
        )
        elapsed = time.monotonic() - t0
        # late case：必须 ≥ 0.5s（证明经过了多轮 poll，不是 early case）
        assert elapsed >= 0.5, f"late decision 应该 ≥ 0.5s，实际 {elapsed:.3f}s"

        await _finish_with_session_idle_and_join(stub_server, driver_task, session_id)
    finally:
        if not driver_task.done():
            driver_task.cancel()
            try:
                await driver_task
            except BaseException:
                pass

    # ── 断言 ────────────────────────────────────────────────────────────────
    # 1) opencode 收到 "reject"
    assert stub_server.permission_responses[perm_id] == "reject"

    # 2) decision_received.choice == reject
    events = await _read_events(temp_db, task_id)
    decision_payload = next(p for k, p in events if k == "decision_received")
    assert decision_payload["choice"] == "reject"

    # 3) 单次 reject 不应触发 reject_threshold → 没有 mode_escalation_suggested
    assert "mode_escalation_suggested" not in [k for k, _ in events]
    assert driver._reject_count == 1  # 仅累计一次
    assert not driver._abort_event.is_set()
    assert driver_task.exception() is None


# ── T1.3 重复提交幂等 ────────────────────────────────────────────────────────


async def test_hitl_decision_duplicate_submission_idempotent(
    temp_db, stub_server, patch_data_dir, reset_buses, fast_hitl_poll,
):
    """同一 decision_id 提交两次：第二次 resolve_decision 必须返回 False，
    driver 仅看到第一次的 choice，decision_received 事件只有 1 条。
    """
    task_id, driver, _ = await _make_task_and_driver(
        temp_db, stub_server, decision_timeout_sec=2.0,
    )
    driver_task = asyncio.create_task(driver.run(), name=f"driver-{task_id[:6]}")

    try:
        await wait_until(
            lambda: len(stub_server._sse_subscribers) > 0 and driver.session_id is not None,
            description="driver subscribed to SSE",
        )
        session_id = driver.session_id

        perm_id = "per-dup-1"
        await stub_server._broadcast_event(make_permission_event(perm_id, tool="bash"))

        decision_id = await _wait_for_pending_decision(temp_db, task_id)

        # 第一次：approve → True
        ok_1 = await resolve_decision(
            temp_db, decision_id,
            DecisionResponse(decision_id=decision_id, choice=DecisionChoice.approve),
        )
        assert ok_1 is True

        # 第二次：尝试 reject 覆盖 → 必须 False（status 已是 resolved）
        ok_2 = await resolve_decision(
            temp_db, decision_id,
            DecisionResponse(decision_id=decision_id, choice=DecisionChoice.reject),
        )
        assert ok_2 is False, "第二次 resolve_decision 应返回 False（已 resolved）"

        await wait_until(
            lambda: stub_server.permission_responses.get(perm_id) is not None,
            timeout=2.0,
            description="stub recorded permission response",
        )

        await _finish_with_session_idle_and_join(stub_server, driver_task, session_id)
    finally:
        if not driver_task.done():
            driver_task.cancel()
            try:
                await driver_task
            except BaseException:
                pass

    # ── 断言 ────────────────────────────────────────────────────────────────
    # 1) stub 收到 "once"，证明用了 approve（不是 reject）
    assert stub_server.permission_responses[perm_id] == "once"

    # 2) decision_received 事件只有 1 条
    events = await _read_events(temp_db, task_id)
    decision_received = [p for k, p in events if k == "decision_received"]
    assert len(decision_received) == 1, (
        f"重复提交不应该写出第 2 条 decision_received，实际 {len(decision_received)}"
    )
    assert decision_received[0]["choice"] == "once"

    # 3) 决策行 resolved
    assert await _read_decision_status(temp_db, decision_id) == "resolved"


# ── T1.4 决策超时 + on_timeout=abort ─────────────────────────────────────────


async def test_hitl_decision_timeout_triggers_abort_policy(
    temp_db, stub_server, patch_data_dir, reset_buses, fast_hitl_poll,
):
    """决策完全不响应 + on_timeout=abort → driver 抛 TaskAbortedError。

    断言覆盖：
        - DB decision 行被 expire_decision 标记为 timed_out
        - 事件流出现 hitl_required → hitl_timeout
        - hitl_timeout.payload.on_timeout == "abort", resolved_choice == "abort"
        - driver.run() 最终抛 TaskAbortedError(reason="hitl_timeout")
        - 出于"必须给 opencode 闭环"的约束，driver 仍会回一次 reject
          （driver._handle_permission 在 abort 路径下也调用 respond_permission）
    """
    task_id, driver, _ = await _make_task_and_driver(
        temp_db, stub_server, decision_timeout_sec=0.5, on_timeout="abort",
    )
    driver_task = asyncio.create_task(driver.run(), name=f"driver-{task_id[:6]}")

    try:
        await wait_until(
            lambda: len(stub_server._sse_subscribers) > 0 and driver.session_id is not None,
            description="driver subscribed to SSE",
        )
        session_id = driver.session_id

        perm_id = "per-to-1"
        await stub_server._broadcast_event(make_permission_event(perm_id, tool="bash"))

        # 等 _handle_permission 超时后把 hitl_timeout 写入 DB
        async def _has_hitl_timeout_event() -> bool:
            events = await _read_events(temp_db, task_id)
            return any(k == "hitl_timeout" for k, _ in events)

        await wait_until_async(
            _has_hitl_timeout_event,
            timeout=3.0,
            description="hitl_timeout event written",
        )

        # 广播 session.idle 让 SSE 循环退出 → driver 检查 _abort_event 抛错
        await stub_server._broadcast_event(make_session_idle_event(session_id))

        # driver.run() 应抛 TaskAbortedError(reason="hitl_timeout")
        with pytest.raises(TaskAbortedError) as exc_info:
            await asyncio.wait_for(driver_task, timeout=5.0)
        assert exc_info.value.reason == "hitl_timeout"
    finally:
        if not driver_task.done():
            driver_task.cancel()
            try:
                await driver_task
            except BaseException:
                pass

    # ── 断言 ────────────────────────────────────────────────────────────────
    events = await _read_events(temp_db, task_id)
    kinds = [k for k, _ in events]
    # hitl_required 必须在 hitl_timeout 之前
    assert "hitl_required" in kinds and "hitl_timeout" in kinds
    assert kinds.index("hitl_required") < kinds.index("hitl_timeout")

    timeout_payload = next(p for k, p in events if k == "hitl_timeout")
    assert timeout_payload["on_timeout"] == "abort"
    assert timeout_payload["resolved_choice"] == "abort"
    assert timeout_payload["permission_id"] == perm_id

    # 决策行被 expire_decision 标记为 timed_out
    decision_id = timeout_payload["decision_id"]
    assert await _read_decision_status(temp_db, decision_id) == "timed_out"

    # driver 仍向 opencode 闭环 reject（必须，否则 opencode 永远卡住等响应）
    assert stub_server.permission_responses.get(perm_id) == "reject", (
        "即使 abort 路径，driver 也应回 reject 给 opencode（闭环），实际："
        f"{stub_server.permission_responses!r}"
    )

    # 任务状态本身由 queue 层写入；driver 单测路径下不会自动写 aborted，
    # 但 status 字段应停留在 awaiting_human（_handle_permission 设置过）
    task = await get_task(temp_db, task_id)
    assert task is not None
    assert task.status == TaskStatus.awaiting_human
