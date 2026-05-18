# Safe OpenCode Worker — Roadmap

> 本文档是 `VibeTradingOpenCodeWorker` 仓库的实施路线图。
>
> **当前阶段（2026-05-18 更新）**：
>
> | Phase | 状态 | 摘要 |
> |---|---|---|
> | Phase 0 | ✅ 已归档 | 基线 + ADR-001~006 全部 Accepted |
> | Phase 1 | ✅ 已归档 | Worker Contract + API 骨架（§1.4 smoke test 通过） |
> | Phase 2 | ✅ 已归档 | Docker Sandbox + Workspace；镜像 + 安全回归 7/7 PASS。Broker 三件套（P0-1/2/3）⏸ 推迟 Phase 7（详见 §H1b 与 [ADR-004](../adr/ADR-004-broker-boundary.md)） |
> | Phase 3 | ✅ 已归档 | OpenCode HTTP Adapter + oh-my agent 路由 + HITL；E2E 跑通 |
> | Phase 5 | ✅ 已归档 | HITL 闭环、超时事件、mode escalation、断线重连 |
> | Phase 6 | 🟡 部分完成 | 可观测性 + 单测齐备（118/118 pass）；P1-9/11/12 等 8 项 closed；集成测试 + 安全回归脚本化待补 |
> | Phase 7 | ⬜ 规划中 | 多租户 / 加密 / 跨节点 / broker 完整交付 |
>
> Sprint 0/1 P0/P1 详细闭环情况见 §8；逐项证据见
> [archive/code-review-2026-05-14.md](../archive/code-review-2026-05-14.md)
> （2026-05-14 全量 review，已归档；含 8 P0 + 12 P1 + 8 P2 + 测试缺口）。
>
> §1.3 / §1.4 保留 2026-05-13 的本机自检与骨架验证记录。
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
| Host Broker | **MVP 阶段不交付完整 broker 出口代理**：CONNECT 隧道与 broker 进程启停推迟到 Phase 7（详见 ADR-004 / code-review-2026-05-14 P0-1/2/3）；`src/broker/policy.py` 与 HTTP forward proxy 已实装但 lifespan 未启动；MVP 默认 `WORKER_BROKER_ENABLED=false`、`internal=False`，容器可直连外网 |
| MCP 形态 | **MVP 内所有 MCP 均为 stdio 本地服务并打包进镜像**（与 opencode/oh-my-opencode 一起版本 pin），不引入 HTTP/SSE MCP |
| 出网策略 | **域名级白名单** + **按 task 动态下发**（TaskRequest.broker_policy.allow_egress_hosts）；MVP 默认空，必须显式放行 |
| LLM 凭据 | **容器 env 注入 provider API key**；容器启动时基于 env 生成 opencode 配置文件，不挂载宿主登录态；零密钥/broker 凭据代理移 Phase 7 |
| 上游协议 | **HTTP + SSE 单协议** |
| Worker API 鉴权 | **静态 Bearer token**（env 配置） |
| Workspace bundle | **tar.gz**（流式上传 / 引用 URL）+ **git url + commit sha** |
| 并发模型 | **单 worker 进程 + 内置任务队列**，每任务一容器 |
| 基础镜像 | **ubuntu:24.04**（本地离线构建），当前 pin opencode 1.15.0 / oh-my-openagent 4.1.2 / MCP 版本；本地构建 + GHCR 私有 tag，不签名 |
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
  > ⚠️ **2026-05-14 修订**：MVP 阶段此条**未达成**——broker CONNECT 隧道与进程启停推迟到 Phase 7，容器实际可直连任意外网。详见 ADR-004 与 README §MVP 安全模型现状。
  > **§H1b（域名级白名单）实现推迟到 broker 联通之后**——白名单代码 (`src/broker/policy.py`) 已就位，但在 broker 真正接管出站流量前不会实际生效。
- 挂载用户主目录或宿主项目根目录为可写。
- HTTP/SSE 远程 MCP；MCP 全部 stdio + 随镜像发布。
- Broker 零密钥凭据代理、MCP server spawn/kill、跨重启任务续跑、`safe_explore` 只读模式、静态加密、cosign 镜像签名。

### 1.3 技术基线（已验证，2026-05-13~16）

| 验证项 | 结论 |
|---|---|
| opencode `1.15.0` serve endpoints | `health` / `agent` / plugin load 在 arm64 验证镜像中实测通过；其余 HTTP adapter endpoints 仍沿用 1.14.30 基线验证 |
| `OPENCODE_CONFIG_CONTENT` + `{env:X}` 注入 | 当前链路为 env 注入 + materialize 到 `~/.config/opencode/opencode.json`；`GET /config` 可校验配置 |
| `OPENCODE_PERMISSION` 注入 | 实测有效，白名单注入正常 |
| `OPENCODE_DISABLE_AUTOUPDATE=1` | 验证有效，双层禁用已写入 ADR-002 |
| oh-my-opencode `4.1.2` CLI | latest 版本已确认；本次修复未重跑宿主机 CLI smoke |
| 容器内 oh-my-openagent 加载 | `GET /agent` 实测：`1.15.0 + 4.1.2` 在 arm64 镜像中已成功加载，健康检查后约 12 秒出现 `Prometheus` / `Sisyphus` |
| arm64 验证镜像（1.15.0 / 4.1.2） | 镜像重建成功；entrypoint 通过 `/agent` 校验并输出 `verified oh-my-openagent agents loaded` |
| Phase 1 API 骨架 smoke test | 10 端点全部通过（2026-05-13）|

> 详细 spike 实测记录已归档，可通过 git history 查阅。

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
│  - MVP：可直连外网（broker 推迟到 Phase 7，见 ADR-004）  │
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
│ Host Broker (Phase 7 交付，MVP 不联通)                  │
│  - HTTP egress 代理（域名白名单 + 审计 hook）           │
│  - 不管理 MCP 生命周期；MVP MCP 为容器内 stdio 服务     │
│  - ACL / rate-limit / audit hook                        │
│  ⚠️ MVP 阶段此组件未在 lifespan 启动；容器直连外网      │
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
    "on_timeout": "abort",                  // 示例值；continue/escalate 会在 driver 端归一化为 approve fallback
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

### Phase 0 — 基线与技术 spike ✅

**交付物**：ADR-001~006 全部落地；Spike 1a/1b/3 完成（证据见 §1.3）。Spike 4/5（env 注入完整流程、Broker 原型）推迟至 Phase 7。

| Spike | 结论 |
|---|---|
| Spike 1a — serve endpoints | HTTP Server 路径完整验证；CLI+tmux 已由 ADR-001 关闭 |
| Spike 1b — runtime config | `OPENCODE_CONFIG_CONTENT` + `{env:X}` 纯 env 注入；ADR-003 升级 |
| Spike 2 — PTY POC | 已取消（ADR-001）|
| Spike 3 — 容器镜像 | Phase 2.5 历史镜像已归档；2026-05-16 已升级并验证 `opencode 1.15.0` + `oh-my-openagent 4.1.2` |
| ADR-001~006 | 全部 Accepted，见 `docs/adr/` |

---

### Phase 1 — Worker Contract 与 API 骨架 ✅

**交付物**：`src/worker/` 项目骨架完整初始化；Pydantic contract schemas（TaskRequest/TaskEvent/Decision/Artifact/Error）；FastAPI 10 端点含 Bearer token 中间件、SSE `Last-Event-ID` 回放、asyncio 任务队列、SQLite 4 表存储；smoke test 10/10 PASS（2026-05-13）。

---

### Phase 2 — Docker Sandbox + Workspace + Broker

> **状态：✅ 已归档（Phase 2 代码 commit df01b23；Phase 2.5 镜像构建 + 安全回归 2026-05-14；运行时 baseline 已于 2026-05-16 升级到 opencode 1.15.0 / oh-my-openagent 4.1.2）**
>
> **关联 review 项**：P0-1 / P0-2 / P0-3（broker 三件套）⏸ 推迟 Phase 7；P0-4（local workspace）✅ 已加 `WORKER_ALLOW_HOST_MOUNT` gate。详见 [archive/code-review-2026-05-14.md](../archive/code-review-2026-05-14.md)。

**主要交付**：Dockerfile（ubuntu:24.04 + opencode / oh-my-openagent 离线制品，sandbox uid 1000，`--read-only/cap-drop/pids-limit`）；`sandbox/manager.py` 完整生命周期；`workspace/handler.py`（zip-slip/symlink 防逃逸）；`broker/proxy.py` 域名白名单 SSRF 防护；Orchestrator 全链路（workspace→network→policy→container→cleanup）；安全回归 7/7 PASS（commit df01b23）。2026-05-16 已将运行时 baseline 升级到 `1.15.0 / 4.1.2`。

---

### Phase 3 — OpenCode HTTP Server Adapter（含 oh-my agent 路由，Phase 3/4 合并）

> **状态：✅ 已归档（commit e32c5e5，2026-05-14；E2E 天齐锂业分析跑通）**
>
> **关联 review 项**：P0-5 / P0-6 / P0-7（agent 名 + 终态事件）✅ 已于 2026-05-16 修复；P1-15（reject 阈值）✅ 已于 2026-05-18 修复。详见 [archive/code-review-2026-05-14.md](../archive/code-review-2026-05-14.md)。

**主要交付**：`adapters/opencode/client.py`（health/SSE/session/message/prompt_async/permission/diff/abort 全链路）；`event_stream.py` opencode↔TaskEvent 映射；`plan_first`（Prometheus）与 `direct_execute`（Sisyphus）双模式；`_handle_plan_approval` + `_handle_permission` HITL 路径；artifact 收集（diff + transcript）；E2E 天齐锂业分析跑通（commit e32c5e5）。

> ⚠️ Phase 4（oh-my Mode Adapter）已由 ADR-001 合并入 Phase 3，不再单独存在。

---

### Phase 5 — HITL 最小闭环 + 事件可靠性

> **状态：✅ 已归档（commit fbaa13b，2026-05-14）**
>
> **关联 review 项**：P1-13（HITL on_timeout）✅ 2026-05-16；P1-14（auto_approve）/ P1-10（event_id race）✅ 2026-05-18。详见 [archive/code-review-2026-05-14.md](../archive/code-review-2026-05-14.md)。

**主要交付**：统一 DecisionRequest（plan approval/tool permission/file write/broker egress/long-task continue）；HITL 超时 `default_on_timeout=abort`（`hitl_timeout` 事件 + `expire_decision` DB + abort + 容器 stop）；SSE `Last-Event-ID` 断线续传；Decision 幂等；`mode_escalation_suggested`（权限请求 ≥3 次触发）；重启后孤儿任务标 `failed(orphaned)`（commit fbaa13b）。

---

### Phase 6 — Worker Hardening

> **状态：🟡 部分完成，待收尾**
>
> **关联 review 项**：P1-9（WAL）/ P1-11（metrics callsites）/ P1-12（SSE 事件驱动）✅ 已于 2026-05-18 修复。集成测试（HITL 时序、安全回归脚本化）仍 pending —— 现仅有 `tests/fixtures/stub_opencode_server.py` 未串到 integration 用例。详见 [archive/code-review-2026-05-14.md](../archive/code-review-2026-05-14.md)。

- [x] 测试矩阵：
  - [x] 单元：state machine（test_state_machine.py）、event mapper（test_event_stream.py 41 用例）、permission mapper（test_permission_mapper.py）。
  - [x] 集成 fixture：stub opencode HTTP server（tests/fixtures/stub_opencode_server.py，FastAPI+uvicorn，可脚本驱动 SSE 事件）。
  - [ ] 契约：JSON Schema 校验上游 contract（Phase 7 补充）。
  - [ ] 安全回归：Phase 2 列表 + secret 泄漏扫描 + 容器 escape 尝试（需真实 Docker 环境）。
  - [ ] HITL 时序：决策早到 / 晚到 / 重复 / 超时边界集成测试（pending）。
- [x] 可观测性：
  - [x] 结构化日志（CorrelationFilter 注入 task_id / session_id / decision_id，observability/logging.py）。
  - [x] Metrics（Prometheus text format GET /metrics endpoint）：task_count、task_duration、hitl_wait_seconds、container_start_ms、abort_rate、token_usage（observability/metrics.py）。
  - [x] OpenTelemetry tracing hook 预留（ContextVar correlation 已就绪）。
  - [x] `/health`、`/ready` 已实现（routes.py）。
- [ ] 资源回收：
  - 容器/workspace/临时 config 在终态后清理（基础已在 orchestrator，TTL 策略 pending）。
  - artifact size limit、log truncation、event TTL（Phase 7）。
- [ ] 版本治理：
  - opencode / oh-my-opencode pin 列表 + 自动更新禁用验证。
  - 升级 playbook（spike → ADR → bump → 回归）。

Phase 6 退出检查：
- [x] 单元测试 118/118 通过（pytest tests/unit/）。
- [x] Prometheus /metrics 端点**格式**以 text/plain; version=0.0.4 响应。
- [x] **Metrics counter 接入** — `[REVIEW: P1-11]` 已于 2026-05-18 修复：queue / sandbox / driver 关键路径全部接入 counter / summary。
- [x] 结构化日志 correlation filter 通过语法检查 + 模块导入验证。
- [ ] 集成测试（HITL 时序、安全回归脚本化）需真实 Docker 环境，标记为 pending。
- [x] WAL 模式启用 — `[REVIEW: P1-9]` 已于 2026-05-18 修复，`init_db` 启用 WAL + synchronous=NORMAL + busy_timeout=5000。

---

### Phase 7 — 生产化 todo（仅占位）

- **Broker 完整交付**（P0-1/P0-2/P0-3）：CONNECT 隧道、`internal=True` 网络隔离、lifespan 启动 broker 进程。
- 多用户/多租户：认证、RBAC、租户配额、secret manager（Vault/KMS）。
- 强审计：hash chain、对象存储归档、查询 API、保留策略。
- 扩展性：Redis queue、多 worker、PostgreSQL state store、S3 artifact、K8s sandbox。
- 安全增强：egress proxy 细粒度白名单、MCP policy engine、seccomp/AppArmor、镜像签名（cosign）、SAST/DAST、SBOM/CVE。
- 上游生态：SDK、OpenAPI 发布、stdio JSON-RPC bridge、订阅态凭据代理方案。
- 任务恢复：进行中任务跨 worker 重启续跑 / 零密钥 Broker 代理 LLM 请求。
- 业务集成模板：vibe-trading MCP/skills 接入指南（仍在上游交付）。
- Safe-explore 只读模式 / auto_recommend 模式 / 静态加密 / 镜像 cosign 签名。

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
| H11 | oh-my 版本 pin | ✅ ADR-006 收口：当前 pin `oh-my-openagent 4.1.2`，并已完成 arm64 容器验证 |
| H12 | `plan_first` 主入口 | ✅ ADR-001 收口：HTTP Server + `prompt_async` 传 `agent:"Prometheus"`；`oh-my-opencode run` 仅用于本机 smoke test；`/start-work` 作为可选验证，不阻塞 MVP |

---

## 8. Sprint Backlog（来源：archive/code-review-2026-05-14.md）

> 完整 review 原文（含每项的修复证据 + 测试链接）见
> [`docs/archive/code-review-2026-05-14.md`](../archive/code-review-2026-05-14.md)。
> 本节只列**当前仍 open** 的条目；已闭环条目折叠为一行汇总。

### Sprint 0 — 正确性修复（阻塞合并）

**全部已闭环或推迟 Phase 7。**

| 条目 | 状态 |
|---|---|
| P0-4 / P0-5 / P0-6 / P0-7 / P0-8 | ✅ 已闭环（详见 archive） |
| P0-1 / P0-2 / P0-3 | ⏸ 推迟 Phase 7（broker 完整交付前 MVP 接受降级） |

### Sprint 1 — 可靠性与完整性

**已闭环（共 12 项）**：P1-9（WAL）/ P1-10（event_id race）/ P1-11（metrics callsites）/ P1-12（SSE 事件驱动）/ P1-13（HITL on_timeout）/ P1-14（auto_approve）/ P1-15（reject 阈值）/ P1-16（state 双写）/ P1-17（孤儿任务恢复）/ P1-18（git_subpath cleanup）/ P1-19（artifact GC）/ P1-20（README 同步）。逐项证据见 archive 各项的"修订"块。

**仍 open**：无（Sprint 1 全部清零；剩余测试覆盖缺口归并到下方"测试覆盖缺口"统一推进）。

### Sprint 2 — 代码质量

| ID | 问题 | 修复方向 |
|---|---|---|
| P2-21 | 顶层 lazy import 影响启动性能 | 将重型 import 移到函数内或启动 hook |
| P2-22 | `asyncio.get_event_loop()` 已废弃 | 替换为 `asyncio.get_running_loop()` |
| P2-23 | except 块吞异常不重抛 | 关键路径 except 加 `raise` 或 `logger.exception` |
| P2-24 | provider key 映射硬编码字符串 | 改用 enum / constants 避免拼写错误 |
| P2-25 | 模块 import 有副作用（DB 初始化） | 将副作用移入显式 `init()` 调用 |
| P2-26 | 死代码（已注释的旧 stub 函数） | 删除旧 stub |
| P2-27 | 未使用的常量/字段 | 清理 |
| P2-28 | silent error（swallowed exception 无日志） | 补 log + 考虑 Sentry/alerting hook |

### 测试覆盖缺口

| 范围 | 现状 | 目标 |
|---|---|---|
| Orchestrator 集成（HITL 时序） | 无 | stub server 驱动的决策早到/晚到/超时场景 |
| 安全回归脚本化 | 手工 | pytest + Docker fixture 自动运行 |
| Contract JSON Schema 校验 | 无 | 上游 TaskRequest/Event 通过 JSON Schema 严格校验 |
| abort/timeout 终态事件 | 已有单元测试覆盖（`tests/unit/test_terminal_exceptions.py`） | 补充 driver/queue 级端到端用例，覆盖真实终态落库与 SSE 输出 |
