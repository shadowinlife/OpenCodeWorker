"""
McpFieldRecorder（W2-4）—— 聚合 tool_call_finished 事件，按
(mcp_name, tool_name) 统计已观测到的 input/output 字段，终态时写
mcp_field_summary.json 独立 artifact。

与上层业务领域完全解耦（参见 W2-1 design §3.1 不变量）。本拦截器只做
"聚合 → 序列化"，不预设任何业务概念；mcp_name 提取规则由编排层注入。

订阅清单：
    tool_call_started  —— 暂存 tool_use_id → (tool_name, args)
    tool_call_finished —— 查回 args；提取 mcp_name（regex 第 1 组）；
                          input fields 取 args top-level keys；
                          output fields 取 raw_payload.part.metadata.read_fields[]

终态行为（flush）：
    1. 若一次都没聚合 → 返回 None（不产生空 artifact）
    2. 写 mcp_field_summary.json 到 <artifacts_dir>/<task_id>/
    3. 返回 InterceptorArtifact，metadata.tool_count = 聚合的 (mcp,tool) 个数

幂等性：同一 tool_use_id 重复出现只统计一次。

设计依据见上游 backlog 文档。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping, Optional

from worker.adapters.opencode.interceptors.base import EventInterceptor
from worker.adapters.opencode.interceptors.types import (
    InterceptorArtifact,
    InterceptorEvent,
    TerminalSignal,
)
from worker.config import get_settings

logger = logging.getLogger(__name__)


# ── 常量 ───────────────────────────────────────────────────────────────────

#: 默认 mcp_name 提取正则：取 tool 名第一个 "." 之前的 kebab-case 段
_DEFAULT_MCP_NAME_REGEX = r"^([a-z][a-z0-9-]+)\."
#: tool_call_started 暂存上限（防 LLM 异常刷量）
_DEFAULT_MAX_PENDING_CALLS = 1024


# ── 拦截器主体 ─────────────────────────────────────────────────────────────


class McpFieldRecorder(EventInterceptor):
    """聚合所有 tool_call_finished，按 (mcp_name, tool_name) 记录字段使用。

    Args:
        mcp_name_pattern:    从 tool 名提取 mcp 命名空间的正则；第 1 个捕获组即
                             ``mcp_name``。默认 ``^([a-z][a-z0-9-]+)\\.``。
                             不匹配的 tool（如本地内置工具）被忽略。
        read_fields_key:     从 ``raw_payload.part.metadata`` 读取 output fields
                             提示的 key（默认 ``"read_fields"``）。
        max_pending_calls:   暂存 tool_use_id 上限；溢出时丢最早条目（FIFO）。

    禁忌（W2-1 invariants）：
        - 不写 DB；落盘走 flush() 返回 InterceptorArtifact
        - 不发 SSE 事件
        - 不调用 OpenCodeClient
    """

    def __init__(
        self,
        *,
        mcp_name_pattern: str = _DEFAULT_MCP_NAME_REGEX,
        read_fields_key: str = "read_fields",
        max_pending_calls: int = _DEFAULT_MAX_PENDING_CALLS,
    ):
        try:
            self._mcp_name_re = re.compile(mcp_name_pattern)
        except re.error as exc:
            raise ValueError(
                f"invalid mcp_name_pattern {mcp_name_pattern!r}: {exc}"
            ) from exc
        self._read_fields_key = read_fields_key
        self._max_pending_calls = max_pending_calls

        # 累积状态
        self._pending_calls: dict[str, tuple[str, Mapping[str, Any]]] = {}
        self._processed_calls: set[str] = set()
        self._aggregates: dict[tuple[str, str], dict[str, Any]] = {}
        self._task_id: Optional[str] = None
        self._terminal_status: Optional[str] = None

    @property
    def name(self) -> str:
        return "mcp-fields"

    # ── 事件处理 ────────────────────────────────────────────────────────────

    async def on_event(self, event: InterceptorEvent) -> None:
        if self._task_id is None:
            self._task_id = event.task_id

        kind = event.normalized_kind
        if kind is None:
            return
        payload = event.normalized_payload or {}

        if kind == "tool_call_started":
            self._stash_pending(payload)
            return

        if kind == "tool_call_finished":
            self._aggregate_finished(event, payload)
            return

    async def on_terminal(self, signal: TerminalSignal) -> None:
        self._terminal_status = signal.status

    # ── 终态落盘 ────────────────────────────────────────────────────────────

    async def flush(self) -> Optional[InterceptorArtifact]:
        if not self._aggregates or self._task_id is None:
            return None

        artifacts_root = get_settings().artifacts_dir / self._task_id
        try:
            artifacts_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "McpFieldRecorder task=%s mkdir failed: %s",
                self._task_id, exc,
            )
            return None

        target = artifacts_root / "mcp_field_summary.json"
        tools: list[dict[str, Any]] = []
        for (mcp_name, tool_name), agg in sorted(self._aggregates.items()):
            tools.append({
                "mcp_name": mcp_name,
                "tool_name": tool_name,
                "call_count": agg["call_count"],
                "required_input_fields": sorted(agg["input_fields"]),
                "required_output_fields": sorted(agg["output_fields"]),
            })

        payload = {
            "task_id": self._task_id,
            "terminal_status": self._terminal_status,
            "tool_count": len(tools),
            "tools": tools,
        }
        try:
            with target.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.warning(
                "McpFieldRecorder task=%s write failed: %s",
                self._task_id, exc,
            )
            return None

        return InterceptorArtifact(
            artifact_type="custom",
            filename="mcp_field_summary.json",
            local_path=str(target),
            metadata={
                "subtype": "mcp_field_summary",
                "tool_count": len(tools),
                "terminal_status": self._terminal_status,
            },
        )

    # ── helper：tool_call_started ───────────────────────────────────────────

    def _stash_pending(self, payload: Mapping[str, Any]) -> None:
        tool_use_id = payload.get("tool_use_id")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            return
        tool_name = payload.get("tool")
        if not isinstance(tool_name, str):
            return
        args = payload.get("args") or {}
        if not isinstance(args, Mapping):
            args = {}
        # 溢出保护：丢最早条目
        if len(self._pending_calls) >= self._max_pending_calls:
            try:
                first = next(iter(self._pending_calls))
                self._pending_calls.pop(first, None)
            except StopIteration:
                pass
        self._pending_calls[tool_use_id] = (tool_name, args)

    # ── helper：tool_call_finished ──────────────────────────────────────────

    def _aggregate_finished(
        self, event: InterceptorEvent, payload: Mapping[str, Any],
    ) -> None:
        tool_use_id = payload.get("tool_use_id")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            return
        if tool_use_id in self._processed_calls:
            return
        pending = self._pending_calls.pop(tool_use_id, None)
        if pending is None:
            return
        self._processed_calls.add(tool_use_id)
        tool_name, args = pending

        m = self._mcp_name_re.match(tool_name)
        if not m:
            return
        try:
            mcp_name = m.group(1)
        except IndexError:
            logger.debug(
                "McpFieldRecorder task=%s pattern %r has no group(1)",
                self._task_id, self._mcp_name_re.pattern,
            )
            return

        key = (mcp_name, tool_name)
        agg = self._aggregates.setdefault(key, {
            "call_count": 0,
            "input_fields": set(),
            "output_fields": set(),
        })
        agg["call_count"] += 1
        for k in args.keys():
            if isinstance(k, str):
                agg["input_fields"].add(k)

        for field in self._extract_read_fields(event.raw_payload):
            agg["output_fields"].add(field)

    # ── helper：output fields 提取 ─────────────────────────────────────────

    def _extract_read_fields(
        self, raw_payload: Mapping[str, Any],
    ) -> list[str]:
        part = raw_payload.get("part") if isinstance(raw_payload, Mapping) else None
        if not isinstance(part, Mapping):
            return []
        meta = part.get("metadata")
        if not isinstance(meta, Mapping):
            return []
        value = meta.get(self._read_fields_key)
        if not isinstance(value, (list, tuple)):
            return []
        out: list[str] = []
        for v in value:
            if isinstance(v, str) and v:
                out.append(v)
        return out


__all__ = ["McpFieldRecorder"]
