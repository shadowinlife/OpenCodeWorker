# ADR-002 — 容器镜像基线

| 字段 | 值 |
|---|---|
| **状态** | Accepted |
| **日期** | 2026-05-13 |
| **关联 Spike** | Phase 0 Spike 3（镜像构建验证，待完成） |
| **关联 HITL** | H2（镜像分发与签名），H11（oh-my 版本 pin，待 Spike 3 确认） |

---

## 背景

Worker 需要一个可重现、版本可 pin 的容器镜像，内含 opencode + oh-my-openagent 插件 + 所需 stdio MCP 二进制。需要决定：基础镜像选型、版本管理策略、分发渠道和签名策略。

---

## 决策

### 基础镜像

使用 **`ubuntu:24.04`**（Noble），手动安装依赖。

- **镜像来源约束**：当前开发/CI 环境无法直接从 DockerHub 拉取镜像；所有基础镜像须人工预先下载到本机并 `docker load`（HITL 操作）。`ubuntu:24.04` 已本地确认可用（`docker images ubuntu:24.04` = `e0f16e6366fe`，2026-05-13 验证）。
- 若需更换其他 DockerHub 基础镜像（如 debian-slim、node 等），必须触发 🟠 HITL：由人工从外部下载并 `docker load`，不可在 CI 自动拉取。
- 选择 ubuntu:24.04 而非 debian-slim 的原因：本地已有且无法自动拉取其他镜像；Ubuntu LTS 系列 apt 工具链完整，apt pin 版本同样直接；long-term 支持周期充足。
- 安装顺序：apt 基础工具（curl/ca-certificates 等）→ Bun（opencode 运行时）→ opencode 二进制 → oh-my-opencode npm 包 → stdio MCP 二进制。

### 版本 Pin 策略

| 组件 | Pin 方式 | 当前已知版本 |
|---|---|---|
| opencode | 下载固定版本二进制 URL | `1.14.30` |
| oh-my-opencode CLI | npm `@<version>` exact pin | `3.17.5`（待 H11 收口后确认） |
| oh-my-openagent plugin | opencode 插件配置 exact version | 与 oh-my CLI 版本对应 |
| Bun | 固定版本安装脚本或二进制 | 待 Spike 3 确认 |
| stdio MCP 二进制 | 构建时 pin；按需列入 Dockerfile ARG | 待 Phase 2 按业务需求确定 |

版本变更流程（升级 playbook）：
1. Spike：在临时镜像里验证新版本组合可通过 `opencode --version` + `oh-my-opencode doctor`。
2. ADR 追加记录：bump 版本号及验证结果。
3. 回归：重跑 Phase 6 测试矩阵后发布新 tag。

### 用户模型

镜像内创建非 root 用户（如 `worker`），opencode serve 以该用户运行，容器启动参数 `--security-opt no-new-privileges`。

### 分发渠道

- 本地构建 + GHCR 私有 tag（`ghcr.io/<org>/opencode-worker:<semver>`）。
- 不引入公共 registry。
- 镜像 tag 策略：`<opencode版本>-<oh-my版本>-<worker版本>`，明确三方关系。

### 签名

**不签名**（cosign 移 Phase 7）。MVP 阶段依赖 GHCR 私有可见性作为访问控制。

### Auto-update 禁用

opencode 和 oh-my-opencode 均有自动更新机制，**必须双层禁用**（Spike 1b 已验证，H10 收口）：

- **env 层**：`OPENCODE_DISABLE_AUTOUPDATE=1`（boolean env）——启动前设置，禁用运行时更新检查。
- **配置层**：在 `OPENCODE_CONFIG_CONTENT` JSON 里设置 `"autoupdate": false`——与 env 层形成双保险。

两者均已在 Spike 1b 中通过 `GET /config` 验证生效。

---

## 影响

- Dockerfile 模板作为 Phase 2 交付物之一，需包含上述所有 pin ARG。
- H11（oh-my 版本选择）在 Spike 3 执行前不影响本 ADR 主体，仅影响版本号填入。
- Phase 2 退出检查要求镜像构建产物推送到 GHCR 并在 ADR 里记录 pin 版本。
