"""SSE 解析与自动重连（design §9）。

为什么 SDK 必须把 SSE 重连做成内建能力：

1. Worker 端使用 ``sse-starlette`` 实现 SSE，事件格式为 ``id: <cursor>\\nevent:
   <kind>\\ndata: <json>``，其中 ``id`` 是任务内单调递增的事件序号。
2. 服务端支持通过 ``Last-Event-ID`` 请求头补发历史事件，这一机制不应该让
   每个上游 runtime 重新实现一遍。
3. terminal event 一旦推出，服务端会主动关闭连接；上游既需要捕获这个信号
   退出迭代，又需要把"网络中断 / 服务端正常关闭"两种情况区分开。

本模块只做：

- 从 ``httpx.AsyncByteStream`` 解析 SSE 事件
- 跟踪最近一次 ``cursor``，断线后用它重新请求
- 在收到终态事件后让迭代器自然结束
- 重连次数耗尽抛 ``WorkerSSEError``

不做：无限重连、心跳超时推断任务失败、本地持久化 cursor、多订阅者共享
event bus（这些都被显式排除在薄 SDK 边界之外）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import httpx
from httpx_sse import EventSource, ServerSentEvent

from worker_sdk.errors import WorkerSSEError, WorkerTransportError
from worker_sdk.models import WorkerEvent

logger = logging.getLogger(__name__)


# 与 worker.contract.event.TERMINAL_EVENT_KINDS 对齐；这里写成字符串集合以避
# 免把服务端 enum 拖到 SDK 公开依赖里（决策 C4）。
TERMINAL_EVENT_KINDS: frozenset[str] = frozenset(
    {"task_completed", "task_failed", "task_aborted", "task_timed_out"}
)

_HEARTBEAT_KIND = "heartbeat"


SseConnectFactory = Callable[[int | None], AbstractAsyncContextManager["_SseStream"]]
"""``connect(last_event_id) -> AsyncContextManager[_SseStream]`` 的回调形态。

client 拼好 URL + 头之后把"建立一次 SSE 流"的能力以函数形式注入到
``stream_events_with_reconnect``，让重连逻辑保持纯粹（不直接依赖
httpx.AsyncClient 状态）。
"""


class _SseStream:
    """一次 SSE 连接的最小包装。

    封装 ``httpx.Response`` 与 ``httpx_sse.EventSource``，给重连循环提供：

    - ``aiter_events()`` —— 迭代解析后的 ``ServerSentEvent``
    - ``aclose()``        —— 释放底层连接（``response.aclose()``）

    复用 ``httpx_sse.EventSource`` 而不是手写解析，是因为 httpx_sse 已经处理
    了 CR/LF 边界、multi-line ``data:`` 拼接、注释行（``: comment``）等容易
    写错的细节。
    """

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self._source = EventSource(response)

    def aiter_events(self) -> AsyncIterator[ServerSentEvent]:
        return self._source.aiter_sse()

    async def aclose(self) -> None:
        # httpx.Response.aclose 幂等，重复调用安全
        await self._response.aclose()


@asynccontextmanager
async def open_sse_stream(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    timeout: httpx.Timeout | float | None,
) -> AsyncIterator[_SseStream]:
    """打开一次 SSE 连接的 async context manager。

    用 ``httpx.AsyncClient.stream(...)`` 拿到原始 ``Response``，再包成
    ``_SseStream``。非 2xx 状态码在此抛出 ``WorkerSSEError``，让重连循环上层
    决定是否重试（默认不重试 HTTP error，只重试 transport error）。
    """
    try:
        async with client.stream("GET", url, headers=headers, timeout=timeout) as response:
            if response.status_code != 200:
                # 读出 body 便于排查；超过 1KB 截断以免污染日志
                body = await response.aread()
                snippet = body[:1024].decode("utf-8", errors="replace")
                raise WorkerSSEError(
                    f"SSE handshake failed: status={response.status_code} body={snippet!r}"
                )
            yield _SseStream(response)
    except httpx.HTTPError as exc:
        # 把 httpx 的网络错误统一上抛为 WorkerTransportError，外层据此重连
        raise WorkerTransportError(f"SSE transport error: {exc!r}") from exc


def _parse_sse_event(sse: ServerSentEvent) -> WorkerEvent | None:
    """把 ``httpx_sse.ServerSentEvent`` 解析为 SDK 的 ``WorkerEvent``。

    服务端约定：

    - ``id``    —— 任务内单调递增的 ``cursor``（字符串形式的整数）
    - ``event`` —— 事件 ``kind``（如 ``task_started``、``hitl_required``）
    - ``data``  —— ``payload`` 的 JSON 字符串

    解析不出 ``id`` 的事件会被跳过——这通常是 ``: keep-alive`` 之类的注释行
    或心跳事件，没有写入 DB 也没有 cursor。
    """
    if sse.id is None or sse.id == "":
        # heartbeat 也会带 event 字段但没有 id（DB 里不存在对应行）
        return None
    try:
        cursor = int(sse.id)
    except ValueError:
        logger.warning("SSE event with non-integer id, skipping: id=%r", sse.id)
        return None

    kind = sse.event or "message"
    payload: dict[str, Any]
    if sse.data:
        try:
            decoded = json.loads(sse.data)
        except json.JSONDecodeError:
            logger.warning("SSE event with non-JSON data, payload set to raw: kind=%s", kind)
            decoded = {"_raw": sse.data}
        # 服务端约定 payload 是 object；其它情况包一层避免类型不一致
        payload = decoded if isinstance(decoded, dict) else {"_value": decoded}
    else:
        payload = {}

    return WorkerEvent(cursor=cursor, kind=kind, payload=payload)


async def stream_events_with_reconnect(
    connect: SseConnectFactory,
    *,
    initial_last_event_id: int | None,
    include_heartbeats: bool,
    auto_reconnect: bool,
    max_reconnect_attempts: int,
    reconnect_backoff_sec: float = 0.5,
) -> AsyncIterator[WorkerEvent]:
    """带断线重连的 SSE 事件迭代器（design §9.2 算法实现）。

    Args:
        connect:                 ``async (last_event_id) -> _SseStream`` 工厂。
        initial_last_event_id:   首连时携带的 ``Last-Event-ID``；首次订阅传 ``None``。
        include_heartbeats:      是否把 ``heartbeat`` 事件 yield 给调用方；默认丢弃。
        auto_reconnect:          关闭时遇到 transport error 直接抛错。
        max_reconnect_attempts:  达到上限仍未连上抛 ``WorkerSSEError``。
        reconnect_backoff_sec:   重连之间的固定退避；保持简单不引入抖动/指数退避。

    Yields:
        ``WorkerEvent``：已按 cursor 顺序解析、过滤了非业务事件后的对象。

    Raises:
        WorkerSSEError:        重连次数耗尽或 SSE 握手失败。
        WorkerTransportError:  ``auto_reconnect=False`` 时透传给调用方。
    """
    last_cursor: int | None = initial_last_event_id
    attempts = 0
    seen_terminal = False

    while True:
        try:
            async with connect(last_cursor) as stream:
                async for sse in stream.aiter_events():
                    event = _parse_sse_event(sse)
                    if event is None:
                        # heartbeat / 无 id 的注释行
                        continue
                    last_cursor = event.cursor
                    if event.kind == _HEARTBEAT_KIND and not include_heartbeats:
                        continue
                    yield event
                    if event.kind in TERMINAL_EVENT_KINDS:
                        seen_terminal = True
                        break
            # connect block 正常退出：要么收到终态，要么服务端主动关流
            if seen_terminal:
                return
            # 若没有看到终态就退出，按"服务端主动关闭/EOF"处理：算作一次
            # 连接断开，进入重连分支。重连后服务端会从 last_cursor 继续补发。
            if not auto_reconnect:
                return
            # 落入 transport-error 分支共享退避/计数逻辑
            raise WorkerTransportError("SSE stream closed before terminal event")
        except WorkerTransportError as exc:
            if not auto_reconnect:
                raise
            attempts += 1
            if attempts > max_reconnect_attempts:
                raise WorkerSSEError(
                    f"SSE reconnect attempts exhausted ({max_reconnect_attempts}): {exc}"
                ) from exc
            logger.info(
                "SSE reconnect attempt %d/%d last_cursor=%s reason=%s",
                attempts,
                max_reconnect_attempts,
                last_cursor,
                exc,
            )
            await asyncio.sleep(reconnect_backoff_sec)
            continue
