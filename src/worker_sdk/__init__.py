"""Worker Client SDK — 薄的、内部使用的、async-first Python 客户端。

依据 docs/design/worker-client-sdk-interface-design.md (Draft v1, 2026-05-16) 实现。

公开入口：

    from worker_sdk import AsyncWorkerClient

SDK 的职责是把 Worker HTTP + SSE 协议中容易写错的部分（Bearer 认证、SSE 断
线重连、终态等待、HITL 决策、artifact 下载）收口为一组 async 接口；它不引入
任何 workflow / strategy 业务语义，也不发明新的服务端能力。

兼容性矩阵（见 compat.py）：

    SDK 0.1.x  ↔  Worker 0.1.x
"""
from __future__ import annotations

from worker_sdk.client import AsyncWorkerClient
from worker_sdk.errors import (
    WorkerClientError,
    WorkerCompatibilityError,
    WorkerConflictError,
    WorkerHTTPError,
    WorkerNotFoundError,
    WorkerSSEError,
    WorkerServerError,
    WorkerTaskAborted,
    WorkerTaskFailed,
    WorkerTaskTerminalError,
    WorkerTaskTimedOut,
    WorkerTransportError,
    WorkerUnauthorizedError,
)
from worker_sdk.models import (
    WorkerArtifactRef,
    WorkerEvent,
    WorkerTaskHandle,
    WorkerTerminalResult,
)
from worker_sdk.retry import RetryPolicy

__all__ = [
    "AsyncWorkerClient",
    "RetryPolicy",
    "WorkerArtifactRef",
    "WorkerEvent",
    "WorkerTaskHandle",
    "WorkerTerminalResult",
    "WorkerClientError",
    "WorkerCompatibilityError",
    "WorkerConflictError",
    "WorkerHTTPError",
    "WorkerNotFoundError",
    "WorkerSSEError",
    "WorkerServerError",
    "WorkerTaskAborted",
    "WorkerTaskFailed",
    "WorkerTaskTerminalError",
    "WorkerTaskTimedOut",
    "WorkerTransportError",
    "WorkerUnauthorizedError",
]

__version__ = "0.1.0"
