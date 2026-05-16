# ADR-006 — oh-my-openagent 版本 Pin 与运行时入口

> **2026-05-16 fix 验证**：Worker 已修复 oh-my-openagent 加载链路。当前方案为：
> 1. 在容器启动前把 `OPENCODE_CONFIG_CONTENT` 写入 `~/.config/opencode/opencode.json`；
> 2. 使用 `"plugin": ["oh-my-openagent@latest"]`（单数 key，tag 而非精确 semver）；
> 3. `/global/health` 通过后继续轮询 `/agent`，等待外部插件完成初始化。
>
> arm64 验证结果：`opencode 1.15.0` + `oh-my-openagent 4.1.2` 下，日志显示 `service=plugin path=oh-my-openagent@latest loading plugin`，`/agent` 在健康检查后第 12 秒返回包含 `Prometheus` / `Sisyphus` 的 agent 列表。

| 字段 | 值 |
|---|---|
| **状态** | Accepted（当前运行时已生效） |
| **日期** | 2026-05-13 |
| **关联 Spike** | Phase 0 Spike 1b（版本确认，已完成）；Spike 3（容器内验证，2026-05-16 完成） |
| **关联 HITL** | H11（oh-my 版本 pin，本 ADR 收口） |

---

## 背景

opencode 通过"plugin"机制加载 oh-my-openagent，使得 Prometheus/Sisyphus agent 可在 HTTP Server 模式下通过 `prompt_async` 的 `agent` 参数路由。Worker 需要决定：

1. 运行时调用入口：`oh-my-opencode run` CLI vs HTTP API `agent` 参数。
2. oh-my-openagent 插件版本 pin 策略。

### 包名澄清

| npm 包名 | 用途 | 当前 pin | npm latest（2026-05-16） |
|---|---|---|---|
| `oh-my-openagent` | opencode **插件**，通过 opencode 插件配置加载 | `4.1.2`（install-based cache，含运行时依赖） | `4.1.2` |
| `oh-my-opencode` | **CLI 工具**，用于 smoke test 和本机调试 | `4.1.2` | `4.1.2` |

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

**Dockerfile 中 pin `oh-my-openagent` 到 `4.1.2`；运行时配置使用 `oh-my-openagent@latest` 插件入口。**

```dockerfile
# opencode 插件配置（通过 OPENCODE_CONFIG_CONTENT 写入 opencode.json）
# "plugin": ["oh-my-openagent@latest"]
```

opencode 配置文件中的插件版本：

```json
{
  "plugin": ["oh-my-openagent@latest"]
}
```

理由：
- `4.1.2` 是当前 npm latest，且已与 `opencode 1.15.0` 在 arm64 验证镜像中实测通过 `/agent` 加载检查。
- `oh-my-openagent 4.1.2` 的发布包会在运行时引用 `zod` 等外部依赖，因此 cache 必须按 install-based 方式预置完整依赖树，而不能只拷贝包目录。
- 配置入口使用 `@latest`，而**镜像制品**通过离线 cache pin 到 `4.1.2`，既兼容上游安装器生成的 plugin entry，也避免把精确 semver 写死到运行时 JSON 中。

### 升级路径

1. 先更新离线制品：`opencode-linux-*` tgz 和 install-based 的 `oh-my-openagent-cache.tar.gz`。
2. 重建镜像后验证两条日志：`service=plugin path=oh-my-openagent@latest loading plugin` 与 `verified oh-my-openagent agents loaded: Prometheus, Sisyphus`。
3. 若新版本导致 `/agent` 就绪时间变化，优先调整 entrypoint 的轮询窗口，不要退回到内置 `plan` / `build` agent。

---

## 影响

- Dockerfile / dist 离线制品已升级到 `opencode 1.15.0` + `oh-my-openagent 4.1.2`。
- Worker entrypoint 会先写 `opencode.json`，再等待 `/agent` 就绪；不能再把 `OPENCODE_CONFIG_CONTENT` 仅视为环境变量透传。
- `OPENCODE_CONFIG_CONTENT` 中的插件数组 key 为 `plugin`（单数），值使用 `oh-my-openagent@latest`。
- Worker adapter 的 agent routing 参数值：`"Prometheus"`（plan_first）、`"Sisyphus"`（ultrawork）——在 Spike 3 容器 smoke test 中随插件升级一并验证。
- H11 正式收口：当前 pin 为 `4.1.2`，并已完成容器级验证。
