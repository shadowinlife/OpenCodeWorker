"""
单元测试：P1-19 artifact GC

验证：
    - expires_at <= now 的 artifact 被删除（文件 + DB 行）
    - expires_at > now 的 artifact 保留
    - 文件已不存在时仍删除 DB 行（missing_file 计入返回值）
    - file_path 落在 artifacts_dir 之外时拒绝 unlink、保留 DB 行
    - file_path 为 NULL 的 artifact 仍被 GC（仅删 DB 行）
    - 单轮 limit 控制扫描行数
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from worker.contract.artifact import Artifact, ArtifactType
from worker.observability import metrics
from worker.orchestrator import artifact_gc
from worker.storage import db as db_module
from worker.storage.repo import insert_artifact, insert_task, list_artifacts
from worker.contract.task import Message, TaskMode, TaskRequest


@pytest.fixture
async def temp_db(tmp_path: Path):
    db_file = tmp_path / "gc_test.db"
    conn = await db_module.init_db(db_file)
    try:
        yield conn
    finally:
        await db_module.close_db()


@pytest.fixture
async def task_id(temp_db):
    # artifacts.task_id 是外键，需要先插入 tasks 一行
    req = TaskRequest(
        messages=[Message(role="user", content="x")],
        mode=TaskMode.direct_execute,
    )
    resp = await insert_task(temp_db, req)
    return resp.task_id


def _make_artifact(task_id: str, expires_at: float | None) -> Artifact:
    return Artifact(
        artifact_id=str(uuid.uuid4()),
        task_id=task_id,
        type=ArtifactType.diff,
        filename="changes.diff.json",
        size=10,
        created_at=time.time(),
        expires_at=expires_at,
    )


async def test_gc_deletes_expired_file_and_row(temp_db, task_id, tmp_path: Path):
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()
    file_path = artifacts_root / "expired.json"
    file_path.write_text("dummy")

    art = _make_artifact(task_id, expires_at=100.0)  # 远早于 now
    await insert_artifact(temp_db, art, file_path=str(file_path))

    stats = await artifact_gc.gc_run_once(
        now=time.time(), db=temp_db, artifacts_root=artifacts_root,
    )

    assert stats["deleted"] == 1
    assert stats["missing_file"] == 0
    assert stats["errors"] == 0
    assert not file_path.exists()
    assert await list_artifacts(temp_db, task_id) == []


async def test_gc_skips_unexpired(temp_db, task_id, tmp_path: Path):
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()
    file_path = artifacts_root / "fresh.json"
    file_path.write_text("dummy")

    art = _make_artifact(task_id, expires_at=time.time() + 3600)
    await insert_artifact(temp_db, art, file_path=str(file_path))

    stats = await artifact_gc.gc_run_once(
        now=time.time(), db=temp_db, artifacts_root=artifacts_root,
    )

    assert stats == {"deleted": 0, "missing_file": 0, "errors": 0}
    assert file_path.exists()
    assert len(await list_artifacts(temp_db, task_id)) == 1


async def test_gc_handles_missing_file(temp_db, task_id, tmp_path: Path):
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()
    ghost_path = artifacts_root / "ghost.json"  # 故意不创建

    art = _make_artifact(task_id, expires_at=100.0)
    await insert_artifact(temp_db, art, file_path=str(ghost_path))

    stats = await artifact_gc.gc_run_once(
        now=time.time(), db=temp_db, artifacts_root=artifacts_root,
    )

    assert stats["missing_file"] == 1
    assert stats["deleted"] == 0
    assert stats["errors"] == 0
    assert await list_artifacts(temp_db, task_id) == []


async def test_gc_refuses_path_outside_root(temp_db, task_id, tmp_path: Path):
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("don't touch me")

    art = _make_artifact(task_id, expires_at=100.0)
    await insert_artifact(temp_db, art, file_path=str(outside))

    stats = await artifact_gc.gc_run_once(
        now=time.time(), db=temp_db, artifacts_root=artifacts_root,
    )

    # 拒绝 unlink、不删 DB 行：保留为 errors 等待人工处理
    assert stats["errors"] == 1
    assert stats["deleted"] == 0
    assert outside.exists()
    assert len(await list_artifacts(temp_db, task_id)) == 1


async def test_gc_handles_null_file_path(temp_db, task_id, tmp_path: Path):
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()

    art = _make_artifact(task_id, expires_at=100.0)
    await insert_artifact(temp_db, art, file_path=None)

    stats = await artifact_gc.gc_run_once(
        now=time.time(), db=temp_db, artifacts_root=artifacts_root,
    )

    assert stats["deleted"] == 1
    assert await list_artifacts(temp_db, task_id) == []


async def test_gc_respects_batch_limit(temp_db, task_id, tmp_path: Path):
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()

    for i in range(5):
        f = artifacts_root / f"a{i}.json"
        f.write_text("x")
        art = _make_artifact(task_id, expires_at=100.0 + i)
        await insert_artifact(temp_db, art, file_path=str(f))

    stats = await artifact_gc.gc_run_once(
        now=time.time(), db=temp_db, artifacts_root=artifacts_root, limit=2,
    )

    assert stats["deleted"] == 2
    remaining = await list_artifacts(temp_db, task_id)
    assert len(remaining) == 3


async def test_gc_metric_counter_increments(temp_db, task_id, tmp_path: Path):
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()
    file_path = artifacts_root / "m.json"
    file_path.write_text("x")

    art = _make_artifact(task_id, expires_at=100.0)
    await insert_artifact(temp_db, art, file_path=str(file_path))

    before = metrics._artifact_gc_deleted.get("ok", 0)
    await artifact_gc.gc_run_once(
        now=time.time(), db=temp_db, artifacts_root=artifacts_root,
    )
    after = metrics._artifact_gc_deleted.get("ok", 0)
    assert after == before + 1
