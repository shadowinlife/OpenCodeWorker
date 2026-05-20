"""
OpenCode Task Driver — Phase 3 核心实现。

替换 Orchestrator._drive_opencode 的 Phase 2 stub，实现完整的 opencode HTTP 交互。

职责：
    1. 创建 opencode session
    2. 注入 TaskRequest.messages（noReply=True，仅写入上下文）
    3. 按 mode（plan_first / direct_execute）路由 agent 并调用 prompt_async
    4. 订阅 SSE /global/event，归一化事件写入 DB（insert_event）
    5. 权限请求 → HITL DecisionRequest → 轮询 DB → respond_permission
    6. plan_first：捕获 plan → plan_ready 事件 → 等待审批 → 继续 / 中止
    7. 收集 artifacts（diff JSON + transcript JSON）
    8. 优雅 abort 和超时处理

使用方式：
    from worker.adapters.opencode.driver import OpenCodeDriver
    driver = OpenCodeDriver(task_id, request, host_port, container_env, db)
    await driver.run()
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Optional, Sequence

import aiosqlite

from worker.adapters.opencode.client import OpenCodeClient
from worker.adapters.opencode.event_stream import (
    extract_diff,
    extract_permission_request,
    is_session_idle,
    normalize_opencode_event,
)
from worker.adapters.opencode.interceptors import (
    EventInterceptor,
    InterceptorArtifact,
    InterceptorEvent,
    InterceptorRunner,
    TerminalSignal,
)
from worker.config import get_settings
from worker.contract.artifact import Artifact, ArtifactType
from worker.contract.decision import DecisionChoice, DecisionKind, DecisionRequest
from worker.contract.event import TaskEventKind
from worker.contract.exceptions import TaskAbortedError, TaskTimedOutError
from worker.contract.task import TaskMode, TaskRequest, TaskStatus
from worker.observability import metrics
from worker.storage.repo import (
    expire_decision,
    get_resolved_decision,
    insert_artifact,
    insert_decision,
    insert_event,
    update_task_status,
)

logger = logging.getLogger(__name__)

# Agent 路由对齐 ADR-001 / ADR-006 / oh-my-openagent 4.1.2：
#   - plan_first    → "Prometheus" （规划 agent，read-only 工具集）
#   - direct_exec   → "Sisyphus"   （执行 agent，bash/write/edit/webfetch 全开）
#
# 这两个 agent 名称的可用性由容器入口脚本（docker/worker/entrypoint.sh）在
# opencode serve 启动后通过 GET /agent 验证：oh-my 未加载或 agent 缺失时
# 容器启动失败（非零退出码），不会回退到 opencode 内置 "plan"/"build"。
AGENT_PROMETHEUS = "Prometheus"
AGENT_SISYPHUS = "Sisyphus"

# HITL 决策轮询间隔（秒）
_HITL_POLL_INTERVAL = 2.0

# 默认任务超时（resource_limits.timeout_sec 可覆盖）
_DEFAULT_TIMEOUT_SEC = 1800

# P1-15: 连续用户 reject 阈值。opencode 的 reject 是单次拒绝（工具会再 ask），
# 没有计数上限会让 agent 与 reject 形成死循环直到 timeout。
# 达到阈值后自动 abort 并发 mode_escalation_suggested 事件。
# 计数仅累积**用户主动 reject**；approve 重置为 0；abort/timeout 走各自终态分支。
_REJECT_THRESHOLD = 3


class OpenCodeDriver:
    """驱动容器内 opencode 完成一个任务的完整生命周期。"""

    def __init__(
        self,
        task_id: str,
        request: TaskRequest,
        host_port: int,
        container_env: dict[str, str],
        db: aiosqlite.Connection,
        interceptors: Sequence[EventInterceptor] = (),
    ):
        """构造 driver。

        Args:
            interceptors: W2-1 — 事件拦截器列表，默认空（行为与 W2-1 之前完全一致）。
                          driver 通过 InterceptorRunner 隔离每个拦截器的错误，
                          单个抛错不影响 SSE 主循环或兄弟拦截器。
        """
        self.task_id = task_id
        self.request = request
        self.host_port = host_port
        self.password = container_env.get("OPENCODE_SERVER_PASSWORD", "")
        self.db = db
        self.settings = get_settings()
        self._runner = InterceptorRunner(interceptors)

        # 状态
        self.client: Optional[OpenCodeClient] = None
        self.session_id: Optional[str] = None

        # 中止信号：外部 abort 或超时时设置，SSE 消费循环检测后退出
        self._abort_event = asyncio.Event()
        # abort 来源（与 TaskAbortedError.reason 对齐）：在 _abort_event.set 时一并赋值，
        # 由 _run_inner 在抛出 TaskAbortedError 时透传给 queue
        self._abort_reason: Optional[str] = None
        self._abort_decision_id: Optional[str] = None

        # 累积 assistant 文本（plan_first 时用于提取 plan）
        self._assistant_buffer: list[str] = []
        # plan_first 模式下提取到的计划全文
        self._plan_text: Optional[str] = None
        # 最近一次 session.diff 内容（artifact 收集时使用）
        self._last_diff: list = []
        # P1-15：连续 reject 计数，达到 _REJECT_THRESHOLD 时自动 abort 防死循环
        self._reject_count: int = 0

        # 超时时间
        self.timeout_sec = _DEFAULT_TIMEOUT_SEC
        if request.resource_limits and request.resource_limits.timeout_sec:
            self.timeout_sec = request.resource_limits.timeout_sec

    # ── 公开入口 ──────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """执行完整 Phase 3 驱动逻辑，由 orchestrator._drive_opencode 调用。

        异常分类（由 queue._run_one 路由到不同终态）：
            - TaskTimedOutError: 超过 timeout_sec → task_timed_out 终态
            - TaskAbortedError:  HITL 决策中止/拒绝 → task_aborted 终态
            - 其它 Exception:    内部错误 → task_failed 终态

        W2-1：在 finally 块中先于 client 清理触发拦截器终态分发 + flush。
              拦截器异常被 InterceptorRunner 隔离，不影响终态语义。
        """
        from worker.observability.logging import set_correlation, clear_correlation
        set_correlation(task_id=self.task_id)

        client = OpenCodeClient(
            host="127.0.0.1",
            port=self.host_port,
            password=self.password,
        )
        self.client = client
        # W2-1：终态信号默认 completed；下面的 except 分支根据异常类型覆盖
        final_status: TaskStatus = TaskStatus.completed
        final_reason: Optional[str] = None
        try:
            async with asyncio.timeout(self.timeout_sec):
                await self._run_inner()
        except asyncio.TimeoutError:
            logger.warning("task %s: timed out after %ds", self.task_id, self.timeout_sec)
            if self.session_id:
                await client.abort_session(self.session_id)
            final_status = TaskStatus.timed_out
            final_reason = "timeout"
            raise TaskTimedOutError(timeout_sec=self.timeout_sec)
        except TaskAbortedError as exc:
            final_status = TaskStatus.aborted
            final_reason = exc.reason
            raise
        except TaskTimedOutError:
            final_status = TaskStatus.timed_out
            final_reason = "timeout"
            raise
        except Exception as exc:  # noqa: BLE001 — 显式分类落 final_status
            final_status = TaskStatus.failed
            final_reason = type(exc).__name__
            raise
        finally:
            # W2-1：终态信号分发 + flush；任何错误均由 runner 内部隔离
            await self._dispatch_terminal_and_flush(final_status, final_reason)
            if self.session_id:
                await client.delete_session(self.session_id)
            await client.aclose()
            clear_correlation()

    # ── 内部主流程 ─────────────────────────────────────────────────────────────

    async def _run_inner(self) -> None:
        """核心驱动流程（已在 asyncio.timeout 包装内）。"""
        from worker.observability.logging import set_correlation
        client = self.client

        # Step 1: 创建 opencode session
        session_data = await client.create_session()
        session_id = session_data.get("id") or session_data.get("sessionId")
        if not session_id:
            raise RuntimeError(
                f"opencode create_session returned no id: {session_data!r}"
            )
        self.session_id = session_id
        set_correlation(session_id=session_id)
        await update_task_status(
            self.db, self.task_id, TaskStatus.starting_opencode,
            opencode_session_id=session_id,
        )
        logger.info("task %s: opencode session=%s", self.task_id, session_id)

        # Step 2: 注入 TaskRequest.messages（历史上下文，noReply=True）
        if self.request.messages:
            await self._inject_messages(session_id)

        # Step 3: 按 mode 决定 agent 并更新状态
        mode = self.request.mode
        profile = self.request.opencode_profile
        model = profile.model if profile else None

        if mode == TaskMode.plan_first:
            await update_task_status(self.db, self.task_id, TaskStatus.planning)
            await insert_event(
                self.db, self.task_id, TaskEventKind.task_started,
                {"phase": "planning"},
            )
            agent = AGENT_PROMETHEUS
        else:
            await update_task_status(self.db, self.task_id, TaskStatus.executing)
            await insert_event(
                self.db, self.task_id, TaskEventKind.execution_started,
                {"mode": mode.value},
            )
            agent = AGENT_SISYPHUS

        # Step 4: 启动 SSE 消费（后台 asyncio Task）+ REST 轮询（并行）
        # opencode 1.14.30 的 /global/event 只传心跳；session idle 必须通过
        # REST 轮询 GET /session/{id}/message 检测（info.time.completed 有值）
        sse_task = asyncio.create_task(
            self._consume_sse(session_id),
            name=f"sse-{self.task_id[:8]}",
        )
        poll_task = asyncio.create_task(
            self._poll_session_idle(session_id),
            name=f"poll-{self.task_id[:8]}",
        )

        try:
            # Step 5: 投递 prompt_async（opencode 开始执行）
            prompt_parts = self._build_prompt_parts()
            await client.prompt_async(
                session_id=session_id,
                parts=prompt_parts,
                agent=agent,
                model=model,
            )
            logger.info(
                "task %s: prompt_async sent (agent=%s, mode=%s)",
                self.task_id, agent, mode.value,
            )

            # Step 6: SSE（错误/权限检测）与 REST 轮询并行，任一完成即退出
            done, pending = await asyncio.wait(
                [sse_task, poll_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            # 取消未完成的任务
            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            # 检查是否有异常（session.error 由 _consume_sse 抛出）
            for t in done:
                exc = t.exception()
                if exc is not None:
                    raise exc

        except Exception:
            for t in [sse_task, poll_task]:
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
            raise

        # Step 7: plan_first 模式下等待人工审批计划
        if mode == TaskMode.plan_first and not self._abort_event.is_set():
            await self._handle_plan_approval(session_id)

        # Step 8: 收集产物（diff + transcript）
        if not self._abort_event.is_set():
            await self._collect_artifacts(session_id)

        # abort_event 由权限超时或 plan 拒绝设置，抛出让 queue 写 task_aborted
        if self._abort_event.is_set():
            raise TaskAbortedError(
                reason=self._abort_reason or "system",
                decision_id=self._abort_decision_id,
            )

    # ── REST 轮询检测 session idle ────────────────────────────────────────────

    async def _poll_session_idle(self, session_id: str) -> None:
        """轮询 GET /session/{id}/message，直到最后一条 assistant 消息完成。

        opencode 1.14.30 的 /global/event SSE 只发送心跳；真正的任务完成信号
        需要通过检查 message.info.time.completed 字段来检测。
        当检测到完成时，此协程正常返回（_run_inner 会取消 _consume_sse）。
        """
        poll_interval = 5.0  # 每 5 秒轮询一次
        logger.info("task %s: starting REST poll for session idle (interval=%.0fs)",
                    self.task_id, poll_interval)
        while not self._abort_event.is_set():
            await asyncio.sleep(poll_interval)
            if self._abort_event.is_set():
                break
            try:
                messages = await self.client.get_messages(session_id)
                if self._is_session_messages_idle(messages):
                    logger.info("task %s: session idle detected via REST poll "
                                "(messages=%d)", self.task_id, len(messages))
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("task %s: REST poll error: %s", self.task_id, exc)

    @staticmethod
    def _is_session_messages_idle(messages: list) -> bool:
        """判断 session 消息列表是否表示任务已完成。

        opencode 在 assistant 消息完成时会在 info.time.completed 写入时间戳。
        检测最后一条 assistant 消息是否已有 completed 时间戳。
        """
        if not messages:
            return False
        for msg in reversed(messages):
            info = msg.get("info", {})
            if not isinstance(info, dict):
                continue
            if info.get("role") == "assistant":
                time_info = info.get("time", {})
                return bool(isinstance(time_info, dict) and time_info.get("completed"))
        return False

    # ── SSE 消费循环 ──────────────────────────────────────────────────────────

    async def _consume_sse(self, session_id: str) -> None:
        """持续消费 opencode SSE 事件直到 session idle 或 abort 信号。

        事件处理优先级：
            1. abort_event → 退出
            2. session.idle / session.status=idle → 退出（任务完成）
            3. session.diff → 更新 _last_diff
            4. permission 请求 → 派发 _handle_permission Task
            5. assistant_delta / tool_call_* → 写入 DB 事件
        """
        _pending_perm_ids: set[str] = set()  # 防止同一 permission_id 重复处理
        # direct_execute 模式：权限请求频率计数（达到阈值时发 mode_escalation_suggested）
        _perm_ask_count = 0
        _MODE_ESCALATION_THRESHOLD = 3

        try:
            async for raw_event in self.client.stream_events():
                if self._abort_event.is_set():
                    break

                # 检查 session idle（任务完成）
                if is_session_idle(raw_event):
                    logger.info("task %s: session idle → SSE done", self.task_id)
                    break

                # 检查 session.error（prompt_async 失败时快速退出）
                if raw_event.get("type") == "session.error":
                    err_msg = (
                        raw_event.get("payload", {}).get("error")
                        or raw_event.get("payload", {}).get("message")
                        or str(raw_event.get("payload", ""))
                    )
                    logger.error(
                        "task %s: session.error from opencode: %s",
                        self.task_id, err_msg,
                    )
                    raise RuntimeError(f"opencode session.error: {err_msg}")

                # 捕获 diff 快照（最新版本）
                diff = extract_diff(raw_event)
                if diff is not None:
                    self._last_diff = diff

                # 检查权限请求（去重）
                perm_req = extract_permission_request(raw_event)
                if perm_req:
                    pid = perm_req["permission_id"]
                    if pid not in _pending_perm_ids:
                        _pending_perm_ids.add(pid)
                        _perm_ask_count += 1
                        asyncio.create_task(
                            self._handle_permission(session_id, perm_req),
                            name=f"perm-{self.task_id[:8]}-{pid}",
                        )
                        # direct_execute 模式：频繁 ask 时发 mode_escalation_suggested
                        if (
                            self.request.mode == TaskMode.direct_execute
                            and _perm_ask_count == _MODE_ESCALATION_THRESHOLD
                        ):
                            await insert_event(
                                self.db, self.task_id,
                                TaskEventKind.mode_escalation_suggested,
                                {
                                    "reason": "repeated_permission_asks",
                                    "ask_count": _perm_ask_count,
                                    "suggestion": "Consider resubmitting with mode=plan_first",
                                },
                            )
                    continue

                # 归一化并写入 Worker 事件
                norm = normalize_opencode_event(raw_event)

                # W2-1：分发给拦截器（在 normalize 之后、insert_event 之前）
                # 即使 norm 为 None（心跳/sync）也允许拦截器看 raw_payload
                # — 但当前 driver 在 norm is None 时 continue，故只在 norm 非 None
                # 时通知拦截器。后续 W2-4 若需要拿到非归一化事件可再扩展。
                if norm is not None:
                    await self._dispatch_to_interceptors(
                        normalized_kind=norm.kind,
                        normalized_payload=dict(norm.payload),
                        raw_type=norm.raw_type,
                        raw_payload=dict(raw_event.get("payload") or {}),
                    )

                if norm is None:
                    continue

                # 累积 assistant 文本（plan_first 时用于提取 plan）
                if norm.kind == "assistant_delta":
                    text = norm.payload.get("content", "")
                    if text:
                        self._assistant_buffer.append(text)

                try:
                    kind = TaskEventKind(norm.kind)
                    await insert_event(self.db, self.task_id, kind, norm.payload)
                except ValueError:
                    logger.debug(
                        "task %s: unknown event kind %r, skipping",
                        self.task_id, norm.kind,
                    )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # SSE 断开：记录警告后退出，不向上抛出
            # orchestrator 会在 collecting_artifacts 阶段检测最终状态
            logger.warning("task %s: SSE stream error: %s", self.task_id, exc)

        # plan_first：SSE 完成后将累积文本保存为 plan
        if self.request.mode == TaskMode.plan_first and self._assistant_buffer:
            self._plan_text = "".join(self._assistant_buffer)

    # ── 权限 HITL 处理 ────────────────────────────────────────────────────────

    async def _handle_permission(
        self,
        session_id: str,
        perm_req: dict[str, Any],
    ) -> None:
        """处理 opencode 权限请求的完整 HITL 流程。

        流程：
            1. 写入 DecisionRequest → DB，发出 hitl_required 事件
            2. 更新状态 → awaiting_human
            3. 轮询 DB，直到 DecisionResponse 出现或超时
            4. 映射 choice → opencode response（"once" / "reject"）
            5. POST respond_permission 回传给 opencode
            6. 恢复执行状态
        """
        permission_id = perm_req["permission_id"]
        tool = perm_req.get("tool", "unknown")

        # P1-14：auto_approve 短路——若 hitl_policy.auto_approve 命中此 kind:tool，
        # 直接 respond "once" 并写 decision_received（auto_approved=True），
        # 跳过 hitl_required / awaiting_human 全流程
        matched_pattern = self._match_auto_approve(
            DecisionKind.tool_permission, tool,
        )
        if matched_pattern is not None:
            await self._auto_approve_permission(
                session_id, permission_id, tool, matched_pattern,
            )
            return

        hitl_policy = self.request.hitl_policy
        timeout_sec = hitl_policy.decision_timeout_sec if hitl_policy else 600
        on_timeout_action, default_timeout_choice = self._resolve_hitl_timeout_policy()

        decision_id = str(uuid.uuid4())
        decision_req = DecisionRequest(
            decision_id=decision_id,
            kind=DecisionKind.tool_permission,
            summary=(
                f"opencode 请求执行工具：{tool}\n"
                f"{perm_req.get('description', '')}"
            ),
            options=[
                DecisionChoice.approve,
                DecisionChoice.reject,
                DecisionChoice.abort,
            ],
            default_on_timeout=default_timeout_choice,
            expires_at=None,
            context={
                "tool": tool,
                "permission_id": permission_id,
                "args": perm_req.get("args", {}),
                "title": perm_req.get("title", ""),
            },
        )

        await insert_decision(self.db, self.task_id, decision_req)
        await insert_event(
            self.db, self.task_id, TaskEventKind.hitl_required,
            {
                "decision_id": decision_id,
                "kind": DecisionKind.tool_permission.value,
                "summary": decision_req.summary,
                "permission_id": permission_id,
            },
        )
        await update_task_status(self.db, self.task_id, TaskStatus.awaiting_human)

        # 轮询等待决策（P1-11：观测 HITL 等待耗时）
        _hitl_t0 = time.monotonic()
        resolved = await self._wait_for_decision(decision_id, timeout_sec)
        metrics.observe_hitl_wait(time.monotonic() - _hitl_t0)

        # 映射 choice → opencode response
        opencode_response = "reject"  # 保守默认
        if resolved is not None and resolved.response is not None:
            choice = resolved.response.choice
            if choice == DecisionChoice.approve:
                opencode_response = "once"
                # P1-15：approve 重置连续 reject 计数
                self._reject_count = 0
            elif choice == DecisionChoice.abort:
                self._signal_abort("permission_rejected", decision_id)
                opencode_response = "reject"
            else:
                # reject / revise：用户拒绝本次请求
                opencode_response = "reject"
                self._reject_count += 1
                # P1-15：累积达到阈值即 abort，发 mode_escalation_suggested 提示上游
                if self._reject_count >= _REJECT_THRESHOLD:
                    await insert_event(
                        self.db, self.task_id,
                        TaskEventKind.mode_escalation_suggested,
                        {
                            "reason": "reject_threshold_exceeded",
                            "reject_count": self._reject_count,
                            "threshold": _REJECT_THRESHOLD,
                            "suggestion": (
                                "Repeated rejects detected; aborting to break loop. "
                                "Consider resubmitting with a different mode or "
                                "scope-limited permissions."
                            ),
                        },
                    )
                    self._signal_abort("reject_threshold_exceeded", decision_id)
                    logger.warning(
                        "task %s: reject threshold reached (%d/%d), auto-aborting",
                        self.task_id, self._reject_count, _REJECT_THRESHOLD,
                    )
        else:
            # 超时：标记 DB decision 为 timed_out，发 hitl_timeout 事件，按策略处理
            await expire_decision(self.db, decision_id)
            await insert_event(
                self.db, self.task_id, TaskEventKind.hitl_timeout,
                {
                    "decision_id": decision_id,
                    "kind": DecisionKind.tool_permission.value,
                    "permission_id": permission_id,
                    "on_timeout": on_timeout_action,
                    "resolved_choice": default_timeout_choice.value,
                },
            )
            if default_timeout_choice == DecisionChoice.abort:
                self._signal_abort("hitl_timeout", decision_id)
                opencode_response = "reject"
            else:
                opencode_response = "once"
            logger.warning(
                "task %s: permission HITL timeout (decision_id=%s, on_timeout=%s, resolved_choice=%s)",
                self.task_id,
                decision_id,
                on_timeout_action,
                default_timeout_choice.value,
            )

        # 回传 opencode
        try:
            await self.client.respond_permission(session_id, permission_id, opencode_response)
            decision_payload = {
                "decision_id": decision_id,
                "choice": opencode_response,
                "permission_id": permission_id,
            }
            await insert_event(
                self.db, self.task_id, TaskEventKind.decision_received,
                decision_payload,
            )
            # W2-1：合成 decision_received 事件分发给拦截器
            await self._dispatch_to_interceptors(
                normalized_kind=TaskEventKind.decision_received.value,
                normalized_payload=decision_payload,
                raw_type="<synthesized:decision_received>",
            )
        except Exception as exc:
            logger.warning(
                "task %s: respond_permission %s failed: %s",
                self.task_id, permission_id, exc,
            )

        # 恢复执行状态（若未 abort）
        if not self._abort_event.is_set():
            if self.request.mode == TaskMode.plan_first:
                await update_task_status(self.db, self.task_id, TaskStatus.planning)
            else:
                await update_task_status(self.db, self.task_id, TaskStatus.executing)

    # ── plan_first 审批 ───────────────────────────────────────────────────────

    async def _handle_plan_approval(self, session_id: str) -> None:
        """plan_first 模式：发出 plan_ready 事件并等待人工审批。

        approve → execution_started → 继续使用 Sisyphus 执行
        reject / abort → 抛出异常，任务终止
        revise → 当前版本等同 reject（Phase 5 再实现完整 revise 流程）
        """
        plan_text = self._plan_text or ""
        hitl_policy = self.request.hitl_policy
        timeout_sec = hitl_policy.decision_timeout_sec if hitl_policy else 600
        on_timeout_action, default_timeout_choice = self._resolve_hitl_timeout_policy()

        decision_id = str(uuid.uuid4())

        # 发出 plan_ready 事件
        await insert_event(
            self.db, self.task_id, TaskEventKind.plan_ready,
            {"plan_text": plan_text, "decision_id": decision_id},
        )

        # 创建 HITL 审批请求
        decision_req = DecisionRequest(
            decision_id=decision_id,
            kind=DecisionKind.plan_approval,
            summary="Prometheus 已生成执行计划，请审批后继续。",
            options=[
                DecisionChoice.approve,
                DecisionChoice.reject,
                DecisionChoice.revise,
                DecisionChoice.abort,
            ],
            default_on_timeout=default_timeout_choice,
            expires_at=None,
            context={"plan_text": plan_text},
        )
        await insert_decision(self.db, self.task_id, decision_req)
        await insert_event(
            self.db, self.task_id, TaskEventKind.hitl_required,
            {
                "decision_id": decision_id,
                "kind": DecisionKind.plan_approval.value,
                "summary": decision_req.summary,
            },
        )
        await update_task_status(self.db, self.task_id, TaskStatus.awaiting_human)

        # 轮询等待决策（P1-11：观测 HITL 等待耗时）
        _hitl_t0 = time.monotonic()
        resolved = await self._wait_for_decision(decision_id, timeout_sec)
        metrics.observe_hitl_wait(time.monotonic() - _hitl_t0)

        choice_val: Optional[str] = None
        if resolved is not None and resolved.response is not None:
            choice_val = resolved.response.choice.value
        else:
            # 超时：标记 DB decision 为 timed_out，发 hitl_timeout 事件
            await expire_decision(self.db, decision_id)
            choice_val = default_timeout_choice.value
            await insert_event(
                self.db, self.task_id, TaskEventKind.hitl_timeout,
                {
                    "decision_id": decision_id,
                    "kind": DecisionKind.plan_approval.value,
                    "on_timeout": on_timeout_action,
                    "resolved_choice": choice_val,
                },
            )
            logger.warning(
                "task %s: plan approval HITL timeout (decision_id=%s, on_timeout=%s, resolved_choice=%s)",
                self.task_id,
                decision_id,
                on_timeout_action,
                choice_val,
            )

        decision_payload = {"decision_id": decision_id, "choice": choice_val}
        await insert_event(
            self.db, self.task_id, TaskEventKind.decision_received,
            decision_payload,
        )
        # W2-1：plan_approval 决策合成事件分发给拦截器
        await self._dispatch_to_interceptors(
            normalized_kind=TaskEventKind.decision_received.value,
            normalized_payload=decision_payload,
            raw_type="<synthesized:decision_received>",
        )

        if choice_val == DecisionChoice.approve.value:
            # 计划批准 → 切换到 executing，继续用 Sisyphus 执行
            await update_task_status(self.db, self.task_id, TaskStatus.executing)
            await insert_event(
                self.db, self.task_id, TaskEventKind.execution_started,
                {"mode": TaskMode.plan_first.value, "plan_approved": True},
            )

            profile = self.request.opencode_profile
            model = profile.model if profile else None

            # 重置 assistant buffer，在同一 session 继续监听 SSE
            self._assistant_buffer = []
            self._plan_text = None

            sse_task = asyncio.create_task(
                self._consume_sse(session_id),
                name=f"sse-exec-{self.task_id[:8]}",
            )
            try:
                await self.client.prompt_async(
                    session_id=session_id,
                    parts=[{"type": "text", "text": "/start-work"}],
                    agent=AGENT_SISYPHUS,
                    model=model,
                )
                await sse_task
            except Exception:
                sse_task.cancel()
                try:
                    await sse_task
                except (asyncio.CancelledError, Exception):
                    pass
                raise

        else:
            # reject / revise / abort → 终止任务
            self._signal_abort("plan_rejected", decision_id)
            raise TaskAbortedError(
                reason="plan_rejected",
                message=f"plan rejected/aborted by user (choice={choice_val!r})",
                decision_id=decision_id,
            )

    # ── 产物收集 ──────────────────────────────────────────────────────────────

    async def _collect_artifacts(self, session_id: str) -> None:
        """收集 diff 和 transcript 产物，写文件并注册到 DB。"""
        import time as _time

        settings = self.settings
        artifacts_dir = settings.artifacts_dir / self.task_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        now = _time.time()
        retention_secs = settings.artifact_retention_days * 86400

        # 1. Diff artifact
        try:
            diff_data = await self.client.get_diff(session_id)
            if not diff_data:
                diff_data = self._last_diff  # 回退到 SSE 中缓存的最后 diff
            if diff_data:
                diff_path = artifacts_dir / "changes.diff.json"
                diff_path.write_text(
                    json.dumps(diff_data, indent=2, ensure_ascii=False)
                )
                artifact_id = str(uuid.uuid4())
                artifact = Artifact(
                    artifact_id=artifact_id,
                    task_id=self.task_id,
                    type=ArtifactType.diff,
                    filename="changes.diff.json",
                    size=diff_path.stat().st_size,
                    created_at=now,
                    expires_at=now + retention_secs,
                )
                await insert_artifact(
                    self.db, artifact, file_path=str(diff_path)
                )
                await insert_event(
                    self.db, self.task_id, TaskEventKind.artifact_ready,
                    {
                        "artifact_id": artifact_id,
                        "type": ArtifactType.diff.value,
                        "filename": "changes.diff.json",
                    },
                )
                logger.info(
                    "task %s: diff artifact saved (%d bytes)",
                    self.task_id, diff_path.stat().st_size,
                )
        except Exception as exc:
            logger.warning("task %s: diff artifact failed: %s", self.task_id, exc)

        # 2. Transcript artifact
        try:
            messages = await self.client.get_messages(session_id)
            if messages:
                transcript_path = artifacts_dir / "transcript.json"
                transcript_path.write_text(
                    json.dumps(messages, indent=2, ensure_ascii=False)
                )
                artifact_id = str(uuid.uuid4())
                artifact = Artifact(
                    artifact_id=artifact_id,
                    task_id=self.task_id,
                    type=ArtifactType.transcript,
                    filename="transcript.json",
                    size=transcript_path.stat().st_size,
                    created_at=now,
                    expires_at=now + retention_secs,
                )
                await insert_artifact(
                    self.db, artifact, file_path=str(transcript_path)
                )
                await insert_event(
                    self.db, self.task_id, TaskEventKind.artifact_ready,
                    {
                        "artifact_id": artifact_id,
                        "type": ArtifactType.transcript.value,
                        "filename": "transcript.json",
                    },
                )
        except Exception as exc:
            logger.warning("task %s: transcript artifact failed: %s", self.task_id, exc)

    # ── 辅助方法 ──────────────────────────────────────────────────────────────

    async def _inject_messages(self, session_id: str) -> None:
        """将 TaskRequest.messages 注入 session（noReply=True，仅写上下文）。"""
        for msg in self.request.messages:
            if msg.role == "user":
                await self.client.send_message(
                    session_id=session_id,
                    parts=[{"type": "text", "text": msg.content}],
                    no_reply=True,
                )

    def _build_prompt_parts(self) -> list[dict[str, Any]]:
        """构建 prompt_async 的 parts 参数。

        取 TaskRequest.messages 中最后一条 user 消息作为 prompt 触发内容；
        若 messages 为空，使用 "Begin task." 作为默认触发。
        """
        if self.request.messages:
            last_user = next(
                (m for m in reversed(self.request.messages) if m.role == "user"),
                None,
            )
            if last_user:
                return [{"type": "text", "text": last_user.content}]
        return [{"type": "text", "text": "Begin task."}]

    def _signal_abort(self, reason: str, decision_id: Optional[str] = None) -> None:
        """记录 abort 来源并触发 _abort_event。

        在多次触发时保留首个 reason（首次决定终态原因），后续仅刷新 decision_id 不变。
        """
        if not self._abort_event.is_set():
            self._abort_reason = reason
            self._abort_decision_id = decision_id
        self._abort_event.set()

    # ── W2-1: 拦截器分发辅助 ──────────────────────────────────────────────────

    async def _dispatch_to_interceptors(
        self,
        normalized_kind: Optional[str],
        normalized_payload: Optional[dict[str, Any]],
        raw_type: str,
        raw_payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """构造 InterceptorEvent 并分发给 runner。

        decision/permission 路径合成事件时，raw_type 以 ``<synthesized:...>`` 前缀
        标记，便于拦截器按需过滤。"""
        if not self._runner.interceptors:
            return
        ic_event = InterceptorEvent(
            task_id=self.task_id,
            session_id=self.session_id,
            normalized_kind=normalized_kind,
            normalized_payload=normalized_payload,
            raw_type=raw_type,
            raw_payload=raw_payload or {},
            received_at=time.monotonic(),
        )
        await self._runner.dispatch_event(ic_event)

    async def _dispatch_terminal_and_flush(
        self, status: TaskStatus, reason: Optional[str],
    ) -> None:
        """终态分发 + flush 收集 + 标准登记产物。

        三段都包在 try/except 中：拦截器侧任何故障都不传染到 driver 主路径。
        """
        if not self._runner.interceptors:
            return
        signal = TerminalSignal(
            task_id=self.task_id,
            session_id=self.session_id,
            status=status.value,
            reason=reason,
            ended_at=time.monotonic(),
        )
        try:
            await self._runner.dispatch_terminal(signal)
        except Exception:  # noqa: BLE001 — runner 内部已隔离；这里再做一层防御
            logger.exception("task %s: terminal dispatch outer failure", self.task_id)
        try:
            artifacts = await self._runner.collect_artifacts()
        except Exception:  # noqa: BLE001
            logger.exception("task %s: collect_artifacts outer failure", self.task_id)
            return
        for art in artifacts:
            try:
                await self._register_interceptor_artifact(art)
            except Exception:
                logger.exception(
                    "task %s: register interceptor artifact failed (%s)",
                    self.task_id, art.filename,
                )

    async def _register_interceptor_artifact(
        self, art: InterceptorArtifact,
    ) -> None:
        """走标准 insert_artifact + artifact_ready 路径登记拦截器产物。

        校验：
            1. local_path 必须落在 settings.artifacts_dir / task_id 子树内
               （复用 P0-8 同款防越权约束）
            2. 文件必须存在（否则 stat 抛 → 上层 catch 后跳过）
            3. ArtifactType 必须合法
        """
        artifacts_root = (self.settings.artifacts_dir / self.task_id).resolve()
        target = Path(art.local_path).resolve()
        try:
            target.relative_to(artifacts_root)
        except ValueError:
            logger.error(
                "task %s: interceptor artifact rejected (path escape): %s outside %s",
                self.task_id, target, artifacts_root,
            )
            return
        if not target.exists():
            logger.error(
                "task %s: interceptor artifact rejected (file missing): %s",
                self.task_id, target,
            )
            return
        try:
            artifact_type = ArtifactType(art.artifact_type)
        except ValueError:
            logger.error(
                "task %s: interceptor artifact rejected (unknown type): %r",
                self.task_id, art.artifact_type,
            )
            return

        size = art.size_bytes if art.size_bytes is not None else target.stat().st_size
        now = time.time()
        retention_secs = self.settings.artifact_retention_days * 86400
        artifact_id = str(uuid.uuid4())
        artifact = Artifact(
            artifact_id=artifact_id,
            task_id=self.task_id,
            type=artifact_type,
            filename=art.filename,
            size=size,
            created_at=now,
            expires_at=now + retention_secs,
            metadata=dict(art.metadata),
        )
        await insert_artifact(self.db, artifact, file_path=str(target))
        await insert_event(
            self.db, self.task_id, TaskEventKind.artifact_ready,
            {
                "artifact_id": artifact_id,
                "type": artifact_type.value,
                "filename": art.filename,
                "metadata": dict(art.metadata),
            },
        )
        logger.info(
            "task %s: interceptor artifact registered (%s, %d bytes)",
            self.task_id, art.filename, size,
        )

    # ── P1-14: HITL auto_approve ────────────────────────────────────────────

    def _match_auto_approve(
        self, kind: DecisionKind, context_key: str,
    ) -> Optional[str]:
        """检查 (kind, context_key) 是否命中 hitl_policy.auto_approve。

        匹配格式：`<DecisionKind>:<context_key>`，支持 fnmatch 通配符
        （`?` / `*`）。例如：
            - "tool_permission:read_file"  → 仅 tool=read_file
            - "tool_permission:*"          → 任意 tool_permission
            - "tool_permission:read*"      → tool 以 read 开头

        Returns:
            首个命中的 pattern 字符串；无匹配返回 None。
        """
        policy = self.request.hitl_policy
        if not policy or not policy.auto_approve:
            return None
        target = f"{kind.value}:{context_key}"
        for pattern in policy.auto_approve:
            if fnmatch.fnmatch(target, pattern):
                return pattern
        return None

    async def _auto_approve_permission(
        self,
        session_id: str,
        permission_id: str,
        tool: str,
        matched_pattern: str,
    ) -> None:
        """auto_approve 短路：直接回 opencode "once" + 写 decision_received。

        P1-14：跳过 hitl_required / awaiting_human / decision DB 流程。
        审计可通过 decision_received 事件的 `auto_approved=True` +
        `matched_pattern` 字段重建路径。
        """
        decision_id = str(uuid.uuid4())
        logger.info(
            "task %s: auto-approved permission %s (tool=%s, pattern=%s)",
            self.task_id, permission_id, tool, matched_pattern,
        )
        decision_payload = {
            "decision_id": decision_id,
            "choice": "once",
            "permission_id": permission_id,
            "auto_approved": True,
            "matched_pattern": matched_pattern,
            "tool": tool,
        }
        await insert_event(
            self.db, self.task_id, TaskEventKind.decision_received,
            decision_payload,
        )
        # W2-1：合成 decision_received 事件分发给拦截器
        await self._dispatch_to_interceptors(
            normalized_kind=TaskEventKind.decision_received.value,
            normalized_payload=decision_payload,
            raw_type="<synthesized:decision_received>",
        )
        # auto-approve 等价于用户 approve，重置 reject 计数
        self._reject_count = 0
        try:
            await self.client.respond_permission(session_id, permission_id, "once")
        except Exception as exc:
            logger.warning(
                "task %s: auto-approve respond_permission %s failed: %s",
                self.task_id, permission_id, exc,
            )

    def _resolve_hitl_timeout_policy(self) -> tuple[str, DecisionChoice]:
        """将 HitlPolicy.on_timeout 解析为规范动作及超时默认决策。

        当前 Worker 没有独立的外部 escalation 通道，因此：
            - abort     -> 超时即中止
            - continue  -> 超时视为 approve，继续任务
            - escalate  -> 先发 hitl_timeout 事件通知上游，再按 approve 继续

        返回：
            (normalized_action, fallback_choice)
        """
        raw_action = self.request.hitl_policy.on_timeout if self.request.hitl_policy else "abort"
        action = str(raw_action or "abort").lower()
        if action == "abort":
            return action, DecisionChoice.abort
        if action in {"continue", "escalate"}:
            return action, DecisionChoice.approve

        logger.warning(
            "task %s: unsupported hitl on_timeout=%r, fallback to abort",
            self.task_id,
            raw_action,
        )
        return "abort", DecisionChoice.abort

    async def _wait_for_decision(
        self,
        decision_id: str,
        timeout_sec: float,
    ) -> Optional[Any]:
        """轮询 DB，直到指定 decision 被 resolved 或超时。

        Returns:
            PendingDecision（含 response）若在 timeout 内收到决策；
            None 若超时。
        """
        deadline = asyncio.get_event_loop().time() + timeout_sec
        while True:
            resolved = await get_resolved_decision(self.db, decision_id)
            if resolved is not None:
                return resolved
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return None
            await asyncio.sleep(_HITL_POLL_INTERVAL)
