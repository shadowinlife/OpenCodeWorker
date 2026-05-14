"""
OpenCode HTTP Client — 封装 opencode serve 的 HTTP/SSE API。

所有方法都以 httpx.AsyncClient 为底层，对外暴露类型化接口。
连接参数（host/port/password）在实例化时固定，适合每任务创建一个实例。

认证：
    opencode 使用 HTTP Basic Auth，用户名固定为 "opencode"，
    密码来自 OPENCODE_SERVER_PASSWORD（容器 env 注入）。

错误处理：
    4xx/5xx HTTP 错误会抛出 httpx.HTTPStatusError。
    SSE 流断开会抛出 httpx.RemoteProtocolError 或 StopAsyncIteration。
    调用方（driver.py）负责重试和错误映射。
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Optional

import httpx

logger = logging.getLogger(__name__)


class OpenCodeClient:
    """与容器内 opencode serve 交互的 async HTTP 客户端。"""

    def __init__(self, host: str, port: int, password: str, timeout: float = 30.0):
        """
        Args:
            host:     opencode 监听地址（宿主侧，通常 127.0.0.1）
            port:     宿主侧映射端口（docker -p host_port:4096）
            password: OPENCODE_SERVER_PASSWORD 值
            timeout:  普通请求超时秒数（SSE stream 的 read timeout 单独设为 None）
        """
        self._base_url = f"http://{host}:{port}"
        self._auth = httpx.BasicAuth(username="opencode", password=password)
        self._timeout = timeout
        # 共享连接池（同一任务期间复用）
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            auth=self._auth,
            timeout=httpx.Timeout(timeout, read=None),
        )

    async def aclose(self) -> None:
        """关闭底层 HTTP 连接池。任务结束后调用。"""
        await self._client.aclose()

    async def __aenter__(self) -> "OpenCodeClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    # ── Session ──────────────────────────────────────────────────────────────

    async def create_session(self) -> dict[str, Any]:
        """POST /session — 创建新的 opencode 会话。

        Returns:
            opencode 返回的 session 对象（含 id 字段）。
        """
        resp = await self._client.post("/session", json={})
        resp.raise_for_status()
        return resp.json()

    async def get_session(self, session_id: str) -> dict[str, Any]:
        """GET /session/:id — 获取会话信息。"""
        resp = await self._client.get(f"/session/{session_id}")
        resp.raise_for_status()
        return resp.json()

    async def delete_session(self, session_id: str) -> None:
        """DELETE /session/:id — 删除会话（任务结束时清理）。"""
        try:
            resp = await self._client.delete(f"/session/{session_id}")
            resp.raise_for_status()
        except Exception as exc:
            logger.debug("delete_session %s: %s", session_id, exc)

    # ── Messages ─────────────────────────────────────────────────────────────

    async def send_message(
        self,
        session_id: str,
        parts: list[dict[str, Any]],
        *,
        no_reply: bool = True,
    ) -> dict[str, Any]:
        """POST /session/:id/message — 写入 user message。

        noReply=True 时只写消息，不触发 LLM 回复（用于注入历史上下文）。

        Args:
            session_id: 目标会话 ID
            parts:      消息内容，如 [{"type":"text","text":"..."}]
            no_reply:   True=只写消息不触发回复
        """
        body: dict[str, Any] = {"parts": parts}
        if no_reply:
            body["noReply"] = True
        resp = await self._client.post(f"/session/{session_id}/message", json=body)
        resp.raise_for_status()
        return resp.json()

    async def prompt_async(
        self,
        session_id: str,
        parts: list[dict[str, Any]],
        *,
        agent: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        """POST /session/:id/prompt_async — 异步触发 LLM 执行。

        成功时立即返回 204（No Content），实际结果通过 SSE /global/event 读取。

        Args:
            session_id: 目标会话 ID
            parts:      提示内容，如 [{"type":"text","text":"..."}]
            agent:      oh-my-openagent agent 名称（如 "Prometheus" / "Sisyphus"）
            model:      显式指定 model（覆盖 OPENCODE_CONFIG_CONTENT 中的设置）
        """
        body: dict[str, Any] = {"parts": parts}
        if agent:
            body["agent"] = agent
        if model:
            body["model"] = model
        resp = await self._client.post(
            f"/session/{session_id}/prompt_async",
            json=body,
            timeout=self._timeout,
        )
        resp.raise_for_status()

    # ── Abort ─────────────────────────────────────────────────────────────────

    async def abort_session(self, session_id: str) -> None:
        """POST /session/:id/abort — 中止正在运行的会话。"""
        try:
            resp = await self._client.post(
                f"/session/{session_id}/abort",
                json={},
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("abort_session %s: %s", session_id, exc)

    # ── Permissions ───────────────────────────────────────────────────────────

    async def respond_permission(
        self,
        session_id: str,
        permission_id: str,
        response: str,
    ) -> None:
        """POST /session/:id/permissions/:permissionID — 回应 opencode 权限请求。

        Args:
            session_id:    会话 ID
            permission_id: 权限请求 ID（以 "per" 开头，来自 opencode SSE 事件）
            response:      "once" | "always" | "reject"

        Raises:
            ValueError: response 不合法时（避免向 opencode 发出 400）
        """
        valid = {"once", "always", "reject"}
        if response not in valid:
            raise ValueError(
                f"Invalid permission response: {response!r}, must be one of {valid}"
            )
        resp = await self._client.post(
            f"/session/{session_id}/permissions/{permission_id}",
            json={"response": response},
            timeout=self._timeout,
        )
        resp.raise_for_status()

    # ── Artifacts ─────────────────────────────────────────────────────────────

    async def get_diff(self, session_id: str) -> list[dict[str, Any]]:
        """GET /session/:id/diff — 获取会话工作区 diff。

        Returns:
            diff 列表（无变更时返回 []）。
        """
        resp = await self._client.get(
            f"/session/{session_id}/diff",
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = resp.json()
        if isinstance(result, list):
            return result
        return []

    async def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        """GET /session/:id/message — 获取会话所有消息（用于 transcript 产物）。"""
        resp = await self._client.get(f"/session/{session_id}/message")
        resp.raise_for_status()
        result = resp.json()
        return result if isinstance(result, list) else []

    # ── SSE Event Stream ──────────────────────────────────────────────────────

    async def stream_events(self) -> AsyncIterator[dict[str, Any]]:
        """GET /global/event — 订阅 opencode 全局事件流（SSE）。

        生成器持续产出已解析的事件 dict，直到连接断开或调用方 break。

        opencode 1.14.30 的 SSE 格式（§1.3 Spike 实测）：
            data: { "type": "...", "payload": {...} }
            （无 id: 字段，无 event: 字段）

        Yields:
            解析后的事件 dict（至少含 "type" 字段）。
        """
        async with self._client.stream("GET", "/global/event") as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[len("data:"):].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    event = json.loads(raw)
                    yield event
                except json.JSONDecodeError as exc:
                    logger.debug("SSE JSON decode error: %s | raw=%r", exc, raw)
                    continue
