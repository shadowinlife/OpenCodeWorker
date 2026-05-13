"""
Bearer token 认证中间件。

所有非豁免路径（/health、/ready、/docs、/openapi.json）都需要
携带有效的 Bearer token，中间件在请求到达路由处理器之前完成验证。

安全设计：
    - 使用 hmac.compare_digest() 进行常数时间比较，防止时序攻击
      （timing attack）——即攻击者通过响应时间差来猜测 token 字符串。
    - token 不会出现在任何日志输出中。
    - 认证失败统一返回 401，不区分"无 token"和"token 错误"两种情况，
      防止信息泄露。
    - 公开路径使用精确前缀匹配，不使用正则，防止绕过漏洞。
"""
from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from worker.config import get_settings

# 无需鉴权的路径前缀（精确前缀匹配）
# 注意：/docs 和 /openapi.json 仅在开发模式下开放，生产部署时建议关闭 Swagger UI
_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/health",
    "/ready",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/favicon.ico",
)


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """验证 HTTP Authorization: Bearer <token> 头。

    对所有非公开路径强制进行 Bearer token 验证：
        1. 提取 Authorization 头
        2. 校验格式为 "Bearer <value>"（不区分 "Bearer " 大小写）
        3. 使用 hmac.compare_digest 进行常数时间比较
        4. 校验失败时返回 401 JSON 响应，不传播到路由层
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # 公开路径直接放行，不做任何认证
        if self._is_public(request.url.path):
            return await call_next(request)

        # 提取并验证 Authorization 头
        auth_header = request.headers.get("Authorization", "")
        token = self._extract_bearer(auth_header)
        if token is None:
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized", "message": "missing or malformed Authorization header"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        # 常数时间比较，防止时序攻击
        settings = get_settings()
        if not hmac.compare_digest(token.encode(), settings.bearer_token.encode()):
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized", "message": "invalid token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)

    # ------------------------------------------------------------------ #
    # 内部辅助                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_public(path: str) -> bool:
        """检查路径是否在公开路径白名单内（精确前缀匹配）。"""
        for prefix in _PUBLIC_PREFIXES:
            if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
                return True
        return False

    @staticmethod
    def _extract_bearer(auth_header: str) -> str | None:
        """从 Authorization 头提取 Bearer token 值。

        合法格式（大小写不敏感前缀）：
            Authorization: Bearer <token>

        返回 token 字符串（去除首尾空白），格式不符合时返回 None。
        """
        if not auth_header:
            return None
        # 允许 "Bearer " 大小写不敏感
        lower = auth_header.lower()
        if not lower.startswith("bearer "):
            return None
        token = auth_header[7:].strip()  # len("bearer ") == 7
        if not token:
            return None
        return token
