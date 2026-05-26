import pytest

from app.services.action_report.remote_debug_audit.cdp_client import (
    CdpClient,
    FakeCdpSession,
)
from app.services.action_report.remote_debug_audit.host_allowlist import (
    CdpDebugHostBlocked,
)


def test_client_construction_host_locked() -> None:
    CdpClient(host_port="127.0.0.1:9222")  # ok
    with pytest.raises(CdpDebugHostBlocked):
        CdpClient(host_port="127.0.0.1:9999")


@pytest.mark.asyncio
async def test_fake_session_returns_canned_value_by_url() -> None:
    fake = FakeCdpSession(results={"https://x/?code=005930": '{"code":"005930"}'})
    out = await fake.fetch_rendered("https://x/?code=005930", "js", timeout_s=1.0)
    assert out == '{"code":"005930"}'


@pytest.mark.asyncio
async def test_fake_session_raises_for_unknown_url() -> None:
    fake = FakeCdpSession(results={})
    with pytest.raises(RuntimeError):
        await fake.fetch_rendered("https://x/?code=000660", "js", timeout_s=1.0)
