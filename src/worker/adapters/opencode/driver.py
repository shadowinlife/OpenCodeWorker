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
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from worker.adapters.opencode.client import OpenCodeClient
from worker.adapters.opencode.event_stream import (
    extract_diff,
    extract_permission_request,
    is_session_idle,
    normalize_opencode_event,
)
from worker.config import get_settings
from worker.contract.artifact import Artifact, ArtifactType
from worker.contract.decision import DecisionChoice, DecisionKind, DecisionRequest
from worker.contract.event import TaskEventKind
from worker.contract.task import TaskMode, TaskRequest, TaskStatus
from worker.storage.repo import (
    expire_decision,
    get_resolved_decision,
    insert_artifact,
    insert_decision,
    insert_event,
    update_task_status,
)

logger = logging.getLogger(__name__)

# Agent 路由对齐 ADR-001 / ADR-006 / oh-my-openagent 3.17.2：
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


class OpenCodeDriver:
    """驱动容器内 opencode 完成一个任务的完整生命周期。"""

    def __init__(
        self,
        task_id: str,
        request: TaskRequest,
        host_port: int,
        container_env: dict[str, str],
        db: aiosqlite.Connection,
    ):
        self.task_id = task_id
        self.request = request
        self.host_port = host_port
        self.password = container_env.get("OPENCODE_SERVER_PASSWORD", "")
        self.db = db
        self.settings = get_settings()

        # 状态
        self.client: Optional[OpenCodeClient] = None
        self.session_id: Optional[str] = None

        # 中止信号：外部 abort 或超时时设置，SSE 消费循环检测后退出
        self._abort_event = asyncio.Event()

        # 累积 assistant 文本（plan_first 时用于提取 plan）
        self._assistant_buffer: list[str] = []
        # plan_first 模式下提取到的计划全文
        self._plan_text: Optional[str] = None
        # 最近一次 session.diff 内容（artifact 收集时使用）
        self._last_diff: list = []

        # 超时时间
        self.timeout_sec = _DEFAULT_TIMEOUT_SEC
        if request.resource_limits and request.resource_limits.timeout_sec:
            self.timeout_sec = request.resource_limits.timeout_sec

    # ── 公开入口 ──────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """执行完整 Phase 3 驱动逻辑，由 orchestrator._drive_opencode 调用。

        超时时抛出 RuntimeError；内部任何未处理异常直接向上冒泡，
        由 orchestrator 的 except 块写入 task_failed 事件。
        """
        from worker.observability.logging import set_correlation, clear_correlation
        set_correlation(task_id=self.task_id)

        client = OpenCodeClient(
            host="127.0.0.1",
            port=self.host_port,
            password=self.password,
        )
        self.client = client
        try:
            async with asyncio.timeout(self.timeout_sec):
                await self._run_inner()
        except asyncio.TimeoutError:
            logger.warning("task %s: timed out after %ds", self.task_id, self.timeout_sec)
            if self.session_id:
                await client.abort_session(self.session_id)
            raise RuntimeError(f"task timed out after {self.timeout_sec}s")
        finally:
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

        # abort_event 由权限超时或 plan 拒绝设置，抛出让 orchestrator 写 aborted
        if self._abort_event.is_set():
            raise RuntimeError("task aborted (HITL decision or permission timeout)")

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

        hitl_policy = self.request.hitl_policy
        timeout_sec = hitl_policy.decision_timeout_sec if hitl_policy else 600

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
            default_on_timeout=DecisionChoice.abort,
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

        # 轮询等待决策
        resolved = await self._wait_for_decision(decision_id, timeout_sec)

        # 映射 choice → opencode response
        opencode_response = "reject"  # 保守默认
        if resolved is not None and resolved.response is not None:
            choice = resolved.response.choice
            if choice == DecisionChoice.approve:
                opencode_response = "once"
            elif choice == DecisionChoice.abort:
                self._abort_event.set()
                opencode_response = "reject"
            else:
                opencode_response = "reject"
        else:
            # 超时：标记 DB decision 为 timed_out，发 hitl_timeout 事件，按策略处理
            await expire_decision(self.db, decision_id)
            on_timeout = hitl_policy.on_timeout if hitl_policy else "abort"
            await insert_event(
                self.db, self.task_id, TaskEventKind.hitl_timeout,
                {
                    "decision_id": decision_id,
                    "kind": DecisionKind.tool_permission.value,
                    "permission_id": permission_id,
                    "on_timeout": on_timeout,
                },
            )
            if on_timeout == "abort":
                self._abort_event.set()
            logger.warning(
                "task %s: permission HITL timeout (decision_id=%s)",
                self.task_id, decision_id,
            )

        # 回传 opencode
        try:
            await self.client.respond_permission(session_id, permission_id, opencode_response)
            await insert_event(
                self.db, self.task_id, TaskEventKind.decision_received,
                {
                    "decision_id": decision_id,
                    "choice": opencode_response,
                    "permission_id": permission_id,
                },
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
            default_on_timeout=DecisionChoice.abort,
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

        # 轮询等待决策
        resolved = await self._wait_for_decision(decision_id, timeout_sec)

        choice_val: Optional[str] = None
        if resolved is not None and resolved.response is not None:
            choice_val = resolved.response.choice.value
        else:
            # 超时：标记 DB decision 为 timed_out，发 hitl_timeout 事件
            await expire_decision(self.db, decision_id)
            choice_val = (hitl_policy.on_timeout if hitl_policy else "abort")
            await insert_event(
                self.db, self.task_id, TaskEventKind.hitl_timeout,
                {
                    "decision_id": decision_id,
                    "kind": DecisionKind.plan_approval.value,
                    "on_timeout": choice_val,
                },
            )

        await insert_event(
            self.db, self.task_id, TaskEventKind.decision_received,
            {"decision_id": decision_id, "choice": choice_val},
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
            raise RuntimeError(
                f"plan rejected/aborted by user (choice={choice_val!r})"
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
