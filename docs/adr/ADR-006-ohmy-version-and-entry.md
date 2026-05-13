# ADR-006 — oh-my-openagent 版本 Pin 与运行时入口

| 字段 | 值 |
|---|---|
| **状态** | Accepted |
| **日期** | 2026-05-13 |
| **关联 Spike** | Phase 0 Spike 1b（版本确认，已完成）；Spike 3（容器内验证，待完成） |
| **关联 HITL** | H11（oh-my 版本 pin，本 ADR 收口） |

---

## 背景

opencode 通过"plugin"机制加载 oh-my-openagent，使得 Prometheus/Sisyphus agent 可在 HTTP Server 模式下通过 `prompt_async` 的 `agent` 参数路由。Worker 需要决定：

1. 运行时调用入口：`oh-my-opencode run` CLI vs HTTP API `agent` 参数。
2. oh-my-openagent 插件版本 pin 策略。

### 包名澄清

| npm 包名 | 用途 | 本机缓存版本 | npm latest（2026-05-13） |
|---|---|---|---|
| `oh-my-openagent` | opencode **插件**，通过 opencode 插件配置加载 | `3.17.2`（~/.cache/opencode/packages/ ） | `4.1.1` |
| `oh-my-opencode` | **CLI 工具**，用于 smoke test 和本机调试 | `3.17.5`（~/.npm/_npx/ ） | `4.1.1` |

两者是不同 npm 包但随 oh-my 项目同步发版。Worker 运行时加载的是 `oh-my-openagent`（插件），不是 `oh-my-opencode`（CLI）。

---

## 决策

### 运行时入口

**HTTP API + `agent` 参数（不使用 `oh-my-opencode run`）。**

- `POST /session/:id/prompt_async` 请求体中传 `"agent": "Prometheus"`（plan_first）或 `"agent": "Sisyphus"`（direct execute）。
- opencode server 内部路由到对应 agent；oh-my-openagent 插件负责扩展行为。
- `oh-my-opencode run` 保留为**本机 smoke test 工具**，不作为 Worker 运行时调用路径。

理由（见 ADR-001）：HTTP API 路径已在 Spike 1a 完整验证；`oh-my-opencode run` 为黑盒子进程，缺乏 SSE 事件流和细粒度控制，不适合 Worker 架构。

### 版本 Pin 策略

**Dockerfile 中 pin `oh-my-openagent` 到 `3.17.2`（当前本机已验证版本）。**

```dockerfile
# opencode 插件配置（通过 OPENCODE_CONFIG_CONTENT 或 opencode.json）
# "plugins": ["oh-my-openagent@3.17.2"]
```

opencode 配置文件中的插件版本：

```json
{
  "plugins": ["oh-my-openagent@3.17.2"]
}
```

理由：
- `3.17.2` 是本机 opencode `1.14.30` 实测可用版本（Spike 1b server toast 显示 `OhMyOpenCode 3.17.2`）。
- npm latest `4.1.1` 尚未与 opencode `1.14.30` 组合验证；升级需 Spike 3 容器构建 + smoke test 重跑。
- 使用 `@latest` 会导致构建时版本漂移，无法复现。

### 升级路径

1. Spike 3（容器镜像构建）时同步验证 `oh-my-openagent@4.1.1` + opencode `1.14.30` 组合。
2. 通过 `oh-my-opencode doctor --status` 确认插件版本加载正常，smoke test（Prometheus + Sisyphus）通过。
3. 验证通过后在 ADR-002 和本 ADR 追加记录，bump Dockerfile 版本到 `4.1.1`。

---

## 影响

- Dockerfile 插件安装命令从 `oh-my-openagent@latest` 改为 `oh-my-openagent@3.17.2`（Phase 2 交付物）。
- `OPENCODE_CONFIG_CONTENT` 中的 `plugins` 数组需使用精确版本号而非 `@latest`。
- Worker adapter 的 agent routing 参数值：`"Prometheus"`（plan_first）、`"Sisyphus"`（ultrawork）——在 Spike 3 容器 smoke test 中随插件升级一并验证。
- H11 正式收口：`3.17.2` 为当前 pin，`4.1.1` 升级路径已明确。
