# Worker Client SDK 使用文档

> **适用版本**：`worker_sdk` 0.1.x ↔ Worker 0.1.x
> **目标读者**：上游 agent runtime / broker / CLI 工具的开发者
> **配套文档**：
> - [worker-sdk-examples.md](worker-sdk-examples.md) —— 端到端可运行示例
> - [design/worker-client-sdk-interface-design.md](design/worker-client-sdk-interface-design.md) —— SDK 接口设计稿
> - [usage-guide.md](usage-guide.md) —— Worker 服务端使用指南

---

## 目录

1. [SDK 是什么 / 不是什么](#1-sdk-是什么--不是什么)
2. [安装与依赖](#2-安装与依赖)
3. [30 秒快速开始](#3-30-秒快速开始)
4. [核心概念](#4-核心概念)
5. [构造客户端](#5-构造客户端)
6. [API 接口参考](#6-api-接口参考)
7. [错误模型](#7-错误模型)
8. [SSE 事件流与断线重连](#8-sse-事件流与断线重连)
9. [HTTP 重试策略](#9-http-重试策略)
10. [兼容性检查](#10-兼容性检查)
11. [测试时注入 ASGI Transport](#11-测试时注入-asgi-transport)
12. [最佳实践与常见陷阱](#12-最佳实践与常见陷阱)

---

## 1. SDK 是什么 / 不是什么

### 是
- 一层 **薄、内部使用、async-first** 的 Python 客户端，封装 Worker 的 HTTP + SSE 协议。
- 收口 6 件容易写错的事：Bearer 认证、任务创建/查询、SSE 解析与断线重连、终态等待、HITL 决策提交、artifact 列举/下载。
- 把瞬时网络故障、5xx、`Retry-After` 这些重试细节做成可配置策略。

### 不是
- 不发明任何新的服务端能力 —— 所有方法严格映射到一个或多个 Worker HTTP 端点。
- 不内嵌 strategy / workflow / vibe-trading 等业务语义。
- 不持久化任何状态（cursor、idempotency key 都在调用栈内生成）。
- 不提供同步版客户端，也不提供多语言绑定。
- 不提供 callback plugin / event bus 抽象 —— 上游自己消费 `AsyncIterator[WorkerEvent]`。

如果你需要的是 "封装协议复杂度"，SDK 是对的工具；如果你想要 "封装编排逻辑"，那应该写在调用方而不是 SDK 里。

---

## 2. 安装与依赖

SDK 当前作为本仓库的内部 package 发布，包名为 `worker_sdk`，源码位于 [src/worker_sdk/](../src/worker_sdk/)。

```bash
# 在你的上游项目中（确保已经 conda activate legonanobot 或对应虚拟环境）
pip install -e /path/to/VibeTradingOpenCodeWorker
```

运行时依赖：

| 依赖 | 用途 |
|---|---|
| `httpx >= 0.27` | HTTP / SSE 底层传输 |
| `httpx-sse >= 0.4` | SSE 解析（id / event / data 拼接） |
| Python ≥ 3.11 | 用到了 `asyncio.TaskGroup`、PEP 604 类型语法 |

SDK 自身不依赖 FastAPI / Pydantic / SQLAlchemy —— 服务端模型不会反向耦合到上游。

---

## 3. 30 秒快速开始

```python
import asyncio
from worker_sdk import AsyncWorkerClient

async def main() -> None:
    async with AsyncWorkerClient(
        base_url="http://worker.internal:8080",
        bearer_token="<bearer>",
    ) as client:
        result = await client.create_and_wait(
            request={
                "mode": "direct_execute",
                "messages": [{"role": "user", "content": "给 add 函数写单测"}],
            },
            timeout=900,           # SDK 等待终态的上限（秒）
            raise_on_failure=True, # 非 completed 终态直接抛异常
        )
        print(result.final_status)        # "completed"
        print(result.task_snapshot)       # 完整 GET /tasks/:id 快照

asyncio.run(main())
```

`create_and_wait` = `create_task` + `wait_until_terminal`，是最常见的 "fire-and-forget + 拿结果" 场景。需要细粒度控制时拆开调用即可。

---

## 4. 核心概念

SDK 公开 4 个数据类（都是 `frozen=True` 的 dataclass，可哈希、可在并发中安全传递），定义在 [src/worker_sdk/models.py](../src/worker_sdk/models.py)。

### `WorkerTaskHandle`

```python
@dataclass(frozen=True)
class WorkerTaskHandle:
    task_id: str
    status: str   # 提交时返回的初始状态，如 "pending" / "queued"
```

`create_task()` 的返回值。轻量句柄，只够后续接口寻址。

### `WorkerEvent`

```python
@dataclass(frozen=True)
class WorkerEvent:
    cursor: int               # 任务内单调递增序号，等于 SSE 的 id
    kind: str                 # "task_started" / "hitl_required" / ...
    payload: dict[str, Any]   # 业务字段，schema 见 worker.contract.event
```

`stream_events()` 迭代出来的对象。`cursor` 用于断线后通过 `Last-Event-ID` 续传。

### `WorkerTerminalResult`

```python
@dataclass(frozen=True)
class WorkerTerminalResult:
    task_id: str
    final_status: str                       # completed / failed / aborted / timed_out
    terminal_event: WorkerEvent | None      # 触发终态的事件（可能 None：见下文 §8）
    task_snapshot: dict[str, Any]           # 终态后 GET /tasks/:id 的完整 JSON
```

`wait_until_terminal()` / `create_and_wait()` 的返回值。**始终**会带上 `task_snapshot`，因为事件 payload 不一定包含完整字段（如 `opencode_session_id`）。

### `WorkerArtifactRef`

```python
@dataclass(frozen=True)
class WorkerArtifactRef:
    artifact_id: str
    task_id: str
    type: str           # 服务端枚举的字符串值，避免泄露 enum 类型
    filename: str
    size: int | None
    created_at: float
    expires_at: float | None
    download_url: str | None
    metadata: dict[str, Any]
```

`list_artifacts()` 的元素。结合 `download_artifact_bytes()` / `download_artifact_to()` 拿到正文。

---

## 5. 构造客户端

```python
AsyncWorkerClient(
    *,
    base_url: str,
    bearer_token: str,
    timeout: float = 30.0,
    auto_reconnect_sse: bool = True,
    max_sse_reconnect_attempts: int = 5,
    compatibility_check: bool = True,
    retry_policy: RetryPolicy | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
)
```

| 参数 | 说明 |
|---|---|
| `base_url` | Worker 根地址，**不带尾斜杠**。如 `http://worker.internal:8080`。 |
| `bearer_token` | 必填、不能为空。SDK 在每个请求自动注入 `Authorization: Bearer ...`。 |
| `timeout` | 单次 HTTP 请求总超时（秒）。**不影响 SSE 的 read 超时**（SSE read=None）。 |
| `auto_reconnect_sse` | SSE 网络中断时是否自动用最近的 `cursor` 重连。 |
| `max_sse_reconnect_attempts` | SSE 重连上限；超出抛 [`WorkerSSEError`](#错误层次)。 |
| `compatibility_check` | 首次请求前是否调用 `/health` 校验 Worker 版本（详见 §10）。 |
| `retry_policy` | 普通 HTTP 请求的重试策略。`None` → 默认策略（见 §9）。`RetryPolicy.disabled()` → 完全关闭。 |
| `transport` | 可选 `httpx.AsyncBaseTransport`。主要用于测试注入 `httpx.ASGITransport(app=...)`，详见 §11。 |

### 生命周期

推荐 async context manager，自动释放底层 httpx 连接池：

```python
async with AsyncWorkerClient(...) as client:
    ...
```

也支持手动管理：

```python
client = AsyncWorkerClient(...)
try:
    ...
finally:
    await client.aclose()       # 多次调用安全
```

`aclose()` 之后客户端不应再使用；若需要新连接，构造新的 `AsyncWorkerClient`。

---

## 6. API 接口参考

下表给出全部公开方法 → Worker 端点的映射；详细参数见对应章节或 docstring。

| 方法 | HTTP 端点 | 失败时的典型异常 |
|---|---|---|
| `get_health()` | `GET /health` | `WorkerTransportError`, `WorkerServerError` |
| `assert_compatible()` | `GET /health` | `WorkerCompatibilityError` |
| `create_task(request)` | `POST /tasks` | `WorkerConflictError` (409 幂等冲突) |
| `get_task(task_id)` | `GET /tasks/{id}` | `WorkerNotFoundError` (404) |
| `abort_task(task_id)` | `POST /tasks/{id}/abort` | `WorkerNotFoundError`, `WorkerConflictError` |
| `stream_events(task_id, ...)` | `GET /tasks/{id}/events` (SSE) | `WorkerSSEError`, `WorkerTransportError` |
| `wait_until_terminal(task_id, ...)` | SSE + `GET /tasks/{id}` | `WorkerClientError`(SDK timeout), `WorkerTaskTerminalError` |
| `submit_decision(task_id, ...)` | `POST /tasks/{id}/decisions` | `WorkerNotFoundError`, `WorkerConflictError` |
| `list_artifacts(task_id)` | `GET /tasks/{id}/artifacts` | `WorkerNotFoundError` |
| `download_artifact_bytes(task_id, artifact_id)` | `GET /tasks/{id}/artifacts/{aid}` | `WorkerNotFoundError` |
| `download_artifact_to(task_id, artifact_id, dest)` | 同上（流式） | `FileExistsError`, `WorkerTransportError` |
| `create_and_wait(request, ...)` | `POST /tasks` + SSE + `GET /tasks/{id}` | 同 `create_task` ∪ `wait_until_terminal` |

### 6.1 提交任务

```python
handle = await client.create_task({
    "task_id": "user-supplied-uuid",   # 可选：用于幂等重提交
    "mode": "plan_first",
    "messages": [{"role": "user", "content": "..."}],
    "workspace": {"kind": "empty"},
    # ...其它字段见 worker.contract.task.TaskRequest
})
print(handle.task_id, handle.status)
```

- `request` 接受 **任意 JSON 可序列化字典**；SDK 不复用服务端 Pydantic 模型（决策 C4），调用方负责字段拼装。
- 服务端会校验 schema；非法字段返回 422 → 抛 `WorkerHTTPError(status_code=422)`。
- 同一 `task_id` 重复提交：服务端返回 409 → `WorkerConflictError`。

### 6.2 查询与中止

```python
snapshot = await client.get_task(task_id)
result = await client.abort_task(task_id)
```

- `get_task` 返回完整 `TaskResponse` JSON（dict），含 `status` / `created_at` / `opencode_session_id` 等。
- `abort_task` 在已终态的任务上返回 409 → `WorkerConflictError`（这通常是幂等保护，可以安全忽略）。

### 6.3 订阅事件流

见 §8。

### 6.4 等待终态

```python
result = await client.wait_until_terminal(
    task_id,
    timeout=1800,            # SDK 层最长等待秒数；None 表示无限
    raise_on_failure=False,  # True 时非 completed 终态会主动抛异常
)
```

实现路径（[src/worker_sdk/client.py](../src/worker_sdk/client.py) 中 `wait_until_terminal`）：

1. 内部 `stream_events()` 等到一个 `kind ∈ TERMINAL_EVENT_KINDS` 的事件。
2. 收到后再 `get_task()` 拿一次完整快照；事件 payload 不一定带 `opencode_session_id` 之类的字段。
3. 即使 SSE 在重连耗尽后才推完终态、SDK 没拿到 terminal event，也会兜底用 snapshot 的 `status` 作为 `final_status` ——  状态机里 `final_status` 是真实事实，事件只是通知通道。

⚠️ **注意区分两个 timeout**：
- `timeout=` 参数 → SDK 在客户端等待的上限，超时抛 `WorkerClientError`。
- 服务端 `resource_limits.timeout_sec` → 服务端任务自身超时，会推 `task_timed_out` 事件，`final_status="timed_out"`。

### 6.5 提交 HITL 决策

```python
await client.submit_decision(
    task_id,
    decision_id="dec_abc",   # 来自 hitl_required 事件 payload
    choice="approve",        # approve / reject / revise / abort
    feedback="LGTM",         # 可选；revise 时通常必填
    patch={"plan_step_3": "改用 polars"},  # 可选结构化修订
    idempotency_key=None,    # None → SDK 自动生成 UUIDv4
)
```

- `idempotency_key` 缺省时 SDK 用 `uuid.uuid4()` 自动生成；重复点击 / 网络重试不会重复消费决策。
- 如果你需要 "外部系统重发同一决策" 的幂等性（如 webhook 重试），自己传一个稳定的 key。
- 服务端会校验 `decision_id` 是否仍在 `awaiting_human` 状态；已处理的决策返回 409。

### 6.6 Artifacts

```python
refs = await client.list_artifacts(task_id)
for ref in refs:
    print(ref.artifact_id, ref.type, ref.size)

# 小产物（log / transcript）：拿 bytes
data = await client.download_artifact_bytes(task_id, refs[0].artifact_id)

# 大产物（workspace_snapshot）：流式落盘
path = await client.download_artifact_to(
    task_id,
    refs[0].artifact_id,
    dest_path="/tmp/snapshot.tar.gz",
    overwrite=False,            # True → 覆盖已存在文件
)
```

`download_artifact_to` 的行为细节：

- 父目录不存在会自动创建（`mkdir -p`）。
- 落盘走 `*.part` 临时文件，写完后 `os.replace()` 原子重命名 → 失败/取消时不会留下半成品。
- 流式 `aiter_bytes`，避免大文件全量进内存。

`artifact_id` 经过格式校验（`[A-Za-z0-9_.\-]+`）；不允许 `../` 之类的穿越路径，提前过滤避免无意义的 4xx。

---

## 7. 错误模型

错误类层次定义在 [src/worker_sdk/errors.py](../src/worker_sdk/errors.py)：

```text
WorkerClientError                                  # 所有 SDK 异常的基类
├── WorkerTransportError                            # 网络层（连接拒绝 / 超时 / 断流）
├── WorkerCompatibilityError                        # /health.version 不在支持矩阵
├── WorkerSSEError                                  # SSE 解析失败 / 重连耗尽
├── WorkerHTTPError(status_code, response_body)     # 服务端非 2xx 的统一基类
│   ├── WorkerUnauthorizedError                     # 401
│   ├── WorkerNotFoundError                         # 404
│   ├── WorkerConflictError                         # 409
│   └── WorkerServerError                           # 5xx
└── WorkerTaskTerminalError(task_id, final_status,
                              terminal_event,
                              task_snapshot)
    ├── WorkerTaskFailed                            # final_status == "failed"
    ├── WorkerTaskAborted                           # final_status == "aborted"
    └── WorkerTaskTimedOut                          # final_status == "timed_out"
```

### 7.1 选哪一层 except

| 你想做的事 | 应该 except 什么 |
|---|---|
| 仅在网络故障时退避并重试 | `WorkerTransportError`（4xx/5xx 不会落入此分支） |
| 区别处理 "任务不存在" | `WorkerNotFoundError` |
| 区别处理 "任务已终态导致 abort 失败" | `WorkerConflictError` |
| 监控所有 5xx | `WorkerServerError` |
| 兜底所有 HTTP 错误 | `WorkerHTTPError`（含 `.status_code` 与 `.response_body`） |
| 兜底所有 SDK 异常 | `WorkerClientError` |
| 任务非 completed 终态时短路 | `WorkerTaskTerminalError`（`raise_on_failure=True` 时才会抛） |

### 7.2 终态错误的两种暴露方式

- **默认（`raise_on_failure=False`）**：`wait_until_terminal()` 返回 `WorkerTerminalResult`，由调用方检查 `final_status` 自行决定后续动作。适合需要在失败时仍下载 logs/snapshot 的场景。
- **`raise_on_failure=True`**：非 completed 直接抛 `WorkerTaskFailed` / `WorkerTaskAborted` / `WorkerTaskTimedOut`，异常对象上带完整的 `task_snapshot` 与 `terminal_event`。适合 fire-and-forget 风格。

### 7.3 何时透传给上游

SDK 的异常已经是 "无法在 SDK 层修复的失败" —— 上游通常只需要：

1. 区分 transport / HTTP / terminal 三层来决定要不要重试或上报。
2. 把 `task_id` 写进日志/告警上下文。
3. 对外抛你自己的 domain error（不要把 `WorkerClientError` 直接泄漏到 HTTP API）。

---

## 8. SSE 事件流与断线重连

`stream_events()` 是 SDK 的核心异步迭代器：

```python
async for event in client.stream_events(
    task_id,
    last_event_id=None,         # 首次订阅传 None；继续断点续传时传上次最后的 cursor
    include_heartbeats=False,   # True 才会 yield kind="heartbeat" 的保活事件
    auto_resume=None,           # None → 用构造时的 auto_reconnect_sse；可单次覆盖
):
    print(event.cursor, event.kind, event.payload)
    # 终态事件后迭代器会自然结束 —— 无需手动 break
```

实现细节（[src/worker_sdk/sse.py](../src/worker_sdk/sse.py)）：

1. 用 `httpx.AsyncClient.stream("GET", url, headers=...)` 拿到 chunked 响应。
2. 用 `httpx_sse.EventSource` 解析 `id / event / data`，处理 multi-line `data:`、CRLF、注释行。
3. 维护 `last_cursor`；网络层失败（`httpx.HTTPError`）抛成 `WorkerTransportError`，触发重连分支。
4. 重连时把 `last_cursor` 放进 `Last-Event-ID` 头，服务端从 DB 补发 `event_id > cursor` 的事件再切实时。
5. 收到任一 `kind ∈ {task_completed, task_failed, task_aborted, task_timed_out}` → 迭代器 return。
6. 重连次数超过 `max_sse_reconnect_attempts` → 抛 `WorkerSSEError`。

### 8.1 何时 `terminal_event` 会是 None

正常路径下 `wait_until_terminal()` 返回的 `terminal_event` 不为 None。但有两种边界 case 会让它退化为 None，此时 `final_status` 仍来自 `get_task()` 的真实快照：

- **重连耗尽 → SSE 抛 `WorkerSSEError`**：`stream_events()` 已经被异常打断，但服务端实际已经把任务推到了终态。SDK 在 `wait_until_terminal()` 里仍会兜底 `get_task()`，但这条路径会先把异常抛出去。
- **`auto_resume=False` 时单次连接断开**：迭代器静默 return，没有 terminal event 但也没异常 —— 由调用方再 `get_task()` 自行确认。

### 8.2 心跳与终态的关系

- `heartbeat` 事件没有 DB 行，因此 `cursor` 字段不可用 —— SDK 在 [sse.py 中 `_parse_sse_event`](../src/worker_sdk/sse.py) 直接把无 `id` 的事件跳过。
- `include_heartbeats=True` 时仍会 yield，但其 `cursor` 等于"上一条业务事件的 cursor" 还是 0 取决于服务端实现，不要依赖它做去重。
- **不要** 用 "心跳间隔超时" 推断任务失败。SDK 不做这件事 —— 心跳缺失就是 transport 故障，会通过 `WorkerTransportError` / 重连分支暴露。

### 8.3 自己消费 vs `wait_until_terminal`

| 场景 | 推荐 |
|---|---|
| 只关心终态结果 | `wait_until_terminal()` |
| 需要把事件转发到日志/指标系统、UI、Slack | 自己 `async for event in client.stream_events(...)` |
| 需要 plan_first 的 HITL 交互 | 自己迭代，遇到 `hitl_required` 时调用 `submit_decision` |

混合用法见 [worker-sdk-examples.md](worker-sdk-examples.md) 的 "plan_first + HITL" 与 "事件转发到日志" 两个示例。

---

## 9. HTTP 重试策略

定义在 [src/worker_sdk/retry.py](../src/worker_sdk/retry.py)。SSE 的重连**与本策略独立**，不要混用。

### 9.1 默认行为

```python
RetryPolicy(
    max_attempts=3,
    initial_backoff_sec=0.5,
    max_backoff_sec=8.0,
    backoff_multiplier=2.0,
    jitter_ratio=0.25,
    retry_on_5xx=True,
    retry_on_transport_error=True,
    respect_retry_after=True,
)
```

并且：

- **GET / HEAD 默认启用重试**，POST / PUT / DELETE 默认 **不重试**。
- 只对 5xx 与 `WorkerTransportError` 重试；4xx (401/404/409/422) 永不重试 —— 重试一次只会得到同一结果。
- 5xx 路径下若响应带 `Retry-After`（仅支持 delta-seconds），优先采用服务端建议（取 `max(server, local)`）。

POST 默认不重试是因为它们多数非幂等：`create_task` 不带 `task_id` 时会 fork 出新任务，`submit_decision` 不带 `idempotency_key` 时会重复消费决策。当你 **确认请求是幂等的**，可以在调用层手动包一层重试，或考虑为这些请求显式传 `task_id` / `idempotency_key`。

### 9.2 自定义策略

```python
from worker_sdk import AsyncWorkerClient, RetryPolicy

policy = RetryPolicy(
    max_attempts=5,
    initial_backoff_sec=1.0,
    max_backoff_sec=30.0,
    jitter_ratio=0.5,
    retry_on_5xx=True,
)

async with AsyncWorkerClient(..., retry_policy=policy) as client:
    ...
```

完全关闭：

```python
async with AsyncWorkerClient(..., retry_policy=RetryPolicy.disabled()) as client:
    ...
```

`RetryPolicy` 是 `frozen=True` 的 dataclass，构造时校验所有字段：`max_attempts >= 1`、`initial_backoff_sec >= 0`、`jitter_ratio ∈ [0, 1]` 等。非法值在构造时直接 `ValueError`，不会拖到运行时才暴露。

### 9.3 抖动公式

第 `n` 次失败后，第 `n+1` 次重试前的等待秒数：

```text
base   = initial_backoff_sec * (backoff_multiplier ** (n - 1))
capped = min(base, max_backoff_sec)
wait   ∈ [capped * (1 - jitter_ratio), capped * (1 + jitter_ratio)]   # 均匀分布
```

参考 AWS SDK 的 full-jitter 思路；目的是避免大量客户端在同一时刻退避同一时长，造成 thundering herd。

---

## 10. 兼容性检查

```python
async with AsyncWorkerClient(..., compatibility_check=True) as client:
    # 首次"非 /health" 请求前，SDK 会自动 GET /health，校验 version 在矩阵内
    handle = await client.create_task(...)
```

- 当前矩阵：`SDK 0.1.x ↔ Worker 0.1.x`（参见 [src/worker_sdk/compat.py](../src/worker_sdk/compat.py)）。
- 兼容检查只跑一次；首次成功后短路。
- 失败 → `WorkerCompatibilityError`，此时 SDK 会把"已检查"标记退回，后续调用还会再次触发检查（避免一次失败让 SDK 永久跳过校验）。
- 需要忽略：在构造时传 `compatibility_check=False`，并用 `assert_compatible()` 自行决定时机。

不要把 `/health` 当业务可用性指标 —— 它不依赖鉴权头，仅反映服务进程是否 ready。

---

## 11. 测试时注入 ASGI Transport

写集成/契约测试时，可以用 `httpx.ASGITransport` 把内存里的 FastAPI app 直接挂到 SDK 上，**不需要起真实端口**：

```python
import httpx
from fastapi import FastAPI
from worker_sdk import AsyncWorkerClient

app = FastAPI()

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "0.1.0"}

@app.post("/tasks", status_code=201)
async def create_task() -> dict:
    return {"task_id": "t1", "status": "queued"}

async def test_create():
    transport = httpx.ASGITransport(app=app)
    async with AsyncWorkerClient(
        base_url="http://stub",
        bearer_token="t",
        transport=transport,
    ) as client:
        handle = await client.create_task({...})
        assert handle.task_id == "t1"
```

实战示例参考 [tests/unit/test_worker_sdk.py](../tests/unit/test_worker_sdk.py) 中的 `StubWorker` —— 它覆盖了 `/health`、`/tasks`、`/tasks/{id}/events`（含 SSE 重连脚本）、`/tasks/{id}/decisions`、artifact 下载等完整协议路径。

---

## 12. 最佳实践与常见陷阱

### 12.1 用 async context manager 管理生命周期
不用 `async with` 时务必 `await client.aclose()`，否则 httpx 连接池泄漏（pytest 中会冒出 "Unclosed connection" 警告）。

### 12.2 显式提供 `task_id` 和 `idempotency_key`
- 重要任务自己生成 `task_id`，写日志时一起记录 → 重提交安全。
- HITL 决策从 webhook / 消息队列触发时，把 webhook 的 message id 当 `idempotency_key`。

### 12.3 不要把 SDK 异常泄漏到 HTTP API
SDK 异常带服务端响应体，可能含敏感信息。在你的边界处包一层 domain error：

```python
try:
    result = await client.wait_until_terminal(task_id, raise_on_failure=True)
except WorkerTaskFailed as e:
    raise MyDomainError(f"task {e.task_id} failed", cause=e) from e
```

### 12.4 SSE 长连接的 `timeout`
`AsyncWorkerClient(timeout=30)` 只控制 **连接握手** 与普通 HTTP；SSE 的 read timeout 永远是 None。如果你想让 "迟迟不到终态" 早点抛错，用 `wait_until_terminal(task_id, timeout=...)`，不要去改 client 的 `timeout`。

### 12.5 `download_artifact_bytes` vs `download_artifact_to`
- 小产物（< 5 MB） → `download_artifact_bytes` 更方便。
- `workspace_snapshot.tar.gz` 这种几百 MB 的 → 必须 `download_artifact_to`，否则 OOM。

### 12.6 区分 SDK 等待超时 vs 服务端任务超时
看到 `WorkerClientError("wait_until_terminal timed out after 60s ...")` → **是 SDK 没等到，不代表任务失败**。任务可能仍在执行，可以再 `get_task()` 看真实状态，或继续 `stream_events(last_event_id=...)`。

### 12.7 重试 POST 时的安全前提
默认不对 POST 重试是为了保护非幂等接口。**只有当你能保证调用是幂等的**（自己提供了 `task_id` / `idempotency_key`），才适合调用层手动包一层重试。

### 12.8 不要依赖 cursor 跨任务
`cursor` 是 **任务内** 单调递增的；不同 task 之间没有可比性。重连必须用同一个 `task_id` + 该任务的 `last_event_id`。

### 12.9 关于 `/health` 不带鉴权
SDK 仍统一附加 `Authorization` 头，这是为了实现简化；服务端会忽略。不要据此推断 "其它端点也免鉴权"。

---

## 附录 A：完整方法签名速查

```python
class AsyncWorkerClient:
    def __init__(self, *, base_url, bearer_token,
                 timeout=30.0,
                 auto_reconnect_sse=True,
                 max_sse_reconnect_attempts=5,
                 compatibility_check=True,
                 retry_policy=None,
                 transport=None) -> None: ...

    async def __aenter__(self) -> "AsyncWorkerClient": ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...
    async def aclose(self) -> None: ...

    async def get_health(self) -> dict[str, Any]: ...
    async def assert_compatible(self) -> None: ...

    async def create_task(self, request: dict[str, Any]) -> WorkerTaskHandle: ...
    async def get_task(self, task_id: str) -> dict[str, Any]: ...
    async def abort_task(self, task_id: str) -> dict[str, Any]: ...

    def stream_events(self, task_id: str, *,
                      last_event_id: int | None = None,
                      include_heartbeats: bool = False,
                      auto_resume: bool | None = None
                     ) -> AsyncIterator[WorkerEvent]: ...

    async def wait_until_terminal(self, task_id: str, *,
                                  timeout: float | None = None,
                                  raise_on_failure: bool = False
                                 ) -> WorkerTerminalResult: ...

    async def submit_decision(self, task_id: str, *,
                              decision_id: str,
                              choice: str,
                              feedback: str | None = None,
                              patch: dict[str, Any] | None = None,
                              idempotency_key: str | None = None
                             ) -> dict[str, Any]: ...

    async def list_artifacts(self, task_id: str) -> list[WorkerArtifactRef]: ...
    async def download_artifact_bytes(self, task_id: str,
                                      artifact_id: str) -> bytes: ...
    async def download_artifact_to(self, task_id: str, artifact_id: str,
                                   dest_path: str, *,
                                   overwrite: bool = False) -> str: ...

    async def create_and_wait(self, request: dict[str, Any], *,
                              timeout: float | None = None,
                              raise_on_failure: bool = False
                             ) -> WorkerTerminalResult: ...
```

## 附录 B：导出符号

```python
from worker_sdk import (
    AsyncWorkerClient,
    RetryPolicy,
    # 数据模型
    WorkerArtifactRef,
    WorkerEvent,
    WorkerTaskHandle,
    WorkerTerminalResult,
    # 错误
    WorkerClientError,
    WorkerCompatibilityError,
    WorkerConflictError,
    WorkerHTTPError,
    WorkerNotFoundError,
    WorkerSSEError,
    WorkerServerError,
    WorkerTaskAborted,
    WorkerTaskFailed,
    WorkerTaskTerminalError,
    WorkerTaskTimedOut,
    WorkerTransportError,
    WorkerUnauthorizedError,
)
```
