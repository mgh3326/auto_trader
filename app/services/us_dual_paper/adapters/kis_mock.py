from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.core.config import settings
from app.schemas.us_dual_paper import (
    AccountStateSummary,
    BrokerPreviewRequest,
    BrokerPreviewResult,
    DualPaperBrokerStatus,
)
from app.services.us_dual_paper.adapters.base import BrokerPreviewAdapter

_USD_NATIONS = {"미국", "US", "USA"}
_KIS_MOCK_ENV_KEYS = (
    "KIS_MOCK_ENABLED",
    "KIS_MOCK_APP_KEY",
    "KIS_MOCK_APP_SECRET",
    "KIS_MOCK_ACCOUNT_NO",
)
_MAX_LIMIT_DEVIATION_PCT = 10.0


def _to_float(value: Any) -> float | None:
    try:
        if isinstance(value, str):
            value = value.replace(",", "").strip()
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_usd_row(row: Mapping[str, Any]) -> bool:
    crcy = str(row.get("crcy_cd") or "").strip().upper()
    natn = str(row.get("natn_name") or "").strip().upper()
    return crcy == "USD" and (not natn or natn in {n.upper() for n in _USD_NATIONS})


def _default_kis_client() -> Any:
    from app.services.brokers.kis import kis  # local import: never at module scope

    return kis


class KisMockUsAdapter(BrokerPreviewAdapter):
    account_scope = "kis_mock"

    def __init__(
        self, *, kis_client: Any | None = None, enabled: bool | None = None
    ) -> None:
        self._kis_client = (
            kis_client if kis_client is not None else _default_kis_client()
        )
        self._enabled_override = enabled

    def is_enabled(self) -> bool:
        if self._enabled_override is not None:
            return self._enabled_override
        return not self.missing_env_keys()

    def missing_env_keys(self) -> list[str]:
        checks = {
            "KIS_MOCK_ENABLED": bool(getattr(settings, "kis_mock_enabled", False)),
            "KIS_MOCK_APP_KEY": bool(getattr(settings, "kis_mock_app_key", None)),
            "KIS_MOCK_APP_SECRET": bool(getattr(settings, "kis_mock_app_secret", None)),
            "KIS_MOCK_ACCOUNT_NO": bool(getattr(settings, "kis_mock_account_no", None)),
        }
        return [name for name, present in checks.items() if not present]

    async def read_account_state(self) -> AccountStateSummary:
        rows = await self._kis_client.inquire_overseas_margin(is_mock=True)
        cash_usd: float | None = None
        buying_power_usd: float | None = None
        for row in rows or []:
            if isinstance(row, Mapping) and _is_usd_row(row):
                cash_usd = _to_float(row.get("frcr_dncl_amt1"))
                buying_power_usd = _to_float(row.get("frcr_ord_psbl_amt1"))
                break
        holdings = await self._kis_client.fetch_my_us_stocks(is_mock=True)
        return AccountStateSummary(
            cash_usd=cash_usd,
            buying_power_usd=buying_power_usd,
            position_count=len(holdings or []),
            open_order_count=None,
        )

    async def preview(self, req: BrokerPreviewRequest) -> BrokerPreviewResult:
        blocked: list[str] = []
        warnings: list[str] = []
        notional = req.quantity * req.limit_price_usd

        if req.quantity <= 0:
            blocked.append("quantity_must_be_positive")
        if req.limit_price_usd <= 0:
            blocked.append("limit_price_must_be_positive")
        if notional > req.notional_cap_usd:
            blocked.append("notional_exceeds_cap")

        summary = await self.read_account_state()
        if summary.buying_power_usd is None:
            warnings.append("buying_power_unavailable")
        elif notional > summary.buying_power_usd:
            blocked.append("insufficient_buying_power")

        if req.reference_price_usd is None or req.reference_price_usd <= 0:
            warnings.append("reference_price_missing_for_limit_sanity")
        else:
            deviation = (
                abs(req.limit_price_usd - req.reference_price_usd)
                / req.reference_price_usd
                * 100.0
            )
            if deviation > _MAX_LIMIT_DEVIATION_PCT:
                blocked.append("limit_price_deviation_exceeds_bound")

        status = (
            DualPaperBrokerStatus.BLOCKED if blocked else DualPaperBrokerStatus.PREVIEWED
        )
        return BrokerPreviewResult(
            account_scope=self.account_scope,
            status=status,
            blocked_reasons=blocked,
            warnings=warnings,
            quantity=req.quantity,
            limit_price_usd=req.limit_price_usd,
            notional_usd=round(notional, 2),
            account_state=summary,
            check_details={"account_mode": "kis_mock", "broker_mutation": "disabled"},
        )
