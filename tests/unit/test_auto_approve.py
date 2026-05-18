"""
单元测试：P1-14 HitlPolicy.auto_approve 实现

修复前：HitlPolicy.auto_approve 字段在 schema 中存在，driver 完全不查 →
用户配置不生效，黑盒。

修复后：driver `_match_auto_approve` 使用 fnmatch 匹配
`<DecisionKind>:<context_key>` 形式的 pattern；命中即跳过 HITL 全流程
（hitl_required / awaiting_human / DB decision），直接 respond
opencode "once" 并写 decision_received 事件（auto_approved=True）。

验证：
    - 精确匹配命中 → 自动通过
    - 通配符 "*" / "read*" 命中
    - 未命中 → 走原 HITL 流程
    - auto_approve 为空（默认）→ 走原 HITL 流程
    - auto-approve 也重置 reject 计数器
    - decision_received 事件含 auto_approved + matched_pattern 字段
    - 不写入 DB decisions 表（hitl_required 也不发）
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from worker.adapters.opencode.driver import OpenCodeDriver
from worker.config import get_settings
from worker.contract.decision import DecisionChoice, DecisionKind
from worker.contract.task import HitlPolicy, TaskMode, TaskRequest
from worker.storage import db as db_module
from worker.storage.repo import insert_task


class DummyClient:
    def __init__(self) -> None:
        self.permission_calls: list[tuple[str, str, str]] = []

    async def respond_permission(
        self, session_id: str, permission_id: str, response: str,
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
    db_file = tmp_path / "auto_approve_test.db"
    conn = await db_module.init_db(db_file)
    try:
        yield conn
    finally:
        await db_module.close_db()


async def _make_driver(db, *, auto_approve: list[str]) -> OpenCodeDriver:
    request = TaskRequest(
        mode=TaskMode.direct_execute,
        messages=[],
        hitl_policy=HitlPolicy(
            decision_timeout_sec=10,
            on_timeout="abort",
            auto_approve=auto_approve,
        ),
    )
    response = await insert_task(db, request)
    driver = OpenCodeDriver(
        response.task_id, request, 4096,
        {"OPENCODE_SERVER_PASSWORD": "pw"}, db,
    )
    driver.client = DummyClient()
    return driver


async def _read_events(db, task_id: str) -> list[tuple[str, dict]]:
    async with db.execute(
        "SELECT kind, payload_json FROM task_events WHERE task_id = ? ORDER BY event_id",
        (task_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [(row["kind"], json.loads(row["payload_json"])) for row in rows]


async def _decision_count(db, task_id: str) -> int:
    async with db.execute(
        "SELECT COUNT(*) FROM decisions WHERE task_id = ?", (task_id,),
    ) as cur:
        row = await cur.fetchone()
    return row[0]


# ── _match_auto_approve helper ──────────────────────────────────────────


async def test_match_exact_pattern(temp_db):
    driver = await _make_driver(
        temp_db, auto_approve=["tool_permission:read_file"],
    )
    assert driver._match_auto_approve(
        DecisionKind.tool_permission, "read_file",
    ) == "tool_permission:read_file"
    assert driver._match_auto_approve(
        DecisionKind.tool_permission, "bash",
    ) is None


async def test_match_wildcard_all(temp_db):
    driver = await _make_driver(
        temp_db, auto_approve=["tool_permission:*"],
    )
    for tool in ["read", "write", "bash", "exotic_tool"]:
        assert driver._match_auto_approve(
            DecisionKind.tool_permission, tool,
        ) == "tool_permission:*"


async def test_match_wildcard_prefix(temp_db):
    driver = await _make_driver(
        temp_db, auto_approve=["tool_permission:read*"],
    )
    assert driver._match_auto_approve(
        DecisionKind.tool_permission, "read_file",
    ) == "tool_permission:read*"
    assert driver._match_auto_approve(
        DecisionKind.tool_permission, "readlink",
    ) == "tool_permission:read*"
    # bash 不匹配 read*
    assert driver._match_auto_approve(
        DecisionKind.tool_permission, "bash",
    ) is None


async def test_match_returns_first_pattern(temp_db):
    """多 pattern 命中时返回首个，便于审计稳定性。"""
    driver = await _make_driver(
        temp_db,
        auto_approve=[
            "tool_permission:read*",
            "tool_permission:read_file",
        ],
    )
    assert driver._match_auto_approve(
        DecisionKind.tool_permission, "read_file",
    ) == "tool_permission:read*"


async def test_no_policy_returns_none(temp_db):
    driver = await _make_driver(temp_db, auto_approve=[])
    assert driver._match_auto_approve(
        DecisionKind.tool_permission, "anything",
    ) is None


# ── _handle_permission 端到端 ──────────────────────────────────────────


async def test_auto_approve_skips_hitl_and_responds_once(temp_db):
    """auto_approve 命中：不发 hitl_required，不写 decision，直接 respond once。"""
    driver = await _make_driver(
        temp_db, auto_approve=["tool_permission:read"],
    )

    # _wait_for_decision 不应被调用
    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("_wait_for_decision should not be called")
    driver._wait_for_decision = fail_if_called

    await driver._handle_permission(
        "sess-1",
        {"permission_id": "perm-1", "tool": "read",
         "description": "read file", "args": {}, "title": "perm"},
    )

    # 验证 opencode 收到了 "once"
    assert driver.client.permission_calls == [("sess-1", "perm-1", "once")]

    # 验证未写 decision
    assert await _decision_count(temp_db, driver.task_id) == 0

    # 验证未发 hitl_required，但发了 decision_received(auto_approved=True)
    events = await _read_events(temp_db, driver.task_id)
    kinds = [k for k, _ in events]
    assert "hitl_required" not in kinds
    assert "decision_received" in kinds
    received = next(p for k, p in events if k == "decision_received")
    assert received["auto_approved"] is True
    assert received["matched_pattern"] == "tool_permission:read"
    assert received["choice"] == "once"
    assert received["tool"] == "read"


async def test_no_match_falls_through_to_hitl(temp_db):
    """auto_approve 未命中：原 HITL 流程 → hitl_required 事件被写入。"""
    driver = await _make_driver(
        temp_db, auto_approve=["tool_permission:read"],
    )

    async def unresolved(_decision_id: str, _timeout: float):
        return None  # 模拟超时
    driver._wait_for_decision = unresolved

    await driver._handle_permission(
        "sess-1",
        {"permission_id": "perm-2", "tool": "bash",
         "description": "rm", "args": {}, "title": "perm"},
    )

    events = await _read_events(temp_db, driver.task_id)
    kinds = [k for k, _ in events]
    assert "hitl_required" in kinds
    assert await _decision_count(temp_db, driver.task_id) == 1


async def test_auto_approve_resets_reject_counter(temp_db):
    """auto-approve 视同用户 approve，重置 reject 计数。"""
    driver = await _make_driver(
        temp_db, auto_approve=["tool_permission:safe_tool"],
    )
    driver._reject_count = 2  # 假装之前累积过 reject

    await driver._handle_permission(
        "sess-1",
        {"permission_id": "perm-3", "tool": "safe_tool",
         "description": "", "args": {}, "title": ""},
    )

    assert driver._reject_count == 0


async def test_empty_auto_approve_uses_hitl(temp_db):
    """auto_approve 为空（默认）：所有请求走 HITL。"""
    driver = await _make_driver(temp_db, auto_approve=[])

    async def unresolved(_decision_id: str, _timeout: float):
        return None
    driver._wait_for_decision = unresolved

    await driver._handle_permission(
        "sess-1",
        {"permission_id": "perm-4", "tool": "anything",
         "description": "", "args": {}, "title": ""},
    )

    events = await _read_events(temp_db, driver.task_id)
    kinds = [k for k, _ in events]
    assert "hitl_required" in kinds
