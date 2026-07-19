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

_US_EXCHANGE_CODES = ("NASD", "NYSE", "AMEX")
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


def _default_kis_client() -> Any:
    # Mock vs live is decided by the CLIENT INSTANCE (host), not the per-call
    # is_mock arg (which only selects the TR id). The module-level `kis`
    # singleton is a LIVE-host client, so mock TRs sent through it are rejected
    # by the live server (EGW02005). A KISClient(is_mock=True) targets the mock
    # host (openapivts) where VTTS3007R and overseas holdings reads succeed.
    from app.services.brokers.kis.client import KISClient  # local import

    return KISClient(is_mock=True)


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
        cash_usd: float | None = None
        buying_power_usd: float | None = None
        try:
            # ROB-951: KIS mock does NOT offer overseas foreign-margin (OPSQ0002
            # "no such service code"). USD buying power comes from the same
            # mock-only TR the order preflight gate uses (VTTS3007R), so the
            # public ``kis_mock.account_cash_read`` capability is honest.
            buyable = await self._kis_client.inquire_mock_overseas_buyable_amount()
            if isinstance(buyable, Mapping):
                # ``ord_psbl_frcr_amt`` (parsed as ``ovrs_ord_psbl_amt``) is the
                # verified USD orderable cash — the identical convention as
                # ``order_validation._get_balance_for_order``. VTTS3007R exposes no
                # separate deposit balance, so cash and buying power share it.
                buying_power_usd = _to_float(buyable.get("ovrs_ord_psbl_amt"))
                cash_usd = buying_power_usd
        except Exception:
            # Fail-closed: an unreadable balance stays unknown (None), never 0.
            cash_usd = None
            buying_power_usd = None
        holdings = await self._kis_client.fetch_my_us_stocks(is_mock=True)
        open_order_count = await self._read_open_order_count()
        return AccountStateSummary(
            cash_usd=cash_usd,
            buying_power_usd=buying_power_usd,
            position_count=len(holdings or []),
            open_order_count=open_order_count,
        )

    async def _read_open_order_count(self) -> int | None:
        """Best-effort open-order count across US exchanges (is_mock pinned).

        KIS mock overseas open-order reads are not guaranteed; on any failure
        return None ("where available") rather than breaking account-state read.
        """
        reader = getattr(self._kis_client, "inquire_overseas_orders", None)
        if not callable(reader):
            return None
        total = 0
        saw_any = False
        for exchange_code in _US_EXCHANGE_CODES:
            try:
                rows = await reader(exchange_code=exchange_code, is_mock=True)
            except Exception:  # best-effort per exchange
                continue
            saw_any = True
            total += len(rows or [])
        return total if saw_any else None

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
            DualPaperBrokerStatus.BLOCKED
            if blocked
            else DualPaperBrokerStatus.PREVIEWED
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
