"""Worker 兼容性矩阵（design §7.2 / §14）。

服务端目前没有专门的 API version endpoint，只有 ``GET /health.version``。
SDK 在首次请求前可读取该字段并与本地兼容矩阵比对，避免上游误把 SDK 接到
不兼容的 Worker 实例。
"""
from __future__ import annotations

# SDK 0.1.x ↔ Worker 0.1.x（design §C7）
_SUPPORTED_MAJOR_MINOR: tuple[tuple[int, int], ...] = ((0, 1),)


def is_compatible(worker_version: str) -> bool:
    """检查 worker ``version`` 字符串是否落在 SDK 支持矩阵内。

    解析失败时按"不兼容"处理；上游可以选择关闭 ``compatibility_check`` 后
    手动决定如何应对。
    """
    parsed = _parse_major_minor(worker_version)
    if parsed is None:
        return False
    return parsed in _SUPPORTED_MAJOR_MINOR


def _parse_major_minor(version: str) -> tuple[int, int] | None:
    """从 SemVer 字符串提取 ``(major, minor)``。无法解析时返回 ``None``。"""
    if not version:
        return None
    parts = version.split(".")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def supported_matrix_str() -> str:
    """返回人类可读的支持矩阵描述，用于错误信息。"""
    pairs = ", ".join(f"{major}.{minor}.x" for major, minor in _SUPPORTED_MAJOR_MINOR)
    return f"Worker {pairs}"
