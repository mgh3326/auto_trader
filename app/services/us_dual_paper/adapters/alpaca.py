from __future__ import annotations

from collections.abc import Callable

from app.schemas.us_dual_paper import (
    AccountStateSummary,
    BrokerPreviewRequest,
    BrokerPreviewResult,
)
from app.services.brokers.alpaca.config import AlpacaPaperSettings
from app.services.brokers.alpaca.exceptions import AlpacaPaperConfigurationError
from app.services.brokers.alpaca.service import AlpacaPaperBrokerService
from app.services.us_dual_paper.adapters.base import BrokerPreviewAdapter

ServiceFactory = Callable[[], AlpacaPaperBrokerService]

_ALPACA_ENV_KEYS = ("ALPACA_PAPER_API_KEY", "ALPACA_PAPER_API_SECRET")


def _default_factory() -> AlpacaPaperBrokerService:
    return AlpacaPaperBrokerService()


class AlpacaPaperAdapter(BrokerPreviewAdapter):
    account_scope = "alpaca_paper"

    def __init__(self, service_factory: ServiceFactory = _default_factory) -> None:
        self._service_factory = service_factory

    def is_enabled(self) -> bool:
        return not self.missing_env_keys()

    def missing_env_keys(self) -> list[str]:
        try:
            s = AlpacaPaperSettings.from_app_settings()
        except AlpacaPaperConfigurationError:
            return list(_ALPACA_ENV_KEYS)
        missing: list[str] = []
        if not s.api_key:
            missing.append("ALPACA_PAPER_API_KEY")
        if not s.api_secret:
            missing.append("ALPACA_PAPER_API_SECRET")
        return missing

    async def read_account_state(self) -> AccountStateSummary:
        service = self._service_factory()
        cash = await service.get_cash()
        positions = await service.list_positions()
        return AccountStateSummary(
            cash_usd=float(cash.cash),
            buying_power_usd=float(cash.buying_power),
            position_count=len(positions),
            open_order_count=None,
        )

    async def preview(self, req: BrokerPreviewRequest) -> BrokerPreviewResult:  # PR2 Task 11
        raise NotImplementedError("preview() is implemented in PR2")
