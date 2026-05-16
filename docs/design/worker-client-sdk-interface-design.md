# Design: Worker Client SDK Interface

> **状态**：Draft v1（2026-05-16）
> **来源**：基于当前 Worker HTTP API、contract schema、SSE/HITL 行为与上游集成讨论收敛
> **目的**：定义一套面向上游 runtime 的薄 Python SDK 接口，屏蔽 Worker 协议细节，但**不发明新的服务端能力**
> **范围**：SDK 的公开接口、错误模型、SSE 重连语义、版本边界、实现切分
>
> **不在范围**：
> - Worker 服务端接口改造
> - 多语言 SDK
> - 业务层 strategy / vibe-trading 抽象
> - 多租户控制面、RBAC、计费

---

## 0. TL;DR

建议为 Worker 写一套 **薄的、内部使用的、Python async-first SDK**。

这套 SDK 的职责不是替上游做任务编排，而是把当前 Worker 协议里最容易重复和写错的部分收口：

1. Bearer 认证
2. 任务创建与查询
3. SSE 事件订阅与断线重连
4. 终态等待与终态错误映射
5. HITL decision 提交
6. artifact 枚举与下载

SDK **不应**：

1. 发明新的 Worker endpoint
2. 内嵌 strategy / MCP / agent 业务语义
3. 做复杂 workflow DSL
4. 提前抽象多租户控制面

---

## 1. 背景与问题

当前 Worker 暴露的是一组任务级 HTTP + SSE 接口，而不是单纯的 `prompt_async` 包装：

- `POST /tasks`
- `GET /tasks/{task_id}`
- `GET /tasks/{task_id}/events`
- `POST /tasks/{task_id}/decisions`
- `POST /tasks/{task_id}/abort`
- `GET /tasks/{task_id}/artifacts`
- `GET /tasks/{task_id}/artifacts/{artifact_id}`

如果上游直接用裸 `httpx` 调这些接口，每个调用方都需要重复处理：

1. Bearer token 注入
2. Pydantic request/response 序列化
3. SSE 解析
4. `Last-Event-ID` 断线重连
5. terminal event 判定
6. `failed / aborted / timed_out` 的差异处理
7. HITL decision 提交与幂等 key 生成
8. artifact 下载路径与文件流保存

这些复杂度本身没有业务价值，但会持续污染上游 runtime。

因此需要一层 SDK，把 **Worker 协议复杂度** 封装掉，而不是把 **业务编排逻辑** 下沉进去。

---

## 2. 设计目标

| # | 目标 | 说明 |
|---|---|---|
| G1 | 封装 Worker 协议复杂度 | 上游不再手写 task/event/decision/artifact 的 HTTP 细节 |
| G2 | 不新增服务端能力 | SDK 只包装现有 API，不推动服务端为 SDK 特化 |
| G3 | 薄抽象 | 暴露 Worker 原生概念，不引入 workflow DSL |
| G4 | async-first | SSE、重连、流式 artifact 更适合 async 接口 |
| G5 | 明确错误边界 | transport error、HTTP error、terminal task error 分层 |
| G6 | 可渐进演进 | 先服务单个上游 runtime，后续再评估是否独立发包 |

---

## 3. 非目标

以下内容明确**不属于 MVP SDK**：

1. 同步版 client
2. JavaScript / Go / Java 多语言实现
3. strategy registry、scheduler、meta-skill 等业务接口
4. tenant secret store、quota、RBAC 抽象
5. 通用 event bus / callback plugin system
6. 自动审批策略引擎
7. OpenAPI codegen 驱动的超厚客户端

---

## 4. 关键决策

| 决策 | 选择 | 不选项 | 理由 |
|---|---|---|---|
| C1. 语言 | Python | 立即多语言 | 当前 Worker 与主要上游都在 Python 生态 |
| C2. 接口风格 | async-first | sync-first | SSE/流式下载/重连天然是 async 场景 |
| C3. SDK 定位 | 内部薄 SDK | 公共重量 SDK | 当前 Worker API 仍在演进，先稳内部接口 |
| C4. 模型来源 | SDK 自己声明公开模型 | 直接复用服务端全部模块 | 避免上游依赖 FastAPI/Docker/server internals |
| C5. 重连语义 | SDK 内建 SSE 断线重连 | 交给上游自己做 | 这是最容易写错、最值得收口的协议细节 |
| C6. terminal 行为 | 默认返回终态结果；可选 strict 抛异常 | 永远抛异常 | 上游有时需要保留失败态对象而不是异常短路 |
| C7. 版本策略 | 与 Worker `0.1.x` 小版本兼容矩阵绑定 | 无版本检查 | 当前无正式 API versioning，需显式约束 |

---

## 5. 对外接口概览

### 5.1 包结构

建议 SDK 目录结构：

```text
worker_sdk/
├── __init__.py
├── client.py              # AsyncWorkerClient 公开入口
├── models.py              # request/response/event/artifact/terminal result
├── errors.py              # transport/http/terminal errors
├── sse.py                 # SSE 解析与自动重连
├── auth.py                # Bearer token 注入
└── compat.py              # /health 版本检查与兼容矩阵
```

### 5.2 公开类

MVP 只暴露一个主类：

```python
class AsyncWorkerClient:
    ...
```

辅助公开类型：

```python
class WorkerTaskHandle: ...
class WorkerEvent: ...
class WorkerTerminalResult: ...
class WorkerArtifactRef: ...
```

---

## 6. 核心数据模型

### 6.1 WorkerTaskHandle

表示一次已经提交到 Worker 的任务句柄。

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerTaskHandle:
    task_id: str
    status: str
```

### 6.2 WorkerEvent

SDK 暴露给上游的统一事件对象。

注意：当前服务端 SSE 并**不会**直接把 `ts` 一起推出来，因此 SDK 事件对象只保证以下字段：

```python
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkerEvent:
    cursor: int
    kind: str
    payload: dict[str, Any]
```

### 6.3 WorkerTerminalResult

用于 `wait_until_terminal()` 的统一返回。

```python
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkerTerminalResult:
    task_id: str
    final_status: str                # completed | failed | aborted | timed_out
    terminal_event: WorkerEvent | None
    task_snapshot: dict[str, Any]
```

### 6.4 WorkerArtifactRef

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerArtifactRef:
    artifact_id: str
    task_id: str
    type: str
    filename: str
    size: int | None
    created_at: float
    expires_at: float | None
    download_url: str | None
```

---

## 7. AsyncWorkerClient 接口定义

### 7.1 初始化

```python
class AsyncWorkerClient:
    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        timeout: float = 30.0,
        auto_reconnect_sse: bool = True,
        max_sse_reconnect_attempts: int = 5,
        compatibility_check: bool = True,
    ) -> None:
        ...
```

参数说明：

| 参数 | 说明 |
|---|---|
| `base_url` | Worker 服务地址，例如 `http://worker.internal:8080` |
| `bearer_token` | Worker Bearer token |
| `timeout` | 普通 HTTP 请求超时 |
| `auto_reconnect_sse` | 是否自动重连 SSE |
| `max_sse_reconnect_attempts` | SSE 最大重连次数 |
| `compatibility_check` | 初始化或首次请求前是否校验 `/health.version` |

### 7.2 健康与兼容性

```python
async def get_health(self) -> dict: ...
async def assert_compatible(self) -> None: ...
```

语义：

1. `get_health()` 直接读取 `/health`
2. `assert_compatible()` 检查 Worker 版本是否在 SDK 支持矩阵中

建议初版兼容策略：

```text
SDK 0.1.x 仅声明兼容 Worker 0.1.x
```

### 7.3 任务创建与查询

```python
async def create_task(self, request: dict) -> WorkerTaskHandle: ...

async def get_task(self, task_id: str) -> dict: ...

async def abort_task(self, task_id: str) -> dict: ...
```

设计说明：

1. MVP 接受 `dict` 或 Pydantic-compatible object，避免过早绑定服务端内部 model
2. 返回结果保持接近服务端响应，不做业务裁剪

### 7.4 事件流订阅

```python
async def stream_events(
    self,
    task_id: str,
    *,
    last_event_id: int | None = None,
    include_heartbeats: bool = False,
    auto_resume: bool | None = None,
) -> AsyncIterator[WorkerEvent]:
    ...
```

语义要求：

1. 使用 `GET /tasks/{task_id}/events`
2. 若指定 `last_event_id`，通过 `Last-Event-ID` 头请求补发历史事件
3. 当网络中断且 `auto_resume=True` 时，SDK 用最近一个 `cursor` 自动重连
4. 默认忽略 `heartbeat`，除非显式要求
5. 收到 terminal event 后，迭代器自然结束

### 7.5 终态等待

```python
async def wait_until_terminal(
    self,
    task_id: str,
    *,
    timeout: float | None = None,
    raise_on_failure: bool = False,
) -> WorkerTerminalResult:
    ...
```

行为：

1. 内部基于 `stream_events()` 等待 terminal event
2. 拿到终态后再调用一次 `get_task()` 获取最终 task snapshot
3. 若 `raise_on_failure=True` 且终态不是 `completed`，抛专门异常

### 7.6 决策提交

```python
async def submit_decision(
    self,
    task_id: str,
    *,
    decision_id: str,
    choice: str,
    feedback: str | None = None,
    patch: dict | None = None,
    idempotency_key: str | None = None,
) -> dict:
    ...
```

语义：

1. 调用 `POST /tasks/{task_id}/decisions`
2. 若未提供 `idempotency_key`，SDK 自动生成 UUID
3. SDK 不代替上游做 decision policy，只负责协议提交

### 7.7 artifact 接口

```python
async def list_artifacts(self, task_id: str) -> list[WorkerArtifactRef]: ...

async def download_artifact_bytes(
    self,
    task_id: str,
    artifact_id: str,
) -> bytes: ...

async def download_artifact_to(
    self,
    task_id: str,
    artifact_id: str,
    dest_path: str,
    *,
    overwrite: bool = False,
) -> str: ...
```

说明：

1. 同时提供 bytes 模式与文件落盘模式
2. 文件落盘模式负责目录创建和 overwrite 保护
3. 不在 SDK 中推断 artifact 业务语义，只暴露 `type/filename` 等元数据

### 7.8 高阶便利方法

MVP 仅保留一个薄 convenience helper：

```python
async def create_and_wait(
    self,
    request: dict,
    *,
    timeout: float | None = None,
    raise_on_failure: bool = False,
) -> WorkerTerminalResult:
    ...
```

它等价于：

1. `create_task()`
2. `wait_until_terminal()`

不再提供更高阶的 workflow 组合，防止 SDK 侵入上游调度逻辑。

---

## 8. 错误模型

### 8.1 错误分层

```python
class WorkerClientError(Exception): ...

class WorkerTransportError(WorkerClientError): ...      # DNS / connect / timeout / broken pipe
class WorkerHTTPError(WorkerClientError): ...           # 非 2xx HTTP
class WorkerUnauthorizedError(WorkerHTTPError): ...     # 401
class WorkerNotFoundError(WorkerHTTPError): ...         # 404
class WorkerConflictError(WorkerHTTPError): ...         # 409
class WorkerServerError(WorkerHTTPError): ...           # 5xx
class WorkerCompatibilityError(WorkerClientError): ...  # /health version mismatch
class WorkerSSEError(WorkerClientError): ...            # SSE parse / reconnect exhausted

class WorkerTaskTerminalError(WorkerClientError): ...
class WorkerTaskFailed(WorkerTaskTerminalError): ...
class WorkerTaskAborted(WorkerTaskTerminalError): ...
class WorkerTaskTimedOut(WorkerTaskTerminalError): ...
```

### 8.2 终态抛错策略

`wait_until_terminal(..., raise_on_failure=True)` 时：

| 终态 | 抛错 |
|---|---|
| `completed` | 不抛 |
| `failed` | `WorkerTaskFailed` |
| `aborted` | `WorkerTaskAborted` |
| `timed_out` | `WorkerTaskTimedOut` |

异常对象应携带：

1. `task_id`
2. `final_status`
3. `terminal_event`
4. `task_snapshot`

这样上游既能简单 `except`，也能拿到足够上下文做审计或重试判断。

---

## 9. SSE 重连设计

### 9.1 设计原则

SDK 必须把 SSE 重连做成内建能力，因为这是 Worker 协议最容易出错的部分。

### 9.2 重连算法

```text
1. 首次连接：不带 Last-Event-ID 或使用调用方给定值
2. 每收到一条事件，记录其 cursor
3. 若连接意外断开：
   - 若已收到 terminal event：直接结束
   - 否则使用最近 cursor 作为 Last-Event-ID 重连
4. 达到最大重连次数仍失败：抛 WorkerSSEError
```

### 9.3 不做的事

SDK 不做：

1. 无限重连
2. 心跳超时推断任务失败
3. 本地持久化 cursor
4. 多订阅者共享一个本地 event bus

这些都超出“薄 SDK”边界。

---

## 10. 认证与请求头

所有请求统一注入：

```text
Authorization: Bearer <token>
```

SDK 还应在内部统一设置：

```text
Content-Type: application/json
Accept: application/json
```

对于 SSE：

```text
Accept: text/event-stream
Last-Event-ID: <cursor>   # 若需要补发/重连
```

---

## 11. 与多租户的关系

SDK **不负责**多租户控制；tenant/user 识别仍然属于上游 runtime。

但 SDK 需要支持把租户上下文自然透传进 Worker 请求：

```python
request["metadata"] = {
    "trace_id": "...",
    "tenant_hint": "tenant-a",
    "extra": {"user_id": "u-123"},
}
```

也就是说：

1. 多租户边界在上游
2. SDK 只是把租户上下文作为普通 task metadata 透传
3. SDK 不做 tenant policy、tenant secret、tenant rate limit 抽象

---

## 12. 示例用法

### 12.1 最小调用

```python
from worker_sdk import AsyncWorkerClient


async with AsyncWorkerClient(
    base_url="http://worker.internal:8080",
    bearer_token="...",
) as client:
    handle = await client.create_task({
        "mode": "plan_first",
        "messages": [{"role": "user", "content": "给 add 函数写单测"}],
    })

    result = await client.wait_until_terminal(
        handle.task_id,
        raise_on_failure=True,
    )

    artifacts = await client.list_artifacts(handle.task_id)
```

### 12.2 手动消费事件流与 HITL

```python
async for event in client.stream_events(task_id):
    if event.kind == "hitl_required":
        await client.submit_decision(
            task_id,
            decision_id=event.payload["decision_id"],
            choice="approve",
        )
    elif event.kind in {"task_completed", "task_failed", "task_aborted", "task_timed_out"}:
        break
```

### 12.3 artifact 下载

```python
await client.download_artifact_to(
    task_id,
    artifact_id,
    dest_path="/tmp/transcript.json",
    overwrite=True,
)
```

---

## 13. 实现建议

### 13.1 MVP 实现顺序

建议按以下顺序实现：

1. `AsyncWorkerClient.__init__` + 认证头注入
2. `get_health()` / `assert_compatible()`
3. `create_task()` / `get_task()` / `abort_task()`
4. `stream_events()` + 自动重连
5. `wait_until_terminal()`
6. `submit_decision()`
7. `list_artifacts()` / `download_artifact_*()`
8. `create_and_wait()` convenience helper

### 13.2 技术选型

建议使用：

1. `httpx.AsyncClient` 做普通 HTTP 请求
2. 原生 SSE 解析或轻量封装，不引入复杂依赖
3. `dataclasses` 或轻量 Pydantic model 做公开结果类型

### 13.3 测试策略

SDK 测试建议分三层：

1. 纯单元测试：错误映射、SSE parser、重连逻辑
2. 基于 Worker stub server 的协议测试
3. 与真实 Worker 的窄集成测试

---

## 14. 已知限制

当前服务端协议对 SDK 有这些限制：

1. SSE 事件不直接携带 `ts`
2. 无显式 API version endpoint，只有 `/health.version`
3. 无批量 artifact 下载接口
4. 无 server-side long-poll 任务等待接口，必须基于 SSE 或轮询自行实现
5. 无专门的 decision query endpoint，SDK 应以事件驱动为主

这些限制都**不阻塞**薄 SDK 落地，但决定了 SDK 必须保持克制，不能伪装成一个“更智能的控制面”。

---

## 15. 后续演进点

当以下条件满足后，再考虑把 SDK 升级为独立仓库或公共包：

1. Worker API 在两个以上上游调用方之间稳定
2. HITL `continue/escalate`、`auto_approve` 等接口语义稳定
3. W2 interceptors 完成，artifact 类型稳定
4. 至少形成一版明确的 compatibility matrix

到那个阶段，再评估：

1. 是否抽出共享 `worker-protocol` 包
2. 是否提供 sync wrapper
3. 是否支持多语言 SDK

在这之前，SDK 应保持为一套**内部薄封装**。

---

## 16. 结论

应该写 Worker client SDK，但只应该写成：

1. Python
2. async-first
3. 内部使用
4. 面向任务协议
5. 不新增服务端能力

它的核心价值是：

1. 把 SSE/HITL/terminal/artifact 这些重复协议细节从上游 runtime 中剥离出去
2. 保持 Worker 的服务端边界稳定
3. 让未来多租户、调度、审计等上游能力在更干净的调用面上演进

— end —