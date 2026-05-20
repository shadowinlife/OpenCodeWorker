"""
InterceptorRunner —— driver 私有调度器（W2-1）。

职责：
    1. 持有有序 list[EventInterceptor]
    2. 并发分发 on_event / on_terminal；sequential flush
    3. 隔离每个拦截器的错误：单个抛错不影响兄弟拦截器、不影响 driver 主流程
    4. 错误预算（默认 10）：累计错误超限即静默 disable 该拦截器

设计文档：claudedocs/design_w2_1_event_interceptor_20260520.md §3.2 / §5
"""
from __future__ import annotations

import asyncio
import logging
from typing import Sequence

from worker.adapters.opencode.interceptors.base import EventInterceptor, _validate_name
from worker.adapters.opencode.interceptors.types import (
    InterceptorArtifact,
    InterceptorEvent,
    TerminalSignal,
)

logger = logging.getLogger(__name__)


class InterceptorRunner:
    """driver 持有的拦截器调度器；外部不应直接构造。

    Args:
        interceptors: 注册的拦截器列表，顺序会被保留
        error_budget: 单个拦截器累计错误数上限；超过即静默 disable

    Raises:
        ValueError: 拦截器 name 不合规或重名
    """

    DEFAULT_ERROR_BUDGET = 10

    def __init__(
        self,
        interceptors: Sequence[EventInterceptor],
        error_budget: int = DEFAULT_ERROR_BUDGET,
    ):
        # 校验 name 格式 + 唯一性
        seen: set[str] = set()
        for ic in interceptors:
            _validate_name(ic.name)
            if ic.name in seen:
                raise ValueError(f"duplicate interceptor name: {ic.name!r}")
            seen.add(ic.name)

        self._interceptors: list[EventInterceptor] = list(interceptors)
        self._error_budget = error_budget
        self._error_counts: dict[str, int] = {ic.name: 0 for ic in interceptors}
        self._disabled: set[str] = set()

    # ── 状态查询（仅供测试 / 调试）──────────────────────────────────────────────

    @property
    def interceptors(self) -> list[EventInterceptor]:
        return list(self._interceptors)

    def is_disabled(self, name: str) -> bool:
        return name in self._disabled

    def error_count(self, name: str) -> int:
        return self._error_counts.get(name, 0)

    # ── 分发入口 ────────────────────────────────────────────────────────────────

    async def dispatch_event(self, event: InterceptorEvent) -> None:
        """对所有未 disabled 的拦截器并发触发 on_event。"""
        active = self._active()
        if not active:
            return
        await asyncio.gather(
            *(self._safe_call(ic, "on_event", ic.on_event(event)) for ic in active),
            return_exceptions=False,  # _safe_call 已捕获，不会向外抛
        )

    async def dispatch_terminal(self, signal: TerminalSignal) -> None:
        """对所有未 disabled 的拦截器并发触发 on_terminal。"""
        active = self._active()
        if not active:
            return
        await asyncio.gather(
            *(self._safe_call(ic, "on_terminal", ic.on_terminal(signal)) for ic in active),
            return_exceptions=False,
        )

    async def collect_artifacts(self) -> list[InterceptorArtifact]:
        """sequential flush；返回所有非 None 的产物声明。

        sequential 而非并发是为了保证错误日志顺序可读；性能不敏感
        （flush 只在任务终态调用一次）。
        """
        out: list[InterceptorArtifact] = []
        for ic in self._interceptors:
            if ic.name in self._disabled:
                continue
            artifact = await self._safe_call_with_return(ic, "flush", ic.flush())
            if artifact is not None:
                out.append(artifact)
        return out

    # ── 内部 helper ─────────────────────────────────────────────────────────────

    def _active(self) -> list[EventInterceptor]:
        return [ic for ic in self._interceptors if ic.name not in self._disabled]

    async def _safe_call(self, ic: EventInterceptor, phase: str, coro) -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001 — 隔离边界
            self._record_error(ic, phase)

    async def _safe_call_with_return(self, ic: EventInterceptor, phase: str, coro):
        try:
            return await coro
        except Exception:  # noqa: BLE001
            self._record_error(ic, phase)
            return None

    def _record_error(self, ic: EventInterceptor, phase: str) -> None:
        self._error_counts[ic.name] += 1
        logger.exception(
            "interceptor %s failed in phase=%s (count=%d/%d)",
            ic.name,
            phase,
            self._error_counts[ic.name],
            self._error_budget,
        )
        if self._error_counts[ic.name] >= self._error_budget:
            self._disabled.add(ic.name)
            logger.error(
                "interceptor %s disabled (error_count=%d >= budget=%d)",
                ic.name,
                self._error_counts[ic.name],
                self._error_budget,
            )


__all__ = ["InterceptorRunner"]
