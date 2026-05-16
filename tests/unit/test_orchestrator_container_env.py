from __future__ import annotations

import json

from worker.config import Settings
from worker.contract.task import Message, OpencodeProfile, TaskMode, TaskRequest
from worker.orchestrator.orchestrator import _build_container_env


def test_build_container_env_registers_oh_my_plugin_and_provider_config():
    request = TaskRequest(
        mode=TaskMode.plan_first,
        messages=[Message(role="user", content="inspect repo")],
        opencode_profile=OpencodeProfile(
            model="anthropic/claude-opus-4-5",
            providers=["openai"],
            provider_extra_config={
                "openai": {
                    "options": {
                        "baseURL": "https://example.invalid/v1",
                    }
                }
            },
        ),
    )

    env = _build_container_env(
        task_id="task-123",
        request=request,
        settings=Settings(bearer_token="test-token"),
    )

    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])

    assert config["plugin"] == ["oh-my-openagent@latest"]
    assert config["model"] == "anthropic/claude-opus-4-5"
    assert config["provider"]["openai"]["options"]["baseURL"] == "https://example.invalid/v1"
    assert config["provider"]["openai"]["options"]["apiKey"] == "{env:OPENAI_API_KEY}"