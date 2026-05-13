# ADR-005 — 实现语言与技术栈

| 字段 | 值 |
|---|---|
| **状态** | Accepted |
| **日期** | 2026-05-13 |
| **关联 HITL** | 无（决策无争议） |

---

## 背景

Worker 主进程（HTTP API + 任务调度 + 容器管理 + SSE 事件流）需要选择实现语言和核心依赖库。约束条件：
- 开发机已有 conda 环境 `legonanobot`，Python 生态已在用。
- 需要异步 SSE 支持、Docker SDK、HTTP 客户端（调用容器内 opencode HTTP server）。
- 不引入重型框架或需要单独运维的中间件（Kafka、Redis 等）到 MVP。

---

## 决策

### 实现语言：Python 3.11+

运行于 conda 环境 `legonanobot`，通过 `pyproject.toml` 管理依赖。

### 核心库

| 用途 | 库 | 说明 |
|---|---|---|
| HTTP API + SSE | `fastapi` + `uvicorn` | 原生 `StreamingResponse` 支持 SSE |
| 数据校验 / Contract schema | `pydantic` v2 | TaskRequest / TaskEvent / Decision / Artifact |
| HTTP 客户端（调用 opencode） | `httpx` | 支持 async + SSE streaming（`httpx-sse`） |
| Docker 容器管理 | `docker` SDK（python-docker） | 创建/停止/清理容器、读取日志 |
| 持久化 | `sqlite3`（stdlib）+ `aiosqlite` | 任务状态、事件 cursor、decisions、artifacts |
| 配置管理 | `pydantic-settings` | env 驱动配置，与 pydantic v2 统一 |
| 测试框架 | `pytest` + `pytest-asyncio` + `httpx` test client | 单元 + 集成测试 |
| Stub server（集成测试） | `respx`（httpx mock）或轻量 FastAPI stub | 不依赖真实 LLM 的 adapter 测试 |

### 不引入（MVP）

- Redis / Celery：任务队列用 asyncio + SQLite，无需独立中间件。
- PostgreSQL：SQLite 足够 MVP 单节点场景。
- gRPC / WebSocket：上游协议只需 HTTP + SSE。
- TypeScript / Go：不引入第二语言，避免 toolchain 维护负担。

### 项目结构（Phase 1 初始化目标）

```
pyproject.toml
src/
  worker/
    api/            # FastAPI routes
    contract/       # pydantic schemas
    orchestrator/   # state machine, queue
    adapters/
      opencode/     # HTTP server adapter（含 oh-my agent routing，Phase 3/4 合并）
    sandbox/        # docker manager, reaper
    workspace/      # bundle unpack
    broker_client/  # Worker → Broker HTTP calls
    storage/        # sqlite models + queries
    observability/  # logging, metrics, tracing hooks
  broker/           # Host Broker MVP
tests/
docs/
  adr/
  roadmap/
```

### 执行环境

- 开发：macOS + conda `legonanobot`，`conda run -n legonanobot python ...`。
- CI/部署：目标 x86_64/Linux；Docker 容器内的 Worker 进程也用同一 Python 栈。

---

## 影响

- Phase 1 交付 `pyproject.toml` + 目录骨架 + 基础 pytest 配置。
- 所有 Python 脚本执行命令统一为 `conda run -n legonanobot python <script>`（参见 AGENTS.md）。
- `pydantic` v2 字段命名、序列化规则在 Phase 1 锁定，避免跨 Phase 破坏性更改 contract schema。
