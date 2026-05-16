"""
Worker 内部任务执行异常类型。

这些异常类专用于在 driver/orchestrator 内部传递结构化的失败原因，
让 queue._run_one 能够把它们路由到对应的终态：

    TaskTimedOutError → TaskStatus.timed_out + task_timed_out 事件
    TaskAbortedError  → TaskStatus.aborted   + task_aborted   事件
    其他 Exception    → TaskStatus.failed    + task_failed    事件

约定：
    - 异常仅在 Worker 进程内传递，不会序列化到 HTTP 响应或 DB；
      对外的错误契约仍然走 worker.contract.error.WorkerError。
    - 异常类的字段（reason、timeout_sec 等）会被 queue 落到对应事件 payload。
"""
from __future__ import annotations

from typing import Optional


class TaskTimedOutError(Exception):
    """任务执行超过 resource_limits.timeout_sec 未完成。

    由 OpenCodeDriver 在捕获 asyncio.TimeoutError 时抛出，queue 层会写入
    TaskStatus.timed_out + task_timed_out 终态事件。
    """

    def __init__(self, timeout_sec: float, message: Optional[str] = None) -> None:
        self.timeout_sec = timeout_sec
        super().__init__(message or f"task timed out after {timeout_sec}s")


class TaskAbortedError(Exception):
    """任务被主动中止（HITL 决策、超时降级到 abort、计划拒绝、权限拒绝等）。

    reason 取值（与 task_aborted 事件 payload.reason 对齐）：
        - "user_requested":       用户通过 POST /tasks/:id/abort 主动中止
        - "hitl_timeout":         HITL 决策超时且 on_timeout=abort
        - "plan_rejected":        plan_first 模式下计划被拒绝/撤销
        - "permission_rejected":  权限请求被拒绝且无法降级
        - "system":               其它系统侧主动中止（兜底）
    """

    def __init__(
        self,
        reason: str = "system",
        message: Optional[str] = None,
        decision_id: Optional[str] = None,
    ) -> None:
        self.reason = reason
        self.decision_id = decision_id
        super().__init__(message or f"task aborted (reason={reason})")
