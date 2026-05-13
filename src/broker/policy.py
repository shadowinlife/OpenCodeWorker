"""
Broker 出站策略管理：维护每个任务的域名白名单。

设计说明：
    每个任务在 POST /tasks 创建后，Worker Orchestrator 会调用
    set_task_policy() 注册该任务允许访问的出站域名列表
    （来自 TaskRequest.broker_policy.allow_egress_hosts）。

    Broker Proxy 在代理每个 HTTP 请求时查询 is_allowed()，
    若目标主机不在白名单则返回 403。

    进程重启后策略从内存中清除；已结束的任务应调用 remove_task_policy()
    释放内存。默认策略：空白名单（禁止所有出站）。

数据结构：
    {task_id: frozenset(hostname)}

线程安全：Python GIL 保证 dict 操作的原子性，asyncio 单线程不需要额外锁。
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# 进程单例：策略存储
# ──────────────────────────────────────────────────────────────────────────────

_policies: dict[str, frozenset[str]] = {}


# ──────────────────────────────────────────────────────────────────────────────
# 公开接口
# ──────────────────────────────────────────────────────────────────────────────

def set_task_policy(task_id: str, allow_egress_hosts: list[str]) -> None:
    """注册任务出站白名单。

    白名单元素格式：纯域名或带端口的 host:port（均小写）。
    例如：["api.openai.com", "pypi.org:443"]

    注意：不支持通配符（* / *.example.com），防止白名单被绕过。
    """
    normalized = frozenset(h.lower().strip() for h in allow_egress_hosts if h.strip())
    _policies[task_id] = normalized
    logger.info(
        "broker policy set for task %s: %d hosts allowed: %s",
        task_id,
        len(normalized),
        sorted(normalized),
    )


def remove_task_policy(task_id: str) -> None:
    """任务结束后清理策略（释放内存）。"""
    if task_id in _policies:
        del _policies[task_id]
        logger.debug("broker policy removed for task %s", task_id)


def is_allowed(task_id: Optional[str], host: str, port: Optional[int] = None) -> bool:
    """检查 task_id 对应任务是否允许访问 host[:port]。

    匹配规则（按顺序）：
        1. 白名单包含 "host:port"（精确带端口匹配）
        2. 白名单包含 "host"（纯域名匹配，允许任意端口）

    若 task_id 为 None 或不在策略表中，默认拒绝。
    """
    if task_id is None:
        return False
    policy = _policies.get(task_id)
    if policy is None:
        return False

    h = host.lower().strip()
    if port is not None:
        if f"{h}:{port}" in policy:
            return True
    return h in policy


def get_task_policy(task_id: str) -> list[str]:
    """返回任务白名单（调试/测试用途）。"""
    policy = _policies.get(task_id)
    if policy is None:
        return []
    return sorted(policy)


def list_active_policies() -> dict[str, list[str]]:
    """返回所有活跃任务的策略（调试/运维用途）。"""
    return {tid: sorted(hosts) for tid, hosts in _policies.items()}
