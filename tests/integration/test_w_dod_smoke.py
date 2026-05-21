"""
W-DoD §3.3 端到端 smoke：feed 一个 fixture task 让三个内置拦截器同时落盘。

验收对象：W2 SSE Hooks 退出门 —— ``opencode_profile.interceptors`` 声明
``conversations`` / ``backtest`` / ``mcp-fields`` 三个内置工厂时，driver
应通过 ``build_interceptors_from_config`` 实例化、按事件流分发、终态时
全部 flush 并把产物登记到 DB + 落盘到 artifacts 目录的预期位置。

本测试不依赖真实 opencode 容器：直接复用 driver 的内部分发入口
``_dispatch_to_interceptors`` / ``_dispatch_terminal_and_flush`` 喂入
一段确定的、覆盖三类拦截器订阅事件的合成 SSE 序列，并断言：

    <artifacts_dir>/<task_id>/conversations/{ISO8601}-{slug}.jsonl
    <artifacts_dir>/<task_id>/backtests/index.json + 一个 ISO 子目录
    <artifacts_dir>/<task_id>/mcp_field_summary.json

DB 侧应有 3 条 artifact 行（均为 ``custom`` 类型）+ 3 条 ``artifact_ready``
事件，以及 driver 自己在终态分发期间不抛错。

设计依据：
    claudedocs/workflow_phase_x1_implementation_backlog.md §3.3
    docs/roadmap/opencode-worker.md §9.A（W-DoD 待启动 → 本测试关闭）
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from worker.adapters.opencode.driver import OpenCodeDriver
from worker.adapters.opencode.interceptors import (
    build_interceptors_from_config,
    list_factories,
)
from worker.contract.event import TaskEventKind
from worker.contract.task import (
    InterceptorConfig,
    Message,
    OpencodeProfile,
    TaskMode,
    TaskRequest,
    TaskStatus,
)
from worker.storage import db as db_module
from worker.storage.repo import get_events_after, insert_task, list_artifacts


# ── 公共 fixture ───────────────────────────────────────────────────────────


@pytest.fixture
async def temp_db(tmp_path: Path):
    """每个 case 独立 SQLite DB，避免跨用例污染。"""
    db_file = tmp_path / "w_dod_smoke.db"
    conn = await db_module.init_db(db_file)
    try:
        yield conn
    finally:
        await db_module.close_db()


@pytest.fixture
def patched_data_dir(tmp_path, monkeypatch):
    """把全局 Settings.data_dir 重定向到 tmp_path/data；artifacts_dir 自然派生。

    ``WORKER_BEARER_TOKEN`` 由项目级 ``tests/conftest.py`` 在收集期统一注入。
    """
    from worker import config as config_module

    data_dir = tmp_path / "data"
    art_dir = data_dir / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    settings = config_module.get_settings()
    monkeypatch.setattr(settings, "data_dir", data_dir)
    return art_dir


@pytest.fixture
def fixture_backtest_dir(tmp_path) -> Path:
    """合成 backtest 工具的 run_dir：放几个有内容的文件供 BacktestInterceptor 复制。"""
    run_dir = tmp_path / "runs" / "smoke-run-01"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "metrics.json").write_text(
        json.dumps({"sharpe": 1.42, "max_drawdown": -0.18}),
        encoding="utf-8",
    )
    (run_dir / "trades.csv").write_text(
        "date,symbol,side\n2026-05-01,000001,buy\n2026-05-02,000001,sell\n",
        encoding="utf-8",
    )
    return run_dir


# ── 测试主体 ───────────────────────────────────────────────────────────────


async def test_w_dod_smoke_all_three_interceptors_materialize(
    temp_db, patched_data_dir, fixture_backtest_dir,
):
    """W-DoD §3.3 acceptance：三拦截器声明 → driver 装配 → 全部落盘 + 登记。"""

    # 1. 三个内置工厂必须已注册（import 拦截器包即注册）
    for expected in ("conversations", "backtest", "mcp-fields"):
        assert expected in list_factories(), (
            f"内置工厂 {expected!r} 未注册；W2-{{2,3,4}} 的 import-time "
            "register_factory 可能被破坏"
        )

    # 2. 构造一个声明三拦截器的 TaskRequest 并持久化
    profile = OpencodeProfile(
        interceptors=[
            InterceptorConfig(
                name="conversations",
                options={"slug_prefix": "smoke"},
            ),
            InterceptorConfig(
                name="backtest",
                options={"tool_pattern": "*.backtest"},
            ),
            InterceptorConfig(
                name="mcp-fields",
                # 默认 ^([a-z][a-z0-9-]+)\. 即可同时匹配
                # `quant.fetch_kline` 和 `vibe-trading.backtest`
                options={},
            ),
        ],
    )
    req = TaskRequest(
        mode=TaskMode.direct_execute,
        messages=[Message(role="user", content="跑一次冒烟")],
        opencode_profile=profile,
    )
    resp = await insert_task(temp_db, req)
    task_id = resp.task_id

    # 3. 走 orchestrator 同款装配路径 —— 验证 build_interceptors_from_config
    #    既不静默丢配置，也不重复实例化
    interceptors = build_interceptors_from_config(profile.interceptors)
    assert [ic.name for ic in interceptors] == [
        "conversations", "backtest", "mcp-fields",
    ], "拦截器顺序/数量与声明不一致，build_interceptors_from_config 行为漂移"

    driver = OpenCodeDriver(
        task_id=task_id,
        request=req,
        host_port=4096,
        container_env={"OPENCODE_SERVER_PASSWORD": "smoke"},
        db=temp_db,
        interceptors=interceptors,
    )
    driver.session_id = "smoke-session"

    # 4. 喂入合成 SSE 事件序列；覆盖三类拦截器的订阅清单
    #    （顺序模拟一次真实 plan_first→executing→completed 流：
    #     注入历史 → LLM 分两段流式 → 一个 MCP 读 → 一段流式 → 一个 backtest 调用 → 决策 → 终态）

    # 4.1 driver 合成的 initial user message
    await driver._dispatch_to_interceptors(
        normalized_kind="initial_user_message",
        normalized_payload={"role": "user", "content": "请帮我跑一次冒烟测试"},
        raw_type="<synthesized:initial_user_message>",
    )

    # 4.2 LLM 流式增量（应 coalesce 成单条 assistant message）
    for chunk in ("分析中", "...完成第一段"):
        await driver._dispatch_to_interceptors(
            normalized_kind="assistant_delta",
            normalized_payload={"content": chunk},
            raw_type="message.part.delta",
            raw_payload={"part": {"text": chunk}},
        )

    # 4.3 第一个工具调用：MCP 读字段（quant.fetch_kline）—— 命中 mcp-fields；
    #     不匹配 backtest pattern *.backtest，所以不会被 BacktestInterceptor 复制
    await driver._dispatch_to_interceptors(
        normalized_kind="tool_call_started",
        normalized_payload={
            "tool": "quant.fetch_kline",
            "args": {"symbol": "000001", "start": "2026-01-01", "limit": 100},
            "tool_use_id": "u-mcp-1",
        },
        raw_type="message.part.updated",
    )
    await driver._dispatch_to_interceptors(
        normalized_kind="tool_call_finished",
        normalized_payload={
            "tool_use_id": "u-mcp-1",
            "result": [{"date": "2026-01-02", "close": 12.3}],
            "is_error": False,
        },
        raw_type="message.part.updated",
        # McpFieldRecorder 从 part.metadata.read_fields[] 读 output fields
        raw_payload={"part": {"metadata": {"read_fields": ["date", "close"]}}},
    )

    # 4.4 一段 LLM 文本
    await driver._dispatch_to_interceptors(
        normalized_kind="assistant_delta",
        normalized_payload={"content": "数据已获取，准备回测"},
        raw_type="message.part.delta",
    )

    # 4.5 第二个工具调用：backtest（vibe-trading.backtest）——
    #     同时命中 BacktestInterceptor（复制 run_dir） + McpFieldRecorder（聚合字段）
    backtest_args = {"run_dir": str(fixture_backtest_dir), "params": {"window": 20}}
    await driver._dispatch_to_interceptors(
        normalized_kind="tool_call_started",
        normalized_payload={
            "tool": "vibe-trading.backtest",
            "args": backtest_args,
            "tool_use_id": "u-bt-1",
        },
        raw_type="message.part.updated",
    )
    await driver._dispatch_to_interceptors(
        normalized_kind="tool_call_finished",
        normalized_payload={
            "tool_use_id": "u-bt-1",
            "result": "ok",
            "is_error": False,
        },
        raw_type="message.part.updated",
        # 同时声明 backtest_label override（走 override 路径）+ read_fields
        raw_payload={"part": {"metadata": {
            "backtest_label": "smoke-baseline",
            "read_fields": ["sharpe", "max_drawdown"],
        }}},
    )

    # 4.6 driver 合成的 decision_received（auto_approve 路径）
    await driver._dispatch_to_interceptors(
        normalized_kind="decision_received",
        normalized_payload={
            "decision_id": "d-1",
            "choice": "approve",
            "auto_approved": True,
        },
        raw_type="<synthesized:decision_received>",
    )

    # 5. 终态分发 + flush + 登记产物（driver 终态前调用的链路）
    await driver._dispatch_terminal_and_flush(TaskStatus.completed, None)

    # ── 断言 ──────────────────────────────────────────────────────────────

    # 6.1 ConversationsWriter 产物：jsonl 文件存在、行数符合预期、slug 用了 prefix
    conv_dir = patched_data_dir / task_id / "conversations"
    assert conv_dir.exists(), "conversations/ 目录未创建"
    jsonl_files = sorted(conv_dir.glob("*.jsonl"))
    assert len(jsonl_files) == 1, (
        f"conversations 目录预期 1 个 jsonl，实际 {len(jsonl_files)}: {jsonl_files}"
    )
    # fallback slug 形如 untitled-<6char>；本用例 slug_prefix=smoke → smoke-<6char>
    assert "-smoke-" in jsonl_files[0].name, (
        f"slug_prefix=smoke 未透传到文件名: {jsonl_files[0].name}"
    )
    rows = [
        json.loads(line)
        for line in jsonl_files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # 预期 rows：
    #   initial_user (1) + assistant coalesced (1) +
    #   tool_call_started (1) + tool_result (1) +
    #   assistant (1) + tool_call_started (1) + tool_result (1) +
    #   decision system entry (1) = 8
    assert len(rows) == 8, f"conversations jsonl 消息条数异常: {len(rows)}\n{rows}"
    roles = [r["role"] for r in rows]
    assert roles[0] == "user"
    assert roles.count("assistant") == 2  # 两段 LLM 输出
    assert roles.count("tool_call") == 2  # 两次工具发起
    assert roles.count("tool_result") == 2
    assert roles[-1] == "system"  # decision_received 写成 system 行
    assert rows[-1]["choice"] == "approve"
    assert rows[-1]["auto_approved"] is True

    # 6.2 BacktestInterceptor 产物：index.json + 一个 ISO 子目录（label=override）
    bt_dir = patched_data_dir / task_id / "backtests"
    assert bt_dir.exists(), "backtests/ 目录未创建"
    bt_index = bt_dir / "index.json"
    assert bt_index.exists(), "backtests/index.json 未生成"
    bt_summary = json.loads(bt_index.read_text(encoding="utf-8"))
    assert bt_summary["task_id"] == task_id
    assert bt_summary["terminal_status"] == "completed"
    assert bt_summary["count"] == 1
    record = bt_summary["backtests"][0]
    assert record["label"] == "smoke-baseline"
    assert record["label_source"] == "override"
    assert record["tool"] == "vibe-trading.backtest"
    assert record["source"] == str(fixture_backtest_dir.resolve())
    # 复制的文件应原样存在于 dest_relpath 子目录下
    copied_root = patched_data_dir / task_id / record["dest_relpath"]
    assert (copied_root / "metrics.json").read_text(encoding="utf-8") == (
        '{"sharpe": 1.42, "max_drawdown": -0.18}'
    )
    assert (copied_root / "trades.csv").exists()

    # 6.3 McpFieldRecorder 产物：mcp_field_summary.json 聚合两个 (mcp, tool)
    mcp_summary_path = patched_data_dir / task_id / "mcp_field_summary.json"
    assert mcp_summary_path.exists(), "mcp_field_summary.json 未生成"
    mcp_summary = json.loads(mcp_summary_path.read_text(encoding="utf-8"))
    assert mcp_summary["task_id"] == task_id
    assert mcp_summary["terminal_status"] == "completed"
    assert mcp_summary["tool_count"] == 2
    # 输出已按 (mcp_name, tool_name) 排序，便于稳定断言
    by_tool = {t["tool_name"]: t for t in mcp_summary["tools"]}
    assert set(by_tool.keys()) == {"quant.fetch_kline", "vibe-trading.backtest"}

    fetch = by_tool["quant.fetch_kline"]
    assert fetch["mcp_name"] == "quant"
    assert fetch["call_count"] == 1
    assert fetch["required_input_fields"] == ["limit", "start", "symbol"]
    assert fetch["required_output_fields"] == ["close", "date"]

    backtest = by_tool["vibe-trading.backtest"]
    assert backtest["mcp_name"] == "vibe-trading"
    assert backtest["call_count"] == 1
    assert backtest["required_input_fields"] == ["params", "run_dir"]
    assert backtest["required_output_fields"] == ["max_drawdown", "sharpe"]

    # 6.4 DB 侧：三条 artifact 行 + 三条 artifact_ready 事件
    artifacts = await list_artifacts(temp_db, task_id)
    assert len(artifacts) == 3, (
        f"预期 3 条 artifact 登记，实际 {len(artifacts)}: "
        f"{[a.filename for a in artifacts]}"
    )
    subtypes = {a.metadata.get("subtype") for a in artifacts}
    assert subtypes == {"conversations", "backtests", "mcp_field_summary"}, (
        f"artifact subtypes 不全：{subtypes}"
    )
    # 三个产物均走 ArtifactType.custom
    assert all(a.type.value == "custom" for a in artifacts), (
        f"非 custom 类型出现：{[a.type for a in artifacts]}"
    )
    # 每个 artifact 的 size 必须 > 0（说明 stat 拿到了真实文件）
    assert all(a.size > 0 for a in artifacts), (
        f"出现 size==0 的产物登记，可能 file_path 错位：{artifacts}"
    )

    events = await get_events_after(temp_db, task_id, after_cursor=0)
    artifact_ready_events = [
        e for e in events if e.kind == TaskEventKind.artifact_ready
    ]
    assert len(artifact_ready_events) == 3, (
        f"artifact_ready 事件数与产物数不符："
        f"{[e.payload.get('filename') for e in artifact_ready_events]}"
    )
    # 事件 payload 必须含 artifact_id / type / filename / metadata
    for e in artifact_ready_events:
        assert e.payload.get("artifact_id"), f"缺 artifact_id: {e.payload}"
        assert e.payload.get("type") == "custom"
        assert e.payload.get("filename")
        assert isinstance(e.payload.get("metadata"), dict)


async def test_w_dod_smoke_terminal_dispatch_isolated_from_main_flow(
    temp_db, patched_data_dir,
):
    """补充：即使一段事件序列里有产物，aborted 终态也应被 BacktestInterceptor
    + McpFieldRecorder 透传到 metadata.terminal_status；任何异常不传染 driver。

    这是 W-DoD §3.3 的隐含要求：终态语义（status / reason）必须忠实落到产物。
    """
    profile = OpencodeProfile(
        interceptors=[
            InterceptorConfig(name="conversations"),
            InterceptorConfig(name="mcp-fields"),
        ],
    )
    req = TaskRequest(
        mode=TaskMode.direct_execute,
        messages=[Message(role="user", content="aborted smoke")],
        opencode_profile=profile,
    )
    resp = await insert_task(temp_db, req)
    task_id = resp.task_id

    interceptors = build_interceptors_from_config(profile.interceptors)
    driver = OpenCodeDriver(
        task_id=task_id,
        request=req,
        host_port=4096,
        container_env={"OPENCODE_SERVER_PASSWORD": "smoke"},
        db=temp_db,
        interceptors=interceptors,
    )

    await driver._dispatch_to_interceptors(
        normalized_kind="initial_user_message",
        normalized_payload={"role": "user", "content": "abort soon"},
        raw_type="<synthesized:initial_user_message>",
    )
    await driver._dispatch_to_interceptors(
        normalized_kind="tool_call_started",
        normalized_payload={
            "tool": "quant.read", "args": {"k": "v"},
            "tool_use_id": "u-1",
        },
        raw_type="message.part.updated",
    )
    await driver._dispatch_to_interceptors(
        normalized_kind="tool_call_finished",
        normalized_payload={
            "tool_use_id": "u-1", "result": "ok", "is_error": False,
        },
        raw_type="message.part.updated",
        raw_payload={"part": {"metadata": {"read_fields": ["a"]}}},
    )

    # 用户主动中止（driver._abort_reason 来源之一）
    await driver._dispatch_terminal_and_flush(
        TaskStatus.aborted, "user_requested",
    )

    artifacts = await list_artifacts(temp_db, task_id)
    # conversations 必出（有 user + tool_call + tool_result）
    # mcp-fields 必出（有 1 个 (mcp, tool) 聚合）
    assert {a.metadata.get("subtype") for a in artifacts} == {
        "conversations", "mcp_field_summary",
    }
    for art in artifacts:
        assert art.metadata.get("terminal_status") == "aborted", (
            f"终态状态未透传到 {art.metadata.get('subtype')!r} 的 metadata: "
            f"{art.metadata}"
        )
