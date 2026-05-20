"""
单元测试：W2-1 InterceptorRunner 错误隔离与调度

覆盖设计文档 §7.2：
    - dispatch_event 对所有拦截器分发
    - 单个 on_event 抛错不影响兄弟拦截器
    - dispatch_terminal 同上
    - collect_artifacts sequential + 错误隔离
    - 累计错误超 budget 后拦截器被 quiet disable
    - 重名 / 非法 name 在构造时被拒
"""
from __future__ import annotations

from typing import Optional

import pytest

from worker.adapters.opencode.interceptors import (
    EventInterceptor,
    InterceptorRunner,
)
from worker.adapters.opencode.interceptors.types import (
    InterceptorArtifact,
    InterceptorEvent,
    TerminalSignal,
)


def _make_event() -> InterceptorEvent:
    return InterceptorEvent(
        task_id="t1", session_id="s1",
        normalized_kind="assistant_delta",
        normalized_payload={"content": "x"},
        raw_type="message.part.delta",
        raw_payload={},
        received_at=0.0,
    )


def _make_terminal() -> TerminalSignal:
    return TerminalSignal(
        task_id="t1", session_id="s1",
        status="completed", reason=None, ended_at=0.0,
    )


class _Recording(EventInterceptor):
    """记录所有 hook 调用次数的拦截器。"""

    def __init__(self, name: str, fail_on: Optional[str] = None,
                 fail_count: int = 1):
        self._name = name
        self._fail_on = fail_on
        self._fail_remaining = fail_count
        self.events: list[InterceptorEvent] = []
        self.terminals: list[TerminalSignal] = []
        self.flush_calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def on_event(self, event):
        if self._fail_on == "on_event" and self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise RuntimeError("boom-event")
        self.events.append(event)

    async def on_terminal(self, signal):
        if self._fail_on == "on_terminal" and self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise RuntimeError("boom-terminal")
        self.terminals.append(signal)

    async def flush(self):
        self.flush_calls += 1
        if self._fail_on == "flush" and self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise RuntimeError("boom-flush")
        return InterceptorArtifact(
            artifact_type="custom",
            filename=f"{self._name}.json",
            local_path=f"/tmp/{self._name}.json",
        )


# ── 构造器校验 ─────────────────────────────────────────────────────────────


def test_runner_rejects_duplicate_names():
    a = _Recording("alpha")
    b = _Recording("alpha")
    with pytest.raises(ValueError, match="duplicate"):
        InterceptorRunner([a, b])


def test_runner_rejects_invalid_name():
    bad = _Recording("BAD_NAME")
    with pytest.raises(ValueError):
        InterceptorRunner([bad])


def test_runner_empty_is_ok():
    runner = InterceptorRunner([])
    assert runner.interceptors == []


# ── dispatch_event ─────────────────────────────────────────────────────────


async def test_dispatch_event_to_all():
    a = _Recording("alpha")
    b = _Recording("beta")
    runner = InterceptorRunner([a, b])
    evt = _make_event()
    await runner.dispatch_event(evt)
    assert len(a.events) == 1
    assert len(b.events) == 1


async def test_dispatch_event_isolates_error():
    """一个 on_event raise，其他仍正常收到事件。"""
    bad = _Recording("bad-one", fail_on="on_event", fail_count=99)
    good = _Recording("good-one")
    runner = InterceptorRunner([bad, good])
    for _ in range(3):
        await runner.dispatch_event(_make_event())
    assert len(good.events) == 3
    assert len(bad.events) == 0
    assert runner.error_count("bad-one") == 3


# ── dispatch_terminal ──────────────────────────────────────────────────────


async def test_dispatch_terminal_to_all():
    a = _Recording("alpha")
    b = _Recording("beta")
    runner = InterceptorRunner([a, b])
    await runner.dispatch_terminal(_make_terminal())
    assert len(a.terminals) == 1
    assert len(b.terminals) == 1


async def test_dispatch_terminal_isolates_error():
    bad = _Recording("bad-t", fail_on="on_terminal", fail_count=1)
    good = _Recording("good-t")
    runner = InterceptorRunner([bad, good])
    await runner.dispatch_terminal(_make_terminal())
    assert len(good.terminals) == 1
    assert len(bad.terminals) == 0
    assert runner.error_count("bad-t") == 1


# ── collect_artifacts ──────────────────────────────────────────────────────


async def test_collect_artifacts_sequential_returns_all():
    a = _Recording("alpha")
    b = _Recording("beta")
    runner = InterceptorRunner([a, b])
    arts = await runner.collect_artifacts()
    assert len(arts) == 2
    names = {art.filename for art in arts}
    assert names == {"alpha.json", "beta.json"}


async def test_collect_artifacts_isolates_flush_error():
    """单个 flush 失败不影响其他拦截器返回产物。"""
    bad = _Recording("bad-f", fail_on="flush", fail_count=1)
    good = _Recording("good-f")
    runner = InterceptorRunner([bad, good])
    arts = await runner.collect_artifacts()
    # bad 抛错 → 不计入 arts；good 正常返回
    assert len(arts) == 1
    assert arts[0].filename == "good-f.json"
    # flush 都被调用过
    assert bad.flush_calls == 1
    assert good.flush_calls == 1


# ── 错误预算 / disable ─────────────────────────────────────────────────────


async def test_runner_disables_after_budget():
    """连续抛错达到 budget 后该拦截器被静默 disable。"""
    bad = _Recording("bad-budget", fail_on="on_event", fail_count=999)
    good = _Recording("good-budget")
    runner = InterceptorRunner([bad, good], error_budget=3)
    for _ in range(5):
        await runner.dispatch_event(_make_event())
    assert runner.is_disabled("bad-budget")
    # good 全程不受影响
    assert len(good.events) == 5
    # bad 不再被调用：累计错误数停在 budget 上（最后两次直接跳过）
    assert runner.error_count("bad-budget") == 3


async def test_disabled_interceptor_skipped_in_terminal_and_flush():
    bad = _Recording("bad-skip", fail_on="on_event", fail_count=999)
    good = _Recording("good-skip")
    runner = InterceptorRunner([bad, good], error_budget=2)
    # 触发 disable
    for _ in range(3):
        await runner.dispatch_event(_make_event())
    assert runner.is_disabled("bad-skip")
    # 后续 on_terminal / flush 都跳过 bad
    await runner.dispatch_terminal(_make_terminal())
    arts = await runner.collect_artifacts()
    assert len(bad.terminals) == 0
    assert bad.flush_calls == 0
    assert len(arts) == 1  # 只有 good 的产物
