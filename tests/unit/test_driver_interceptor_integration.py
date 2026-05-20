"""
单元测试：W2-1 OpenCodeDriver 与拦截器集成

覆盖设计文档 §7.3：
    - 默认 interceptors 参数为空时 driver 行为不变
    - _dispatch_to_interceptors 构造正确的 InterceptorEvent
    - _dispatch_terminal_and_flush 触发 dispatch + flush + 标准登记
    - _register_interceptor_artifact 路径越权被拒
    - _register_interceptor_artifact 文件缺失被拒
    - _register_interceptor_artifact 非法 ArtifactType 被拒
    - 拦截器异常不影响主流程（终态 status 不被污染）
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from worker.adapters.opencode.driver import OpenCodeDriver
from worker.adapters.opencode.interceptors import EventInterceptor
from worker.adapters.opencode.interceptors.types import (
    InterceptorArtifact,
    InterceptorEvent,
    TerminalSignal,
)
from worker.contract.task import (
    InterceptorConfig,
    OpencodeProfile,
    TaskMode,
    TaskRequest,
    TaskStatus,
)
from worker.storage import db as db_module
from worker.storage.repo import insert_task, list_artifacts


@pytest.fixture
async def temp_db(tmp_path: Path):
    db_file = tmp_path / "interceptor_driver.db"
    conn = await db_module.init_db(db_file)
    try:
        yield conn
    finally:
        await db_module.close_db()


@pytest.fixture
def settings_artifacts_dir(tmp_path, monkeypatch):
    """把 settings.data_dir 指向 tmp_path/data，artifacts_dir 自然派生。

    Settings.artifacts_dir 是 property（data_dir/artifacts），不能直接 setattr；
    改 data_dir 即可让 artifacts_dir 重定向到测试目录。
    """
    from worker import config as config_module

    data_dir = tmp_path / "data"
    art_dir = data_dir / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    settings = config_module.get_settings()
    monkeypatch.setattr(settings, "data_dir", data_dir)
    return art_dir


class _Recording(EventInterceptor):
    def __init__(self, name: str, artifact_path: Optional[Path] = None):
        self._name = name
        self._artifact_path = artifact_path
        self.events: list[InterceptorEvent] = []
        self.terminals: list[TerminalSignal] = []
        self.flush_count = 0

    @property
    def name(self) -> str:
        return self._name

    async def on_event(self, event):
        self.events.append(event)

    async def on_terminal(self, signal):
        self.terminals.append(signal)

    async def flush(self):
        self.flush_count += 1
        if self._artifact_path is None:
            return None
        return InterceptorArtifact(
            artifact_type="custom",
            filename=self._artifact_path.name,
            local_path=str(self._artifact_path),
            metadata={"subtype": "test", "name": self._name},
        )


def _make_driver(task_id: str, request: TaskRequest, db, interceptors=()):
    return OpenCodeDriver(
        task_id=task_id,
        request=request,
        host_port=4096,
        container_env={"OPENCODE_SERVER_PASSWORD": "x"},
        db=db,
        interceptors=interceptors,
    )


async def _make_task_id(db) -> str:
    req = TaskRequest(mode=TaskMode.direct_execute, messages=[])
    resp = await insert_task(db, req)
    return resp.task_id


# ── 默认空拦截器：行为不变 ────────────────────────────────────────────────


async def test_driver_default_interceptors_empty(temp_db):
    """构造时不传 interceptors → runner 持有空列表，dispatch 全部 no-op。"""
    task_id = await _make_task_id(temp_db)
    req = TaskRequest(mode=TaskMode.direct_execute, messages=[])
    driver = _make_driver(task_id, req, temp_db)
    assert driver._runner.interceptors == []
    # dispatch 不抛错
    await driver._dispatch_to_interceptors(
        normalized_kind="assistant_delta",
        normalized_payload={"content": "x"},
        raw_type="message.part.delta",
    )
    await driver._dispatch_terminal_and_flush(TaskStatus.completed, None)


# ── dispatch_to_interceptors ──────────────────────────────────────────────


async def test_dispatch_to_interceptors_constructs_event(temp_db):
    task_id = await _make_task_id(temp_db)
    req = TaskRequest(mode=TaskMode.direct_execute, messages=[])
    ic = _Recording("rec-a")
    driver = _make_driver(task_id, req, temp_db, interceptors=[ic])
    driver.session_id = "sess-1"
    await driver._dispatch_to_interceptors(
        normalized_kind="assistant_delta",
        normalized_payload={"content": "hello"},
        raw_type="message.part.delta",
        raw_payload={"part": {"text": "hello"}},
    )
    assert len(ic.events) == 1
    e = ic.events[0]
    assert e.task_id == task_id
    assert e.session_id == "sess-1"
    assert e.normalized_kind == "assistant_delta"
    assert e.normalized_payload == {"content": "hello"}
    assert e.raw_type == "message.part.delta"
    assert e.raw_payload == {"part": {"text": "hello"}}


# ── 终态分发 + 产物登记 ──────────────────────────────────────────────────


async def test_terminal_dispatch_invokes_all_hooks(temp_db, settings_artifacts_dir):
    task_id = await _make_task_id(temp_db)
    req = TaskRequest(mode=TaskMode.direct_execute, messages=[])

    # 准备一个真实文件供拦截器返回
    art_dir = settings_artifacts_dir / task_id
    art_dir.mkdir(parents=True, exist_ok=True)
    art_file = art_dir / "rec-a.json"
    art_file.write_text('{"hello":"world"}')

    ic = _Recording("rec-a", artifact_path=art_file)
    driver = _make_driver(task_id, req, temp_db, interceptors=[ic])
    driver.session_id = "s1"

    await driver._dispatch_terminal_and_flush(TaskStatus.completed, None)

    assert len(ic.terminals) == 1
    assert ic.terminals[0].status == "completed"
    assert ic.terminals[0].reason is None
    assert ic.flush_count == 1

    # DB 中应有一条 artifact
    arts = await list_artifacts(temp_db, task_id)
    assert len(arts) == 1
    assert arts[0].filename == "rec-a.json"
    assert arts[0].metadata.get("subtype") == "test"


async def test_terminal_dispatch_for_aborted_status(temp_db, settings_artifacts_dir):
    task_id = await _make_task_id(temp_db)
    req = TaskRequest(mode=TaskMode.direct_execute, messages=[])
    ic = _Recording("rec-abort")
    driver = _make_driver(task_id, req, temp_db, interceptors=[ic])
    await driver._dispatch_terminal_and_flush(TaskStatus.aborted, "user_requested")
    assert ic.terminals[0].status == "aborted"
    assert ic.terminals[0].reason == "user_requested"


# ── 产物登记安全校验 ─────────────────────────────────────────────────────


async def test_artifact_registration_rejects_path_escape(
    temp_db, settings_artifacts_dir, caplog
):
    """拦截器返回 /etc/passwd 类越权路径 → 拒绝登记，不抛错。"""
    task_id = await _make_task_id(temp_db)
    req = TaskRequest(mode=TaskMode.direct_execute, messages=[])
    driver = _make_driver(task_id, req, temp_db, interceptors=[])
    driver.session_id = "s1"

    bad = InterceptorArtifact(
        artifact_type="custom",
        filename="evil.json",
        local_path="/etc/passwd",  # 显然不在 artifacts_dir/task_id 下
    )
    # 直接调内部 helper（不靠拦截器）以隔离测试
    await driver._register_interceptor_artifact(bad)

    arts = await list_artifacts(temp_db, task_id)
    assert len(arts) == 0


async def test_artifact_registration_rejects_missing_file(
    temp_db, settings_artifacts_dir
):
    task_id = await _make_task_id(temp_db)
    req = TaskRequest(mode=TaskMode.direct_execute, messages=[])
    driver = _make_driver(task_id, req, temp_db, interceptors=[])
    art_dir = settings_artifacts_dir / task_id
    art_dir.mkdir(parents=True, exist_ok=True)
    fake = art_dir / "ghost.json"  # 不创建该文件
    await driver._register_interceptor_artifact(InterceptorArtifact(
        artifact_type="custom", filename="ghost.json", local_path=str(fake),
    ))
    arts = await list_artifacts(temp_db, task_id)
    assert len(arts) == 0


async def test_artifact_registration_rejects_unknown_type(
    temp_db, settings_artifacts_dir
):
    task_id = await _make_task_id(temp_db)
    req = TaskRequest(mode=TaskMode.direct_execute, messages=[])
    driver = _make_driver(task_id, req, temp_db, interceptors=[])
    art_dir = settings_artifacts_dir / task_id
    art_dir.mkdir(parents=True, exist_ok=True)
    f = art_dir / "ok.json"
    f.write_text("{}")
    await driver._register_interceptor_artifact(InterceptorArtifact(
        artifact_type="not-a-real-type",
        filename="ok.json", local_path=str(f),
    ))
    arts = await list_artifacts(temp_db, task_id)
    assert len(arts) == 0


# ── 拦截器抛错隔离（终态语义不被污染）────────────────────────────────────


class _Boom(EventInterceptor):
    @property
    def name(self) -> str:
        return "boom-ic"

    async def on_event(self, event):
        raise RuntimeError("event-boom")

    async def on_terminal(self, signal):
        raise RuntimeError("terminal-boom")

    async def flush(self):
        raise RuntimeError("flush-boom")


async def test_terminal_dispatch_isolated_from_main_flow(
    temp_db, settings_artifacts_dir
):
    """拦截器三 hook 全部抛错 → driver._dispatch_terminal_and_flush 不抛。"""
    task_id = await _make_task_id(temp_db)
    req = TaskRequest(mode=TaskMode.direct_execute, messages=[])
    driver = _make_driver(task_id, req, temp_db, interceptors=[_Boom()])
    # 三 hook 都炸；这里如果有未捕获异常会让 await 失败
    await driver._dispatch_to_interceptors(
        normalized_kind="x", normalized_payload={}, raw_type="r",
    )
    await driver._dispatch_terminal_and_flush(TaskStatus.completed, None)


# ── orchestrator 注入路径 ───────────────────────────────────────────────


async def test_opencode_profile_carries_interceptor_config():
    """OpencodeProfile.interceptors 字段持久化往返，serialize/deserialize 不丢。"""
    profile = OpencodeProfile(
        interceptors=[
            InterceptorConfig(name="conversations", options={"max_size": 1024}),
            InterceptorConfig(name="backtest"),
        ],
    )
    j = profile.model_dump_json()
    parsed = OpencodeProfile.model_validate_json(j)
    assert len(parsed.interceptors) == 2
    assert parsed.interceptors[0].name == "conversations"
    assert parsed.interceptors[0].options == {"max_size": 1024}
    assert parsed.interceptors[1].options == {}
