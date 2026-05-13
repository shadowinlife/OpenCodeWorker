"""
Worker 错误契约 Schema。

Worker API 在所有 4xx/5xx 响应中统一使用 WorkerError 作为错误体：
    {"kind": "...", "message": "...", "retryable": false, ...}

同时，task_failed 事件的 payload 中也会内嵌 WorkerError 的序列化，
方便 SSE 订阅方直接解析错误原因而无需额外轮询。

错误处理建议：
    - retryable=True  → 调用方可在适当退避后重试相同请求
    - requires_hitl=True → 需要人工介入（如 secrets 未配置、quota 超限）
    - counts_against_quota=False → 平台侧不扣费（系统内部错误）
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel


class ErrorKind(str, Enum):
    """Worker 错误类型枚举，对应 HTTP 状态码和处理策略。

    请求层错误（对应 4xx）：
        invalid_request:  请求体验证失败（字段缺失、类型错误），→ 400
        unauthorized:     Bearer token 无效或缺失，→ 401
        not_found:        task_id / artifact_id 不存在，→ 404
        quota_exceeded:   并发任务数达到 max_concurrent_tasks 上限，→ 429

    任务执行错误（写入 task_failed 事件，HTTP 202 已返回）：
        workspace_prepare_failed:
            tarball 下载失败、git clone 超时、磁盘空间不足等。
            retryable=True（临时性资源问题）

        sandbox_start_failed:
            docker run 失败（镜像不存在、内存不足、cgroup 限制）。
            retryable=True（通常是资源抢占问题）

        opencode_start_failed:
            容器内 opencode serve 进程未能在超时时间内通过健康检查。
            可能原因：opencode 二进制不存在、ANTHROPIC_API_KEY 未配置、
            oh-my-openagent 插件加载失败。retryable=False（需排查配置）

        opencode_failed:
            opencode 进程运行中异常退出或返回错误。
            payload 中包含 exit_code 和最后若干行 stderr。

        broker_denied:
            请求的出口连接被 BrokerPolicy 防火墙规则拒绝（Phase 3）

        hitl_timeout:
            HITL 决策等待超时，按 HitlPolicy.on_timeout 策略处理。
            requires_hitl=True

        task_cancelled:
            调用方通过 POST /tasks/:id/abort 主动取消。
            counts_against_quota=False

        resource_exhausted:
            任务执行时间超过 resource_limits.timeout_sec，强制终止。

        artifact_too_large:
            workspace_snapshot 超过配置的单文件大小上限，跳过归档。

    内部错误（对应 5xx）：
        internal_error: Worker 进程内部未预期的异常，→ 500
    """
    invalid_request = "invalid_request"
    unauthorized = "unauthorized"
    not_found = "not_found"
    quota_exceeded = "quota_exceeded"
    workspace_prepare_failed = "workspace_prepare_failed"
    sandbox_start_failed = "sandbox_start_failed"
    opencode_start_failed = "opencode_start_failed"
    opencode_failed = "opencode_failed"
    broker_denied = "broker_denied"
    hitl_timeout = "hitl_timeout"
    task_cancelled = "task_cancelled"
    resource_exhausted = "resource_exhausted"
    artifact_too_large = "artifact_too_large"
    internal_error = "internal_error"


class WorkerError(BaseModel):
    """统一错误响应体，用于 HTTP 错误响应和 task_failed 事件 payload。

    Attributes:
        kind:                  错误类型，见 ErrorKind
        message:               内部技术消息（英文，用于日志和 metrics label）
        retryable:             True 表示相同请求可在退避后重试
        requires_hitl:         True 表示需要人工介入才能解决（不要自动重试）
        counts_against_quota:  False 表示平台侧不应为此次失败扣费/计数
        task_id:               关联的任务 UUID（HTTP 错误响应中可能为 None）
        user_visible_message:  面向用户展示的友好消息（中文可），
                               None 时 UI 可降级展示 message 字段
        detail:                附加调试信息字典，仅在日志和内部工具中使用，
                               不应暴露给最终用户（防止信息泄漏）
    """
    kind: ErrorKind
    message: str
    retryable: bool = False
    requires_hitl: bool = False
    counts_against_quota: bool = True
    task_id: Optional[str] = None
    user_visible_message: Optional[str] = None
    # 注意：detail 含有内部路径/堆栈信息，API 层应在非调试模式下过滤此字段
    detail: Optional[dict[str, Any]] = None
