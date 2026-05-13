# ADR-003 — 凭据模型

| 字段 | 值 |
|---|---|
| **状态** | Accepted (已升级，2026-05-13 Spike 1b) |
| **日期** | 2026-05-13 |
| **关联 Spike** | Phase 0 Spike 1b（env 注入 + config 验证，已完成）；Spike 4（容器内完整流程验证，待完成） |
| **关联 HITL** | H8（Broker 凭据代理，决策：MVP 不做） |

---

## 背景

Worker 需要把 LLM API key 安全地传入容器，让 opencode 可以调用 LLM。候选方案：
1. 宿主 `~/.config/opencode/` 挂载：与宿主登录态耦合，多租户/隔离下不可用。
2. 容器 env 注入 + 启动时生成配置文件：隔离性好，但需在容器内写文件。
3. **纯 env 注入（Spike 1b 新发现）**：利用 `OPENCODE_CONFIG_CONTENT` + `{env:X}` 变量替换，不需要写任何文件。
4. Broker 凭据代理：容器内 opencode 把所有 LLM 请求发往 Broker，Broker 注入 API key——即零密钥方案，复杂度高。

---

## 决策

**MVP 采用方案 3（升级自原方案 2）：纯 env 注入，无文件写入。**

Spike 1b 验证确认：`OPENCODE_CONFIG_CONTENT` 支持 `{env:VARIABLE_NAME}` 变量替换，替换在 opencode 内部 config 解析管道中执行，结果已通过 `GET /config` 验证。

### 流程

```
TaskRequest
  └── provider_credentials: { "alibaba": "<api_key>", ... }
        │
        ▼
Worker → docker run \
  -e WORKER_ALIBABA_API_KEY=<key> \
  -e OPENCODE_DISABLE_AUTOUPDATE=1 \
  -e OPENCODE_SERVER_PASSWORD=<random> \
  -e OPENCODE_CONFIG_CONTENT='{ ... }' \
  -e OPENCODE_PERMISSION='{ ... }' \
  ...
        │
        ▼
容器入口脚本（entrypoint.sh）
  ├── 动态生成 OPENCODE_CONFIG_CONTENT（引用 {env:WORKER_*_API_KEY}）
  ├── 动态生成 OPENCODE_PERMISSION（来自 TaskRequest.broker_policy）
  └── exec opencode serve --hostname 127.0.0.1 --port 4096
```

### OPENCODE_CONFIG_CONTENT 结构模板

```json
{
  "autoupdate": false,
  "model": "{env:WORKER_DEFAULT_MODEL}",
  "provider": {
    "alibaba": {
      "options": {
        "apiKey": "{env:WORKER_ALIBABA_API_KEY}"
      }
    },
    "anthropic": {
      "options": {
        "apiKey": "{env:WORKER_ANTHROPIC_API_KEY}"
      }
    }
  },
  "permission": {
    "bash": "ask",
    "write": "ask",
    "edit": "ask"
  }
}
```

**注**：`permission` 既可嵌入 `OPENCODE_CONFIG_CONTENT`，也可通过独立的 `OPENCODE_PERMISSION` env 注入；后者优先级可能更高，实际 Spike 4 验证以 `GET /config` 返回值为准。

### 安全原则

- API key 仅通过 env 传入，**不写入镜像层**，不记录在 Worker 结构化日志（只记录 key 的 hash 或存在标志用于 audit）。
- `OPENCODE_CONFIG_CONTENT` 中的 key 值以 `{env:X}` 形式引用，实际 key 值通过独立 env 变量传递，不出现在 config JSON 字符串明文中（防止 docker inspect 泄漏）。
- 容器无需写 `~/.config/opencode/` 或 `auth.json`，减少文件系统攻击面。
- 入口脚本生成 `OPENCODE_CONFIG_CONTENT` 后可 `unset` 原始凭据 env 变量（在 exec opencode serve 之前），降低子进程泄漏风险。

### Provider 支持范围（MVP）

- Alibaba（通义系列）/ Alibaba China（DeepSeek 系列）：已有本机验证凭据。
- GitHub Copilot：本机已认证，容器内走 env 注入可复现。
- 其他 provider：同一机制，增加新 env 变量即可，入口脚本按 provider ID 映射。

### 不做项

- **Broker 凭据代理（零密钥）**：容器内 opencode 的所有 LLM 请求经 Broker 转发并注入 key，API key 不进容器——延到 Phase 7。
- 订阅态凭据（Claude Code / Codex / Gemini CLI OAuth token）：MVP 仅支持 API key。

---

## 影响

- 入口脚本（Phase 3 交付）简化为：生成 `OPENCODE_CONFIG_CONTENT` JSON 字符串 + export 若干 env → `exec opencode serve`；**无文件系统写入**。
- Spike 4 需验证：`OPENCODE_CONFIG_CONTENT` + `{env:X}` 引用路径在 x86_64/Linux 容器内行为一致（macOS 本机已验证）。
- TaskRequest schema 中 `provider_credentials` 字段需标记 `sensitive: true`，禁止在事件流和 audit log 中明文输出。
