"""
BacktestInterceptor（W2-3）—— 把 backtest-style 工具调用产出复制到 artifacts/backtests/。

与上层业务领域完全解耦（参见 W2-1 design §3.1 不变量）。
本拦截器只做"匹配 → 复制 → 索引"三件事，不预设任何业务概念；匹配模式由上游
`opencode_profile.interceptors` 注入（见上游 backlog 设计文档）。

工作流（订阅清单）：
    tool_call_started  —— 暂存 tool_use_id → (tool_name, args)
    tool_call_finished —— 查回 args；若 tool 名匹配 pattern 且非错误，
                          抽取 `run_dir`、复制到 backtests/{ISO8601}-{label}/

终态行为（flush）：
    1. 若一次都没复制 → 返回 None（不产生空 artifact）
    2. 写一份 `backtests/index.json` 汇总（label / source / dest / copied_at）
    3. 返回 InterceptorArtifact 指向 index.json，metadata.backtests[] 列出
       所有目录相对路径

幂等性：以源路径为 dedup key，同一 run_dir 重复出现只复制一次。

设计依据见上游 backlog 文档。
"""
from __future__ import annotations

import fnmatch
import json
import logging
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from worker.adapters.opencode.interceptors.base import EventInterceptor
from worker.adapters.opencode.interceptors.types import (
    InterceptorArtifact,
    InterceptorEvent,
    TerminalSignal,
)
from worker.config import get_settings

logger = logging.getLogger(__name__)


# ── 常量 ───────────────────────────────────────────────────────────────────

#: label 校验：kebab-case，3-41 字符；不合规自动 fallback
_LABEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,40}$")
#: 单条 tool_call_started 暂存上限（防 LLM 异常刷量）
_DEFAULT_MAX_PENDING_CALLS = 512


# ── 拦截器主体 ─────────────────────────────────────────────────────────────


class BacktestInterceptor(EventInterceptor):
    """监听 backtest-style 工具调用，复制 run_dir 到 artifacts/backtests/ 子目录。

    Args:
        tool_pattern:       fnmatch 风格模式（默认 ``"*.backtest"``）；匹配
                            ``tool_call_started.tool`` 字段。**必须**由编排层显式
                            注入业务相关 pattern，本拦截器内部不预设业务名。
        run_dir_key:        从 tool args 中读取源目录的 key（默认 ``"run_dir"``）
        label_prefix:       默认 label 前缀（默认 ``"iter"``，产出 ``iter-1`` /
                            ``iter-2`` …）
        workspace_root:     相对路径解析根（可选）。源路径绝对时直接使用；相对时
                            若提供 root 则相对解析，否则跳过 + 日志。
        max_pending_calls:  暂存 tool_use_id 上限；溢出时丢最早条目（FIFO）

    禁忌（W2-1 invariants）：
        - 不写 DB；落盘走 flush() 返回 InterceptorArtifact
        - 不发 SSE 事件
        - 不调用 OpenCodeClient
    """

    def __init__(
        self,
        *,
        tool_pattern: str = "*.backtest",
        run_dir_key: str = "run_dir",
        label_prefix: str = "iter",
        workspace_root: Optional[Union[str, Path]] = None,
        max_pending_calls: int = _DEFAULT_MAX_PENDING_CALLS,
    ):
        self._tool_pattern = tool_pattern
        self._run_dir_key = run_dir_key
        self._label_prefix = label_prefix
        self._workspace_root = (
            Path(workspace_root).resolve() if workspace_root else None
        )
        self._max_pending_calls = max_pending_calls

        # 累积状态
        self._pending_calls: dict[str, tuple[str, Mapping[str, Any]]] = {}
        self._copied_sources: set[str] = set()
        self._records: list[dict[str, Any]] = []
        self._auto_counter: int = 0
        self._task_id: Optional[str] = None
        self._terminal_status: Optional[str] = None

    @property
    def name(self) -> str:
        return "backtest"

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
            await self._handle_finished(event, payload)
            return

    async def on_terminal(self, signal: TerminalSignal) -> None:
        self._terminal_status = signal.status

    # ── 终态落盘 ────────────────────────────────────────────────────────────

    async def flush(self) -> Optional[InterceptorArtifact]:
        if not self._records or self._task_id is None:
            return None

        artifacts_root = get_settings().artifacts_dir / self._task_id
        target_dir = artifacts_root / "backtests"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "BacktestInterceptor task=%s mkdir failed: %s",
                self._task_id, exc,
            )
            return None

        index_path = target_dir / "index.json"
        payload = {
            "task_id": self._task_id,
            "terminal_status": self._terminal_status,
            "count": len(self._records),
            "backtests": self._records,
        }
        try:
            with index_path.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.warning(
                "BacktestInterceptor task=%s write index failed: %s",
                self._task_id, exc,
            )
            return None

        return InterceptorArtifact(
            artifact_type="custom",
            filename="backtests/index.json",
            local_path=str(index_path),
            metadata={
                "subtype": "backtests",
                "count": len(self._records),
                "backtests": [r["dest_relpath"] for r in self._records],
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
                first_key = next(iter(self._pending_calls))
                self._pending_calls.pop(first_key, None)
            except StopIteration:
                pass
        self._pending_calls[tool_use_id] = (tool_name, args)

    # ── helper：tool_call_finished ──────────────────────────────────────────

    async def _handle_finished(
        self, event: InterceptorEvent, payload: Mapping[str, Any],
    ) -> None:
        tool_use_id = payload.get("tool_use_id")
        if not isinstance(tool_use_id, str):
            return
        pending = self._pending_calls.pop(tool_use_id, None)
        if pending is None:
            return
        tool_name, args = pending

        if not fnmatch.fnmatchcase(tool_name, self._tool_pattern):
            return
        if payload.get("is_error"):
            return

        raw_run_dir = args.get(self._run_dir_key)
        if not isinstance(raw_run_dir, str) or not raw_run_dir.strip():
            logger.debug(
                "BacktestInterceptor task=%s tool=%s missing %r in args",
                self._task_id, tool_name, self._run_dir_key,
            )
            return

        source = self._resolve_source(raw_run_dir.strip())
        if source is None:
            return

        source_key = str(source)
        if source_key in self._copied_sources:
            logger.debug(
                "BacktestInterceptor task=%s skip already-copied source: %s",
                self._task_id, source_key,
            )
            return

        label, label_source = self._resolve_label(event.raw_payload)
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest_name = f"{ts_str}-{label}"

        artifacts_root = get_settings().artifacts_dir / self._task_id
        dest_dir = artifacts_root / "backtests" / dest_name

        try:
            dest_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, dest_dir)
        except (OSError, shutil.Error) as exc:
            logger.warning(
                "BacktestInterceptor task=%s copy failed: %s → %s: %s",
                self._task_id, source, dest_dir, exc,
            )
            return

        self._copied_sources.add(source_key)
        self._records.append({
            "label": label,
            "label_source": label_source,
            "tool": tool_name,
            "source": source_key,
            "dest_relpath": f"backtests/{dest_name}",
            "copied_at": time.time(),
        })
        logger.info(
            "BacktestInterceptor task=%s copied %s → %s (label=%s)",
            self._task_id, source, dest_dir, label,
        )

    # ── helper：路径解析 ───────────────────────────────────────────────────

    def _resolve_source(self, raw: str) -> Optional[Path]:
        candidate = Path(raw)
        if not candidate.is_absolute():
            if self._workspace_root is None:
                logger.debug(
                    "BacktestInterceptor task=%s relative run_dir %r without "
                    "workspace_root → skip",
                    self._task_id, raw,
                )
                return None
            candidate = self._workspace_root / candidate
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            logger.warning(
                "BacktestInterceptor task=%s resolve failed: %s (%s)",
                self._task_id, raw, exc,
            )
            return None
        if not resolved.is_dir():
            logger.debug(
                "BacktestInterceptor task=%s run_dir not a directory: %s",
                self._task_id, resolved,
            )
            return None
        return resolved

    # ── helper：label 决议 ─────────────────────────────────────────────────

    def _resolve_label(
        self, raw_payload: Mapping[str, Any],
    ) -> tuple[str, str]:
        """返回 (label, source)。source ∈ {"override", "auto"}。

        override 取自原始事件的 ``part.metadata.backtest_label``（opencode 把
        工具结果的 metadata 透传到 part 上）；不合规则回退到 auto。
        """
        override = self._extract_override_label(raw_payload)
        if override is not None:
            candidate = override.strip().lower()
            if _LABEL_PATTERN.match(candidate):
                return candidate, "override"
            logger.debug(
                "BacktestInterceptor task=%s reject malformed label %r",
                self._task_id, override,
            )
        # auto: iter-N（N 从 1 起递增；只对 auto 路径计数，override 不消耗）
        self._auto_counter += 1
        return f"{self._label_prefix}-{self._auto_counter}", "auto"

    @staticmethod
    def _extract_override_label(
        raw_payload: Mapping[str, Any],
    ) -> Optional[str]:
        part = raw_payload.get("part") if isinstance(raw_payload, Mapping) else None
        if not isinstance(part, Mapping):
            return None
        meta = part.get("metadata")
        if not isinstance(meta, Mapping):
            return None
        value = meta.get("backtest_label")
        return value if isinstance(value, str) else None


__all__ = ["BacktestInterceptor"]
