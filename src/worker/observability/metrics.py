"""
Worker 可观测性：Prometheus-style Metrics

职责：
    1. 维护内存中的计数器/直方图，供 GET /metrics 端点以 Prometheus 文本格式导出
    2. 暴露增量 helper 函数，供 orchestrator / driver / routes 调用
    3. 预留 OpenTelemetry tracing hook（OTEL_SDK_PRESENT 标志控制）

已定义指标：
    task_count{status}              — 按终态/创建计数
    task_duration_seconds           — 任务完成耗时直方图
    hitl_wait_seconds               — HITL 等待时长直方图（从发出 hitl_required 到收到 decision）
    container_start_ms              — 容器启动耗时直方图（ms）
    abort_count{reason}             — 中止计数
    token_usage_total{direction}    — token 使用量（input/output）
    active_tasks                    — 当前活跃（非终态）任务数

线程安全假设：单进程 asyncio，不需要 threading.Lock（全为简单累加）。
"""
from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Any

# ─── 全局状态 ────────────────────────────────────────────────────────────────

# Counter：task_count{status}
_task_count: dict[str, int] = defaultdict(int)

# Counter：abort_count{reason}
_abort_count: dict[str, int] = defaultdict(int)

# Counter：token_usage_total{direction}
_token_usage: dict[str, int] = defaultdict(int)

# Gauge：active_tasks
_active_tasks: int = 0

# 简单直方图（只记录 sum/count，不做 bucket 细分；可扩展为真正的 bucket histogram）
_task_duration_sum: float = 0.0
_task_duration_count: int = 0

_hitl_wait_sum: float = 0.0
_hitl_wait_count: int = 0

_container_start_sum: float = 0.0
_container_start_count: int = 0

# ─── 修改 helper ─────────────────────────────────────────────────────────────

def inc_task_count(status: str) -> None:
    """任务状态计数 +1（通常在终态写入时调用）。"""
    _task_count[status] += 1


def inc_abort_count(reason: str = "unknown") -> None:
    """中止计数 +1。"""
    _abort_count[reason] += 1


def add_token_usage(direction: str, count: int) -> None:
    """累加 token 使用量。direction: 'input' | 'output'。"""
    _token_usage[direction] += count


def set_active_tasks(n: int) -> None:
    """设置当前活跃任务数（绝对值，由 orchestrator queue 在任务启停时调用）。"""
    global _active_tasks
    _active_tasks = max(0, n)


def inc_active_tasks() -> None:
    """活跃任务数 +1。"""
    global _active_tasks
    _active_tasks += 1


def dec_active_tasks() -> None:
    """活跃任务数 -1（最小 0）。"""
    global _active_tasks
    _active_tasks = max(0, _active_tasks - 1)


def observe_task_duration(seconds: float) -> None:
    """记录一次任务完成耗时（秒）。"""
    global _task_duration_sum, _task_duration_count
    _task_duration_sum += seconds
    _task_duration_count += 1


def observe_hitl_wait(seconds: float) -> None:
    """记录一次 HITL 等待时长（秒）。"""
    global _hitl_wait_sum, _hitl_wait_count
    _hitl_wait_sum += seconds
    _hitl_wait_count += 1


def observe_container_start(ms: float) -> None:
    """记录一次容器启动耗时（毫秒）。"""
    global _container_start_sum, _container_start_count
    _container_start_sum += ms
    _container_start_count += 1


# ─── 导出 ────────────────────────────────────────────────────────────────────

def render_prometheus() -> str:
    """将所有指标以 Prometheus text format 0.0.4 格式输出。"""
    lines: list[str] = []

    def _counter(name: str, help_text: str, labels_values: dict[str, int]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} counter")
        for labels, val in sorted(labels_values.items()):
            lines.append(f'{name}{{{labels}}} {val}')

    def _gauge(name: str, help_text: str, value: Any) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")

    def _summary(name: str, help_text: str, s: float, count: int) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} summary")
        lines.append(f"{name}_sum {s:.6f}")
        lines.append(f"{name}_count {count}")

    # task_count
    _counter(
        "worker_task_count_total",
        "Total tasks by terminal status.",
        {f'status="{k}"': v for k, v in _task_count.items()},
    )

    # abort_count
    _counter(
        "worker_abort_count_total",
        "Total task aborts by reason.",
        {f'reason="{k}"': v for k, v in _abort_count.items()},
    )

    # token_usage
    _counter(
        "worker_token_usage_total",
        "Total LLM tokens used.",
        {f'direction="{k}"': v for k, v in _token_usage.items()},
    )

    # active_tasks
    _gauge("worker_active_tasks", "Current number of active (non-terminal) tasks.", _active_tasks)

    # task_duration
    _summary(
        "worker_task_duration_seconds",
        "Task completion duration in seconds.",
        _task_duration_sum,
        _task_duration_count,
    )

    # hitl_wait
    _summary(
        "worker_hitl_wait_seconds",
        "HITL decision wait time in seconds.",
        _hitl_wait_sum,
        _hitl_wait_count,
    )

    # container_start
    _summary(
        "worker_container_start_ms",
        "Container start latency in milliseconds.",
        _container_start_sum,
        _container_start_count,
    )

    return "\n".join(lines) + "\n"
