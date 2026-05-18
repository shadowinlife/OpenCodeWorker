# 后续工作建议

> **生成日期**：2026-05-15
> **来源**：基于 [strategy-artifact-and-scheduling.md v2.10](./strategy-artifact-and-scheduling.md)（5 题 + 16 OQ 全部闭环）
> **目的**：把 design doc 翻译成可立即执行的工作清单 + 跨 session 衔接指南

---

## 0. TL;DR

设计已 **implementation-ready**。下一步分三条线并行：

1. **本仓库（worker）线**：先关 P0/P1 修复（5 项），再做 Phase 6 的 3 个 SSE hooks
2. **上游 runtime 线**：起草 meta-skill `strategy-skill-author` + Strategy Registry 骨架
3. **MCP 线**：起 `LegoNanoBot/mcps/` 仓库 + 内部 vibe-trading fork 仓库

三条线**不强依赖**——可以并行启动。约 2~3 周可达成 X1 端到端验证。

---

## 1. 立即可启动的工作（按线分）

### 1.1 Worker 线（本仓库）

**P0/P1 backlog 进度**（来自 archive review）：

- ✅ **已闭环**：P0-4 / P0-5 / P0-6 / P0-7 / P0-8 + P1-9 / P1-10 / P1-11 / P1-12 / P1-13 / P1-14 / P1-15 / P1-17 / P1-20。逐项证据见
  [docs/archive/code-review-2026-05-14.md](../archive/code-review-2026-05-14.md) 各项的"修订"块。
- ⏸ **推迟 Phase 7**：P0-1 / P0-2 / P0-3（broker 三件套）。
- ❌ **仍 open**：P1-16（queue/orchestrator 状态双写）/ P1-18（git_subpath workspace 残留）/ P1-19（artifact GC 未实现）。详见 [roadmap §8](../roadmap/opencode-worker.md#8-sprint-backlog来源archivecode-review-2026-05-14md)。

**优先级 P1：Phase 6 SSE Hooks（design §5.3）**

| 任务 | 描述 | 工作量 |
|---|---|---|
| Conversations writer | 监听 SSE 组装 messages → JSONL，任务终态写入 `conversations/{slug}.jsonl` | 1 day |
| Backtest interceptor | 拦截 `tool_call_finished` 识别 `vibe-trading.backtest`，复制 vibe-trading runs/ 到 SKILL `backtests/` | 1 day |
| MCP field auto-recorder | 监听所有 `tool_call_finished`，按 (mcp, tool) 聚合实际 input/output 字段 → 回填 manifest | 2 days |
| 三 hooks 共享 SSE 拦截器抽象 | 重构出 `EventInterceptor` 基类 + 三个具体实现 | 1 day |

---

### 1.2 上游 Runtime 线

**优先级 P0：meta-skill 起草（OQ-1）**

新建：上游 runtime 仓库内（或共享 skills 仓库）`skills/strategy-skill-author/SKILL.md`

内容草稿（基于 design §5.5）：

```
---
name: strategy-skill-author
description: Meta-skill teaching Prometheus how to author a strategy SKILL bundle
category: meta
version: 0.1.0
---

# Strategy SKILL Author Guide

## 目的
在用户与 Prometheus 多轮对话后，产出一份**符合规范**的 strategy SKILL bundle，
可被任意 skill-aware agent 加载执行。

## 必备产物清单（Prometheus 自检）
- [ ] SKILL.md（含 frontmatter + 6 必备小节）
- [ ] code/signal_engine.py（class SignalEngine + generate + screen_today）
- [ ] config.json（vibe-trading 兼容）
- [ ] prompts/*.md（每个含 input_schema + output_schema + Offline Equivalent）
- [ ] manifest.json（env_lock 含 channel + 强锁字段）
- [ ] reason_template.md（fallback 模板）

## SKILL.md 必备小节
1. Purpose
2. When to Use
3. How to Invoke（每日选股 + 回测两条路径）
4. Inputs / Outputs
5. Dependencies（指向 manifest.json）
6. Provenance（指向 conversations/ + backtests/）

## frontmatter 字段
- name: kebab-case，正则 ^[a-z][a-z0-9-]{2,40}$
- description: 1 句话
- category: strategy
- version: SemVer

## prompts/*.md 必备小节
- frontmatter（prompt_id / input_schema / output_schema / model_default）
- Task / Input Format / Output Format（strict JSON）
- Examples
- Offline Quantization Equivalent（顶部含 quality）

## manifest.json env_lock 填法
- 必须显式声明 channel（community / internal-a-share）
- required_mcps[] 必须列每个 tool 的 required_input_fields / required_output_fields
- min_version 不留空
- source 写 git tag 或 pypi spec
```

**优先级 P0：Strategy Registry 骨架（D16）**

新建目录：`LegoNanoBot/strategies/`
- 创建 `INDEX.json`（schema 见 design §6.1）
- 写入空的初始内容 `{"schema_version": "1.0", "updated_at": "...", "strategies": {}}`
- 写一个 `README.md` 说明结构 + 哪些字段不可改

**优先级 P1：Agent Loader 校验器（D17 / OQ-7）**

实现一个 `strategy_skill_loader` 模块（Python lib，可被 Claude Code / opencode / vibe-trading-mcp 复用）：
- 读 manifest.json
- 按 design §6.2.1 顺序校验
- 输出 structured error
- 单元测试覆盖 5 个 stage 的失败/聚合场景

**优先级 P1：每日 scheduler 雏形**

最简 cron + Python 脚本：
- `00 09 * * 1-5 python run_strategy.py --skill ma250-pullback --date today`
- 脚本内：调用 agent loader → 拿 picks → 推送（暂时 print / 写文件）
- Phase X2 接通知通道（钉钉 / 邮件 / 微信）

---

### 1.3 MCP 线

**优先级 P0：仓库骨架（D18 / OQ-11）**

新建 GitHub repos（或同 monorepo 不同包）：
- `LegoNanoBot/mcps-trading-data-cn`
- `LegoNanoBot/mcps-historical-news-cn`

每个 repo 必备结构：
```
mcps-{name}/
├── README.md          # 工具列表 + 接入示例
├── pyproject.toml     # entry_point: {name}-mcp
├── src/{name}/
│   ├── __init__.py
│   ├── server.py      # stdio MCP server
│   └── tools/
├── schemas/           # 每个 tool 的 input/output JSON Schema
│   ├── get_security_meta.input.json
│   ├── get_security_meta.output.json
│   └── ...
├── tests/
└── CHANGELOG.md       # SemVer
```

**优先级 P0：内部 vibe-trading fork 仓库**

`LegoNanoBot/vibe-trading-a-share`（fork 自社区 vibe-trading）：
- 起草 `GOVERNANCE.md`（基于 design §6.7 的 4 类变更分类 + PR 准入流程）
- 删除非 A 股能力（crypto / forex / okx / ccxt loader）
- CHANGELOG 写入第一条 `revert: removed crypto/forex/okx/ccxt loaders for A-share focus`
- 添加 internal-only 字段（如 board / 龙虎榜 / 北向资金 — 后续逐步加）

**优先级 P1：MCP 自描述协议（D18 / D10）**

每个 MCP 必须实现：
- `list_tools()` —— 返回 `[{name, version, ...}]`
- `describe_tool(name)` —— 返回 `{input_schema, output_schema}`（JSON Schema）
- 顶层 `version` 字段

这是 Agent Loader env_lock validator 的依赖，没有这个，强锁机制无法落地。

---

## 2. 跨团队协作建议

### 2.1 三方接口对齐会议（建议第 1 周内）

**议题**：
- meta-skill `strategy-skill-author` 的内容是否合 worker / 上游 / MCP 三方理解
- INDEX.json schema 是否能服务上游 UI 需求
- MCP 自描述协议（schemas/ 内容格式）是否能被 Agent Loader 消费

**输出**：一份对齐纪要 + 三方各自的 implementation 启动信号

### 2.2 vibe-trading fork 启动会议

**议题**：
- 内部 fork 维护者是谁
- rebase 节奏定盘（design 建议 4 周）
- 哪些社区 PR 立即跟进，哪些观望
- 第一批要加的 internal-only 字段清单

### 2.3 MCP 字段表定盘

**前置**：先列业务**最小依赖字段**清单（不是"我能想到的所有字段"）。
**输出**：每个 MCP 的 v1.0 schemas/ 写定，后续按需 minor bump。

---

## 3. 实现路径（对应 design Phase X1/X2/X3）

### Phase X1：MVP 闭环（建议 2~3 周）

**Worker side**
- ✅ 完成所有 P0 修复（§1.1 P0 list）
- ✅ 完成 Phase 6 三个 SSE hooks

**上游 side**
- ✅ meta-skill v0.1 上线
- ✅ Strategy Registry 骨架
- ✅ Agent Loader 校验器（含 D17 完整校验流程）
- ✅ 最简 cron + 推送脚本

**MCP side**
- ✅ `trading-data-cn` v1.0（含 `get_security_meta` / `get_universe`）
- ✅ `historical-news-cn` v1.0（含 `query_news`）
- ✅ 内部 vibe-trading fork 起骨架（暂不删 community 能力）

**验证 DoD**
- 用户和 Prometheus 对话 30 分钟，产出一份 `ma250-pullback@0.0.1` SKILL
- 通过 Agent Loader 校验
- vibe-trading.backtest 跑通一份 baseline 回测
- 手工调用 daily runner，产出 ≤7 只 picks + reason_text + degradation_report
- 用户能看到完整 conversations + backtests 沉淀

### Phase X2：演进证据 + 调度（建议 +2 周）

- ✅ 接通知渠道（钉钉/邮件/微信）
- ✅ 用户迭代闭环（改一条规则 → opencode 出 0.0.2 → INDEX.json 自动更新 latest_active）
- ✅ degradation_report 在推送 UI 中显眼标注
- ✅ 内部 fork 第一批 internal-only 字段（如 board）落地，第一份 SKILL channel=internal-a-share

**验证 DoD**
- 连续 5 个交易日自动跑、自动推送
- 用户给一次反馈，触发新版本生成
- 至少一次降级路径被触发（手动断网测试）

### Phase X3：硬化 + 多 SKILL（建议 +3 周）

- ✅ 同时承载 ≥3 个 SKILL
- ✅ env_lock 校验严格化（含 schema introspection 边界 case）
- ✅ MCP 升级流程演练（minor bump → SKILL 自动 verify pass / major bump → 触发新 SKILL 版本）
- ✅ vibe-trading 内部 fork 完成首次 enhance-upstream PR

---

## 4. 风险清单与应对

| 风险 | 触发条件 | 应对 |
|---|---|---|
| **Prometheus 写 SKILL 不稳定** | 即使有 meta-skill，LLM 偶尔产出格式错误的 SKILL | 在 worker 任务终态前用 lightweight schema validator 做最后一道把关，不通过 → HITL 让用户决定回炉还是 force-accept |
| **MCP schema 漂移** | MCP 升级后字段名变了但忘了 major bump | Agent Loader 校验时 hard reject + 在内部 fork CI 加"schema diff vs 上一版"自动检测 |
| **vibe-trading 社区改 SignalEngine 接口** | 社区破坏 backtest runner 兼容 | 内部 fork 锁定 SignalEngine 接口为 internal-only； fork 不跟进社区破坏性改动 |
| **生产态 LLM 成本失控** | 多用户多 SKILL 并发，token 烧得快 | 上游 runtime 必须先有 per-tenant 月度预算（OQ-13）才上 X2 |
| **conversations 泄漏敏感信息** | 用户在对话中输入 API key 或个人信息 | conversations writer 加敏感信息扫描（API key / 身份证号 pattern），命中即脱敏写入 |
| **SKILL 版本爆炸** | 用户每天迭代 → 每天新版本 → registry 膨胀 | INDEX.json 加 retention 策略：保留 latest active + 最近 N 个 deprecated；其余压缩归档（Phase 7） |

---

## 5. 推荐的下一个 sc 命令序列

按时间顺序（**新 session 中执行**）：

### 第 1 步：把 design 翻译成具体 backlog
```
/sc:workflow Phase X1 implementation backlog
  --include worker-side, upstream-side, mcp-side
  --reference docs/design/strategy-artifact-and-scheduling.md
  --reference docs/design/follow-up-recommendations.md
```
**预期产出**：JIRA-style 任务列表，每项含 estimate + dependency。

### 第 2 步：细化 MCP 自描述协议
```
/sc:design "MCP 自描述协议规范"
  --type api
  --format spec
  --reference docs/design/strategy-artifact-and-scheduling.md §6.6
```
**预期产出**：JSON Schema 范式 + describe_tool 输出格式 + Agent Loader 比对算法。

### 第 3 步：细化 meta-skill 内容
```
/sc:design "meta-skill strategy-skill-author 内容"
  --type component
  --format spec
  --reference docs/design/strategy-artifact-and-scheduling.md §5.5
```
**预期产出**：完整可发布的 SKILL.md + checklist + 例子。

### 第 4 步：内部 fork GOVERNANCE.md 起草
```
/sc:design "vibe-trading 内部 fork GOVERNANCE 治理细则"
  --reference docs/design/strategy-artifact-and-scheduling.md §6.7
```
**预期产出**：完整 GOVERNANCE.md 草案。

### 第 5 步：开始实现
```
/sc:implement worker P0-5 P0-6 P0-7 fix
/sc:implement Phase 6 SSE hooks (conversations + backtest + field-recorder)
```

---

## 6. 长期建议（设计层面，不是 MVP）

这些不在 X1/X2/X3 范围，但建议团队**心里有数**，避免 MVP 决策无意中堵死未来路径：

### 6.1 跨 SKILL 共享物的演进
当 SKILL 数量 ≥ 5 后，会出现重复的 prompts / helpers。建议：
- 不要急着抽 shared library
- 先观察 3 个月，统计真正高频复用的部分
- 再决定是抽出 `LegoNanoBot/strategies-shared/` 还是让每个 SKILL 自包含

### 6.2 SKILL 演化为 Marketplace
当用户 ≥ 5 人后，会出现"我的 SKILL 借给其他用户用吗"的需求。建议：
- MVP 沿用单租户假设（D16）
- Phase 7 加入 `owner` / `sharing_policy` / RBAC 字段
- 不要在 MVP 提前优化，避免过度设计

### 6.3 回测与实盘的"完美对齐"
Offline Equivalent（D19）只能做到 approximate。要追求 exact，可以考虑：
- 每天把当日 LLM 输出**回写**到历史数据库
- 回测时直接用历史 LLM 输出，避免重新调用
- 这是高级玩法，Phase 7+ 评估

### 6.4 Prometheus 的"自学"循环
长期目标：让 Prometheus 看到自己产出的 SKILL 在生产态的实际表现，自动总结经验。
- 这需要 audit loop：daily runner 结果回流 → meta-skill 学习库 → 下次写 SKILL 用上
- 这是 agent self-improvement 的方向，Phase 7+

---

## 7. 文档维护说明

本文档与 [strategy-artifact-and-scheduling.md](./strategy-artifact-and-scheduling.md) **同步演进**：
- 当 design 文档有 v2.x 升级时，本文档对应 §1~§3 也要更新
- 当某项工作完成时，在本文档对应位置标 ✅
- 当发现新风险时，加入 §4
- 当某条 sc 命令执行后，在 §5 标记完成 + 链接产出文档

---

## 附录 A：相关文档索引

- 主设计：[strategy-artifact-and-scheduling.md](./strategy-artifact-and-scheduling.md)
- Worker code review（前置依赖）：[../archive/code-review-2026-05-14.md](../archive/code-review-2026-05-14.md)
- Worker roadmap：[../roadmap/opencode-worker.md](../roadmap/opencode-worker.md)
- ADR 索引：[../adr/](../adr/)

## 附录 B：未来 session 接续模板

新 session 开始时，把这两段贴进去帮助 Claude 快速入状态：

```
请基于以下两份文档继续工作：
1. docs/design/strategy-artifact-and-scheduling.md（v2.10，5 题 + 16 OQ 全闭环）
2. docs/design/follow-up-recommendations.md（implementation 路线图）

当前 Phase 进度：[X1 进行中 / X1 完成 / X2 进行中 ...]

本次 session 目标：[具体目标]

请先确认你已读懂上述两份文档的关键决策（D1~D19）和 architecture invariants（§11.3），
然后开始任务。
```
