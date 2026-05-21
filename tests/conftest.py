"""
项目级 pytest 配置。

唯一职责：在测试收集前注入测试用 ``WORKER_BEARER_TOKEN``。

背景：``worker.config.Settings`` 通过 pydantic-settings 把 ``bearer_token``
声明为必填字段（API 鉴权依赖），未注入时 ``get_settings()`` / 构造
``OpenCodeDriver`` 会直接抛 ``ValidationError``。生产链路依赖该约束做
安全兜底，**不能放宽**；但单测进程没有真实部署上下文，需要在采集
test module 之前用一个伪 token 把这条 invariants 满足掉。

做法：在 conftest 模块顶层（pytest 加载 conftest 的时机早于任何 test
module / fixture），如果 env 中没有 ``WORKER_BEARER_TOKEN`` 才注入；
存在则保留，避免覆盖 CI / 本地真值。任何具体 fixture 仍可用
``monkeypatch.setenv`` 临时覆盖并自动回滚，互不影响。
"""
from __future__ import annotations

import os

os.environ.setdefault("WORKER_BEARER_TOKEN", "test-bearer-" + "x" * 20)
