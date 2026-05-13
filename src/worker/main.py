"""
FastAPI 应用入口。

职责：
    1. 创建 FastAPI 实例，注册 lifespan 钩子（startup / shutdown）
    2. 挂载 BearerTokenMiddleware 进行全局鉴权
    3. 注册 APIRouter（来自 worker.api.routes）
    4. 提供 run() 入口（供 pyproject.toml entry_point 调用）

Lifespan 流程：
    startup:
        - 确保数据目录和产物目录存在
        - 初始化 SQLite 连接（WAL 模式，写并发友好）
        - 启动后台 asyncio 队列消费协程（_worker_loop）

    shutdown:
        - 等待 asyncio 队列消费协程退出
        - 关闭 SQLite 连接，刷写 WAL 日志

典型启动命令：
    WORKER_BEARER_TOKEN=<token> conda run -n legonanobot \\
        python -m uvicorn worker.main:app --host 0.0.0.0 --port 8080

    或通过 entry_point：
    WORKER_BEARER_TOKEN=<token> opencode-worker
"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

from worker.api.middleware import BearerTokenMiddleware
from worker.api.routes import router
from worker.config import get_settings
from worker.orchestrator.queue import start_queue_worker
from worker.storage.db import close_db, init_db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan 上下文管理器（替代已废弃的 on_event 装饰器）。

    yield 之前为 startup 逻辑，yield 之后为 shutdown 逻辑。
    """
    # ------------------------------------------------------------------ #
    # startup                                                               #
    # ------------------------------------------------------------------ #
    settings = get_settings()

    # 确保数据目录存在（db_path 父目录 + artifacts 目录）
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    logger.info("data dirs ready: db=%s artifacts=%s",
                settings.db_path, settings.artifacts_dir)

    # 初始化 SQLite 连接（进程单例，开启 WAL 模式）
    await init_db(settings.db_path)
    logger.info("SQLite initialized: %s", settings.db_path)

    # 启动后台任务队列消费协程
    queue_task = await start_queue_worker()
    logger.info("queue worker started")

    # ------------------------------------------------------------------ #
    # 将控制权交给 FastAPI（处理请求）                                       #
    # ------------------------------------------------------------------ #
    yield

    # ------------------------------------------------------------------ #
    # shutdown                                                              #
    # ------------------------------------------------------------------ #
    logger.info("shutting down queue worker...")
    # 取消后台队列协程，等待其干净退出
    queue_task.cancel()
    try:
        await queue_task
    except Exception:
        pass

    # 关闭 SQLite 连接，确保 WAL 日志刷写到主 DB 文件
    await close_db()
    logger.info("SQLite closed")


def create_app() -> FastAPI:
    """工厂函数，构造并配置 FastAPI 实例。

    拆成独立函数方便测试时直接调用，不依赖全局 `app` 变量。
    """
    settings = get_settings()

    _app = FastAPI(
        title="VibeTradingOpenCodeWorker",
        description=(
            "安全的 OpenCode Worker：在隔离 Docker 沙箱中运行 opencode，"
            "通过 HTTP + SSE 向上游回传事件，支持 HITL 人机交互决策。"
        ),
        version="0.1.0",
        # 生产建议：通过反向代理限制 /docs 访问，或将 docs_url=None
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # Bearer token 鉴权中间件（在所有路由之前执行）
    _app.add_middleware(BearerTokenMiddleware)

    # 注册路由（所有 /health、/ready、/tasks/... 端点）
    _app.include_router(router)

    return _app


# ---------------------------------------------------------------------------
# 全局 FastAPI 实例（uvicorn worker.main:app 入口）
# ---------------------------------------------------------------------------
app = create_app()


# ---------------------------------------------------------------------------
# Entry point（opencode-worker 命令，由 pyproject.toml [scripts] 定义）
# ---------------------------------------------------------------------------

def run() -> None:
    """CLI 入口，等价于直接运行 uvicorn。

    用法：WORKER_BEARER_TOKEN=<token> opencode-worker
    通常在容器内通过 CMD ["opencode-worker"] 启动。
    """
    import uvicorn

    settings = get_settings()
    # 配置 Python 根 logger，让 uvicorn 和应用日志统一格式
    logging.basicConfig(
        stream=sys.stdout,
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    )
    uvicorn.run(
        "worker.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        # 生产模式不启用 reload；开发时在命令行加 --reload
        reload=False,
    )
