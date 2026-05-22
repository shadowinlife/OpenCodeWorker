# Worker Client SDK 用户示例

> **配套文档**：[worker-sdk-usage.md](worker-sdk-usage.md) —— 接口与概念参考
>
> 本文给出**端到端可运行**的示例集合。所有 snippet 均假设：
> - Python ≥ 3.11
> - 已经 `pip install -e /path/to/VibeTradingOpenCodeWorker`
> - 环境变量 `WORKER_BASE_URL` / `WORKER_TOKEN` 已设置（或在代码中直接替换）

---

## 目录

1. [示例 1：最小可运行 demo（30 秒上手）](#示例-1最小可运行-demo30-秒上手)
2. [示例 2：plan_first 模式 + HITL 决策](#示例-2plan_first-模式--hitl-决策)
3. [示例 3：直接执行 + 流式监控事件](#示例-3直接执行--流式监控事件)
4. [示例 4：自定义 RetryPolicy](#示例-4自定义-retrypolicy)
5. [示例 5：关闭兼容性检查 / 手动校验](#示例-5关闭兼容性检查--手动校验)
6. [示例 6：枚举并下载 artifacts](#示例-6枚举并下载-artifacts)
7. [示例 7：主动 abort 一个长任务](#示例-7主动-abort-一个长任务)
8. [示例 8：把事件转发到日志 / 指标系统](#示例-8把事件转发到日志--指标系统)
9. [示例 9：断点续传 — 用 `last_event_id` 接续订阅](#示例-9断点续传--用-last_event_id-接续订阅)
10. [示例 10：用 `httpx.ASGITransport` 写单元测试](#示例-10用-httpxasgitransport-写单元测试)
11. [示例 11：在同步上下文里调用 SDK](#示例-11在同步上下文里调用-sdk)
12. [示例 12：批量并发提交任务](#示例-12批量并发提交任务)

---

## 示例 1：最小可运行 demo（30 秒上手）

最简场景：提交一个 `direct_execute` 任务，等到终态后打印结果。

```python
import asyncio
import os

from worker_sdk import AsyncWorkerClient


async def main() -> None:
    async with AsyncWorkerClient(
        base_url=os.environ["WORKER_BASE_URL"],
        bearer_token=os.environ["WORKER_TOKEN"],
    ) as client:
        result = await client.create_and_wait(
            request={
                "mode": "direct_execute",
                "messages": [
                    {"role": "user", "content": "在 /workspace 下创建 hello.txt，写入 'hi'"}
                ],
            },
            timeout=300,
            raise_on_failure=True,
        )

    print("final_status:", result.final_status)
    print("opencode_session_id:", result.task_snapshot.get("opencode_session_id"))


if __name__ == "__main__":
    asyncio.run(main())
```

要点：
- `create_and_wait` = `create_task` + `wait_until_terminal`，常用 fire-and-forget。
- `raise_on_failure=True` 时非 `completed` 终态直接抛 `WorkerTaskTerminalError` 子类。
- `result.task_snapshot` 是终态后再次 `GET /tasks/:id` 的完整 JSON。

---

## 示例 2：plan_first 模式 + HITL 决策

`plan_first` 模式下，Worker 先让 LLM 生成执行计划，然后通过 SSE 推一条 `hitl_required` 事件等待人工审批。本示例演示如何：自己消费事件流 → 检测到 HITL → 提交 `approve`/`revise`/`reject`/`abort` 决策。

```python
import asyncio
import json
import os

from worker_sdk import (
    AsyncWorkerClient,
    WorkerEvent,
    WorkerTaskFailed,
)


def review_plan_interactively(plan_text: str) -> tuple[str, str | None]:
    """模拟运维同学读完计划做出决策。
    实际场景里这一步可能是发到 Slack 等待按钮回调，或写到 Web UI。
    """
    print("=== 待审批计划 ===")
    print(plan_text)
    print("==================")
    answer = input("approve / reject / revise / abort: ").strip()
    feedback = None
    if answer == "revise":
        feedback = input("修改建议: ")
    return answer, feedback


async def run_with_hitl(client: AsyncWorkerClient, request: dict) -> str:
    handle = await client.create_task(request)
    task_id = handle.task_id
    print(f"submitted task_id={task_id}")

    async for event in client.stream_events(task_id):
        print(f"[{event.cursor}] {event.kind}")

        if event.kind == "plan_ready":
            # plan_ready 之后 Worker 会很快推 hitl_required；这里只打印
            print(event.payload.get("plan_text", "")[:200])

        elif event.kind == "hitl_required":
            decision_id = event.payload["decision_id"]
            plan_text = event.payload.get("summary", "")
            choice, feedback = review_plan_interactively(plan_text)
            await client.submit_decision(
                task_id,
                decision_id=decision_id,
                choice=choice,
                feedback=feedback,
            )

        elif event.kind in ("task_completed", "task_failed", "task_aborted", "task_timed_out"):
            return event.kind

    # 极端边界：流早于终态结束（重连次数耗尽前），fallback 到快照
    snapshot = await client.get_task(task_id)
    return f"unknown:{snapshot.get('status')}"


async def main() -> None:
    async with AsyncWorkerClient(
        base_url=os.environ["WORKER_BASE_URL"],
        bearer_token=os.environ["WORKER_TOKEN"],
    ) as client:
        terminal_kind = await run_with_hitl(
            client,
            request={
                "mode": "plan_first",
                "messages": [
                    {"role": "user", "content": "重构 src/utils 下的工具函数，让命名更一致"}
                ],
                "resource_limits": {"timeout_sec": 1800},
                "hitl_policy": {"decision_timeout_sec": 600, "on_timeout": "abort"},
            },
        )
        print("terminal:", terminal_kind)


if __name__ == "__main__":
    asyncio.run(main())
```

要点：
- 自己迭代 `stream_events()` 拿原始事件，便于在 `hitl_required` / `plan_ready` 上插入业务逻辑。
- `submit_decision` 不显式传 `idempotency_key` 时，SDK 会自动生成 UUIDv4。从消息队列触发时建议显式传一个稳定 key（如 webhook id）。
- 事件流在终态后自然结束，无需 `break`。

---

## 示例 3：直接执行 + 流式监控事件

`direct_execute` 跳过 plan 阶段，但仍可能因权限不足触发 HITL。下面演示如何在 LLM 流式输出片段（`assistant_delta`）和工具调用（`tool_call_started/finished`）发生时实时打印进度条。

```python
import asyncio
import os
import sys

from worker_sdk import AsyncWorkerClient


async def stream_to_stdout(client: AsyncWorkerClient, task_id: str) -> str:
    final_status = ""
    async for event in client.stream_events(task_id):
        if event.kind == "assistant_delta":
            sys.stdout.write(event.payload.get("content", ""))
            sys.stdout.flush()
        elif event.kind == "tool_call_started":
            tool = event.payload.get("tool", "?")
            print(f"\n>>> tool call: {tool}", flush=True)
        elif event.kind == "tool_call_finished":
            exit_code = event.payload.get("exit_code")
            print(f"<<< exit_code={exit_code}", flush=True)
        elif event.kind in ("task_completed", "task_failed", "task_aborted", "task_timed_out"):
            print(f"\n*** terminal: {event.kind}", flush=True)
            final_status = event.kind
    return final_status


async def main() -> None:
    async with AsyncWorkerClient(
        base_url=os.environ["WORKER_BASE_URL"],
        bearer_token=os.environ["WORKER_TOKEN"],
    ) as client:
        handle = await client.create_task(
            {
                "mode": "direct_execute",
                "messages": [
                    {"role": "user", "content": "运行 pytest tests/unit 并把失败用例总结成一份 markdown"}
                ],
            }
        )
        await stream_to_stdout(client, handle.task_id)


if __name__ == "__main__":
    asyncio.run(main())
```

要点：
- 不依赖 `wait_until_terminal()`，自己判断终态字符串。
- `assistant_delta.payload.content` 是 LLM 流式增量，直接拼接即可。

---

## 示例 4：自定义 RetryPolicy

需要更激进的重试或者完全关闭重试时，传入自己的 `RetryPolicy`。

```python
from worker_sdk import AsyncWorkerClient, RetryPolicy


# 5 次尝试、指数退避到 30s、抖动 50%、尊重 Retry-After
aggressive = RetryPolicy(
    max_attempts=5,
    initial_backoff_sec=1.0,
    max_backoff_sec=30.0,
    backoff_multiplier=2.0,
    jitter_ratio=0.5,
    retry_on_5xx=True,
    retry_on_transport_error=True,
    respect_retry_after=True,
)

# 完全关闭（一次失败立刻抛）
no_retry = RetryPolicy.disabled()


async def with_aggressive_retry():
    async with AsyncWorkerClient(
        base_url="http://worker.internal:8080",
        bearer_token="...",
        retry_policy=aggressive,
    ) as client:
        return await client.get_health()
```

要点：
- 默认只对 GET / HEAD 自动重试，POST 不重试。
- 5xx 路径下若响应带 `Retry-After: 5`，等待时间取 `max(server, local)`。
- 4xx 永远不重试 —— 401/404/409 再试一次只会得到同一结果。

---

## 示例 5：关闭兼容性检查 / 手动校验

灰度环境里 Worker 版本可能短暂落在 SDK 矩阵之外，但你 **明知** 接口兼容；可以关掉自动检查，自己决定时机。

```python
from worker_sdk import AsyncWorkerClient, WorkerCompatibilityError


async def main():
    async with AsyncWorkerClient(
        base_url="http://canary.worker.internal:8080",
        bearer_token="...",
        compatibility_check=False,    # 关掉自动校验
    ) as client:
        # 自己决定要不要检查
        try:
            await client.assert_compatible()
        except WorkerCompatibilityError as exc:
            # 比如发到指标系统，但仍然继续业务调用
            print(f"compat warning: {exc}")

        await client.create_task({...})
```

要点：
- `compatibility_check=True`（默认）只在首次非 `/health` 调用前跑一次。
- 失败时 SDK 会把"已检查"标记退回，下次调用还会再检查 —— 不会因一次失败永久跳过。

---

## 示例 6：枚举并下载 artifacts

任务结束后通常会有 `log`、`transcript`、`workspace_snapshot` 三类产物。

```python
import asyncio
from pathlib import Path

from worker_sdk import AsyncWorkerClient


async def fetch_all_artifacts(client: AsyncWorkerClient, task_id: str, dest_dir: Path) -> None:
    refs = await client.list_artifacts(task_id)
    print(f"task {task_id} has {len(refs)} artifacts")

    for ref in refs:
        target = dest_dir / ref.filename
        if ref.size and ref.size < 5 * 1024 * 1024:
            # 小产物：直接拿 bytes 落盘（log / transcript 等）
            data = await client.download_artifact_bytes(task_id, ref.artifact_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        else:
            # 大产物（workspace_snapshot.tar.gz）：流式
            await client.download_artifact_to(
                task_id,
                ref.artifact_id,
                dest_path=str(target),
                overwrite=True,
            )
        print(f"  saved {target} ({ref.type}, {ref.size} bytes)")
```

要点：
- 小于 5 MB 用 `download_artifact_bytes`，否则一律 `download_artifact_to` 避免 OOM。
- `download_artifact_to` 内部走 `*.part` 临时文件 + `os.replace()` 原子重命名，失败/取消不会留下半成品。

---

## 示例 7：主动 abort 一个长任务

监控发现任务跑飞了，主动停止。

```python
import asyncio

from worker_sdk import (
    AsyncWorkerClient,
    WorkerConflictError,
    WorkerNotFoundError,
)


async def safe_abort(client: AsyncWorkerClient, task_id: str) -> bool:
    """返回是否真的执行了 abort（False 表示任务已不在或已终态）。"""
    try:
        await client.abort_task(task_id)
        return True
    except WorkerNotFoundError:
        # 任务被删 / 不存在
        return False
    except WorkerConflictError:
        # 已经在终态了 —— 幂等保护
        return False
```

要点：
- `abort_task` 在已终态的任务上返回 409 / `WorkerConflictError`，通常可以安全忽略。
- 后续仍可 `get_task()` 查询当前状态、`stream_events()` 看 `task_aborted` 事件。

---

## 示例 8：把事件转发到日志 / 指标系统

把每个 `WorkerEvent` 转发到 structured logging + Prometheus counter。

```python
import asyncio
import logging
import os

from worker_sdk import AsyncWorkerClient

logger = logging.getLogger("worker.events")

# 假设你已经有自己的 metrics module
# from myapp.metrics import event_counter   # Counter("worker_events_total", labelnames=["kind"])


async def forward_events(client: AsyncWorkerClient, task_id: str) -> None:
    async for event in client.stream_events(task_id, include_heartbeats=False):
        logger.info(
            "worker event",
            extra={
                "task_id": task_id,
                "cursor": event.cursor,
                "kind": event.kind,
                "payload": event.payload,
            },
        )
        # event_counter.labels(kind=event.kind).inc()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    async with AsyncWorkerClient(
        base_url=os.environ["WORKER_BASE_URL"],
        bearer_token=os.environ["WORKER_TOKEN"],
    ) as client:
        handle = await client.create_task(
            {"mode": "direct_execute", "messages": [{"role": "user", "content": "ls /workspace"}]}
        )
        await forward_events(client, handle.task_id)


if __name__ == "__main__":
    asyncio.run(main())
```

要点：
- `include_heartbeats=False`（默认）会跳过 `heartbeat` 事件 —— 不要拿心跳推断任务存活，那是 transport 层问题应该由 SDK 重连兜底。
- `WorkerEvent` 是 `frozen=True` 的 dataclass，可以安全跨 task 传递、放进队列。

---

## 示例 9：断点续传 — 用 `last_event_id` 接续订阅

如果你的进程崩溃过、或换了一台机器接着消费同一个任务，可以用上次记录的 `cursor` 续订事件。

```python
import asyncio

from worker_sdk import AsyncWorkerClient


async def resume_stream(client: AsyncWorkerClient, task_id: str, last_cursor: int) -> None:
    print(f"resuming from cursor={last_cursor}")
    async for event in client.stream_events(task_id, last_event_id=last_cursor):
        # 第一批通常是补发的历史事件 (event_id > last_cursor)，
        # 之后切到实时推送，不需要客户端做模式切换
        print(event.cursor, event.kind)
```

要点：
- `last_event_id` 通过 `Last-Event-ID` 头传给服务端，服务端从 DB 读 `event_id > last_event_id` 补发。
- `cursor` 在任务内单调递增；**不要** 用其它任务的 cursor 续。
- 自己持久化 `cursor`（DB / Redis）。SDK **不持久化**任何东西。

---

## 示例 10：用 `httpx.ASGITransport` 写单元测试

不起真实端口，把 FastAPI stub 直接挂到 SDK 上测协议。

```python
import asyncio

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from worker_sdk import AsyncWorkerClient, WorkerNotFoundError


def make_stub_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict:
        if task_id != "stub-1":
            raise HTTPException(status_code=404, detail="task not found")
        return {"task_id": task_id, "status": "completed", "mode": "direct_execute",
                "created_at": 0.0, "updated_at": 1.0, "completed_at": 1.0}

    return app


@pytest.mark.asyncio
async def test_get_task_not_found():
    transport = httpx.ASGITransport(app=make_stub_app())
    async with AsyncWorkerClient(
        base_url="http://stub",
        bearer_token="t",
        transport=transport,
    ) as client:
        with pytest.raises(WorkerNotFoundError) as ei:
            await client.get_task("missing")
        assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_get_task_ok():
    transport = httpx.ASGITransport(app=make_stub_app())
    async with AsyncWorkerClient(
        base_url="http://stub",
        bearer_token="t",
        transport=transport,
    ) as client:
        snap = await client.get_task("stub-1")
        assert snap["status"] == "completed"
```

要点：
- `httpx.ASGITransport(app=...)` 把 FastAPI 跑在内存里，无需端口。
- 实战 stub 参考 [tests/unit/test_worker_sdk.py](../tests/unit/test_worker_sdk.py) 的 `StubWorker`，覆盖 SSE 重连脚本、HITL、artifact 下载。

---

## 示例 11：在同步上下文里调用 SDK

SDK 是 async-only，但同步代码也可以用 `asyncio.run()` 包一层（注意：每次都新建 event loop，**不要在已有 loop 内嵌套调用** `asyncio.run`）：

```python
import asyncio
import os

from worker_sdk import AsyncWorkerClient


def submit_and_wait_sync(prompt: str) -> str:
    async def _run() -> str:
        async with AsyncWorkerClient(
            base_url=os.environ["WORKER_BASE_URL"],
            bearer_token=os.environ["WORKER_TOKEN"],
        ) as client:
            result = await client.create_and_wait(
                {"mode": "direct_execute", "messages": [{"role": "user", "content": prompt}]},
                timeout=600,
                raise_on_failure=True,
            )
            return result.final_status

    return asyncio.run(_run())


if __name__ == "__main__":
    print(submit_and_wait_sync("hello"))
```

如果你已经在 Jupyter / Streamlit / FastAPI 等已有 event loop 的环境里：

```python
# 已有 loop 的环境
loop = asyncio.get_event_loop()
status = loop.run_until_complete(_run())
```

或者用 `nest_asyncio.apply()`（仅 Jupyter 等场景）。

---

## 示例 12：批量并发提交任务

用 `asyncio.gather` 同时跑多个独立任务，复用同一个 client（内部 httpx 连接池会自动复用）。

```python
import asyncio
import os

from worker_sdk import AsyncWorkerClient, WorkerTaskTerminalError


async def run_one(client: AsyncWorkerClient, prompt: str) -> tuple[str, str]:
    try:
        result = await client.create_and_wait(
            {"mode": "direct_execute", "messages": [{"role": "user", "content": prompt}]},
            timeout=600,
            raise_on_failure=True,
        )
        return prompt, result.final_status
    except WorkerTaskTerminalError as exc:
        # 失败任务也返回，避免一个 fail 掉整个 gather
        return prompt, f"failed:{exc.final_status}"


async def main() -> None:
    prompts = [
        "把 README 翻译成英文",
        "把 src 目录的 import 排序",
        "给 utils.py 写测试",
    ]
    async with AsyncWorkerClient(
        base_url=os.environ["WORKER_BASE_URL"],
        bearer_token=os.environ["WORKER_TOKEN"],
    ) as client:
        results = await asyncio.gather(*(run_one(client, p) for p in prompts))
    for prompt, status in results:
        print(f"{status:30s} {prompt!r}")


if __name__ == "__main__":
    asyncio.run(main())
```

要点：
- 单个 `AsyncWorkerClient` 实例可以并发使用 —— httpx 连接池天然支持。
- 用 `return_exceptions=False`（gather 默认）时，单个失败会取消其它；这里把异常吃掉返回字符串避免连锁取消。需要 fail-fast 时反过来。
- Worker 服务端有自己的并发 slot 上限（`worker.config.max_concurrent_tasks`），SDK 不替你做客户端限流。需要时在你这一层用 `asyncio.Semaphore` 控制。

---

## 进一步阅读

- **接口与概念参考**：[worker-sdk-usage.md](worker-sdk-usage.md)
- **接口设计稿**：[design/worker-client-sdk-interface-design.md](design/worker-client-sdk-interface-design.md)
- **服务端契约**：
  - [src/worker/contract/task.py](../src/worker/contract/task.py) —— `TaskRequest` 字段语义
  - [src/worker/contract/event.py](../src/worker/contract/event.py) —— `TaskEventKind` 枚举与 payload 约定
  - [src/worker/contract/decision.py](../src/worker/contract/decision.py) —— HITL `choice` 取值
  - [src/worker/contract/artifact.py](../src/worker/contract/artifact.py) —— `ArtifactType` 枚举
- **协议级测试参考**：[tests/unit/test_worker_sdk.py](../tests/unit/test_worker_sdk.py)
