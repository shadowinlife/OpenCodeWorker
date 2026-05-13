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
