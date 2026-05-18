"""
单元测试：P1-15 HITL reject 计数器上限

修复前：opencode 的 reject 是单次拒绝（工具会再 ask）；driver 没有计数上限，
极端情况下 user 反复 reject → opencode 反复 ask → 死循环到 timeout。

修复后：driver 累积连续 reject 计数，达到 _REJECT_THRESHOLD（=3）时：
    - 发出 mode_escalation_suggested 事件（reason=reject_threshold_exceeded）
    - 调用 _signal_abort，让 SSE 消费循环退出 → 任务终态 aborted

验证：
    - 累计 3 次 reject 后 abort_event 被设置
    - mode_escalation_suggested 事件写入并带 reject_count=3
    - approve 重置计数（防误伤）
    - abort 直接走原 abort 路径，不进入 reject 计数器
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from worker.adapters.opencode.driver import OpenCodeDriver, _REJECT_THRESHOLD
from worker.config import get_settings
from worker.contract.decision import (
    DecisionChoice,
    DecisionKind,
    DecisionRequest,
    DecisionResponse,
    PendingDecision,
)
from worker.contract.task import HitlPolicy, TaskMode, TaskRequest
from worker.storage import db as db_module
from worker.storage.repo import insert_task


class DummyClient:
    def __init__(self) -> None:
        self.permission_calls: list[tuple[str, str, str]] = []

    async def respond_permission(
        self, session_id: str, permission_id: str, response: str
    ) -> None:
        self.permission_calls.append((session_id, permission_id, response))


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    monkeypatch.setenv("WORKER_BEARER_TOKEN", "test-token")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def temp_db(tmp_path: Path):
    db_file = tmp_path / "reject_test.db"
    conn = await db_module.init_db(db_file)
    try:
        yield conn
    finally:
        await db_module.close_db()


async def _make_driver(db) -> OpenCodeDriver:
    request = TaskRequest(
        mode=TaskMode.direct_execute,
        messages=[],
        hitl_policy=HitlPolicy(decision_timeout_sec=10, on_timeout="abort"),
    )
    response = await insert_task(db, request)
    driver = OpenCodeDriver(
        response.task_id, request, 4096,
        {"OPENCODE_SERVER_PASSWORD": "pw"}, db,
    )
    driver.client = DummyClient()
    return driver


def _resolved(decision_id: str, choice: DecisionChoice) -> PendingDecision:
    """构造一条 resolved PendingDecision 模拟用户选择。"""
    req = DecisionRequest(
        decision_id=decision_id,
        kind=DecisionKind.tool_permission,
        summary="test",
        options=[DecisionChoice.approve, DecisionChoice.reject, DecisionChoice.abort],
    )
    resp = DecisionResponse(decision_id=decision_id, choice=choice)
    return PendingDecision(
        decision_id=decision_id,
        task_id="t1",
        kind=DecisionKind.tool_permission,
        status="resolved",
        request=req,
        response=resp,
        created_at=0.0,
        resolved_at=1.0,
    )


async def _read_events(db, task_id: str) -> list[tuple[str, dict]]:
    async with db.execute(
        "SELECT kind, payload_json FROM task_events WHERE task_id = ? ORDER BY event_id",
        (task_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [(row["kind"], json.loads(row["payload_json"])) for row in rows]


async def _send_perm(driver: OpenCodeDriver, perm_id: str) -> None:
    await driver._handle_permission(
        "sess-1",
        {
            "permission_id": perm_id,
            "tool": "bash",
            "description": "rm something",
            "args": {"cmd": "rm -rf foo"},
            "title": "bash permission",
        },
    )


async def test_three_consecutive_rejects_trigger_abort(temp_db):
    """连续 3 次 reject 触发 abort_event + mode_escalation_suggested 事件。"""
    driver = await _make_driver(temp_db)
    assert _REJECT_THRESHOLD == 3  # 测试假设阈值=3

    async def reject(decision_id: str, _timeout: float):
        return _resolved(decision_id, DecisionChoice.reject)

    driver._wait_for_decision = reject

    for i in range(3):
        await _send_perm(driver, f"perm-{i}")

    assert driver._reject_count == 3
    assert driver._abort_event.is_set()
    assert driver._abort_reason == "reject_threshold_exceeded"

    events = await _read_events(temp_db, driver.task_id)
    escalations = [
        payload for kind, payload in events
        if kind == "mode_escalation_suggested"
    ]
    assert len(escalations) == 1, f"expected 1 escalation event, got {escalations}"
    assert escalations[0]["reason"] == "reject_threshold_exceeded"
    assert escalations[0]["reject_count"] == 3
    assert escalations[0]["threshold"] == _REJECT_THRESHOLD


async def test_two_rejects_do_not_abort(temp_db):
    """2 次 reject 不触发 abort（在阈值以下）。"""
    driver = await _make_driver(temp_db)

    async def reject(decision_id: str, _timeout: float):
        return _resolved(decision_id, DecisionChoice.reject)

    driver._wait_for_decision = reject

    for i in range(2):
        await _send_perm(driver, f"perm-{i}")

    assert driver._reject_count == 2
    assert not driver._abort_event.is_set()

    events = await _read_events(temp_db, driver.task_id)
    escalations = [
        kind for kind, _ in events if kind == "mode_escalation_suggested"
    ]
    assert escalations == [], "should not escalate before threshold"


async def test_approve_resets_reject_counter(temp_db):
    """approve 把连续 reject 计数清零，避免误伤。"""
    driver = await _make_driver(temp_db)

    choices = [DecisionChoice.reject, DecisionChoice.reject,
               DecisionChoice.approve, DecisionChoice.reject,
               DecisionChoice.reject]

    async def by_choice(decision_id: str, _timeout: float):
        choice = choices.pop(0)
        return _resolved(decision_id, choice)

    driver._wait_for_decision = by_choice

    for i in range(5):
        await _send_perm(driver, f"perm-{i}")

    # 序列：reject(1) reject(2) approve(reset→0) reject(1) reject(2)
    assert driver._reject_count == 2
    assert not driver._abort_event.is_set()


async def test_explicit_abort_does_not_increment_reject_counter(temp_db):
    """user 直接选 abort：走原 abort 路径，不进 reject 计数器。"""
    driver = await _make_driver(temp_db)

    async def abort(decision_id: str, _timeout: float):
        return _resolved(decision_id, DecisionChoice.abort)

    driver._wait_for_decision = abort

    await _send_perm(driver, "perm-abort")

    assert driver._reject_count == 0
    assert driver._abort_event.is_set()
    assert driver._abort_reason == "permission_rejected"  # 原有 abort 路径的 reason
