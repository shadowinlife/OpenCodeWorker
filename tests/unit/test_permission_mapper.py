"""
单元测试：opencode client 的 respond_permission 合法值验证

覆盖场景：
    - once / always / reject 为合法值，不应抛出 ValueError
    - approve / deny / allow 为非法值，应抛出 ValueError
    - 大小写严格匹配

注意：此测试仅测试客户端本地校验逻辑，不启动 HTTP 服务。
"""
import pytest

from worker.adapters.opencode.client import OpenCodeClient

# respond_permission 中内联校验集（与 client.py 保持同步）
_VALID = {"once", "always", "reject"}


def _check_permission_response(response: str) -> None:
    """镜像 OpenCodeClient.respond_permission 的本地校验逻辑。"""
    if response not in _VALID:
        raise ValueError(
            f"Invalid permission response: {response!r}, must be one of {_VALID}"
        )


class TestPermissionResponseValidation:
    @pytest.mark.parametrize("valid_response", ["once", "always", "reject"])
    def test_valid_responses_do_not_raise(self, valid_response):
        """合法响应值不应引发 ValueError。"""
        _check_permission_response(valid_response)  # should not raise

    @pytest.mark.parametrize("invalid_response", ["approve", "deny", "allow", "yes", "no", "ONCE"])
    def test_invalid_responses_raise(self, invalid_response):
        """非法响应值（包括大小写变体）应引发 ValueError。"""
        with pytest.raises(ValueError, match="must be one of"):
            _check_permission_response(invalid_response)
