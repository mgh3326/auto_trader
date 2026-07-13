"""Automated Alpaca PAPER submit boundary MCP tools (ROB-842).

Two-step, server-owned handshake that is the ONLY automated broker path:

  alpaca_paper_automated_preview_order
      Server validates the order, builds and *persists* the approval packet
      (server-owned decision identity + preview hash + market-data as-of) as a
      preview row in the existing Alpaca paper ledger, and returns an
      ``approval_token``. No broker call.

  alpaca_paper_automated_submit_order
      Loads the server-persisted packet by ``approval_token`` (the caller never
      supplies a canonical payload or client_order_id), then routes through the
      ledger atomic-claim coordinator: exactly one broker POST for the winner;
      replay / recovered / idempotency_in_progress for everyone else.

Trust boundary:
- The idempotency key is derived server-side from correlation_id + snapshot_id +
  canonical; the caller cannot inject or overwrite it.
- There is NO caller-selectable ``origin`` — this module *is* the automated
  entrypoint, physically separate from the manual operator smoke tool.
- Default-disabled behind ``settings.alpaca_paper_automated_submit_enabled``.
- Paper-host pin preserved (the only broker built is AlpacaPaperBrokerService);
  no live endpoint / live credential path is imported.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.alpaca_paper_preview import PreviewOrderInput
from app.services.alpaca_paper_order_application import AlpacaPaperOrderApplication
from app.services.alpaca_paper_submit_service import (
    build_canonical_payload,
)
from app.services.brokers.alpaca.service import AlpacaPaperBrokerService

if TYPE_CHECKING:
    from fastmcp import FastMCP

ALPACA_PAPER_AUTOMATED_TOOL_NAMES: set[str] = {
    "alpaca_paper_automated_preview_order",
    "alpaca_paper_automated_submit_order",
}

DEFAULT_PREVIEW_TTL_SECONDS = 300

SessionFactory = Callable[[], async_sessionmaker[AsyncSession]]
BrokerFactory = Callable[[], AlpacaPaperBrokerService]


def _default_session_factory() -> async_sessionmaker[AsyncSession]:
    return AsyncSessionLocal  # type: ignore[return-value]


def _default_broker_factory() -> AlpacaPaperBrokerService:
    return AlpacaPaperBrokerService()


_session_factory: SessionFactory = _default_session_factory
_broker_factory: BrokerFactory = _default_broker_factory


def set_alpaca_paper_automated_factories(
    *,
    session_factory: SessionFactory | None = None,
    broker_factory: BrokerFactory | None = None,
) -> None:
    global _session_factory, _broker_factory
    if session_factory is not None:
        _session_factory = session_factory
    if broker_factory is not None:
        _broker_factory = broker_factory


def reset_alpaca_paper_automated_factories() -> None:
    global _session_factory, _broker_factory
    _session_factory = _default_session_factory
    _broker_factory = _default_broker_factory


def _enabled() -> bool:
    from app.core.config import settings

    return bool(getattr(settings, "alpaca_paper_automated_submit_enabled", False))


def _disabled_result(tool: str) -> dict[str, Any]:
    return {
        "success": False,
        "account_mode": "alpaca_paper",
        "source": "alpaca_paper",
        "submitted": False,
        "disabled": True,
        "reason_code": "automated_submit_disabled",
        "message": (
            f"{tool} is disabled; set alpaca_paper_automated_submit_enabled=true to arm"
        ),
    }


async def alpaca_paper_automated_preview_order(
    symbol: str,
    side: str,
    type: str,  # noqa: A002
    quote_snapshot_id: int,
    qty: Decimal | None = None,
    notional: Decimal | None = None,
    time_in_force: str | None = None,
    limit_price: Decimal | None = None,
    asset_class: str = "us_equity",
    valid_for_seconds: int = DEFAULT_PREVIEW_TTL_SECONDS,
) -> dict[str, Any]:
    """Build and persist the server-owned approval packet for an automated submit.

    The caller supplies ONLY the order intent plus an opaque, server-issued
    ``quote_snapshot_id`` referencing a trusted ``market_quote_snapshots`` row.
    Identity (correlation/snapshot), market provenance (as-of/source), the signal
    symbol and the trusted reference price are loaded from that artifact — never
    from the caller — and the ceiling is the server hard-cap policy. A missing /
    stale / symbol-mismatched / non-finite-priced snapshot fails closed before any
    packet is built. Automated SELL is explicitly disabled until ROB-845 wires an
    opaque buy/position source (see reason ``automated_sell_disabled``). Returns an
    ``approval_token`` bound to the persisted packet. No broker call.
    """
    if not _enabled():
        return _disabled_result("alpaca_paper_automated_preview_order")

    validated = PreviewOrderInput(
        symbol=symbol,
        side=side,
        type=type,
        qty=qty,
        notional=notional,
        time_in_force=time_in_force,
        limit_price=limit_price,
        stop_price=None,
        client_order_id=None,
        asset_class=asset_class,
    )

    canonical = build_canonical_payload(
        symbol=validated.symbol,
        side=validated.side,
        type=validated.type,
        time_in_force=validated.time_in_force,
        qty=validated.qty,
        notional=validated.notional,
        limit_price=validated.limit_price,
        asset_class=validated.asset_class,
    )
    application = AlpacaPaperOrderApplication(
        session_factory=_session_factory(),
        broker_factory=_broker_factory,
    )
    return await application.preview_trusted_snapshot(
        canonical=canonical,
        quote_snapshot_id=quote_snapshot_id,
        valid_for_seconds=valid_for_seconds,
    )


async def alpaca_paper_automated_submit_order(
    approval_token: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Submit an automated Alpaca paper order bound to a server-persisted preview.

    The caller passes only the ``approval_token`` from preview; the server loads
    its own packet and routes through the atomic-claim coordinator. Defaults to
    ``confirm=False`` (no claim, no broker call).
    """
    if not _enabled():
        return _disabled_result("alpaca_paper_automated_submit_order")

    application = AlpacaPaperOrderApplication(
        session_factory=_session_factory(),
        broker_factory=_broker_factory,
    )
    return await application.submit_token(
        approval_token=approval_token,
        confirm=confirm,
    )


def register_alpaca_paper_automated_orders_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="alpaca_paper_automated_preview_order",
        description=(
            "Automated Alpaca PAPER preview (buy only; automated sell is disabled "
            "until ROB-845). The caller passes ONLY the order intent plus an opaque, "
            "server-issued quote_snapshot_id (a trusted market_quote_snapshots row). "
            "The server loads identity (correlation/snapshot), market-data as-of/"
            "source, signal symbol and the trusted reference price from that row, "
            "sets the ceiling from hard-cap policy, persists the packet, and returns "
            "an approval_token. No broker call. A missing / stale / symbol-mismatched "
            "/ non-finite-priced snapshot fails closed. There is NO caller-supplied "
            "correlation, snapshot, market-data, ceiling, origin, or client_order_id."
        ),
    )(alpaca_paper_automated_preview_order)
    _ = mcp.tool(
        name="alpaca_paper_automated_submit_order",
        description=(
            "Automated Alpaca PAPER submit: bind to a server-persisted preview by "
            "approval_token and route through the ledger atomic-claim boundary. "
            "Exactly one broker POST for the winner; replay / recovered / "
            "idempotency_in_progress otherwise. Defaults to confirm=False (no claim, "
            "no broker call). The caller cannot supply a client_order_id or canonical."
        ),
    )(alpaca_paper_automated_submit_order)


__all__ = [
    "ALPACA_PAPER_AUTOMATED_TOOL_NAMES",
    "alpaca_paper_automated_preview_order",
    "alpaca_paper_automated_submit_order",
    "register_alpaca_paper_automated_orders_tools",
    "reset_alpaca_paper_automated_factories",
    "set_alpaca_paper_automated_factories",
]
