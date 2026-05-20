"""
拦截器数据模型（W2-1 EventInterceptor 基类）。

三个 frozen dataclass 是 driver ↔ 拦截器之间的唯一数据契约：

    InterceptorEvent     —— 单条 SSE 事件的只读视图（原始 + 归一化双视图）
    TerminalSignal       —— 任务终态信号（status + reason）
    InterceptorArtifact  —— 拦截器声明的待登记产物

设计文档：claudedocs/design_w2_1_event_interceptor_20260520.md §2
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class InterceptorEvent:
    """拦截器看到的单条事件（只读视图）。

    生命周期：driver._consume_sse 每收到一条 opencode 原始事件，
    完成归一化后立即构造一个 InterceptorEvent，分发给所有拦截器。

    `normalized_kind` / `normalized_payload` 在以下情况为 None：
        1. 该原始事件不映射到任何 TaskEventKind（如 server.heartbeat / sync）
        2. driver 合成的 decision_received 事件（raw_type 以 "<synthesized:" 前缀标记）
           此时 normalized_kind 仍非 None，仅 raw_payload 为空 dict
    """

    task_id: str
    session_id: Optional[str]
    normalized_kind: Optional[str]
    normalized_payload: Optional[Mapping[str, Any]]
    raw_type: str
    raw_payload: Mapping[str, Any]
    received_at: float


@dataclass(frozen=True)
class TerminalSignal:
    """任务终态信号。driver 在写终态事件前调用所有拦截器的 on_terminal。

    `status` 取 TaskStatus.value（"completed" / "failed" / "aborted" / "timed_out"）。
    `reason`：
        - status="aborted"  → driver._abort_reason
                              (user_requested / hitl_timeout / plan_rejected /
                               permission_rejected / reject_threshold_exceeded / system)
        - status="failed"   → 异常类名
        - status="timed_out" → "timeout"
        - status="completed" → None
    """

    task_id: str
    session_id: Optional[str]
    status: str
    reason: Optional[str]
    ended_at: float


@dataclass(frozen=True)
class InterceptorArtifact:
    """拦截器声明的待登记产物。

    driver 负责实际登记：
        1. 校验 local_path 必须落在 artifacts_dir / task_id 子树内（防 P0-8 类越权）
        2. 走标准 insert_artifact 路径写入 DB
        3. 发出 artifact_ready 事件

    Attributes:
        artifact_type: ArtifactType enum value（如 "custom" / "transcript"）
        filename:      建议下载文件名（含扩展名）
        local_path:    已落盘的绝对路径
        metadata:      自定义元数据，原样存入 Artifact.metadata
        size_bytes:    文件字节数；None 时由 driver 用 stat 自取
    """

    artifact_type: str
    filename: str
    local_path: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    size_bytes: Optional[int] = None
