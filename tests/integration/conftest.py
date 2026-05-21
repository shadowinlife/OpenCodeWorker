"""
集成层共用 fixture（T1 HITL 时序 + T2 终态全链路 E2E）。

为什么集中：
    HITL 时序与 abort/timeout 全链路两组测试都需要：
        - 独立 SQLite DB（防跨用例污染）
        - StubOpenCodeServer 取代真实 opencode 容器
        - settings.data_dir 重定向（artifacts 写入隔离）
        - queue / event_bus 模块级全局状态隔离
        - HITL 轮询常量缩短到亚秒级（默认 2s 会让单测跑 30s+）
    放在 conftest.py 让两份测试文件零拷贝共享，也避免任一遗忘清理钩子。

WORKER_BEARER_TOKEN 由顶层 ``tests/conftest.py`` 在收集期统一注入；
本文件不重复处理。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

import pytest

from tests.fixtures.stub_opencode_server import StubOpenCodeServer
from worker.adapters.opencode import driver as driver_module
from worker.orchestrator import event_bus
from worker.orchestrator import queue as queue_module
from worker.storage import db as db_module


# ── DB ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def temp_db(tmp_path: Path):
    """每个用例独立 SQLite DB（init_db / close_db 配对）。"""
    db_file = tmp_path / "integration.db"
    conn = await db_module.init_db(db_file)
    try:
        yield conn
    finally:
        await db_module.close_db()


# ── StubOpenCodeServer ────────────────────────────────────────────────────────


@pytest.fixture
async def stub_server():
    """启动一个 StubOpenCodeServer，yield 实例；退出时关停。

    使用 stub 的 ``_broadcast_event`` 主动注入事件，比 ``schedule_events``
    更稳——它不依赖 driver 已经创建 session 也不需要预排时序。
    """
    server = StubOpenCodeServer(password="stub-pw")
    async with server.run():
        yield server


# ── 配置重定向 ────────────────────────────────────────────────────────────────


@pytest.fixture
def patch_data_dir(tmp_path, monkeypatch):
    """把全局 Settings.data_dir 重定向到 tmp_path/data。

    与 [tests/integration/test_w_dod_smoke.py::patched_data_dir] 同款做法，
    复用是为了让产物收集（_collect_artifacts / interceptors）落在沙箱里。
    """
    from worker import config as config_module

    data_dir = tmp_path / "data"
    art_dir = data_dir / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    settings = config_module.get_settings()
    monkeypatch.setattr(settings, "data_dir", data_dir)
    return art_dir


# ── 模块级全局状态隔离 ────────────────────────────────────────────────────────


@pytest.fixture
def reset_queue_state():
    """保存/恢复 queue 全局 executor + semaphore，避免跨用例污染。

    复刻 [tests/unit/test_terminal_dispatch.py::reset_queue_state] 的语义。
    """
    saved_executor = queue_module._task_executor
    saved_semaphore = queue_module._semaphore
    queue_module._semaphore = asyncio.Semaphore(1)
    yield
    queue_module._task_executor = saved_executor
    queue_module._semaphore = saved_semaphore


@pytest.fixture
def reset_buses():
    """清理 event_bus 模块的 _buses 字典（前后各一次）。"""
    event_bus._buses.clear()
    yield
    event_bus._buses.clear()


# ── HITL 加速 ─────────────────────────────────────────────────────────────────


@pytest.fixture
def fast_hitl_poll(monkeypatch):
    """把 driver._HITL_POLL_INTERVAL 从 2s 调到 20ms，缩短测试耗时。

    HitlPolicy.decision_timeout_sec 仍然按业务设置；只是把"多久轮询一次"
    压缩，让 1.5s 超时的用例真正在 1.5s 完成而不是被 2s 轮询粘住。
    """
    monkeypatch.setattr(driver_module, "_HITL_POLL_INTERVAL", 0.02)
    return 0.02


# ── 异步轮询 helper ──────────────────────────────────────────────────────────


async def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 3.0,
    interval: float = 0.01,
    description: str = "predicate",
) -> None:
    """轮询 ``predicate()``，True 即返回；timeout 内未 True 则 AssertionError。

    用法：
        await wait_until(
            lambda: len(stub_server._sse_subscribers) > 0,
            description="driver subscribed to SSE",
        )
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception:
            # predicate 内部异常也算未达；忽略再试
            pass
        await asyncio.sleep(interval)
    raise AssertionError(
        f"wait_until timed out after {timeout:.2f}s: {description}"
    )


async def wait_until_async(
    predicate: Callable[[], Awaitable[bool]],
    *,
    timeout: float = 3.0,
    interval: float = 0.01,
    description: str = "async predicate",
) -> None:
    """异步版 wait_until：predicate 是返回 awaitable bool 的可调用对象。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if await predicate():
                return
        except Exception:
            pass
        await asyncio.sleep(interval)
    raise AssertionError(
        f"wait_until_async timed out after {timeout:.2f}s: {description}"
    )


# ── 事件构造 helper ──────────────────────────────────────────────────────────


def make_permission_event(
    permission_id: str,
    *,
    tool: str = "bash",
    description: str = "permission needed",
    args: Optional[dict] = None,
    title: str = "tool permission",
) -> dict:
    """合成一个 stub 可广播的 ``message.part.updated`` 权限请求事件。

    与 [src/worker/adapters/opencode/event_stream.py::extract_permission_request]
    的"方式 2"对齐：part.permissionId 以 ``per`` 开头即被 driver 识别。
    """
    return {
        "type": "message.part.updated",
        "payload": {
            "part": {
                "permissionId": permission_id,
                "toolName": tool,
                "message": description,
                "input": args or {},
                "title": title,
            }
        },
    }


def make_session_idle_event(session_id: str) -> dict:
    """合成 session.idle 事件，让 driver 的 SSE 消费循环退出。"""
    return {
        "type": "session.idle",
        "payload": {"sessionID": session_id},
    }
