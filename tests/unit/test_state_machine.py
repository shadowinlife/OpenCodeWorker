"""
单元测试：contract/task.py state machine 流转规则

覆盖场景：
    - TaskStatus 覆盖全部 14 种状态值
    - TERMINAL_STATUSES 集合正确包含终态
    - TaskMode 枚举值正确
    - 非终态状态不在 TERMINAL_STATUSES 中
"""
import pytest

from worker.contract.task import TaskMode, TaskStatus, TERMINAL_STATUSES


class TestTaskStatus:
    def test_all_statuses_defined(self):
        expected = {
            "pending", "queued", "preparing_workspace", "starting_container",
            "starting_opencode", "planning", "awaiting_human", "revising",
            "executing", "collecting_artifacts", "completed", "failed",
            "aborted", "timed_out",
        }
        actual = {s.value for s in TaskStatus}
        assert actual == expected

    def test_terminal_statuses_are_subset(self):
        for status in TERMINAL_STATUSES:
            assert isinstance(status, TaskStatus)

    def test_terminal_statuses_contain_completed_failed_aborted_timedout(self):
        assert TaskStatus.completed in TERMINAL_STATUSES
        assert TaskStatus.failed in TERMINAL_STATUSES
        assert TaskStatus.aborted in TERMINAL_STATUSES
        assert TaskStatus.timed_out in TERMINAL_STATUSES

    def test_non_terminal_not_in_terminal(self):
        non_terminals = {
            TaskStatus.pending, TaskStatus.queued, TaskStatus.preparing_workspace,
            TaskStatus.starting_container, TaskStatus.starting_opencode,
            TaskStatus.planning, TaskStatus.awaiting_human, TaskStatus.revising,
            TaskStatus.executing, TaskStatus.collecting_artifacts,
        }
        for status in non_terminals:
            assert status not in TERMINAL_STATUSES, f"{status} should not be terminal"


class TestTaskMode:
    def test_modes_defined(self):
        assert TaskMode.plan_first.value == "plan_first"
        assert TaskMode.direct_execute.value == "direct_execute"
