"""
单元测试：W2-1 EventInterceptor 基类与数据模型不变量

覆盖设计文档 §7.1：
    - InterceptorEvent / TerminalSignal / InterceptorArtifact frozen
    - name 校验（kebab-case 正则）
    - 默认 hook 行为（no-op + flush 返回 None）
    - 注册中心（register_factory / build_interceptor / build_interceptors_from_config）
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from worker.adapters.opencode.interceptors import (
    EventInterceptor,
    build_interceptor,
    build_interceptors_from_config,
    list_factories,
    register_factory,
    unregister_factory,
)
from worker.adapters.opencode.interceptors.base import _validate_name
from worker.adapters.opencode.interceptors.types import (
    InterceptorArtifact,
    InterceptorEvent,
    TerminalSignal,
)


# ── 数据模型不变量 ──────────────────────────────────────────────────────────


def test_interceptor_event_is_frozen():
    """InterceptorEvent 是 frozen dataclass，赋值应抛 FrozenInstanceError。"""
    evt = InterceptorEvent(
        task_id="t1",
        session_id="s1",
        normalized_kind="assistant_delta",
        normalized_payload={"content": "hi"},
        raw_type="message.part.delta",
        raw_payload={},
        received_at=1.0,
    )
    with pytest.raises(FrozenInstanceError):
        evt.task_id = "t2"  # type: ignore[misc]


def test_terminal_signal_is_frozen():
    sig = TerminalSignal(
        task_id="t1", session_id=None,
        status="completed", reason=None, ended_at=1.0,
    )
    with pytest.raises(FrozenInstanceError):
        sig.status = "failed"  # type: ignore[misc]


def test_interceptor_artifact_default_metadata_is_separate_dict():
    """field(default_factory=dict) 保证多实例不共享 mutable 默认值。"""
    a = InterceptorArtifact(
        artifact_type="custom", filename="a.json", local_path="/tmp/a.json",
    )
    b = InterceptorArtifact(
        artifact_type="custom", filename="b.json", local_path="/tmp/b.json",
    )
    assert a.metadata == {}
    assert b.metadata == {}
    assert a.metadata is not b.metadata


# ── name 校验 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("good", [
    "abc",
    "abc-def",
    "conv1",
    "mcp-field-recorder",
    "a" + "b" * 40,  # 41 chars total — pattern is ^[a-z][a-z0-9-]{2,40}$
])
def test_validate_name_accepts_kebab_case(good):
    _validate_name(good)  # no raise


@pytest.mark.parametrize("bad", [
    "AB",                      # uppercase
    "ab",                      # too short (3-41)
    "1abc",                    # leading digit
    "with spaces",             # space
    "snake_case",              # underscore
    "CamelCase",
    "",
    "a" * 50,                  # too long
])
def test_validate_name_rejects_invalid(bad):
    with pytest.raises(ValueError):
        _validate_name(bad)


# ── 基类默认行为 ────────────────────────────────────────────────────────────


class _MinimalInterceptor(EventInterceptor):
    @property
    def name(self) -> str:
        return "minimal"


async def test_default_on_event_is_noop():
    ic = _MinimalInterceptor()
    evt = InterceptorEvent(
        task_id="t1", session_id="s1",
        normalized_kind="x", normalized_payload={},
        raw_type="r", raw_payload={}, received_at=0.0,
    )
    # 不抛异常即视为通过
    await ic.on_event(evt)


async def test_default_on_terminal_is_noop():
    ic = _MinimalInterceptor()
    sig = TerminalSignal(
        task_id="t1", session_id=None,
        status="completed", reason=None, ended_at=0.0,
    )
    await ic.on_terminal(sig)


async def test_default_flush_returns_none():
    ic = _MinimalInterceptor()
    assert await ic.flush() is None


# ── 注册中心 ────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_factory():
    """每个测试结束后清掉注册过的工厂，避免跨用例污染。"""
    yield
    for name in list_factories():
        if name.startswith("test-"):
            unregister_factory(name)


def test_register_and_build_factory(clean_factory):
    register_factory("test-minimal", lambda **opts: _MinimalInterceptor())
    ic = build_interceptor("test-minimal")
    assert isinstance(ic, _MinimalInterceptor)


def test_register_duplicate_raises(clean_factory):
    register_factory("test-dup", lambda **opts: _MinimalInterceptor())
    with pytest.raises(ValueError):
        register_factory("test-dup", lambda **opts: _MinimalInterceptor())


def test_build_unknown_raises(clean_factory):
    with pytest.raises(KeyError):
        build_interceptor("test-not-registered")


def test_build_from_config_dict(clean_factory):
    register_factory("test-d1", lambda **opts: _MinimalInterceptor())
    out = build_interceptors_from_config({"test-d1": {}, "test-missing": {"x": 1}})
    # 未注册的 test-missing 应被静默跳过
    assert len(out) == 1
    assert isinstance(out[0], _MinimalInterceptor)


def test_build_from_config_list_of_objects(clean_factory):
    """支持 InterceptorConfig 形态（具有 .name / .options 属性的对象）。"""
    from types import SimpleNamespace

    register_factory("test-l1", lambda **opts: _MinimalInterceptor())
    cfgs = [
        SimpleNamespace(name="test-l1", options={}),
        SimpleNamespace(name="test-skip", options={}),
    ]
    out = build_interceptors_from_config(cfgs)
    assert len(out) == 1
