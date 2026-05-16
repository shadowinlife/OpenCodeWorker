"""
单元测试：P0-6 / P0-7 终态异常 + 终态事件契约

覆盖场景：
    - TaskTimedOutError 字段（timeout_sec / message）
    - TaskAbortedError 字段（reason / decision_id / message）
    - TaskEventKind.task_timed_out 已注册
    - TERMINAL_EVENT_KINDS 包含全部四个终态事件
"""
import pytest

from worker.contract.event import TERMINAL_EVENT_KINDS, TaskEventKind
from worker.contract.exceptions import TaskAbortedError, TaskTimedOutError


class TestTaskTimedOutError:
    def test_default_message(self):
        exc = TaskTimedOutError(timeout_sec=120.5)
        assert exc.timeout_sec == 120.5
        assert "120.5" in str(exc)

    def test_custom_message(self):
        exc = TaskTimedOutError(timeout_sec=60, message="custom")
        assert exc.timeout_sec == 60
        assert str(exc) == "custom"

    def test_is_exception(self):
        with pytest.raises(TaskTimedOutError):
            raise TaskTimedOutError(timeout_sec=10)


class TestTaskAbortedError:
    def test_default_reason(self):
        exc = TaskAbortedError()
        assert exc.reason == "system"
        assert exc.decision_id is None
        assert "system" in str(exc)

    def test_custom_fields(self):
        exc = TaskAbortedError(
            reason="hitl_timeout",
            message="timeout reached",
            decision_id="dec-123",
        )
        assert exc.reason == "hitl_timeout"
        assert exc.decision_id == "dec-123"
        assert str(exc) == "timeout reached"

    @pytest.mark.parametrize(
        "reason",
        [
            "user_requested",
            "hitl_timeout",
            "plan_rejected",
            "permission_rejected",
            "system",
        ],
    )
    def test_canonical_reasons(self, reason):
        exc = TaskAbortedError(reason=reason)
        assert exc.reason == reason


class TestTaskEventKindTerminals:
    def test_task_timed_out_registered(self):
        assert TaskEventKind.task_timed_out.value == "task_timed_out"

    def test_terminal_event_kinds_contains_all_four(self):
        assert TaskEventKind.task_completed in TERMINAL_EVENT_KINDS
        assert TaskEventKind.task_failed in TERMINAL_EVENT_KINDS
        assert TaskEventKind.task_aborted in TERMINAL_EVENT_KINDS
        assert TaskEventKind.task_timed_out in TERMINAL_EVENT_KINDS

    def test_terminal_event_kinds_size(self):
        assert len(TERMINAL_EVENT_KINDS) == 4

    def test_non_terminal_events_excluded(self):
        for kind in (
            TaskEventKind.task_created,
            TaskEventKind.task_queued,
            TaskEventKind.task_started,
            TaskEventKind.heartbeat,
            TaskEventKind.hitl_timeout,
            TaskEventKind.mode_escalation_suggested,
        ):
            assert kind not in TERMINAL_EVENT_KINDS
