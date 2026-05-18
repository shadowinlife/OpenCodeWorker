"""
Worker 进程配置，基于 pydantic-settings 从环境变量加载。

优先级（高→低）：
    1. 系统环境变量（WORKER_* 前缀）
    2. 项目根目录的 .env 文件
    3. Settings 类中定义的默认值

必须设置的环境变量：
    WORKER_BEARER_TOKEN  — Worker HTTP API 的 Bearer 认证令牌
                          建议用 `openssl rand -hex 32` 生成，长度 ≥ 32 字符

典型最小化启动命令：
    WORKER_BEARER_TOKEN=<token> \\
    ANTHROPIC_API_KEY=<key> \\
    conda run -n legonanobot python -m uvicorn worker.main:app --port 8080

安全注意事项：
    - bearer_token 不会出现在日志、repr 或 /metrics 端点中
    - secrets_dir=None 防止意外从 /run/secrets 挂载额外 secrets
"""
from __future__ import annotations

import functools
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Worker 全局配置，单例模式，通过 get_settings() 访问。"""

    model_config = SettingsConfigDict(
        env_prefix="WORKER_",
        env_file=".env",
        env_file_encoding="utf-8",
        # 禁止从 secrets_dir 加载，防止 Docker secrets 文件意外覆盖 env var
        secrets_dir=None,
    )

    # ------------------------------------------------------------------ #
    # 认证（必须设置）                                                      #
    # ------------------------------------------------------------------ #
    # Worker API 的静态 Bearer token，调用方须在 Authorization 头中携带
    # 格式：Authorization: Bearer <token>
    # 无默认值——启动时若未设置会立即抛出 ValidationError
    bearer_token: str

    # ------------------------------------------------------------------ #
    # 并发控制                                                              #
    # ------------------------------------------------------------------ #
    # 同时运行的最大任务数，超出时新请求返回 429 quota_exceeded
    # Colima 开发环境（2 CPU / 2 GB）推荐设为 1~2，防止资源争抢
    max_concurrent_tasks: int = 3

    # ------------------------------------------------------------------ #
    # 持久化存储                                                            #
    # ------------------------------------------------------------------ #
    # SQLite 数据库文件路径（相对于进程 cwd 或绝对路径均可）
    db_path: Path = Path("data/worker.db")
    # 数据根目录，artifacts 子目录会在此下自动创建
    data_dir: Path = Path("data")
    # 任务产物文件保留天数，超期后由清理任务删除文件和 DB 记录
    artifact_retention_days: int = 7
    # artifact GC 扫描周期（秒）。每次扫描会删除 expires_at <= now 的文件
    # 与 DB 行；默认 1 小时，测试可调小至 0.05 秒触发立即扫描
    # [REVIEW: P1-19]
    artifact_gc_interval_sec: float = 3600.0

    # ------------------------------------------------------------------ #
    # HTTP 服务器                                                           #
    # ------------------------------------------------------------------ #
    host: str = "0.0.0.0"
    port: int = 8080
    # uvicorn 日志级别：DEBUG / INFO / WARNING / ERROR
    log_level: str = "INFO"

    # ------------------------------------------------------------------ #
    # SSE 推送                                                              #
    # ------------------------------------------------------------------ #
    # heartbeat 事件发送间隔（秒），防止代理/负载均衡因无数据而断连
    # 建议 ≤ 30 秒；Nginx 默认 proxy_read_timeout 为 60 秒
    sse_heartbeat_sec: float = 15.0

    # ------------------------------------------------------------------ #
    # Docker 沙箱（Phase 2）                                                #
    # ------------------------------------------------------------------ #
    # Worker 沙箱容器使用的 Docker 镜像（需预先构建并推送）
    # 本地开发可设为 "worker-sandbox:dev"
    sandbox_image: str = "worker-sandbox:latest"

    # 沙箱容器使用的 Docker network 名称（internal=True，无外网访问）
    sandbox_network: str = "worker-sandbox-net"

    # 是否允许 workspace.kind="local"（host bind mount，root + 关只读 FS）
    # 默认关闭——local 模式会以 root 挂载宿主目录、关闭只读根文件系统，
    # 任何上游若能控制请求体即可绕过 MVP 的"非 root + read-only FS"安全策略。
    # 仅在受信任的开发/调试环境（用户掌控全部调用方）显式打开。
    # 环境变量：WORKER_ALLOW_HOST_MOUNT=true
    allow_host_mount: bool = False

    # ------------------------------------------------------------------ #
    # Host Broker（推迟到 Phase 7，MVP 阶段不视作默认安全模型）             #
    # ------------------------------------------------------------------ #
    # 是否启用 Broker 出口代理（True = 注入 HTTP_PROXY 并管控容器出站流量）
    # ⚠️ MVP 阶段默认 False：CONNECT 隧道与 broker 进程启停尚未实现
    # （见 docs/archive/code-review-2026-05-14.md P0-1/P0-3、ADR-004 实现状态表）。
    # 即便显式设为 True，也只会注入 HTTP_PROXY 环境变量，宿主上无人监听，
    # 容器对外请求会立即失败。在 broker 完整实现前不要打开此开关。
    # 环境变量：WORKER_BROKER_ENABLED=true
    broker_enabled: bool = False
    # Broker 服务监听地址（Worker 进程本机）
    broker_host: str = "127.0.0.1"
    broker_port: int = 8090

    @property
    def artifacts_dir(self) -> Path:
        """产物文件存储目录（data_dir/artifacts/），由 Orchestrator 按 task_id 建子目录。"""
        return self.data_dir / "artifacts"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回全局单例 Settings 对象。

    使用 lru_cache 确保整个进程生命周期内只读取一次环境变量，
    避免运行中因环境变量变化导致配置不一致。

    测试时可通过 get_settings.cache_clear() + mock 环境变量来替换配置。
    """
    return Settings()
