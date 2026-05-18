"""
单元测试：P1-9 SQLite WAL 模式启用

验证 init_db() 之后：
    - PRAGMA journal_mode 返回 'wal'
    - PRAGMA synchronous 返回 1（NORMAL）
    - PRAGMA busy_timeout 返回 5000
"""
from __future__ import annotations

from pathlib import Path

import pytest

from worker.storage import db as db_module


@pytest.fixture
async def temp_db(tmp_path: Path):
    db_file = tmp_path / "wal_test.db"
    conn = await db_module.init_db(db_file)
    try:
        yield conn
    finally:
        await db_module.close_db()


async def test_journal_mode_is_wal(temp_db):
    async with temp_db.execute("PRAGMA journal_mode") as cur:
        row = await cur.fetchone()
    assert row[0].lower() == "wal", f"expected wal, got {row[0]!r}"


async def test_synchronous_is_normal(temp_db):
    async with temp_db.execute("PRAGMA synchronous") as cur:
        row = await cur.fetchone()
    # 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA
    assert row[0] == 1, f"expected NORMAL(1), got {row[0]}"


async def test_busy_timeout_is_5000ms(temp_db):
    async with temp_db.execute("PRAGMA busy_timeout") as cur:
        row = await cur.fetchone()
    assert row[0] == 5000, f"expected 5000, got {row[0]}"


async def test_wal_persists_across_reopen(tmp_path: Path):
    """journal_mode=WAL 写入 db 文件 header 后跨重启保持。"""
    db_file = tmp_path / "wal_persist.db"
    await db_module.init_db(db_file)
    await db_module.close_db()

    conn = await db_module.init_db(db_file)
    try:
        async with conn.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
        assert row[0].lower() == "wal"
    finally:
        await db_module.close_db()
