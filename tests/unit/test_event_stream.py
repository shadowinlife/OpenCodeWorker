"""
单元测试：worker.adapters.opencode.event_stream

覆盖场景：
    - normalize_opencode_event
        * message.part.delta → assistant_delta（各种嵌套结构）
        * message.part.updated tool-use → tool_call_started
        * message.part.updated tool-result → tool_call_finished
        * 不关心的事件类型 → None
    - extract_permission_request
        * session.status 含 pending permissions
        * message.part.updated 含 permissionId
        * 无权限请求 → None
        * permission_id 不以 "per" 开头 → None
    - is_session_idle
        * session.idle 事件 → True
        * session.status status=idle → True
        * 其他事件 → False
    - extract_diff
        * session.diff 含 diff 列表 → 列表
        * 其他事件 → None
"""
import pytest

from worker.adapters.opencode.event_stream import (
    extract_diff,
    extract_permission_request,
    is_session_idle,
    normalize_opencode_event,
)


# ─────────────────────────────────────────────────────────────────────────────
# normalize_opencode_event
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizeOpenCodeEvent:
    def test_delta_nested_part_structure(self):
        event = {
            "type": "message.part.delta",
            "payload": {"part": {"type": "text", "text": "Hello world"}},
        }
        result = normalize_opencode_event(event)
        assert result is not None
        assert result.kind == "assistant_delta"
        assert result.payload["content"] == "Hello world"

    def test_delta_flat_text_structure(self):
        """部分 opencode 版本直接在 payload 层面放 text。"""
        event = {
            "type": "message.part.delta",
            "payload": {"text": "Flat text"},
        }
        result = normalize_opencode_event(event)
        assert result is not None
        assert result.kind == "assistant_delta"
        assert result.payload["content"] == "Flat text"

    def test_delta_empty_text_returns_none(self):
        event = {
            "type": "message.part.delta",
            "payload": {"part": {"type": "text", "text": ""}},
        }
        result = normalize_opencode_event(event)
        assert result is None

    def test_tool_use_part(self):
        event = {
            "type": "message.part.updated",
            "payload": {
                "part": {
                    "type": "tool-use",
                    "toolName": "bash",
                    "input": {"cmd": "ls"},
                    "toolUseId": "tu-001",
                }
            },
        }
        result = normalize_opencode_event(event)
        assert result is not None
        assert result.kind == "tool_call_started"
        assert result.payload["tool"] == "bash"
        assert result.payload["args"] == {"cmd": "ls"}
        assert result.payload["tool_use_id"] == "tu-001"

    def test_tool_result_part_string_content(self):
        event = {
            "type": "message.part.updated",
            "payload": {
                "part": {
                    "type": "tool-result",
                    "toolUseId": "tu-001",
                    "content": "exit code 0",
                    "isError": False,
                }
            },
        }
        result = normalize_opencode_event(event)
        assert result is not None
        assert result.kind == "tool_call_finished"
        assert result.payload["result"] == "exit code 0"
        assert result.payload["is_error"] is False

    def test_tool_result_part_list_content(self):
        """content 为 [{"type": "text", "text": "..."}] 列表结构。"""
        event = {
            "type": "message.part.updated",
            "payload": {
                "part": {
                    "type": "tool-result",
                    "toolUseId": "tu-002",
                    "content": [{"type": "text", "text": "output line 1"}, {"type": "text", "text": "line 2"}],
                    "isError": False,
                }
            },
        }
        result = normalize_opencode_event(event)
        assert result is not None
        assert result.kind == "tool_call_finished"
        assert "output line 1" in result.payload["result"]
        assert "line 2" in result.payload["result"]

    def test_server_heartbeat_returns_none(self):
        event = {"type": "server.heartbeat", "payload": {}}
        assert normalize_opencode_event(event) is None

    def test_session_idle_returns_none(self):
        """session.idle 是 driver 直接消费的信号，归一化层不产出 Worker 事件。"""
        event = {"type": "session.idle", "payload": {"sessionID": "ses-abc"}}
        assert normalize_opencode_event(event) is None

    def test_unknown_event_type_returns_none(self):
        event = {"type": "completely.unknown", "payload": {}}
        assert normalize_opencode_event(event) is None

    def test_message_part_updated_text_part_returns_none(self):
        """text part 不是工具调用，不产出事件。"""
        event = {
            "type": "message.part.updated",
            "payload": {"part": {"type": "text", "text": "some text"}},
        }
        assert normalize_opencode_event(event) is None


# ─────────────────────────────────────────────────────────────────────────────
# extract_permission_request
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractPermissionRequest:
    def test_session_status_with_permissions(self):
        event = {
            "type": "session.status",
            "payload": {
                "status": "busy",
                "permissions": [
                    {
                        "id": "per-abc123",
                        "tool": "bash",
                        "description": "Run shell command",
                        "input": {"cmd": "rm -rf /tmp/work"},
                        "title": "Confirm shell",
                    }
                ],
            },
        }
        result = extract_permission_request(event)
        assert result is not None
        assert result["permission_id"] == "per-abc123"
        assert result["tool"] == "bash"
        assert result["description"] == "Run shell command"
        assert result["args"] == {"cmd": "rm -rf /tmp/work"}

    def test_session_status_empty_permissions(self):
        event = {
            "type": "session.status",
            "payload": {"status": "busy", "permissions": []},
        }
        assert extract_permission_request(event) is None

    def test_session_status_no_permissions_key(self):
        event = {"type": "session.status", "payload": {"status": "idle"}}
        assert extract_permission_request(event) is None

    def test_message_part_updated_with_permission_id(self):
        event = {
            "type": "message.part.updated",
            "payload": {
                "part": {
                    "type": "tool-use",
                    "permissionId": "per-xyz789",
                    "toolName": "write_file",
                    "input": {"path": "/etc/passwd"},
                    "title": "Write file",
                    "message": "About to write to /etc/passwd",
                }
            },
        }
        result = extract_permission_request(event)
        assert result is not None
        assert result["permission_id"] == "per-xyz789"
        assert result["tool"] == "write_file"

    def test_permission_id_not_starting_with_per_rejected(self):
        """permission_id 不以 'per' 开头时不应被识别为权限请求。"""
        event = {
            "type": "session.status",
            "payload": {
                "permissions": [
                    {"id": "tool-123", "tool": "bash", "description": "x"}
                ]
            },
        }
        assert extract_permission_request(event) is None

    def test_heartbeat_event_returns_none(self):
        event = {"type": "server.heartbeat", "payload": {}}
        assert extract_permission_request(event) is None

    def test_non_dict_payload_returns_none(self):
        event = {"type": "session.status", "payload": None}
        assert extract_permission_request(event) is None


# ─────────────────────────────────────────────────────────────────────────────
# is_session_idle
# ─────────────────────────────────────────────────────────────────────────────

class TestIsSessionIdle:
    def test_session_idle_event(self):
        event = {"type": "session.idle", "payload": {"sessionID": "ses-001"}}
        assert is_session_idle(event) is True

    def test_session_status_idle(self):
        event = {"type": "session.status", "payload": {"status": "idle"}}
        assert is_session_idle(event) is True

    def test_session_status_busy(self):
        event = {"type": "session.status", "payload": {"status": "busy"}}
        assert is_session_idle(event) is False

    def test_server_heartbeat_not_idle(self):
        event = {"type": "server.heartbeat", "payload": {}}
        assert is_session_idle(event) is False

    def test_unknown_type_not_idle(self):
        event = {"type": "message.part.delta", "payload": {"part": {"text": "hi"}}}
        assert is_session_idle(event) is False


# ─────────────────────────────────────────────────────────────────────────────
# extract_diff
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractDiff:
    def test_session_diff_with_list(self):
        diff_data = [
            {"path": "src/main.py", "added": 10, "removed": 2},
        ]
        event = {
            "type": "session.diff",
            "payload": {"diff": diff_data},
        }
        result = extract_diff(event)
        assert result == diff_data

    def test_session_diff_empty_list(self):
        event = {"type": "session.diff", "payload": {"diff": []}}
        assert extract_diff(event) == []

    def test_non_diff_event_returns_none(self):
        event = {"type": "session.idle", "payload": {}}
        assert extract_diff(event) is None

    def test_session_diff_missing_key(self):
        event = {"type": "session.diff", "payload": {}}
        assert extract_diff(event) is None

    def test_session_diff_non_list_value_returns_none(self):
        event = {"type": "session.diff", "payload": {"diff": "not-a-list"}}
        assert extract_diff(event) is None
