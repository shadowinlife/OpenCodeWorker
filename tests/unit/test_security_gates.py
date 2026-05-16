"""
单元测试：P0-4 / P0-8 安全门

P0-4：orchestrator 在 workspace.kind="local" 且 WORKER_ALLOW_HOST_MOUNT=false 时拒绝执行
P0-8：artifact 下载路径必须落在 settings.artifacts_dir 内（防止路径穿越）
"""
from __future__ import annotations

from pathlib import Path

import pytest

from worker.config import Settings
from worker.contract.task import TaskMode, TaskRequest, WorkspaceSpec


# ---------------------------------------------------------------------------
# P0-4：local workspace 安全门
# ---------------------------------------------------------------------------


def test_settings_default_disallows_host_mount(monkeypatch):
    """默认配置下 allow_host_mount=False，不允许 host bind mount。"""
    monkeypatch.setenv("WORKER_BEARER_TOKEN", "x" * 32)
    s = Settings()
    assert s.allow_host_mount is False


def test_settings_explicit_enable_host_mount(monkeypatch):
    """WORKER_ALLOW_HOST_MOUNT=true 时显式打开。"""
    monkeypatch.setenv("WORKER_BEARER_TOKEN", "x" * 32)
    monkeypatch.setenv("WORKER_ALLOW_HOST_MOUNT", "true")
    s = Settings()
    assert s.allow_host_mount is True


async def test_orchestrator_rejects_local_kind_when_disabled(monkeypatch, tmp_path):
    """workspace.kind=local + allow_host_mount=False → orchestrator 抛 PermissionError。

    跑 run_task 全流程开销太大且依赖 docker；这里直接验证 _run_inner 等价的
    安全分支：从 orchestrator 模块取出 run_task 并构造最小路径。
    """
    from worker.orchestrator import orchestrator as orch
    from worker.storage import db as db_module
    from worker.storage.repo import insert_task

    # 隔离 settings
    monkeypatch.setenv("WORKER_BEARER_TOKEN", "x" * 32)
    monkeypatch.setenv("WORKER_ALLOW_HOST_MOUNT", "false")
    monkeypatch.setenv("WORKER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKER_DB_PATH", str(tmp_path / "data" / "worker.db"))
    from worker.config import get_settings
    get_settings.cache_clear()

    # 隔离 DB
    db_file = tmp_path / "worker.db"
    await db_module.init_db(db_file)
    try:
        db = await db_module.get_db()
        # 构造 local workspace 任务
        req = TaskRequest(
            mode=TaskMode.direct_execute,
            messages=[],
            workspace=WorkspaceSpec(kind="local", local_path="/tmp/some_repo"),
        )
        resp = await insert_task(db, req)

        with pytest.raises(PermissionError) as exc_info:
            await orch.run_task(resp.task_id)
        assert "local" in str(exc_info.value)
        assert "WORKER_ALLOW_HOST_MOUNT" in str(exc_info.value)
    finally:
        await db_module.close_db()
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# P0-8：artifact 下载路径穿越防护
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel_path, should_pass",
    [
        ("task-abc/changes.diff.json", True),
        ("task-abc/sub/transcript.json", True),
        ("../../etc/passwd", False),
        ("../another-task/leak.json", False),
    ],
)
def test_artifact_path_constraint_logic(tmp_path: Path, rel_path: str, should_pass: bool):
    """复现 routes.download_artifact 的路径校验逻辑：
    Path(file_path).resolve().relative_to(artifacts_root) 失败即拒绝。
    """
    artifacts_root = (tmp_path / "data" / "artifacts").resolve()
    artifacts_root.mkdir(parents=True)
    # 在 artifacts_root 之外建一个文件供恶意路径指向
    (tmp_path / "etc").mkdir(exist_ok=True)
    (tmp_path / "etc" / "passwd").write_text("secret")

    candidate = (artifacts_root / rel_path).resolve()

    try:
        candidate.relative_to(artifacts_root)
        within = True
    except ValueError:
        within = False

    assert within is should_pass
