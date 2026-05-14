"""
任务 Orchestrator：驱动完整的任务生命周期。

职责范围（Phase 2）：
    1. 从 DB 读取 TaskRequest
    2. 调用 workspace/handler.py 准备工作区
    3. 构建容器规格（env 注入、broker policy、opencode 配置）
    4. 调用 sandbox/manager.py 启动容器
    5. 等待 opencode /global/health 就绪
    6. 更新 DB 状态到 starting_opencode（占位，Phase 3 opencode adapter 替换）
    7. Phase 2 stub：等待容器自然退出，收集 exit code
    8. 终态清理：停止/删除容器 + 清理 workspace + 移除 broker policy

Phase 3 集成点：
    此 Orchestrator 的 _drive_opencode() 方法是 Phase 3 的主扩展点。
    Phase 2 中该方法为 stub（等容器退出），Phase 3 会替换为真实的
    opencode HTTP adapter 调用（SSE 订阅 + session 管理 + HITL 路由）。

注册方式：
    from worker.orchestrator.orchestrator import run_task
    from worker.orchestrator.queue import set_executor
    set_executor(run_task)
"""
from __future__ import annotations

import json
import logging
import socket
import time
from pathlib import Path
from typing import Optional

from worker.config import get_settings
from worker.contract.event import TaskEventKind
from worker.contract.task import TaskMode, TaskRequest, TaskStatus
from worker.sandbox.manager import (
    ContainerSpec,
    ensure_worker_network,
    get_container,
    remove_container,
    reap_orphaned_containers,
    start_container,
    stop_container,
    wait_for_opencode_health,
)
from worker.storage.db import get_db
from worker.storage.repo import (
    insert_event,
    update_task_status,
)
from worker.workspace.handler import cleanup_workspace, prepare_workspace

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# 全局常量
# ──────────────────────────────────────────────────────────────────────────────

WORKER_DOCKER_NETWORK = "worker-sandbox-net"


# ──────────────────────────────────────────────────────────────────────────────
# 主入口（由 queue._run_one 调用）
# ──────────────────────────────────────────────────────────────────────────────

async def run_task(task_id: str) -> None:
    """完整执行一个任务的生命周期（Phase 2）。

    调用方（queue._run_one）已负责：
        - 进入 semaphore 槽位（并发控制）
        - 状态更新到 starting_container
        - 异常捕获 → task_failed 事件

    本函数负责：
        - preparing_workspace 状态及工作区准备
        - container 启动、健康检查
        - opencode 驱动（Phase 2 stub）
        - 终态写入（completed / failed）
        - 清理：container + workspace + broker policy

    若出现任何未捕获异常，都会由 queue._run_one 的 except 块写入 task_failed 事件。
    """
    settings = get_settings()
    db = await get_db()

    # ── 读取完整 TaskRequest ──────────────────────────────────────────────────
    request = await _load_request(db, task_id)
    if request is None:
        raise RuntimeError(f"TaskRequest not found for task_id={task_id}")

    workspace_dir: Optional[Path] = None

    try:
        # ── Step 1: 准备 workspace ────────────────────────────────────────────
        await update_task_status(db, task_id, TaskStatus.preparing_workspace)
        await insert_event(db, task_id, TaskEventKind.task_started,
                           {"phase": "preparing_workspace"})

        workspaces_base = settings.data_dir / "workspaces"
        workspace_dir = await prepare_workspace(
            task_id=task_id,
            base_dir=workspaces_base,
            kind=request.workspace.kind,
            tarball_url=request.workspace.tarball_url,
            tarball_inline_b64=request.workspace.tarball_inline_b64,
            git_url=request.workspace.git.url if request.workspace.git else None,
            git_sha=request.workspace.git.sha if request.workspace.git else None,
            git_subpath=request.workspace.git.subpath if request.workspace.git else None,
            local_path=request.workspace.local_path,
        )
        logger.info("task %s: workspace ready at %s", task_id, workspace_dir)

        # ── Step 2: 确保 Docker network 存在 ─────────────────────────────────
        await ensure_worker_network(WORKER_DOCKER_NETWORK)

        # ── Step 3: 构建 broker policy ────────────────────────────────────────
        if settings.broker_enabled:
            from broker.policy import set_task_policy
            allow_hosts = request.broker_policy.allow_egress_hosts if request.broker_policy else []
            set_task_policy(task_id, allow_hosts)
        else:
            logger.debug("task %s: broker disabled, skipping policy setup", task_id)

        # ── Step 4: 构建容器 env ──────────────────────────────────────────────
        container_env = _build_container_env(task_id, request, settings)

        # ── Step 5: 找一个可用宿主端口 ────────────────────────────────────────
        host_port = _find_free_port()

        # ── Step 6: 启动容器 ──────────────────────────────────────────────────
        await update_task_status(db, task_id, TaskStatus.starting_container)

        resource_limits = request.resource_limits
        # local workspace：以 root 运行、关闭只读 FS（开发/测试模式，用户已知悉安全风险）
        is_local = request.workspace.kind == "local"
        spec = ContainerSpec(
            task_id=task_id,
            image=settings.sandbox_image,
            workspace_dir=workspace_dir,
            env=container_env,
            opencode_port_host=host_port,
            cpu_limit=str(resource_limits.cpu) if resource_limits else "2",
            memory_limit=str(resource_limits.memory) if resource_limits else "4g",
            pids_limit=resource_limits.pids if resource_limits else 512,
            network_name=WORKER_DOCKER_NETWORK,
            broker_host=settings.broker_host if settings.broker_enabled else None,
            broker_port=settings.broker_port if settings.broker_enabled else None,
            timeout_sec=resource_limits.timeout_sec if resource_limits else 1800,
            container_user="0:0" if is_local else "1000:1000",
            read_only=not is_local,
        )
        container = await start_container(spec)

        # ── Step 7: 更新 DB container_id ─────────────────────────────────────
        await update_task_status(
            db, task_id, TaskStatus.starting_container,
            container_id=container.id,
        )
        await insert_event(db, task_id, TaskEventKind.container_started,
                           {"container_id": container.id[:12]})

        # ── Step 8: 等待 opencode 健康 ────────────────────────────────────────
        await update_task_status(db, task_id, TaskStatus.starting_opencode)
        opencode_password = container_env.get("OPENCODE_SERVER_PASSWORD", "")
        await wait_for_opencode_health(
            host="127.0.0.1",
            port=host_port,
            password=opencode_password,
        )
        await insert_event(db, task_id, TaskEventKind.opencode_ready,
                           {"port": host_port})

        # ── Step 9: 驱动 opencode（Phase 2 stub）────────────────────────────
        await _drive_opencode(task_id, request, container, host_port, container_env)

        # ── Step 10: 完成 ─────────────────────────────────────────────────────
        await update_task_status(db, task_id, TaskStatus.completed)
        await insert_event(db, task_id, TaskEventKind.task_completed)
        logger.info("task %s: completed", task_id)

    finally:
        # ── 清理：无论成功失败都执行 ──────────────────────────────────────────
        await _cleanup(task_id, workspace_dir, workspace_kind=request.workspace.kind)


# ──────────────────────────────────────────────────────────────────────────────
# Phase 3：opencode HTTP adapter 驱动
# ──────────────────────────────────────────────────────────────────────────────

async def _drive_opencode(
    task_id: str,
    request: TaskRequest,
    container,
    host_port: int,
    container_env: dict[str, str],
) -> None:
    """驱动容器内 opencode 执行任务（Phase 3 实现）。

    创建 OpenCodeDriver 实例并委托其完整生命周期：
        - SSE 订阅 /global/event
        - session 创建 + prompt_async（按 mode 路由 agent）
        - 权限 HITL（tool_permission DecisionRequest）
        - plan_first HITL 审批
        - artifact 收集（diff + transcript）

    超时或 abort 时抛出 RuntimeError，由 queue._run_one 写入 task_failed 事件。
    """
    from worker.adapters.opencode.driver import OpenCodeDriver
    from worker.storage.db import get_db

    db = await get_db()
    driver = OpenCodeDriver(
        task_id=task_id,
        request=request,
        host_port=host_port,
        container_env=container_env,
        db=db,
    )
    await driver.run()


# ──────────────────────────────────────────────────────────────────────────────
# 清理
# ──────────────────────────────────────────────────────────────────────────────

async def _cleanup(task_id: str, workspace_dir: Optional[Path], workspace_kind: str = "empty") -> None:
    """任务终态后清理容器 + workspace + broker policy。"""
    # 停止并删除容器
    try:
        await stop_container(task_id, timeout=10)
        await remove_container(task_id, force=True)
    except Exception as exc:
        logger.warning("task %s: container cleanup error: %s", task_id, exc)

    # 清理工作区（local 模式跳过，避免删除宿主机原始目录）
    if workspace_dir is not None and workspace_kind != "local":
        try:
            await cleanup_workspace(workspace_dir)
        except Exception as exc:
            logger.warning("task %s: workspace cleanup error: %s", task_id, exc)

    # 移除 broker policy（仅在 broker 启用时）
    settings = get_settings()
    if settings.broker_enabled:
        try:
            from broker.policy import remove_task_policy
            remove_task_policy(task_id)
        except Exception as exc:
            logger.warning("task %s: broker policy cleanup error: %s", task_id, exc)


# ──────────────────────────────────────────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────────────────────────────────────────

async def _load_request(db, task_id: str) -> Optional[TaskRequest]:
    """从 DB 读取任务的 TaskRequest（反序列化 request_json 列）。"""
    import aiosqlite

    async with db.execute(
        "SELECT request_json FROM tasks WHERE id = ?", (task_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return TaskRequest.model_validate_json(row["request_json"])


def _build_container_env(
    task_id: str,
    request: TaskRequest,
    settings,
) -> dict[str, str]:
    """构建注入容器的环境变量字典。

    包含：
        - OPENCODE_SERVER_PASSWORD（随机生成，每任务唯一）
        - OPENCODE_CONFIG_CONTENT（内联 JSON，注入 model/provider/permission）
        - OPENCODE_DISABLE_AUTOUPDATE=1
        - OPENCODE_PERMISSION（权限模板 JSON）
        - worker 侧 proxy 设置
        - TaskRequest.env_policy.extra_env（用户自定义 env）
        - provider API keys（从宿主 env 透传）
    """
    import os
    import secrets

    opencode_password = secrets.token_hex(16)

    # 构建 OPENCODE_CONFIG_CONTENT
    profile = request.opencode_profile
    config_content: dict = {
        "autoupdate": False,
    }
    if profile:
        if profile.model:
            config_content["model"] = profile.model
        # 构建 provider 块：先用 provider_extra_config 作为基础，再注入 apiKey
        providers_block: dict = {}
        # 合并 provider_extra_config（自定义 npm/name/options/models 等）
        if profile.provider_extra_config:
            import copy
            for p, extra in profile.provider_extra_config.items():
                providers_block[p] = copy.deepcopy(extra)
        # 为 providers 列表中声明的 provider 注入 apiKey（若有环境变量映射）
        for provider in (profile.providers or []):
            key_env_var = _provider_key_env_var(provider)
            if key_env_var:
                entry = providers_block.setdefault(provider, {})
                opts = entry.setdefault("options", {})
                # 仅在未明确设置 apiKey 时才注入
                if "apiKey" not in opts:
                    opts["apiKey"] = f"{{env:{key_env_var}}}"
        if providers_block:
            # opencode 配置 key 为 "provider"（单数）
            config_content["provider"] = providers_block

    # 权限模板
    permission_json = _build_permission_json(
        profile.permission_template if profile else None,
        profile.permission_overrides if profile else None,
    )

    env: dict[str, str] = {
        "OPENCODE_SERVER_PASSWORD": opencode_password,
        "OPENCODE_CONFIG_CONTENT": json.dumps(config_content),
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_PERMISSION": permission_json,
    }

    # broker proxy（仅当 broker_enabled=True 时注入，否则容器直连外网）
    if settings.broker_enabled:
        env["HTTP_PROXY"] = f"http://broker:{settings.broker_port}"
        env["HTTPS_PROXY"] = f"http://broker:{settings.broker_port}"
        # task_id 供 broker 白名单检查使用
        env["WORKER_TASK_ID"] = task_id

    # 透传 provider API keys（从 Worker 进程 env 复制到容器 env）
    if request.env_policy:
        for key_name in request.env_policy.provider_keys:
            val = os.environ.get(key_name)
            if val:
                env[key_name] = val
        # 用户自定义 extra_env（不允许覆盖 OPENCODE_* 系统变量）
        for k, v in (request.env_policy.extra_env or {}).items():
            if not k.startswith("OPENCODE_"):
                env[k] = v

    return env


def _provider_key_env_var(provider: str) -> Optional[str]:
    """将 provider 名称映射到 API key 环境变量名。"""
    mapping = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "alibaba": "ALIBABA_API_KEY",
        "alibaba-cn": "ALIBABA_CN_API_KEY",
        "alibabacloud": "DASHSCOPE_API_KEY",
        "dashscope": "DASHSCOPE_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "gemini": "GEMINI_API_KEY",
    }
    return mapping.get(provider.lower())


def _build_permission_json(
    template: Optional[str],
    overrides: Optional[dict],
) -> str:
    """根据权限模板 + overrides 构建 OPENCODE_PERMISSION JSON 字符串。"""
    # 内置默认模板
    templates: dict[str, dict] = {
        "plan_first_default": {
            "bash": "ask",
            "write": "ask",
            "edit": "ask",
            "webfetch": "ask",
            "external_directory": "deny",
        },
        "direct_execute_default": {
            "bash": "ask",
            "write": "allow",
            "edit": "allow",
            "webfetch": "ask",
            "external_directory": "deny",
        },
    }

    # 选择基础模板
    if template and template in templates:
        base = dict(templates[template])
    elif template == "custom":
        base = {}
    else:
        # 默认使用 plan_first_default
        base = dict(templates["plan_first_default"])

    # 合并 overrides（只允许合法值）
    VALID_VALUES = {"allow", "ask", "deny"}
    if overrides:
        for k, v in overrides.items():
            if v in VALID_VALUES:
                base[k] = v

    return json.dumps(base)


def _find_free_port() -> int:
    """在宿主上找一个空闲端口用于容器 port mapping。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
