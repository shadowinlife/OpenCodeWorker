"""Unit tests for ``worker_sdk.retry`` + retry wiring in ``AsyncWorkerClient``.

测试矩阵：

- RetryPolicy 自身：构造校验、backoff 单调递增并被 max_backoff_sec 夹住、
  ``disabled()`` 等同于 ``max_attempts=1``、``parse_retry_after`` 边界。
- 与 client 协同：
    * GET 5xx → 重试 → 第 N 次成功
    * GET transport error → 重试 → 成功
    * GET 5xx 持续失败 → 抛 ``WorkerServerError``
    * GET 404 → 不重试（4xx 是确定性错误）
    * POST 默认不重试（保护非幂等操作）
    * 显式 ``RetryPolicy.disabled()`` → 即使是 GET 也立即失败
    * ``Retry-After`` 头优先于本地 backoff
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from worker_sdk import AsyncWorkerClient, RetryPolicy
from worker_sdk.errors import (
    WorkerNotFoundError,
    WorkerServerError,
    WorkerTransportError,
)
from worker_sdk.retry import parse_retry_after


# ---------------------------------------------------------------------------
# RetryPolicy unit
# ---------------------------------------------------------------------------

def test_retry_policy_validates_inputs():
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(initial_backoff_sec=-1)
    with pytest.raises(ValueError):
        RetryPolicy(initial_backoff_sec=5, max_backoff_sec=1)
    with pytest.raises(ValueError):
        RetryPolicy(backoff_multiplier=0.5)
    with pytest.raises(ValueError):
        RetryPolicy(jitter_ratio=1.5)


def test_backoff_growth_and_cap():
    """无 jitter 的退避序列应该呈指数增长并被 max_backoff_sec 夹住。"""
    policy = RetryPolicy(
        max_attempts=10,
        initial_backoff_sec=0.1,
        max_backoff_sec=1.0,
        backoff_multiplier=2.0,
        jitter_ratio=0.0,
    )
    seq = [policy.backoff_for_attempt(i) for i in range(1, 8)]
    # 0.1, 0.2, 0.4, 0.8, 1.0(capped), 1.0(capped), 1.0(capped)
    assert seq == [0.1, 0.2, 0.4, 0.8, 1.0, 1.0, 1.0]


def test_backoff_jitter_within_bounds():
    """带 jitter 时实际等待必须落在 [base*(1-r), base*(1+r)] 之内。"""
    policy = RetryPolicy(
        max_attempts=5,
        initial_backoff_sec=1.0,
        max_backoff_sec=1.0,
        backoff_multiplier=2.0,
        jitter_ratio=0.25,
    )
    for _ in range(50):
        wait = policy.backoff_for_attempt(1)
        assert 0.75 <= wait <= 1.25


def test_disabled_policy_only_runs_once():
    policy = RetryPolicy.disabled()
    assert policy.max_attempts == 1


def test_parse_retry_after_seconds():
    assert parse_retry_after("3") == 3.0
    assert parse_retry_after("1.5") == 1.5
    assert parse_retry_after(" 2 ") == 2.0
    # HTTP-date format intentionally not supported → None
    assert parse_retry_after("Wed, 21 Oct 2025 07:28:00 GMT") is None
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("-1") is None


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------

class FlakeyStub:
    """A tiny FastAPI app that fails N times then succeeds.

    Each route counts its own calls so multiple endpoints can coexist in one
    test if needed.
    """

    def __init__(
        self,
        *,
        fail_count: int = 0,
        fail_status: int = 503,
        retry_after_header: str | None = None,
    ) -> None:
        self.app = FastAPI()
        self.fail_count = fail_count
        self.fail_status = fail_status
        self.retry_after_header = retry_after_header
        self.calls = 0

        @self.app.get("/tasks/{task_id}")
        async def get_task(task_id: str):
            self.calls += 1
            if self.calls <= self.fail_count:
                headers = (
                    {"Retry-After": self.retry_after_header}
                    if self.retry_after_header is not None
                    else {}
                )
                return JSONResponse(
                    status_code=self.fail_status,
                    content={"detail": "transient"},
                    headers=headers,
                )
            return {"task_id": task_id, "status": "completed"}

        @self.app.post("/tasks", status_code=201)
        async def create_task(request: Request):
            self.calls += 1
            if self.calls <= self.fail_count:
                return JSONResponse(
                    status_code=self.fail_status,
                    content={"detail": "transient"},
                )
            body = await request.json()
            return {
                "task_id": body.get("task_id", "auto-1"),
                "status": "queued",
                "mode": "plan_first",
                "created_at": 0.0,
                "updated_at": 0.0,
            }


def _fast_retry_policy(max_attempts: int = 3) -> RetryPolicy:
    """A retry policy with effectively-zero backoff so tests don't sleep."""
    return RetryPolicy(
        max_attempts=max_attempts,
        initial_backoff_sec=0.0,
        max_backoff_sec=0.0,
        backoff_multiplier=1.0,
        jitter_ratio=0.0,
    )


# ---------------------------------------------------------------------------
# GET retry on 5xx / transport error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_retries_on_5xx_then_succeeds():
    stub = FlakeyStub(fail_count=2, fail_status=503)
    transport = httpx.ASGITransport(app=stub.app)
    async with AsyncWorkerClient(
        base_url="http://stub.test",
        bearer_token="t",
        compatibility_check=False,
        retry_policy=_fast_retry_policy(max_attempts=3),
        transport=transport,
    ) as sdk:
        snapshot = await sdk.get_task("abc")
    assert snapshot["status"] == "completed"
    assert stub.calls == 3  # 2 failures + 1 success


@pytest.mark.asyncio
async def test_get_gives_up_after_max_attempts():
    """5xx 持续返回时，超出 max_attempts 必须抛 WorkerServerError。"""
    stub = FlakeyStub(fail_count=99, fail_status=500)
    transport = httpx.ASGITransport(app=stub.app)
    async with AsyncWorkerClient(
        base_url="http://stub.test",
        bearer_token="t",
        compatibility_check=False,
        retry_policy=_fast_retry_policy(max_attempts=2),
        transport=transport,
    ) as sdk:
        with pytest.raises(WorkerServerError) as exc:
            await sdk.get_task("abc")
    assert exc.value.status_code == 500
    assert stub.calls == 2  # exactly max_attempts


@pytest.mark.asyncio
async def test_get_does_not_retry_4xx():
    """404 是确定性错误，不应该被重试。"""
    app = FastAPI()
    calls = {"n": 0}

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str):
        calls["n"] += 1
        raise HTTPException(status_code=404, detail="missing")

    transport = httpx.ASGITransport(app=app)
    async with AsyncWorkerClient(
        base_url="http://stub.test",
        bearer_token="t",
        compatibility_check=False,
        retry_policy=_fast_retry_policy(max_attempts=5),
        transport=transport,
    ) as sdk:
        with pytest.raises(WorkerNotFoundError):
            await sdk.get_task("missing")
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_get_retries_on_transport_error_then_succeeds(monkeypatch):
    """前 N 次 raise ConnectError，最后一次返回 200。"""
    stub = FlakeyStub(fail_count=0)
    transport = httpx.ASGITransport(app=stub.app)
    async with AsyncWorkerClient(
        base_url="http://stub.test",
        bearer_token="t",
        compatibility_check=False,
        retry_policy=_fast_retry_policy(max_attempts=3),
        transport=transport,
    ) as sdk:
        real_request = sdk._http.request
        attempts = {"n": 0}

        async def flaky_request(*args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise httpx.ConnectError("connection refused")
            return await real_request(*args, **kwargs)

        monkeypatch.setattr(sdk._http, "request", flaky_request)
        snapshot = await sdk.get_task("abc")
    assert snapshot["status"] == "completed"
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_transport_error_gives_up_after_max_attempts(monkeypatch):
    stub = FlakeyStub(fail_count=0)
    transport = httpx.ASGITransport(app=stub.app)
    async with AsyncWorkerClient(
        base_url="http://stub.test",
        bearer_token="t",
        compatibility_check=False,
        retry_policy=_fast_retry_policy(max_attempts=2),
        transport=transport,
    ) as sdk:
        async def always_fail(*args, **kwargs):
            raise httpx.ConnectError("nope")

        monkeypatch.setattr(sdk._http, "request", always_fail)
        with pytest.raises(WorkerTransportError):
            await sdk.get_task("abc")


# ---------------------------------------------------------------------------
# POST default behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_does_not_retry_by_default():
    """create_task 默认不重试——避免对非幂等操作造成重复副作用。"""
    stub = FlakeyStub(fail_count=2, fail_status=503)
    transport = httpx.ASGITransport(app=stub.app)
    async with AsyncWorkerClient(
        base_url="http://stub.test",
        bearer_token="t",
        compatibility_check=False,
        retry_policy=_fast_retry_policy(max_attempts=5),
        transport=transport,
    ) as sdk:
        with pytest.raises(WorkerServerError):
            await sdk.create_task({"mode": "plan_first"})
    # 只调用了一次——证明 POST 没有被自动重试
    assert stub.calls == 1


# ---------------------------------------------------------------------------
# Disabled policy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disabled_policy_skips_retry_even_on_get():
    stub = FlakeyStub(fail_count=2, fail_status=503)
    transport = httpx.ASGITransport(app=stub.app)
    async with AsyncWorkerClient(
        base_url="http://stub.test",
        bearer_token="t",
        compatibility_check=False,
        retry_policy=RetryPolicy.disabled(),
        transport=transport,
    ) as sdk:
        with pytest.raises(WorkerServerError):
            await sdk.get_task("abc")
    assert stub.calls == 1


# ---------------------------------------------------------------------------
# Retry-After header
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_after_header_is_respected(monkeypatch):
    """5xx 响应携带 Retry-After 时，SDK 必须 sleep 至少这么长。"""
    stub = FlakeyStub(fail_count=1, fail_status=503, retry_after_header="0.2")
    transport = httpx.ASGITransport(app=stub.app)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        # 测试中真正 sleep 会拖慢套件；这里只记录请求的时长
        await asyncio.sleep(0)

    monkeypatch.setattr("worker_sdk.client.sleep_for_backoff", fake_sleep)

    async with AsyncWorkerClient(
        base_url="http://stub.test",
        bearer_token="t",
        compatibility_check=False,
        # 本地 backoff 设 0.01 → Retry-After=0.2 必须胜出
        retry_policy=RetryPolicy(
            max_attempts=3,
            initial_backoff_sec=0.01,
            max_backoff_sec=0.01,
            backoff_multiplier=1.0,
            jitter_ratio=0.0,
            respect_retry_after=True,
        ),
        transport=transport,
    ) as sdk:
        snapshot = await sdk.get_task("abc")
    assert snapshot["status"] == "completed"
    # 至少一次 sleep 等待了 ≥0.2 秒（服务端 Retry-After）
    assert sleep_calls, "expected at least one backoff sleep"
    assert max(sleep_calls) >= 0.2


@pytest.mark.asyncio
async def test_retry_after_ignored_when_disabled(monkeypatch):
    stub = FlakeyStub(fail_count=1, fail_status=503, retry_after_header="5.0")
    transport = httpx.ASGITransport(app=stub.app)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr("worker_sdk.client.sleep_for_backoff", fake_sleep)

    async with AsyncWorkerClient(
        base_url="http://stub.test",
        bearer_token="t",
        compatibility_check=False,
        retry_policy=RetryPolicy(
            max_attempts=3,
            initial_backoff_sec=0.01,
            max_backoff_sec=0.01,
            backoff_multiplier=1.0,
            jitter_ratio=0.0,
            respect_retry_after=False,
        ),
        transport=transport,
    ) as sdk:
        await sdk.get_task("abc")
    # respect_retry_after=False 时所有 sleep 都应使用本地 0.01s
    assert all(s <= 0.02 for s in sleep_calls), sleep_calls


# ---------------------------------------------------------------------------
# Plain-text 5xx body still maps to WorkerServerError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_plaintext_5xx_response_still_typed():
    """nginx 502/504 常返回 text/html；SDK 仍应翻译成 WorkerServerError。"""
    app = FastAPI()

    @app.get("/tasks/{task_id}")
    async def boom(task_id: str):
        return PlainTextResponse("Bad Gateway", status_code=502)

    transport = httpx.ASGITransport(app=app)
    async with AsyncWorkerClient(
        base_url="http://stub.test",
        bearer_token="t",
        compatibility_check=False,
        retry_policy=RetryPolicy.disabled(),
        transport=transport,
    ) as sdk:
        with pytest.raises(WorkerServerError) as exc:
            await sdk.get_task("abc")
    assert exc.value.status_code == 502
