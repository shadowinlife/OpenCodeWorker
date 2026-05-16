"""单元测试：P1-13 HITL on_timeout=continue|escalate 语义。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from worker.adapters.opencode.driver import AGENT_SISYPHUS, OpenCodeDriver
from worker.config import get_settings
from worker.contract.task import HitlPolicy, TaskMode, TaskRequest, TaskStatus
from worker.storage import db as db_module
from worker.storage.repo import get_task, insert_task


class DummyClient:
    def __init__(self) -> None:
        self.permission_calls: list[tuple[str, str, str]] = []
        self.prompt_calls: list[dict] = []

    async def respond_permission(
        self,
        session_id: str,
        permission_id: str,
        response: str,
    ) -> None:
        self.permission_calls.append((session_id, permission_id, response))

    async def prompt_async(self, **kwargs) -> None:
        self.prompt_calls.append(kwargs)


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    monkeypatch.setenv("WORKER_BEARER_TOKEN", "test-token")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def temp_db(tmp_path: Path):
    db_file = tmp_path / "worker_test.db"
    conn = await db_module.init_db(db_file)
    try:
        yield conn
    finally:
        await db_module.close_db()


async def _make_driver(db, *, mode: TaskMode, on_timeout: str) -> OpenCodeDriver:
    request = TaskRequest(
        mode=mode,
        messages=[],
        hitl_policy=HitlPolicy(decision_timeout_sec=1, on_timeout=on_timeout),
    )
    response = await insert_task(db, request)
    driver = OpenCodeDriver(
        response.task_id,
        request,
        4096,
        {"OPENCODE_SERVER_PASSWORD": "pw"},
        db,
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


async def _read_latest_decision_request(db, task_id: str) -> dict:
    async with db.execute(
        "SELECT request_json FROM decisions WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
        (task_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    return json.loads(row["request_json"])


@pytest.mark.parametrize("on_timeout", ["continue", "escalate"])
async def test_permission_timeout_auto_continues(temp_db, on_timeout: str):
    driver = await _make_driver(
        temp_db,
        mode=TaskMode.direct_execute,
        on_timeout=on_timeout,
    )

    async def unresolved(_decision_id: str, _timeout_sec: float):
        return None

    driver._wait_for_decision = unresolved

    await driver._handle_permission(
        "sess-1",
        {
            "permission_id": "per-1",
            "tool": "bash",
            "description": "run tests",
            "args": {"cmd": "pytest"},
            "title": "bash permission",
        },
    )

    task = await get_task(temp_db, driver.task_id)
    assert task is not None
    assert task.status == TaskStatus.executing
    assert not driver._abort_event.is_set()

    decision_req = await _read_latest_decision_request(temp_db, driver.task_id)
    assert decision_req["default_on_timeout"] == "approve"

    events = await _read_events(temp_db, driver.task_id)
    hitl_timeout = next(payload for kind, payload in events if kind == "hitl_timeout")
    assert hitl_timeout["on_timeout"] == on_timeout
    assert hitl_timeout["resolved_choice"] == "approve"

    decision_received = next(
        payload for kind, payload in reversed(events) if kind == "decision_received"
    )
    assert decision_received["choice"] == "once"

    assert driver.client.permission_calls == [("sess-1", "per-1", "once")]


@pytest.mark.parametrize("on_timeout", ["continue", "escalate"])
async def test_plan_timeout_auto_approves(temp_db, on_timeout: str):
    driver = await _make_driver(
        temp_db,
        mode=TaskMode.plan_first,
        on_timeout=on_timeout,
    )
    driver._plan_text = "1. inspect\n2. edit\n3. run tests"

    async def unresolved(_decision_id: str, _timeout_sec: float):
        return None

    async def no_sse(_session_id: str):
        return None

    driver._wait_for_decision = unresolved
    driver._consume_sse = no_sse

    await driver._handle_plan_approval("sess-2")

    task = await get_task(temp_db, driver.task_id)
    assert task is not None
    assert task.status == TaskStatus.executing
    assert not driver._abort_event.is_set()

    decision_req = await _read_latest_decision_request(temp_db, driver.task_id)
    assert decision_req["default_on_timeout"] == "approve"

    events = await _read_events(temp_db, driver.task_id)
    hitl_timeout = next(payload for kind, payload in events if kind == "hitl_timeout")
    assert hitl_timeout["on_timeout"] == on_timeout
    assert hitl_timeout["resolved_choice"] == "approve"

    decision_received = next(
        payload for kind, payload in reversed(events) if kind == "decision_received"
    )
    assert decision_received["choice"] == "approve"

    execution_started = next(
        payload for kind, payload in reversed(events) if kind == "execution_started"
    )
    assert execution_started["mode"] == TaskMode.plan_first.value
    assert execution_started["plan_approved"] is True

    assert len(driver.client.prompt_calls) == 1
    prompt_call = driver.client.prompt_calls[0]
    assert prompt_call["session_id"] == "sess-2"
    assert prompt_call["agent"] == AGENT_SISYPHUS
    assert prompt_call["parts"] == [{"type": "text", "text": "/start-work"}]