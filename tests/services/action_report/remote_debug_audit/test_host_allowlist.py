import pytest

from app.services.action_report.remote_debug_audit.host_allowlist import (
    CdpDebugHostBlocked,
    assert_cdp_debug_host,
)


def test_allows_only_localhost_9222() -> None:
    assert_cdp_debug_host("127.0.0.1:9222")  # no raise


@pytest.mark.parametrize(
    "bad",
    ["localhost:9222", "127.0.0.1:9223", "0.0.0.0:9222", "10.0.0.5:9222", "127.0.0.1", ""],
)
def test_rejects_everything_else(bad: str) -> None:
    with pytest.raises(CdpDebugHostBlocked):
        assert_cdp_debug_host(bad)
