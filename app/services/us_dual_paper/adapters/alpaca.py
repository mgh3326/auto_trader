from __future__ import annotations

from collections.abc import Callable

from app.mcp_server.tooling.alpaca_paper_preview import alpaca_paper_preview_order
from app.schemas.us_dual_paper import (
    AccountStateSummary,
    BrokerPreviewRequest,
    BrokerPreviewResult,
    DualPaperBrokerStatus,
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

    async def preview(self, req: BrokerPreviewRequest) -> BrokerPreviewResult:
        notional = req.quantity * req.limit_price_usd
        blocked: list[str] = []
        warnings: list[str] = []
        if req.quantity <= 0:
            blocked.append("quantity_must_be_positive")
        if req.limit_price_usd <= 0:
            blocked.append("limit_price_must_be_positive")
        if notional > req.notional_cap_usd:
            blocked.append("notional_exceeds_cap")

        buying_power: float | None = None
        if not blocked:
            try:
                echo = await alpaca_paper_preview_order(
                    symbol=req.symbol,
                    side="buy",
                    type="limit",
                    qty=req.quantity,
                    limit_price=req.limit_price_usd,
                    asset_class="us_equity",
                )
                ctx = echo.get("account_context") or {}
                if ctx.get("buying_power") is not None:
                    buying_power = float(ctx["buying_power"])
                if echo.get("would_exceed_buying_power") is True:
                    blocked.append("would_exceed_buying_power")
                warnings.extend(echo.get("warnings") or [])
            except Exception as exc:  # surfaced to orchestrator as error
                raise exc

        status = DualPaperBrokerStatus.BLOCKED if blocked else DualPaperBrokerStatus.PREVIEWED
        return BrokerPreviewResult(
            account_scope=self.account_scope,
            status=status,
            blocked_reasons=blocked,
            warnings=warnings,
            quantity=req.quantity,
            limit_price_usd=req.limit_price_usd,
            notional_usd=round(notional, 2),
            account_state=AccountStateSummary(buying_power_usd=buying_power),
        )

