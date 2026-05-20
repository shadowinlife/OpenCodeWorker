"""
单元测试：W2-1 拦截器层"业务无关"不变量

CI gate：禁止 src/worker/adapters/opencode/interceptors/ 内出现任何
业务字符串（vibe-trading / strategy / signal_engine / ma250 / skill），
对应设计文档 §8 与 X1 backlog DoD §8 row 8。

未来 W2-2 / W2-3 / W2-4 任何 PR 必须保持这道闸门绿色。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# 仓库根目录：tests/unit/ → 上两级
_REPO_ROOT = Path(__file__).resolve().parents[2]
_INTERCEPTORS_DIR = _REPO_ROOT / "src" / "worker" / "adapters" / "opencode" / "interceptors"

_FORBIDDEN = re.compile(
    r"\b(vibe-trading|signal_engine|ma250)\b|\bstrategy\b|\bskill\b",
    re.IGNORECASE,
)


def test_interceptors_dir_exists():
    assert _INTERCEPTORS_DIR.is_dir(), (
        f"expected interceptors directory at {_INTERCEPTORS_DIR}"
    )


def test_interceptors_layer_has_no_business_strings():
    """grep gate：扫描所有 .py 文件确认零业务关键词。"""
    offenders: list[tuple[str, int, str]] = []
    for py in sorted(_INTERCEPTORS_DIR.rglob("*.py")):
        for lineno, line in enumerate(py.read_text().splitlines(), start=1):
            if _FORBIDDEN.search(line):
                offenders.append((str(py.relative_to(_REPO_ROOT)), lineno, line))
    assert not offenders, (
        "Business strings leaked into worker interceptor layer (architecture "
        "invariant break per design §11.3 / claudedocs/design_w2_1_event_"
        "interceptor_20260520.md §8). Move business knowledge to the "
        "upstream meta-skill instead.\n"
        + "\n".join(f"  {p}:{n}: {l}" for p, n, l in offenders)
    )
