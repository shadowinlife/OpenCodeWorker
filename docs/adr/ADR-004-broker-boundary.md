# ADR-004 — Host Broker 责任边界与 MVP 形态

| 字段 | 值 |
|---|---|
| **状态** | Accepted（决策本身有效；**broker 完整交付推迟到 Phase 7**，见下方"MVP 阶段决策"） |
| **日期** | 2026-05-13 |
| **更新日期** | 2026-05-14（撤回 MVP 默认 broker，明确 Phase 7 交付） |
| **关联 Spike** | Phase 0 Spike 5（Broker 最小原型，部分完成 — 见实现状态） |
| **关联 HITL** | H1（Broker 与 MCP 关系），H1b（白名单粒度），H8（凭据代理，决策：不做） |

---

## MVP 阶段决策（2026-05-14 修订）

> 经 [code-review-2026-05-14.md](../code-review-2026-05-14.md) 评审确认：
>
> 1. **MVP 阶段不再把 broker 视作默认安全模型**。`WORKER_BROKER_ENABLED` 默认值已改为 `False`，调用方启用前需自行确认 broker 进程已可用。
> 2. **容器网络当前为 `internal=False`**：容器可直连任意外网。原"默认无外网，仅可访问 Host Broker"的对外承诺**撤回**到 Phase 7（与 broker CONNECT 隧道、broker 进程 lifespan 启停一同恢复）。
> 3. **MVP 阶段的容器隔离仅依赖**：非 root（uid=1000）+ read-only root FS + cap-drop + no-new-privileges + seccomp + workspace 路径校验。这些手段不能替代网络隔离；调用方在合规/审计等场景下需自行通过 host firewall / VPC ACL / namespace 兜底。
> 4. 未来恢复 `internal=True` 默认时（Phase 7），E2E 通过显式开关 `WORKER_SANDBOX_NETWORK_INTERNAL=false` 短路。

---

## 实现状态（2026-05-14 review 输出）

> 详见 [code-review-2026-05-14.md](../code-review-2026-05-14.md) P0-1/P0-2/P0-3。

| 子能力 | 决策（MVP / Phase 7） | 当前实现 | 备注 |
|---|---|---|---|
| 域名级白名单（按 task 动态下发） | ✅ MVP | ✅ | `src/broker/policy.py` 完整；broker 未联通前不实际生效 |
| HTTP forward proxy（普通 GET/POST） | ✅ MVP | ✅ | `_handle_http_forward` 可用；broker 未联通前未生效 |
| HTTPS CONNECT 隧道 | ⏸️ **延迟到 Phase 7** | ❌ | `_handle_proxy_connect` 是占位代码；Starlette 路由表未注册 CONNECT 方法 |
| Broker 进程在 lifespan 启动 | ⏸️ **延迟到 Phase 7** | ❌ | `main.py` lifespan 未启动 broker；`HTTP_PROXY=http://broker:8090` 无人监听 |
| 容器网络 `internal=True`（broker 是唯一出口） | ⏸️ **延迟到 Phase 7** | ❌ | `ensure_worker_network` 强制 `internal=False`，容器可直连外网 |

**当前可用范围**：仅 `WORKER_BROKER_ENABLED=false`（local / E2E 模式）路径已端到端验证；`broker_enabled=True` 路径**不要在生产环境打开**——即便打开，HTTP_PROXY 也无人监听。

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
