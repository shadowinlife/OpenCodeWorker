# Safe OpenCode Worker — Roadmap

> 本文档是 `VibeTradingOpenCodeWorker` 仓库的实施路线图。
> 当前阶段：**Phase 1 已完成（Worker Contract & API 骨架，本机 smoke test 通过）**。
> 2026-05-13 已补充 opencode / oh-my-opencode 本机自检证据，见 §1.3。
> 2026-05-13 Phase 1 骨架实现完成并本机验证，见 §1.4。
> 后续若执行中出现新的人工决策点，按 `🟠 HITL` 标注追加。

---

## 1. 项目定位

封装一个**通用的、安全的 OpenCode Worker 模块**，供更上游的 agent runtime 调用。

- 上游 agent runtime 负责：用户对话、调研进度维护、任务上下文准备（含 `AGENTS.md`）、最终用户 HITL UI、业务 skills/MCP 接入。
- 本仓库（Worker）负责：接收任务 → 启动隔离 Docker 沙箱 → 驱动 opencode + oh-my-opencode → 流式回传事件 → 暴露 HITL 决策点 → 输出 artifacts/logs。
- vibe-trading 等业务逻辑**不在本仓库**，由上游 runtime 通过 MCP/skills 注入。

### 1.1 已确认决策（本次 HITL 讨论结果）

| 维度 | 决策 |
|---|---|
| Host Broker | **本仓库交付 broker MVP**，仅做 **HTTP egress 代理 + 域名白名单**；不参与 MCP 生命周期 |
| MCP 形态 | **MVP 内所有 MCP 均为 stdio 本地服务并打包进镜像**（与 opencode/oh-my-opencode 一起版本 pin），不引入 HTTP/SSE MCP |
| 出网策略 | **域名级白名单** + **按 task 动态下发**（TaskRequest.broker_policy.allow_egress_hosts）；MVP 默认空，必须显式放行 |
| LLM 凭据 | **容器 env 注入 provider API key**；容器启动时基于 env 生成 opencode 配置文件，不挂载宿主登录态；零密钥/broker 凭据代理移 Phase 7 |
| 上游协议 | **HTTP + SSE 单协议** |
| Worker API 鉴权 | **静态 Bearer token**（env 配置） |
| Workspace bundle | **tar.gz**（流式上传 / 引用 URL）+ **git url + commit sha** |
| 并发模型 | **单 worker 进程 + 内置任务队列**，每任务一容器 |
| 基础镜像 | **自建 debian-slim**，手动 pin opencode / oh-my-opencode / MCP 版本；本地构建 + GHCR 私有 tag，不签名 |
| 任务模式 | 仅 `plan_first` / `direct_execute`，**去掉 `auto_recommend`**；`direct_execute` 失败仅发 `mode_escalation_suggested` 事件，不自动升级 |
| Safe-explore 模式 | **不提供**（移 Phase 7） |
| Debug 模式 | 不开放 `--debug-no-sandbox`；集成测试通过 stub opencode HTTP server |
| 可观测性 | 仅暴露 `/metrics`（Prometheus 风格）+ OTLP hook，**不锁定后端栈** |
| 崩溃恢复 | Worker 重启后进行中任务标 `failed(orphaned)`，**不做跨重启续跑**（移 Phase 7） |
| 数据保留 | workspace 任务终态后**立即删除**；artifact 默认保留 **7 天**；提供 `DELETE /tasks/:id` 硬删除接口；**不做静态加密**（移 Phase 7） |
| 实现语言 | **Python**（FastAPI + httpx + docker SDK，复用 `legonanobot` conda 环境） |
| 文档落点 | `docs/roadmap/opencode-worker.md`（建 `docs/` 结构） |

### 1.2 MVP 不做（明确移入 Phase 7+ todo）

- 多租户、计费、RBAC、团队权限矩阵、客户登录。
- 不可篡改审计链、合规留痕、第三方时间戳。
- Kubernetes 调度、跨节点容器池、PostgreSQL/Redis 集群状态。
- 业务层：vibe-trading skills、数据源 MCP、策略/回测/交易执行。
- 订阅态 LLM 登录（Claude Code / Codex / Gemini CLI），仅支持 API key。
- 容器自由外网访问；仅允许通过宿主 broker 的域名白名单。
- 挂载用户主目录或宿主项目根目录为可写。
- HTTP/SSE 远程 MCP；MCP 全部 stdio + 随镜像发布。
- Broker 零密钥凭据代理、MCP server spawn/kill、跨重启任务续跑、`safe_explore` 只读模式、静态加密、cosign 镜像签名。

### 1.3 2026-05-13 本机自检证据

本节记录已经实际验证过的事实，避免后续实现时回到猜测状态。当前开发机为 macOS；目标真实执行环境按 x86_64/Linux 容器规划。本机 `CPU lacks AVX support` warning 已明确不作为当前阻塞项。

#### opencode 基线

- 官方 server 文档存在：`https://opencode.ai/docs/en/server/`（页面更新时间：2026-05-13）。它明确 `opencode serve` 是 headless HTTP server，并列出 Server APIs。
- 官方用法：`opencode serve [--port <number>] [--hostname <string>] [--cors <origin>]`；默认 port `4096`、hostname `127.0.0.1`；另有 `--mdns` / `--mdns-domain`。
- 官方认证规则：设置 `OPENCODE_SERVER_PASSWORD` 后启用 HTTP Basic Auth；用户名默认 `opencode`，也可用 `OPENCODE_SERVER_USERNAME` 覆盖。本机自检仅验证了默认用户名路径。
- 官方 API 文档列出的 Worker 相关主路径包括：`GET /global/health`、`GET /global/event`、`GET|POST /session`、`POST /session/:id/message`、`POST /session/:id/prompt_async`、`POST /session/:id/command`、`GET /session/:id/diff`、`POST /session/:id/abort`、`POST /session/:id/permissions/:permissionID`。
- `opencode` 可执行文件存在：`/usr/local/bin/opencode`。
- `opencode --version` 输出版本：`1.14.30`。
- `opencode serve --hostname 127.0.0.1 --port 41913` 可启动 headless server。
- Basic Auth 实测规则：用户名必须是 `opencode`，密码来自 `OPENCODE_SERVER_PASSWORD`。
  - `opencode:local-selfcheck` -> `/global/health` 返回 `200`。
  - `:local-selfcheck`、`x:local-selfcheck`、`local-selfcheck:` -> 返回 `401`。
- `/global/health` 实测返回：`{"healthy":true,"version":"1.14.30"}`。
- `/global/event` SSE 可连接，首条事件包含 `server.connected`。
- `/session` API 可用：`POST /session` 能创建空 session，`GET /session` 能列出，`DELETE /session/:id` 能删除；自检 session 已删除，最终列表为空。
- OpenAPI 路径存在版本差异：
  - `/doc` 返回 OpenAPI `3.1.1`，但本机 `1.14.30` 下只暴露少量 global paths（如 `/auth/{providerID}`、`/log`）。
  - `/openapi.json` 实测返回 Web UI HTML，不是 JSON。
  - 结论：Worker adapter 以官方 server 文档作为 API 地图，以当前 pin 版本实测 fixture 锁定请求/响应细节，并在 ADR 中记录差异。

#### opencode endpoint spike（2026-05-13 续测）

本轮使用临时 server：`OPENCODE_SERVER_PASSWORD=endpoint-spike opencode serve --hostname 127.0.0.1 --port 41914`。测试 session 已全部删除，41914 端口已关闭。

- `/session/:id/message`
  - `POST` 存在；缺少 `parts` 时返回 `400`，错误路径为 `parts`，要求 array。
  - `POST` body 实测最小形态：`{"noReply":true,"parts":[{"type":"text","text":"..."}]}`。
  - `noReply:true` 返回 `200`，响应形态为 `{"info": {...}, "parts": [...]}`，只写入 user message，不触发 assistant 回复。
  - `GET /session/:id/message` 返回 message array，每条为 `{"info": {...}, "parts": [...]}`；不是顶层 `id/role/parts` 扁平结构。
- `/session/:id/prompt_async`
  - `POST` 存在；缺少 `parts` 时返回 `400`，错误路径为 `parts`，要求 array。
  - `POST` body 实测最小形态：`{"parts":[{"type":"text","text":"..."}]}`。
  - 成功时立即返回 `204 No Content`；最终输出不在 HTTP response body，而在 `/global/event` SSE 与后续 `GET /session/:id/message` 中读取。
  - 未显式传 `agent/model` 时，本机配置会走 `Sisyphus - Ultraworker` + `alibaba-cn/deepseek-v4-pro`，并注入 oh-my 的 analyze-mode prompt。Worker 应显式设置 agent/model，避免隐式默认值影响 mode routing。
- `/global/event`
  - 事件为 `data: { ... }` SSE frame，无显式 SSE `id:` 字段；payload 内包含 `payload.type`。
  - 实测事件类型包括：`server.connected`、`server.heartbeat`、`message.updated`、`message.part.updated`、`message.part.delta`、`session.updated`、`session.status`、`session.diff`、`session.idle`、`sync`。
  - `session.status` 在运行中出现 `busy`，完成或 abort 后出现 `idle`。
  - `sync` 事件中有 `syncEvent.id` / `seq` / `aggregateID`，可作为内部游标候选；Worker SSE 对上游仍应生成自己的单调 `event_id`。
- `/session/:id/diff`
  - 正确方法是 `GET`；无 diff 时返回 `200 application/json`，body 为 `[]`。
  - `POST /session/:id/diff` 命中 Web UI HTML fallback，不应作为 API 使用。
  - SSE 中也会出现 `session.diff`，payload 内含 `diff: []` 或实际 diff。
- `/session/:id/abort`
  - 正确方法是 `POST`；空 body 即可，返回 `200 true`。
  - 对 idle session 调用也返回 `true`。
  - 对 busy `prompt_async` 立即 abort 时，事件流出现 `session.status: busy -> idle` 与 `session.idle`；assistant message 可能已创建但 `parts` 为空。
- `/session/:id/permissions/:permissionID`
  - 正确方法是 `POST`；`permissionID` 必须以 `per` 开头，否则返回 `400`。
  - body schema：`{"response":"once|always|reject"}`。
  - `once`、`reject` 实测返回 `200 true`；`allow`、`deny`、`approve` 均返回 `400`，合法值只有 `once|always|reject`。
  - 本轮未触发真实工具 permission request，只验证了 response endpoint schema。后续 HITL spike 仍需通过 ask policy 触发真实 `per_*` 请求并确认事件 payload。
- 前端 bundle 反查确认的相关路由还包括：`GET /session/:id`、`GET /session/:id/todo`、`GET /session/status`、`POST /session/:id/init`、`POST /session/:id/fork`、`DELETE /session/:id/message/:messageID`、`GET /vcs/diff`。这些不进入当前 MVP 主路径，除非后续 adapter 需要。

#### oh-my-opencode 基线

- `omo` / `oh-my-opencode` 不在 PATH，但 npx 缓存里已有 CLI wrapper：
  - `~/.npm/_npx/47ababa9653307a4/node_modules/oh-my-opencode/bin/oh-my-opencode.js`
- 本地 npx 缓存包版本：`oh-my-opencode 3.17.5`（CLI 工具）。
- opencode 插件（`oh-my-openagent@latest`）在 opencode 包缓存中的版本：`3.17.2`（`~/.cache/opencode/packages/oh-my-openagent@latest/`，最近更新 2026-04-14）。
- npm registry 最新版本：`oh-my-openagent 4.1.1`、`oh-my-opencode 4.1.1`（两者同步发布）。
- **注意**：`oh-my-openagent`（opencode 插件）与 `oh-my-opencode`（CLI 工具）是两个不同 npm 包名，当前版本均为 `4.1.1`，但本机缓存 plugin 为 `3.17.2`、CLI 为 `3.17.5`。
- opencode 全局插件配置声明：
  - `./plugins/cr-aicc.mjs`
  - `superpowers@git+https://github.com/obra/superpowers.git`
  - `oh-my-openagent@latest`
- server 启动日志显示 `oh-my-openagent@latest` 被加载，未见插件加载错误。
- `oh-my-opencode doctor --status` 结果：
  - System：OpenCode `1.14.30`，oh-my loaded plugin `3.17.2`（与 `oh-my-openagent@latest` 缓存版本一致），Bun `1.3.13`。
  - Configuration：`~/.config/opencode/oh-my-openagent.json` valid。
  - Tools：LSP 可见 5 servers；AST-Grep 不可用；GitHub CLI 已认证。
  - MCPs：doctor 展示 builtin `context7`、`grep_app`。
- `oh-my-opencode doctor --json` 识别出 `11 agents`、`8 categories`、`18 overrides`。
- `opencode agent list` 不显示 Prometheus/Sisyphus/Metis/Momus；这不是 oh-my 不可用，而是 oh-my 有自己的 agent resolution。实现时不能用 `opencode agent list` 作为 oh-my agent 可用性的唯一判断。

#### oh-my-opencode smoke tests

- Prometheus 最小 smoke test 已通过：
  - 命令入口：`oh-my-opencode run --agent Prometheus --model alibaba-cn/deepseek-v4-pro --directory "$PWD" --json ...`
  - 运行输出标识：`Prometheus - Plan Builder`。
  - 固定响应：`OMO_PROMETHEUS_SMOKE_OK`。
  - JSON 结果：`success: true`。
- Sisyphus / `ulw` 最小 smoke test 已通过：
  - 命令入口：`oh-my-opencode run --agent Sisyphus --model alibaba-cn/deepseek-v4-pro --directory "$PWD" --json ...`
  - Prompt 包含 `ulw smoke test`。
  - 运行输出标识：`Sisyphus - Ultraworker`。
  - 固定响应：`OMO_ULW_SMOKE_OK`。
  - JSON 结果：`success: true`。
- 两次 smoke test 都要求“不创建、不编辑、不删除、不读取文件、不运行 shell、不启动 background agents”；结果中 session summary 显示 `additions: 0`、`deletions: 0`、`files: 0`。
- oh-my `run` 会启动 `opencode serve` 并可能留下监听进程；本次测试残留的 `4096` / `4097` server 已手动停止。Worker 实现必须负责 server/container lifecycle，不能假设 CLI run 会自动清理所有后台进程。

#### MCP / provider / Docker 基线

- `opencode auth list` 可见 3 组凭据：Alibaba、Alibaba China、GitHub Copilot。
- `opencode mcp list` 实测本机用户配置中的 `search mcp` 与 `vibe-trading` 均 connected。
- Docker daemon 可用，当前 context 为 Colima；daemon 环境：Linux / `aarch64` / 2 CPU / 约 2GB memory。
- 本地没有 opencode 相关 Docker image。目标执行环境按 x86_64/Linux 规划，镜像 spike 仍需单独完成。

#### Spike 1b：env 变量与 config 注入（2026-05-13）

使用临时 server（`opencode serve --hostname 127.0.0.1 --port 41920/41921/41922`）进行以下验证，测试后 server 已全部关闭：

- **`OPENCODE_DISABLE_AUTOUPDATE=1`**（boolean）：禁用自动更新检查，对应 H10 填回。与 `autoupdate: false` 配置项组合实现双层禁用，均已实测有效。
- **`OPENCODE_CONFIG_CONTENT`**（string）：内联 JSON 配置注入，优先级最高（覆盖 global + project config）。实测设置 `autoupdate:false` 和 `model:` 字段，`GET /config` 返回值正确反映注入内容。
- **`OPENCODE_PERMISSION`**（string）：内联 JSON 权限配置注入。实测 `{"bash":"ask","write":"ask","edit":"ask"}` 注入后，`GET /config` 的 `permission.*` 字段正确反映。
- **`{env:VARIABLE_NAME}` 替换在 `OPENCODE_CONFIG_CONTENT` 中生效**（关键发现）：实测在 inline JSON 里写 `"apiKey": "{env:SPIKE_TEST_KEY}"` 并设置 `SPIKE_TEST_KEY=hello_from_env_var`，`GET /config` 返回已解析值 `hello_from_env_var`。这意味着 Worker **不需要写任何配置文件**，provider API key 可通过 `OPENCODE_CONFIG_CONTENT` + env 替换纯 env 方式注入。
- **`OPENCODE_CONFIG_DIR`**：文档确认为「自定义配置目录（相当于 `.opencode/`，搜索 agents/commands/modes/plugins）」，与全局 config 文件路径不同（全局文件路径由 `OPENCODE_CONFIG` 控制）。本轮未单独测试文件访问；鉴于 `OPENCODE_CONFIG_CONTENT` 可满足 config 注入需求，容器方案不依赖此变量。
- **oh-my-openagent 版本确认**：opencode 插件包 `oh-my-openagent@latest` 本机缓存版本为 **3.17.2**（npm latest：4.1.1）。server toast 事件显示 `OhMyOpenCode 3.17.2`，与缓存版本一致。见 ADR-006。
- **真实 `per_*` permission 事件 payload**：本轮未触发（需要真实 LLM 调用且设置 bash:ask）。已知 response endpoint 的完整 schema，事件 payload 的字段细节（permissionID 出现的 SSE event type）推迟到 Phase 3 adapter 实现时通过集成测试确认。

---

### 1.4 2026-05-13 Phase 1 本机验证证据

本节记录 Phase 1 骨架实现完成后的本机 smoke test 结果。
运行环境：macOS + conda env `legonanobot` + Python 3.11 + FastAPI 0.135 + uvicorn 0.41。

**实现清单（全部完成）：**

| 文件 | 描述 |
|------|------|
| `src/worker/contract/task.py` | TaskRequest / TaskResponse / TaskStatus（14 状态）/ TERMINAL_STATUSES |
| `src/worker/contract/event.py` | TaskEvent / TaskEventKind（18 种）/ TERMINAL_EVENT_KINDS |
| `src/worker/contract/decision.py` | DecisionRequest / DecisionResponse / PendingDecision（HITL 5 步流程）|
| `src/worker/contract/artifact.py` | Artifact / ArtifactType（9 种产物类型）|
| `src/worker/contract/error.py` | WorkerError / ErrorKind（14 种，含 retryable/requires_hitl 分类）|
| `src/worker/config.py` | Settings（pydantic-settings，WORKER_* 前缀，bearer_token 必须设置）|
| `src/worker/storage/db.py` | SQLite init（4 张表），WAL 模式预留，进程单例连接 |
| `src/worker/storage/repo.py` | 全参数化 SQL CRUD（tasks/events/decisions/artifacts），无 SQL 注入风险 |
| `src/worker/orchestrator/queue.py` | asyncio.Queue + Semaphore + Phase 1 stub executor |
| `src/worker/api/middleware.py` | BearerTokenMiddleware（hmac.compare_digest 防时序攻击）|
| `src/worker/api/routes.py` | 全部 10 个端点（含 SSE Last-Event-ID 回放 + heartbeat）|
| `src/worker/main.py` | FastAPI app + lifespan（init_db / start_queue_worker / close_db）|

**Smoke test 结果（2026-05-13 本机实测，端口 18080~18082）：**

- `GET /health` → `{"status":"ok","version":"0.1.0"}` ✅
- `GET /ready` → `{"status":"ready","version":"0.1.0"}` ✅
- 无 token 请求 → `401 unauthorized` ✅
- `GET /tasks/nonexistent` with token → `404` ✅
- `POST /tasks` → `201`，任务状态通过 Phase 1 stub 经 `pending→queued→starting_container→completed` 流转（约 5ms）✅
- `GET /tasks/:id` → `{"status":"completed",...}` ✅
- 重复 `task_id` → `409 Conflict` ✅（幂等保护有效）
- SSE `GET /tasks/:id/events` → 回放 4 条历史事件（task_created / task_queued / task_started / task_completed），格式 `id: N\nevent: kind\ndata: {}\n\n` ✅
- `POST /tasks/:id/abort`（已终态）→ `409` ✅
- `GET /tasks/:id/artifacts` → `[]` ✅

**已知 Phase 1 限制（Phase 2 解决）：**

- Phase 1 stub executor 直接将任务标为 completed，无真实 Docker 容器操作。
- `abort` 端点只修改 DB 状态，不向容器发送 stop 信号（Phase 2 实现）。
- HITL decisions、artifacts 下载端点逻辑完整，但需要 Orchestrator 写入真实 artifacts 才能测试下载。
- heartbeat 轮询间隔为 0.5s（适合开发），生产建议调整 `sse_heartbeat_sec` 配置。

---

## 2. 架构总览

```
┌─────────────────────────────────────────────────────────┐
│ Upstream Agent Runtime  (HTTP + SSE, Bearer auth)       │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│ Worker API (FastAPI)                                    │
│  - /tasks, /tasks/:id/events (SSE), /tasks/:id/decisions│
│  - /tasks/:id/messages, /tasks/:id/abort                │
│  - /tasks/:id/artifacts                                 │
├─────────────────────────────────────────────────────────┤
│ Task Orchestrator                                       │
│  - 状态机 / 队列 / 并发上限 / 崩溃恢复                  │
│  - Mode Router (plan_first | direct_execute)            │
│  - HITL Gateway (event ↔ decision)                      │
│  - Event Bus (cursor + replay)                          │
├─────────────────────────────────────────────────────────┤
│ OpenCode Adapter                                        │
│  - 主路径：`opencode serve` HTTP/SSE                    │
│  - 备路径：`opencode CLI + tmux/PTY`（POC，不进 MVP 默认）│
│  - 事件归一化、permission ↔ DecisionRequest 映射        │
├─────────────────────────────────────────────────────────┤
│ Sandbox Manager (docker SDK)                            │
│  - 一任务一容器、reaper、镜像 pin                       │
│  - 非 root / cap-drop / no-new-privileges / tmpfs       │
│  - 默认无外网，仅可访问 Host Broker                     │
├─────────────────────────────────────────────────────────┤
│ Workspace Manager                                       │
│  - tar.gz / git 解包、symlink/zip-slip 防护             │
│  - diff / snapshot / logs / custom artifacts 输出       │
├─────────────────────────────────────────────────────────┤
│ Storage Lite (SQLite + 本地文件)                        │
│  - tasks / events / decisions / artifacts / secrets refs│
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│ Host Broker (本仓库交付)                                │
│  - HTTP egress 代理（域名白名单 + 审计 hook）           │
│  - 不管理 MCP 生命周期；MVP MCP 为容器内 stdio 服务     │
│  - ACL / rate-limit / audit hook                        │
└─────────────────────────────────────────────────────────┘
```

---

## 3. Worker Contract（v0 草案）

### 3.1 TaskRequest

```jsonc
{
  "task_id": "uuid?",                       // 可选幂等键
  "mode": "plan_first | direct_execute",
  "messages": [{"role":"user","content":"..."}],
  "workspace": {
    "kind": "tarball | git | empty",
    "tarball_url": "...",                   // 或 inline base64（受 size 限制）
    "git": {"url":"...","sha":"...","subpath":"?"}
  },
  "opencode_profile": {
    "model": "anthropic/claude-...",
    "providers": ["anthropic","openai"],    // 影响 env 注入
    "permission_template": "plan_first_default | direct_execute_default | custom",
    "permission_overrides": { /* 受白名单约束 */ }
  },
  "env_policy": {
    "provider_keys": ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"],  // 由 worker 从 secret store 注入
    "extra_env": {}
  },
  "resource_limits": {"cpu":"2","memory":"4Gi","pids":512,"timeout_sec":1800},
  "hitl_policy": {
    "decision_timeout_sec": 600,
    "on_timeout": "abort",                  // MVP 仅支持 abort
    "auto_approve": []                      // 默认空；可白名单某些低风险 permission kind
  },
  "broker_policy": {
    "allow_egress_hosts": [],               // 默认空，按需放行
    "mcp_servers": []                       // MVP 保留字段；不管理生命周期，默认空
  },
  "metadata": {"trace_id":"...", "tenant_hint":"..."}
}
```

### 3.2 TaskEvent（SSE 流，每条带 `event_id` 单调递增）

`task_created` · `task_queued` · `task_started` · `container_started` · `opencode_ready`
· `assistant_delta` · `tool_call_started` · `tool_call_finished`
· `plan_ready` · `hitl_required` · `decision_received`
· `execution_started` · `artifact_ready`
· `task_completed` · `task_failed` · `task_aborted`
· `heartbeat`（SSE keep-alive）

每个事件包含 `event_id`、`task_id`、`ts`、`kind`、`payload`、`cursor`。

### 3.3 DecisionRequest / DecisionResponse

```jsonc
// hitl_required 事件 payload
{
  "decision_id": "uuid",
  "kind": "plan_approval | tool_permission | file_write | broker_egress | continue_long_task | custom",
  "summary": "...",
  "options": ["approve","reject","revise","abort"],
  "default_on_timeout": "abort",
  "expires_at": "2026-05-13T12:00:00Z",
  "context": { /* 计划文本 / 命令 / 文件 diff / URL 等 */ }
}

// POST /tasks/:id/decisions
{
  "decision_id": "uuid",
  "choice": "approve | reject | revise | abort",
  "feedback": "可选文本",
  "patch": { /* 可选：修改后的命令 / 计划 */ },
  "idempotency_key": "uuid"
}
```

### 3.4 Artifact

`type ∈ {workspace_snapshot, diff, plan, log, stdout, stderr, transcript, report, custom}`
统一通过 `GET /tasks/:id/artifacts` 列表 + `GET /tasks/:id/artifacts/:artifact_id` 下载。

### 3.5 错误模型

`invalid_request` · `unauthorized` · `quota_exceeded`
· `workspace_prepare_failed` · `sandbox_start_failed` · `opencode_start_failed`
· `opencode_failed` · `broker_denied` · `hitl_timeout`
· `task_cancelled` · `resource_exhausted` · `artifact_too_large` · `internal_error`

每类错误标注：`retryable`、`requires_hitl`、`counts_against_quota`、`user_visible_message`。

---

## 4. 任务状态机

```
pending
  → queued
  → preparing_workspace
  → starting_container
  → starting_opencode
  → planning            (plan_first)
  → awaiting_human      ←→ revising
  → executing           (direct_execute 或 plan 批准后)
  → collecting_artifacts
  → completed | failed | aborted | timed_out
```

崩溃恢复：worker 重启后，所有非终态任务标记为 `failed(orphaned)`，关联容器由 reaper 清理；终态任务可继续查询/下载 artifact。

---

## 5. Roadmap 分阶段

### Phase 0 — 基线与技术 spike

**目标**：固定运行边界，验证关键不确定性。

- [x] 本机 opencode / oh-my-opencode 安装验证：`opencode --version`、`oh-my-opencode doctor --status/--json`、Prometheus/Sisyphus smoke tests；证据见 §1.3。
- [x] **Spike 1a**：`opencode serve` endpoint spike；官方 server 文档已确认 serve/API/auth 主路径，且本机已验证 health/SSE/session/message/prompt_async/diff/abort/permission response schema/password，证据见 §1.3。
- [x] **Spike 1b**：runtime 配置 spike 完成；确认 `OPENCODE_DISABLE_AUTOUPDATE=1`、`OPENCODE_CONFIG_CONTENT` 内联注入（含 `{env:X}` 替换）、`OPENCODE_PERMISSION` 注入；ADR-003 凭据方案升级为纯 env 注入（无需写文件）；oh-my 插件版本确认为 3.17.2；真实 `per_*` 事件 payload 推迟到 Phase 3 集成测试（非阻塞）；证据见 §1.3 Spike 1b 小节。
- [x] **Spike 2**：~~`opencode CLI + tmux/PTY` 可行性 POC~~ — **ADR-001 已正式关闭此选项**；HTTP Server 路径已充分验证，不做 PTY POC。
- [ ] **Spike 3**：debian-slim 基础镜像 + pin opencode / oh-my-opencode 版本可运行；记录可重现的 Dockerfile 构建过程。
- [ ] **Spike 4**：容器内 env 注入 API key → 启动时生成 opencode 配置（auth.json / opencode.json）的完整流程。
- [ ] **Spike 5**：Host Broker 最小原型（单机 HTTP forward proxy + 白名单），验证容器内 opencode 在仅有 broker 时仍能正常工作。
- [ ] **ADR 输出**：
  - [x] `ADR-001` HTTP Server vs CLI+tmux → **Accepted**（见 `docs/adr/ADR-001-opencode-adapter-mode.md`）：HTTP Server 唯一主路径；oh-my-openagent 以插件形式加载，`prompt_async` 传 `agent` 参数路由；CLI+tmux 与 oh-my run 黑盒均关闭；Phase 3/4 合并。
  - [x] `ADR-002` 镜像基线 = 自建 debian-slim（见 `docs/adr/ADR-002-container-image-baseline.md`）；H10 Spike 1b 已回填：`OPENCODE_DISABLE_AUTOUPDATE=1`。
  - [x] `ADR-003` 凭据模型升级 = `OPENCODE_CONFIG_CONTENT` + `{env:X}` 替换，纯 env 注入无需写文件（见 `docs/adr/ADR-003-credential-model.md`）。
  - [x] `ADR-004` Broker 责任边界与 MVP 形态（见 `docs/adr/ADR-004-broker-boundary.md`）。
  - [x] `ADR-005` 实现语言 = Python（FastAPI + docker SDK + httpx）（见 `docs/adr/ADR-005-implementation-language.md`）。
  - [x] `ADR-006` oh-my 版本 pin = `oh-my-openagent 3.17.2`，运行时入口为 HTTP API + `agent` 参数（见 `docs/adr/ADR-006-ohmy-version-and-entry.md`）；H11 收口。

Phase 0 退出检查（无新 HITL，按 ADR 落实即可）：
- ADR-001 ~ ADR-006 全部输出。
- Spike 中如出现阻塞性问题（如 opencode 版本与基础镜像不兼容、stdio MCP 在容器内无法工作），追加 `🟠 HITL` 议题再讨论。

---

### Phase 1 — Worker Contract 与 API 骨架

- [x] 仓库结构初始化：
  ```
  src/worker/            # FastAPI app
    api/                 # routes
    contract/            # pydantic schemas (TaskRequest 等)
    orchestrator/        # state machine, queue
    adapters/opencode/   # http server adapter
    sandbox/             # docker manager, reaper
    workspace/           # bundle handling
    broker_client/       # 容器内/外通信契约
    storage/             # sqlite + filesystem
    observability/       # logging, metrics, tracing hooks
  src/broker/            # Host Broker MVP
  tests/
  docs/
  ```
- [x] Pydantic schema 锁定 TaskRequest / TaskEvent / Decision / Artifact / Error。
- [x] FastAPI 路由 shell + Bearer token middleware + 请求 trace_id 注入。
- [x] SSE endpoint：支持 `Last-Event-ID` 重连、cursor 回放、heartbeat。
- [x] 内置任务队列（asyncio + 持久化到 SQLite）；`max_concurrent_tasks` 配置；超出排队返回 `queued` 事件。
- [x] Storage Lite：tasks / events / decisions / artifacts 表；事件 append-only + cursor 索引。
- [x] Idempotency：基于 `task_id` 和 `decision.idempotency_key`。
- [x] OpenAPI schema 自动导出供上游使用（FastAPI 自动生成，访问 `/docs`）。

---

### Phase 2 — Docker Sandbox + Workspace + Broker

- [ ] Dockerfile：debian-slim + Bun + opencode pin + oh-my-opencode pin + **需要的 stdio MCP 二进制 pin** + 非 root 用户；禁用 auto-update（env + 配置双层）。
- [ ] Sandbox 启动参数：`--read-only`、`--tmpfs /tmp`、`--cap-drop ALL`、`--security-opt no-new-privileges`、`--pids-limit`、`--memory`、`--cpus`、自定义 network。
- [ ] 网络隔离：自建 docker network；容器默认无 default route，仅能访问 broker container/host endpoint。
- [ ] Workspace handling：
  - tar.gz 解包：限大小、symlink 解析白名单、防 zip-slip。
  - git clone（在 worker 进程或 broker，**不在沙箱内 clone 以避免敏感凭据**）。
  - 解包结果以独立 volume 挂载到容器。
- [ ] Reaper / GC：基于 label 扫描孤儿容器与 tmp dir；worker 启动时清理。
- [ ] Host Broker MVP：
  - HTTP forward proxy + **域名级白名单**（按 task 动态下发，默认空）。
  - **不管 MCP 生命周期**：所有 MCP 已 stdio + 打包入镜像，容器内 opencode 直接 spawn；MCP 若需公网由 broker 放行域名。
  - **不做凭据代理**：容器内通过 env 持有 LLM API key（MVP 简化）。
  - 审计 hook：记录所有出站请求 + task_id 关联。
  - Broker 配置接口：`POST /broker/tasks/:id/policy`（worker 内部调用）下发白名单与 TTL。
- [ ] 安全回归测试：rm -rf /、读取 /etc/shadow、curl 外网、fork bomb、超大输出。

Phase 2 退出检查：
- 镜像构建产物推送到 GHCR 私有 tag；ADR 记录 pin 版本。
- 安全回归测试全部通过；任意单条失败必须修复后才能进入 Phase 3。

---

### Phase 3 — OpenCode HTTP Server Adapter（含 oh-my agent 路由，Phase 3/4 合并）

- [ ] 容器入口脚本：读取 env（API keys、server password、permission template、mode）→ 生成 `~/.config/opencode/opencode.json` 与 auth 文件 → 启动 `opencode serve --port ... --hostname 127.0.0.1`。（oh-my-openagent 插件已装在镜像内，`opencode serve` 启动后自动加载，Prometheus/Sisyphus 通过 `prompt_async` 的 `agent` 参数路由）
- [ ] Adapter 通过 docker network 调用容器内 opencode HTTP：
  - `/global/health` 启动探测（含超时与重试）。
  - `/global/event` SSE 订阅；本机 `1.14.30` 未见 SSE `id:` 字段，adapter 需要用自有单调 cursor 包装上游事件，并可参考 `sync.syncEvent.id/seq`。
  - `/session` 创建/恢复。
  - `POST /session/:id/message` 投递 `noReply:true` 消息时只写 user message；返回 `{info, parts}`。
  - `POST /session/:id/prompt_async` 投递异步 prompt；成功返回 `204`，结果从 SSE 和 `GET /session/:id/message` 读取。
  - `POST /session/:id/permissions/:permissionID` 映射 worker Decision；`permissionID` 必须以 `per` 开头，response 取值为 `once|always|reject`。
  - `GET /session/:id/diff` 收集 artifact；`POST /diff` 是 Web UI fallback，不可用作 API。
  - `POST /session/:id/abort` + 容器 stop 双层中止；busy abort 后会出现 `session.status: idle`，assistant message 可能为空 parts。
- [ ] API discovery fixture：以官方 server 文档作为 API 地图，针对 pin 版本保存实测 endpoint 行为；特别记录 `/doc` 与 `/openapi.json` 的本机差异，避免生成器误读 HTML 为 OpenAPI。
- [ ] 事件归一化：opencode event kind ↔ TaskEvent kind 映射表，并冻结。
- [ ] Permission template 默认值（YAML/JSON 内嵌）：
  - `plan_first_default`：edit ask、bash ask、webfetch ask、external_directory deny。
  - `direct_execute_default`：edit allow（受 workspace 限定）、bash ask、webfetch ask、external_directory deny。
  - `safe_explore` 不进入 MVP；如 Phase 7 恢复，再定义 edit deny、bash deny、read allow、glob/grep allow、webfetch ask。

---

### Phase 4 — ~~oh-my-opencode Mode Adapter~~ → 合并入 Phase 3

> ADR-001 决定：oh-my-openagent 以插件形式运行，Phase 3 的 HTTP adapter 已覆盖所有 oh-my 能力。原 Phase 4 独立 adapter 计划取消；以下条目并入 Phase 3 执行：

- [ ] 镜像内安装 oh-my-opencode CLI（pin 版本，待 H11 确认）并通过 `oh-my-opencode doctor` 自检；doctor 失败时 Worker 日志告警。
- [ ] Version policy：pin oh-my 版本；待 Spike 3 容器验证时决定沿用 `3.17.5` 还是升级到 `4.1.0`（H11）。
- [ ] `plan_first` 流程：`prompt_async` + `{agent:"Prometheus", parts:[...]}`；验证 oh-my Plan Builder 行为被完整激活（Spike 1b）。
- [ ] `direct_execute` 流程：`prompt_async` + `{agent:"Sisyphus", parts:[...]}`，prompt 中含 `ultrawork` / `ulw`；失败/风险过高/permission 频繁触发 ask 时，发 `mode_escalation_suggested` 事件。
- [ ] `/start-work` slash command 作为可选验证：`POST /session/:id/command` + `{command:"/start-work", ...}`，确认是否可替代直接传 agent；不阻塞 MVP。
- [ ] 不以 `opencode agent list` 判断 oh-my agent 是否存在；以 `oh-my-opencode doctor --json` 和容器内 smoke test 作为能力判定。
- [ ] 事件丰富化：tool_use、stdout/stderr、transcript 统一到 TaskEvent。

Phase 3（合并后）退出检查：
- 两种 mode 在真实 LLM 上各跑通 1 个端到端用例。
- `mode_escalation_suggested` 事件具备稳定 schema，可被上游消费。
- oh-my doctor 在容器内通过；agent routing 验证完成（Spike 1b 结果落档）。

---

### Phase 5 — HITL 最小闭环 + 事件可靠性

- [ ] 统一 DecisionRequest：plan approval / tool permission / file write / broker egress / long-task continue。
- [ ] HITL 超时：`default_on_timeout = abort`；超时事件 `hitl_timeout` + 触发 `/session/:id/abort` + 容器 stop。
- [ ] Event cursor & replay：上游 SSE 断线重连后能从 `Last-Event-ID` 续传，确保 decision 窗口不丢失。
- [ ] Decision 幂等：重复 `decision_id` 返回上次结果。
- [ ] 任务恢复策略（受 MVP 限制）：
  - 终态任务可重连查事件、下 artifact。
  - 进行中任务在 worker 重启后标 `failed(orphaned)`，不尝试自动恢复（MVP 不做）。

---

### Phase 6 — Worker Hardening

- [ ] 测试矩阵：
  - 单元：state machine、event mapper、permission mapper、bundle 解包安全。
  - 集成：stub opencode HTTP server（避免每次跑真实 LLM）；录制/回放 fixture。
  - 契约：JSON Schema 校验上游 contract。
  - 安全回归：Phase 2 列表 + secret 泄漏扫描 + 容器 escape 尝试。
  - HITL 时序：决策早到 / 晚到 / 重复 / 超时边界。
- [ ] 可观测性：
  - 结构化日志（task_id / decision_id / session_id correlation）。
  - Metrics（Prometheus 风格 endpoint）：task_count、task_duration、hitl_wait_seconds、container_start_ms、abort_rate、token_usage（如可获取）。
  - OpenTelemetry tracing hook 预留。
  - `/health`、`/ready`（区分 docker daemon / broker 健康）。
- [ ] 资源回收：
  - 容器/workspace/临时 config 在终态后清理。
  - artifact size limit、log truncation、event TTL。
- [ ] 版本治理：
  - opencode / oh-my-opencode pin 列表 + 自动更新禁用验证。
  - "upstream version → worker image tag → contract version" 兼容矩阵。
  - 升级 playbook（spike → ADR → bump → 回归）。

Phase 6 退出检查：
- 测试覆盖率达标（建议 70%+，最终值在 Phase 1 锁定）。
- 可观测性 endpoint 通过外部工具验证可采集。
- 升级 playbook 在 README 落地，供后续 opencode 版本 bump 使用。

---

### Phase 7 — 生产化 todo（仅占位）

- 多用户/多租户：认证、RBAC、租户配额、secret manager（Vault/KMS）。
- 强审计：hash chain、对象存储归档、查询 API、保留策略。
- 扩展性：Redis queue、多 worker、PostgreSQL state store、S3 artifact、K8s sandbox。
- 安全增强：egress proxy 细粒度白名单、MCP policy engine、seccomp/AppArmor、镜像签名（cosign）、SAST/DAST、SBOM/CVE。
- 上游生态：SDK、OpenAPI 发布、stdio JSON-RPC bridge、订阅态凭据代理方案。
- 任务恢复：进行中任务跨 worker 重启续跑。
- 业务集成模板：vibe-trading MCP/skills 接入指南（仍在上游交付）。
- 零密钥方案：Broker 作为 OpenAI-compatible endpoint 代理所有 LLM 请求。
- HTTP/SSE MCP 支持：当上游需要非本地 MCP 时再扩展 broker。
- Safe-explore 只读模式 / auto_recommend 模式 / 跨重启任务续跑 / 静态加密 / 镜像 cosign 签名。

---

## 6. 验证标准（MVP DoD）

- 上游 runtime 能通过 HTTP+SSE 创建任务，完成 `plan_first` 与 `direct_execute` 两条路径。
- TaskRequest / TaskEvent / Decision / Artifact 通过 JSON Schema 严格校验。
- 安全：让 opencode 尝试 `rm -rf /`、读取 `/etc/shadow`、`curl https://example.com`（无放行）、fork bomb 时，操作被沙箱/挂载/网络/资源限制阻断。
- HITL：plan 审批 / 修改 / 拒绝 / abort / 超时自动 abort 五种均有可重复测试。
- 事件可靠性：SSE 断线 30s 后重连可从 `Last-Event-ID` 续传，无重复无丢失。
- 资源回收：任务完成/失败/取消后，容器、workspace、临时 config、临时凭据全部清理。
- oh-my-opencode：doctor 通过；Prometheus/Sisyphus smoke tests 通过；Prometheus 计划被捕获为 `plan_ready`；`ultrawork` 路径可直接进入 `executing`；server/session lifecycle 可控且无残留。
- Artifacts：成功任务可拉取 diff、plan、transcript、stdout/stderr；失败任务仍可拉到诊断信息。

---

## 7. HITL 决策记录

规划阶段所有人工议题已闭环，结论如下；执行期若出现新议题再追加。

| # | 议题 | 决策 |
|---|---|---|
| H1 | Broker 与 MCP 关系 | **MVP 内 MCP 全部 stdio 本地化并打包入镜像**；Broker 不管 MCP 生命周期，仅做 HTTP egress 代理 |
| H1b | Broker 白名单粒度与下发 | 域名级 + 按 task 动态下发（TaskRequest.broker_policy） |
| H2 | 镜像分发与签名 | 本地构建 + GHCR 私有 tag，**不签名**（cosign 移 Phase 7） |
| H3 | `direct_execute` 失败自动升级 | **否**；仅发 `mode_escalation_suggested` 事件，由上游决策 |
| H4 | `safe_explore` 只读模式 | **不提供**（移 Phase 7） |
| H5 | `--debug-no-sandbox` | **不开放**；集成测试用 stub opencode HTTP server |
| H6 | 可观测性后端 | 仅暴露 `/metrics` + OTLP hook，**不锁后端** |
| H7 | 跨重启续跑 | **不提前**；重启后进行中任务标 `failed(orphaned)` |
| H8 | Broker 凭据代理（容器零密钥） | **否**；MVP env 注入 API key，零密钥移 Phase 7 |
| H9 | 数据保留 | workspace 立删 + artifact 7 天 + `DELETE /tasks/:id`；不做静态加密 |
| H10 | opencode auto-update 禁用 env | ✅ Spike 1b 收口：`OPENCODE_DISABLE_AUTOUPDATE=1`（boolean env）+ `autoupdate: false`（config），已写入 ADR-002 |
| H11 | oh-my 版本 pin | ✅ ADR-006 收口：pin `oh-my-openagent 3.17.2`（本机已验证，npm latest 4.1.1 升级推迟到 Spike 3 后确认） |
| H12 | `plan_first` 主入口 | ✅ ADR-001 收口：HTTP Server + `prompt_async` 传 `agent:"Prometheus"`；`oh-my-opencode run` 仅用于本机 smoke test；`/start-work` 作为可选验证，不阻塞 MVP |

---

## 8. 下一步执行顺序

### 8.1 立即执行（Phase 0 收口）

1. ✅ `ADR-001` 已输出（见 `docs/adr/ADR-001-opencode-adapter-mode.md`）：HTTP Server 唯一主路径；oh-my-openagent 以插件形式加载；Prometheus/Sisyphus 通过 `prompt_async` 的 `agent` 参数路由；CLI+tmux 与 oh-my run 黑盒均关闭；Phase 3/4 合并；H12 收口。
2. 把 endpoint spike 固化为 fixture：保存 message / prompt_async / diff / abort / permissions 的请求与响应样例，并在 adapter 测试中复用。
3. ✅ runtime 配置 spike 完成（Spike 1b）：`OPENCODE_DISABLE_AUTOUPDATE=1`、`OPENCODE_CONFIG_CONTENT` 内联注入（含 `{env:X}` 替换）、`OPENCODE_PERMISSION` 注入；ADR-002 H10 已回填；ADR-003 升级为纯 env 注入方案；证据见 §1.3。
4. 真实 permission HITL spike：通过 ask policy 触发真实 `per_*` request，确认 SSE 事件 payload，映射到 Worker `DecisionRequest`；推迟到 Phase 3 集成测试（知道 endpoint schema，不阻塞 Phase 1）。
5. 做 `/start-work` slash command spike：确认它在 `opencode serve` 会话内的事件形态，判断是否适合作为 `plan_first` 主入口；若不稳定，MVP 使用 `prompt_async + agent:Prometheus`。
6. 做真实但安全的 ultrawork 只读用例：使用临时 workspace，让 Sisyphus 读取一个小型 fixture 并产出报告，不允许 edit/bash，以验证事件、artifact、超时和残留进程清理。
7. ✅ `ADR-006` 已输出（见 `docs/adr/ADR-006-ohmy-version-and-entry.md`）：oh-my-openagent 3.17.2 pin；H11 收口。

### 8.2 下一批工程任务

1. 初始化 Python/FastAPI 项目骨架、pyproject、基础测试框架。
2. 先实现 contract schemas：TaskRequest / TaskEvent / Decision / Artifact / Error。
3. 实现 stub opencode server fixture，用于不依赖真实 LLM 的 adapter 单元/集成测试。
4. 实现 OpenCode HTTP adapter 的 health/session/event 基线，再补 permission/diff/abort。
5. 开始 Dockerfile spike：debian-slim + pinned opencode + pinned oh-my-opencode + 非 root 用户。

### 8.3 下一个 HITL 回顾点

Phase 0 收口后只需要人工确认两件事：

1. ~~oh-my 版本 pin~~：已在 ADR-006 收口，pin `oh-my-openagent 3.17.2`；升级到 4.1.1 推迟到 Spike 3 容器验证后。
2. `plan_first` 主入口：`prompt_async` + `agent:"Prometheus"` 已选定为 MVP 主路径；`/start-work` slash command 作为 Phase 3 可选优化。
