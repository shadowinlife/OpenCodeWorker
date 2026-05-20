"""
单元测试：W2-2 ConversationsWriter

覆盖：
    - 初始注入消息回放（initial_user_message / initial_assistant_message）
    - assistant_delta 多片段 coalesce 为单条 assistant 消息
    - tool_call_started / tool_call_finished 写入独立消息 + 字段截断
    - decision_received 写入 system 消息
    - JSONL well-formed（逐行可 parse）
    - slug 决议：callback (sync / async) → 校验通过 / 不合规 / 抛错；fallback
    - 敏感信息正则脱敏（API key、China ID、Bearer header）
    - 单条 content 超长截断
    - 总条数超限触发 sentinel 截断
    - 路径落在 artifacts_dir/{task_id}/conversations/
    - 工厂注册可用
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from worker.adapters.opencode.interceptors import (
    build_interceptor,
    list_factories,
)
from worker.adapters.opencode.interceptors.conversations import (
    ConversationsWriter,
    scrub,
)
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


def _evt(task_id: str, kind: str, payload: dict, *, raw_type: str = "x",
         received_at: float = 0.0) -> InterceptorEvent:
    return InterceptorEvent(
        task_id=task_id, session_id="s1",
        normalized_kind=kind, normalized_payload=payload,
        raw_type=raw_type, raw_payload={}, received_at=received_at,
    )


# ── 工厂注册 ─────────────────────────────────────────────────────────────


def test_factory_registered():
    assert "conversations" in list_factories()
    instance = build_interceptor("conversations")
    assert isinstance(instance, ConversationsWriter)


# ── 基础消息流水线 ─────────────────────────────────────────────────────────


async def test_initial_user_message_is_first_record(patched_artifacts_dir):
    w = ConversationsWriter()
    await w.on_event(_evt("t-001", "initial_user_message",
                          {"role": "user", "content": "hello"}, received_at=1.0))
    await w.on_terminal(TerminalSignal("t-001", "s1", "completed", None, 2.0))
    art = await w.flush()
    assert art is not None
    lines = Path(art.local_path).read_text().splitlines()
    msgs = [json.loads(l) for l in lines]
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "hello"


async def test_assistant_deltas_are_coalesced(patched_artifacts_dir):
    w = ConversationsWriter()
    tid = "t-002"
    for i, chunk in enumerate(["hi ", "there", "!"]):
        await w.on_event(_evt(tid, "assistant_delta",
                              {"content": chunk}, received_at=float(i)))
    # tool call 触发 flush
    await w.on_event(_evt(tid, "tool_call_started",
                          {"tool": "read_file", "args": {"path": "x"}, "tool_use_id": "u1"},
                          received_at=10.0))
    await w.on_terminal(TerminalSignal(tid, "s1", "completed", None, 11.0))
    art = await w.flush()
    msgs = [json.loads(l) for l in Path(art.local_path).read_text().splitlines()]
    # 第一条应是 coalesce 后的 assistant
    assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["content"] == "hi there!"
    # 然后是 tool_call
    tool_calls = [m for m in msgs if m["role"] == "tool_call"]
    assert len(tool_calls) == 1
    assert "read_file" in tool_calls[0]["content"]
    assert tool_calls[0]["tool"] == "read_file"


async def test_tool_call_finished_recorded(patched_artifacts_dir):
    w = ConversationsWriter()
    tid = "t-003"
    await w.on_event(_evt(tid, "tool_call_finished",
                          {"result": "ok", "tool_use_id": "u1", "is_error": False},
                          received_at=1.0))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 2.0))
    art = await w.flush()
    msgs = [json.loads(l) for l in Path(art.local_path).read_text().splitlines()]
    assert any(m["role"] == "tool_result" and m["content"] == "ok" for m in msgs)


async def test_decision_received_recorded(patched_artifacts_dir):
    w = ConversationsWriter()
    tid = "t-004"
    await w.on_event(_evt(tid, "decision_received",
                          {"decision_id": "d1", "choice": "approve",
                           "auto_approved": False},
                          received_at=1.0))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 2.0))
    art = await w.flush()
    msgs = [json.loads(l) for l in Path(art.local_path).read_text().splitlines()]
    sys_msgs = [m for m in msgs if m["role"] == "system"]
    assert sys_msgs and sys_msgs[0]["choice"] == "approve"


# ── slug 决议 ──────────────────────────────────────────────────────────────


async def test_slug_fallback_when_no_callback(patched_artifacts_dir):
    w = ConversationsWriter()
    tid = "t-fallback-12345"
    await w.on_event(_evt(tid, "initial_user_message",
                          {"role": "user", "content": "x"}, received_at=0.0))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    assert art.metadata["slug_source"] == "fallback"
    # fallback 形如 untitled-<6chars>
    assert art.metadata["slug"].startswith("untitled-")
    assert Path(art.local_path).name.endswith(art.metadata["slug"] + ".jsonl")


async def test_slug_callback_sync(patched_artifacts_dir):
    cb_calls = []

    def cb(messages):
        cb_calls.append(len(messages))
        return "tune-pullback"

    w = ConversationsWriter(summarize_callback=cb)
    tid = "t-sync-cb"
    await w.on_event(_evt(tid, "initial_user_message",
                          {"role": "user", "content": "h"}, received_at=0.0))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    assert art.metadata["slug"] == "tune-pullback"
    assert art.metadata["slug_source"] == "callback"
    assert cb_calls == [1]


async def test_slug_callback_async(patched_artifacts_dir):
    async def cb(messages):
        return "Async-Slug-OK"  # 大小写 → lower 后仍合规

    w = ConversationsWriter(summarize_callback=cb)
    tid = "t-async-cb"
    await w.on_event(_evt(tid, "initial_user_message",
                          {"role": "user", "content": "h"}, received_at=0.0))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    assert art.metadata["slug"] == "async-slug-ok"


async def test_slug_callback_invalid_falls_back(patched_artifacts_dir):
    """Callback 返回不合规 slug → fallback。"""
    def cb(messages):
        return "Has Spaces"

    w = ConversationsWriter(summarize_callback=cb)
    tid = "t-bad-slug"
    await w.on_event(_evt(tid, "initial_user_message",
                          {"role": "user", "content": "h"}, received_at=0.0))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    assert art.metadata["slug_source"] == "callback-rejected"
    assert art.metadata["slug"].startswith("untitled-")


async def test_slug_callback_raises(patched_artifacts_dir):
    def cb(messages):
        raise RuntimeError("boom")

    w = ConversationsWriter(summarize_callback=cb)
    tid = "t-cb-throw"
    await w.on_event(_evt(tid, "initial_user_message",
                          {"role": "user", "content": "h"}, received_at=0.0))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    assert art.metadata["slug_source"] == "callback-failed"


# ── 脱敏 ───────────────────────────────────────────────────────────────────


def test_scrub_openai_key():
    out = scrub("my key sk-abcdefghijklmnopqrstuvwxyz1234567890 done")
    assert "sk-abcdefghij" not in out
    assert "<REDACTED:api_key>" in out


def test_scrub_china_id():
    # 18 位身份证（X 末尾）
    out = scrub("ID: 11010519491231002X here")
    assert "11010519491231002X" not in out
    assert "<REDACTED:id_number>" in out


def test_scrub_bearer():
    out = scrub("curl -H 'Authorization: Bearer ABCD1234567890XYZabcdefghij' x")
    assert "Bearer ABCD" not in out
    assert "Bearer <REDACTED>" in out


def test_scrub_idempotent():
    s = "sk-abcdefghijklmnopqrstuvwxyz1234567890"
    once = scrub(s)
    twice = scrub(once)
    assert once == twice


async def test_message_content_is_scrubbed(patched_artifacts_dir):
    w = ConversationsWriter()
    tid = "t-scrub"
    secret = "sk-abcdefghijklmnopqrstuvwxyz1234567890"
    await w.on_event(_evt(tid, "initial_user_message",
                          {"role": "user", "content": f"my key is {secret}"},
                          received_at=0.0))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    raw = Path(art.local_path).read_text()
    assert secret not in raw
    assert "<REDACTED:api_key>" in raw


# ── 截断 ───────────────────────────────────────────────────────────────────


async def test_long_content_is_truncated(patched_artifacts_dir):
    w = ConversationsWriter(max_content_chars=64)
    tid = "t-truncate"
    huge = "x" * 1000
    await w.on_event(_evt(tid, "initial_user_message",
                          {"role": "user", "content": huge}, received_at=0.0))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    msgs = [json.loads(l) for l in Path(art.local_path).read_text().splitlines()]
    assert len(msgs[0]["content"]) == 64
    assert msgs[0]["truncated"] == "[truncated]"
    assert art.metadata["truncated_count"] == 1


async def test_too_many_messages_get_sentinel(patched_artifacts_dir):
    w = ConversationsWriter(max_messages=5)
    tid = "t-many"
    for i in range(20):
        await w.on_event(_evt(tid, "initial_user_message",
                              {"role": "user", "content": f"m{i}"},
                              received_at=float(i)))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 99.0))
    art = await w.flush()
    msgs = [json.loads(l) for l in Path(art.local_path).read_text().splitlines()]
    assert len(msgs) == 5
    sentinels = [m for m in msgs if m.get("kind") == "messages_truncated"]
    assert len(sentinels) == 1
    assert sentinels[0]["dropped"] > 0


# ── 路径与 artifact 元数据 ───────────────────────────────────────────────


async def test_output_path_is_under_artifacts_dir(patched_artifacts_dir):
    w = ConversationsWriter()
    tid = "t-path-check"
    await w.on_event(_evt(tid, "initial_user_message",
                          {"role": "user", "content": "h"}, received_at=0.0))
    await w.on_terminal(TerminalSignal(tid, None, "completed", None, 1.0))
    art = await w.flush()
    expected_root = patched_artifacts_dir / tid / "conversations"
    target = Path(art.local_path).resolve()
    expected_root_resolved = expected_root.resolve()
    target.relative_to(expected_root_resolved)  # 抛 ValueError 即测试失败
    assert art.filename.endswith(".jsonl")
    assert art.metadata["conversations_path"] == str(target)
    assert art.metadata["subtype"] == "conversations"
    assert art.metadata["message_count"] == 1
    assert art.artifact_type == "custom"


async def test_flush_returns_none_when_empty(patched_artifacts_dir):
    """没有任何事件进来 → flush 返回 None，不创建空文件。"""
    w = ConversationsWriter()
    await w.on_terminal(TerminalSignal("t-empty", None, "completed", None, 1.0))
    art = await w.flush()
    assert art is None


async def test_terminal_status_recorded_in_metadata(patched_artifacts_dir):
    w = ConversationsWriter()
    tid = "t-aborted"
    await w.on_event(_evt(tid, "initial_user_message",
                          {"role": "user", "content": "h"}, received_at=0.0))
    await w.on_terminal(TerminalSignal(tid, None, "aborted",
                                       "user_requested", 1.0))
    art = await w.flush()
    assert art.metadata["terminal_status"] == "aborted"
