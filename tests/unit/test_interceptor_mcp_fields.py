"""
单元测试：W2-4 McpFieldRecorder

覆盖：
    - 工厂注册可用
    - 默认正则提取 mcp_name (tool 名形如 "foo.bar.tool" → mcp_name="foo")
    - args top-level keys 聚合为 required_input_fields
    - raw_payload.part.metadata.read_fields[] 聚合为 required_output_fields
    - 多次同 tool 调用 → input/output 字段并集，call_count 自增
    - 自定义 mcp_name_pattern 生效
    - 不匹配 pattern 的 tool → 不计入
    - 同一 tool_use_id 重复 finished 事件 → 只统计一次（幂等）
    - read_fields 非 list / 缺失 → 视为空
    - flush 写 mcp_field_summary.json 并返回 InterceptorArtifact
    - 无任何聚合 → flush 返回 None
    - artifact 落在 artifacts_dir/<task_id>/ 下
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
from worker.adapters.opencode.interceptors.mcp_fields import McpFieldRecorder
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


# ── 工厂注册 ─────────────────────────────────────────────────────────────


def test_factory_registered():
    assert "mcp-fields" in list_factories()
    instance = build_interceptor("mcp-fields")
    assert isinstance(instance, McpFieldRecorder)
    assert instance.name == "mcp-fields"


def test_invalid_pattern_raises():
    with pytest.raises(ValueError):
        McpFieldRecorder(mcp_name_pattern="(")


# ── 默认聚合 ─────────────────────────────────────────────────────────────


async def test_aggregate_single_tool(patched_artifacts_dir):
    w = McpFieldRecorder()
    tid = "t-single"
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "foo-mcp.read",
        "args": {"path": "/x", "lines": 5},
        "tool_use_id": "u1",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }, raw_payload={"part": {"metadata": {"read_fields": ["price", "vol"]}}}))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    assert art is not None

    summary = json.loads(Path(art.local_path).read_text())
    assert summary["task_id"] == tid
    assert summary["tool_count"] == 1
    tool = summary["tools"][0]
    assert tool["mcp_name"] == "foo-mcp"
    assert tool["tool_name"] == "foo-mcp.read"
    assert tool["call_count"] == 1
    assert tool["required_input_fields"] == ["lines", "path"]
    assert tool["required_output_fields"] == ["price", "vol"]


async def test_aggregate_field_union_across_calls(patched_artifacts_dir):
    """多次调用同一 tool → input/output 字段并集；call_count 累计。"""
    w = McpFieldRecorder()
    tid = "t-union"

    # 第一次调用：input keys = {a,b}, output read_fields=[x]
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "mm.tt", "args": {"a": 1, "b": 2}, "tool_use_id": "u1",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }, raw_payload={"part": {"metadata": {"read_fields": ["x"]}}}))

    # 第二次调用：input keys = {b,c}, output read_fields=[y]
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "mm.tt", "args": {"b": 9, "c": 3}, "tool_use_id": "u2",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u2", "result": "ok", "is_error": False,
    }, raw_payload={"part": {"metadata": {"read_fields": ["y"]}}}))

    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    summary = json.loads(Path(art.local_path).read_text())
    tool = summary["tools"][0]
    assert tool["call_count"] == 2
    assert tool["required_input_fields"] == ["a", "b", "c"]
    assert tool["required_output_fields"] == ["x", "y"]


async def test_multi_tool_sorted_output(patched_artifacts_dir):
    w = McpFieldRecorder()
    tid = "t-multi"
    for i, (uid, tname) in enumerate([
        ("u1", "zeta.read"),
        ("u2", "alpha.write"),
        ("u3", "alpha.read"),
    ]):
        await w.on_event(_evt(tid, "tool_call_started", {
            "tool": tname, "args": {"k": "v"}, "tool_use_id": uid,
        }, received_at=float(i)))
        await w.on_event(_evt(tid, "tool_call_finished", {
            "tool_use_id": uid, "result": "ok", "is_error": False,
        }, received_at=float(i) + 0.1))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    summary = json.loads(Path(art.local_path).read_text())
    keys = [(t["mcp_name"], t["tool_name"]) for t in summary["tools"]]
    assert keys == [
        ("alpha", "alpha.read"),
        ("alpha", "alpha.write"),
        ("zeta", "zeta.read"),
    ]


# ── 自定义 pattern ───────────────────────────────────────────────────────


async def test_custom_mcp_name_pattern(patched_artifacts_dir):
    """自定义正则：'mcp__<name>__<tool>' 取中段。"""
    w = McpFieldRecorder(mcp_name_pattern=r"^mcp__([a-z][a-z0-9-]+)__")
    tid = "t-custom"
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "mcp__quant__fetch",
        "args": {"symbol": "0001"}, "tool_use_id": "u1",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    summary = json.loads(Path(art.local_path).read_text())
    assert summary["tools"][0]["mcp_name"] == "quant"
    assert summary["tools"][0]["tool_name"] == "mcp__quant__fetch"


async def test_non_matching_tool_skipped(patched_artifacts_dir):
    """tool 名不含 '.' → 默认正则不匹配 → 不计入。"""
    w = McpFieldRecorder()
    tid = "t-skip"
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "read_file", "args": {"path": "/x"}, "tool_use_id": "u1",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    assert await w.flush() is None


# ── 幂等 ──────────────────────────────────────────────────────────────────


async def test_idempotent_same_tool_use_id(patched_artifacts_dir):
    """同一 tool_use_id 即使 finished 事件重复也只统计一次。"""
    w = McpFieldRecorder()
    tid = "t-idem"
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "mm.tt", "args": {"a": 1}, "tool_use_id": "u1",
    }))
    # 重复两次 finished
    for _ in range(2):
        await w.on_event(_evt(tid, "tool_call_finished", {
            "tool_use_id": "u1", "result": "ok", "is_error": False,
        }, raw_payload={"part": {"metadata": {"read_fields": ["x"]}}}))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    summary = json.loads(Path(art.local_path).read_text())
    assert summary["tools"][0]["call_count"] == 1
    assert summary["tools"][0]["required_output_fields"] == ["x"]


async def test_finished_without_started_ignored(patched_artifacts_dir):
    """没有配对的 started → 直接忽略，不抛错。"""
    w = McpFieldRecorder()
    tid = "t-orphan"
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u-unknown", "result": "ok", "is_error": False,
    }))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    assert await w.flush() is None


# ── read_fields 解析容错 ─────────────────────────────────────────────────


async def test_read_fields_missing_yields_empty_output(patched_artifacts_dir):
    w = McpFieldRecorder()
    tid = "t-no-rf"
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "mm.tt", "args": {"a": 1}, "tool_use_id": "u1",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }))  # no part.metadata at all
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    summary = json.loads(Path(art.local_path).read_text())
    assert summary["tools"][0]["required_output_fields"] == []


async def test_read_fields_wrong_type_ignored(patched_artifacts_dir):
    w = McpFieldRecorder()
    tid = "t-bad-rf"
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "mm.tt", "args": {"a": 1}, "tool_use_id": "u1",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }, raw_payload={"part": {"metadata": {"read_fields": "not-a-list"}}}))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    summary = json.loads(Path(art.local_path).read_text())
    assert summary["tools"][0]["required_output_fields"] == []


async def test_read_fields_non_string_elements_filtered(patched_artifacts_dir):
    w = McpFieldRecorder()
    tid = "t-mixed-rf"
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "mm.tt", "args": {"a": 1}, "tool_use_id": "u1",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }, raw_payload={"part": {"metadata": {
        "read_fields": ["good", "", None, 42, "also"],
    }}}))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    summary = json.loads(Path(art.local_path).read_text())
    assert summary["tools"][0]["required_output_fields"] == ["also", "good"]


async def test_custom_read_fields_key(patched_artifacts_dir):
    w = McpFieldRecorder(read_fields_key="output_used_keys")
    tid = "t-custom-key"
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "mm.tt", "args": {"a": 1}, "tool_use_id": "u1",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }, raw_payload={"part": {"metadata": {
        "read_fields": ["should-be-ignored"],
        "output_used_keys": ["picked"],
    }}}))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    summary = json.loads(Path(art.local_path).read_text())
    assert summary["tools"][0]["required_output_fields"] == ["picked"]


# ── flush 行为 ───────────────────────────────────────────────────────────


async def test_flush_returns_none_when_no_calls(patched_artifacts_dir):
    w = McpFieldRecorder()
    await w.on_terminal(TerminalSignal("t-empty", None, "completed", None, 1.0))
    assert await w.flush() is None


async def test_flush_records_terminal_status(patched_artifacts_dir):
    w = McpFieldRecorder()
    tid = "t-status"
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "mm.tt", "args": {"a": 1}, "tool_use_id": "u1",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }))
    await w.on_terminal(TerminalSignal(tid, None, "aborted",
                                       "user_requested", 1.0))
    art = await w.flush()
    assert art.metadata["terminal_status"] == "aborted"
    summary = json.loads(Path(art.local_path).read_text())
    assert summary["terminal_status"] == "aborted"


async def test_artifact_path_under_artifacts_dir(
    patched_artifacts_dir,
):
    w = McpFieldRecorder()
    tid = "t-path"
    await w.on_event(_evt(tid, "tool_call_started", {
        "tool": "mm.tt", "args": {"a": 1}, "tool_use_id": "u1",
    }))
    await w.on_event(_evt(tid, "tool_call_finished", {
        "tool_use_id": "u1", "result": "ok", "is_error": False,
    }))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    expected_root = (patched_artifacts_dir / tid).resolve()
    target = Path(art.local_path).resolve()
    target.relative_to(expected_root)
    assert target.name == "mcp_field_summary.json"
    assert art.filename == "mcp_field_summary.json"
    assert art.artifact_type == "custom"
    assert art.metadata["subtype"] == "mcp_field_summary"
    assert art.metadata["tool_count"] == 1
