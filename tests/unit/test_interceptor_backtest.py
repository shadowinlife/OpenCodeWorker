"""
单元测试：W2-3 BacktestInterceptor

覆盖：
    - 工厂注册可用
    - tool 名匹配 pattern → 复制 run_dir 到 backtests/{ISO8601}-{label}/
    - tool 名不匹配 pattern → 不复制
    - is_error=True → 不复制
    - 缺失 run_dir / args 非法 → 不复制
    - 源目录不存在 → 不复制（仅日志）
    - 幂等：同一源路径出现两次仅复制一次
    - 默认 label=iter-N 自增；override 不消耗计数
    - override label 通过 raw_payload.part.metadata.backtest_label
    - override 不合规 → 回退 auto + 计数
    - flush 写 backtests/index.json 并返回 InterceptorArtifact
    - 无任何复制 → flush 返回 None（不产生空 artifact）
    - 相对路径需 workspace_root 才能解析
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

from worker.adapters.opencode.interceptors import (
    build_interceptor,
    list_factories,
)
from worker.adapters.opencode.interceptors.backtest import BacktestInterceptor
from worker.adapters.opencode.interceptors.types import (
    InterceptorEvent,
    TerminalSignal,
)


@pytest.fixture
def patched_artifacts_dir(tmp_path, monkeypatch):
    from worker import config as config_module

    data_dir = tmp_path / "data"
    art_dir = data_dir / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    settings = config_module.get_settings()
    monkeypatch.setattr(settings, "data_dir", data_dir)
    return art_dir


def _evt(
    task_id: str,
    kind: str,
    payload: dict,
    *,
    raw_payload: Optional[dict[str, Any]] = None,
    received_at: float = 0.0,
) -> InterceptorEvent:
    return InterceptorEvent(
        task_id=task_id, session_id="s1",
        normalized_kind=kind, normalized_payload=payload,
        raw_type="x", raw_payload=raw_payload or {},
        received_at=received_at,
    )


def _make_source(tmp_path: Path, name: str = "run-a") -> Path:
    src = tmp_path / "runs" / name
    src.mkdir(parents=True, exist_ok=True)
    (src / "metrics.json").write_text('{"sharpe": 1.42}')
    (src / "trades.csv").write_text("date,symbol\n2026-05-01,000001\n")
    return src


# ── 工厂注册 ─────────────────────────────────────────────────────────────


def test_factory_registered():
    assert "backtest" in list_factories()
    instance = build_interceptor("backtest")
    assert isinstance(instance, BacktestInterceptor)
    assert instance.name == "backtest"


# ── 匹配 + 复制 ──────────────────────────────────────────────────────────


async def test_match_and_copy(patched_artifacts_dir, tmp_path):
    src = _make_source(tmp_path)
    w = BacktestInterceptor(tool_pattern="*.backtest")
    tid = "t-match"

    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "vibe-trading.backtest",
        "args": {"run_dir": str(src)},
        "tool_use_id": "u1",
    }, received_at=1.0))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }, received_at=2.0))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 3.0))
    art = await w.flush()

    assert art is not None
    backtests_dir = patched_artifacts_dir / tid / "backtests"
    # 一个 ISO 子目录 + 一个 index.json
    subdirs = [p for p in backtests_dir.iterdir() if p.is_dir()]
    assert len(subdirs) == 1
    assert subdirs[0].name.endswith("-iter-1")
    assert (subdirs[0] / "metrics.json").read_text() == '{"sharpe": 1.42}'
    assert (subdirs[0] / "trades.csv").exists()

    index = json.loads(Path(art.local_path).read_text())
    assert index["count"] == 1
    assert index["backtests"][0]["label"] == "iter-1"
    assert index["backtests"][0]["label_source"] == "auto"
    assert index["backtests"][0]["source"] == str(src.resolve())
    assert index["backtests"][0]["dest_relpath"].startswith("backtests/")
    assert art.metadata["count"] == 1
    assert art.metadata["subtype"] == "backtests"
    assert art.artifact_type == "custom"


async def test_pattern_mismatch_does_not_copy(patched_artifacts_dir, tmp_path):
    src = _make_source(tmp_path)
    w = BacktestInterceptor(tool_pattern="*.backtest")
    tid = "t-mismatch"

    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "read_file", "args": {"run_dir": str(src)},
        "tool_use_id": "u1",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    assert art is None
    assert not (patched_artifacts_dir / tid / "backtests").exists()


async def test_is_error_skipped(patched_artifacts_dir, tmp_path):
    src = _make_source(tmp_path)
    w = BacktestInterceptor()
    tid = "t-error"
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "vibe-trading.backtest",
        "args": {"run_dir": str(src)}, "tool_use_id": "u1",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "boom", "is_error": True,
    }))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    assert art is None


async def test_missing_run_dir_skipped(patched_artifacts_dir):
    w = BacktestInterceptor()
    tid = "t-no-rd"
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "x.backtest", "args": {"other": "v"}, "tool_use_id": "u1",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    assert await w.flush() is None


async def test_missing_source_dir_skipped(patched_artifacts_dir, tmp_path):
    w = BacktestInterceptor()
    tid = "t-missing"
    bogus = tmp_path / "does-not-exist"
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "x.backtest", "args": {"run_dir": str(bogus)},
        "tool_use_id": "u1",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    assert await w.flush() is None


# ── 幂等性 ────────────────────────────────────────────────────────────────


async def test_same_source_copied_once(patched_artifacts_dir, tmp_path):
    src = _make_source(tmp_path)
    w = BacktestInterceptor()
    tid = "t-idem"
    # 两次调用，相同 run_dir
    for i, uid in enumerate(["u1", "u2"]):
        await w.on_event(_evt(tid, "tool_call_started", {
            "tool": "x.backtest", "args": {"run_dir": str(src)},
            "tool_use_id": uid,
        }, received_at=float(i)))
        await w.on_event(_evt(tid, "tool_call_finished", {
            "tool_use_id": uid, "result": "ok", "is_error": False,
        }, received_at=float(i) + 0.1))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 99.0))
    art = await w.flush()
    assert art is not None
    backtests_dir = patched_artifacts_dir / tid / "backtests"
    subdirs = [p for p in backtests_dir.iterdir() if p.is_dir()]
    assert len(subdirs) == 1
    index = json.loads(Path(art.local_path).read_text())
    assert index["count"] == 1


# ── label 决议 ───────────────────────────────────────────────────────────


async def test_auto_label_increments(patched_artifacts_dir, tmp_path):
    src1 = _make_source(tmp_path, "run-a")
    src2 = _make_source(tmp_path, "run-b")
    w = BacktestInterceptor()
    tid = "t-inc"

    for i, (uid, src) in enumerate([("u1", src1), ("u2", src2)]):
        await w.on_event(_evt(tid, "tool_call_started", {
            "tool": "x.backtest", "args": {"run_dir": str(src)},
            "tool_use_id": uid,
        }, received_at=float(i)))
        await w.on_event(_evt(tid, "tool_call_finished", {
            "tool_use_id": uid, "result": "ok", "is_error": False,
        }, received_at=float(i) + 0.5))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 99.0))
    art = await w.flush()
    index = json.loads(Path(art.local_path).read_text())
    labels = [r["label"] for r in index["backtests"]]
    assert labels == ["iter-1", "iter-2"]
    assert all(r["label_source"] == "auto" for r in index["backtests"])


async def test_override_label_via_metadata(patched_artifacts_dir, tmp_path):
    src = _make_source(tmp_path)
    w = BacktestInterceptor()
    tid = "t-override"
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "x.backtest", "args": {"run_dir": str(src)},
        "tool_use_id": "u1",
    }))
    # raw_payload mimics opencode message.part.updated with metadata
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }, raw_payload={"part": {"metadata": {"backtest_label": "baseline"}}}))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    index = json.loads(Path(art.local_path).read_text())
    assert index["backtests"][0]["label"] == "baseline"
    assert index["backtests"][0]["label_source"] == "override"


async def test_override_does_not_consume_auto_counter(
    patched_artifacts_dir, tmp_path,
):
    """override 那次不算 auto；后续 auto 仍从 iter-1 开始。"""
    src1 = _make_source(tmp_path, "run-a")
    src2 = _make_source(tmp_path, "run-b")
    w = BacktestInterceptor()
    tid = "t-ovr-counter"

    # 第一次 override
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "x.backtest", "args": {"run_dir": str(src1)},
        "tool_use_id": "u1",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }, raw_payload={"part": {"metadata": {"backtest_label": "baseline"}}}))

    # 第二次 auto
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "x.backtest", "args": {"run_dir": str(src2)},
        "tool_use_id": "u2",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u2", "result": "ok", "is_error": False,
    }))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    index = json.loads(Path(art.local_path).read_text())
    labels = [r["label"] for r in index["backtests"]]
    assert labels == ["baseline", "iter-1"]


async def test_invalid_override_falls_back_to_auto(
    patched_artifacts_dir, tmp_path,
):
    src = _make_source(tmp_path)
    w = BacktestInterceptor()
    tid = "t-bad-label"
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "x.backtest", "args": {"run_dir": str(src)},
        "tool_use_id": "u1",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }, raw_payload={"part": {"metadata": {"backtest_label": "Has Spaces!"}}}))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    index = json.loads(Path(art.local_path).read_text())
    assert index["backtests"][0]["label"] == "iter-1"
    assert index["backtests"][0]["label_source"] == "auto"


# ── 路径解析 ─────────────────────────────────────────────────────────────


async def test_relative_path_without_workspace_root_skipped(
    patched_artifacts_dir,
):
    w = BacktestInterceptor()
    tid = "t-rel"
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "x.backtest", "args": {"run_dir": "./runs/local"},
        "tool_use_id": "u1",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    assert await w.flush() is None


async def test_relative_path_resolved_via_workspace_root(
    patched_artifacts_dir, tmp_path,
):
    workspace = tmp_path / "ws"
    src_rel = "runs/run-x"
    src = workspace / src_rel
    src.mkdir(parents=True, exist_ok=True)
    (src / "metrics.json").write_text("{}")

    w = BacktestInterceptor(workspace_root=workspace)
    tid = "t-rel-ok"
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "x.backtest", "args": {"run_dir": src_rel},
        "tool_use_id": "u1",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    assert art is not None
    index = json.loads(Path(art.local_path).read_text())
    assert index["backtests"][0]["source"] == str(src.resolve())


# ── flush 行为 ───────────────────────────────────────────────────────────


async def test_flush_returns_none_when_no_copies(patched_artifacts_dir):
    w = BacktestInterceptor()
    await w.on_terminal(TerminalSignal("t-empty", None, "completed", None, 1.0))
    assert await w.flush() is None


async def test_flush_records_terminal_status(patched_artifacts_dir, tmp_path):
    src = _make_source(tmp_path)
    w = BacktestInterceptor()
    tid = "t-status"
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "x.backtest", "args": {"run_dir": str(src)},
        "tool_use_id": "u1",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }))
    await w.on_terminal(TerminalSignal(tid, None, "aborted",
                                       "user_requested", 1.0))
    art = await w.flush()
    assert art.metadata["terminal_status"] == "aborted"


async def test_artifact_path_under_artifacts_dir(
    patched_artifacts_dir, tmp_path,
):
    src = _make_source(tmp_path)
    w = BacktestInterceptor()
    tid = "t-path"
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "x.backtest", "args": {"run_dir": str(src)},
        "tool_use_id": "u1",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    expected_root = (patched_artifacts_dir / tid / "backtests").resolve()
    target = Path(art.local_path).resolve()
    # 抛 ValueError 即测试失败
    target.relative_to(expected_root)
    assert target.name == "index.json"
    assert art.filename == "backtests/index.json"
