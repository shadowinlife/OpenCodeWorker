"""SDK 错误模型（design §8）。

错误分三层：

1. ``WorkerTransportError`` —— 网络层（连接被拒、超时、断连）
2. ``WorkerHTTPError`` 及子类 —— 服务端返回非 2xx
3. ``WorkerTaskTerminalError`` 及子类 —— 任务进入失败终态

``WorkerTaskTerminalError`` 仅在 ``wait_until_terminal(..., raise_on_failure=True)``
时由 SDK 主动抛出，便于上游做 ``except`` 短路；默认行为是返回
``WorkerTerminalResult`` 让上游自行判断。
"""
from __future__ import annotations

from typing import Any

from worker_sdk.models import WorkerEvent


class WorkerClientError(Exception):
    """SDK 所有自定义异常的基类。"""


# ---------------------------------------------------------------------------
# Transport / HTTP
# ---------------------------------------------------------------------------

class WorkerTransportError(WorkerClientError):
    """DNS 解析失败、TCP 连接超时、TLS 错误、写半边断开等网络层错误。

    通常意味着 ``base_url`` 不可达或网络发生瞬时故障；上游可以重试。
    """


class WorkerHTTPError(WorkerClientError):
    """服务端返回非 2xx 响应的统一基类。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        response_body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class WorkerUnauthorizedError(WorkerHTTPError):
    """401 —— Bearer token 缺失或不匹配。"""


class WorkerNotFoundError(WorkerHTTPError):
    """404 —— task_id / artifact_id 不存在。"""


class WorkerConflictError(WorkerHTTPError):
    """409 —— task 已存在、abort 已终态、decision 已解决等业务冲突。"""


class WorkerServerError(WorkerHTTPError):
    """5xx —— 服务端内部错误。"""


class WorkerCompatibilityError(WorkerClientError):
    """``/health.version`` 落在 SDK 支持矩阵之外。"""


class WorkerSSEError(WorkerClientError):
    """SSE 解析失败或重连次数耗尽。"""


# ---------------------------------------------------------------------------
# Terminal task errors —— 仅在 raise_on_failure=True 时由 SDK 抛出
# ---------------------------------------------------------------------------

class WorkerTaskTerminalError(WorkerClientError):
    """任务以非 ``completed`` 终态结束。

    Attributes:
        task_id:         任务 UUID
        final_status:    ``failed`` / ``aborted`` / ``timed_out``
        terminal_event:  触发终态的 SSE 事件；通过 ``get_task`` 兜底路径时可能为 ``None``
        task_snapshot:   终态后 ``GET /tasks/{task_id}`` 的完整快照
    """

    def __init__(
        self,
        message: str,
        *,
        task_id: str,
        final_status: str,
        terminal_event: WorkerEvent | None,
        task_snapshot: dict[str, Any],
    ) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.final_status = final_status
        self.terminal_event = terminal_event
        self.task_snapshot = task_snapshot


class WorkerTaskFailed(WorkerTaskTerminalError):
    """对应终态 ``failed``。"""


class WorkerTaskAborted(WorkerTaskTerminalError):
    """对应终态 ``aborted``。"""


class WorkerTaskTimedOut(WorkerTaskTerminalError):
    """对应终态 ``timed_out``。"""


# ---------------------------------------------------------------------------
# 内部映射工具
# ---------------------------------------------------------------------------

_TERMINAL_ERROR_MAP: dict[str, type[WorkerTaskTerminalError]] = {
    "failed": WorkerTaskFailed,
    "aborted": WorkerTaskAborted,
    "timed_out": WorkerTaskTimedOut,
}


def terminal_error_for(final_status: str) -> type[WorkerTaskTerminalError] | None:
    """根据终态字符串映射到对应异常类；``completed`` 返回 ``None``。"""
    return _TERMINAL_ERROR_MAP.get(final_status)


def http_error_for(status_code: int) -> type[WorkerHTTPError]:
    """根据 HTTP status code 选择最合适的异常类。"""
    if status_code == 401:
        return WorkerUnauthorizedError
    if status_code == 404:
        return WorkerNotFoundError
    if status_code == 409:
        return WorkerConflictError
    if 500 <= status_code < 600:
        return WorkerServerError
    return WorkerHTTPError
