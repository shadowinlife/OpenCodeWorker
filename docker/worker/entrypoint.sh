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
CONFIG_PATH="${CONFIG_DIR}/opencode.json"

# 将 Worker 注入的内联 JSON materialize 为 opencode.json。
# 仅传环境变量时，opencode 不会稳定地把 plugin/provider 配置应用到运行时。
python3 - "${CONFIG_PATH}" <<'PY'
import json
import os
import sys

config_path = sys.argv[1]
config_content = os.environ["OPENCODE_CONFIG_CONTENT"]

try:
    parsed = json.loads(config_content)
except json.JSONDecodeError as exc:
    print(f"[entrypoint] ERROR: invalid OPENCODE_CONFIG_CONTENT: {exc}", file=sys.stderr)
    raise SystemExit(1)

with open(config_path, "w", encoding="utf-8") as handle:
    json.dump(parsed, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY

echo "[entrypoint] wrote opencode config to ${CONFIG_PATH}"

echo "[entrypoint] starting opencode serve on port ${PORT}"

# ── 启动 opencode serve（后台），随后验证 oh-my-openagent 已加载 ──────────────
# 设计：以前是 `exec opencode serve ...`，无法在启动后做任何检查。改为
# 后台启动 + Python 健康检查 + GET /agent 校验 + wait，来执行 ADR-001/006
# 要求的 Prometheus/Sisyphus 必备校验。
#   - 校验失败 → kill opencode + 容器以非零退出码失败（NOT 静默回退到 plan/build）
#   - 校验成功 → wait $OPENCODE_PID 阻塞，把 opencode 退出码作为容器退出码
#
# Worker 通过 docker -p 将宿主端口映射到容器 4096，从容器外访问 127.0.0.1:<host_port>
opencode serve \
    --hostname "0.0.0.0" \
    --port "${PORT}" \
    --print-logs &
OPENCODE_PID=$!

# 转发终止信号给 opencode，避免容器停止时遗留 zombie
trap 'kill -TERM "${OPENCODE_PID}" 2>/dev/null || true' TERM INT

# 健康检查 + agent 列表校验（python3 已在镜像中，无需额外 curl）
# 临时关闭 errexit，让 python 非零退出码能被 $? 捕获而不是触发 trap EXIT
set +e
python3 - "${PORT}" <<'PY'
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

port = sys.argv[1]
password = os.environ["OPENCODE_SERVER_PASSWORD"]
auth = base64.b64encode(f"opencode:{password}".encode()).decode()
headers = {"Authorization": f"Basic {auth}"}

def _get(path: str, timeout: float = 2.0) -> tuple[int, str]:
    req = urllib.request.Request(f"http://localhost:{port}{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")

# 1) 等 opencode HTTP API 就绪（最长 30 秒）
for attempt in range(1, 31):
    try:
        status, _ = _get("/global/health")
        if status == 200:
            print(f"[entrypoint] opencode healthy after {attempt}s", flush=True)
            break
    except (urllib.error.URLError, ConnectionError, OSError):
        pass
    time.sleep(1)
else:
    print("[entrypoint] FATAL: opencode failed /global/health within 30s", file=sys.stderr)
    sys.exit(3)

required = ("Prometheus", "Sisyphus")

def _missing_agents(body: str) -> list[str]:
    # 同时支持 JSON 解析校验和子串兜底校验，以防 opencode /agent 响应 schema 变化。
    found_via_json: set[str] = set()
    try:
        parsed = json.loads(body)
        if isinstance(parsed, list):
            items = parsed
        elif isinstance(parsed, dict):
            items = parsed.get("agents") or parsed.get("items") or []
        else:
            items = []
        for item in items:
            name = item.get("name") if isinstance(item, dict) else None
            if isinstance(name, str):
                for required_name in required:
                    if required_name in name:
                        found_via_json.add(required_name)
    except (ValueError, TypeError):
        pass

    return [
        name for name in required
        if name not in found_via_json and f'"{name}"' not in body and name not in body
    ]


# 2) 校验 oh-my-openagent 已注册 Prometheus + Sisyphus
last_status = None
last_body = ""
last_error = None
missing = list(required)

for attempt in range(1, 31):
    try:
        status, body = _get("/agent", timeout=2.0)
        last_status = status
        last_body = body
        missing = _missing_agents(body)
        if not missing:
            print(f"[entrypoint] /agent ready after {attempt}s status={status} body={body[:500]}", flush=True)
            print(f"[entrypoint] verified oh-my-openagent agents loaded: {', '.join(required)}", flush=True)
            break
    except Exception as exc:
        last_error = exc
    time.sleep(1)
else:
    if last_status is None:
        print(f"[entrypoint] FATAL: GET /agent failed within 30s: {last_error}", file=sys.stderr)
        sys.exit(4)
    print(f"[entrypoint] /agent last_status={last_status} body={last_body[:500]}", flush=True)
    print(
        f"[entrypoint] FATAL: oh-my-openagent NOT loaded after 30s — missing required agents: {missing}",
        file=sys.stderr,
    )
    print(
        "[entrypoint] expected per ADR-001/006; check oh-my cache integrity in image "
        "(docker/worker/Dockerfile.arm64) or oh-my-openagent loader logs above",
        file=sys.stderr,
    )
    sys.exit(5)
PY
verify_rc=$?
set -e

if [ "${verify_rc}" -ne 0 ]; then
    echo "[entrypoint] agent verification failed (rc=${verify_rc}); terminating opencode" >&2
    kill -TERM "${OPENCODE_PID}" 2>/dev/null || true
    wait "${OPENCODE_PID}" 2>/dev/null || true
    exit "${verify_rc}"
fi

# 校验通过：把 opencode 退出码作为容器退出码
wait "${OPENCODE_PID}"
exit $?
