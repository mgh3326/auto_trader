import pytest

from app.schemas.us_dual_paper import (
    AccountStateSummary,
    BrokerPreviewResult,
    DualPaperBrokerStatus,
)
from app.services.us_dual_paper.adapters.base import BrokerPreviewAdapter
from app.services.us_dual_paper.packet import build_packet


class _Ok(BrokerPreviewAdapter):
    account_scope = "alpaca_paper"

    def is_enabled(self):
        return True

    def missing_env_keys(self):
        return []

    async def read_account_state(self):
        return AccountStateSummary(buying_power_usd=1000.0)

    async def preview(self, req):
        return BrokerPreviewResult(
            account_scope=self.account_scope, status=DualPaperBrokerStatus.PREVIEWED
        )


class _Boom(BrokerPreviewAdapter):
    account_scope = "kis_mock"

    def is_enabled(self):
        return True

    def missing_env_keys(self):
        return []

    async def read_account_state(self):
        raise RuntimeError("kis down")

    async def preview(self, req):
        raise RuntimeError("kis down")


class _Disabled(BrokerPreviewAdapter):
    account_scope = "kis_mock"

    def is_enabled(self):
        return False

    def missing_env_keys(self):
        return ["KIS_MOCK_ENABLED"]

    async def read_account_state(self):
        raise AssertionError("must not be called when disabled")

    async def preview(self, req):
        raise AssertionError("must not be called when disabled")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_broker_error_does_not_collapse_the_other():
    packet = await build_packet(
        symbol="NVDA",
        quantity=1,
        limit_price_usd=10.0,
        notional_cap_usd=50.0,
        limit_price_source="operator_input",
        adapters=[_Ok(), _Boom()],
    )
    assert packet.submit_enabled is False
    assert packet.brokers["alpaca_paper"].status is DualPaperBrokerStatus.PREVIEWED
    assert packet.brokers["kis_mock"].status is DualPaperBrokerStatus.ERROR
    assert packet.brokers["kis_mock"].reason == "RuntimeError"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disabled_broker_is_unsupported_other_previewed():
    packet = await build_packet(
        symbol="NVDA",
        quantity=1,
        limit_price_usd=10.0,
        notional_cap_usd=50.0,
        limit_price_source="operator_input",
        adapters=[_Ok(), _Disabled()],
    )
    assert packet.brokers["alpaca_paper"].status is DualPaperBrokerStatus.PREVIEWED
    kis = packet.brokers["kis_mock"]
    assert kis.status is DualPaperBrokerStatus.UNSUPPORTED
    assert kis.blocked_reasons == [] and "KIS_MOCK_ENABLED" in (kis.reason or "")
