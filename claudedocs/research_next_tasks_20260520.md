# Research: 下一步可展开的 Tasks 分析

> **Generated**: 2026-05-20
> **Source documents**: `docs/` 全树（roadmap + design + adr + archive）
> **Method**: 阅读 README、roadmap、3 份 design、code-review archive、X1 backlog plan，并核对 src/ 与 tests/ 现状
> **Scope**: 仅 worker 仓库当前可展开任务；上游 runtime / MCP 团队的任务列出但不落仓
> **Confidence**: High（信息全部来自仓内文档 + 当前代码盘点；不依赖 LLM 推断）

---

## 0. Executive Summary

仓库当前处在 **Phase 6 Worker Hardening 收尾 / Phase X1 (worker 侧 W2) 起步** 的过渡点：

- ✅ **Sprint 0 / Sprint 1 全部清零**：8 P0（含 5 项 closed + 3 项 deferred Phase 7）+ 12 P1 全闭环
- ✅ **W1（Worker P0/P1 前置修复）已完成**：W1-1 ~ W1-5 + W1-1b 在 commit `e2e6716`、`e35d858`、`c2a74a7`、`c3708a2`、`2d2298a`、`9fbb2bf` 中陆续落地
- 🟡 **Phase 6 测试缺口仍 open**：integration 目录空，仅 stub 用 fixture，安全回归未脚本化
- ⬜ **W2（Phase 6 SSE Hooks）尚未启动**：4 个任务全部 pending，是 Phase X1 worker DoD 的硬阻塞
- ⬜ **Worker Client SDK**：设计文档 v1 已完成，0 行实现代码
- ⬜ **Sprint 2 代码质量（P2-21 ~ P2-28）**：8 项全部 pending（非阻塞）

按 **优先级**排序，下一步可展开的 worker 侧任务有 **3 个 cluster + 2 个独立项**，详见 §3。

---

## 1. 现状盘点（2026-05-20）

### 1.1 已闭环 ✅

| 范围 | 证据 | 备注 |
|---|---|---|
| Phase 0 ~ 5 | roadmap §5 全部 ✅ | 不再 active |
| Sprint 0 P0-4 ~ P0-8 | archive code-review 各项"修订"块 | 5 项 closed |
| Sprint 0 P0-1/2/3（broker 三件套）| ADR-004 / roadmap §H1b | ⏸ Phase 7 |
| Sprint 1 P1-9 ~ P1-20（12 项）| commits `2d2298a` / `c3708a2` / `9fbb2bf` / `33fe197` | 全部 closed |
| W1-1 ~ W1-5（X1 P0 前置）| commits `e2e6716` / `e35d858` / `c2a74a7` 等 | X1 worker 前置门 |
| Phase 6 单元测试 118+ | tests/unit/ 共 19 个 test 文件 | 含 db_wal / event_id_race / metrics_wiring / orphan_recovery / state_flow / artifact_gc 等 |

### 1.2 仍 open ⬜（按归属分）

#### 1.2.1 Worker 仓库（本仓库）

| 类别 | 任务 | 估时 | 阻塞关系 |
|---|---|---|---|
| **W2 SSE Hooks** | W2-1 EventInterceptor 基类 | 1d | 阻塞 W2-2/3/4 |
| **W2 SSE Hooks** | W2-2 ConversationsWriter 拦截器 | 1d | 依赖 W2-1，被 U5 反向依赖 |
| **W2 SSE Hooks** | W2-3 BacktestInterceptor | 1d | 依赖 W2-1 |
| **W2 SSE Hooks** | W2-4 McpFieldRecorder | 2d | 依赖 W2-1 |
| **测试缺口** | Orchestrator 集成（HITL 时序）| ~1.5d | 复用现有 `tests/fixtures/stub_opencode_server.py` |
| **测试缺口** | 安全回归脚本化（pytest + Docker fixture）| ~2d | 需真实 Docker |
| **测试缺口** | Contract JSON Schema 校验 | ~0.5d | 上游协议端 |
| **测试缺口** | abort/timeout 终态事件 driver/queue 级端到端 | ~1d | 已有单元 ✓，缺 E2E |
| **Worker SDK** | 实现 `worker_sdk/` Python 包（13.1 节 8 步序）| ~4-5d | 0 代码，仅 design |
| **Sprint 2 P2 质量** | 8 项 lint / dead-code / asyncio API 升级 | 累计 ~3d | 非阻塞 |

#### 1.2.2 上游 Runtime（不在本仓）

| 任务 | 估时 | 备注 |
|---|---|---|
| U1 meta-skill `strategy-skill-author` v0.1 | 2d | 设计文档 v1 已完整（[meta-skill-strategy-skill-author.md](../docs/design/meta-skill-strategy-skill-author.md)） |
| U2 Strategy Registry 骨架 + INDEX.json | 0.5d | INDEX.json schema 已写定 |
| U3 Agent Loader + env_lock validator | 3d | D17 / OQ-7 |
| U4 Cron scheduler 雏形 | 1d | |
| U5 `summarize_callback` provider | 0.5d | 依赖 W2-2 |
| U6 fallback template registry | 0.5d | |
| U7 per-tenant cost ledger 骨架 | 1d | X2 之前可视化即可 |

#### 1.2.3 MCP / vibe-trading（不在本仓）

| 任务 | 估时 | 备注 |
|---|---|---|
| M1 `trading-data-cn` v1.0 | 2d | 依赖 M4 |
| M2 `historical-news-cn` v1.0 | 2d | 依赖 M4 |
| M3 `vibe-trading-a-share` fork + GOVERNANCE.md | 1d | OQ-10 |
| M4 MCP 自描述协议 spec + 参考实现 | 1d | gates U3、M1、M2 |
| M5 channel-aware `opencode_profile` | 0.5d | 依赖 M3 |
| M6 MCP schema CI gate | 0.5d | 依赖 M1/M2 |

---

## 2. 关键依赖关系

```
                                 ┌───────────────────────────────┐
                                 │ Phase X1 DoD: 端到端跑通       │
                                 │ ma250-pullback@0.0.1 SKILL    │
                                 └─────────────┬─────────────────┘
                                               │
        ┌──────────────────────────────────────┼────────────────────────────────┐
        ▼                                      ▼                                ▼
  Worker 侧 W-DoD                        Upstream U-DoD                    MCP M-DoD
        │                                       │                                │
   ┌────┴────┐                       ┌──────────┴──────────┐                  ┌───┴───┐
   │ W2-1    │ ←─── 阻塞 ───┐         │ U3 Agent Loader     │ ←─ 依赖 ── M4    │ M1 M2 │
   │ (基类)  │              │         │   + env_lock vldr   │                  │  M3   │
   └────┬────┘              │         └──────────┬──────────┘                  └───────┘
        │                   │                    │
   ┌────┼────┬───────┐      │                    │
   ▼    ▼    ▼       ▼      │                    │
 W2-2  W2-3  W2-4   测试    │                    ▼
   │                  │     │              U1 meta-skill
   │                  │     └──── 不强阻塞，可与 W2 并行
   └─── U5 callback ──┘
```

**关键路径**（calendar）：`M4 → M1/M2 → U3 → U4 → X1 acceptance`（≈ 7.5d，假设零空闲）。
worker 侧并行 slack：W-DoD 完成时间通常早于 U-DoD，因此**当前最优是先把 W2 推进到完成**。

---

## 3. 下一步可展开的 Worker 侧任务（按推荐顺序）

### 3.1 Cluster A — W2 SSE Hooks（P0，强烈推荐立刻启动）

**为什么是它**：
- W1 全部 closed → 无前置阻塞
- W2 是 worker 仓库**唯一**对 Phase X1 的硬贡献；不做 W2，SKILL bundle 的 conversations / backtests / mcp_fields 都无法落盘，X1 acceptance 跑不出来
- 每个子任务工作量小（1~2d），可独立 PR
- 设计 §5.3 已写明三个 hook 共享同一套 SSE 拦截基础设施 → W2-1 是 enabler

**落地顺序**（强制串行）：
1. **W2-1 EventInterceptor 基类**（1d）— 新建 `src/worker/adapters/opencode/interceptors/base.py`，提供 `on_event` / `on_terminal` / `flush` 三方法；driver `_consume_sse` 注入拦截器列表，errors isolated
2. **W2-2 ConversationsWriter**（1d）— 监听 `assistant_delta` / `tool_call_*` / `decision_received`，终态写 JSONL；slug 通过 `summarize_callback`（上游注入）+ regex 兜底；含敏感信息正则脱敏
3. **W2-3 BacktestInterceptor**（1d）— 匹配可配置 pattern（不硬编码 `vibe-trading`），从 tool args 提取 `run_dir` 复制到 `backtests/{ISO8601}-{label}/`；幂等
4. **W2-4 McpFieldRecorder**（2d）— 聚合 `(mcp_name, tool_name)` 维度的 input/output 字段；终态写 `mcp_field_summary.json`（**不**直接改 manifest）

**架构不变量（CI grep gate）**：
- `src/worker/` 内禁止出现 `vibe-trading|strategy|signal_engine|ma250` 任何字符串
- 拦截器对 SKILL.md 内容**完全不可见**，只产出 opaque artifact

**DoD**：
- 4 个拦截器全部接入默认 opencode profile
- 现有 `tests/e2e/test_tianqi_e2e.py` 不退化
- 新增 unit test 覆盖率 ≥ 80%
- 文档更新：`docs/usage-guide.md` 描述 `opencode_profile` 新字段（`summarize_callback`、`backtest_tool_pattern`、`mcp_name_prefix_regex`、`auto_approve` patterns）

**估时**：5 person-days；可压缩到 4d 如果 W2-2/3/4 部分并行（不同人）。

---

### 3.2 Cluster B — Phase 6 测试缺口（P1，并行启动）

**为什么是它**：
- Phase 6 退出检查最后 2 项（`集成测试 pending` / `安全回归脚本化 pending`）卡在测试缺口
- `tests/integration/__init__.py` 是空的，stub server fixture 已就位但**未串到任何测试**——投入产出比高
- abort/timeout 已有单元测试覆盖，但缺 driver→queue→DB→SSE 全链路用例

**子任务**：

| ID | 任务 | 估时 | 依赖 |
|---|---|---|---|
| T1 | HITL 时序集成测试（决策早到 / 晚到 / 重复 / 超时边界）| 1.5d | 复用 `tests/fixtures/stub_opencode_server.py` |
| T2 | abort/timeout 终态事件 driver/queue 端到端 | 1d | 与 T1 共享 fixture |
| T3 | 安全回归 pytest + Docker fixture 自动化 | 2d | 需真实 Docker（CI 上跑可选） |
| T4 | Contract JSON Schema 校验 | 0.5d | 上游契约协议 |

**先做 T1 + T2**（共 2.5d），它们能在不依赖真实 Docker 的情况下补齐 Phase 6 退出检查的 80%；T3/T4 可与 X2 一起规划。

---

### 3.3 Cluster C — Worker Client SDK（P1，可与 Cluster A/B 并行）

**为什么是它**：
- design v1 已完成（[worker-client-sdk-interface-design.md](../docs/design/worker-client-sdk-interface-design.md)）实现指南完整（§13）
- 上游 runtime 接 worker 不写 SDK 就要重复造轮子（SSE 重连 / Last-Event-ID / terminal 终态映射）
- 不依赖 W2 / U / M 任何任务，**纯独立**
- 是 Phase X1 上游 lane 真正动手前的低成本抢先项

**落地顺序**（设计 §13.1 已给）：
1. `AsyncWorkerClient.__init__` + 认证头注入（0.5d）
2. `get_health()` / `assert_compatible()`（0.5d）
3. `create_task()` / `get_task()` / `abort_task()`（0.5d）
4. `stream_events()` + 自动重连（1.5d，最复杂）
5. `wait_until_terminal()`（0.5d）
6. `submit_decision()`（0.5d）
7. `list_artifacts()` / `download_artifact_*()`（0.5d）
8. `create_and_wait()` convenience（0.25d）

**估时**：≈ 4.5d；可分 2 个 PR（前 3 步 + 后 5 步）。

**测试策略**（design §13.3）：
- L1 单元测试：错误映射、SSE parser、重连逻辑
- L2 协议测试：基于 `tests/fixtures/stub_opencode_server.py` 派生 worker stub
- L3 窄集成：与真实 worker 跑一次

**未列入仓库结构**：建议路径 `src/worker_sdk/`（与服务端 `src/worker/` 平级），将来好抽出独立包。

---

### 3.4 独立项 D — Sprint 2 代码质量（P2，机会战术）

**为什么不优先**：
- 8 项全是非阻塞 quality；不影响 Phase X1 任何 DoD
- archive review §建议修复顺序把它们排在 Sprint 2，明确"先关 Phase 6 / 再做 Phase X1 / 再回头"

**清单**（archive §P2 + roadmap §Sprint 2）：
- P2-21 顶层 lazy import（启动性能）
- P2-22 `asyncio.get_event_loop()` deprecated → 换 `get_running_loop()` / `to_thread()`
- P2-23 except 块吞异常不重抛
- P2-24 provider key 映射硬编码 → enum / constants
- P2-25 模块 import 副作用（DB 初始化）→ 显式 init
- P2-26 死代码 / 旧 stub
- P2-27 未使用的常量字段
- P2-28 silent error swallowed exception 无日志

**建议时机**：W2 完成后、Cluster B 整理 CI 时一起做；不主动起 task。

---

### 3.5 独立项 E — README + Roadmap 状态同步（已部分完成）

P1-20（README 同步）已 closed（commit `9fbb2bf`），但建议在 W2 完成后再做一轮："Phase X1 worker side complete" 标记 + CHANGELOG。

---

## 4. 推荐的执行计划（worker 仓库视角）

### 4.1 本周（第 1 周）

```
Day 1-2: 启动 Cluster A（W2-1 基类） + Cluster C（SDK 步骤 1-3）
         可两条线由不同人推进；如单人推荐先 W2-1
Day 3-5: W2-2 / W2-3 / W2-4（可部分并行）+ SDK 步骤 4-8
Day 5-6: 集成 W-DoD 检查 + grep gate CI + usage-guide 文档更新
```

### 4.2 第 2 周

```
Day 7-8:  Cluster B T1 + T2（HITL 时序 + 终态事件 E2E）
Day 9-10: 等上游 U-DoD 收敛；本仓机会战术做 Sprint 2 P2 项
```

### 4.3 第 3 周

```
- 配合上游做 X1 acceptance run（design §1.2 8 项检查）
- T3/T4 看 CI 容量决定本期是否做
- 起 W3（Phase 7 探路：broker CONNECT 隧道 spike）
```

---

## 5. 风险与缓解

| 风险 | 触发条件 | 缓解 |
|---|---|---|
| W2 hooks 写脏 worker 业务边界 | 拦截器 import 业务字符串 | CI grep gate（design 不变量 §11.3 / DoD §8 第 8 行）|
| ConversationsWriter 泄漏敏感信息 | 用户在对话中输入 API key | W2-2 接受标准已包含正则脱敏（API key / 18 位身份证） |
| McpFieldRecorder 字段不全 | 仅看 worker 端无法判断 output 字段 | upstream 用 Prometheus "只读用到的字段"双保险（OQ-9）；worker 接受 tool_result metadata `read_fields[]` 提示 |
| SDK 提前优化 multi-tenant | 上游需求未稳定 | design §11/§3 明确 SDK 不做 tenant；MVP 仅透传 metadata |
| 集成测试依赖 Colima | CI 无 Docker | T1/T2 不依赖 Docker（用 stub server）；T3 标 optional |

---

## 6. 关键文件索引（实施时必读）

| 主题 | 文档 |
|---|---|
| W2 hooks 设计 | [docs/design/strategy-artifact-and-scheduling.md §5.3](../docs/design/strategy-artifact-and-scheduling.md) |
| W2 任务接受标准 | [claudedocs/workflow_phase_x1_implementation_backlog.md §3.2](workflow_phase_x1_implementation_backlog.md) |
| Worker SDK 接口 | [docs/design/worker-client-sdk-interface-design.md](../docs/design/worker-client-sdk-interface-design.md) |
| meta-skill 内容（上游引用）| [docs/design/meta-skill-strategy-skill-author.md](../docs/design/meta-skill-strategy-skill-author.md) |
| review 原文 + 修订证据 | [docs/archive/code-review-2026-05-14.md](../docs/archive/code-review-2026-05-14.md) |
| 主路线图 | [docs/roadmap/opencode-worker.md](../docs/roadmap/opencode-worker.md) |

---

## 7. Recommendation 一句话版

**worker 仓库下一步推荐：先启 W2 SSE Hooks（4 任务，~5 人日），同步起 Worker Client SDK 第一阶段（~2 人日，独立人）；Phase 6 测试缺口 T1/T2 紧随其后（~2.5 人日）。Sprint 2 P2 质量项作为机会战术穿插，不主动排期。** 上游 / MCP 团队的工作不在本仓推进，但建议 W2-2 完成时同步触发 U5 callback 对接。

---

## 8. Boundaries

**本研究 will**：列出 worker 仓库下一步可展开的具体任务、依赖、估时、风险。

**本研究 won't**：实现任何代码、修改任何源文件、做架构决策。**用户决定下一动作**——典型选项：
- `/sc:implement W2-1 EventInterceptor 基类`
- `/sc:design "ConversationsWriter 详细规格"`（如需更细化）
- `/sc:implement worker_sdk MVP 第一阶段`
- 跨团队协调：M0 kickoff（design follow-up §10.1）

— end of research —
