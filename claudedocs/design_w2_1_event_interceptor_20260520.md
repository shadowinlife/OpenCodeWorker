# Design: W2-1 `EventInterceptor` 基类

> **Status**: Draft v1（2026-05-20）—— spec only, no implementation
> **Type**: Component / interface specification
> **Anchored to code**: `src/worker/adapters/opencode/driver.py`（commit `33fe197`）/ `event_stream.py` / `contract/event.py`
> **Scope**: 设计 W2-2 / W2-3 / W2-4 三个拦截器**共享**的基础设施层；不实现任何具体业务拦截器
> **Out of scope**: ConversationsWriter / BacktestInterceptor / McpFieldRecorder 自身的设计（见各自 design doc）

---

## 0. TL;DR

`EventInterceptor` 是一个**业务无关**的抽象基类，提供 4 个能力：

1. 接收 driver 在 SSE 主循环中归一化前的**原始** opencode 事件 + 归一化后的 `NormalizedEvent`
2. 接收任务终态信号（含具体终态类别 + reason）
3. 在终态时执行 flush，可选返回一个 `Artifact` 注册到 DB
4. 任意 hook 抛错对 driver 主流程 / 兄弟拦截器 **零影响**（隔离 + 记录 + 计数）

driver 通过构造器持有一个**有序** `list[EventInterceptor]`，默认空列表；调用方（orchestrator / opencode_profile）显式注入实例。基类不感知 SKILL / strategy / vibe-trading 任何业务概念。

---

## 1. 设计目标 & 不变量

### 1.1 Goals

| # | Goal | Why |
|---|---|---|
| G1 | 为 W2-2/3/4 提供同一套 SSE 事件订阅、终态回调、产物登记机制 | design §5.3 "三个 hook 共享 SSE 拦截基础设施" |
| G2 | driver 主流程（HITL / artifact / abort）**零行为变化** | 不允许拦截器影响 task 终态语义 |
| G3 | 拦截器异常**完全隔离**：单个拦截器抛错不影响 SSE 循环、不影响兄弟拦截器、不影响终态写入 | Phase 6 reliability invariant |
| G4 | 拦截器可声明性配置（构造器注入），可被 unit test 单独驱动 | 可测试 + 可独立演进 |
| G5 | 业务无关：基类、注入点、数据模型不出现 `vibe-trading` / `strategy` / `signal_engine` / `ma250` / `skill` 任何字符串 | architecture invariant from design §11.3 |

### 1.2 Invariants（实施期不能破）

1. **driver 主路径优先级最高**：抛错隔离 / I/O 异步化 / 不阻塞 SSE 主循环
2. **顺序保证**：同一拦截器的 `on_event` 调用顺序与 driver 接收事件顺序一致；不同拦截器之间**无顺序保证**
3. **终态唯一**：每个拦截器在一个任务生命周期内最多收到一次 `on_terminal` 调用
4. **产物入口集中**：拦截器**不直接写 DB**；通过 `flush() -> InterceptorArtifact | None` 把要登记的产物交给 driver，由 driver 调用 `insert_artifact`
5. **不修改事件**：`on_event` 接收只读视图；不允许拦截器吃掉 / 改写事件让 driver 看不到

---

## 2. 数据模型

### 2.1 `InterceptorEvent`

> 拦截器接收的事件视图。**不直接复用** driver 内部的 `NormalizedEvent`，因为后者将来可能新增 driver 私有字段（如 SQLite 写入 cursor）。

```python
# src/worker/adapters/opencode/interceptors/types.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class InterceptorEvent:
    """拦截器看到的单条事件（只读视图）。

    生命周期：driver 在 _consume_sse 主循环中，每收到一条 opencode 原始事件，
    完成归一化后立即构造一个 InterceptorEvent，分发给所有注册的拦截器。

    Attributes:
        task_id:          所属任务 ID（拦截器本身不持有任务上下文，避免误用）
        session_id:       opencode session ID；拦截器订阅前可能为 None
        normalized_kind:  Worker TaskEventKind value (str)；
                          归一化后 Worker 可消费的事件类型；
                          若该原始事件不映射到任何 TaskEventKind（如心跳）则为 None
        normalized_payload: 归一化后的 payload；与 normalized_kind 同时为 None
        raw_type:         原始 opencode event type（如 "message.part.delta"）
        raw_payload:      原始 opencode payload（不可变视图）
        received_at:      driver 接收事件的 monotonic 时间戳（秒，float）
    """
    task_id: str
    session_id: Optional[str]
    normalized_kind: Optional[str]
    normalized_payload: Optional[Mapping[str, Any]]
    raw_type: str
    raw_payload: Mapping[str, Any]
    received_at: float
```

**为什么 `normalized_*` 字段允许 None**：W2-4 McpFieldRecorder 需要看到原始 `tool_call_finished` payload 以提取 `args` 中的非 top-level 嵌套字段，但当下 `event_stream.py:_normalize_part_updated` 在 normalize 时已抹掉一些原始结构。把原始视图保留给拦截器，避免后续因业务需求逆向修改 normalize 路径。

**为什么 `raw_payload` 用 `Mapping` 而不是 `dict`**：声明只读意图（PEP 484）。运行时不强制 immutable，但任何拦截器修改都被视为 bug。

### 2.2 `TerminalSignal`

```python
@dataclass(frozen=True)
class TerminalSignal:
    """任务终态信号。driver 在写终态事件前调用每个拦截器的 on_terminal。

    Attributes:
        task_id:     任务 ID
        session_id:  opencode session ID（可能为 None，如 session 创建失败）
        status:      最终 TaskStatus value (str)：
                     "completed" | "failed" | "aborted" | "timed_out"
        reason:      终态原因；
                     - status="aborted" 时取 self._abort_reason
                       ("user_requested" / "hitl_timeout" / "plan_rejected" /
                        "permission_rejected" / "system")
                     - status="failed" 时取异常类名
                     - status="timed_out" 时取 "timeout"
                     - status="completed" 时取 None
        ended_at:    monotonic 时间戳（秒）
    """
    task_id: str
    session_id: Optional[str]
    status: str
    reason: Optional[str]
    ended_at: float
```

### 2.3 `InterceptorArtifact`

> 拦截器在 flush 时返回的产物声明。**driver** 负责实际写文件 + 注册 DB + 发 `artifact_ready` 事件——拦截器只声明"我要登记什么"。

```python
@dataclass(frozen=True)
class InterceptorArtifact:
    """拦截器声明的待登记产物。

    Attributes:
        artifact_type:    Artifact.type 字段值（ArtifactType enum value）。
                          W2 拦截器一律用 ArtifactType.custom；通过 metadata.subtype 区分子类
                          （'conversations' / 'backtests' / 'mcp_field_summary'）。
        filename:         拟登记的文件名（含扩展名），用于 Content-Disposition
        local_path:       已落盘的绝对路径；driver 会校验路径必须在
                          settings.artifacts_dir / task_id 子树内（防 P0-8 类越权）
        metadata:         任意自定义元数据，会原样写入 Artifact.metadata
                          推荐字段：subtype, conversations_path / backtests / read_fields_map
        size_bytes:       文件字节数；None 时 driver 用 stat 自取
    """
    artifact_type: str
    filename: str
    local_path: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    size_bytes: Optional[int] = None
```

**注**：`Artifact` schema 当前**没有** `metadata` 字段（见 [contract/artifact.py:68-93](../src/worker/contract/artifact.py#L68-L93)）。W2-1 实施时**附带补充**该字段：`metadata: dict[str, Any] = Field(default_factory=dict)`，存储到 `artifacts` 表新增的 TEXT 列（JSON）。这是 spec 的一部分，不是 implementation 边界外的"顺手改"。

---

## 3. 抽象基类

### 3.1 `EventInterceptor`

```python
# src/worker/adapters/opencode/interceptors/base.py

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

from worker.adapters.opencode.interceptors.types import (
    InterceptorEvent,
    TerminalSignal,
    InterceptorArtifact,
)


class EventInterceptor(ABC):
    """driver 注入的事件拦截器抽象基类。

    生命周期（与 OpenCodeDriver.run 对齐）：
        1. driver 构造时持有 list[EventInterceptor]
        2. driver._consume_sse 每收到一条事件 → 对每个拦截器调用 on_event
        3. driver.run finally 块（即将写终态前）→ 对每个拦截器调用 on_terminal
        4. driver._collect_artifacts 后 → 对每个拦截器调用 flush，拿到的非 None
           InterceptorArtifact 由 driver 走标准 insert_artifact 路径登记

    并发模型：
        - on_event:    在 SSE 主协程中 await 调用；拦截器可 await 自身 IO，
                       但不应 sleep / 做长时间 CPU 工作（会阻塞 SSE 推进）
        - on_terminal: 同上
        - flush:       同上；可执行较重的 IO（如写整个 JSONL 文件）

    错误处理：
        - 三个 hook 抛出的任何异常都由 driver 的 InterceptorRunner 捕获，
          记录 logger.exception + 计入 metric，不向上游传播
        - 拦截器若需要"软失败"（不希望 driver 记 error log），自己内部捕获即可

    禁止事项：
        - 不直接调用 storage.repo.* 写 DB
        - 不直接发送 SSE 事件给上游
        - 不修改 InterceptorEvent.raw_payload / normalized_payload（视为只读）
        - 不持有 OpenCodeDriver 引用 / OpenCodeClient 引用（解耦）
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """拦截器名称（kebab-case），用于日志、metric label、错误隔离上下文。

        必须满足 ^[a-z][a-z0-9-]{2,40}$；driver 在注册时校验。
        """

    async def on_event(self, event: InterceptorEvent) -> None:
        """每条 opencode SSE 事件的回调（默认 no-op）。

        实现注意：
            - 必须是 idempotent：同一 raw_payload 重复进入不应产生重复副作用
            - 必须是 thread-safe：driver 当前是 single-event-loop，不需要锁；
              但子类不应启动后台 task 写共享状态
            - 可保存到子类实例字段以累积；终态时由 flush 统一落盘
        """

    async def on_terminal(self, signal: TerminalSignal) -> None:
        """终态信号回调（默认 no-op）。

        在 driver 写终态事件**前**调用。子类可在此停止任何后台计时器、
        关闭句柄等；不要在此做重 IO（建议放 flush）。
        """

    async def flush(self) -> Optional[InterceptorArtifact]:
        """终态后落盘 + 返回产物声明（默认返回 None，表示无产物登记）。

        生命周期：在 on_terminal 之后、driver 写终态事件之前调用一次。
        返回 None：拦截器不希望登记产物（如本次任务无 tool_call 需要记录）。
        返回 InterceptorArtifact：driver 走标准登记流程：
            1. 校验 local_path 在 artifacts_dir/task_id 子树内
            2. insert_artifact(...)
            3. emit artifact_ready 事件
        """
        return None
```

### 3.2 `InterceptorRunner`（driver 内部调度器）

> 不暴露给业务；driver 私有组件。提供"对所有拦截器并行调用 + 错误隔离"的薄封装。

```python
# src/worker/adapters/opencode/interceptors/runner.py

from __future__ import annotations

import asyncio
import logging
from typing import Sequence, Optional

from worker.adapters.opencode.interceptors.base import EventInterceptor
from worker.adapters.opencode.interceptors.types import (
    InterceptorEvent,
    TerminalSignal,
    InterceptorArtifact,
)
from worker.observability import metrics

logger = logging.getLogger(__name__)


class InterceptorRunner:
    """driver 私有调度器：调度多个 EventInterceptor，隔离每个的错误。

    并发策略：
        - on_event / on_terminal:  asyncio.gather(..., return_exceptions=True)
                                   所有拦截器并发推进；任一失败不影响其他
        - flush:                   sequential（保证日志顺序可读，性能不敏感）

    错误处理：
        - 任何拦截器抛出异常 → logger.exception + metrics.inc("interceptor_error",
          {"name": <name>, "phase": "on_event"|"on_terminal"|"flush"})
        - 同一拦截器累计错误数达到 _ERROR_BUDGET（默认 10）→ 静默 disable，
          后续不再调用其 hook；写一次 logger.error 标记 disable 原因
    """

    _ERROR_BUDGET = 10

    def __init__(self, interceptors: Sequence[EventInterceptor]):
        self._interceptors = list(interceptors)
        self._error_counts: dict[str, int] = {ic.name: 0 for ic in interceptors}
        self._disabled: set[str] = set()
        # 校验 name 格式 + 唯一
        names = [ic.name for ic in interceptors]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate interceptor names: {names}")

    async def dispatch_event(self, event: InterceptorEvent) -> None:
        active = [ic for ic in self._interceptors if ic.name not in self._disabled]
        if not active:
            return
        results = await asyncio.gather(
            *(self._safe_call(ic, "on_event", ic.on_event(event)) for ic in active),
            return_exceptions=False,  # _safe_call 已捕获
        )
        del results  # for clarity

    async def dispatch_terminal(self, signal: TerminalSignal) -> None:
        active = [ic for ic in self._interceptors if ic.name not in self._disabled]
        if not active:
            return
        await asyncio.gather(
            *(self._safe_call(ic, "on_terminal", ic.on_terminal(signal)) for ic in active),
            return_exceptions=False,
        )

    async def collect_artifacts(self) -> list[InterceptorArtifact]:
        """sequential flush；返回所有非 None 的产物声明。"""
        out: list[InterceptorArtifact] = []
        for ic in self._interceptors:
            if ic.name in self._disabled:
                continue
            artifact = await self._safe_call_with_return(ic, "flush", ic.flush())
            if artifact is not None:
                out.append(artifact)
        return out

    async def _safe_call(self, ic: EventInterceptor, phase: str, coro) -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001 — 隔离边界
            self._record_error(ic, phase)

    async def _safe_call_with_return(
        self, ic: EventInterceptor, phase: str, coro,
    ) -> Optional[InterceptorArtifact]:
        try:
            return await coro
        except Exception:
            self._record_error(ic, phase)
            return None

    def _record_error(self, ic: EventInterceptor, phase: str) -> None:
        self._error_counts[ic.name] += 1
        logger.exception(
            "interceptor %s failed in phase=%s (count=%d)",
            ic.name, phase, self._error_counts[ic.name],
        )
        # metrics hook 占位：metrics.inc_interceptor_error(ic.name, phase)
        if self._error_counts[ic.name] >= self._ERROR_BUDGET:
            self._disabled.add(ic.name)
            logger.error(
                "interceptor %s disabled (error_count=%d >= budget=%d)",
                ic.name, self._error_counts[ic.name], self._ERROR_BUDGET,
            )
```

---

## 4. driver 集成点

### 4.1 构造器变化

```python
# src/worker/adapters/opencode/driver.py — 仅展示 diff 意图

class OpenCodeDriver:
    def __init__(
        self,
        task_id: str,
        request: TaskRequest,
        host_port: int,
        container_env: dict[str, str],
        db: aiosqlite.Connection,
        interceptors: Sequence[EventInterceptor] = (),  # ← 新增；默认空，向后兼容
    ):
        ...
        self._runner = InterceptorRunner(interceptors)
```

**向后兼容**：默认空 tuple → 现有 `tests/e2e/test_tianqi_e2e.py` 与所有 unit test 不需改动。

**注入路径**：`orchestrator._drive_opencode` 在创建 driver 时根据 `request.opencode_profile` 决定注入哪些拦截器。该层属于 orchestrator 改造（W2-2/3/4 各自接 PR），**不在 W2-1 范围**。

### 4.2 `_consume_sse` 注入点

唯一改动点在 [driver.py:391-408](../src/worker/adapters/opencode/driver.py#L391-L408) 之后插入一行 `dispatch_event`：

```python
# 归一化并写入 Worker 事件
norm = normalize_opencode_event(raw_event)

# ── 拦截器分发（在 normalize 之后、insert_event 之前） ──
ic_event = InterceptorEvent(
    task_id=self.task_id,
    session_id=session_id,
    normalized_kind=norm.kind if norm else None,
    normalized_payload=norm.payload if norm else None,
    raw_type=raw_event.get("type", ""),
    raw_payload=raw_event.get("payload", {}) or {},
    received_at=time.monotonic(),
)
await self._runner.dispatch_event(ic_event)

# 现有逻辑：写 DB + assistant_buffer 累积 ...
```

**位置选择理由**：
- 在 normalize 之后 → 拦截器可看 `normalized_kind`（多数 hook 用得到）
- 在 `insert_event` 之前 → 即使 DB 写入失败、ValueError 跳过，拦截器仍能看到事件
- 在 permission 路径之外（permission 走 `_handle_permission`，自带 HITL 流程）→ 拦截器**不**接收 permission 请求事件本身，仅接收 `decision_received` 事件（已经被写到主流程）

### 4.3 `_handle_permission` 中的 `decision_received` 事件

W2-2 ConversationsWriter 需要 `decision_received` 进入对话流。当前 [driver.py:537-543](../src/worker/adapters/opencode/driver.py#L537-L543) 在 HITL 解析后写 `insert_event(... decision_received ...)` —— 但**没有走 SSE 主路径**。需要补一次 `dispatch_event`：

```python
# 在 _handle_permission 写完 decision_received 后追加：
ic_event = InterceptorEvent(
    task_id=self.task_id,
    session_id=session_id,
    normalized_kind="decision_received",
    normalized_payload={"decision_id": decision_id, "choice": choice_value, ...},
    raw_type="<synthesized:decision_received>",
    raw_payload={},
    received_at=time.monotonic(),
)
await self._runner.dispatch_event(ic_event)
```

`_auto_approve_permission` 同理。`_handle_plan_approval` 同理。

> **注**：`raw_type` 用 `<synthesized:...>` 前缀显式标记此事件**不是** opencode 原生事件，而是 driver 合成的"决策线"事件。拦截器若关心可按 prefix 过滤；不关心可忽略 raw fields，只看 normalized_*。

### 4.4 终态注入点

`run` 方法的 finally 块：

```python
async def run(self) -> None:
    ...
    final_status: TaskStatus
    final_reason: Optional[str] = None
    try:
        async with asyncio.timeout(self.timeout_sec):
            await self._run_inner()
        final_status = TaskStatus.completed
    except TaskTimedOutError:
        final_status = TaskStatus.timed_out
        final_reason = "timeout"
        raise  # 给 queue 路由
    except TaskAbortedError as e:
        final_status = TaskStatus.aborted
        final_reason = e.reason
        raise
    except Exception as e:
        final_status = TaskStatus.failed
        final_reason = type(e).__name__
        raise
    finally:
        # ── 拦截器终态分发 + flush ──
        signal = TerminalSignal(
            task_id=self.task_id,
            session_id=self.session_id,
            status=final_status.value,
            reason=final_reason,
            ended_at=time.monotonic(),
        )
        try:
            await self._runner.dispatch_terminal(signal)
            extra_artifacts = await self._runner.collect_artifacts()
            for art in extra_artifacts:
                await self._register_interceptor_artifact(art)
        except Exception:
            logger.exception("interceptor terminal phase failed; ignored")
        ...
        # 现有 cleanup（delete_session / aclose / clear_correlation）
```

`_register_interceptor_artifact` 是新增的 driver 私有方法，逻辑：

```python
async def _register_interceptor_artifact(self, art: InterceptorArtifact) -> None:
    # 1. 路径合规校验：必须落在 artifacts_dir / task_id 子树内
    artifacts_root = (self.settings.artifacts_dir / self.task_id).resolve()
    target = Path(art.local_path).resolve()
    try:
        target.relative_to(artifacts_root)
    except ValueError:
        logger.error(
            "interceptor artifact rejected (path escape): %s outside %s",
            target, artifacts_root,
        )
        return

    # 2. 走标准 insert_artifact + artifact_ready 路径
    artifact = Artifact(
        artifact_id=str(uuid.uuid4()),
        task_id=self.task_id,
        type=ArtifactType(art.artifact_type),
        filename=art.filename,
        size=art.size_bytes or target.stat().st_size,
        created_at=time.time(),
        expires_at=time.time() + self.settings.artifact_retention_days * 86400,
        metadata=dict(art.metadata),  # ← 见 §2.3 注，需补 schema
    )
    await insert_artifact(self.db, artifact, file_path=str(target))
    await insert_event(
        self.db, self.task_id, TaskEventKind.artifact_ready,
        {
            "artifact_id": artifact.artifact_id,
            "type": artifact.type.value,
            "filename": artifact.filename,
            "metadata": dict(art.metadata),
        },
    )
```

---

## 5. 失败语义矩阵

| 失败场景 | 行为 |
|---|---|
| 拦截器 `__init__` 抛错 | InterceptorRunner 构造失败 → driver 启动失败 → task 进 `failed` 终态。这是**显式**契约：注入 broken 拦截器视为编程错误，不容忍。 |
| 拦截器 `name` 不合规 / 重名 | `InterceptorRunner.__init__` 抛 `ValueError` → 同上 |
| `on_event` 抛同步 / 异步错 | `_safe_call` 捕获 → `logger.exception` + error_count++；driver SSE 主流程**继续**；其他拦截器**继续** |
| `on_terminal` 抛错 | 同上；不影响 flush 阶段 |
| `flush` 抛错 | 不返回产物；不影响其他拦截器的 flush；**不影响**任务终态写入 |
| 拦截器累计 error >= `_ERROR_BUDGET` | 拦截器被 quiet disable；后续 `on_event` / `on_terminal` / `flush` 全部跳过；写一次 `logger.error` |
| 拦截器返回的 `local_path` 越权（不在 `artifacts_dir/task_id` 下）| `_register_interceptor_artifact` 拒绝登记 + `logger.error`；不抛错；任务终态不变 |
| 拦截器返回的 `local_path` 不存在 | `target.stat()` 抛 `FileNotFoundError` → `_register_interceptor_artifact` 捕获 + 记 error；不抛错 |
| `dispatch_event` 在 driver 已 cancel 后被调用 | 子类 `on_event` 应能正常 await；如果其内部用 `asyncio.shield` 等机制，由子类自己处理 |
| driver 主流程 raise 后 finally 中 `dispatch_terminal` 失败 | `try/except Exception` 包整段，`logger.exception` 后吞错；终态写入照常 |

> **关键不变量**：拦截器**没有任何方式**让任务终态从 `completed` 变成 `failed`，反之亦然。

---

## 6. 配置与注入

### 6.1 注入路径

```
TaskRequest.opencode_profile.interceptors  (新增字段，可选)
                ↓
orchestrator._drive_opencode 读取该字段
                ↓
对每个拦截器名调用工厂 → EventInterceptor 实例
                ↓
OpenCodeDriver(..., interceptors=[...])
```

`opencode_profile.interceptors` 字段建议形态（W2-1 仅落 schema，不实装具体名）：

```python
class OpenCodeProfile(BaseModel):
    ...
    interceptors: list[InterceptorConfig] = Field(default_factory=list)


class InterceptorConfig(BaseModel):
    name: str  # "conversations" / "backtest" / "mcp_field_recorder"
    options: dict[str, Any] = Field(default_factory=dict)
```

**工厂注册**通过 entry_point 或 module-level dict（W2-2/3/4 各自注册自己），W2-1 仅提供注册中心：

```python
# src/worker/adapters/opencode/interceptors/__init__.py

from typing import Callable
from worker.adapters.opencode.interceptors.base import EventInterceptor

_FACTORIES: dict[str, Callable[..., EventInterceptor]] = {}


def register_factory(name: str, factory: Callable[..., EventInterceptor]) -> None:
    if name in _FACTORIES:
        raise ValueError(f"interceptor factory {name!r} already registered")
    _FACTORIES[name] = factory


def build_interceptor(name: str, **options) -> EventInterceptor:
    if name not in _FACTORIES:
        raise KeyError(f"unknown interceptor {name!r}")
    return _FACTORIES[name](**options)
```

### 6.2 默认配置

W2-1 不预注入任何拦截器；driver 默认行为与现状完全一致。

W2-2/3/4 落地后，由 orchestrator 在构造默认 profile 时填入；上游可通过 TaskRequest 覆盖或追加。

---

## 7. 测试矩阵

> 单元测试覆盖率门槛 ≥ 90%（基类比业务实现简单，应该更严）。

### 7.1 基类 / 数据模型

| Test | 断言 |
|---|---|
| `test_interceptor_event_immutable` | 修改 `InterceptorEvent.normalized_payload` 抛 `TypeError`（dataclass frozen）/ 至少 mutation 不影响 driver |
| `test_terminal_signal_status_values` | 仅接受 4 个合法 status；非法值抛 |
| `test_interceptor_artifact_path_required` | `local_path` 缺失 → 构造抛 |

### 7.2 Runner

| Test | 断言 |
|---|---|
| `test_runner_dispatches_event_to_all` | 注入 3 个 mock 拦截器，每条事件每个都收到 |
| `test_runner_isolates_event_error` | 1 个 raises，2 个正常 → 2 个仍收到完整事件流；driver 不受影响 |
| `test_runner_isolates_terminal_error` | terminal 阶段同上 |
| `test_runner_isolates_flush_error` | flush 阶段同上；正常拦截器的 InterceptorArtifact 仍返回 |
| `test_runner_disables_after_budget` | 一个拦截器连续抛 10 次 → 第 11 次的 on_event 不再被调用 |
| `test_runner_rejects_duplicate_names` | 构造器抛 ValueError |
| `test_runner_rejects_invalid_name_format` | 构造器抛 ValueError（uppercase / 含空格 / 太短）|

### 7.3 driver 集成

| Test | 断言 |
|---|---|
| `test_driver_default_no_interceptors` | 不传 interceptors 参数，task 行为与现状完全一致（回归保护现有 e2e）|
| `test_driver_dispatches_synthesized_decision_event` | 一次 plan_approval / permission / auto_approve 之后，拦截器收到 `normalized_kind="decision_received"` 的合成事件 |
| `test_driver_terminal_dispatch_on_completed` | 任务正常完成 → 拦截器收到 status="completed", reason=None |
| `test_driver_terminal_dispatch_on_aborted` | HITL reject → status="aborted", reason="plan_rejected" |
| `test_driver_terminal_dispatch_on_timeout` | timeout → status="timed_out", reason="timeout" |
| `test_driver_terminal_dispatch_on_failed` | RuntimeError → status="failed", reason="RuntimeError" |
| `test_driver_artifact_path_escape_rejected` | 拦截器 flush 返回 `/etc/passwd` 路径 → driver 拒绝登记，task 不失败 |
| `test_driver_artifact_normal_registration` | 拦截器 flush 返回合规 path → DB 出现 Artifact 记录 + emit `artifact_ready` |
| `test_driver_terminal_phase_isolated_from_main` | dispatch_terminal 抛错 → 终态事件仍正确写入 |

### 7.4 Property test（可选，建议加）

| Test | 断言 |
|---|---|
| `test_runner_event_order_preserved_per_interceptor` | 用 hypothesis 生成 N 条事件 → 每个拦截器看到的顺序与 driver 接收顺序一致 |

---

## 8. 不变量校验（CI gate）

W2-1 落地后，CI 脚本必须新增以下 grep gate（与 X1 backlog DoD §8 row 8 同源）：

```bash
# 任何业务字符串出现在 interceptors/ 内立即失败
! grep -RE "vibe-trading|strategy|signal_engine|ma250|skill" \
  src/worker/adapters/opencode/interceptors/
```

W2-2/3/4 实施时**绝不**应让任何业务字符串渗入。这是基类设计的核心承诺。

---

## 9. 与已知约束的关系

| 约束 | 关系 |
|---|---|
| ADR-001（worker 业务无关）| ✅ 强化：基类 + types 完全 generic |
| Phase 6 退出门 P1-10 (event_id race) | ✅ 不冲突：拦截器**不写** task_events 表 |
| Phase 6 退出门 P1-12 (SSE 事件驱动)| ✅ 不冲突：拦截器在 dispatch event 之前并行运行；不阻塞 SSE 推进 |
| P0-8（artifact 路径越权）| ✅ 强化：`_register_interceptor_artifact` 复用 `artifacts_dir/task_id` 子树校验 |
| design D5（worker 契约零变更）| ⚠️ 部分破坏：`OpenCodeProfile.interceptors` 字段是新增；但属于**可选** field，不影响现有 client 调用 |
| design §11.3 invariant 4（生产态零 worker 介入）| ✅ 不冲突：拦截器只在研究态运行 |

---

## 10. 实施清单（给 /sc:implement）

1. 创建目录 `src/worker/adapters/opencode/interceptors/`
2. 新增 `types.py`：`InterceptorEvent` / `TerminalSignal` / `InterceptorArtifact`
3. 新增 `base.py`：`EventInterceptor` ABC
4. 新增 `runner.py`：`InterceptorRunner`
5. 新增 `__init__.py`：注册中心 `register_factory` / `build_interceptor`
6. 修改 `driver.py`：构造器加 `interceptors`、`_consume_sse` / `_handle_permission` / `_handle_plan_approval` 加 `dispatch_event`、`run` finally 加 `dispatch_terminal` + flush 收集 + `_register_interceptor_artifact`
7. 修改 `contract/artifact.py`：`Artifact` 加 `metadata: dict[str, Any]`
8. 修改 `storage/repo.py:insert_artifact`：序列化 metadata 到 TEXT 列；初始化 schema 加 `metadata TEXT DEFAULT '{}'`（写入 `init_db` 既有路径，不创独立 migration —— 与现状一致）
9. 修改 `contract/task.py`：`OpenCodeProfile` 加 `interceptors: list[InterceptorConfig]`
10. 添加单元测试 §7（`tests/unit/test_interceptor_base.py` / `test_interceptor_runner.py` / `test_driver_interceptor_integration.py`）
11. 添加 CI grep gate（`pyproject.toml` 或 `Makefile` 内的 `lint:interceptor-purity` target）

**估时**（与 backlog 一致）：1 person-day；如果包含 metadata schema 变更与 grep gate，1.5 day。

---

## 11. 不做的事（明确边界）

- ❌ 不实现任何具体拦截器（W2-2/3/4 各自有 design + implement）
- ❌ 不引入插件 / entry_point 自动发现机制（YAGNI；显式 register_factory 足够）
- ❌ 不做拦截器之间的**显式**顺序约定（design §11.3 invariant 之外的隐式依赖视为 bug）
- ❌ 不做拦截器跨任务的状态共享（每个任务一组实例）
- ❌ 不做事件持久化重放（拦截器是"实时旁路"，不是"事件溯源消费者"）
- ❌ 不做异步背压（如果拦截器写盘慢，是该拦截器的问题；driver 不为它做 buffering）

---

## 12. 风险与缓解

| 风险 | 触发 | 缓解 |
|---|---|---|
| 拦截器误吞 SSE 事件 → 业务错过决策 | 错误的 hook 实现把事件 mutate 掉 | `InterceptorEvent` frozen + `Mapping` 声明；review 阶段强制 |
| 拦截器阻塞 SSE 主循环 | 拦截器 `on_event` 中 `time.sleep` | 不主动加超时（YAGNI）；review 阶段强制 async-only IO；后续如出现可加 `asyncio.wait_for` 包装 |
| InterceptorArtifact metadata 撑爆 DB | 拦截器写入大字段 | metadata 字段有 30KB 软上限（写入时 truncate 并 log；W2-2/3/4 实施时统一用同一 helper）|
| 多个拦截器同名 artifact 冲突 | 两个都登记 `conversations.jsonl` | filename 由各拦截器命名空间化（如 `conversations/{slug}.jsonl`）；driver 不强制命名 |

---

## 13. Open Questions（implementation 前必须澄清）

| OQ | 问题 | 推荐答案 |
|---|---|---|
| OQ-1 | metadata 字段是否需要单独表？ | **否**。直接加列；现有 schema 已用 TEXT 列存其他 JSON（如 task_events.payload） |
| OQ-2 | InterceptorArtifact 是否支持多文件？ | **否**。一次 flush 返回 1 个声明；多文件用多 flush 调用？—— **不行**，flush 也只调一次。结论：W2-2/3/4 内部把多个文件**打包**成一个 .tar.gz 或写一个 manifest 文件 |
| OQ-3 | error_budget 是否可配置？ | 是。`InterceptorRunner` 构造器接受 `error_budget: int = 10` |
| OQ-4 | dispatch_event 是否对 `heartbeat` 事件分发？ | **否**。heartbeat 在 normalize 阶段就已被吞掉（返回 None）；拦截器看不到 |
| OQ-5 | 拦截器是否能看到 `tool_call_started` 但看不到对应的 `_finished`（任务中途 abort）？ | **是**，这是合法状态。拦截器实现需 idempotent + 容忍未配对事件 |

— end of design —
