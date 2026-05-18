"""
Artifact GC：周期性删除过期 artifact 文件与 DB 记录。

[REVIEW: P1-19] 设置项 `artifact_retention_days` 此前只在 insert 时写入
`expires_at`，从未读取，导致 `data/artifacts/` 永久增长直到 ENOSPC。
本模块在 lifespan 中起一个后台协程，按 `artifact_gc_interval_sec` 周期
扫描 `expires_at <= now` 的行，先删文件再删 DB 行。

设计要点：
    - 文件删除先于 DB 行删除：若文件 unlink 失败则 DB 行保留，下轮重试；
      避免 DB 行已删但文件残留无主导致永久泄漏。
    - 文件已不存在视作 "missing_file"，仍删除 DB 行收敛状态。
    - 只 unlink 落在 `settings.artifacts_dir` 子树内的路径，防误删。
    - 单轮限 `_GC_BATCH_LIMIT` 行，超出由下轮续清，避免长事务。
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

import aiosqlite

from worker.config import get_settings
from worker.observability import metrics
from worker.storage.db import get_db
from worker.storage.repo import (
    delete_artifact_row,
    select_expired_artifacts,
)

logger = logging.getLogger(__name__)

_GC_BATCH_LIMIT = 500


async def start_artifact_gc() -> asyncio.Task:
    """启动 artifact GC 后台协程，FastAPI lifespan 中调用一次。"""
    settings = get_settings()
    interval = settings.artifact_gc_interval_sec
    logger.info("artifact GC starting, interval=%.1fs", interval)
    return asyncio.create_task(_gc_loop(interval), name="artifact-gc")


async def _gc_loop(interval: float) -> None:
    """循环执行一轮 GC + sleep。单轮异常吞掉、循环不退出。"""
    while True:
        try:
            stats = await gc_run_once(now=time.time())
            if stats["deleted"] or stats["errors"] or stats["missing_file"]:
                logger.info(
                    "artifact GC: deleted=%d missing_file=%d errors=%d",
                    stats["deleted"],
                    stats["missing_file"],
                    stats["errors"],
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("artifact GC cycle failed")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


async def gc_run_once(
    now: float,
    *,
    db: Optional[aiosqlite.Connection] = None,
    artifacts_root: Optional[Path] = None,
    limit: int = _GC_BATCH_LIMIT,
) -> dict[str, int]:
    """扫描一次过期 artifact，物理清理 + DB 行清理。

    可选参数主要用于单元测试覆盖（注入临时 DB / artifacts 根目录）。

    Returns:
        dict 含 deleted / missing_file / errors 三项 int 计数。
    """
    if db is None:
        db = await get_db()
    if artifacts_root is None:
        artifacts_root = get_settings().artifacts_dir.resolve()
    else:
        artifacts_root = artifacts_root.resolve()

    expired = await select_expired_artifacts(db, before=now, limit=limit)
    stats = {"deleted": 0, "missing_file": 0, "errors": 0}

    for artifact_id, file_path in expired:
        result = _unlink_file(file_path, artifacts_root)
        if result == "error":
            stats["errors"] += 1
            metrics.inc_artifact_gc_deleted("error")
            continue

        try:
            await delete_artifact_row(db, artifact_id)
        except Exception:
            logger.exception(
                "artifact GC: db delete failed id=%s", artifact_id,
            )
            stats["errors"] += 1
            metrics.inc_artifact_gc_deleted("error")
            continue

        if result == "missing_file":
            stats["missing_file"] += 1
        else:
            stats["deleted"] += 1
        metrics.inc_artifact_gc_deleted(result)

    return stats


def _unlink_file(file_path: Optional[str], root: Path) -> str:
    """unlink 一个 artifact 文件。返回 'ok' / 'missing_file' / 'error'。

    file_path 为 None 视作 ok（仅元数据 artifact）。
    路径不在 root 子树内则拒绝删除并返回 error。
    """
    if not file_path:
        return "ok"
    p = Path(file_path)
    try:
        resolved = p.resolve(strict=False)
    except OSError:
        return "error"
    try:
        resolved.relative_to(root)
    except ValueError:
        logger.warning(
            "artifact GC: refused to unlink path outside artifacts_dir: %s",
            file_path,
        )
        return "error"
    try:
        resolved.unlink()
        return "ok"
    except FileNotFoundError:
        return "missing_file"
    except OSError as exc:
        logger.warning("artifact GC: unlink failed %s: %s", file_path, exc)
        return "error"
