# VibeTradingOpenCodeWorker

> **安全的 OpenCode Worker** —— 为上游 agent runtime 提供隔离的 AI 编程任务执行环境。

## 项目定位

本仓库封装一个**通用的、安全的 OpenCode Worker 模块**，接收任务后在独立 Docker 沙箱中驱动 `opencode` + `oh-my-opencode` 执行代码任务，通过 HTTP + SSE 向上游回传实时事件，并暴露 HITL（Human-In-The-Loop）决策接入点。

**本仓库不包含** vibe-trading 业务逻辑；业务 skills / MCP 由上游 runtime 注入。

```
上游 agent runtime
  │  POST /tasks          （提交任务）
  │  GET  /tasks/:id/events  （SSE 实时事件流）
  │  POST /tasks/:id/decisions  （HITL 人工决策）
  ▼
┌─────────────────────────────────────────┐
│  Worker（本仓库）                         │
│  FastAPI + asyncio queue + SQLite        │
│        │                                 │
│        │  docker run                     │
│        ▼                                 │
│  ┌───────────────────────────────────┐   │
│  │ 沙箱容器 (ubuntu:24.04)            │   │
│  │  opencode serve + oh-my-openagent │   │
│  └───────────────────────────────────┘   │
└─────────────────────────────────────────┘
```

## 当前状态

| 阶段 | 内容 | 状态 |
|------|------|------|
| Phase 0 | 架构决策（ADR 001~006）+ 本机自检（opencode / oh-my-opencode）| ✅ 完成 |
| Phase 1 | Worker Contract & API 骨架（本次提交）| 🚧 进行中 |
| Phase 2 | Orchestrator 完整实现（容器生命周期、opencode 驱动）| ⬜ 待开始 |
| Phase 3 | 网络隔离（egress 白名单）| ⬜ 待开始 |
| Phase 4 | 崩溃恢复、任务重播 | ⬜ 待开始 |
| Phase 5 | 镜像构建 CI | ⬜ 待开始 |
| Phase 6 | 集成测试 | ⬜ 待开始 |
| Phase 7 | 多租户、加密、跨节点调度等高级特性 | ⬜ 规划中 |

详细路线图见 [docs/roadmap/opencode-worker.md](docs/roadmap/opencode-worker.md)。

## 快速启动（Phase 1 骨架）

### 前提

- macOS / Linux，[Colima](https://github.com/abiosoft/colima) 或 Docker Desktop
- [Conda](https://docs.conda.io/) 环境 `legonanobot`（包含 fastapi / uvicorn / pydantic 等）

### 安装依赖

```bash
conda activate legonanobot
pip install -e ".[dev]"
```

### 启动 Worker

```bash
export WORKER_BEARER_TOKEN=$(openssl rand -hex 32)
conda run -n legonanobot python -m uvicorn worker.main:app --host 0.0.0.0 --port 8080
```

### 验证健康检查

```bash
curl http://localhost:8080/health
# {"status":"ok","version":"0.1.0"}
```

### 提交任务（示例）

```bash
curl -X POST http://localhost:8080/tasks \
  -H "Authorization: Bearer $WORKER_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode":"plan_first","messages":[{"role":"user","content":"给 add 函数写单测"}]}'
```

## 目录结构

```
src/worker/
├── config.py              # 配置（pydantic-settings，WORKER_* 环境变量）
├── main.py                # FastAPI app 入口，lifespan 钩子
├── contract/              # Pydantic 契约 Schema（任务/事件/决策/产物/错误）
├── api/                   # HTTP 路由（tasks CRUD、SSE 事件流、HITL 决策）
├── orchestrator/          # 任务调度队列（asyncio）
├── storage/               # SQLite 持久化（db 初始化 + CRUD repo）
├── adapters/opencode/     # opencode HTTP 客户端适配器（Phase 2）
├── sandbox/               # Docker 沙箱生命周期管理（Phase 2）
├── workspace/             # 工作区准备（tarball / git clone）（Phase 2）
├── broker_client/         # Host broker egress 代理（Phase 3）
└── observability/         # Prometheus metrics + OTLP（Phase 6）
docs/
├── adr/                   # Architecture Decision Records（ADR-001~006）
└── roadmap/               # 实施路线图
```

## 架构决策记录（ADR）

| ADR | 主题 |
|-----|------|
| [ADR-001](docs/adr/ADR-001-opencode-adapter-mode.md) | opencode 适配模式选择 |
| [ADR-002](docs/adr/ADR-002-container-image-baseline.md) | 容器基础镜像基线（ubuntu:24.04）|
| [ADR-003](docs/adr/ADR-003-credential-model.md) | 凭据注入模型 |
| [ADR-004](docs/adr/ADR-004-broker-boundary.md) | Broker 边界定义 |
| [ADR-005](docs/adr/ADR-005-implementation-language.md) | 实现语言选择（Python）|
| [ADR-006](docs/adr/ADR-006-ohmy-version-and-entry.md) | oh-my-opencode 版本与入口 |

## API 概览

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 健康检查 |
| `GET` | `/ready` | 就绪检查（DB 可用） |
| `POST` | `/tasks` | 提交任务 |
| `GET` | `/tasks/:id` | 查询任务状态 |
| `GET` | `/tasks/:id/events` | **SSE** 实时事件流（支持 Last-Event-ID 断线重连）|
| `POST` | `/tasks/:id/decisions` | 提交 HITL 人工决策 |
| `POST` | `/tasks/:id/abort` | 中止任务 |
| `GET` | `/tasks/:id/artifacts` | 列出产物 |
| `GET` | `/tasks/:id/artifacts/:artifact_id` | 下载产物文件 |
| `DELETE` | `/tasks/:id` | 删除任务（含产物）|
| `GET` | `/metrics` | Prometheus 指标（Phase 6）|

完整 OpenAPI 规范在 Worker 启动后可通过 `/docs` 访问。

## 开发规范

- Python 运行环境：`conda run -n legonanobot python`
- 代码注释：中文
- 量化/投资逻辑优先保证正确性，所有假设和数据边界须显式标注
- 变更须先经 ADR 评审再实现（见 [AGENTS.md](AGENTS.md)）

## 许可

本仓库为私有项目，未经授权不得分发。
