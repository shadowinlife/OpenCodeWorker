"""
Task 相关的核心契约 Schema（TaskRequest / TaskResponse / TaskStatus）。

本模块定义了 Worker HTTP API 的请求/响应数据结构，是 Caller（外部系统、
Broker、CLI 工具）与 Worker 之间的 **唯一稳定接口层**。所有字段的语义
一旦进入 v1，须保持向后兼容；变更须走 ADR 评审流程。

任务生命周期状态机：
    pending
      → queued                   # 进入 asyncio 任务队列
      → preparing_workspace      # 解压 tarball / clone git / 创建空目录
      → starting_container       # docker run，设置 cgroup / seccomp
      → starting_opencode        # opencode serve 进程就绪（/global/health 200）
      → planning                 # plan_first 模式：LLM 生成执行计划
      → awaiting_human           # 等待 HITL 决策（plan 审批 / 权限请求）
      → revising                 # 收到 revise 决策，重新生成计划
      → executing                # 工具调用执行阶段
      → collecting_artifacts     # 压缩工作区快照，写出产物元数据
      → completed / failed / aborted / timed_out  # 终态
"""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class TaskMode(str, Enum):
    """任务执行模式。

    plan_first:
        两阶段执行——先让 LLM 生成完整执行计划（plan），等待人工审批后
        再进入 executing 阶段。适合高风险、长链路的代码修改任务。
        对应的 opencode 权限模板会把 bash/write 等危险操作全部设为 ask。

    direct_execute:
        跳过计划阶段，直接进入 executing。适合低风险、确定性强的短任务
        （如读取文件、查询数据、运行单测）。需要调用方自行评估风险。
        注意：即使在 direct_execute 模式下，opencode 仍可能因权限不足
        触发 HITL 中断（例如意外碰到 write 操作），此时状态机会跳入
        awaiting_human 而非直接失败。
    """
    plan_first = "plan_first"
    direct_execute = "direct_execute"


class TaskStatus(str, Enum):
    """任务状态机的完整状态集合。

    状态流转由 Orchestrator 驱动，每次转换都会写入 task_events 表
    并通过 SSE 推送给订阅方。终态（completed/failed/aborted/timed_out）
    写入后不可再变更。

    各状态含义：
        pending:               已收到请求，尚未入队（容量满时会在此阻塞）
        queued:                已入 asyncio 队列，等待 worker slot 释放
        preparing_workspace:   正在准备沙箱工作目录
        starting_container:    正在启动 Docker 沙箱容器
        starting_opencode:     容器已起，等待 opencode serve 健康检查通过
        planning:              LLM 正在生成执行计划（plan_first 模式）
        awaiting_human:        等待人工 HITL 决策，计时器已启动
        revising:              人工要求修订计划，LLM 重新生成中
        executing:             工具调用执行阶段（bash/write/read 等）
        collecting_artifacts:  任务正文完成，正在归档产物
        completed:             成功终态
        failed:                失败终态（opencode 内部错误 / 工具异常）
        aborted:               被人工或系统主动中止
        timed_out:             超过 resource_limits.timeout_sec 未完成
    """
    pending = "pending"
    queued = "queued"
    preparing_workspace = "preparing_workspace"
    starting_container = "starting_container"
    starting_opencode = "starting_opencode"
    planning = "planning"
    awaiting_human = "awaiting_human"
    revising = "revising"
    executing = "executing"
    collecting_artifacts = "collecting_artifacts"
    completed = "completed"
    failed = "failed"
    aborted = "aborted"
    timed_out = "timed_out"


# 终态集合——进入这些状态后任务不再流转，可安全关闭 SSE 连接
TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.completed,
    TaskStatus.failed,
    TaskStatus.aborted,
    TaskStatus.timed_out,
})


class Message(BaseModel):
    """单条对话消息，与 OpenAI Chat Completion 格式对齐。

    role 取值：
        user      — 用户输入
        assistant — 历史对话中 LLM 的回复（用于多轮上下文注入）
        system    — 系统提示（通常由 opencode_profile 内置，不建议外部覆盖）
    """
    role: str
    content: str


class GitSpec(BaseModel):
    """Git 仓库工作区规格。

    Worker 会在沙箱容器内执行 `git clone --depth 1 <url>` 并 checkout 到
    指定 sha。出于安全考虑，只允许 Worker 配置中白名单内的 git host。

    Attributes:
        url:     克隆 URL，支持 https / ssh（需提前在容器中配置 SSH key）
        sha:     目标提交的完整 40 位 SHA，禁止使用分支名（不确定性过高）
        subpath: 仅挂载仓库子目录作为工作区（monorepo 场景），None 表示根目录
    """
    url: str
    sha: str
    subpath: Optional[str] = None


class WorkspaceSpec(BaseModel):
    """沙箱工作区初始化规格。

    支持三种初始化方式，由 kind 字段区分：

    empty:
        创建一个空目录。适合从零开始的代码生成任务。

    tarball:
        从 URL 下载 .tar.gz 并解压，或直接内联 base64 编码的 tarball。
        - tarball_url: Worker 在容器网络内可访问的下载地址
        - tarball_inline_b64: 小型工作区可直接嵌入请求体（解码后 ≤ 50 MB）
        两者互斥，优先使用 tarball_url。

    git:
        克隆指定 Git 仓库到工作区，见 GitSpec。

    注意：工作区目录在任务结束后会被压缩为产物（workspace_snapshot），
    保留 artifact_retention_days 天后自动清理。
    """
    # 初始化方式：empty | tarball | git
    kind: str = "empty"
    # tarball 方式：远程 URL（优先）
    tarball_url: Optional[str] = None
    # tarball 方式：内联 base64，解码后不超过 50 MB
    tarball_inline_b64: Optional[str] = None
    # git 方式
    git: Optional[GitSpec] = None


class PermissionTemplate(str, Enum):
    """opencode 权限模板，决定容器内各工具操作是否需要人工审批。

    plan_first_default:
        bash/write/file_delete 等破坏性操作全部设为 ask（触发 HITL）。
        适合 plan_first 模式——计划审批通过后再执行，高安全感。

    direct_execute_default:
        read/list 类操作自动放行，bash/write 仍设为 ask。
        适合 direct_execute 模式的受控场景。

    custom:
        完全由 permission_overrides 字典控制，适合高级用户。
        键为 opencode 工具名，值为 "allow" | "ask" | "deny"。
    """
    plan_first_default = "plan_first_default"
    direct_execute_default = "direct_execute_default"
    custom = "custom"


class OpencodeProfile(BaseModel):
    """传递给沙箱内 opencode serve 进程的配置参数。

    这些参数会被序列化为 OPENCODE_CONFIG_CONTENT 环境变量注入容器，
    opencode 在启动时读取并应用。不支持运行时热更新。

    Attributes:
        model:
            使用的 LLM 模型，格式为 "provider/model-id"，例如：
            - "anthropic/claude-opus-4-5"（默认，高能力）
            - "anthropic/claude-haiku-3-5"（低延迟低成本）
            Worker 不验证模型名，非法值会导致 opencode 启动失败。

        permission_template:
            选择权限预设模板，见 PermissionTemplate。

        permission_overrides:
            在模板基础上的细粒度覆盖，格式：
            {"bash": "allow", "write_file": "deny"}
            键名与 opencode 内部工具名完全一致（区分大小写）。
    """
    model: str = "anthropic/claude-opus-4-5"
    permission_template: PermissionTemplate = PermissionTemplate.plan_first_default
    permission_overrides: dict[str, Any] = Field(default_factory=dict)


class EnvPolicy(BaseModel):
    """控制沙箱容器的环境变量注入策略。

    出于安全考虑，Worker 不允许调用方直接传入任意 key=value 的 secrets，
    所有 provider key（如 API key）只能通过 provider_keys 白名单从宿主机
    环境变量透传，Worker 侧会做存在性检查。

    Attributes:
        provider_keys:
            需要从宿主机透传到容器的环境变量名列表，例如：
            ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]
            Worker 会验证这些变量在宿主机上已设置，否则拒绝启动任务。

        extra_env:
            非 secrets 类的辅助环境变量，可直接设值，例如：
            {"TASK_DEBUG": "1", "PYTHONPATH": "/workspace/src"}
            禁止通过此字段传入包含 KEY/SECRET/TOKEN 等敏感词的变量。
    """
    provider_keys: list[str] = Field(default_factory=list)
    extra_env: dict[str, str] = Field(default_factory=dict)


class ResourceLimits(BaseModel):
    """沙箱容器的资源配额，对应 Docker --cpus / --memory / --pids-limit。

    Colima 开发环境（2 CPU / ~2 GB RAM）下建议保持默认值，
    生产环境可适当调大 memory 和 timeout_sec。

    Attributes:
        cpu:         CPU 核数上限，传给 docker run --cpus，字符串格式（如 "1.5"）
        memory:      内存上限，支持 Docker 内存字符串格式（如 "2Gi", "512m"）
        pids:        最大进程数，防止 fork bomb，对应 --pids-limit
        timeout_sec: 任务总超时秒数（含 planning + executing），超出后强制 timed_out
    """
    cpu: str = "2"
    memory: str = "4Gi"
    pids: int = 512
    # 默认 30 分钟；复杂重构任务可设到 3600 秒（1小时）
    timeout_sec: int = 1800


class HitlPolicy(BaseModel):
    """人机交互（HITL, Human-In-The-Loop）超时与自动审批策略。

    每当 opencode 发出需要人工决策的权限请求（bash/write/plan 审批等），
    Worker 会挂起当前任务并等待调用方通过 POST /tasks/:id/decisions 接口
    提交决策。HitlPolicy 控制等待行为。

    Attributes:
        decision_timeout_sec:
            单次 HITL 等待的最长时间（秒）。默认 600 秒（10 分钟）。
            超时后按 on_timeout 策略处理。

        on_timeout:
            超时后的处理策略：
            - abort:     主动停止任务（最安全，默认）
            - continue:  以 default_on_timeout 决策自动继续（谨慎使用）
            - escalate:  通知外部系统（通过 broker_policy.mcp_servers 配置）

        auto_approve:
            无需人工审批、自动批准的决策类型列表，例如：
            ["tool_permission:read_file", "tool_permission:list_directory"]
            格式："<DecisionKind>:<context_key>"，留空表示全部需要人工确认。
    """
    decision_timeout_sec: int = 600
    # abort | continue | escalate
    on_timeout: str = "abort"
    auto_approve: list[str] = Field(default_factory=list)


class BrokerPolicy(BaseModel):
    """控制沙箱容器的出口网络访问和 MCP 服务器配置。

    沙箱容器默认处于隔离网络（docker 内部网络），仅能访问
    Worker 进程（宿主机）。如需访问外部服务，须在 allow_egress_hosts
    白名单中声明，Worker 会在容器内通过 iptables/nftables 规则放行。

    Attributes:
        allow_egress_hosts:
            允许容器访问的外部主机列表（域名或 IP），例如：
            ["api.github.com", "pypi.org"]
            不在列表中的出口连接会被防火墙拒绝。
            注意：Phase 1 暂未实现，留作 Phase 3 网络隔离功能的占位。

        mcp_servers:
            注入给 opencode 的 MCP（Model Context Protocol）服务器配置列表。
            每个元素对应 opencode config 中的一个 mcp server 条目。
            通过 MCP 可以给 LLM 提供额外工具（如数据库查询、API 调用）。
            Phase 1 暂未实现，配置会被 Orchestrator 忽略。
    """
    allow_egress_hosts: list[str] = Field(default_factory=list)
    mcp_servers: list[Any] = Field(default_factory=list)


class TaskMetadata(BaseModel):
    """任务附加元数据，用于链路追踪和租户路由，不影响执行逻辑。

    Attributes:
        trace_id:     分布式追踪 ID，若调用方传入则透传到所有日志和事件中；
                      若为 None，Worker 会在入口处自动生成一个 UUID。
        tenant_hint:  租户标识符，用于多租户监控看板的数据分组，不影响隔离。
        extra:        任意键值扩展字段，由调用方自定义，Worker 不作任何处理
                      （原样存入 DB，原样出现在任务查询响应中）。
    """
    trace_id: Optional[str] = None
    tenant_hint: Optional[str] = None
    extra: dict[str, Any] = Field(default_factory=dict)


class TaskRequest(BaseModel):
    """POST /tasks 的请求体，描述一次完整的 AI 编程任务。

    调用方构造 TaskRequest 并通过 Bearer token 认证后提交给 Worker。
    Worker 会持久化请求至 SQLite，异步调度执行，并通过 SSE 实时推送进度。

    最小合法请求示例：
    {
        "mode": "plan_first",
        "messages": [{"role": "user", "content": "给 add 函数写单测"}]
    }

    Attributes:
        task_id:          调用方可指定 UUID 用于幂等重提交；若省略则自动生成。
        mode:             执行模式，见 TaskMode。
        messages:         对话消息列表，至少包含一条 role=user 的消息。
        workspace:        工作区初始化规格，默认为 empty（空目录）。
        opencode_profile: LLM 模型和权限配置。
        env_policy:       API key 透传和辅助环境变量。
        resource_limits:  CPU / 内存 / 超时配额。
        hitl_policy:      HITL 超时与自动审批策略。
        broker_policy:    出口网络白名单和 MCP 服务器（Phase 3 实现）。
        metadata:         链路追踪元数据。
    """
    # 调用方可指定 task_id 实现幂等重提交，省略则自动生成 UUID v4
    task_id: Optional[str] = Field(default_factory=lambda: str(uuid.uuid4()))
    mode: TaskMode
    messages: list[Message]
    workspace: WorkspaceSpec = Field(default_factory=WorkspaceSpec)
    opencode_profile: OpencodeProfile = Field(default_factory=OpencodeProfile)
    env_policy: EnvPolicy = Field(default_factory=EnvPolicy)
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)
    hitl_policy: HitlPolicy = Field(default_factory=HitlPolicy)
    broker_policy: BrokerPolicy = Field(default_factory=BrokerPolicy)
    metadata: TaskMetadata = Field(default_factory=TaskMetadata)


class TaskResponse(BaseModel):
    """GET /tasks/:id 及 POST /tasks 的响应体，表示任务的当前快照。

    Attributes:
        task_id:              任务 UUID
        status:               当前状态，见 TaskStatus
        mode:                 执行模式（与请求一致）
        created_at:           任务创建时间（Unix 时间戳，秒）
        updated_at:           最后状态变更时间（Unix 时间戳，秒）
        completed_at:         进入终态的时间；未终结时为 None
        container_id:         Docker 容器 ID（starting_container 之后可用）
        opencode_session_id:  opencode 创建的 session UUID
                              （starting_opencode 成功后由 Orchestrator 填入）
    """
    task_id: str
    status: TaskStatus
    mode: TaskMode
    created_at: float
    updated_at: float
    completed_at: Optional[float] = None
    container_id: Optional[str] = None
    opencode_session_id: Optional[str] = None
