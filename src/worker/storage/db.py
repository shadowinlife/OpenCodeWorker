"""
SQLite 数据库初始化与连接管理。

库设计原则：
    - 使用单一持久连接（_connection 全局单例），在 FastAPI lifespan
      中 init_db() / close_db() 管理生命周期。
    - task_events 表设计为 append-only（尚未写入的行不能再修改），
      为 SSE cursor replay 提供可靠的事件溯源。
    - 所有写操作完成后立即 commit，避免内存中数据在进程崩溃后丢失。
    - 启用 WAL 模式 + busy_timeout（init_db 中设置；详见 P1-9 修复）：
        * journal_mode=WAL：读写不互斥，并发任务写事件不再被读 SSE 阻塞
        * synchronous=NORMAL：在 WAL 下兼顾耐久性与吞吐
        * busy_timeout=5000：读写抢锁时等待 5s 而非立刻 SQLITE_BUSY

    表结构视图：
        tasks          — 任务元数据与状态，每任务一行
        task_events    — 任务事件流，append-only，包含 SSE cursor 索引
        decisions      — HITL 决策记录，idempotency_key 唯一索引
        artifacts      — 任务产物元数据（文件内容存储在文件系统）
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import aiosqlite

# 进程级单例连接，需在 FastAPI lifespan 中 init_db() 后才可使用
_connection: Optional[aiosqlite.Connection] = None

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

# DDL 一次性建表语句。IF NOT EXISTS 保证幂等性，重启进程不会丢失数据。
_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    id                  TEXT PRIMARY KEY,
    status              TEXT NOT NULL,
    mode                TEXT NOT NULL,
    request_json        TEXT NOT NULL,
    container_id        TEXT,
    opencode_session_id TEXT,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL,
    completed_at        REAL
);

CREATE TABLE IF NOT EXISTS task_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,   -- global cursor
    event_id     INTEGER NOT NULL,                    -- per-task sequence
    task_id      TEXT    NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind         TEXT    NOT NULL,
    payload_json TEXT    NOT NULL DEFAULT '{}',
    ts           REAL    NOT NULL,
    UNIQUE (task_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_task_events_cursor
    ON task_events (task_id, event_id);

CREATE TABLE IF NOT EXISTS decisions (
    id               TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind             TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    request_json     TEXT NOT NULL,
    response_json    TEXT,
    idempotency_key  TEXT,
    created_at       REAL NOT NULL,
    resolved_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_decisions_task
    ON decisions (task_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_idempotency
    ON decisions (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS artifacts (
    id         TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    type       TEXT NOT NULL,
    filename   TEXT NOT NULL,
    file_path  TEXT,
    size       INTEGER,
    created_at REAL NOT NULL,
    expires_at REAL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task
    ON artifacts (task_id);
"""


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def init_db(db_path: Path) -> aiosqlite.Connection:
    """建表并返回持久连接。应在 FastAPI lifespan 的开始事件中调用，且只调用一次。

    此函数会：
        1. 确保 db_path 父目录存在
        2. 创建或打开现有 SQLite 文件
        3. 设置 row_factory 为 aiosqlite.Row（支持按列名访问）
        4. 执行建表 DDL
        5. 将连接存入全局 _connection
    """
    global _connection
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row

    # P1-9: 启用 WAL + 抢锁等待，必须先于任何数据写入
    # journal_mode=WAL 一旦设置即写入数据库文件 header，跨重启持久化
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA busy_timeout=5000")

    await conn.executescript(_DDL)
    await conn.commit()
    _connection = conn
    return conn


async def close_db() -> None:
    """关闭持久连接。应在 FastAPI lifespan 的关闭事件中调用。"""
    global _connection
    if _connection is not None:
        await _connection.close()
        _connection = None


async def get_db() -> aiosqlite.Connection:
    """返回全局连接对象。FastAPI 路由和后台任务均通过此函数获取 DB 实例。

    Raises:
        RuntimeError: 若 init_db() 尚未调用（通常是程序启动顺序错误）
    """
    if _connection is None:
        raise RuntimeError("数据库尚未初始化 — 请先调用 init_db()。")
    return _connection
