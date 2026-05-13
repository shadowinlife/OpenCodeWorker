"""
HITL（Human-In-The-Loop）决策相关的契约 Schema。

什么是 HITL 决策流程：
    opencode 在执行过程中遇到被标记为 "ask" 的操作（如 bash 命令、文件写入）
    时，会通过 Worker 的权限回调接口暂停并等待人工审批。整个流程如下：

    1. opencode 向 Worker 发起权限请求
       (POST /session/:id/permissions/:permId，由 Orchestrator 拦截)
    2. Worker 将请求包装为 DecisionRequest，写入 DB，发出 hitl_required 事件
    3. 外部系统（UI / Broker）收到 SSE 事件后，向用户展示决策界面
    4. 用户提交 DecisionResponse（POST /tasks/:id/decisions）
    5. Worker 将选择通过 opencode 权限回调接口回传，任务继续

    超时处理：若 decision_timeout_sec 内未收到响应，按 HitlPolicy.on_timeout
    策略处理（默认 abort）。

    幂等性：DecisionResponse.idempotency_key 用于防止用户多次点击导致
    重复提交。相同 idempotency_key 的二次提交会被静默忽略（INSERT OR IGNORE）。
"""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class DecisionKind(str, Enum):
    """需要人工决策的操作类型。

    每种 kind 对应一类需要审批的场景，UI 层可根据 kind 渲染不同的展示样式：

    plan_approval:
        plan_first 模式下，LLM 生成执行计划后请求人工审批。
        这是最常见的 HITL 入口，用户可选择 approve / reject / revise。
        payload 中包含完整的计划文本（plan_text）。

    tool_permission:
        opencode 尝试调用被标记为 ask 的工具（如 bash 命令执行）。
        payload 中包含工具名（tool）和参数（args），方便用户评估风险。

    file_write:
        写文件操作的专项权限申请。payload 包含目标路径和内容摘要。
        从 tool_permission 中单独拆出，是因为文件写入有不可逆性，
        需要更明确的展示和更谨慎的默认策略。

    broker_egress:
        容器尝试访问不在 allow_egress_hosts 白名单中的外部地址时触发。
        Phase 3 网络隔离实现后启用。

    continue_long_task:
        任务执行时间超过预设阈值（如 resource_limits.timeout_sec 的 80%），
        询问人工是否继续等待。防止长任务无限阻塞 worker slot。

    custom:
        保留给 MCP 服务器或 Broker 自定义的决策类型。
    """
    plan_approval = "plan_approval"
    tool_permission = "tool_permission"
    file_write = "file_write"
    broker_egress = "broker_egress"
    continue_long_task = "continue_long_task"
    custom = "custom"


class DecisionChoice(str, Enum):
    """人工决策的可选操作。

    并非所有 choice 在每种 DecisionKind 下都有效，
    DecisionRequest.options 字段会列出当前决策允许的选项子集。

    approve: 批准操作，任务继续执行
    reject:  拒绝操作，任务继续但跳过该步骤（opencode 会收到 reject 反馈）
    revise:  要求修订计划（仅 plan_approval 有效），附带 feedback 文本
    abort:   立即中止整个任务，状态变为 aborted
    """
    approve = "approve"
    reject = "reject"
    revise = "revise"
    abort = "abort"


class DecisionRequest(BaseModel):
    """Worker 发出的 HITL 决策请求，存入 DB 并通过 SSE hitl_required 事件推送。

    调用方 / UI 从 hitl_required 事件的 payload 中提取 decision_id，
    然后通过 GET /tasks/:id（查看 pending decision）或直接从事件 payload
    获取完整的 DecisionRequest 信息，向用户展示审批界面。

    Attributes:
        decision_id:        唯一标识本次决策请求的 UUID
        kind:               决策类型，见 DecisionKind
        summary:            面向用户的一句话摘要（用于通知推送）
        options:            本次决策允许提交的 choice 子集
        default_on_timeout: 超时时自动使用的默认选择（通常为 abort）
        expires_at:         ISO-8601 格式的过期时间，由 Worker 根据
                            HitlPolicy.decision_timeout_sec 计算填入
        context:            面向用户展示的上下文细节，不同 kind 有不同结构：
                            plan_approval → {"plan_text": str}
                            tool_permission → {"tool": str, "args": dict}
                            file_write → {"path": str, "diff_summary": str}
    """
    # 由 Worker 在创建时生成，外部系统提交 response 时须引用此 ID
    decision_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: DecisionKind
    summary: str
    options: list[DecisionChoice]
    default_on_timeout: DecisionChoice = DecisionChoice.abort
    # ISO-8601 字符串，例如 "2026-05-13T12:00:00Z"
    expires_at: Optional[str] = None
    context: dict[str, Any] = Field(default_factory=dict)


class DecisionResponse(BaseModel):
    """外部系统通过 POST /tasks/:id/decisions 提交的人工决策。

    Attributes:
        decision_id:      与 DecisionRequest.decision_id 对应，用于路由
        choice:           人工选择，必须在 DecisionRequest.options 列表内
        feedback:         可选的文字说明（revise 时必须填写修订意见）
        patch:            可选的结构化修订内容（如替换计划中的特定步骤），
                          格式由具体 DecisionKind 定义
        idempotency_key:  防重提交 key，建议设为调用方的请求 UUID；
                          相同 key 的重复提交会被幂等忽略
    """
    decision_id: str
    choice: DecisionChoice
    feedback: Optional[str] = None
    patch: Optional[dict[str, Any]] = None
    idempotency_key: Optional[str] = None


class PendingDecision(BaseModel):
    """数据库 / API 视图层的决策记录，表示一条待处理或已解决的决策。

    此结构用于 GET /tasks/:id 响应中内嵌当前待决策信息，
    以及 decisions 表的内存映射。

    Attributes:
        decision_id:  决策 UUID
        task_id:      所属任务 UUID
        kind:         决策类型
        status:       pending（等待中）| resolved（已处理）| timed_out（已超时）
        request:      原始请求对象
        response:     已提交的响应（status=resolved 时不为 None）
        created_at:   决策请求创建时间
        resolved_at:  决策被解决的时间（超时也算解决）
    """
    decision_id: str
    task_id: str
    kind: DecisionKind
    # pending | resolved | timed_out
    status: str
    request: DecisionRequest
    response: Optional[DecisionResponse] = None
    created_at: float
    resolved_at: Optional[float] = None
