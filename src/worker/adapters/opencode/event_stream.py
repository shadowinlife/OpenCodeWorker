"""
OpenCode SSE 事件类型定义与归一化映射。

opencode 通过 GET /global/event 推送 SSE，每条消息格式为：
    data: { "type": "<event_type>", "payload": { ... } }

本模块负责：
    1. 定义已知的 opencode 事件类型（OpenCodeEventType）
    2. 将 opencode 事件归一化为 Worker TaskEventKind
    3. 提取 permission 请求、session idle 信号、diff 快照等关键信息

opencode 1.14.30 已知事件类型（§1.3 Spike 实测）：
    server.connected      — SSE 连接建立，忽略
    server.heartbeat      — 心跳（约 30s 一次），忽略
    message.updated       — 新消息写入会话
    message.part.updated  — 消息 part 更新（工具调用 / 权限请求等）
    message.part.delta    — LLM 流式文本增量 → assistant_delta
    session.updated       — 会话元数据更新，忽略
    session.status        — 会话状态变更（busy/idle + 可能含 pending permissions）
    session.diff          — 工作区 diff 快照
    session.idle          — 会话进入 idle（任务完成信号）
    sync                  — 内部同步事件，忽略
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional


class OpenCodeEventType(str, Enum):
    """opencode SSE 事件类型（以实测 1.14.30 为准）。"""
    server_connected = "server.connected"
    server_heartbeat = "server.heartbeat"
    message_updated = "message.updated"
    message_part_updated = "message.part.updated"
    message_part_delta = "message.part.delta"
    session_updated = "session.updated"
    session_status = "session.status"
    session_diff = "session.diff"
    session_idle = "session.idle"
    sync = "sync"


# ── 归一化结果 ────────────────────────────────────────────────────────────────

class NormalizedEvent:
    """归一化后的事件，供 driver.py 消费。"""

    __slots__ = ("kind", "payload", "raw_type", "raw_payload")

    def __init__(
        self,
        kind: str,
        payload: dict[str, Any],
        raw_type: str,
        raw_payload: Any,
    ):
        self.kind = kind           # Worker TaskEventKind value (str)
        self.payload = payload     # Worker 事件 payload dict
        self.raw_type = raw_type   # 原始 opencode event type str
        self.raw_payload = raw_payload  # 原始 payload，供调试


# ── 主归一化入口 ──────────────────────────────────────────────────────────────

def normalize_opencode_event(event: dict[str, Any]) -> Optional[NormalizedEvent]:
    """将单个 opencode SSE 事件归一化为 Worker NormalizedEvent。

    返回 None 表示此事件不需要转发给上游（如心跳、内部 sync）。

    归一化规则：
        message.part.delta     → assistant_delta
        message.part.updated   → tool_call_started | tool_call_finished | None
        其余                   → None（忽略）

    注意：session.idle / session.status / session.diff 由 driver 直接使用
    辅助函数（is_session_idle / extract_diff），不经过此函数产出 Worker 事件。
    """
    event_type = event.get("type", "")
    payload = event.get("payload", {})

    if event_type == OpenCodeEventType.message_part_delta:
        content = _extract_delta_content(payload)
        if content:
            return NormalizedEvent(
                kind="assistant_delta",
                payload={"content": content},
                raw_type=event_type,
                raw_payload=payload,
            )
        return None

    if event_type == OpenCodeEventType.message_part_updated:
        return _normalize_part_updated(event_type, payload)

    return None


def _extract_delta_content(payload: Any) -> Optional[str]:
    """从 message.part.delta payload 中提取文本内容。

    opencode 可能的结构（根据版本不同）：
        {"part": {"type": "text", "text": "..."}}
        {"text": "..."}
    """
    if not isinstance(payload, dict):
        return None
    # 优先尝试嵌套 part 结构
    part = payload.get("part", payload)
    if isinstance(part, dict):
        return part.get("text") or part.get("content") or None
    return None


def _normalize_part_updated(event_type: str, payload: Any) -> Optional[NormalizedEvent]:
    """归一化 message.part.updated 事件 → tool_call_started / tool_call_finished。"""
    if not isinstance(payload, dict):
        return None
    part = payload.get("part", {})
    if not isinstance(part, dict):
        return None

    part_type = part.get("type", "")

    if part_type == "tool-use":
        # LLM 发起工具调用（尚未执行）
        return NormalizedEvent(
            kind="tool_call_started",
            payload={
                "tool": part.get("toolName") or part.get("tool") or "unknown",
                "args": part.get("input") or part.get("args") or {},
                "tool_use_id": part.get("toolUseId") or part.get("id"),
            },
            raw_type=event_type,
            raw_payload=payload,
        )

    if part_type == "tool-result":
        # 工具执行完成，返回结果
        content = part.get("content", "")
        if isinstance(content, list):
            # opencode 有时将 content 包装为 [{"type":"text","text":"..."}] 列表
            content = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict)
            )
        return NormalizedEvent(
            kind="tool_call_finished",
            payload={
                "tool_use_id": part.get("toolUseId") or part.get("id"),
                "result": content,
                "is_error": part.get("isError", False),
            },
            raw_type=event_type,
            raw_payload=payload,
        )

    # 其他 part type（"text"、"step-start"、"step-finish" 等）→ 忽略
    return None


# ── 专项信号提取 ──────────────────────────────────────────────────────────────

def extract_permission_request(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """从 opencode SSE 事件中提取权限请求信息（如有）。

    opencode 在需要人工审批时，会将权限请求内嵌到以下两种事件中：
        1. session.status — payload.permissions[] 列表（含 pending 状态的权限）
        2. message.part.updated — part.permissionId 字段（以 "per" 开头）

    返回 dict 含：
        permission_id: str（以 "per" 开头）
        tool:          str（工具名）
        description:   str
        args:          dict
        title:         str

    若事件不含权限请求，返回 None。
    """
    event_type = event.get("type", "")
    payload = event.get("payload", {})

    if not isinstance(payload, dict):
        return None

    # 方式 1: session.status 中包含 pending permissions 列表
    if event_type == "session.status":
        permissions = (
            payload.get("permissions")
            or payload.get("pendingPermissions")
            or []
        )
        if isinstance(permissions, list) and len(permissions) > 0:
            perm = permissions[0]
            pid = perm.get("id") or perm.get("permissionId")
            if pid and str(pid).startswith("per"):
                return {
                    "permission_id": pid,
                    "tool": perm.get("tool") or perm.get("toolName") or "unknown",
                    "description": perm.get("description") or perm.get("message") or "",
                    "args": perm.get("input") or perm.get("args") or {},
                    "title": perm.get("title") or "",
                }

    # 方式 2: message.part.updated 中 part.permissionId 字段
    if event_type == "message.part.updated":
        part = payload.get("part", {})
        if isinstance(part, dict):
            pid = part.get("permissionId") or part.get("permId")
            if pid and str(pid).startswith("per"):
                return {
                    "permission_id": pid,
                    "tool": part.get("toolName") or part.get("tool") or "unknown",
                    "description": part.get("message") or part.get("description") or "",
                    "args": part.get("input") or part.get("args") or {},
                    "title": part.get("title") or "",
                }

    return None


def is_session_idle(event: dict[str, Any]) -> bool:
    """判断 opencode 事件是否表示会话进入 idle（任务完成信号）。"""
    event_type = event.get("type", "")
    if event_type == OpenCodeEventType.session_idle:
        return True
    if event_type == OpenCodeEventType.session_status:
        payload = event.get("payload", {})
        if isinstance(payload, dict):
            return payload.get("status") == "idle"
    return False


def extract_diff(event: dict[str, Any]) -> Optional[list]:
    """从 session.diff 事件中提取 diff 列表（无 diff 时返回 None）。"""
    if event.get("type") != OpenCodeEventType.session_diff:
        return None
    payload = event.get("payload", {})
    if isinstance(payload, dict):
        diff = payload.get("diff")
        if isinstance(diff, list):
            return diff
    return None
