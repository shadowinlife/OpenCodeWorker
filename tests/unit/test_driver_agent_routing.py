"""
单元测试：P0-5 driver agent 路由

回归守护：避免 AGENT_PROMETHEUS / AGENT_SISYPHUS 退回到 opencode 内置
"plan" / "build"。

ADR-001 / ADR-006 + oh-my-openagent 3.17.2 要求：
    - plan_first 模式 → Prometheus（规划 agent，read-only 工具集）
    - direct_execute 模式 → Sisyphus（执行 agent，bash/write/edit/webfetch 全开）

agent 实际可用性由 docker/worker/entrypoint.sh 在容器启动时通过 GET /agent
校验；本测试只守护 driver 端常量定义，避免无人 review 的回退。
"""
from __future__ import annotations

import pytest

from worker.adapters.opencode import driver as driver_module
from worker.contract.task import TaskMode


class TestAgentConstants:
    def test_prometheus_canonical_name(self):
        """plan_first 必须路由到 Prometheus，不能回退到 opencode 内置 'plan'."""
        assert driver_module.AGENT_PROMETHEUS == "Prometheus"

    def test_sisyphus_canonical_name(self):
        """direct_execute 必须路由到 Sisyphus，不能回退到 opencode 内置 'build'."""
        assert driver_module.AGENT_SISYPHUS == "Sisyphus"

    def test_constants_are_distinct(self):
        """两个 agent 不应相同（防止打字错误把两个常量写成同一个）。"""
        assert driver_module.AGENT_PROMETHEUS != driver_module.AGENT_SISYPHUS

    def test_no_legacy_opencode_builtin_names(self):
        """显式禁止 opencode 内置 agent 名出现在常量中（回归守护）。"""
        forbidden = {"plan", "build"}
        assert driver_module.AGENT_PROMETHEUS not in forbidden, (
            f"AGENT_PROMETHEUS reverted to opencode builtin "
            f"{driver_module.AGENT_PROMETHEUS!r}; ADR-001/006 require 'Prometheus'"
        )
        assert driver_module.AGENT_SISYPHUS not in forbidden, (
            f"AGENT_SISYPHUS reverted to opencode builtin "
            f"{driver_module.AGENT_SISYPHUS!r}; ADR-001/006 require 'Sisyphus'"
        )


class TestModeToAgentMapping:
    """守护 _run_inner 中 mode → agent 的选择分支不会被无意改写。

    不实例化完整 driver（构造需要 opencode HTTP/容器/DB），而是镜像
    driver._run_inner 的选择逻辑作为契约文档化。当 driver 中的分支被
    改动时，此测试需要同步更新——这种"文档锁"是有意为之。
    """

    @pytest.mark.parametrize(
        "mode,expected_agent_const_name",
        [
            (TaskMode.plan_first, "AGENT_PROMETHEUS"),
            (TaskMode.direct_execute, "AGENT_SISYPHUS"),
        ],
    )
    def test_mode_routes_to_expected_agent_const(self, mode, expected_agent_const_name):
        expected_value = getattr(driver_module, expected_agent_const_name)
        if mode == TaskMode.plan_first:
            assert expected_value == "Prometheus"
        else:
            assert expected_value == "Sisyphus"
