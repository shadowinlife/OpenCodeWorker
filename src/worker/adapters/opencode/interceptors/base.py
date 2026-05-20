"""
EventInterceptor 抽象基类（W2-1）。

driver 注入的事件拦截器；W2-2 / W2-3 / W2-4 三个具体拦截器都继承此基类。
基类与上层业务领域完全解耦（参见设计文档 §11.3 architecture invariants）。

生命周期（与 OpenCodeDriver.run 对齐）：
    1. driver 构造时持有 list[EventInterceptor]
    2. driver._consume_sse 每收到一条事件 → 对每个拦截器调用 on_event
    3. driver.run finally 块（即将写终态前）→ 对每个拦截器调用 on_terminal
    4. 之后调用 flush() 收集 InterceptorArtifact 并由 driver 走标准登记路径

错误隔离：所有 hook 抛错均由 InterceptorRunner 捕获 + log，不影响 driver 主流程。

设计文档：claudedocs/design_w2_1_event_interceptor_20260520.md §3
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Optional

from worker.adapters.opencode.interceptors.types import (
    InterceptorArtifact,
    InterceptorEvent,
    TerminalSignal,
)


# kebab-case，3-41 字符；driver 注册时校验
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,40}$")


def _validate_name(name: str) -> None:
    """校验拦截器 name 是否符合规范。"""
    if not isinstance(name, str) or not _NAME_PATTERN.match(name):
        raise ValueError(
            f"interceptor name must match {_NAME_PATTERN.pattern!r}, got {name!r}"
        )


class EventInterceptor(ABC):
    """拦截器抽象基类。

    禁止事项（review 阶段强制）：
        - 不直接调用 storage.repo.* 写 DB（所有产物登记走 flush() 返回值）
        - 不直接发送 SSE 事件
        - 不修改 InterceptorEvent.raw_payload / normalized_payload（视为只读）
        - 不持有 OpenCodeDriver 引用 / OpenCodeClient 引用
        - 不在 on_event 中做长时间 CPU 工作或 time.sleep（会阻塞 SSE 主循环）

    并发模型：driver 当前是 single-event-loop，多拦截器之间通过
    asyncio.gather 并发推进，但同一拦截器实例的 on_event 调用是串行的
    （顺序保证与 driver 接收事件顺序一致）。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """拦截器名称（kebab-case），用于日志、metric label、错误隔离上下文。

        必须满足正则 ^[a-z][a-z0-9-]{2,40}$。重名会导致 InterceptorRunner 构造失败。
        """

    async def on_event(self, event: InterceptorEvent) -> None:
        """每条 opencode SSE 事件的回调（默认 no-op）。

        实现注意：
            - 必须 idempotent：同一 raw_payload 重复进入不应产生重复副作用
            - 异步 IO 友好：可 await 自身 IO，但不应 sleep / 做长时间 CPU 工作
            - 累积式：保存到子类实例字段；终态时由 flush 统一落盘
        """

    async def on_terminal(self, signal: TerminalSignal) -> None:
        """终态信号回调（默认 no-op）。

        在 driver 写终态事件**前**调用。子类可在此停止后台计时器、
        关闭句柄等；不要在此做重 IO（建议放 flush）。
        """

    async def flush(self) -> Optional[InterceptorArtifact]:
        """终态后落盘 + 返回产物声明（默认返回 None，表示无产物登记）。

        生命周期：在 on_terminal 之后、driver 写终态事件之前调用一次。
        返回 None：拦截器不希望登记产物。
        返回 InterceptorArtifact：driver 走标准登记流程。
        """
        return None


__all__ = ["EventInterceptor", "_validate_name"]
