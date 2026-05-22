"""Transient-failure retry policy for ``AsyncWorkerClient`` HTTP requests.

Scope and non-scope:

- 适用于普通 HTTP 请求（``_request`` / ``_raw_get``），**不适用于 SSE**——
  SSE 走 ``sse.stream_events_with_reconnect`` 自己维护 ``Last-Event-ID``
  断线重连，与本模块的指数退避是两个独立机制。
- 只对"可安全重试"的失败重试：
    * ``WorkerTransportError`` —— 网络层（连接被拒/超时/中断），无法判断
      请求是否到达服务端，但对幂等操作重试是安全的。
    * ``WorkerServerError`` (5xx) —— 服务端临时不可用。
- 4xx 永远不重试（401/404/409 都是确定性错误，再试一次只会得到同一结果）。
- 默认只对调用方标记为 idempotent 的请求生效；``AsyncWorkerClient`` 默认把
  GET 当作 idempotent，POST 默认 ``retry=False``——调用方在确定其请求幂等
  （例如自带 ``task_id`` 的 create_task、自带 ``idempotency_key`` 的
  submit_decision）后可显式 opt-in。

设计参考：
- AWS SDK retry guidance: full jitter
- httpx 文档：``Retry-After`` 头在 503 / 429 响应里常见，应优先于本地退避。
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    """配置 SDK 对瞬时错误的重试行为。

    Attributes:
        max_attempts:           包含首次在内的总尝试次数。``1`` 表示"不重试"。
        initial_backoff_sec:    首次重试前的基础等待秒数。
        max_backoff_sec:        单次等待的上限（指数退避会被夹在此值）。
        backoff_multiplier:     每次重试 backoff 的乘数（默认 2，即倍增）。
        jitter_ratio:           ``±ratio`` 范围内的随机抖动，0 表示无抖动。
                                例如 0.25 → 实际等待 ∈ [base*0.75, base*1.25]。
        retry_on_5xx:           是否把 ``WorkerServerError`` 视作可重试。
        retry_on_transport_error: 是否把 ``WorkerTransportError`` 视作可重试。
        respect_retry_after:    若响应带 ``Retry-After`` 头且大于本地等待时间，
                                优先采用服务端建议（仅 5xx 路径生效）。
    """

    max_attempts: int = 3
    initial_backoff_sec: float = 0.5
    max_backoff_sec: float = 8.0
    backoff_multiplier: float = 2.0
    jitter_ratio: float = 0.25
    retry_on_5xx: bool = True
    retry_on_transport_error: bool = True
    respect_retry_after: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.initial_backoff_sec < 0:
            raise ValueError("initial_backoff_sec must be >= 0")
        if self.max_backoff_sec < self.initial_backoff_sec:
            raise ValueError("max_backoff_sec must be >= initial_backoff_sec")
        if self.backoff_multiplier < 1:
            raise ValueError("backoff_multiplier must be >= 1")
        if not 0.0 <= self.jitter_ratio <= 1.0:
            raise ValueError("jitter_ratio must be within [0, 1]")

    @classmethod
    def disabled(cls) -> "RetryPolicy":
        """快捷构造一个完全禁用重试的策略。"""
        return cls(max_attempts=1)

    def backoff_for_attempt(self, attempt: int) -> float:
        """返回第 ``attempt`` 次失败后（即将进行第 ``attempt+1`` 次尝试前）的等待秒数。

        ``attempt`` 从 1 计数，即第一次失败后调用 ``backoff_for_attempt(1)``。
        """
        if attempt < 1:
            raise ValueError("attempt must be >= 1")
        base = self.initial_backoff_sec * (self.backoff_multiplier ** (attempt - 1))
        capped = min(base, self.max_backoff_sec)
        if self.jitter_ratio == 0:
            return capped
        # 在 [capped*(1-ratio), capped*(1+ratio)] 区间内均匀抖动
        lo = capped * (1.0 - self.jitter_ratio)
        hi = capped * (1.0 + self.jitter_ratio)
        return random.uniform(lo, hi)


_DEFAULT_POLICY = RetryPolicy()


def default_policy() -> RetryPolicy:
    """模块级默认 ``RetryPolicy`` 单例。"""
    return _DEFAULT_POLICY


async def sleep_for_backoff(seconds: float) -> None:
    """统一的 backoff sleep 入口，便于测试 monkeypatch。"""
    if seconds > 0:
        await asyncio.sleep(seconds)


def parse_retry_after(header_value: str | None) -> float | None:
    """解析 HTTP ``Retry-After`` 头（仅支持 delta-seconds 形式）。

    HTTP-date 形式（RFC 7231 §7.1.3）的 ``Retry-After`` 在生产 Worker 上极
    少见且解析复杂，SDK 选择忽略——回退到本地指数退避。
    """
    if not header_value:
        return None
    try:
        delta = float(header_value.strip())
    except ValueError:
        return None
    if delta < 0:
        return None
    return delta
