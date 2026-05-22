"""Bearer 认证头构造工具。

SDK 把鉴权头作为最简单的 helper 抽出来，避免在 client 内部散落字符串拼接。
"""
from __future__ import annotations


def bearer_headers(token: str) -> dict[str, str]:
    """构造 JSON 请求的标准头部（Authorization + Content-Type + Accept）。"""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def bearer_sse_headers(token: str, *, last_event_id: int | None = None) -> dict[str, str]:
    """构造 SSE 订阅请求的头部。

    如指定 ``last_event_id``，会自动注入 ``Last-Event-ID`` 头供服务端补发历
    史事件（与 worker.api.routes.task_events 实现保持一致）。
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache",
    }
    if last_event_id is not None:
        headers["Last-Event-ID"] = str(last_event_id)
    return headers
