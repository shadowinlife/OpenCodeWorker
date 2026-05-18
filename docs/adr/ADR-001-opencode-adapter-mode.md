# ADR-001 — OpenCode 接入路径选择

| 字段 | 值 |
|---|---|
| **状态** | Accepted（当前运行时已恢复 `Prometheus` / `Sisyphus` 路由；2026-05-14 的 `plan` / `build` fallback 仅保留为历史记录） |
| **日期** | 2026-05-13 |
| **更新日期** | 2026-05-16（修复插件加载链路，arm64 容器内验证恢复 Prometheus / Sisyphus） |
| **关联 Spike** | Phase 0 Spike 1a（endpoint spike）、oh-my smoke tests |
| **关联 HITL** | H12（plan_first 主入口，部分收口） |

---

## 当前实现状态（2026-05-16 fix 验证）

- Worker 现在会在容器启动前把 `OPENCODE_CONFIG_CONTENT` materialize 到 `~/.config/opencode/opencode.json`，不再只依赖环境变量传递。
- `docker/worker/entrypoint.sh` 在 `/global/health` 成功后会继续轮询 `/agent` 最多 30 秒，避免 oh-my-openagent 延迟初始化导致误判失败。
- 2026-05-16 arm64 镜像验证：`opencode 1.15.0` + `oh-my-openagent 4.1.2` 下，日志显示 `service=plugin path=oh-my-openagent@latest loading plugin`，`/agent` 在健康检查后第 12 秒返回包含 `Prometheus` / `Sisyphus` 的 agent 列表。
- 因此本 ADR 的原始决策重新成为**当前事实**：Worker 运行时走 HTTP Server + `agent: "Prometheus" / "Sisyphus"`，不再以 `plan` / `build` 作为 MVP fallback。

## 历史实现状态（2026-05-14 review 校准）

> 详见 [code-review-2026-05-14.md](../archive/code-review-2026-05-14.md) P0-5。

- **容器内 `GET /agent` 实测返回**：`build / compaction / explore / general / plan / summary / title` —— **全部为 opencode 1.14.30 内置 agent**，没有 oh-my-openagent 暴露的 `Prometheus` / `Sisyphus`。
- **opencode 启动日志中加载的 plugin** 都是内置混淆名（`EJ / bJ / Px / dI / kx / gx`），未见 `oh-my-openagent`。
- 镜像里 oh-my-openagent 3.17.2 包文件确实存在于 `/home/sandbox/.cache/opencode/packages/oh-my-openagent@latest/node_modules/oh-my-openagent/`，但 opencode 没有自动激活它（缺少显式 plugin 声明或加载路径不对）。
- **历史临时决策（2026-05-14）**：Worker driver 曾短暂使用 opencode 内置 `plan` / `build` agent，分别承担 plan_first / direct_execute 角色。该 fallback 已在 2026-05-16 随插件加载链路修复而废止。

> ⚠️ 本节保留为 2026-05-14 排查记录。最终落地方案不是全局安装，也不是 doctor 预热，而是：
> 1. 在容器里显式写入 `~/.config/opencode/opencode.json`；
> 2. 使用 `"plugin": ["oh-my-openagent@latest"]`（单数 key）；
> 3. 将 `/agent` 校验改为带重试的延迟就绪检查。

---

## 背景

Worker 需要驱动容器内的 opencode 执行任务（计划生成 + 执行 + HITL），并把进度/事件实时回传上游 agent runtime。需要在以下三条路径中选一：

1. **HTTP Server**：`opencode serve` 暴露 HTTP + SSE，Worker 以纯 HTTP 客户端接入。
2. **CLI + tmux/PTY**：通过 PTY 捕获 TUI 输出并注入输入，拦截工具权限交互。
3. **oh-my-opencode run 黑盒**：用 `oh-my-opencode run --agent ... --json` 驱动全程，只在 run 退出后读取 `--json` 输出。

典型场景（用于驱动权衡）：用户请求"评估某只股票基本面" → Prometheus 生成分析计划 → Sisyphus 执行 ultrawork（webfetch 财务数据 + bash 处理 + 写报告），执行时间 1–10 分钟，中间可能触发多次工具权限 ask，方向跑偏时需要实时 abort。

---

## 方案对比

### 方案 A — HTTP Server（选定）

Worker 直接在容器内启动 `opencode serve`，oh-my-openagent 已作为 opencode 插件安装在镜像中，在 `POST /session/:id/prompt_async` 的请求体里带 `"agent": "Prometheus"` 或 `"agent": "Sisyphus"` 即可路由。

```
容器内:
  opencode serve --port 9000 --hostname 127.0.0.1
   └── oh-my-openagent 插件已加载（镜像层）
   └── Prometheus / Sisyphus agent 可用

Worker:
  GET  /global/event        ← SSE 订阅实时事件
  POST /session/:id/prompt_async  body: {agent:"Prometheus", parts:[...]}
  POST /session/:id/permissions/:permID  body: {response:"once|always|reject"}
  POST /session/:id/abort
  GET  /session/:id/diff
```

**优势**：
- 实时 SSE 事件：上游可流式看到 AI 思考过程（`message.part.delta`）。
- 实时 HITL：`per_*` permission 事件到达后 Worker 暂停并转发给上游，上游决策后调用 permissions endpoint。
- 干净 abort：`POST /abort` + 容器 stop 双层中止，abort 事件有明确 SSE 语义。
- Worker 完全控制 server lifecycle（单一所有者）。
- Phase 3 与 Phase 4 合并为同一套 HTTP adapter：入口脚本决定是否传 `agent` 参数，核心 HTTP 逻辑共用。

**劣势**：
- 需要验证 "仅凭 `agent` 参数" 是否足以完整激活 Prometheus/Sisyphus 的 oh-my 特定行为（Spike 1b 验证点）。

**实测证据**（Phase 0 Spike 1a）：
- `opencode serve` 启动后 oh-my-openagent 插件已加载（见 §1.3 oh-my 基线）。
- `prompt_async` 未显式传 `agent` 时默认走 Sisyphus，说明 `agent` 路由在 HTTP 层生效。
- message / prompt_async / diff / abort / permissions 全部实测通过。

### 方案 B — CLI + tmux/PTY（关闭）

用 PTY 驱动 opencode TUI，捕获文本输出并解析权限交互。

**放弃原因**：
- opencode TUI 输出依赖终端渲染，解析脆弱；版本升级即破坏。
- 无结构化 SSE，无法做可靠事件游标和断线重连。
- Permission 交互需要匹配 TUI 文本模式，维护成本高。
- HTTP Server 路径已充分验证，此备路径无必要。

**处置**：Phase 0 Spike 2 明确取消，不进行 POC。ADR-001 正式关闭此选项。

### 方案 C — oh-my-opencode run 黑盒（关闭）

用 `oh-my-opencode run --agent ... --json` 包裹整个 session，只读 run 退出后的 `--json` 结果。

**放弃原因**：
- run 结束后才有输出，无实时事件流，不满足流式进度需求。
- 执行中无法触发 HITL：工具权限 ask 在 oh-my 内部被处理，上游没有介入窗口。
- abort 只能 kill 进程，无语义。
- oh-my-openagent 本身是 opencode 插件；直接走 HTTP Server + agent 参数可复现同等行为，无需额外 run 命令包装。

**注意**：`oh-my-opencode run` 仍用于**本机 smoke test**（Phase 0 验证工具），不作为 Worker 运行时调用路径。

---

## 决策

**Worker 采用 HTTP Server 路径（方案 A）作为唯一 MVP 路径。**

具体实现：
1. 容器镜像预装 oh-my-openagent 插件（pin 版本，待 H11 确认）。
2. 容器入口脚本（Phase 3）启动 `opencode serve --port <PORT> --hostname 127.0.0.1`，Worker 通过容器网络访问。
3. `plan_first` 模式：`prompt_async` + `agent: "Prometheus"`。
4. `direct_execute` 模式：`prompt_async` + `agent: "Sisyphus"`（或默认 agent）。
5. Phase 3 与 Phase 4 合并：单一 `adapters/opencode/` 模块，入口模式通过配置切换，HTTP 连接逻辑共用。
6. CLI+tmux 和 oh-my run 黑盒路径均正式关闭，不进入 Phase 7 计划。

---

## 待验证项（Spike 1b）

| 验证点 | 目标 | 风险等级 |
|---|---|---|
| `agent: "Prometheus"` via `prompt_async` 完整激活 oh-my Plan Builder 行为 | 确认 agent 路由与 oh-my prompt injection 等价 | 中（若不等价需重新评估 oh-my run 旁路方案） |
| `OPENCODE_CONFIG_DIR` 隔离容器内 config | 确认环境变量名，避免污染宿主 `~/.config/opencode` | 低 |
| disable auto-update 实际 env 名 | 写入 ADR-002 / Dockerfile | 低 |
| 真实 `per_*` permission 事件 payload | 确认 permission 事件字段，用于 DecisionRequest mapper | 中 |

若 Prometheus 激活验证失败（`agent` 参数无法触发 oh-my Plan Builder），回退策略：在容器内以 `oh-my-opencode run --agent Prometheus` 启动，Worker 立即旁路接入其暴露的 HTTP server（方案 A2）。

---

## 影响

- **Phase 3/4 合并**：roadmap 将 Phase 4 条目合并进 Phase 3，统一为 "OpenCode HTTP Server Adapter（含 oh-my agent routing）"。
- **Spike 2 取消**：Phase 0 Spike 2 从计划中移除。
- **H12 部分收口**：`plan_first` 主入口确认为 HTTP Server + `agent: "Prometheus"`；`/start-work` slash command spike 降级为可选验证，不阻塞 MVP 实现。
- **H11 已收口**：当前版本 pin 为 `oh-my-openagent 4.1.2` / `oh-my-opencode 4.1.2`，并已完成 arm64 容器验证。
