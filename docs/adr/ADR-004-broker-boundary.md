# ADR-004 — Host Broker 责任边界与 MVP 形态

| 字段 | 值 |
|---|---|
| **状态** | Accepted |
| **日期** | 2026-05-13 |
| **关联 Spike** | Phase 0 Spike 5（Broker 最小原型，待完成） |
| **关联 HITL** | H1（Broker 与 MCP 关系），H1b（白名单粒度），H8（凭据代理，决策：不做） |

---

## 背景

容器内 opencode 需要访问公网（LLM API、webfetch 工具、MCP 公网依赖），但为了安全隔离，不能给容器默认外网路由。需要一个 Broker 组件控制出站流量，并决定它的责任边界。

---

## 决策

### Broker 职责（MVP）

| 职责 | MVP | Phase 7+ |
|---|---|---|
| HTTP egress 代理（容器唯一出网通道） | ✅ | ✅ |
| 域名级白名单（按 task 动态下发） | ✅ | ✅ |
| 审计 hook（所有出站请求 + task_id） | ✅ | ✅ |
| MCP 生命周期管理 | ❌ | 待定 |
| LLM 凭据代理（零密钥） | ❌ | ✅ |
| HTTP/SSE MCP 路由 | ❌ | ✅ |

### 白名单模型

- 粒度：**域名级**（`api.alibaba.com`、`api.deepseek.com` 等），不做 URL 级精细控制。
- 下发方式：`TaskRequest.broker_policy.allow_egress_hosts[]`，Worker 在容器启动前调用 `POST /broker/tasks/:id/policy`。
- 默认值：**空列表**，必须显式放行。
- TTL：与任务生命周期绑定，任务终态后白名单自动失效。

### MCP 不归 Broker 管

所有 MVP MCP 均为 **stdio 本地服务，打包进镜像**，容器内 opencode 直接 spawn，无需 Broker 参与 MCP lifecycle。若某 MCP 需要公网连接（如 context7、grep_app），由 Broker 在白名单中放行对应域名即可。

### 凭据代理不做

MVP 中容器内 opencode 通过 env 持有 LLM API key（见 ADR-003），Broker 不做凭据注入/代理。零密钥方案移 Phase 7。

### 网络拓扑

```
[容器]  ──HTTP_PROXY=http://broker:8080──►  [Host Broker]  ──►  公网（白名单域名）
  │
  └── 容器无 default route，仅 broker 地址可达
```

Broker 以宿主进程或 sidecar 容器运行，监听 Worker 内网地址。

### Broker API（Worker 内部调用）

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/broker/tasks/:id/policy` | 下发白名单与 TTL |
| `DELETE` | `/broker/tasks/:id/policy` | 任务终态时清理（可选，TTL 也会自动过期） |
| `GET` | `/broker/tasks/:id/audit` | 查询该任务的出站请求记录 |

---

## 影响

- Phase 2 需实现 Broker MVP，Spike 5 验证容器内 opencode 仅有 Broker 时仍可正常调用 LLM。
- Worker 在每次 `docker run` 之前调用 `POST /broker/tasks/:id/policy`，任务终态后不需要显式删除（TTL 兜底），但 Worker 可选择主动清理。
- 安全回归测试（Phase 2）必须包含：容器直接 `curl https://example.com`（无放行）应被拦截。
