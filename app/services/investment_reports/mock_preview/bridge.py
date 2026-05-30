"""ROB-373 — report item -> KIS mock preview bridge (read-only, submit OFF).

Translates a projected BUY intent into a us_dual_paper preview using the
``kis_mock`` adapter ONLY (Alpaca is never invoked here — KIS mock and Alpaca
Paper evidence must not mix). Fail-closed: if the adapter is not configured the
bridge returns ``status='unsupported'`` (env key NAMES only) without any network
call. No order is ever submitted: ``submit_enabled`` is always False.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.schemas.us_dual_paper import BrokerPreviewRequest
from app.services.us_dual_paper.adapters.kis_mock import KisMockUsAdapter

_DEFAULT_NOTIONAL_CAP_USD = 50.0


@dataclass(frozen=True)
class OrderParams:
    symbol: str
    quantity: float
    limit_price_usd: float
    notional_cap_usd: float
    reference_price_usd: float


def extract_order_params(
    *,
    symbol: str | None,
    evidence_snapshot: dict[str, Any],
    max_action: dict[str, Any],
) -> OrderParams | None:
    """Derive deterministic limit-order params from an advisory item.

    Returns None (skip — never fabricate) when symbol or a positive reference
    price is unavailable. notional cap comes from ``max_action`` or a default.
    """
    if not symbol:
        return None
    ref = evidence_snapshot.get("reference_price_usd")
    if ref is None:
        ref = evidence_snapshot.get("price") or evidence_snapshot.get("current_price")
    try:
        ref_price = float(ref) if ref is not None else 0.0
    except (TypeError, ValueError):
        return None
    if ref_price <= 0:
        return None

    cap_raw = max_action.get("notional_usd") or max_action.get("notional_cap_usd")
    try:
        cap = float(cap_raw) if cap_raw is not None else _DEFAULT_NOTIONAL_CAP_USD
    except (TypeError, ValueError):
        cap = _DEFAULT_NOTIONAL_CAP_USD

    limit_raw = evidence_snapshot.get("limit_price_usd")
    try:
        limit = float(limit_raw) if limit_raw is not None else ref_price
    except (TypeError, ValueError):
        limit = ref_price
    if limit <= 0:
        return None

    return OrderParams(
        symbol=symbol,
        quantity=cap / limit,
        limit_price_usd=limit,
        notional_cap_usd=cap,
        reference_price_usd=ref_price,
    )


class MockPreviewBridge:
    """Produces a kis_mock preview dict for embedding into a report item."""

    def __init__(self, *, adapter: KisMockUsAdapter | None = None) -> None:
        self._adapter = adapter if adapter is not None else KisMockUsAdapter()

    async def preview(self, params: OrderParams) -> dict[str, Any]:
        # Fail-closed BEFORE any network call: adapter not configured.
        if not self._adapter.is_enabled():
            return {
                "status": "unsupported",
                "account_scope": self._adapter.account_scope,
                "submit_enabled": False,
                "missing_env_keys": self._adapter.missing_env_keys(),
            }

        req = BrokerPreviewRequest(
            symbol=params.symbol,
            quantity=params.quantity,
            limit_price_usd=params.limit_price_usd,
            notional_cap_usd=params.notional_cap_usd,
            reference_price_usd=params.reference_price_usd,
        )
        result = await self._adapter.preview(req)
        payload = result.model_dump(mode="json")
        # DualPaperBrokerStatus is a StrEnum — model_dump(mode="json") serializes
        # directly to the string value (e.g. "previewed", "blocked", "unsupported").
        # The normalization below is a safety net in case future enum changes alter
        # serialization; it strips any enum class prefix and lowercases the result.
        payload["status"] = str(payload.get("status", "")).split(".")[-1].lower()
        payload["submit_enabled"] = False  # invariant: bridge never enables submit
        return payload
