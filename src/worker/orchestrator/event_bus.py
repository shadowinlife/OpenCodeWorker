"""
P1-12 — 进程内 per-task SSE 唤醒总线。

修复前：SSE handler 用 0.5s 轮询 DB 检查新事件 → 流式输出抖动 + N 订阅者 ×
2 reader × 0.5s 拖垮写性能。

修复后：每个 task_id 维护一个订阅者列表（每订阅者一个 asyncio.Event）；
`insert_event` 写入新事件后调用 `notify(task_id)`，所有订阅者立即唤醒
（< 1ms vs 原来最坏 500ms）；订阅者醒后清自己的 Event 再去 DB 拉新增。

注意：
    - 总线仅是 **wakeup signal**，DB 始终是事件数据源。即便丢一次 notify
      也不会丢事件——下一次 heartbeat 间隔到时仍会重新拉。
    - 单线程 asyncio 下读写 _buses dict 之间无 await 边界，无需 meta-lock。
    - 任务终态后由 `discard(task_id)` 释放（queue 终态处理路径调用）。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class TaskEventBus:
    """单个 task_id 的订阅者集合。订阅者各自持有一个 asyncio.Event。"""

    __slots__ = ("_subscribers",)

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Event] = []

    def subscribe(self) -> asyncio.Event:
        """注册一个新订阅者，返回其专属唤醒 Event。"""
        ev = asyncio.Event()
        self._subscribers.append(ev)
        return ev

    def unsubscribe(self, ev: asyncio.Event) -> None:
        """注销订阅者（SSE 流退出 / 客户端断开时调用）。"""
        try:
            self._subscribers.remove(ev)
        except ValueError:
            pass

    def notify(self) -> None:
        """唤醒全部订阅者。idempotent：已 set 的 Event 再 set 是 no-op。"""
        for ev in self._subscribers:
            ev.set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


# ──────────────────────────────────────────────────────────────────────────────
# 模块级 registry
# ──────────────────────────────────────────────────────────────────────────────

_buses: dict[str, TaskEventBus] = {}


def get_bus(task_id: str) -> TaskEventBus:
    """返回 task_id 对应的总线，首次访问时创建。"""
    bus = _buses.get(task_id)
    if bus is None:
        bus = TaskEventBus()
        _buses[task_id] = bus
    return bus


def notify(task_id: str) -> None:
    """新事件落库后由 `insert_event` 调用，唤醒所有订阅者。

    若该 task 当前无订阅者（bus 不存在），noop——不会创建空 bus。
    """
    bus = _buses.get(task_id)
    if bus is not None:
        bus.notify()


def discard(task_id: str) -> None:
    """终态处理后释放 bus，避免 _buses dict 长期增长。

    残留订阅者会在自己的循环中通过 `is_disconnected` / 终态事件检测退出。
    """
    _buses.pop(task_id, None)


def active_bus_count() -> int:
    """诊断用：当前活跃总线数（约等于活跃 task 数）。"""
    return len(_buses)
