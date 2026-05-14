"""
结构化日志工具：correlation context filter

将当前 asyncio Task 中的 task_id / session_id / decision_id 自动注入到
每条日志记录的 extra 字段，方便日志聚合系统（Loki、CloudWatch Insights 等）
按任务/会话/决策过滤日志。

用法（在 driver 或 orchestrator 中）：

    from worker.observability.logging import set_correlation, clear_correlation

    set_correlation(task_id="t-abc", session_id="ses-xyz")
    logger.info("session created")   # → LogRecord.task_id="t-abc", session_id="ses-xyz"
    clear_correlation()

注意：correlation 存储在 asyncio.Task 的 context（contextvars.ContextVar），
因此并发任务之间互相隔离，无需加锁。

在 main.py 的 lifespan 中调用 configure_logging() 启用结构化 JSON 日志（可选）。
"""
from __future__ import annotations

import logging
import contextvars
from typing import Optional

# ── ContextVar ───────────────────────────────────────────────────────────────

_task_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "task_id", default=None
)
_session_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "session_id", default=None
)
_decision_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "decision_id", default=None
)


def set_correlation(
    task_id: Optional[str] = None,
    session_id: Optional[str] = None,
    decision_id: Optional[str] = None,
) -> None:
    """设置当前 async context 的 correlation 字段（对其他并发 task 无影响）。"""
    if task_id is not None:
        _task_id_var.set(task_id)
    if session_id is not None:
        _session_id_var.set(session_id)
    if decision_id is not None:
        _decision_id_var.set(decision_id)


def clear_correlation() -> None:
    """清除当前 async context 的 correlation 字段。"""
    _task_id_var.set(None)
    _session_id_var.set(None)
    _decision_id_var.set(None)


# ── Logging Filter ───────────────────────────────────────────────────────────

class CorrelationFilter(logging.Filter):
    """日志 Filter：将 ContextVar 中的 correlation 字段注入 LogRecord。

    如果某字段未设置，注入空字符串（方便格式化模板始终引用这些字段）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.task_id = _task_id_var.get() or ""
        record.session_id = _session_id_var.get() or ""
        record.decision_id = _decision_id_var.get() or ""
        return True


# ── 配置入口 ─────────────────────────────────────────────────────────────────

def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    """配置根 logger：注入 CorrelationFilter，可选 JSON 格式化。

    在 main.py lifespan 的 startup 阶段调用一次。

    Args:
        level:     日志级别字符串（DEBUG / INFO / WARNING / ERROR）
        json_logs: True 时输出 JSON 格式日志（适合 CloudWatch / Loki）；
                   False 时输出人类可读格式（本地开发）。
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    correlation_filter = CorrelationFilter()

    if json_logs:
        try:
            import json_log_formatter  # type: ignore

            formatter = json_log_formatter.JSONFormatter()
        except ImportError:
            # json_log_formatter 未安装时退回文本格式
            formatter = logging.Formatter(
                "%(asctime)s %(levelname)s [%(task_id)s][%(session_id)s] %(name)s: %(message)s"
            )
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s"
            " [task=%(task_id)s session=%(session_id)s decision=%(decision_id)s]"
            " %(name)s: %(message)s"
        )

    # 重新配置所有已有 handler（uvicorn 通常在 root logger 上添加了 StreamHandler）
    for handler in root.handlers:
        handler.setFormatter(formatter)
        handler.addFilter(correlation_filter)

    # 若尚无 handler（如直接运行脚本）则添加一个
    if not root.handlers:
        sh = logging.StreamHandler()
        sh.setFormatter(formatter)
        sh.addFilter(correlation_filter)
        root.addHandler(sh)
