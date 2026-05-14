"""
Workspace 处理器：负责任务工作区的准备与清理。

职责范围：
    - tarball 模式：下载 .tar.gz 并安全解压（防 zip-slip、symlink 攻击）
    - git 模式：在 Worker 进程侧 clone（不在沙箱内，避免凭据泄露到容器）
    - empty 模式：直接创建空目录
    - 解包后产物挂载到容器（由 sandbox/manager.py 负责 volumes 参数）
    - 任务结束后清理临时目录（GC）

安全注意事项：
    - tarball 解压：所有 member path 必须 canonical 化，禁止 ".." 或绝对路径
    - symlink 解析：目标必须在工作区目录内部（白名单）
    - size 限制：解压后总大小 ≤ WORKSPACE_MAX_BYTES，防止磁盘耗尽
    - git clone：只允许 https:// 协议（禁止 file:// ssh:// 等）
    - inline base64：解码后 ≤ TARBALL_INLINE_MAX_BYTES

Phase 2 限制（未来 Phase 处理）：
    - git SSH 支持（需要 Worker 侧 SSH 密钥管理）
    - workspace diff 快照（Phase 3 artifact 收集）
    - 增量缓存（相同 tarball hash 时复用已解压目录）
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# 限制常量
# ──────────────────────────────────────────────────────────────────────────────

WORKSPACE_MAX_BYTES = 500 * 1024 * 1024   # 500 MB 解压后上限
TARBALL_INLINE_MAX_BYTES = 50 * 1024 * 1024  # 50 MB inline base64 解码后上限
TARBALL_DOWNLOAD_MAX_BYTES = 200 * 1024 * 1024  # 200 MB 远程下载上限
GIT_CLONE_TIMEOUT_SEC = 120


# ──────────────────────────────────────────────────────────────────────────────
# 公开接口
# ──────────────────────────────────────────────────────────────────────────────

async def prepare_workspace(
    *,
    task_id: str,
    base_dir: Path,
    kind: str,
    tarball_url: Optional[str] = None,
    tarball_inline_b64: Optional[str] = None,
    git_url: Optional[str] = None,
    git_sha: Optional[str] = None,
    git_subpath: Optional[str] = None,
    local_path: Optional[str] = None,
) -> Path:
    """准备任务工作区目录，返回挂载到容器的本地 Path。

    Args:
        task_id:    任务 UUID，用于目录命名（避免冲突）。
        base_dir:   工作区根目录（Settings.data_dir / "workspaces"）。
        kind:       "empty" | "tarball" | "git" | "local"
        tarball_url:   kind=tarball 时的远程 URL。
        tarball_inline_b64: kind=tarball 时的内联 base64 数据。
        git_url:    kind=git 时的仓库 URL（仅 https://）。
        git_sha:    kind=git 时的目标提交 SHA（完整 40 位）。
        git_subpath: kind=git 时仅使用的子目录（可选）。
        local_path: kind=local 时的宿主机绝对路径（直接 bind mount）。

    Returns:
        工作区绝对路径（容器将绑定挂载此目录到 /workspace）。

    Raises:
        ValueError:   参数非法（bad kind, bad URL, sha 格式错误等）。
        RuntimeError: 下载失败 / 解压失败 / clone 失败。
    """
    workspace_dir = base_dir / task_id
    workspace_dir.mkdir(parents=True, exist_ok=True)
    logger.info("preparing workspace for task %s: kind=%s dir=%s", task_id, kind, workspace_dir)

    if kind == "empty":
        # 空目录已创建，无需额外操作
        pass

    elif kind == "tarball":
        if tarball_url:
            tarball_bytes = await _download_tarball(tarball_url)
        elif tarball_inline_b64:
            tarball_bytes = _decode_inline_b64(tarball_inline_b64)
        else:
            raise ValueError("tarball kind requires tarball_url or tarball_inline_b64")
        await asyncio.get_event_loop().run_in_executor(
            None, _extract_tarball, tarball_bytes, workspace_dir
        )

    elif kind == "git":
        if not git_url or not git_sha:
            raise ValueError("git kind requires git_url and git_sha")
        _validate_git_url(git_url)
        _validate_git_sha(git_sha)
        await _git_clone(git_url, git_sha, workspace_dir)
        if git_subpath:
            # 把子目录内容提升到 workspace_dir 根
            subpath_dir = workspace_dir / git_subpath
            if not subpath_dir.is_dir():
                raise RuntimeError(
                    f"git subpath '{git_subpath}' not found in cloned repo"
                )
            # 不移动，直接返回 subpath 目录（容器挂载点会映射到此）
            return subpath_dir.resolve()

    elif kind == "local":
        # 直接使用宿主机目录（开发/测试模式，不做安全隔离，不清理）
        if not local_path:
            raise ValueError("local kind requires local_path")
        host_dir = Path(local_path)
        if not host_dir.is_dir():
            raise ValueError(f"local_path is not a directory: {local_path!r}")
        logger.info("task %s: using local workspace at %s (no isolation)", task_id, host_dir)
        return host_dir

    else:
        raise ValueError(f"unknown workspace kind: {kind!r}")

    return workspace_dir.resolve()

async def cleanup_workspace(workspace_dir: Path) -> None:
    """删除工作区目录（任务终态后调用）。

    对不存在的目录静默返回。
    """
    if not workspace_dir.exists():
        return
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, shutil.rmtree, str(workspace_dir), True)
    logger.info("workspace cleaned up: %s", workspace_dir)


# ──────────────────────────────────────────────────────────────────────────────
# tarball 处理
# ──────────────────────────────────────────────────────────────────────────────

async def _download_tarball(url: str) -> bytes:
    """下载远程 tarball，限制大小，超限则报错。"""
    logger.info("downloading tarball from %s", url)
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            chunks: list[bytes] = []
            downloaded = 0
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                downloaded += len(chunk)
                if downloaded > TARBALL_DOWNLOAD_MAX_BYTES:
                    raise RuntimeError(
                        f"tarball download exceeds limit "
                        f"({TARBALL_DOWNLOAD_MAX_BYTES // 1024 // 1024} MB): {url}"
                    )
                chunks.append(chunk)
    data = b"".join(chunks)
    logger.info("tarball downloaded: %d bytes from %s", len(data), url)
    return data


def _decode_inline_b64(b64_data: str) -> bytes:
    """解码内联 base64 tarball，限制大小。"""
    try:
        data = base64.b64decode(b64_data, validate=True)
    except Exception as exc:
        raise ValueError(f"invalid base64 tarball data: {exc}") from exc
    if len(data) > TARBALL_INLINE_MAX_BYTES:
        raise ValueError(
            f"inline tarball exceeds limit "
            f"({TARBALL_INLINE_MAX_BYTES // 1024 // 1024} MB)"
        )
    return data


def _extract_tarball(data: bytes, dest: Path) -> None:
    """安全解压 tarball 到目标目录。

    安全措施：
        1. 禁止绝对路径 member（以 / 开头）
        2. 禁止包含 ".." 的路径组件
        3. 解压后每个文件的 canonical 路径必须在 dest 内部
        4. 跳过 symlink member（防止 symlink 指向沙箱外）
        5. 累计解压字节数不超过 WORKSPACE_MAX_BYTES
    """
    dest_resolved = dest.resolve()
    total_bytes = 0

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        for member in tf.getmembers():
            # 1. 禁止绝对路径
            if os.path.isabs(member.name):
                logger.warning("tarball: skipping absolute path member: %s", member.name)
                continue

            # 2. 禁止 ".." 组件
            parts = Path(member.name).parts
            if any(p == ".." for p in parts):
                logger.warning("tarball: skipping path traversal member: %s", member.name)
                continue

            # 3. 跳过 symlink（防止解包后指向 dest 外部）
            if member.issym() or member.islnk():
                logger.warning("tarball: skipping symlink member: %s -> %s",
                               member.name, member.linkname)
                continue

            # 4. 计算目标绝对路径并验证在 dest 内
            target_path = (dest_resolved / member.name).resolve()
            try:
                target_path.relative_to(dest_resolved)
            except ValueError:
                logger.warning(
                    "tarball: skipping path escaping dest: %s -> %s",
                    member.name,
                    target_path,
                )
                continue

            # 5. 累计大小检查
            total_bytes += member.size
            if total_bytes > WORKSPACE_MAX_BYTES:
                raise RuntimeError(
                    f"tarball extraction exceeds size limit "
                    f"({WORKSPACE_MAX_BYTES // 1024 // 1024} MB)"
                )

            # 目录直接 makedirs，文件用 extractfile 流式写出
            if member.isdir():
                target_path.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target_path.parent.mkdir(parents=True, exist_ok=True)
                fobj = tf.extractfile(member)
                if fobj is not None:
                    with open(target_path, "wb") as out:
                        shutil.copyfileobj(fobj, out)

    logger.info(
        "tarball extracted: %d bytes to %s", total_bytes, dest
    )


# ──────────────────────────────────────────────────────────────────────────────
# git clone
# ──────────────────────────────────────────────────────────────────────────────

def _validate_git_url(url: str) -> None:
    """只允许 https:// 协议，禁止 file:// ssh:// git:// 等。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("https",):
        raise ValueError(
            f"git_url must use https:// scheme, got: {parsed.scheme!r}"
        )
    if not parsed.netloc:
        raise ValueError(f"git_url has no host: {url!r}")


def _validate_git_sha(sha: str) -> None:
    """要求完整 40 位十六进制 SHA。"""
    if len(sha) != 40 or not all(c in "0123456789abcdefABCDEF" for c in sha):
        raise ValueError(
            f"git_sha must be a 40-char hex string, got: {sha!r}"
        )


async def _git_clone(url: str, sha: str, dest: Path) -> None:
    """在 Worker 进程侧执行 git clone + checkout。

    步骤：
        1. git clone --depth 1 <url> <dest>  （浅克隆加速）
        2. git fetch origin <sha>             （如果 sha 不在浅历史中）
        3. git checkout <sha>

    注意：此函数运行在 Worker 进程中，而非容器内，避免 git 凭据注入容器。
    """
    logger.info("git clone %s @ %s into %s", url, sha[:8], dest)

    # Step 1: 浅克隆
    ret = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1", url, str(dest),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        ret.communicate(), timeout=GIT_CLONE_TIMEOUT_SEC
    )
    if ret.returncode != 0:
        raise RuntimeError(
            f"git clone failed (rc={ret.returncode}): {stderr.decode()[:500]}"
        )

    # Step 2: 尝试 fetch 指定 sha（浅克隆可能不包含）
    fetch_proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(dest), "fetch", "--depth", "1", "origin", sha,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(fetch_proc.communicate(), timeout=GIT_CLONE_TIMEOUT_SEC)
    # fetch 可能失败（sha 已在历史中），不强制成功

    # Step 3: checkout 到指定 sha
    co_proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(dest), "checkout", sha,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    co_stdout, co_stderr = await asyncio.wait_for(
        co_proc.communicate(), timeout=30
    )
    if co_proc.returncode != 0:
        raise RuntimeError(
            f"git checkout {sha} failed (rc={co_proc.returncode}): "
            f"{co_stderr.decode()[:500]}"
        )

    logger.info("git clone complete: %s @ %s", url, sha[:8])
