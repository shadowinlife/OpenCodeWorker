"""
ConversationsWriter（W2-2）—— 把 driver SSE 流组装成 JSONL 演进证据。

与上层业务领域完全解耦（参见 W2-1 design §3.1 不变量）。
本拦截器只做序列化落盘 + 安全清洗；语义判断在上游编排层。

驱动事件（订阅清单）：
    initial_user_message / initial_assistant_message / initial_system_message
                       —— driver 合成的 TaskRequest.messages 注入回放
    assistant_delta    —— LLM 流式增量；连续多条 coalesce 为一条 assistant message
    tool_call_started  —— 记录工具名 + args（截断 + 脱敏）
    tool_call_finished —— 记录结果（截断 + 脱敏）
    decision_received  —— 人工/auto 决策记录

终态行为：
    1. flush 任何尚未刷掉的 assistant buffer
    2. 调 summarize_callback(messages) 取 slug；失败/缺省 → fallback `untitled-{tid[:6]}`
    3. 强制 slug 正则；不合规 → fallback
    4. 写 JSONL 到 <artifacts_dir>/<task_id>/conversations/<ISO8601>-<slug>.jsonl
    5. 返回 InterceptorArtifact，metadata.conversations_path 指向文件

W2-1 基础设施设计：
    claudedocs/design_w2_1_event_interceptor_20260520.md
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union

from worker.adapters.opencode.interceptors.base import EventInterceptor
from worker.adapters.opencode.interceptors.types import (
    InterceptorArtifact,
    InterceptorEvent,
    TerminalSignal,
)
from worker.config import get_settings

logger = logging.getLogger(__name__)


# ── 配置常量 ────────────────────────────────────────────────────────────────

#: 单条 message content 字符上限；超过截断 + 加 marker
_DEFAULT_MAX_CONTENT_CHARS = 32 * 1024
#: 总 message 条数上限；超过保留最近 N 条 + 头部 sentinel
_DEFAULT_MAX_MESSAGES = 2000
#: slug 正则（kebab-case，3-41 字符；首字符允许数字以匹配 fallback `untitled-<id>`）
_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,40}$")

#: 敏感信息脱敏正则
_SCRUBBERS: list[tuple[re.Pattern[str], str]] = [
    # OpenAI / Anthropic / Generic API key shapes
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "<REDACTED:api_key>"),
    (re.compile(r"\b(sk_live|sk_test)_[A-Za-z0-9]{16,}"), "<REDACTED:api_key>"),
    (re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"), "<REDACTED:slack_token>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<REDACTED:aws_access_key>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"), "<REDACTED:github_token>"),
    # 18-digit China citizen ID number (last digit may be X)
    (re.compile(r"\b\d{17}[\dXx]\b"), "<REDACTED:id_number>"),
    # Bearer header values (defensive — users may paste curl examples)
    (re.compile(r"(?i)Bearer\s+[A-Za-z0-9._\-]{20,}"), "Bearer <REDACTED>"),
]


SummarizeCallback = Callable[
    [list[dict[str, Any]]],
    Union[Optional[str], Awaitable[Optional[str]]],
]


def scrub(text: str) -> str:
    """Apply all sensitive-info scrubbers to a string. Idempotent."""
    if not text:
        return text
    out = text
    for pat, replacement in _SCRUBBERS:
        out = pat.sub(replacement, out)
    return out


# ── 拦截器主体 ─────────────────────────────────────────────────────────────


class ConversationsWriter(EventInterceptor):
    """累积 SSE 事件 → 终态时写 JSONL 到 conversations/ 目录。

    Args:
        summarize_callback: 可选的 slug 生成回调；接受 messages 列表，返回 3-4
                            词 kebab-case slug（或 None 表示用 fallback）。
                            可同步或异步。worker 不直接调 LLM，由上游决定回调实现。
        max_content_chars:  单条消息 content 截断上限
        max_messages:       总条数上限（FIFO 截断，留 sentinel）
        slug_prefix:        fallback slug 前缀（默认 "untitled"）
    """

    def __init__(
        self,
        *,
        summarize_callback: Optional[SummarizeCallback] = None,
        max_content_chars: int = _DEFAULT_MAX_CONTENT_CHARS,
        max_messages: int = _DEFAULT_MAX_MESSAGES,
        slug_prefix: str = "untitled",
    ):
        self._summarize_callback = summarize_callback
        self._max_content_chars = max_content_chars
        self._max_messages = max_messages
        self._slug_prefix = slug_prefix

        # 累积状态
        self._messages: list[dict[str, Any]] = []
        self._assistant_buffer: list[str] = []
        self._assistant_buffer_started_at: Optional[float] = None
        self._task_id: Optional[str] = None
        self._truncated_count = 0

    @property
    def name(self) -> str:
        return "conversations"

    # ── 事件处理 ────────────────────────────────────────────────────────────

    async def on_event(self, event: InterceptorEvent) -> None:
        # 第一次见到事件时锚定 task_id（拦截器构造时拦截器还不知道 task_id）
        if self._task_id is None:
            self._task_id = event.task_id

        kind = event.normalized_kind
        if kind is None:
            return
        payload = event.normalized_payload or {}

        # 初始注入消息：driver 合成的 user/assistant/system 历史
        if kind.startswith("initial_") and kind.endswith("_message"):
            self._flush_assistant_buffer()
            role = str(payload.get("role") or "user")
            content = str(payload.get("content") or "")
            self._append_message(role, content, ts=event.received_at)
            return

        # LLM 流式增量：累积，不立刻入库
        if kind == "assistant_delta":
            text = str(payload.get("content") or "")
            if not text:
                return
            if not self._assistant_buffer:
                self._assistant_buffer_started_at = event.received_at
            self._assistant_buffer.append(text)
            return

        # 工具调用 / 决策事件 → 先 flush assistant buffer，再写一条独立消息
        if kind == "tool_call_started":
            self._flush_assistant_buffer()
            self._append_message(
                role="tool_call",
                content=self._render_tool_call_started(payload),
                ts=event.received_at,
                extra={
                    "tool": str(payload.get("tool") or ""),
                    "tool_use_id": str(payload.get("tool_use_id") or ""),
                },
            )
            return

        if kind == "tool_call_finished":
            self._flush_assistant_buffer()
            result = payload.get("result")
            self._append_message(
                role="tool_result",
                content=self._render_tool_result(result),
                ts=event.received_at,
                extra={
                    "tool_use_id": str(payload.get("tool_use_id") or ""),
                    "is_error": bool(payload.get("is_error", False)),
                },
            )
            return

        if kind == "decision_received":
            self._flush_assistant_buffer()
            self._append_message(
                role="system",
                content="",
                ts=event.received_at,
                extra={
                    "kind": "decision_received",
                    "decision_id": str(payload.get("decision_id") or ""),
                    "choice": str(payload.get("choice") or ""),
                    "auto_approved": bool(payload.get("auto_approved", False)),
                },
            )
            return

        # 其他事件：忽略（非对话语义）

    async def on_terminal(self, signal: TerminalSignal) -> None:
        # 终态前确保最后一段 assistant 文本被刷出
        self._flush_assistant_buffer()
        # 记录终态状态以便审计 metadata
        self._terminal_status = signal.status
        self._terminal_reason = signal.reason

    # ── 终态落盘 ────────────────────────────────────────────────────────────

    async def flush(self) -> Optional[InterceptorArtifact]:
        # 兜底再 flush 一次（on_terminal 可能未跑到）
        self._flush_assistant_buffer()

        if not self._messages or self._task_id is None:
            return None

        # slug 决议：callback → 校验 → fallback
        slug, slug_source = await self._resolve_slug()

        # 落盘路径：必须落在 artifacts_dir/task_id/conversations/ 下，由 driver
        # _register_interceptor_artifact 二次校验（W2-1 §4.4）
        artifacts_root = get_settings().artifacts_dir / self._task_id
        target_dir = artifacts_root / "conversations"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "ConversationsWriter task=%s mkdir failed: %s",
                self._task_id, exc,
            )
            return None

        ts_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"{ts_str}-{slug}.jsonl"
        target = target_dir / filename

        try:
            with target.open("w", encoding="utf-8") as fh:
                for msg in self._messages:
                    fh.write(json.dumps(msg, ensure_ascii=False))
                    fh.write("\n")
        except OSError as exc:
            logger.warning(
                "ConversationsWriter task=%s write failed: %s",
                self._task_id, exc,
            )
            return None

        return InterceptorArtifact(
            artifact_type="custom",
            filename=filename,
            local_path=str(target),
            metadata={
                "subtype": "conversations",
                "conversations_path": str(target),
                "slug": slug,
                "slug_source": slug_source,
                "message_count": len(self._messages),
                "truncated_count": self._truncated_count,
                "terminal_status": getattr(self, "_terminal_status", None),
            },
        )

    # ── slug 决议 ───────────────────────────────────────────────────────────

    async def _resolve_slug(self) -> tuple[str, str]:
        """返回 (slug, source)；source ∈ {"callback", "callback-rejected",
        "callback-failed", "fallback"}。"""
        fallback = self._build_fallback_slug()
        if self._summarize_callback is None:
            return fallback, "fallback"
        try:
            raw = self._summarize_callback(list(self._messages))
            if hasattr(raw, "__await__"):
                raw = await raw  # type: ignore[misc]
        except Exception:
            logger.exception(
                "ConversationsWriter task=%s summarize_callback raised",
                self._task_id,
            )
            return fallback, "callback-failed"
        if not isinstance(raw, str):
            return fallback, "callback-rejected"
        candidate = raw.strip().lower()
        if not _SLUG_PATTERN.match(candidate):
            return fallback, "callback-rejected"
        return candidate, "callback"

    def _build_fallback_slug(self) -> str:
        suffix = (self._task_id or "unknown")[:6]
        candidate = f"{self._slug_prefix}-{suffix}".lower()
        # 兜底：极端情况下前缀有非法字符，强制清洗
        cleaned = re.sub(r"[^a-z0-9-]", "", candidate)
        if not _SLUG_PATTERN.match(cleaned):
            cleaned = f"untitled-{suffix}".lower()
            cleaned = re.sub(r"[^a-z0-9-]", "", cleaned) or "untitled-x"
        return cleaned

    # ── helper ──────────────────────────────────────────────────────────────

    def _flush_assistant_buffer(self) -> None:
        if not self._assistant_buffer:
            return
        joined = "".join(self._assistant_buffer)
        self._append_message(
            role="assistant",
            content=joined,
            ts=self._assistant_buffer_started_at or time.time(),
        )
        self._assistant_buffer = []
        self._assistant_buffer_started_at = None

    def _append_message(
        self,
        role: str,
        content: str,
        ts: float,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        scrubbed = scrub(content)
        truncated_marker = ""
        if len(scrubbed) > self._max_content_chars:
            scrubbed = scrubbed[: self._max_content_chars]
            truncated_marker = "[truncated]"
            self._truncated_count += 1
        msg: dict[str, Any] = {"role": role, "content": scrubbed, "ts": ts}
        if truncated_marker:
            msg["truncated"] = truncated_marker
        if extra:
            for k, v in extra.items():
                if isinstance(v, str):
                    v = scrub(v)
                msg[k] = v
        self._messages.append(msg)
        # 总条数防护：保留首条 + 最后 (max-2) 条 + 一条 sentinel
        if len(self._messages) > self._max_messages:
            head = self._messages[:1]
            tail = self._messages[-(self._max_messages - 2):]
            sentinel = {
                "role": "system",
                "content": "",
                "ts": ts,
                "kind": "messages_truncated",
                "dropped": len(self._messages) - len(head) - len(tail),
            }
            self._messages = head + [sentinel] + tail

    @staticmethod
    def _render_tool_call_started(payload: dict[str, Any]) -> str:
        tool = payload.get("tool") or "unknown"
        args = payload.get("args") or {}
        try:
            args_repr = json.dumps(args, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            args_repr = str(args)
        return f"call {tool}({args_repr})"

    @staticmethod
    def _render_tool_result(result: Any) -> str:
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        try:
            return json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(result)


__all__ = ["ConversationsWriter", "scrub"]
