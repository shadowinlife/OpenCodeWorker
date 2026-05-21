# VibeTradingOpenCodeWorker 使用指南

## 概览

VibeTradingOpenCodeWorker 是一个安全的 AI 编程任务执行器，接收上游 agent runtime 的任务请求，在隔离的 Docker 沙箱中驱动 `opencode`（deepseek-v4-pro 等 LLM）执行代码/分析任务，通过 HTTP + SSE 实时回传事件。

```
调用方
  │  POST /tasks           → 提交任务
  │  GET  /tasks/:id/events → SSE 实时事件流
  │  POST /tasks/:id/decisions → HITL 人工决策
  ▼
Worker（本仓库）
  │  asyncio 调度队列 + SQLite 持久化
  │  docker run（Colima / Docker Desktop）
  ▼
沙箱容器（worker-sandbox:dev-arm64）
  └── opencode serve + build agent（deepseek-v4-pro）
```

---

## 环境依赖

| 依赖 | 版本要求 | 备注 |
|------|----------|------|
| macOS (Apple Silicon) | M1/M2/M3 | ARM64 原生 |
| Colima | ≥ 0.6 | Docker 运行时，`colima start` 启动后自动暴露 socket |
| conda 环境 `legonanobot` | Python ≥ 3.11 | 含 fastapi / uvicorn / aiosqlite / httpx |
| DashScope API Key | — | 用于 deepseek-v4-pro 模型 |
| Tushare Token | — | 用于 A 股数据获取（可选，任务相关） |

### 初始化依赖

```bash
conda activate legonanobot
cd /path/to/VibeTradingOpenCodeWorker
pip install -e ".[dev]"
```

---

## 构建沙箱镜像

### ARM64（macOS Apple Silicon / Colima）

```bash
DOCKER_HOST=unix://$HOME/.colima/default/docker.sock \
  docker build \
    -t worker-sandbox:dev-arm64 \
    -f docker/worker/Dockerfile.arm64 \
    .
```

构建完成后验证：

```bash
DOCKER_HOST=unix://$HOME/.colima/default/docker.sock \
  docker images worker-sandbox:dev-arm64
```

---

## 运行 Worker 服务

### 方式一：直接启动（开发调试）

```bash
export WORKER_BEARER_TOKEN=$(openssl rand -hex 32)
export DOCKER_HOST=unix://$HOME/.colima/default/docker.sock
export WORKER_SANDBOX_IMAGE=worker-sandbox:dev-arm64
export WORKER_BROKER_ENABLED=false        # 无 broker 时禁用

conda run -n legonanobot \
  python -m uvicorn worker.main:app \
    --host 127.0.0.1 \
    --port 18080 \
    --log-level info
```

### 方式二：通过 E2E 测试自动启动

E2E 测试脚本会自动拉起 Worker 子进程：

```bash
cd /path/to/VibeTradingOpenCodeWorker
PYTHONPATH=src \
  conda run -n legonanobot \
  python tests/e2e/test_tianqi_e2e.py
```

---

## 主要 API

### 健康检查

```bash
curl http://localhost:18080/health
# {"status":"ok","version":"0.1.0"}
```

### 提交任务

```bash
curl -X POST http://localhost:18080/tasks \
  -H "Authorization: Bearer $WORKER_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "direct_execute",
    "workspace": {
      "kind": "local",
      "local_path": "/path/to/workspace"
    },
    "opencode_profile": {
      "model": "dashscope/deepseek-v4-pro",
      "provider": { ... }
    },
    "messages": [
      {"role": "user", "content": "分析天齐锂业基本面，保存到 ./reports/tianqi_analysis.md"}
    ],
    "container_env": {
      "DASHSCOPE_API_KEY": "sk-xxx",
      "TUSHARE_TOKEN": "xxx"
    }
  }'
# 返回 {"task_id": "uuid", "status": "queued"}
```

### 订阅 SSE 事件流

```bash
curl -N \
  -H "Authorization: Bearer $WORKER_BEARER_TOKEN" \
  http://localhost:18080/tasks/{task_id}/events
```

事件示例（按顺序）：

```
data: {"kind":"task_created","payload":{}}
data: {"kind":"task_queued","payload":{}}
data: {"kind":"task_started","payload":{"phase":"executing"}}
data: {"kind":"container_started","payload":{"container_id":"..."}}
data: {"kind":"opencode_ready","payload":{}}
data: {"kind":"execution_started","payload":{"mode":"direct_execute"}}
data: {"kind":"assistant_delta","payload":{"content":"正在分析..."}}
data: {"kind":"tool_call_started","payload":{"tool":"bash","args":{...}}}
data: {"kind":"tool_call_finished","payload":{"tool_use_id":"...","result":"..."}}
data: {"kind":"artifact_ready","payload":{"type":"diff","uri":"..."}}
data: {"kind":"task_completed","payload":{"duration_sec":660}}
```

### HITL 决策（权限请求）

当 opencode 请求需要人工确认的操作时，会产生 `hitl_required` 事件：

```bash
# 审批
curl -X POST http://localhost:18080/tasks/{task_id}/decisions \
  -H "Authorization: Bearer $WORKER_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"decision_id":"xxx","choice":"approve"}'

# 拒绝
-d '{"decision_id":"xxx","choice":"reject"}'
```

---

## 任务模式

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| `direct_execute` | 跳过计划阶段，直接执行 | 确定性强的短任务（数据查询、报告生成） |
| `plan_first` | 先生成执行计划，人工审批后再执行 | 高风险代码修改、复杂多步骤任务 |

---

## 任务状态机

```
pending → queued → preparing_workspace → starting_container
       → starting_opencode → [planning → awaiting_human →]
       → executing → collecting_artifacts
       → completed / failed / aborted / timed_out
```

---

## 环境变量配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `WORKER_BEARER_TOKEN` | （必填） | API 认证 Token |
| `WORKER_SANDBOX_IMAGE` | `worker-sandbox:dev-arm64` | Docker 镜像名 |
| `WORKER_DATA_DIR` | `/tmp/worker_data` | SQLite DB 和工作区存储目录 |
| `WORKER_PORT` | `8080` | 监听端口 |
| `WORKER_LOG_LEVEL` | `INFO` | 日志级别 |
| `WORKER_BROKER_ENABLED` | `true` | 是否启用 egress broker（无 broker 时设 `false`） |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | Docker socket 路径（Colima 需设为 `unix://$HOME/.colima/default/docker.sock`） |

---

## E2E 测试：天齐锂业分析

本仓库内置一个完整的端到端测试，用 opencode + deepseek-v4-pro 分析天齐锂业（002466.SZ）基本面并生成报告。

### 前提

1. `~/.colima/default/docker.sock` 可用（`colima start`）
2. `worker-sandbox:dev-arm64` 镜像已构建
3. `/Users/mgong/LegoNanoBot/Qdata/.env` 包含以下内容：
   ```
   DASHSCOPE_API_KEY=sk-xxx
   TUSHARE_TOKEN=xxx
   ```

### 运行

```bash
cd /path/to/VibeTradingOpenCodeWorker
PYTHONPATH=src \
  conda run -n legonanobot \
  python tests/e2e/test_tianqi_e2e.py
```

### 预期输出

- Worker 自动启动，任务提交，容器启动
- AI 调用 tushare/akshare API 收集数据（约 10~20 分钟）
- 报告保存至 `/path/to/Qdata/reports/tianqi_analysis.md`（约 280+ 行）
- 最终打印 `[E2E] 任务终态: completed`

---

## `opencode_profile` 配置

`TaskRequest.opencode_profile` 透传给沙箱内 `opencode serve`，并控制 Worker 侧的可选 SSE 拦截器（W2）。

### 基础字段（模型 / 权限 / Provider）

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `model` | `anthropic/claude-opus-4-5` | `<provider>/<model-id>`；非法值由 opencode 启动时报错 |
| `permission_template` | `plan_first_default` | `plan_first_default` / `direct_execute_default` / `custom` |
| `permission_overrides` | `{}` | 按 opencode 工具名细粒度覆盖，例：`{"bash":"allow","write_file":"deny"}` |
| `providers` | `[]` | 需在 opencode config 中显式声明的 provider 名 |
| `provider_extra_config` | `{}` | 每个 provider 的扩展配置（如自定义 `baseURL`） |

### `interceptors[]` — Worker SSE 拦截器声明（W2）

`opencode_profile.interceptors` 是一组声明式条目：`{name, options}`。Worker 在创建 driver 时按 `name` 查 `register_factory` 注册中心，用 `options` 透传到拦截器构造函数。未注册的 `name` 被静默跳过，便于上游灰度。

内置工厂（import 拦截器包即注册）：

#### `conversations` — 对话流落盘（ConversationsWriter）

把 SSE 事件序列化为 `<task_id>/conversations/{ISO8601}-{slug}.jsonl`，内置 API key / Bearer / ID 等敏感串脱敏。

| `options` 字段 | 默认值 | 说明 |
|---|---|---|
| `max_content_chars` | `32768` | 单条 message content 上限；超出截断并打 `truncated` 标记 |
| `max_messages` | `2000` | 总条数上限；FIFO 截断 + 保留头部 sentinel |
| `slug_prefix` | `"untitled"` | fallback slug 前缀（`<prefix>-<6char>`），上游可改为业务标签 |

`summarize_callback` 接受一个 Python callable（用 `messages` 算 3~4 词 slug），**不可** 通过 JSON 配置传入。需要时上游应自定义子类 + `register_factory("conversations-with-summary", ...)`。当前 worker 内置实现走 fallback slug 路径。

#### `backtest` — 回测目录归档（BacktestInterceptor）

监听匹配 `tool_pattern` 的 `tool_call_finished`，把 `args.run_dir` 整目录复制到 `<task_id>/backtests/{ISO8601}-{label}/`，终态写汇总 `backtests/index.json`。

| `options` 字段 | 默认值 | 说明 |
|---|---|---|
| `tool_pattern` | `"*.backtest"` | `fnmatch` 风格，匹配 `tool_call_started.tool`；上游显式注入业务相关 pattern |
| `run_dir_key` | `"run_dir"` | 从 tool args 读取源目录的 key |
| `label_prefix` | `"iter"` | 自动 label 形如 `iter-1` / `iter-2`；override label 走 `raw_payload.part.metadata.backtest_label`，需匹配 `^[a-z0-9][a-z0-9-]{2,40}$` |
| `workspace_root` | `null` | 相对 `run_dir` 的解析根；为 `null` 时相对路径直接跳过 |
| `max_pending_calls` | `512` | `tool_use_id` 暂存上限，溢出 FIFO 丢弃 |

#### `mcp-fields` — MCP 字段画像（McpFieldRecorder）

聚合所有 `tool_call_finished`，按 `(mcp_name, tool_name)` 记录 `required_input_fields`（取 args top-level keys）与 `required_output_fields`（取上游 LLM 写到 `raw_payload.part.metadata.read_fields[]` 的字段名）。终态写 `<task_id>/mcp_field_summary.json` 独立 artifact，**不**直接改 SKILL manifest（由上游 meta-skill author 决定如何合并）。

| `options` 字段 | 默认值 | 说明 |
|---|---|---|
| `mcp_name_pattern` | `"^([a-z][a-z0-9-]+)\\."` | 从 tool 名取 MCP 命名空间；第 1 个捕获组即 `mcp_name`，不匹配的 tool 被忽略 |
| `read_fields_key` | `"read_fields"` | `raw_payload.part.metadata` 中 output fields 的 key |
| `max_pending_calls` | `1024` | 同上 |

### `hitl_policy.auto_approve` —— 自动审批模式

`HitlPolicy.auto_approve` 是一组 `"<DecisionKind>:<context_key>"` 模式串；driver 在收到 `permission_request` 时若命中其一即自动 approve，无需人工。`<context_key>` 通常是工具名（例：`tool_permission:read_file`），支持 `fnmatch` 通配。例：

```json
"hitl_policy": {
  "auto_approve": [
    "tool_permission:read_file",
    "tool_permission:list_directory",
    "tool_permission:quant.*"
  ]
}
```

留空（默认）= 所有权限请求都走 HITL。

### 完整示例：声明三个内置拦截器 + 自动审批读类工具

```json
{
  "mode": "direct_execute",
  "messages": [{"role": "user", "content": "跑一次 backtest"}],
  "opencode_profile": {
    "model": "dashscope/deepseek-v4-pro",
    "interceptors": [
      {"name": "conversations", "options": {"slug_prefix": "tianqi"}},
      {"name": "backtest", "options": {"tool_pattern": "vibe-trading.backtest"}},
      {"name": "mcp-fields", "options": {}}
    ]
  },
  "hitl_policy": {
    "auto_approve": ["tool_permission:read_file", "tool_permission:quant.*"]
  }
}
```

终态后产物落在：

```
<artifacts_dir>/<task_id>/
├── conversations/<ISO8601>-tianqi-<6char>.jsonl
├── backtests/
│   ├── index.json
│   └── <ISO8601>-iter-1/
└── mcp_field_summary.json
```

每个文件同时通过标准 `insert_artifact` 路径登记到 DB，并触发一条 `artifact_ready` SSE 事件，可用 `GET /tasks/:id/artifacts/:artifact_id` 下载。

---

## 关键实现说明

### opencode SSE 行为（当前实现按 1.14.30 实测处理）

`/global/event` SSE 端点**仅传输心跳**（`server.heartbeat`），不传递 `message.part.delta` 等会话内事件。Driver 采用以下双轨策略检测任务完成：

- **SSE 轨道**：检测 `session.error`（快速失败）及权限请求
- **REST 轮询轨道**：每 5 秒 `GET /session/{id}/message`，检查最后一条 assistant 消息的 `info.time.completed` 字段是否有值

### opencode SSE 格式（1.14.30 实测）

实际格式（与旧版不同）：

```json
{"payload": {"type": "session.status", "properties": {"status": "busy"}}}
```

Driver 中已归一化为：

```json
{"type": "session.status", "payload": {"status": "busy"}}
```

---

## 目录结构

```
VibeTradingOpenCodeWorker/
├── src/worker/
│   ├── adapters/opencode/
│   │   ├── client.py        # opencode HTTP 客户端（含 SSE 格式归一化）
│   │   ├── driver.py        # 任务驱动主循环（SSE + REST 轮询）
│   │   └── event_stream.py  # SSE 事件类型定义与 session idle 检测
│   ├── api/                 # HTTP 路由（tasks / events / decisions）
│   ├── contract/            # Pydantic Schema（task / event / artifact）
│   ├── orchestrator/        # asyncio 任务调度队列
│   ├── sandbox/             # Docker 容器生命周期管理
│   ├── storage/             # SQLite 持久化
│   └── workspace/           # 工作区准备（local / tarball / git）
├── docker/worker/
│   ├── Dockerfile.arm64     # ARM64 沙箱镜像（当前 pin: opencode 1.15.0）
│   └── entrypoint.sh        # 容器入口（opencode serve --print-logs）
├── tests/e2e/
│   └── test_tianqi_e2e.py   # 天齐锂业基本面分析 E2E 测试
└── docs/
    ├── adr/                 # 架构决策记录（ADR-001~006）
    ├── roadmap/             # 实施路线图
    └── usage-guide.md       # 本文件
```
