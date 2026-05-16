# Design: Meta-Skill `strategy-skill-author`

> **状态**：Draft v1（2026-05-15）—— 收敛 7 项议题，implementation-ready
> **来源**：[follow-up-recommendations.md §1.2](./follow-up-recommendations.md#12-上游-runtime-线) P0 任务的细化；落实 [strategy-artifact-and-scheduling.md §5.5](./strategy-artifact-and-scheduling.md) / OQ-1
> **范围**：本设计回答**一个问题** —— 上游 runtime 维护的 meta-skill `strategy-skill-author` 该长什么样、Prometheus 如何使用它、它与 worker / agent loader 如何协作。
>
> **不在范围**：strategy SKILL bundle 自身规范（已在主设计 §3）、agent loader 实现细节（在 D17 / §6.2.1）、worker SSE hooks 实现（在 §5.3）。

---

## Changelog

- **v1（2026-05-15）**：基于 7 项议题讨论收敛 ——
  1. 阶段化激活（议题 1）
  2. Checklist 与 validator 完全独立（议题 2）
  3. L2 文档+checklist + 禁 dynamic key（议题 3）
  4. INDEX.json A+B 双通道注入 + 硬阻塞撞名 + 显式声明迭代（议题 4）
  5. degradation_policy 核心+条件必备 + custom.* 命名空间 + 自由文本+占位符（议题 5）
  6. reason_template f-string + 白名单 + 推荐风格中性后缀 + MVP 单模板（议题 6）
  7. provenance 分层填写 + 严格仅补不改 + 新增 `meta_skill_version`（议题 7）

---

## Table of Contents

- [1. 设计目标](#1-设计目标)
- [2. 关键决策（M1~M7）](#2-关键决策-m1m7)
- [3. Meta-Skill Bundle 结构](#3-meta-skill-bundle-结构)
- [4. 激活模型（M1 议题 1）](#4-激活模型m1)
- [5. 两道关卡协作（M2 议题 2）](#5-两道关卡协作m2)
- [6. 字段读取自律（M3 议题 3）](#6-字段读取自律m3)
- [7. strategy_id 撞名校验（M4 议题 4）](#7-strategy_id-撞名校验m4)
- [8. degradation_policy 速查表（M5 议题 5）](#8-degradation_policy-速查表m5)
- [9. reason_template.md 规范（M6 议题 6）](#9-reason_templatemd-规范m6)
- [10. provenance 责任矩阵（M7 议题 7）](#10-provenance-责任矩阵m7)
- [11. Meta-Skill 自身 SKILL.md 内容](#11-meta-skill-自身-skillmd-内容)
- [12. 模板文件正文](#12-模板文件正文)
- [13. 自检 Checklist 24 项](#13-自检-checklist-24-项)
- [14. 标杆 Examples](#14-标杆-examples)
- [15. 与主设计的关系](#15-与主设计的关系)
- [16. Phase 7 路标](#16-phase-7-路标)

---

## 1. 设计目标

| # | 目标 | 来源 |
|---|---|---|
| MG1 | Prometheus 在与用户对话后，**稳定产出**符合主设计 §3 规范的 strategy SKILL bundle | OQ-1 / 主设计 §5.5 |
| MG2 | 把"格式正确性"模板化，把 LLM 自由度限定在**内容层**而非结构层 | follow-up §4 风险清单 |
| MG3 | 与 worker SSE hooks（OQ-3 / OQ-9）和 agent loader（D17 / OQ-7）**职责互补不重叠** | 议题 2、3 共识 |
| MG4 | 演进路径清晰：发现 SKILL 质量问题 → 改 meta-skill → 下次产出自动跟进，**不改 worker** | 守 ADR-001 |
| MG5 | 不在 MVP 引入静态分析 / lint 工具；保留 Phase 7 升级路径 | 议题 3 共识 |

---

## 2. 关键决策（M1~M7）

| 决策 | 选择 | 替代 | 理由 |
|---|---|---|---|
| **M1. 激活模型** | **阶段化（澄清 → 规则成型 → 终态前自检）** + LLM 自判切换 + 与 vibe-trading SKILL 并存 | 首轮即 load / 触发词 / 全程激活 | 与 Prometheus 默认 plan_first 节奏吻合；早期不被规范拖累，终态前不漏自检 |
| **M2. 两道关卡** | **Checklist（生成期，meta-skill）与 validator（加载期，agent loader）完全独立** | Checklist 内嵌 validator 调用 / Checklist warn validator hard | 实现简单；职责边界清；加载期 hard reject 仍是最后一道硬门 |
| **M3. 字段读取自律** | **L2：模板注释 + checklist 一项 + 禁 dynamic key** | L1 仅文档 / L3 静态扫描 / L4 dataflow | MVP 不投入 lint；禁 dynamic key 让 Phase 7 升级到 L3 不返工 |
| **M4. strategy_id 撞名校验** | **A+B 注入（首条 message + strategy-registry-mcp）** + **i 硬阻塞**重命名 + **用户必须显式声明迭代** | 单通道 / LLM 自动建议替代名 / 隐式判定迭代 | 注入双通道避免单点失败；硬阻塞防误用；显式迭代避免误覆盖 |
| **M5. degradation triggers** | **核心 4 条必备 + llm_step_failed 条件必备 + custom.* 开放扩展** + **自由文本+推荐占位符** | 全部强制 / 全可选 / 关闭枚举 / 强模板 | 贴合战法实际能力；扩展性留给特殊场景；文案风格不僵化 |
| **M6. reason_template.md** | **f-string 占位符 + 变量白名单 + 风格中性后缀（推荐非强制）** + **MVP 单模板** | Jinja2 / 完全自由 / 强制后缀 / 多模板 | 简单可校验；与 D15 透明降级精神一致；多模板退 Phase 7 |
| **M7. provenance 填写** | **分层：Prometheus 写运行时可知字段 / Worker 终态补 commit + created_at** + **严格仅补不改** + **新增 `meta_skill_version`** | Prometheus 全填 / Worker 全覆盖 / 完全重写 | 各取所长；任一方失误不污染另一方；version 字段支撑后续 meta-skill 演进追溯 |

---

## 3. Meta-Skill Bundle 结构

```
strategy-skill-author/                              # 上游 runtime 仓库内
├── SKILL.md                                        ★ 主入口（§11 正文）
├── manifest.json                                   # meta-skill 自身的 manifest（精简版）
│
├── templates/                                      # ★ 4 个产出物模板（§12）
│   ├── strategy_SKILL.md.j2
│   ├── manifest.json.j2
│   ├── signal_engine.py.j2
│   ├── prompt.md.j2
│   └── reason_template.md.j2
│
├── checklists/                                     # ★ 自检清单（§13）
│   ├── pre_artifact.md                             # 终态前 24 项
│   └── degradation_policy_quick_ref.md             # §8 速查表
│
├── references/                                     # 参考资料
│   ├── envlock_field_examples.md                   # 各 MCP 字段示范
│   ├── reason_template_variables.md                # §9 变量白名单
│   └── activation_phases.md                        # §4 阶段化激活说明
│
└── examples/                                       # ★ 标杆样例（§14）
    ├── ma250-pullback/                             # 纯量化 + 简单 LLM 步骤
    └── us-aerospace-event/                         # 事件驱动型
```

**版本演进**：meta-skill 自身按 SemVer。每份产出 SKILL 的 `manifest.provenance.meta_skill_version` 记录所用 meta-skill 版本，供后续追溯（议题 7 / M7）。

---

## 4. 激活模型（M1）

### 4.1 三阶段定义

| 阶段 | 触发条件 | meta-skill 介入程度 | Prometheus 行为 |
|---|---|---|---|
| **① 澄清期** | 用户首条 message 进入 | **不激活** —— meta-skill 仅声明存在，不引用模板 | 自由对话；理解用户意图；Prometheus 默认 plan_first 行为 |
| **② 规则成型期** | Prometheus 自判：用户已对核心规则有 1 次以上肯定确认 | 载入 SKILL.md 模板 / manifest 模板 / 命名规范 | 起草 strategy_id（议题 4 校撞名）；起 manifest 骨架；写 signal_engine.py 草稿 |
| **③ 终态前** | Prometheus 自判：所有产物已草拟完成，准备触发 `final_acceptance` | 加载完整 24 项 checklist + degradation_policy 速查 | 逐项核对 checklist；不通过则 HITL 询问 |

### 4.2 阶段切换信号

阶段切换由 **Prometheus 自判**（LLM 推理），不由 worker 注入。判定依据见 SKILL.md "When to Use" 段（§11.2）。

| 切换 | 信号示例 |
|---|---|
| ① → ② | 用户回应"对，就按这个规则" / "那继续吧" / 主动开始具体参数讨论（如"窗口设 250 还是 200"） |
| ② → ③ | Prometheus 已起草所有 6 文件 + 与用户共识达成 + 准备调 vibe-trading.backtest 验证 |
| ③ → ② | Checklist 失败导致 HITL，用户决定回炉补；进入新一轮规则成型 |

### 4.3 与现有 skills 的并存

- meta-skill 不压制其他 skill。Prometheus 仍可在阶段 ② 期间调用 vibe-trading.* MCP 试跑回测
- 阶段切换**不**改变 MCP 可用性 / opencode_profile；仅改变 Prometheus 引用的"规范片段"

### 4.4 迭代场景特殊处理

用户在 messages 中显式声明"迭代 ma250-pullback"（M4 议题 4）时：
- 跳过阶段 ①
- 直接进阶段 ②，但 strategy_id 已锁定，跳过撞名校验
- version 自增（按 §3.6.0 SemVer 三段语义判定 PATCH/MINOR/MAJOR bump）

---

## 5. 两道关卡协作（M2）

### 5.1 关卡画像

|  | **Checklist（生成期）** | **env_lock validator（加载期）** |
|---|---|---|
| 归属 | meta-skill / Prometheus 自检 | Agent Loader Python lib（D17 / OQ-7） |
| 时机 | Prometheus 阶段 ③ 终态前 | 每次 daily runner / 回测 / agent load |
| 输入 | 整个 SKILL bundle 草稿（**不**调 MCP） | 已冻结 SKILL bundle + 当前环境实际 schema |
| 校验对象 | **结构 + 内容**：6 文件齐 / frontmatter 合规 / Offline Equivalent 章节存在 / 24 项自检 | **环境一致性**：MCP channel/version/字段、env、DB |
| 失败动作 | HITL 让用户决定回炉 / force-accept | structured error + remedy_hint，默认 hard reject |
| 严苛度 | 软门（用户可 force-accept） | 硬门（仅 `--force-channel-mismatch` 一种 escape） |

### 5.2 完全独立 ≠ 不通信

虽然两关卡**互不调用**，但通过**SKILL bundle 自身**间接传递信息：

```
   生成期               加载期
┌──────────┐        ┌──────────────┐
│Prometheus│ writes │ SKILL bundle │ reads ┌───────────┐
│Checklist │───────▶│   manifest   │──────▶│ validator │
└──────────┘        │   prompts    │       └───────────┘
                    │   code       │
                    └──────────────┘
```

Prometheus 在 manifest 中**如实声明**字段表（议题 3 自律 + worker 拦截器 ground truth），validator 加载期再硬校。任何一方不调用对方。

### 5.3 失败回退路径

| 关卡 | 失败 | 处理 |
|---|---|---|
| Checklist | 6 文件不齐 / Offline Equivalent 缺 / frontmatter 不合规 | Prometheus 回炉补；HITL 询问"还需要哪些规则" |
| Checklist | 用户连续 3 次 HITL 仍不确定 | 提供 force-accept 选项（标 `quality: degraded` 写入 CHANGELOG） |
| validator (加载期) | schema_diffs / missing_env | hard reject + structured error；用户读 remedy_hint 自决：升级 MCP 或回研究态 |
| validator (加载期) | channel mismatch | hard reject，除非启动带 `--force-channel-mismatch`（D17） |

### 5.4 已知风险（接受）

- Checklist 不调 MCP → 可能出现"自检通过 / 加载失败"扯皮 → **接受**：因为 worker 拦截器（OQ-9）已在生成期把字段集回填，加载期失败更多源自环境本身（MCP 升级、env 缺失），与 Prometheus 自检无关
- 风险监控：若 X1/X2 真实运行中频繁出现"自检通过 / validator 失败"，Phase 7 升级到模型 B（Checklist 内嵌 validator）

---

## 6. 字段读取自律（M3）

### 6.1 核心规则

`code/signal_engine.py` 中**只能读你真正用于决策的字段**；列名必须是**字面量字符串**，禁止 dynamic key。

```python
# ✅ 合规
close = df["close_adj"]
ma250 = df["close_adj"].rolling(250).mean()

# ❌ 违规：dynamic key（Phase 7 升级到 L3 时会被 lint 拒）
col_name = config["price_col"]
close = df[col_name]

# ❌ 违规：凑数读
volume = df["volume"]      # 实际用到
turnover = df["turnover"]  # 没参与决策但读了
```

### 6.2 模板注释（signal_engine.py.j2 顶部）

```python
"""
{{ strategy_id }} - SignalEngine

★ 字段读取约定（meta-skill M3）：
  1. 仅读取真正用于决策的列；勿"凑数读"
  2. 列名必须是字面量字符串，禁用 df[var_name]
  3. worker 拦截器会自动回填 manifest.tools_used.required_output_fields
     —— 你读什么，manifest 就锁什么；读多了会污染 manifest
"""
```

### 6.3 Checklist 一项

> **§13 第 12 项**：我已审阅 signal_engine.py 中所有 `df[...]` 引用，确认每列都参与决策；无 dynamic key。

### 6.4 与 worker 拦截器的协作

| 维度 | Prometheus 自律（M3） | Worker 拦截器（OQ-9） |
|---|---|---|
| 角色 | 减少误差 | ground truth |
| 失败影响 | manifest 字段集膨胀 | 完全失效（最坏情况） |
| MVP 兜底 | 拦截器永远是最终源 | — |

→ 两者乘法效应：Prometheus 严格 → manifest 干净；Prometheus 偷懒 → 拦截器仍能给出实际字段集，但污染了。

---

## 7. strategy_id 撞名校验（M4）

### 7.1 INDEX.json 双通道注入

| 通道 | 实现 | 用途 |
|---|---|---|
| **A. 首条 message 注入** | UR 起任务前读 `LegoNanoBot/strategies/INDEX.json` 内容，拼进首条 user message | 阶段 ② 撞名校验首选；MVP 战法少（1~3 个），token 成本可忽略 |
| **B. strategy-registry-mcp** | 上游起一个轻量 MCP，工具：`list_strategy_ids()` / `get_strategy(id)` / `get_index_snapshot()` | 实时；多 SKILL 场景 token 成本可控；为 Phase X3 search/list 留扩展点 |

**MVP 选择**：A 必备（首条 message 注入），B 同步实现（即使战法少也起，避免 token 爆）。两者并存，Prometheus 可任选其一查询。

### 7.2 校验流程

```
阶段 ② 进入时（用户已确认核心规则）
  │
  ▼
Prometheus 起草 strategy_id 候选（kebab-case，正则 ^[a-z][a-z0-9-]{2,40}$）
  │
  ▼
查 INDEX.json（A 通道直读 / B 通道调 list_strategy_ids）
  │
  ├─ 命中（撞名）？─→ HITL：「ma250-pullback 已存在；请改名」
  │                    │
  │                    └─→ 用户改名 → 回到查 INDEX.json
  │
  └─ 未命中 ─→ 锁定 strategy_id，继续阶段 ②
```

### 7.3 迭代场景（不查撞名）

判定为迭代的**唯一信号**：用户在 messages 中**显式**表达"迭代 / 改 / 升级 ma250-pullback"等意图（M4 议题 4 共识）。

迭代场景下：
- strategy_id 已锁定，**跳过撞名校验**
- workspace 应是 tarball，含 `{strategy-id}/{old-version}/`（§6.4）
- version 自增（PATCH/MINOR/MAJOR 由 Prometheus 按 §3.6.0 判定）

### 7.4 失败回退

- INDEX.json 注入失败（A 通道）+ MCP 不可用（B 通道）→ 阻塞阶段 ②，HITL 通知用户："撞名校验通道全失效，请人工确认 strategy_id 唯一性"
- 用户拒绝改名（坚持撞名）→ 阻塞至用户接受改名

---

## 8. degradation_policy 速查表（M5）

### 8.1 必备分类

| 类别 | trigger | 何时必备 |
|---|---|---|
| **核心 4 条**（任何 SKILL 必备） | `required_mcp_unavailable` | always |
|  | `required_field_missing` | always |
|  | `picks_count_below_target` | always |
|  | `data_freshness_violation` | always |
| **条件必备** | `llm_step_failed` | 当 `manifest.llm_steps` 非空（即任意 prompts/*.md 会被 agent 调用） |
| **可选扩展** | `custom.*` | 战法特殊场景（命名空间见 §8.3） |

### 8.2 6 条规范结构

| trigger | 必备字段 | 默认 action | report_text 推荐占位符 |
|---|---|---|---|
| `required_mcp_unavailable` | `mcp` | `abort_with_report` | `{mcp_name}` |
| `required_field_missing` | `field`（可填 `"any"`） | `skip_affected_picks` | `{count}`、`{field}` |
| `picks_count_below_target` | （无） | `push_partial` | `{actual}`、`{target}` |
| `data_freshness_violation` | `max_lag_minutes` | `push_with_warning` | `{lag_minutes}` |
| `llm_step_failed` | `step`（与 manifest.llm_steps 中 key 一致） | `skip_step` 或 `fallback_to_template`（如 pick_explanation） | `{step}`、`{error_kind}` |

### 8.3 custom.* 命名空间约束

允许新增 trigger，必须满足：

1. trigger 名以 `custom.` 起头，如 `custom.event_window_expired`
2. SKILL.md 必须用 1~2 句解释为何不能用核心 6 条覆盖
3. 必填字段 ≥ `trigger` / `report_text`（其他字段 Prometheus 自定）
4. agent loader 看到 `custom.*` 时默认按 `push_with_warning` 处理（除非 manifest 显式 override）

### 8.4 写法范例（manifest.json 片段）

```jsonc
"degradation_policy": {
  "rules": [
    {"trigger": "required_mcp_unavailable", "mcp": "any", "action": "abort_with_report",
     "report_text": "{mcp_name} 不可用，本日无法产出选股；建议人工介入"},
    {"trigger": "required_field_missing", "field": "any", "action": "skip_affected_picks",
     "report_text": "{count} 只候选因关键字段缺失被剔除"},
    {"trigger": "picks_count_below_target", "action": "push_partial",
     "report_text": "本日仅命中 {actual} 只（目标 {target}）"},
    {"trigger": "data_freshness_violation", "max_lag_minutes": 60, "action": "push_with_warning",
     "report_text": "数据延迟 {lag_minutes} 分钟于 asof_time，可能影响命中精度"},
    {"trigger": "llm_step_failed", "step": "event_sector_filter", "action": "skip_step",
     "report_text": "事件板块过滤步骤失败，本日选股未叠加事件驱动语义"},
    {"trigger": "llm_step_failed", "step": "pick_explanation", "action": "fallback_to_template",
     "report_text": "命中说明 LLM 失败，已用模板生成"},
    // 战法特有：事件窗口过期
    {"trigger": "custom.event_window_expired", "action": "abort_with_report",
     "report_text": "事件触发窗口（T-3~T-1）已过，本日不产出"}
  ]
}
```

### 8.5 Checklist 校验

§13 第 13 / 14 项强制核对：
- 第 13 项：核心 4 条全部出现
- 第 14 项：若 manifest.llm_steps 非空，至少一条 `llm_step_failed`

---

## 9. reason_template.md 规范（M6）

### 9.1 文件位置与角色

`reason_template.md` 位于 SKILL bundle 根目录（与 prompts/ 同级）。当 `prompts/pick_explanation.md` LLM 调用失败 / 超时 / 输出不合 schema 时，agent 落回此模板生成 reason_text 推送给用户（D12 / OQ-14）。

### 9.2 模板引擎

**f-string-like 占位符**：`{var}` / `{var:format}` / `{list_var|join('、')}`

不引入 Jinja2（避免 if/for 复杂度失控；MVP 单战法不需要）。

### 9.3 frontmatter 必备字段

```markdown
---
template_id: pick_explanation_fallback
schema_version: "1.0"
allowed_variables:
  - symbol               # str, 必备
  - name                 # str, 必备
  - score                # float, 必备
  - matched_rules        # list[str], 用 |join('、') 拼
  - fields.<key>         # 任意字段；点路径访问 pick.fields[key]
  - today                # date
  - sector_filter        # list[str], 可能为 []
  - degraded_steps       # list[str], 来自 degradation_report
  - confidence           # str (high/medium/low), pick.confidence
max_chars: 200
max_lines: 2
---
```

### 9.4 变量白名单约束

- frontmatter 的 `allowed_variables` 是**严格白名单**
- 模板正文中出现白名单外的占位符 → agent 渲染时拒绝（防 typo）
- meta-skill 在 §13 checklist 第 17 项校验白名单一致性

### 9.5 风格中性后缀（推荐非强制）

- meta-skill 推荐：模板末尾加 `[模板版]` 或类似中性标识，让用户分辨这条 reason_text 是模板生成的而非 LLM 的
- 不强制：考虑战法可能希望"模板看起来像 LLM 输出"以保持品牌风格一致
- 与 D15 透明降级精神一致：即使不在 reason_text 标识，degradation_report 中也会标 `pick_explanation` 已降级

### 9.6 长度限制

| 项目 | 限制 | 理由 |
|---|---|---|
| 单条 reason_text | ≤ 200 字符 | 与 LLM `max_tokens=256` 大致对齐 |
| 行数 | 1~2 行 | 推送 UI 友好 |
| 变量数 | ≤ 6 | 控制信息密度 |

### 9.7 标杆模板（meta-skill 提供）

```markdown
---
template_id: pick_explanation_fallback
schema_version: "1.0"
allowed_variables:
  - symbol
  - name
  - score
  - matched_rules
  - fields.ma250_diff_pct
  - fields.volume_ratio
  - today
  - degraded_steps
max_chars: 200
max_lines: 2
---

{name}（{symbol}）：命中 {matched_rules|join('、')}；
价格相对 250 日线偏离 {fields.ma250_diff_pct:.2%}，量比 {fields.volume_ratio:.1f}。[模板版]
```

### 9.8 多模板（Phase 7）

按 `degraded_steps` 路由不同模板（如纯 LLM 失败 vs 字段缺失）退 Phase 7。MVP **固定一条**。

---

## 10. provenance 责任矩阵（M7）

### 10.1 字段填写责任表

| 字段 | 写入方 | 来源 | Prometheus 失败时退化 |
|---|---|---|---|
| `created_by` | Prometheus（模板写死 `"opencode-worker"`） | manifest.json.j2 模板 | — |
| `worker_commit` | **Worker 终态 writeback** | git rev-parse HEAD（worker 启动时已知） | — |
| `opencode_version` | Prometheus | 沙盒内 `opencode --version` | 写 `""`，worker 用镜像声明值补 |
| `ohmy_version` | Prometheus | 沙盒内对应版本读取命令 | 写 `""`，worker 用镜像声明值补 |
| `primary_session_id` | Prometheus | opencode 自报 session_id | 写 `""`，worker 从 sandbox 元数据补 |
| `created_at` | **Worker 终态写** | 任务终态时间戳 ISO8601 | — |
| `meta_skill_version`（新增） | Prometheus | meta-skill 自身 frontmatter 注入 | **阻塞终态 + HITL**（这个不该失败） |

### 10.2 严格仅补不改约束

Worker writeback 时：
- 仅当字段为空字符串 `""` 或 `null` 时填入
- 非空字段一律保留（以 Prometheus 沙盒读到的为准；避免 image drift）
- 未在 §10.1 责任表中的字段不动

### 10.3 manifest.provenance 完整范例

```jsonc
"provenance": {
  "created_by": "opencode-worker",
  "worker_commit": "e32c5e5",                   // worker 终态补
  "opencode_version": "1.15.0",                 // Prometheus 写
  "ohmy_version": "4.1.2",                      // Prometheus 写
  "primary_session_id": "sess_xxx",             // Prometheus 写
  "created_at": "2026-05-15T10:30:00+08:00",    // worker 终态写
  "meta_skill_version": "1.0.0"                 // ★ Prometheus 写（meta-skill 自身版本）
}
```

### 10.4 Phase 7 扩展候选

退 Phase 7 评估的字段：
- `git_remote_url` — meta-skill 来源 URL
- `prometheus_model` — 当时使用的 LLM model id

---

## 11. Meta-Skill 自身 SKILL.md 内容

> 下面是 meta-skill 主入口 `strategy-skill-author/SKILL.md` 的完整正文。Prometheus 通过 opencode_profile 注入后会读到这份文档。

```markdown
---
name: strategy-skill-author
description: Meta-skill teaching Prometheus how to author a strategy SKILL bundle
category: meta
version: 1.0.0
---

# Strategy SKILL Author Guide

## 11.1 Purpose
在用户与 Prometheus 多轮对话后，产出一份**符合主设计 §3 规范**的 strategy SKILL bundle，
能通过 agent loader 的 env_lock validator 校验，被任意 skill-aware agent 加载执行。

## 11.2 When to Use
本 meta-skill 按**三阶段**激活，由你（Prometheus）自判切换：

| 阶段 | 进入条件 | 行为 |
|---|---|---|
| ① 澄清期 | 用户首条 message | **不引用本 meta-skill 内容**；自由对话理解意图；plan_first 默认行为 |
| ② 规则成型期 | 用户已对核心规则有 1 次以上肯定确认（"对，就按这个" / 主动讨论参数） | 载入 `templates/` 起草产物；按 §11.4 流程查 strategy_id 撞名 |
| ③ 终态前 | 6 文件已草拟完整 + 与用户共识达成 | 跑完 `checklists/pre_artifact.md` 24 项；不通过 → HITL |

迭代场景：用户**显式声明**"迭代 / 改 / 升级 {既有 strategy_id}" → 跳过 ①，直接 ②；strategy_id 已锁，跳过撞名校验。

## 11.3 How to Invoke

### 阶段 ② 起草顺序
1. 锁定 strategy_id（按 §11.4）
2. 起草 `manifest.json`（用 `templates/manifest.json.j2`）
3. 起草 `code/signal_engine.py`（用 `templates/signal_engine.py.j2`，遵守 §11.5 字段读取约定）
4. 起草 `prompts/*.md`（每个用 `templates/prompt.md.j2`，**强制**包含 Offline Quantization Equivalent 章节）
5. 起草 `reason_template.md`（用 `templates/reason_template.md.j2`，遵守 §11.6 变量白名单）
6. 起草 `SKILL.md`（用 `templates/strategy_SKILL.md.j2`，6 小节齐备）
7. 调 `vibe-trading.backtest` 验证（worker 自动复制结果到 `backtests/`）
8. 进入阶段 ③，跑 checklist

### 阶段 ③ 终态前自检
- 逐条核对 `checklists/pre_artifact.md` 24 项
- 不通过 → HITL 询问用户：「以下 N 项未达标：[list]。回炉补 / force-accept / 取消？」
- force-accept 时：在 CHANGELOG 标 `quality: degraded`

## 11.4 strategy_id 撞名校验（详见设计 §7）

格式：kebab-case，正则 `^[a-z][a-z0-9-]{2,40}$`

查 INDEX.json：
1. **A 通道**：首条 user message 已注入 INDEX.json 完整内容 → 直接读
2. **B 通道**：调用 `strategy-registry-mcp.list_strategy_ids()` —— 实时

撞名 → HITL 硬阻塞：「{candidate_id} 已存在，请改名」（不允许 LLM 自动建议替代名）

## 11.5 字段读取约定（详见设计 §6）

`code/signal_engine.py` 中：
- 仅读真正用于决策的列
- 列名必须是字面量字符串：`df["close_adj"]` ✅；`df[var]` ❌
- worker 拦截器会基于实际读取自动回填 `manifest.tools_used.required_output_fields`
- 你读什么，manifest 就锁什么；读多了会污染 manifest

## 11.6 reason_template.md 约定（详见设计 §9）

- 引擎：f-string-like，禁 Jinja2 if/for
- frontmatter 必含 `allowed_variables`（严格白名单）
- 推荐文末加风格中性后缀（如 `[模板版]`）
- 长度 ≤ 200 字符 / 1~2 行 / 变量 ≤ 6

## 11.7 degradation_policy 必备项（详见设计 §8）

- **核心 4 条必备**：`required_mcp_unavailable` / `required_field_missing` / `picks_count_below_target` / `data_freshness_violation`
- **条件必备**：若 `manifest.llm_steps` 非空，至少一条 `llm_step_failed`
- **扩展**：用 `custom.*` 命名空间，且在 SKILL.md 解释理由

## 11.8 provenance 字段填写（详见设计 §10）

| 你（Prometheus）填 | Worker 终态补 |
|---|---|
| created_by, opencode_version, ohmy_version, primary_session_id, **meta_skill_version** | worker_commit, created_at |

读不到版本时：写 `""`，worker 退化补镜像声明值。**`meta_skill_version` 不能为空**（这是 meta-skill 自身的 version frontmatter，永远可读）。

## 11.9 Inputs / Outputs

| 输入 | 来源 |
|---|---|
| 用户对话历史 | opencode session messages |
| INDEX.json 快照 | UR 注入 / strategy-registry-mcp |

| 输出 | 形态 |
|---|---|
| SKILL bundle | 主设计 §3 规范，6 文件 + manifest |

## 11.10 Dependencies
- `templates/` 5 个产出物模板
- `checklists/pre_artifact.md` 24 项
- `examples/` 标杆样例（参考用，不复制）

## 11.11 Provenance
- 本 meta-skill 自身的演进：上游 runtime 仓库 git history
- 每份产出 SKILL 的 `manifest.provenance.meta_skill_version` 锁定使用版本
```

---

## 12. 模板文件正文

### 12.1 templates/strategy_SKILL.md.j2

```jinja2
---
name: {{ strategy_id }}
description: {{ description }}
category: strategy
version: {{ version }}
---

# {{ strategy_name }}

## Purpose
{{ purpose_paragraph }}

## When to Use
- 用户已通过 conversations/ 中的对话明确同意此战法逻辑
- 当前交易日为 {{ market }} 开市日
- 必备 MCP 与凭据已就绪（见 manifest.json）

## How to Invoke

### 每日选股流程
{% for step in daily_steps %}
{{ loop.index }}. **{{ step.title }}**：{{ step.body }}
{% endfor %}

### 回测流程
直接调用 `vibe-trading.backtest(<this-skill-dir>)`，框架自动读取 `config.json` 与 `code/signal_engine.py`。
{% if has_llm_steps %}
> ⚠️ 回测路径目前**不**实时复现 LLM 步骤；如需带语义层回测，见 `prompts/{{ llm_step_with_offline }}.md` 末尾的「离线量化等价物」章节。
{% endif %}

## Inputs / Outputs
{{ inputs_outputs_table }}

## Dependencies
见 `manifest.json` 的 `required_mcps` / `required_env` / `required_db`。

## Provenance
- 形成对话：`conversations/`
- 回测验证：`backtests/`
- 历史变更：`CHANGELOG.md`
```

### 12.2 templates/manifest.json.j2

（精简骨架，省略号处填具体内容；完整 schema 见主设计 §3.3）

```jinja2
{
  "schema_version": "1.0",
  "strategy_id": "{{ strategy_id }}",
  "version": "{{ version }}",

  "provenance": {
    "created_by": "opencode-worker",
    "worker_commit": "",                          // worker 终态补
    "opencode_version": "{{ opencode_version }}",
    "ohmy_version": "{{ ohmy_version }}",
    "primary_session_id": "{{ session_id }}",
    "created_at": "",                             // worker 终态补
    "meta_skill_version": "{{ meta_skill_version }}"
  },

  "skill": {
    "skill_md_path": "SKILL.md",
    "agent_compatibility": ["claude-code", "opencode", "vibe-trading-mcp"]
  },

  "execution": {
    "entry_module": "code/signal_engine.py",
    "entry_class": "SignalEngine",
    "entry_method": "generate"
  },

  "env_lock": {
    "required_mcps": [
      {% for mcp in required_mcps %}
      {
        "name": "{{ mcp.name }}",
        {% if mcp.is_vibe_trading %}"channel": "{{ mcp.channel }}",{% endif %}
        "min_version": "{{ mcp.min_version }}",
        "source": "{{ mcp.source }}",
        "tools_used": [
          {% for tool in mcp.tools_used %}
          {
            "tool_name": "{{ tool.name }}",
            "required_input_fields":  {{ tool.input_fields | tojson }},
            "required_output_fields": {{ tool.output_fields | tojson }}
          }{{ "," if not loop.last }}
          {% endfor %}
        ]
      }{{ "," if not loop.last }}
      {% endfor %}
    ],
    "required_env": {{ required_env | tojson }},
    "required_db": {{ required_db | tojson }}
  },

  "llm_steps": { /* 见 prompts/*.md frontmatter，按需填 */ },

  "data_contract": { /* 主设计 §3.3 */ },

  "limits": {
    "production_runtime_sec": 120,
    "production_max_llm_calls": 20,
    "per_step_max_llm_calls": { /* per LLM step */ }
  },

  "degradation_policy": {
    "rules": [
      // 核心 4 条必备（M5 §8）
      {"trigger": "required_mcp_unavailable", "mcp": "any", "action": "abort_with_report",
       "report_text": "{mcp_name} 不可用，本日无法产出选股；建议人工介入"},
      {"trigger": "required_field_missing", "field": "any", "action": "skip_affected_picks",
       "report_text": "{count} 只候选因关键字段缺失被剔除"},
      {"trigger": "picks_count_below_target", "action": "push_partial",
       "report_text": "本日仅命中 {actual} 只（目标 {target}）"},
      {"trigger": "data_freshness_violation", "max_lag_minutes": 60, "action": "push_with_warning",
       "report_text": "数据延迟 {lag_minutes} 分钟于 asof_time，可能影响命中精度"}
      {% if has_llm_steps %}
      // 条件必备：llm_step_failed（manifest.llm_steps 非空时）
      ,{"trigger": "llm_step_failed", "step": "...", "action": "skip_step",
        "report_text": "..."}
      {% endif %}
      // custom.* 按需追加
    ],
    "report_format": {
      "must_include": ["degraded", "degraded_steps", "reasons", "impact"],
      "delivery": "embedded_in_picks_response",
      "field_naming": "english",
      "value_locale": "zh-CN"
    },
    "abort_with_report_behavior": {
      "still_push_empty_picks": true,
      "ui_emphasis": "high"
    }
  },

  "checksums_path": "checksums.txt"
}
```

### 12.3 templates/signal_engine.py.j2

```python
"""
{{ strategy_id }} - SignalEngine

★ 字段读取约定（meta-skill M3）：
  1. 仅读取真正用于决策的列；勿"凑数读"
  2. 列名必须是字面量字符串，禁用 df[var_name]
  3. worker 拦截器会自动回填 manifest.tools_used.required_output_fields
"""

from datetime import date

import pandas as pd


class SignalEngine:
    """与 vibe-trading SignalEngine 接口兼容（回测路径用）."""

    def __init__(self, config: dict):
        self.config = config

    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        """回测路径：返回 per-symbol 信号时间序列。

        signal: 1=long, -1=short, 0=stand aside（vibe-trading 约定）。
        """
        # TODO: Prometheus 填充
        ...

    def screen_today(
        self,
        data_map: dict[str, pd.DataFrame],
        today: date,
        sector_filter: list[str] | None = None,
        limit: int = 7,
    ) -> list:  # list[Pick]
        """生产态：当日 cross-sectional 筛选 + top-N。"""
        # TODO: Prometheus 填充
        ...
```

### 12.4 templates/prompt.md.j2

```jinja2
---
prompt_id: {{ prompt_id }}
input_schema:
{{ input_schema | to_yaml | indent(2) }}
output_schema:
{{ output_schema | to_yaml | indent(2) }}
model_default: {{ model_default | default('deepseek/deepseek-v3.2') }}
{% if invocation_mode -%}
invocation_mode: {{ invocation_mode }}
{% endif -%}
{% if fallback_template -%}
fallback_template:
  path: {{ fallback_template }}
{% endif %}
---

# {{ prompt_title }}

## Task
{{ task_description }}

## Input Format
{{ input_format_explanation }}

## Output Format (strict JSON)
```json
{{ output_format_example }}
```

## Examples
{{ examples_block }}

## Offline Quantization Equivalent
> quality: {{ quality }}    {# exact / approximate / partial #}
>
> {{ offline_equivalent_body }}
>
> 与 LLM 输出的差异：{{ differences_from_llm }}
```

### 12.5 templates/reason_template.md.j2

```jinja2
---
template_id: pick_explanation_fallback
schema_version: "1.0"
allowed_variables:
{{ allowed_variables | to_yaml | indent(2) }}
max_chars: 200
max_lines: 2
---

{{ template_body }}
```

---

## 13. 自检 Checklist 24 项

> 文件位置：`checklists/pre_artifact.md`
> 触发：阶段 ③ 终态前，Prometheus 必须逐项核对

### 13.1 文件齐备性（1~6）

- [ ] 1. `SKILL.md` 存在
- [ ] 2. `manifest.json` 存在
- [ ] 3. `code/signal_engine.py` 存在
- [ ] 4. `config.json` 存在（vibe-trading 兼容）
- [ ] 5. `prompts/*.md` 至少一个（若有 LLM step）/ 否则跳过
- [ ] 6. `reason_template.md` 存在（若 manifest.llm_steps 含 pick_explanation）

### 13.2 命名与版本（7~9）

- [ ] 7. `strategy_id` 通过正则 `^[a-z][a-z0-9-]{2,40}$`
- [ ] 8. `strategy_id` 通过 INDEX.json 撞名校验（迭代场景跳过）
- [ ] 9. `version` 符合 SemVer + bump 类型与主设计 §3.6.0 三段语义一致

### 13.3 SKILL.md 结构（10~11）

- [ ] 10. SKILL.md 含 6 必备小节：Purpose / When to Use / How to Invoke / Inputs/Outputs / Dependencies / Provenance
- [ ] 11. frontmatter 含 name / description / category=strategy / version

### 13.4 signal_engine.py 字段约定（12）

- [ ] 12. 已审阅 signal_engine.py 中所有 `df[...]` 引用，确认每列都参与决策；无 dynamic key（M3）

### 13.5 degradation_policy 完整性（13~14）

- [ ] 13. degradation_policy 含核心 4 条：`required_mcp_unavailable` / `required_field_missing` / `picks_count_below_target` / `data_freshness_violation`
- [ ] 14. 若 `manifest.llm_steps` 非空，至少一条 `llm_step_failed`

### 13.6 prompts/*.md 规范（15~16）

- [ ] 15. 每个 prompts/*.md 含 frontmatter（prompt_id / input_schema / output_schema / model_default）
- [ ] 16. 每个 prompts/*.md 含 "Offline Quantization Equivalent" 章节 + 顶部 `quality:` 声明

### 13.7 reason_template.md 规范（17~19）

- [ ] 17. reason_template.md frontmatter 含 `allowed_variables` 严格白名单
- [ ] 18. 模板正文中所有占位符在白名单内（无 typo）
- [ ] 19. 单条 ≤ 200 字符 / 1~2 行 / 变量 ≤ 6（按 §9.6）

### 13.8 manifest.env_lock 完整性（20~22）

- [ ] 20. 每个 required_mcps[] 含 channel（vibe-trading）/ min_version / source（git tag 或 pypi spec，禁 `@latest` / `@main`）
- [ ] 21. 每个 tool 含 required_input_fields 与 required_output_fields（非空数组）
- [ ] 22. required_env / required_db 完整（无遗漏）

### 13.9 provenance 完整性（23~24）

- [ ] 23. provenance 含 created_by / opencode_version / ohmy_version / primary_session_id / **meta_skill_version**（worker 补字段允许空）
- [ ] 24. meta_skill_version 非空（这是 meta-skill 自身可读字段，不能失败）

### 13.10 失败处理

- 任意一项不通过 → HITL 询问用户：
  ```
  以下 N 项未达标：
  - [item idx + brief]
  - ...
  请选择：[1] 回炉补全 [2] force-accept (标 quality: degraded) [3] 取消任务
  ```
- 连续 3 次仍未达标 → 默认 force-accept 选项可见，但需用户主动选

---

## 14. 标杆 Examples

> 文件位置：`examples/{name}/`
> 用途：参考用；Prometheus **不复制**这些目录，仅参考结构与写法

### 14.1 examples/ma250-pullback/（纯量化 + 简单 LLM）

骨架：
```
ma250-pullback/
├── SKILL.md                     # 6 小节齐备
├── manifest.json                # vibe-trading channel=internal-a-share
├── code/signal_engine.py        # generate + screen_today 双入口
├── config.json
├── prompts/
│   ├── event_sector_filter.md   # quality: approximate
│   └── pick_explanation.md      # invocation_mode: per_pick_concurrent
├── reason_template.md           # 250 日线偏离 + 量比
└── CHANGELOG.md
```

要点：
- 演示 §6.4 用户回环：从 0.0.1 → 0.0.2（板块过滤太激进 → 调阈值）的 CHANGELOG 写法
- 演示 §3.5 Offline Equivalent 的 `quality: approximate` 写法（离线信号是美股 sector ETF 涨幅 + 中文新闻词频）

### 14.2 examples/us-aerospace-event/（事件驱动型）

骨架：
```
us-aerospace-event/
├── SKILL.md
├── manifest.json                # 含 custom.event_window_expired trigger
├── code/signal_engine.py        # 事件触发 + 板块联动
├── config.json
├── prompts/
│   ├── us_event_detector.md     # quality: partial
│   ├── ashare_sector_mapping.md # quality: approximate
│   └── pick_explanation.md
├── reason_template.md           # 引用 fields.event_news + fields.sector_score
└── CHANGELOG.md
```

要点：
- 演示 `custom.event_window_expired` 在 SKILL.md 中的解释（为何核心 4 条不够）
- 演示多 LLM step 的 manifest.llm_steps 编排（`us_event_detector` → `ashare_sector_mapping` → `pick_explanation`）
- 演示 reason_template 引用 LLM 步骤的中间产物（`fields.event_news`）

> 注：MVP 提交 examples/ 目录骨架即可，内部填充可以是 placeholder TODO；Phase X1 末用真实跑通的样例覆盖。

---

## 15. 与主设计的关系

### 15.1 落地的 OQ

| OQ | 在本设计的落点 |
|---|---|
| OQ-1（meta-skill 谁写谁注入） | §3 / §11 整体（结构 + SKILL.md 内容） |
| OQ-2（Offline Equivalent 强制） | §13 第 16 项 + §12.4 模板 |
| OQ-4（strategy_id 命名 + 撞名） | §7 + §13 第 7、8 项 |
| OQ-5（SemVer） | §13 第 9 项 |
| OQ-7（env_lock validator）| §5 边界划清，validator 仍按 D17 实现 |
| OQ-8（conversations slug） | 未在本设计；由 worker SSE hook 处理 |
| OQ-9（MCP 字段自动回填） | §6 字段读取自律（Prometheus 侧） |
| OQ-13（成本预算） | 未在本设计；由上游 runtime 处理 |
| OQ-14（LLM 输出不合 schema 不二次纠错） | §9 reason_template fallback；§13 第 16 项 |
| OQ-15（degradation_report i18n） | §3.3 manifest 字段约定 |
| OQ-16（abort_with_report 仍推空） | §8.4 manifest 范例 |

### 15.2 守住的 Architecture Invariants（主设计 §11.3）

- **Worker 不感知业务**：本 meta-skill 在上游 runtime 维护，通过 opencode_profile 注入；worker 代码无 strategy 知识 ✅
- **SKILL bundle 写完即冻结**：本设计 §13 是终态前自检；终态后 worker writeback 仅补 provenance 两字段（§10），不动其他 ✅
- **强锁不可降级**：本设计 §5 明确 validator 是硬门；checklist 是软门；两者完全独立 ✅
- **生产态零 worker 介入**：meta-skill 仅在研究态使用 ✅
- **降级必须透明**：§8 / §9 与 D14/D15 一致 ✅

### 15.3 不需要新 ADR

- 守 ADR-001（worker 不感知业务）：meta-skill 在上游 ✅
- 守 ADR-006（ohmy 版本与入口）：provenance 记录但不约束实现 ✅
- 不改 worker 契约 ✅

---

## 16. Phase 7 路标

以下放到 Phase 7 评估，**不在 X1/X2/X3**：

| 项 | 触发条件 | 升级思路 |
|---|---|---|
| L3 静态扫描（议题 3） | 真实运行中频繁出现"读了未用字段"污染 manifest | 实现 `field_usage_lint.py`，扫 `df["xxx"]` 引用是否在决策链路 |
| Checklist 内嵌 validator（议题 2） | "自检过 / validator 失败"频率超阈值 | 升级到议题 2 模型 B：Prometheus 终态前调 validator |
| reason_template 多模板（议题 6） | 战法多样化 → 单一模板覆盖不全 | 按 `degraded_steps` 路由不同模板 |
| LLM 输出二次纠错（OQ-14） | 模板降级率太高影响用户体验 | n=1 retry with corrective prompt |
| meta-skill 自学循环 | meta-skill 演进到 v2.x，需要"看产出反推规则" | daily runner 结果回流 → meta-skill 学习库 |
| custom.* trigger 治理（议题 5） | custom.* 数量爆炸 | 提取高频 custom.* 为新核心 trigger |

---

## 附录 A：相关文档索引

- 主设计：[strategy-artifact-and-scheduling.md](./strategy-artifact-and-scheduling.md)（v2.10）
- 工作清单：[follow-up-recommendations.md](./follow-up-recommendations.md)（§1.2 P0 即本设计）
- ADR-001（worker 边界）：[../adr/ADR-001-opencode-adapter-mode.md](../adr/ADR-001-opencode-adapter-mode.md)
- ADR-006（ohmy 版本）：[../adr/ADR-006-ohmy-version-and-entry.md](../adr/ADR-006-ohmy-version-and-entry.md)

## 附录 B：议题讨论回溯

本设计基于 7 项议题的逐项讨论收敛：

| 议题 | 共识 |
|---|---|
| 1. Prometheus 何时启用 meta-skill | 阶段化 + LLM 自判 + 与 vibe-trading 并存（§4） |
| 2. Checklist 与 validator 职责切分 | 完全独立，通过 SKILL bundle 间接通信（§5） |
| 3. OQ-9 字段自律 | L2 文档+checklist + 禁 dynamic key（§6） |
| 4. strategy_id 撞名 | A+B 双注入 + 硬阻塞 + 显式声明迭代（§7） |
| 5. degradation triggers | 核心+条件必备 + custom.* + 自由文本+占位符（§8） |
| 6. reason_template | f-string + 白名单 + 推荐风格中性后缀 + 单模板（§9） |
| 7. provenance | 分层填写 + 仅补不改 + meta_skill_version 新增（§10） |
