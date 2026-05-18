"""
Docker 沙箱管理器：负责容器的完整生命周期。

职责范围：
    - 按 TaskRequest 规格启动隔离容器（安全参数、网络、资源限制）
    - 健康探测容器内 opencode serve
    - 向容器发送 stop 信号（abort 流程）
    - Reaper：扫描并清理孤儿容器（label 标记，Worker 重启时触发）
    - 容器结束后清理关联的临时目录和 volume

安全策略（对应 Phase 2 checklist）：
    - 非 root 用户（容器内 uid 1000）
    - --cap-drop ALL（不保留任何 Linux capabilities）
    - --security-opt no-new-privileges（禁止 setuid/setgid 提权）
    - --read-only + --tmpfs /tmp（根 FS 只读，/tmp 临时内存 FS）
    - --pids-limit（防 fork bomb）
    - --memory / --cpus（资源上限）
    - 自定义隔离 network（默认无 default route，仅可访问 broker）

容器标签约定（label）：
    worker.task_id:   任务 UUID
    worker.managed:   "true"（标记为 worker 托管容器，供 reaper 识别）
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import docker
import docker.errors
from docker.models.containers import Container

from worker.observability import metrics

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────────────

WORKER_LABEL = "worker.managed"
TASK_ID_LABEL = "worker.task_id"

# 容器内 opencode serve 监听的端口（固定，由入口脚本设置）
OPENCODE_CONTAINER_PORT = 4096

# 容器内工作区挂载目标路径
WORKSPACE_MOUNT_TARGET = "/workspace"

# 健康检查参数
HEALTH_CHECK_INTERVAL = 2.0   # 秒
HEALTH_CHECK_MAX_RETRIES = 30  # 最多等待 60 秒

# ──────────────────────────────────────────────────────────────────────────────
# 进程单例
# ──────────────────────────────────────────────────────────────────────────────

_docker_client: Optional[docker.DockerClient] = None


def get_docker_client() -> docker.DockerClient:
    """懒初始化 Docker 客户端（进程单例）。"""
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    return _docker_client


def close_docker_client() -> None:
    """关闭 Docker 客户端，在 Worker 进程退出时调用。"""
    global _docker_client
    if _docker_client is not None:
        try:
            _docker_client.close()
        except Exception:
            pass
        _docker_client = None


# ──────────────────────────────────────────────────────────────────────────────
# 容器规格（从 TaskRequest 提取并传入 start_container）
# ──────────────────────────────────────────────────────────────────────────────

class ContainerSpec:
    """启动容器所需的全部参数，与 TaskRequest 字段对应。"""

    def __init__(
        self,
        *,
        task_id: str,
        image: str,
        workspace_dir: Path,
        env: dict[str, str],
        opencode_port_host: int,
        cpu_limit: str = "2",          # docker --cpus（如 "1.5"）
        memory_limit: str = "4g",      # docker --memory（如 "2g"）
        pids_limit: int = 512,
        network_name: Optional[str] = None,
        broker_host: Optional[str] = None,
        broker_port: Optional[int] = None,
        timeout_sec: int = 1800,
        # dev 模式：local workspace 时以 root 运行、关闭只读 FS
        container_user: str = "1000:1000",
        read_only: bool = True,
    ) -> None:
        self.task_id = task_id
        self.image = image
        self.workspace_dir = workspace_dir
        self.env = env
        self.opencode_port_host = opencode_port_host
        self.cpu_limit = cpu_limit
        self.memory_limit = memory_limit
        self.pids_limit = pids_limit
        self.network_name = network_name
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.timeout_sec = timeout_sec
        self.container_user = container_user
        self.read_only = read_only


# ──────────────────────────────────────────────────────────────────────────────
# 核心 API
# ──────────────────────────────────────────────────────────────────────────────

async def start_container(spec: ContainerSpec) -> Container:
    """启动一个隔离沙箱容器，返回 docker Container 对象。

    容器安全参数（参见模块 docstring）：
        - 非 root（user="1000:1000"）
        - cap-drop ALL
        - no-new-privileges
        - read-only 根 FS + /tmp tmpfs
        - pids-limit / memory / cpus
        - 自定义隔离 network

    此函数在 asyncio 事件循环中通过 run_in_executor 调用阻塞的 Docker SDK，
    避免阻塞事件循环。

    P1-11：测量启动耗时（含 Docker SDK 阻塞调用 + 网络解析），写入
    `worker_container_start_ms` summary。
    """
    loop = asyncio.get_event_loop()
    t0 = time.monotonic()
    container = await loop.run_in_executor(None, _start_container_sync, spec)
    metrics.observe_container_start((time.monotonic() - t0) * 1000.0)
    return container


def _start_container_sync(spec: ContainerSpec) -> Container:
    """同步启动容器（在线程池中执行）。"""
    client = get_docker_client()

    labels = {
        WORKER_LABEL: "true",
        TASK_ID_LABEL: spec.task_id,
    }

    # 挂载工作区目录（绑定挂载，可读写）
    volumes = {
        str(spec.workspace_dir.resolve()): {
            "bind": WORKSPACE_MOUNT_TARGET,
            "mode": "rw",
        }
    }

    # --tmpfs /tmp:rw,noexec,nosuid,size=256m
    tmpfs = {"/tmp": "rw,noexec,nosuid,size=256m"}

    # 资源限制
    # docker SDK 使用字节数，memory_limit 字符串如 "4g" 需转换
    mem_bytes = _parse_memory(spec.memory_limit)

    # 安全选项
    security_opt = ["no-new-privileges:true"]

    # 额外 hosts（broker 主机名解析）
    extra_hosts: dict[str, str] = {}
    if spec.broker_host:
        extra_hosts["broker"] = spec.broker_host

    container_kwargs: dict[str, Any] = {
        "image": spec.image,
        "name": f"worker-task-{spec.task_id[:12]}",
        "detach": True,
        "labels": labels,
        "environment": spec.env,
        "volumes": volumes,
        "tmpfs": tmpfs,
        "read_only": spec.read_only,
        "user": spec.container_user,
        "cap_drop": ["ALL"],
        "security_opt": security_opt,
        "pids_limit": spec.pids_limit,
        "mem_limit": mem_bytes,
        "nano_cpus": int(float(spec.cpu_limit) * 1e9),
        "network": spec.network_name,
        "extra_hosts": extra_hosts,
        "restart_policy": {"Name": "no"},
        # 端口映射：宿主随机端口 → 容器 opencode_port
        # 注：HostIp="127.0.0.1" 迫使 Lima/Colima 创建 docker-proxy 进程，从而
        #   在 macOS 宿主机上实现端口转发（HostIp="" 时 Lima 使用 iptables DNAT 无法转发）
        "ports": {f"{OPENCODE_CONTAINER_PORT}/tcp": ("127.0.0.1", spec.opencode_port_host)},
    }

    logger.info(
        "starting container for task %s, image=%s, workspace=%s",
        spec.task_id,
        spec.image,
        spec.workspace_dir,
    )
    container: Container = client.containers.run(**container_kwargs)
    logger.info(
        "container started: id=%s name=%s",
        container.id[:12],
        container.name,
    )
    return container


async def stop_container(task_id: str, *, timeout: int = 10) -> None:
    """向任务关联容器发送 SIGTERM，等待最多 timeout 秒后 SIGKILL。

    若找不到容器（已退出或从未启动），静默返回。
    """
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _stop_container_sync, task_id, timeout)


def _stop_container_sync(task_id: str, timeout: int) -> None:
    client = get_docker_client()
    containers = client.containers.list(
        all=True,
        filters={"label": [f"{TASK_ID_LABEL}={task_id}"]},
    )
    if not containers:
        logger.debug("stop_container: no container found for task %s", task_id)
        return
    for c in containers:
        logger.info("stopping container %s for task %s", c.id[:12], task_id)
        try:
            c.stop(timeout=timeout)
        except docker.errors.APIError as exc:
            # 容器已停止时 Docker 返回 304，忽略
            logger.debug("stop container %s: %s", c.id[:12], exc)


async def remove_container(task_id: str, *, force: bool = False) -> None:
    """删除任务关联容器（已停止后调用）。"""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _remove_container_sync, task_id, force)


def _remove_container_sync(task_id: str, force: bool) -> None:
    client = get_docker_client()
    containers = client.containers.list(
        all=True,
        filters={"label": [f"{TASK_ID_LABEL}={task_id}"]},
    )
    for c in containers:
        logger.info("removing container %s (task=%s, force=%s)", c.id[:12], task_id, force)
        try:
            c.remove(force=force)
        except docker.errors.APIError as exc:
            logger.warning("remove container %s: %s", c.id[:12], exc)


async def get_container(task_id: str) -> Optional[Container]:
    """按 task_id label 查找容器（running 或 stopped）。"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_container_sync, task_id)


def _get_container_sync(task_id: str) -> Optional[Container]:
    client = get_docker_client()
    containers = client.containers.list(
        all=True,
        filters={"label": [f"{TASK_ID_LABEL}={task_id}"]},
    )
    return containers[0] if containers else None


# ──────────────────────────────────────────────────────────────────────────────
# 网络管理
# ──────────────────────────────────────────────────────────────────────────────

async def ensure_worker_network(network_name: str) -> None:
    """确保隔离 Docker network 存在，不存在时创建。

    使用 bridge 网络（internal=False），容器可访问外网（DashScope 等 API）及宿主端口映射。
    网络隔离由 seccomp/cap_drop 等容器安全策略保障，而非网络 internal 标志。
    """
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _ensure_network_sync, network_name)


def _ensure_network_sync(network_name: str) -> None:
    client = get_docker_client()
    try:
        existing = client.networks.get(network_name)
        # 若已存在的网络是 internal=True，删除重建（修复历史创建）
        if existing.attrs.get("Internal", False):
            logger.info("docker network %s is internal=True, removing to recreate", network_name)
            existing.remove()
            raise docker.errors.NotFound(network_name)
        logger.debug("docker network %s already exists", network_name)
    except docker.errors.NotFound:
        client.networks.create(
            name=network_name,
            driver="bridge",
            internal=False,  # 允许容器访问外网（DashScope API 等），端口映射由 Lima 转发
            labels={WORKER_LABEL: "true"},
        )
        logger.info("created docker network: %s (internal=False)", network_name)


# ──────────────────────────────────────────────────────────────────────────────
# Reaper：清理孤儿容器
# ──────────────────────────────────────────────────────────────────────────────

async def reap_orphaned_containers() -> list[str]:
    """清理 Worker 管理的孤儿容器（已停止或 exited 状态）。

    在 Worker 进程启动时调用（FastAPI lifespan startup），
    清理上次 Worker 崩溃遗留的容器。

    Returns:
        被清理的 task_id 列表。
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _reap_orphaned_sync)


def _reap_orphaned_sync() -> list[str]:
    """同步版 reaper（在线程池执行）。"""
    client = get_docker_client()
    # 查找所有 worker 托管容器（包含已停止的）
    orphans = client.containers.list(
        all=True,
        filters={
            "label": [f"{WORKER_LABEL}=true"],
            "status": ["exited", "dead"],
        },
    )
    reaped: list[str] = []
    for c in orphans:
        task_id = c.labels.get(TASK_ID_LABEL, "unknown")
        logger.info(
            "reaper: removing exited container %s (task=%s)", c.id[:12], task_id
        )
        try:
            c.remove()
            reaped.append(task_id)
        except docker.errors.APIError as exc:
            logger.warning("reaper: failed to remove %s: %s", c.id[:12], exc)
    if reaped:
        logger.info("reaper: removed %d orphaned containers: %s", len(reaped), reaped)
    return reaped


# ──────────────────────────────────────────────────────────────────────────────
# 健康探测
# ──────────────────────────────────────────────────────────────────────────────

async def wait_for_opencode_health(
    host: str,
    port: int,
    *,
    password: str,
    max_retries: int = HEALTH_CHECK_MAX_RETRIES,
    interval: float = HEALTH_CHECK_INTERVAL,
) -> None:
    """轮询容器内 opencode /global/health 直到返回 200。

    使用 httpx 发起 Basic Auth 请求（user=opencode, password=<password>）。

    Raises:
        RuntimeError: max_retries 次内未成功。
    """
    import httpx

    url = f"http://{host}:{port}/global/health"
    auth = ("opencode", password)

    async with httpx.AsyncClient(timeout=3.0) as client:
        for attempt in range(1, max_retries + 1):
            try:
                resp = await client.get(url, auth=auth)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("healthy"):
                        logger.info(
                            "opencode health OK at %s (attempt %d): %s",
                            url,
                            attempt,
                            data,
                        )
                        return
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
                logger.debug(
                    "opencode health attempt %d/%d: %s", attempt, max_retries, exc
                )
            await asyncio.sleep(interval)

    raise RuntimeError(
        f"opencode did not become healthy after {max_retries} attempts "
        f"(interval={interval}s) at {url}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────────────────────────────────────────

def _parse_memory(value: str) -> int:
    """将人类可读内存字符串转换为字节数。

    支持：4g, 4G, 4Gi, 4GiB, 512m, 512M, 512Mi, 1024k, 1024K, 1024Ki
    """
    value = value.strip()
    # IEC 二进制前缀（Gi/Mi/Ki，与 g/m/k 同义均按 1024 进制）
    if value.endswith(("GiB", "GIB")):
        return int(float(value[:-3]) * 1024 ** 3)
    if value.endswith(("MiB", "MIB")):
        return int(float(value[:-3]) * 1024 ** 2)
    if value.endswith(("KiB", "KIB")):
        return int(float(value[:-3]) * 1024)
    if value.endswith(("Gi", "GI")):
        return int(float(value[:-2]) * 1024 ** 3)
    if value.endswith(("Mi", "MI")):
        return int(float(value[:-2]) * 1024 ** 2)
    if value.endswith(("Ki", "KI")):
        return int(float(value[:-2]) * 1024)
    if value.endswith(("g", "G")):
        return int(float(value[:-1]) * 1024 ** 3)
    if value.endswith(("m", "M")):
        return int(float(value[:-1]) * 1024 ** 2)
    if value.endswith(("k", "K")):
        return int(float(value[:-1]) * 1024)
    return int(value)
