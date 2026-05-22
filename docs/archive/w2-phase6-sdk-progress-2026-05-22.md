# W2-3/4 + W-DoD + Phase 6 T1/T2 + SDK Phase 1/2 Progress Snapshot — 2026-05-22

> **状态**：immutable archive snapshot
> **范围**：2026-05-21 ~ 2026-05-22 期间在本仓库（worker side + SDK lane）落地的工作
> **基线**：commit `57745c5`（W2-2 上线，[archive/w1-w2-progress-2026-05-20.md](w1-w2-progress-2026-05-20.md) 收尾）→ commit `b88e41d`（Worker Client SDK Draft v1 落地）
> **写入约定**：本文档不再修改。后续若有补丁以"修订"块**追加**而非覆盖；如有重大里程碑另起新归档。
>
> 上游来源任务清单：[claudedocs/workflow_phase_x1_implementation_backlog.md](../../claudedocs/workflow_phase_x1_implementation_backlog.md)
> 设计文档：
> - [docs/design/strategy-artifact-and-scheduling.md](../design/strategy-artifact-and-scheduling.md) — SSE hooks 业务约定
> - [docs/design/worker-client-sdk-interface-design.md](../design/worker-client-sdk-interface-design.md) — SDK 接口设计 Draft v1

---

## 0. TL;DR

| Lane / Task | 状态 | Commit | 说明 |
|---|---|---|---|
| W2-3 BacktestInterceptor | ✅ | `fac9c75` | `*.backtest` pattern + `iter-N` 自增 label + override |
| W2-4 McpFieldRecorder | ✅ | `fac9c75` | `(mcp_name, tool_name)` 聚合 + `mcp_field_summary.json` 独立 artifact |
| W-DoD smoke + usage-guide | ✅ | `cc2175b` | 端到端三类 artifact 同时落盘 + DB 登记 + `artifact_ready` 事件 |
| Phase 6 T1 HITL 时序 E2E | ✅ | `d459835` | 决策早到 / 晚到 / 重复幂等 / 超时 abort 四类边界 |
| Phase 6 T2 终态 driver/queue E2E | ✅ | `d459835` | HITL→abort / ResourceLimits→timed_out / SSE 订阅者唤醒 |
| Worker Client SDK Phase 1 + 2 | ✅ | `b88e41d` | async client + SSE 自动重连 + 终态等待 + HITL + artifact 下载 |

**累计完成度**：
- **W2 退出门**：3/3 拦截器 + 内置工厂注册 + purity gate + smoke + usage-guide ✓（等价于上一档 W2 4/4 内拦截器全部到齐）
- **Phase 6 退出门**：测试缺口 80% 闭环（T1 + T2 落盘，剩 T3 安全回归脚本化 / T4 Contract JSON Schema 校验）
- **SDK lane**：设计稿 Draft v1 → 实装 0.1.0；93% package coverage

**Test count**: 单元 197 → 245+；集成新增 9 用例（HITL 时序 4 + 终态 3 + W-DoD smoke 2）。

---

## 1. W2-3 — BacktestInterceptor

> **Commit**: [`fac9c75`](../../) feat(w2-3,w2-4): add BacktestInterceptor and McpFieldRecorder
> **代码**: [src/worker/adapters/opencode/interceptors/backtest.py](../../src/worker/adapters/opencode/interceptors/backtest.py)
> **测试**: [tests/unit/test_interceptor_backtest.py](../../tests/unit/test_interceptor_backtest.py) 16 PASS

### 1.1 行为

| 维度 | 落地 |
|---|---|
| 监听事件 | `tool_call_finished`，按 tool name 匹配 pattern（默认 `*.backtest`，可配置）|
| `run_dir` 抽取 | 优先 `raw_payload.part.metadata.run_dir`；缺省回退 `args.run_dir` |
| 复制策略 | 幂等：同一 `run_dir` 第二次见到只递增 label，不重复 copy |
| Label 决议 | 默认 `iter-N`（N 从 1 开始）；`raw_payload.part.metadata.backtest_label` override |
| 落盘路径 | `<artifacts_dir>/<task_id>/backtests/<label>/<run_dir basename>/...`（沿用 P0-8 子树约束）|
| Artifact metadata | `subtype: "backtest"` / `label` / `tool_name` / `source_run_dir` |

### 1.2 业务边界

- ✅ 拦截器层 grep gate 持续 green（不出现 `vibe-trading|signal_engine|ma250|strategy|skill`）
- ✅ 不感知"什么是回测/什么是策略"，只按 tool name pattern 收集 opaque 文件树

---

## 2. W2-4 — McpFieldRecorder

> **Commit**: [`fac9c75`](../../) （同上）
> **代码**: [src/worker/adapters/opencode/interceptors/mcp_fields.py](../../src/worker/adapters/opencode/interceptors/mcp_fields.py)
> **测试**: [tests/unit/test_interceptor_mcp_fields.py](../../tests/unit/test_interceptor_mcp_fields.py) 16 PASS

### 2.1 行为

| 维度 | 落地 |
|---|---|
| 监听事件 | 所有 `tool_call_finished` |
| 聚合 key | `(mcp_name, tool_name)`，mcp_name 提取正则可配置（默认 `^([a-z][a-z0-9-]+)\.`）|
| 输入字段 | `required_input_fields` ← `args` top-level keys |
| 输出字段 | `required_output_fields` ← `raw_payload.part.metadata.read_fields[]` |
| 终态写入 | 独立 artifact `mcp_field_summary.json`（**不**直接改 manifest.json）|
| Artifact metadata | `subtype: "mcp_field_summary"` / `mcp_count` / `tool_count` |

### 2.2 与上游的对齐

- ⛓ MCP 团队需提供 `describe_tool()` 自描述协议（M1/M2/M4），`mcp_field_summary.json` 才能在上游做字段校验闭环 — 详见 [§9.E 跨团队依赖](../roadmap/opencode-worker.md#9e--上游--mcp-团队不在本仓推进仅记录依赖)。

---

## 3. W-DoD — Smoke + usage-guide

> **Commit**: [`cc2175b`](../../) feat(w-dod): close W2 exit gate with smoke test and doc
> **测试**: [tests/integration/test_w_dod_smoke.py](../../tests/integration/test_w_dod_smoke.py) 2 PASS（completed + aborted 终态路径）
> **文档**: [docs/usage-guide.md](../usage-guide.md) 新增 `opencode_profile.interceptors` / `hitl_policy.auto_approve` 完整字段表 + 示例

### 3.1 退出验收链路

声明 3 个内置工厂（`conversations` / `backtest` / `mcp-fields`）→
driver `_dispatch_to_interceptors` 喂合成 SSE →
`_dispatch_terminal_and_flush` 触发 →
**三类 artifact 同时落盘** + DB 登记 + `artifact_ready` 事件，
覆盖 `completed` 与 `aborted` 双终态。

### 3.2 退出门（W2）

- [x] 3/3 拦截器实现（W2-2/3/4）
- [x] 内置工厂注册（[interceptors/__init__.py](../../src/worker/adapters/opencode/interceptors/__init__.py)）
- [x] purity gate 持续 green（[test_interceptor_purity.py](../../tests/unit/test_interceptor_purity.py)）
- [x] 端到端 smoke 关闭
- [x] usage-guide `opencode_profile.interceptors` 字段表完整化

剩余跨团队依赖（不阻塞本仓 W2 closeout）：
- U5 `summarize_callback` provider（让 conversations slug 更可读）
- X1 acceptance run（需上游 meta-skill `strategy-skill-author` v0.1）

---

## 4. Phase 6 — T1 + T2 集成测试缺口闭环

> **Commit**: [`d459835`](../../) test(phase6): close T1 + T2 integration test gaps
> **共享 fixture**: [tests/integration/conftest.py](../../tests/integration/conftest.py)（沿用 [tests/fixtures/stub_opencode_server.py](../../tests/fixtures/stub_opencode_server.py)）

### 4.1 T1 — HITL 时序

[tests/integration/test_hitl_timing_e2e.py](../../tests/integration/test_hitl_timing_e2e.py) 4 PASS：

| 用例 | 覆盖 |
|---|---|
| 决策**早到** | decision 在 `hitl_required` 之前 POST，driver 应等到事件后立即应用 |
| 决策**晚到** | decision 在 `decision_timeout_sec` 内但接近超时点到达 |
| **重复幂等** | 同一 `decision_id` + `idempotency_key` 二次提交不重复推进 |
| **超时 abort** | `decision_timeout_sec` 触发 → `hitl_timeout` 事件 → `expire_decision` → 容器 stop |

### 4.2 T2 — abort/timeout 终态 driver/queue E2E

[tests/integration/test_terminal_e2e.py](../../tests/integration/test_terminal_e2e.py) 3 PASS：

| 用例 | 覆盖 |
|---|---|
| HITL → abort 全链路 | `reason` / `decision_id` 透传到 `task_aborted` 事件 + `failed` task row |
| ResourceLimits → timed_out | `timeout_sec` 透传到 `TaskTimedOutError` 与 `task_timed_out` 事件；metrics counter 增量断言 |
| SSE 订阅者唤醒 | 事先订阅 `event_bus.Event`，终态写入后立即唤醒（无 polling）|

### 4.3 退出门（Phase 6 测试缺口）

- [x] T1 + T2 闭环（80% 缺口已补齐）
- [ ] T3 安全回归脚本化（需真实 Docker；CI 上可标 optional，**与 Phase 7 broker 一并推进**）
- [ ] T4 Contract JSON Schema 校验（上游契约层；与 SDK lane 解耦）

---

## 5. Worker Client SDK — Phase 1 + 2 一次到位

> **Commit**: [`b88e41d`](../../) feat(sdk): add Worker Client SDK with async client and retry policy
> **代码**: [src/worker_sdk/](../../src/worker_sdk/)（8 个模块文件）
> **测试**: [test_worker_sdk.py](../../tests/unit/test_worker_sdk.py) 33 + [test_worker_sdk_retry.py](../../tests/unit/test_worker_sdk_retry.py) 15 = **48 单测**（commit msg 43 计数为去重后；package coverage 93%）
> **使用文档**: [docs/worker-sdk-usage.md](../worker-sdk-usage.md)（660 行）+ [docs/worker-sdk-examples.md](../worker-sdk-examples.md)（602 行）

### 5.1 落地清单

| 文件 | 角色 |
|---|---|
| [src/worker_sdk/__init__.py](../../src/worker_sdk/__init__.py) | 公开入口；`AsyncWorkerClient` / 错误层 / 模型 / `RetryPolicy`；`__version__ = "0.1.0"` |
| [src/worker_sdk/client.py](../../src/worker_sdk/client.py) | `AsyncWorkerClient` 主体（HTTP + SSE + 终态等待 + 原子 artifact 下载）|
| [src/worker_sdk/sse.py](../../src/worker_sdk/sse.py) | SSE parser + `Last-Event-ID` cursor + terminal-event 检测 |
| [src/worker_sdk/retry.py](../../src/worker_sdk/retry.py) | 指数退避 + jitter + `Retry-After` 解析；默认重试 GET，POST 默认跳过保护非幂等 |
| [src/worker_sdk/auth.py](../../src/worker_sdk/auth.py) | Bearer token 注入 |
| [src/worker_sdk/compat.py](../../src/worker_sdk/compat.py) | 兼容性矩阵：SDK 0.1.x ↔ Worker 0.1.x |
| [src/worker_sdk/errors.py](../../src/worker_sdk/errors.py) | 分层错误：transport / HTTP / SSE / terminal-task |
| [src/worker_sdk/models.py](../../src/worker_sdk/models.py) | frozen dataclass 模型，不泄露 server-side Pydantic 类型 |

### 5.2 公开接口（与 [设计稿 §7](../design/worker-client-sdk-interface-design.md) 对齐）

`get_health()` · `assert_compatible()` · `create_task()` · `get_task()` · `abort_task()`
· `stream_events()`（自动重连）· `wait_until_terminal()` · `submit_decision()`
· `list_artifacts()` · `download_artifact_bytes()` · `download_artifact_to()` · `create_and_wait()`

### 5.3 不变量（守住）

1. **不发明新服务端能力**：SDK 仅包装现有 9 个 Worker endpoint
2. **不引入业务 DSL**：暴露 Worker 原生 task / event / decision / artifact 概念
3. **错误边界明确**：4 层异常类型对齐 HTTP transport / 5xx / SSE 协议 / terminal task 失败
4. **幂等保护**：默认重试 GET，POST 默认跳过；调用方可显式覆盖
5. **不泄漏 server 类型**：模型为 frozen dataclass，与 worker `contract/*.py` 解耦

### 5.4 测试矩阵

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| [test_worker_sdk.py](../../tests/unit/test_worker_sdk.py) | 33 | 协议 stub via httpx.ASGITransport / HITL decision flow / SSE 重连 / artifact 下载 |
| [test_worker_sdk_retry.py](../../tests/unit/test_worker_sdk_retry.py) | 15 | 退避 / jitter / `Retry-After` / GET vs POST 重试边界 |

---

## 6. 累计验证

### 6.1 Roadmap 影响

| 项 | 上一档（2026-05-20） | 本档（2026-05-22） |
|---|---|---|
| Phase X1 worker side | W1 ✅ + W2-1/2 ✅ + W2-3/4 ⬜ | **W2 全部 ✅ + W-DoD ✅** |
| Phase 6 测试缺口 | T1/T2 ⬜ + T3/T4 ⬜ | **T1/T2 ✅** + T3/T4 ⬜ |
| SDK lane | 设计 Draft v1 + 0 行实装 | **0.1.0 全量公开接口落地（Phase 1+2 一次到位）** |

### 6.2 不变量校验

- ✅ ADR-001（worker 业务无关）— 拦截器 grep gate + SDK 不嵌业务 DSL
- ✅ ADR-002 / ADR-006（opencode + oh-my 版本 pin）— 未触动镜像层
- ✅ P0-8（artifact 路径越权防御）— W2-3/4 沿用同款约束
- ✅ P1-9 / P1-10（WAL + per-task event lock）— 拦截器**不写** task_events 表，零冲突
- ✅ SDK 兼容性矩阵：`SDK 0.1.x ↔ Worker 0.1.x`（compat.py 静态断言）

### 6.3 仍 open（worker side）

详见 [roadmap §9](../roadmap/opencode-worker.md#9-当前-open-工作项2026-05-22)。

---

## 7. 参考

- [docs/roadmap/opencode-worker.md](../roadmap/opencode-worker.md) — 主路线图
- [docs/archive/w1-w2-progress-2026-05-20.md](w1-w2-progress-2026-05-20.md) — 上一档 snapshot（W1 + W2-1/2）
- [docs/archive/code-review-2026-05-14.md](code-review-2026-05-14.md) — 前置 review（含所有 P0/P1 详情）
- [docs/design/worker-client-sdk-interface-design.md](../design/worker-client-sdk-interface-design.md) — SDK 接口设计 Draft v1
- [docs/worker-sdk-usage.md](../worker-sdk-usage.md) — SDK 使用文档
- [docs/worker-sdk-examples.md](../worker-sdk-examples.md) — SDK 端到端示例

— end of snapshot —
