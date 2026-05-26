"""Dual-broker preview orchestrator (ROB-326). Each broker isolated; submit disabled."""

from __future__ import annotations

from app.schemas.us_dual_paper import (
    BrokerPreviewRequest,
    BrokerPreviewResult,
    DualBrokerPreviewPacket,
    DualPaperBrokerStatus,
)
from app.services.us_dual_paper.adapters.alpaca import AlpacaPaperAdapter
from app.services.us_dual_paper.adapters.base import BrokerPreviewAdapter
from app.services.us_dual_paper.adapters.kis_mock import KisMockUsAdapter


def default_adapters() -> list[BrokerPreviewAdapter]:
    return [KisMockUsAdapter(), AlpacaPaperAdapter()]


async def _preview_one(adapter: BrokerPreviewAdapter, req: BrokerPreviewRequest) -> BrokerPreviewResult:
    if not adapter.is_enabled():
        return BrokerPreviewResult(
            account_scope=adapter.account_scope,
            status=DualPaperBrokerStatus.UNSUPPORTED,
            reason="missing_env_keys: " + ", ".join(adapter.missing_env_keys()),
        )
    try:
        return await adapter.preview(req)
    except Exception as exc:  # isolation boundary — never propagates to other brokers
        return BrokerPreviewResult(
            account_scope=adapter.account_scope,
            status=DualPaperBrokerStatus.ERROR,
            reason=type(exc).__name__,
        )


async def build_packet(
    *,
    symbol: str,
    quantity: float,
    limit_price_usd: float,
    notional_cap_usd: float,
    limit_price_source: str,
    reference_price_usd: float | None = None,
    adapters: list[BrokerPreviewAdapter] | None = None,
) -> DualBrokerPreviewPacket:
    adapters = adapters if adapters is not None else default_adapters()
    req = BrokerPreviewRequest(
        symbol=symbol,
        quantity=quantity,
        limit_price_usd=limit_price_usd,
        notional_cap_usd=notional_cap_usd,
        reference_price_usd=reference_price_usd,
    )
    brokers: dict[str, BrokerPreviewResult] = {}
    for adapter in adapters:  # sequential; each fully isolated
        brokers[adapter.account_scope] = await _preview_one(adapter, req)
    return DualBrokerPreviewPacket(
        symbol=symbol,
        limit_price_source=limit_price_source,
        notional_cap_usd=notional_cap_usd,
        submit_enabled=False,
        brokers=brokers,
    )
