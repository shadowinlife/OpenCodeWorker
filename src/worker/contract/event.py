"""
TaskEvent 契约 Schema。

本模块定义了 Worker 向外部推送的实时事件结构。所有事件都通过
GET /tasks/:id/events (SSE) 端点流式推送，同时持久化到 SQLite
的 task_events 表（作为事件溯源的 append-only 日志）。

SSE 断线重连机制：
    客户端重连时携带 Last-Event-ID 请求头，值为上次收到的 cursor（event_id）。
    SSE 端点从 DB 读取 event_id > last_event_id 的历史事件并补发，
    再继续实时推送，确保客户端不丢事件。

事件流终止条件：
    客户端应在收到 TERMINAL_EVENT_KINDS 中的任意事件后主动关闭 SSE 连接。
    Worker 也会在写出终态事件后停止推送并关闭 SSE 流。
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, model_validator


class TaskEventKind(str, Enum):
    """任务事件类型枚举，覆盖完整的任务生命周期。

    事件按生命周期大致顺序排列，以下是各类型的触发时机：

    基础生命周期事件：
        task_created:    TaskRequest 持久化到 DB 后立即发出
        task_queued:     任务进入 asyncio 队列
        task_started:    Worker 开始处理（获得 semaphore slot）
        container_started: Docker 容器启动成功，container_id 已知
        opencode_ready:  opencode serve 健康检查通过（/global/health 200）

    LLM 交互事件（由 opencode SSE /global/event 转换而来）：
        assistant_delta:    LLM 流式输出片段（payload.content 为增量文本）
        tool_call_started:  LLM 调用工具前（payload.tool, payload.args）
        tool_call_finished: 工具执行完成（payload.result, payload.exit_code）

    计划阶段事件（plan_first 模式）：
        plan_ready:    LLM 完成执行计划生成（payload.plan_text 为计划全文）

    HITL 事件：
        hitl_required:     需要人工决策，payload 包含 decision_id 和 summary
        decision_received: 人工通过 API 提交了决策（payload.choice）

    执行阶段：
        execution_started: 进入 executing 阶段（计划审批后触发）

    产物事件：
        artifact_ready: 有新产物可下载（payload.artifact_id, payload.type）

    终止事件（TERMINAL_EVENT_KINDS 成员）：
        task_completed:  任务成功结束
        task_failed:     任务异常失败（payload.error 包含 WorkerError 序列化）
        task_aborted:    任务被主动中止（payload.reason 区分 user_requested /
                         hitl_timeout / plan_rejected / permission_rejected）
        task_timed_out:  超过 resource_limits.timeout_sec 未完成
                         （payload.timeout_sec 给出当时的超时阈值）

    建议性事件：
        mode_escalation_suggested: direct_execute 模式中 LLM 检测到任务超出
                                   预期复杂度，建议调用方改用 plan_first 模式重提交

    保活事件：
        heartbeat: SSE 保活包，每 sse_heartbeat_sec 秒发送一次，
                   payload 为空，客户端可直接忽略
    """
    task_created = "task_created"
    task_queued = "task_queued"
    task_started = "task_started"
    container_started = "container_started"
    opencode_ready = "opencode_ready"
    assistant_delta = "assistant_delta"
    tool_call_started = "tool_call_started"
    tool_call_finished = "tool_call_finished"
    plan_ready = "plan_ready"
    hitl_required = "hitl_required"
    hitl_timeout = "hitl_timeout"
    decision_received = "decision_received"
    execution_started = "execution_started"
    artifact_ready = "artifact_ready"
    task_completed = "task_completed"
    task_failed = "task_failed"
    task_aborted = "task_aborted"
    task_timed_out = "task_timed_out"
    mode_escalation_suggested = "mode_escalation_suggested"
    heartbeat = "heartbeat"


# 终态事件集合：客户端收到后应关闭 SSE 连接，不再等待后续事件
TERMINAL_EVENT_KINDS: frozenset[TaskEventKind] = frozenset({
    TaskEventKind.task_completed,
    TaskEventKind.task_failed,
    TaskEventKind.task_aborted,
    TaskEventKind.task_timed_out,
})


class TaskEvent(BaseModel):
    """单条任务事件，是 Worker 与外部系统交互的核心信息载体。

    事件以 SSE 格式推送，格式为：
        id: <cursor>\n
        event: <kind>\n
        data: <json_payload>\n\n

    SSE Last-Event-ID 断线重连流程：
        1. 客户端断开时记录最后一条事件的 cursor 值
        2. 重连时在请求头中携带 Last-Event-ID: <cursor>
        3. SSE 端点查询 DB 中 event_id > cursor 的事件并补发
        4. 补发完毕后切换为实时推送模式

    Attributes:
        event_id: 任务维度的单调递增序号（从 1 开始），
                  由 storage.repo._next_event_id() 在写入时分配，
                  保证同一 task_id 内的顺序唯一性。
        task_id:  所属任务的 UUID。
        ts:       事件产生的 Unix 时间戳（秒），默认为写入时的当前时间。
        kind:     事件类型，见 TaskEventKind。
        payload:  事件携带的结构化数据，不同 kind 有不同的 payload schema
                  （当前阶段用 dict 承载，后续可细化为 Union 类型）。
        cursor:   SSE id 字段的值，等于 event_id，供客户端 Last-Event-ID 使用。
    """
    # 任务内单调递增序号，由 storage 层分配，不可由外部传入覆盖
    event_id: int
    task_id: str
    # 默认 0.0，由 _fill_defaults 在模型验证后填充为当前时间
    ts: float = 0.0
    kind: TaskEventKind
    payload: dict[str, Any] = {}
    # cursor == event_id，独立字段方便序列化层直接映射到 SSE id
    cursor: int = 0

    @model_validator(mode="after")
    def _fill_defaults(self) -> "TaskEvent":
        """验证后自动填充 ts 和 cursor 的默认值。

        ts 为 0.0 时说明创建时未指定，填入当前时间戳。
        cursor 为 0 时（默认值）同步为 event_id，避免序列化时出现 0。
        """
        if self.ts == 0.0:
            self.ts = time.time()
        if self.cursor == 0:
            self.cursor = self.event_id
        return self
