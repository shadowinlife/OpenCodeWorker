"""``AsyncWorkerClient`` —— SDK 唯一公开入口（design §5.2 / §7）。

调用方典型用法：

    async with AsyncWorkerClient(base_url=..., bearer_token=...) as client:
        handle = await client.create_task({...})
        result = await client.wait_until_terminal(handle.task_id, raise_on_failure=True)

设计 invariants（来自 design §2 / §3）：

1. SDK 不发明新的服务端能力，所有方法严格对应一个或多个 Worker HTTP 端点。
2. SDK 不内嵌 strategy / workflow DSL，只提供一个 ``create_and_wait``
   convenience helper。
3. SDK 不持久化任何状态；cursor / decision idempotency_key 都在调用栈内生成。
4. terminal 错误默认通过返回值传递，仅 ``raise_on_failure=True`` 时主动抛异常。
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import httpx

from worker_sdk.auth import bearer_headers, bearer_sse_headers
from worker_sdk.compat import is_compatible, supported_matrix_str
from worker_sdk.errors import (
    WorkerClientError,
    WorkerCompatibilityError,
    WorkerHTTPError,
    WorkerServerError,
    WorkerTransportError,
    http_error_for,
    terminal_error_for,
)
from worker_sdk.models import (
    WorkerArtifactRef,
    WorkerEvent,
    WorkerTaskHandle,
    WorkerTerminalResult,
)
from worker_sdk.retry import (
    RetryPolicy,
    default_policy,
    parse_retry_after,
    sleep_for_backoff,
)
from worker_sdk.sse import (
    TERMINAL_EVENT_KINDS,
    _SseStream,
    open_sse_stream,
    stream_events_with_reconnect,
)

logger = logging.getLogger(__name__)


# 仅允许文件名级别的 artifact_id —— 防御性检查，避免 ``../`` 拼接出穿越路径
_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


class AsyncWorkerClient:
    """异步 Worker 客户端。

    支持作为 async context manager 使用以便正确释放 httpx 连接池：

        async with AsyncWorkerClient(...) as client:
            ...

    也支持手动生命周期：``await client.aclose()``。

    所有方法（除 ``aclose``）按 design §7 的接口定义实现。
    """

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        timeout: float = 30.0,
        auto_reconnect_sse: bool = True,
        max_sse_reconnect_attempts: int = 5,
        compatibility_check: bool = True,
        retry_policy: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """构造一个 SDK 客户端。

        Args:
            base_url:                   Worker 根地址（不带尾斜杠），如
                                        ``http://worker.internal:8080``。
            bearer_token:               Worker 启用的 Bearer token；不为空。
            timeout:                    单次 HTTP 请求的总超时；SSE 不使用此值。
            auto_reconnect_sse:         SSE 断线时是否自动重连。
            max_sse_reconnect_attempts: 重连上限；超出后抛 ``WorkerSSEError``。
            compatibility_check:        首次请求前是否调用 ``/health`` 校验版本。
            retry_policy:               对 5xx / transport error 的重试策略；
                                        ``None`` 走 ``RetryPolicy()`` 默认值。
                                        默认只对调用方标记为 idempotent 的请求
                                        （GET 类）生效；POST 不自动重试。
                                        传入 ``RetryPolicy.disabled()`` 可全
                                        局关闭。SSE 重连与本策略独立。
            transport:                  可注入的 httpx Transport，主要用于测试
                                        （如 ``httpx.ASGITransport(app=...)``）。
        """
        if not base_url:
            raise ValueError("base_url is required")
        if not bearer_token:
            raise ValueError("bearer_token is required")

        self._base_url = base_url.rstrip("/")
        self._token = bearer_token
        self._timeout = timeout
        self._auto_reconnect_sse = auto_reconnect_sse
        self._max_sse_reconnect_attempts = max_sse_reconnect_attempts
        self._compat_check_enabled = compatibility_check
        self._compat_checked = False
        self._retry_policy = retry_policy if retry_policy is not None else default_policy()

        # 默认走 httpx 内置 transport；测试场景可以注入 ASGITransport 拿到内存
        # 内的 FastAPI app，避免起真实端口。
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
        )

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> "AsyncWorkerClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """关闭底层 httpx 连接池。多次调用安全。"""
        await self._http.aclose()

    # ------------------------------------------------------------------ #
    # Health / compatibility (design §7.2)                               #
    # ------------------------------------------------------------------ #

    async def get_health(self) -> dict[str, Any]:
        """直接读取 ``GET /health``，返回原始 JSON。

        ``/health`` 是公开端点，**不需要** Bearer token；为简化实现，SDK 还是
        统一带上鉴权头，服务端会忽略。
        """
        response = await self._raw_get("/health")
        return self._parse_json(response)

    async def assert_compatible(self) -> None:
        """检查 Worker 版本是否在 SDK 支持矩阵内，否则抛 ``WorkerCompatibilityError``。"""
        health = await self.get_health()
        version = str(health.get("version", ""))
        if not is_compatible(version):
            raise WorkerCompatibilityError(
                f"Worker version {version!r} is outside SDK support matrix "
                f"({supported_matrix_str()})"
            )
        self._compat_checked = True

    # ------------------------------------------------------------------ #
    # Task CRUD (design §7.3)                                            #
    # ------------------------------------------------------------------ #

    async def create_task(self, request: dict[str, Any]) -> WorkerTaskHandle:
        """提交一个新任务，返回轻量句柄（``task_id`` + ``status``）。

        ``request`` 可以是任何 JSON 可序列化的字典，字段含义遵循
        ``worker.contract.task.TaskRequest``。SDK 不复用服务端 Pydantic 模型
        以保持依赖最小化（决策 C4）。

        Raises:
            WorkerConflictError: 409，``task_id`` 已存在（幂等冲突）。
        """
        await self._maybe_compat_check()
        response = await self._request("POST", "/tasks", json_body=request)
        body = self._parse_json(response)
        return WorkerTaskHandle(task_id=body["task_id"], status=body["status"])

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """读取任务当前快照（``GET /tasks/{task_id}``）。

        Raises:
            WorkerNotFoundError: 404，task 不存在。
        """
        await self._maybe_compat_check()
        response = await self._request("GET", f"/tasks/{task_id}")
        return self._parse_json(response)

    async def abort_task(self, task_id: str) -> dict[str, Any]:
        """主动中止任务（``POST /tasks/{task_id}/abort``）。

        Raises:
            WorkerNotFoundError: 404，task 不存在。
            WorkerConflictError: 409，task 已在终态。
        """
        await self._maybe_compat_check()
        response = await self._request("POST", f"/tasks/{task_id}/abort", json_body={})
        return self._parse_json(response)

    # ------------------------------------------------------------------ #
    # Event stream (design §7.4 / §9)                                    #
    # ------------------------------------------------------------------ #

    async def stream_events(
        self,
        task_id: str,
        *,
        last_event_id: int | None = None,
        include_heartbeats: bool = False,
        auto_resume: bool | None = None,
    ) -> AsyncIterator[WorkerEvent]:
        """订阅任务事件流。

        默认会跳过 ``heartbeat`` 事件并在收到 terminal event 后自然结束。
        ``auto_resume`` 为 ``None`` 时使用构造参数 ``auto_reconnect_sse``，显
        式 True/False 可覆盖单次订阅的行为。

        Yields:
            ``WorkerEvent``：按 ``cursor`` 升序的业务事件。
        """
        await self._maybe_compat_check()
        resume = self._auto_reconnect_sse if auto_resume is None else auto_resume

        def connect(last_cursor: int | None) -> AbstractAsyncContextManager[_SseStream]:
            # SSE 没有上限 timeout：read timeout 设为 None，连接超时仍受
            # self._timeout 限制以便诊断"端点根本起不来"的情况。
            sse_timeout = httpx.Timeout(self._timeout, read=None)
            url = f"{self._base_url}/tasks/{task_id}/events"
            headers = bearer_sse_headers(self._token, last_event_id=last_cursor)
            return open_sse_stream(self._http, url, headers, sse_timeout)

        async for event in stream_events_with_reconnect(
            connect,
            initial_last_event_id=last_event_id,
            include_heartbeats=include_heartbeats,
            auto_reconnect=resume,
            max_reconnect_attempts=self._max_sse_reconnect_attempts,
        ):
            yield event

    # ------------------------------------------------------------------ #
    # Terminal wait (design §7.5)                                        #
    # ------------------------------------------------------------------ #

    async def wait_until_terminal(
        self,
        task_id: str,
        *,
        timeout: float | None = None,
        raise_on_failure: bool = False,
    ) -> WorkerTerminalResult:
        """阻塞等待任务进入终态，返回最终快照。

        实现思路：

        1. 走 ``stream_events`` 等待 terminal event；
        2. 收到后再 ``get_task`` 一次拿完整 snapshot（事件 payload 不一定包含
           所有快照字段，比如 ``opencode_session_id``）；
        3. 若 ``raise_on_failure=True`` 且终态不是 ``completed``，抛对应异常。

        Args:
            timeout:           SDK 层最长等待秒数；超时抛 ``WorkerClientError``
                               （注意：这是 SDK 等待超时，不等于服务端任务
                               ``timed_out`` 终态）。
            raise_on_failure:  非 ``completed`` 终态时是否抛 ``WorkerTaskTerminalError``。

        Raises:
            WorkerClientError:        SDK wait timeout。
            WorkerTaskTerminalError:  ``raise_on_failure=True`` 且终态非 ``completed``。
        """
        import asyncio

        terminal_event: WorkerEvent | None = None

        async def _wait() -> WorkerEvent | None:
            async for event in self.stream_events(task_id):
                if event.kind in TERMINAL_EVENT_KINDS:
                    return event
            return None

        try:
            if timeout is not None:
                terminal_event = await asyncio.wait_for(_wait(), timeout=timeout)
            else:
                terminal_event = await _wait()
        except asyncio.TimeoutError as exc:
            raise WorkerClientError(
                f"wait_until_terminal timed out after {timeout}s for task {task_id}"
            ) from exc

        # 即使 stream 没拿到 terminal event（极端边界：服务端在 SDK 重连用尽
        # 后才推完终态），仍然兜底拉一次 snapshot；状态机里 final_status 是
        # 真实事实，事件只是通知通道。
        snapshot = await self.get_task(task_id)
        final_status = str(snapshot.get("status", ""))

        result = WorkerTerminalResult(
            task_id=task_id,
            final_status=final_status,
            terminal_event=terminal_event,
            task_snapshot=snapshot,
        )

        if raise_on_failure:
            exc_cls = terminal_error_for(final_status)
            if exc_cls is not None:
                raise exc_cls(
                    f"task {task_id} ended with status={final_status}",
                    task_id=task_id,
                    final_status=final_status,
                    terminal_event=terminal_event,
                    task_snapshot=snapshot,
                )

        return result

    # ------------------------------------------------------------------ #
    # HITL decisions (design §7.6)                                       #
    # ------------------------------------------------------------------ #

    async def submit_decision(
        self,
        task_id: str,
        *,
        decision_id: str,
        choice: str,
        feedback: str | None = None,
        patch: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """提交人工决策（``POST /tasks/{task_id}/decisions``）。

        ``idempotency_key`` 未提供时由 SDK 自动生成 UUID v4，保证重复点击/网
        络重试不会重复消费决策。

        Args:
            decision_id:      ``hitl_required`` 事件 payload 中的 ``decision_id``。
            choice:           ``approve`` / ``reject`` / ``revise`` / ``abort``
                              之一；SDK 不校验合法性，由服务端把关。
            feedback:         可选文字说明（``revise`` 时通常必填）。
            patch:            可选结构化修订内容。
            idempotency_key:  防重提交 key；省略则自动生成。

        Raises:
            WorkerNotFoundError: 404，task 不存在。
            WorkerConflictError: 409，decision 不存在或已解决。
        """
        await self._maybe_compat_check()
        body: dict[str, Any] = {
            "decision_id": decision_id,
            "choice": choice,
            "idempotency_key": idempotency_key or str(uuid.uuid4()),
        }
        if feedback is not None:
            body["feedback"] = feedback
        if patch is not None:
            body["patch"] = patch
        response = await self._request(
            "POST", f"/tasks/{task_id}/decisions", json_body=body
        )
        return self._parse_json(response)

    # ------------------------------------------------------------------ #
    # Artifacts (design §7.7)                                            #
    # ------------------------------------------------------------------ #

    async def list_artifacts(self, task_id: str) -> list[WorkerArtifactRef]:
        """列出任务产物元数据。

        Raises:
            WorkerNotFoundError: 404，task 不存在。
        """
        await self._maybe_compat_check()
        response = await self._request("GET", f"/tasks/{task_id}/artifacts")
        items = self._parse_json(response)
        if not isinstance(items, list):
            raise WorkerClientError(
                f"list_artifacts: expected JSON array, got {type(items).__name__}"
            )
        return [self._to_artifact_ref(item) for item in items]

    async def download_artifact_bytes(
        self,
        task_id: str,
        artifact_id: str,
    ) -> bytes:
        """以 bytes 形式下载单个产物。

        适合 small artifact（log / transcript JSON）；对于大尺寸
        ``workspace_snapshot`` 优先用 ``download_artifact_to``。
        """
        self._validate_artifact_id(artifact_id)
        await self._maybe_compat_check()
        url = f"/tasks/{task_id}/artifacts/{artifact_id}"
        response = await self._request("GET", url, expect_json=False)
        return response.content

    async def download_artifact_to(
        self,
        task_id: str,
        artifact_id: str,
        dest_path: str,
        *,
        overwrite: bool = False,
    ) -> str:
        """流式下载产物到本地文件，返回实际写入的绝对路径。

        - 若目标父目录不存在，会自动创建；
        - ``overwrite=False`` 时遇到已存在的目标文件抛 ``FileExistsError``；
        - 流式写入，避免大文件全量加载到内存。
        """
        self._validate_artifact_id(artifact_id)
        await self._maybe_compat_check()

        path = Path(dest_path).expanduser().resolve()
        if path.exists() and not overwrite:
            raise FileExistsError(f"destination already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)

        url = f"{self._base_url}/tasks/{task_id}/artifacts/{artifact_id}"
        headers = bearer_headers(self._token)
        try:
            async with self._http.stream("GET", url, headers=headers) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    self._raise_http_error(response.status_code, body)
                # 用一个 tmp 文件落盘，成功后 rename，避免半成品文件残留
                tmp_path = path.with_suffix(path.suffix + ".part")
                try:
                    with tmp_path.open("wb") as fh:
                        async for chunk in response.aiter_bytes():
                            fh.write(chunk)
                    os.replace(tmp_path, path)
                except BaseException:
                    # 写入失败 / 取消时清理半成品
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise
        except httpx.HTTPError as exc:
            raise WorkerTransportError(f"artifact download failed: {exc!r}") from exc

        return str(path)

    # ------------------------------------------------------------------ #
    # Convenience (design §7.8)                                          #
    # ------------------------------------------------------------------ #

    async def create_and_wait(
        self,
        request: dict[str, Any],
        *,
        timeout: float | None = None,
        raise_on_failure: bool = False,
    ) -> WorkerTerminalResult:
        """``create_task`` + ``wait_until_terminal`` 的薄组合 helper。"""
        handle = await self.create_task(request)
        return await self.wait_until_terminal(
            handle.task_id,
            timeout=timeout,
            raise_on_failure=raise_on_failure,
        )

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    async def _maybe_compat_check(self) -> None:
        """首次请求前做一次版本校验，之后短路。"""
        if not self._compat_check_enabled or self._compat_checked:
            return
        # 标记先行，防止 assert_compatible 内部的请求再次触发递归
        self._compat_checked = True
        try:
            await self.assert_compatible()
        except WorkerCompatibilityError:
            # 兼容检查失败时把状态退回，让后续调用仍能触发检查；
            # 否则一次失败就让 SDK 永久跳过校验，反而掩盖问题。
            self._compat_checked = False
            raise

    async def _raw_get(self, path: str) -> httpx.Response:
        """轻量 GET，**不触发** compat 检查（用于 ``get_health`` 自身）。

        GET 默认走 retry policy；调用方拿到的总是 2xx 响应或一个已经抛出的
        异常。
        """
        return await self._request("GET", path, retry=True)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        expect_json: bool = True,
        retry: bool | None = None,
    ) -> httpx.Response:
        """统一 HTTP 出口：注入鉴权头、捕获 transport error、按状态码抛错。

        Args:
            method:    HTTP 方法。
            path:      相对 ``base_url`` 的路径。
            json_body: 可选 JSON 请求体；为 ``None`` 时不发送 body。
            retry:     ``True`` 启用基于 ``self._retry_policy`` 的指数退避重试；
                       ``False`` 永远不重试；``None`` 走默认（GET → 重试，其
                       它方法 → 不重试）。

        Raises:
            WorkerTransportError: 网络层失败且重试用尽。
            WorkerHTTPError 子类: 服务端返回非 2xx；5xx 在 retry 启用时会经过
                指数退避，4xx 永不重试。
        """
        do_retry = self._should_retry(method, retry)
        policy = self._retry_policy if do_retry else RetryPolicy.disabled()
        attempt = 0
        last_exc: Exception | None = None

        while attempt < policy.max_attempts:
            attempt += 1
            try:
                response = await self._send_once(method, path, json_body=json_body)
            except WorkerTransportError as exc:
                last_exc = exc
                if not policy.retry_on_transport_error or attempt >= policy.max_attempts:
                    raise
                wait = policy.backoff_for_attempt(attempt)
                logger.info(
                    "retrying %s %s after transport error (attempt %d/%d, sleep %.2fs): %s",
                    method, path, attempt, policy.max_attempts, wait, exc,
                )
                await sleep_for_backoff(wait)
                continue

            if 200 <= response.status_code < 300:
                return response

            # 非 2xx：决定是否进入重试分支
            if (
                policy.retry_on_5xx
                and 500 <= response.status_code < 600
                and attempt < policy.max_attempts
            ):
                # 优先服务端 Retry-After 建议，否则本地指数退避
                local_wait = policy.backoff_for_attempt(attempt)
                wait = local_wait
                if policy.respect_retry_after:
                    server_wait = parse_retry_after(response.headers.get("Retry-After"))
                    if server_wait is not None:
                        wait = max(server_wait, local_wait)
                logger.info(
                    "retrying %s %s after status=%d (attempt %d/%d, sleep %.2fs)",
                    method, path, response.status_code, attempt, policy.max_attempts, wait,
                )
                # 显式 aread 以释放连接（response 不再被消费）
                await self._safe_release(response)
                await sleep_for_backoff(wait)
                continue

            # 4xx 或重试次数已用尽：抛 typed error
            self._raise_http_error(response.status_code, response.content)

        # 不应该走到这里——max_attempts >= 1 时循环至少跑一次并 raise/return
        if last_exc is not None:
            raise last_exc
        raise WorkerClientError(  # pragma: no cover - defensive
            f"{method} {path}: retry loop exited without response or exception"
        )

    @staticmethod
    def _should_retry(method: str, override: bool | None) -> bool:
        """决定一次请求是否启用 retry policy。

        - ``override`` 显式指定时直接采用。
        - ``None`` 时默认 GET / HEAD → True，其余方法 → False。
          这一保守默认避免对非幂等 POST （create_task / submit_decision 等）
          做自动重试——见 retry.py 顶部 docstring 的说明。
        """
        if override is not None:
            return override
        return method.upper() in ("GET", "HEAD")

    async def _send_once(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
    ) -> httpx.Response:
        """单次 HTTP 调用，把 httpx 异常翻译为 ``WorkerTransportError``。"""
        try:
            if json_body is None:
                return await self._http.request(
                    method, path, headers=bearer_headers(self._token)
                )
            return await self._http.request(
                method,
                path,
                headers=bearer_headers(self._token),
                content=json.dumps(json_body),
            )
        except httpx.HTTPError as exc:
            raise WorkerTransportError(
                f"{method} {path} transport error: {exc!r}"
            ) from exc

    @staticmethod
    async def _safe_release(response: httpx.Response) -> None:
        """重试前释放上一次 5xx 响应底层连接。``response.aread`` 幂等且安全。"""
        try:
            await response.aread()
        except httpx.HTTPError:  # pragma: no cover - best-effort cleanup
            pass

    @staticmethod
    def _parse_json(response: httpx.Response) -> Any:
        """解析 JSON 响应，失败时把 transport 错误归类为 ``WorkerClientError``。"""
        try:
            return response.json()
        except ValueError as exc:
            raise WorkerClientError(
                f"invalid JSON response from worker: {exc}"
            ) from exc

    @staticmethod
    def _raise_http_error(status_code: int, raw_body: bytes) -> None:
        """根据 status code 抛对应的 ``WorkerHTTPError`` 子类。"""
        body: Any
        try:
            body = json.loads(raw_body) if raw_body else None
        except ValueError:
            # 服务端通常返回 JSON；偶尔遇到 nginx 502 之类 text 响应也别报错
            body = raw_body.decode("utf-8", errors="replace") if raw_body else None
        message = AsyncWorkerClient._format_http_message(status_code, body)
        exc_cls = http_error_for(status_code)
        raise exc_cls(message, status_code=status_code, response_body=body)

    @staticmethod
    def _format_http_message(status_code: int, body: Any) -> str:
        if isinstance(body, dict):
            detail = body.get("detail") or body.get("message") or body
        else:
            detail = body
        return f"worker returned {status_code}: {detail}"

    @staticmethod
    def _validate_artifact_id(artifact_id: str) -> None:
        """防止上游传入 ``../`` 之类的非法 artifact id。

        服务端有 ``Path.resolve().relative_to(artifacts_root)`` 兜底，但 SDK
        提前过滤可以避免无意义的 4xx 请求并给出更清晰的错误。
        """
        if not artifact_id or not _ARTIFACT_ID_RE.match(artifact_id):
            raise ValueError(
                f"invalid artifact_id {artifact_id!r}: must match {_ARTIFACT_ID_RE.pattern}"
            )

    @staticmethod
    def _to_artifact_ref(item: dict[str, Any]) -> WorkerArtifactRef:
        return WorkerArtifactRef(
            artifact_id=item["artifact_id"],
            task_id=item["task_id"],
            type=str(item["type"]),
            filename=item["filename"],
            size=item.get("size"),
            created_at=float(item["created_at"]),
            expires_at=item.get("expires_at"),
            download_url=item.get("download_url"),
            metadata=item.get("metadata") or {},
        )
