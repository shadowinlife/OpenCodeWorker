"""
拦截器包入口（W2-1）。

提供基类 + Runner + 显式工厂注册中心。

工厂注册由 W2-2 / W2-3 / W2-4 各自的模块在 import time 调用 register_factory
完成；driver 在构造时通过 build_interceptor(name, **options) 实例化。

W2-1 阶段不预注册任何拦截器；driver 默认行为与现状完全一致。
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from worker.adapters.opencode.interceptors.base import EventInterceptor
from worker.adapters.opencode.interceptors.runner import InterceptorRunner
from worker.adapters.opencode.interceptors.types import (
    InterceptorArtifact,
    InterceptorEvent,
    TerminalSignal,
)


_FACTORIES: dict[str, Callable[..., EventInterceptor]] = {}


def register_factory(name: str, factory: Callable[..., EventInterceptor]) -> None:
    """注册拦截器工厂；同名重复注册抛 ValueError。

    工厂签名：`factory(**options) -> EventInterceptor`
    options 来自 TaskRequest.opencode_profile.interceptors[].options。
    """
    if name in _FACTORIES:
        raise ValueError(f"interceptor factory {name!r} already registered")
    _FACTORIES[name] = factory


def unregister_factory(name: str) -> None:
    """注销工厂（仅供测试使用，避免 fixture 之间状态泄漏）。"""
    _FACTORIES.pop(name, None)


def build_interceptor(name: str, **options: Any) -> EventInterceptor:
    """按名称实例化拦截器。

    Raises:
        KeyError: 工厂未注册
    """
    if name not in _FACTORIES:
        raise KeyError(f"unknown interceptor {name!r}")
    return _FACTORIES[name](**options)


def list_factories() -> list[str]:
    """已注册工厂名（供调试 / 健康检查）。"""
    return sorted(_FACTORIES.keys())


def build_interceptors_from_config(
    configs: Mapping[str, Mapping[str, Any]] | list[Any],
) -> list[EventInterceptor]:
    """根据配置批量实例化拦截器。

    支持两种形态：
        1. {"name": {"opt1": ..., "opt2": ...}, ...}
        2. [InterceptorConfig, ...]（具有 .name / .options 属性）

    未注册的工厂名被静默跳过；编排层应在 register 阶段 fail-fast。
    """
    out: list[EventInterceptor] = []
    if isinstance(configs, Mapping):
        for name, options in configs.items():
            if name in _FACTORIES:
                out.append(_FACTORIES[name](**(options or {})))
        return out
    # 列表形态：约定有 .name / .options
    for cfg in configs:
        name = getattr(cfg, "name", None)
        options = getattr(cfg, "options", None) or {}
        if name and name in _FACTORIES:
            out.append(_FACTORIES[name](**options))
    return out


# ── 内置工厂注册 ────────────────────────────────────────────────────────────
# import 此包即注册下列工厂；上游通过 InterceptorConfig.name 引用。
# 工厂期不接受 Python callable（只能从 JSON 配置走），如 summarize_callback
# 这类需要 callable 的能力可由上游派生子类或在测试时直接 new ConversationsWriter。
def _register_builtin_factories() -> None:
    from worker.adapters.opencode.interceptors.backtest import (
        BacktestInterceptor,
    )
    from worker.adapters.opencode.interceptors.conversations import (
        ConversationsWriter,
    )
    from worker.adapters.opencode.interceptors.mcp_fields import (
        McpFieldRecorder,
    )

    if "conversations" not in _FACTORIES:
        register_factory(
            "conversations",
            lambda **opts: ConversationsWriter(**opts),
        )
    if "backtest" not in _FACTORIES:
        register_factory(
            "backtest",
            lambda **opts: BacktestInterceptor(**opts),
        )
    if "mcp-fields" not in _FACTORIES:
        register_factory(
            "mcp-fields",
            lambda **opts: McpFieldRecorder(**opts),
        )


_register_builtin_factories()


__all__ = [
    "EventInterceptor",
    "InterceptorRunner",
    "InterceptorEvent",
    "TerminalSignal",
    "InterceptorArtifact",
    "register_factory",
    "unregister_factory",
    "build_interceptor",
    "build_interceptors_from_config",
    "list_factories",
]
