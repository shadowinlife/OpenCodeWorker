"""SDK 公开的数据模型。

这里只声明 SDK 对外暴露的轻量类型，**不复用服务端 Pydantic 模型**，以避免
上游 runtime 通过 SDK 反向耦合到 FastAPI / Docker / server internals
（决策 C4 in design doc）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class WorkerTaskHandle:
    """一次已提交任务的句柄。

    Attributes:
        task_id: 任务 UUID
        status:  服务端返回的当前状态字符串（如 ``pending`` / ``queued``）
    """

    task_id: str
    status: str


@dataclass(frozen=True)
class WorkerEvent:
    """SDK 暴露给上游的统一事件对象。

    服务端 SSE 不直接推送 ``ts`` 字段，因此本对象只保证 ``cursor`` / ``kind``
    / ``payload`` 三项稳定字段（design §6.2）。
    """

    cursor: int
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class WorkerTerminalResult:
    """``wait_until_terminal()`` 的统一返回。

    Attributes:
        task_id:         任务 UUID
        final_status:    ``completed`` / ``failed`` / ``aborted`` / ``timed_out``
        terminal_event:  触发终态的 SSE 事件；若是通过轮询路径回退则为 ``None``
        task_snapshot:   终态后再次 ``GET /tasks/{task_id}`` 拿到的完整快照
    """

    task_id: str
    final_status: str
    terminal_event: WorkerEvent | None
    task_snapshot: dict[str, Any]


@dataclass(frozen=True)
class WorkerArtifactRef:
    """单个产物的元数据引用。

    与服务端 ``Artifact`` 字段对齐，但额外把 ``type`` 暴露为 ``str`` 而不是
    enum，避免把服务端 ``ArtifactType`` 枚举泄露到上游依赖。
    """

    artifact_id: str
    task_id: str
    type: str
    filename: str
    size: int | None
    created_at: float
    expires_at: float | None
    download_url: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
