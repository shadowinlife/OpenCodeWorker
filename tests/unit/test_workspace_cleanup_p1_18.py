"""
单元测试：P1-18 git_subpath cleanup 残留修复

验证：orchestrator._cleanup 现在按 task_id 顶层目录整体删除工作区，
覆盖原本只删 subpath 留下顶层壳的场景。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from worker.orchestrator import orchestrator as orch


@pytest.fixture(autouse=True)
def _stub_sandbox_and_broker(monkeypatch):
    """所有 _cleanup 测试都不真启 Docker / broker；以 mock 替身吞掉调用。"""
    monkeypatch.setenv("WORKER_BEARER_TOKEN", "x" * 32)
    from worker.config import get_settings
    get_settings.cache_clear()

    async def _aok(*_a, **_kw):
        return None

    with patch.object(orch, "stop_container", _aok), \
         patch.object(orch, "remove_container", _aok):
        yield
    get_settings.cache_clear()


async def test_cleanup_removes_full_task_root_for_git_subpath(tmp_path: Path):
    """git_subpath 模式下，subpath 子目录与 task_id 顶层目录都应被删除。"""
    workspaces_base = tmp_path / "workspaces"
    task_id = "task-abc-123"
    task_root = workspaces_base / task_id
    subpath_dir = task_root / "src" / "module-a"
    subpath_dir.mkdir(parents=True)
    (subpath_dir / "file.txt").write_text("x")
    (task_root / ".git").mkdir()  # 模拟 git clone 留下的 .git 目录

    await orch._cleanup(
        task_id,
        workspace_kind="git",
        workspaces_base=workspaces_base,
    )

    assert not task_root.exists(), "task_id 顶层目录应被整体删除"
    # 父 workspaces 目录保留供其它任务使用
    assert workspaces_base.exists()


async def test_cleanup_removes_task_root_for_empty_kind(tmp_path: Path):
    workspaces_base = tmp_path / "workspaces"
    task_id = "task-empty-1"
    task_root = workspaces_base / task_id
    task_root.mkdir(parents=True)
    (task_root / "marker.txt").write_text("y")

    await orch._cleanup(
        task_id,
        workspace_kind="empty",
        workspaces_base=workspaces_base,
    )

    assert not task_root.exists()


async def test_cleanup_skips_local_kind(tmp_path: Path):
    """local 模式不应触碰宿主机 workspaces 目录（实际 task_root 也不存在）。"""
    workspaces_base = tmp_path / "workspaces"
    workspaces_base.mkdir()
    # local 模式下 prepare_workspace 不会创建 task_root，仍调 _cleanup 应不抛
    await orch._cleanup(
        "task-local-1",
        workspace_kind="local",
        workspaces_base=workspaces_base,
    )
    assert workspaces_base.exists()


async def test_cleanup_tolerates_missing_task_root(tmp_path: Path):
    """任务在 workspace 创建前就失败时，task_root 不存在；cleanup 应静默通过。"""
    workspaces_base = tmp_path / "workspaces"
    workspaces_base.mkdir()
    await orch._cleanup(
        "task-never-started",
        workspace_kind="git",
        workspaces_base=workspaces_base,
    )
