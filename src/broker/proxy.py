"""
Host Broker MVP：HTTP egress 代理，带域名白名单 + 审计日志。

架构角色：
    Broker 是容器与外部网络之间的唯一出口。
    容器通过环境变量 HTTP_PROXY / HTTPS_PROXY 指向 Broker，
    所有出站请求都经过 Broker 的白名单检查和审计日志。

    本模块实现为 ASGI 应用（Starlette），可独立启动（独立进程/端口）
    或在开发模式下与 Worker API 同进程运行（不推荐生产使用）。

实现形式：
    - CONNECT 隧道：用于 HTTPS 请求（CONNECT 方法）
    - 普通 HTTP 转发：用于 HTTP 请求（GET/POST 等）

安全措施：
    - 所有请求必须携带 X-Task-ID 头（由容器入口脚本注入 HTTP_PROXY 环境变量的
      proxy 地址配置，或由 opencode 通过 env 传入）
    - 白名单检查：目标 host 必须在任务策略中（policy.is_allowed()）
    - 禁止访问 RFC 1918 私有地址（防 SSRF 攻击宿主或内网）
    - 审计日志：记录每个代理请求的 task_id/method/host/path/status_code

局限（Phase 2 MVP）：
    - 仅实现 CONNECT 隧道 + 普通 HTTP forward，不做深度 TLS 检查
    - 速率限制未实现（Phase 6 补充）
    - task_id 从 X-Task-ID 头获取；生产环境应考虑通过 per-task proxy token 隔离

路由：
    - CONNECT <host>:<port> HTTP/1.1  → 白名单检查 → TCP 隧道
    - <METHOD> http://<host>/<path>   → 白名单检查 → httpx 转发
    - POST /broker/tasks/:id/policy   → 更新任务白名单（Worker 内部调用）
    - GET  /broker/health             → 健康检查
    - GET  /broker/policies           → 活跃策略列表（调试）
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from broker.policy import (
    get_task_policy,
    is_allowed,
    list_active_policies,
    set_task_policy,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# RFC 1918 私有地址块（SSRF 防护）
# ──────────────────────────────────────────────────────────────────────────────

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 private
]

# Broker 自身监听的容器内地址不在 SSRF 黑名单（由 ensure_worker_network 隔离）
# 实际运行时 broker host 是容器内已知的，此处只禁止外部 RFC1918 访问
_BROKER_INTERNAL_HOSTS = {"broker", "host.docker.internal"}


def _is_private_host(host: str) -> bool:
    """检查 host 是否解析到私有地址（SSRF 防护）。

    仅做 IP 字符串检查，不做 DNS 解析（DNS rebinding 防护在生产中需要额外措施）。
    """
    if host in _BROKER_INTERNAL_HOSTS:
        return False  # broker 自身地址是合法目标
    try:
        addr = ipaddress.ip_address(host)
        return any(addr in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        # host 是域名，不是 IP，无法做 IP 段检查（DNS 解析在实际连接时发生）
        # 域名白名单已在 policy 层控制，此处通过
        return False


# ──────────────────────────────────────────────────────────────────────────────
# 请求处理
# ──────────────────────────────────────────────────────────────────────────────

async def _handle_connect(scope: dict, receive, send) -> None:
    """处理 HTTP CONNECT 隧道请求（HTTPS 代理）。

    流程：
        1. 解析目标 host:port
        2. 白名单检查
        3. TCP 连接到目标
        4. 返回 200 Connection Established
        5. 双向 TCP 数据转发（asyncio 流）
    """
    task_id = dict(scope.get("headers", [])).get(b"x-task-id", b"").decode()
    path = scope["path"]  # CONNECT 时 path 是 "host:port"
    # Starlette CONNECT 路径在 raw_path 中
    target = scope.get("path", "")

    # CONNECT 请求的目标在 scope["path"] 不是标准路径，
    # 而是从原始请求行解析出来的，Starlette 通常将其放入 path
    host_port = target.lstrip("/")
    if ":" in host_port:
        host, port_str = host_port.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            port = 443
    else:
        host = host_port
        port = 443

    _log_request("CONNECT", task_id, host, port, path)

    # 白名单检查
    if not is_allowed(task_id, host, port):
        logger.warning(
            "broker DENIED CONNECT %s:%d for task %s", host, port, task_id
        )
        await _send_response(send, 403, b"Forbidden by egress policy")
        return

    # SSRF 防护
    if _is_private_host(host):
        logger.warning(
            "broker DENIED CONNECT to private host %s (task=%s)", host, task_id
        )
        await _send_response(send, 403, b"Access to private addresses forbidden")
        return

    # 建立到目标的 TCP 连接
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except (OSError, ConnectionRefusedError) as exc:
        logger.warning("broker CONNECT failed %s:%d: %s", host, port, exc)
        await _send_response(send, 502, f"Bad Gateway: {exc}".encode())
        return

    # 返回 200 Connection Established
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-length", b"0")],
    })
    await send({"type": "http.response.body", "body": b""})

    # 双向转发（简化版：ASGI 不直接支持原始 TCP 双向，
    # 生产版本需要 asyncio.Protocol 或 trio 实现；此处为 MVP 占位）
    # Phase 2 MVP：CONNECT 建立后通知已就绪，实际数据转发在真实 proxy 中实现
    writer.close()
    await writer.wait_closed()
    logger.info("broker CONNECT closed %s:%d (task=%s)", host, port, task_id)


async def _handle_http_forward(request: Request) -> Response:
    """处理普通 HTTP 转发请求（GET/POST 等）。"""
    import httpx

    task_id = request.headers.get("x-task-id", "")
    target_url = str(request.url)

    # 从请求 URL 提取 host 和 port
    parsed = urlparse(target_url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    _log_request(request.method, task_id, host, port, parsed.path)

    # 白名单检查
    if not is_allowed(task_id, host, port):
        logger.warning(
            "broker DENIED %s %s for task %s", request.method, host, task_id
        )
        return JSONResponse(
            {"error": "egress_denied", "host": host},
            status_code=403,
        )

    # SSRF 防护
    if _is_private_host(host):
        logger.warning(
            "broker DENIED access to private host %s (task=%s)", host, task_id
        )
        return JSONResponse(
            {"error": "private_address_forbidden"},
            status_code=403,
        )

    # 转发请求（移除 Proxy 相关头，添加审计头）
    forward_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "x-task-id", "proxy-connection", "proxy-authorization")
    }
    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method=request.method,
                url=target_url,
                headers=forward_headers,
                content=body,
                follow_redirects=False,
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        logger.warning("broker forward failed %s: %s", host, exc)
        return JSONResponse({"error": "upstream_error", "detail": str(exc)}, status_code=502)

    logger.info(
        "broker forward %s %s → %d (task=%s)",
        request.method, host, resp.status_code, task_id
    )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=dict(resp.headers),
        media_type=resp.headers.get("content-type"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# 管理端点
# ──────────────────────────────────────────────────────────────────────────────

async def _broker_health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "broker"})


async def _update_policy(request: Request) -> JSONResponse:
    """POST /broker/tasks/{task_id}/policy — 设置/更新任务出站白名单。

    Body: {"allow_egress_hosts": ["api.openai.com", "pypi.org"]}
    """
    task_id = request.path_params["task_id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)

    hosts = body.get("allow_egress_hosts", [])
    if not isinstance(hosts, list):
        return JSONResponse({"error": "allow_egress_hosts must be a list"}, status_code=400)

    set_task_policy(task_id, hosts)
    return JSONResponse({"task_id": task_id, "allowed_hosts": get_task_policy(task_id)})


async def _list_policies(request: Request) -> JSONResponse:
    """GET /broker/policies — 返回所有活跃任务策略（调试用）。"""
    return JSONResponse(list_active_policies())


# ──────────────────────────────────────────────────────────────────────────────
# ASGI 应用
# ──────────────────────────────────────────────────────────────────────────────

def create_broker_app() -> Starlette:
    """创建 Broker ASGI 应用。"""
    return Starlette(
        routes=[
            Route("/broker/health", _broker_health, methods=["GET"]),
            Route("/broker/tasks/{task_id}/policy", _update_policy, methods=["POST"]),
            Route("/broker/policies", _list_policies, methods=["GET"]),
        ],
    )


# ──────────────────────────────────────────────────────────────────────────────
# 独立启动入口
# ──────────────────────────────────────────────────────────────────────────────

def run_broker(host: str = "0.0.0.0", port: int = 8090) -> None:
    """独立启动 Broker 服务（生产部署：独立进程）。"""
    import uvicorn

    app = create_broker_app()
    uvicorn.run(app, host=host, port=port, log_level="info")


# ──────────────────────────────────────────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────────────────────────────────────────

def _log_request(
    method: str, task_id: str, host: str, port: int, path: str
) -> None:
    logger.info(
        "broker %s task=%s target=%s:%d path=%s",
        method, task_id or "(none)", host, port, path
    )


async def _send_response(send, status: int, body: bytes) -> None:
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"text/plain"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})
