#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# 容器入口脚本：读取 env 变量 → 生成 opencode 配置 → 启动 opencode serve
#
# 必须的 env 变量（由 Worker 通过 docker run --env 注入）：
#   OPENCODE_SERVER_PASSWORD   — opencode HTTP API 的 Basic Auth 密码
#   OPENCODE_CONFIG_CONTENT    — 完整 opencode 配置 JSON（含 model/providers）
#   OPENCODE_DISABLE_AUTOUPDATE=1
#   OPENCODE_PERMISSION        — 权限策略 JSON
#   WORKER_TASK_ID             — 当前任务 UUID（用于日志/审计）
#
# 可选 env 变量：
#   OPENCODE_SERVER_PORT       — opencode serve 端口（默认 4096）
#   ANTHROPIC_API_KEY          — Anthropic provider API key
#   OPENAI_API_KEY             — OpenAI provider API key
#   （其他 provider keys 由 OPENCODE_CONFIG_CONTENT 中的 {env:XXX} 解析）
#
# 安全注意事项：
#   - 此脚本不向日志输出任何 secret 值（OPENCODE_SERVER_PASSWORD 等）
#   - OPENCODE_CONFIG_CONTENT 通过 env 传入，不写入持久磁盘（tmpfs /tmp）
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

PORT="${OPENCODE_SERVER_PORT:-4096}"
TASK_ID="${WORKER_TASK_ID:-unknown}"

echo "[entrypoint] task_id=${TASK_ID} port=${PORT}"

# 验证必要 env 变量
if [ -z "${OPENCODE_SERVER_PASSWORD:-}" ]; then
    echo "[entrypoint] ERROR: OPENCODE_SERVER_PASSWORD is not set" >&2
    exit 1
fi

if [ -z "${OPENCODE_CONFIG_CONTENT:-}" ]; then
    echo "[entrypoint] ERROR: OPENCODE_CONFIG_CONTENT is not set" >&2
    exit 1
fi

# 配置目录（在 tmpfs /tmp 内，读写 FS 受限场景下安全）
CONFIG_DIR="${HOME}/.config/opencode"
mkdir -p "${CONFIG_DIR}"

echo "[entrypoint] starting opencode serve on port ${PORT}"

# 启动 opencode serve
# - OPENCODE_CONFIG_CONTENT 和 OPENCODE_PERMISSION 已在 env 中，opencode 会自动读取
# - --hostname 0.0.0.0 使 opencode 监听所有接口，Docker 端口映射（DNAT）才能正常转发
#   注：Worker 通过 docker -p 将宿主端口映射到容器 4096，从容器外访问 127.0.0.1:<host_port>
exec opencode serve \
    --hostname "0.0.0.0" \
    --port "${PORT}" \
    --print-logs
