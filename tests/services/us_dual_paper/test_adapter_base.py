import pytest

from app.schemas.us_dual_paper import (
    AccountStateSummary,
    BrokerPreviewRequest,
    BrokerPreviewResult,
)
from app.services.us_dual_paper.adapters.base import BrokerPreviewAdapter


class _Fake(BrokerPreviewAdapter):
    account_scope = "alpaca_paper"

    def is_enabled(self) -> bool:
        return True

    def missing_env_keys(self) -> list[str]:
        return []

    async def read_account_state(self) -> AccountStateSummary:
        return AccountStateSummary(buying_power_usd=10.0)

    async def preview(self, req: BrokerPreviewRequest) -> BrokerPreviewResult:
        from app.schemas.us_dual_paper import DualPaperBrokerStatus

        return BrokerPreviewResult(
            account_scope=self.account_scope, status=DualPaperBrokerStatus.PREVIEWED
        )


@pytest.mark.unit
def test_protocol_conformance():
    adapter = _Fake()
    assert isinstance(adapter, BrokerPreviewAdapter)
    assert adapter.account_scope == "alpaca_paper"
