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

import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.alpaca_paper_preview import (
    ALPACA_PAPER_CRYPTO_MAX_NOTIONAL_USD,
    PreviewOrderInput,
)
from app.models.market_quote_snapshot import MarketQuoteSnapshot
from app.models.trading import InstrumentType
from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService
from app.services.alpaca_paper_submit_service import (
    AlpacaPaperSubmitCoordinator,
    SubmitOutcome,
    build_canonical_payload,
    canonical_hash,
    derive_automated_key,
)
from app.services.brokers.alpaca.service import AlpacaPaperBrokerService
from app.services.crypto_execution_mapping import (
    CryptoExecutionMappingError,
    map_upbit_to_alpaca_paper,
)
from app.services.paper_approval_packet import (
    PaperApprovalPacket,
    PaperApprovalPacketError,
    verify_order_within_packet,
    verify_packet_freshness,
    verify_packet_market_data,
)

# Server hard-cap policy ceiling for an automated per-order notional (never
# caller-supplied). Equity uses the ROB-73 strict cap; crypto the narrow $50 cap.
_HARD_NOTIONAL_CAP_USD = Decimal("1000")
_HARD_EQUITY_MAX_QTY = Decimal("5")

if TYPE_CHECKING:
    from fastmcp import FastMCP

ALPACA_PAPER_AUTOMATED_TOOL_NAMES: set[str] = {
    "alpaca_paper_automated_preview_order",
    "alpaca_paper_automated_submit_order",
}

DEFAULT_PREVIEW_TTL_SECONDS = 300
_QUOTE_MAX_AGE = timedelta(minutes=5)

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


def _instrument_type_for(asset_class: str) -> InstrumentType:
    return (
        InstrumentType.crypto if asset_class == "crypto" else InstrumentType.equity_us
    )


def _rejected(coid: str | None, code: str, message: str) -> dict[str, Any]:
    return {
        "success": False,
        "account_mode": "alpaca_paper",
        "source": "alpaca_paper",
        "submitted": False,
        "status": "rejected",
        "reason_code": code,
        "client_order_id": coid,
        "message": message,
    }


def _hard_ceiling(
    validated: PreviewOrderInput,
) -> tuple[Decimal | None, Decimal | None]:
    """Server policy ceiling (never caller-supplied) for an automated order."""
    hard_notional = (
        ALPACA_PAPER_CRYPTO_MAX_NOTIONAL_USD
        if validated.asset_class == "crypto"
        else _HARD_NOTIONAL_CAP_USD
    )
    if validated.side == "sell":
        # Sell ceiling authority is the live position (checked at submit); the
        # packet's max_qty mirrors the intent so order-within passes.
        return None, validated.qty if validated.qty is not None else Decimal("0")
    if validated.notional is not None or validated.limit_price is not None:
        return hard_notional, None
    if validated.qty is not None:
        return None, _HARD_EQUITY_MAX_QTY
    return hard_notional, None


def _snapshot_content_hash(snap: MarketQuoteSnapshot) -> str:
    blob = "|".join(
        str(x)
        for x in (
            snap.id,
            snap.market,
            snap.symbol,
            snap.source,
            snap.snapshot_at.isoformat(),
            snap.price,
        )
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _snapshot_matches_order(
    snap: MarketQuoteSnapshot, validated: PreviewOrderInput
) -> bool:
    if validated.asset_class == "crypto":
        if snap.market != "crypto":
            return False
        try:
            mapping = map_upbit_to_alpaca_paper(snap.symbol)
        except CryptoExecutionMappingError:
            return False
        return mapping.execution_symbol == validated.symbol
    return snap.market == "us" and (snap.symbol or "").upper() == validated.symbol


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
    Identity (correlation/snapshot), market provenance (as-of/source) and the
    signal symbol are loaded from that trusted artifact — never from the caller —
    and the ceiling is the server hard-cap policy. A missing / stale / symbol-
    mismatched snapshot fails closed before any packet is built. Returns an
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

    now = datetime.now(UTC)
    async with _session_factory()() as db:
        snap = (
            await db.execute(
                select(MarketQuoteSnapshot).where(
                    MarketQuoteSnapshot.id == int(quote_snapshot_id)
                )
            )
        ).scalar_one_or_none()

        if snap is None:
            return _rejected(
                None, "no_trusted_snapshot", "no trusted market snapshot for reference"
            )
        if not _snapshot_matches_order(snap, validated):
            return _rejected(
                None,
                "snapshot_symbol_mismatch",
                f"trusted snapshot symbol {snap.symbol!r} does not map to order {validated.symbol!r}",
            )
        asof = snap.snapshot_at
        if asof.tzinfo is None:
            asof = asof.replace(tzinfo=UTC)
        if now - asof > _QUOTE_MAX_AGE:
            return _rejected(
                None,
                "stale_trusted_snapshot",
                f"trusted snapshot as-of {asof.isoformat()} is stale",
            )

        # --- Server-owned identity + provenance derived from the trusted row ---
        content_hash = _snapshot_content_hash(snap)
        correlation_id = f"rob842dec-{content_hash}"
        packet_snapshot_id = f"qs{snap.id}-{content_hash}"
        signal_symbol = snap.symbol
        max_notional, max_qty = _hard_ceiling(validated)
        qty_source = (
            "ledger_filled_qty" if validated.side == "sell" else "notional_estimate"
        )

        coid = derive_automated_key(
            correlation_id=correlation_id,
            snapshot_id=packet_snapshot_id,
            canonical=canonical,
        )

        try:
            packet = PaperApprovalPacket(
                signal_source="automated_preview",
                artifact_id=uuid.uuid4(),
                signal_symbol=signal_symbol,
                signal_venue="upbit",
                execution_symbol=validated.symbol,
                execution_venue="alpaca_paper",
                execution_asset_class=validated.asset_class,
                side=validated.side,
                max_notional=max_notional,
                max_qty=max_qty,
                qty_source=qty_source,
                expected_lifecycle_step="previewed",
                lifecycle_correlation_id=correlation_id,
                client_order_id=coid,
                expires_at=now + timedelta(seconds=max(1, int(valid_for_seconds))),
                account_mode="alpaca_paper",
                origin="automated",
                market_data_asof=asof,
                market_data_source=snap.source,
                preview_payload_hash=canonical_hash(canonical),
                snapshot_id=packet_snapshot_id,
                execution_order_type=validated.type,
                execution_time_in_force=validated.time_in_force,
            )
        except ValueError as exc:
            return _rejected(coid, "invalid_packet", str(exc))

        # Fail-close market-data + order-authority (ceiling) checks at preview.
        try:
            verify_packet_market_data(packet, now=now, max_age=_QUOTE_MAX_AGE)
            verify_order_within_packet(packet, canonical)
        except PaperApprovalPacketError as exc:
            return _rejected(coid, exc.code, str(exc))

        packet_dict = packet.model_dump(mode="json")
        provenance = {
            "quote_snapshot_id": snap.id,
            "snapshot_content_hash": content_hash,
            "market_data_source": snap.source,
            "policy_max_notional": str(max_notional)
            if max_notional is not None
            else None,
            "policy_max_qty": str(max_qty) if max_qty is not None else None,
            "packet_hash": _packet_hash(packet_dict),
        }

        ledger = AlpacaPaperLedgerService(db)
        await ledger.record_preview(
            client_order_id=coid,
            lifecycle_correlation_id=correlation_id,
            execution_symbol=validated.symbol,
            execution_venue="alpaca_paper",
            instrument_type=_instrument_type_for(validated.asset_class),
            side=validated.side,
            order_type=validated.type,
            time_in_force=validated.time_in_force,
            requested_qty=validated.qty,
            requested_notional=validated.notional,
            requested_price=validated.limit_price,
            preview_payload={
                "canonical": canonical,
                "approval_packet": packet_dict,
                "provenance": provenance,
            },
        )

    return {
        "success": True,
        "account_mode": "alpaca_paper",
        "source": "alpaca_paper",
        "submitted": False,
        "preview": True,
        "approval_token": coid,
        "client_order_id": coid,
        "expires_at": packet.expires_at.isoformat(),
        "order_request": canonical,
        "provenance": provenance,
    }


def _packet_hash(packet_dict: dict[str, Any]) -> str:
    import json

    blob = json.dumps(
        packet_dict, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


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

    token = (approval_token or "").strip()
    if not token:
        raise ValueError("approval_token is required")

    async with _session_factory()() as db:
        ledger = AlpacaPaperLedgerService(db)
        preview = await ledger.get_preview_by_client_order_id(token)
        if preview is None:
            return _rejected(
                token, "no_preview_for_token", "no persisted preview for approval_token"
            )

        payload = preview.preview_payload or {}
        canonical = payload.get("canonical")
        packet_dict = payload.get("approval_packet")
        if not isinstance(canonical, dict) or not isinstance(packet_dict, dict):
            return _rejected(token, "malformed_preview", "preview payload is malformed")

        try:
            packet = PaperApprovalPacket(**packet_dict)
        except ValueError as exc:
            return _rejected(token, "malformed_preview", str(exc))

        if confirm is not True:
            # Re-validate freshness so a stale dry-run reports honestly.
            reason = None
            now = datetime.now(UTC)
            try:
                verify_packet_freshness(packet, now=now)
                verify_packet_market_data(packet, now=now, max_age=_QUOTE_MAX_AGE)
            except PaperApprovalPacketError as exc:
                reason = exc.code
            return {
                "success": True,
                "account_mode": "alpaca_paper",
                "source": "alpaca_paper",
                "submitted": False,
                "blocked_reason": "confirmation_required",
                "client_order_id": token,
                "would_reject_reason": reason,
                "order_request": canonical,
            }

        coordinator = AlpacaPaperSubmitCoordinator(ledger, _broker_factory)
        outcome = await coordinator.submit(packet, submit_canonical=canonical)
        return _outcome_to_result(outcome)


def _outcome_to_result(outcome: SubmitOutcome) -> dict[str, Any]:
    return {
        "success": outcome.status != "rejected",
        "account_mode": "alpaca_paper",
        "source": "alpaca_paper",
        "submitted": outcome.submitted,
        "status": outcome.status,
        "reason_code": outcome.reason_code,
        "client_order_id": outcome.client_order_id,
        "broker_called": outcome.broker_called,
        "order": outcome.order,
        "message": outcome.message,
    }


def register_alpaca_paper_automated_orders_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="alpaca_paper_automated_preview_order",
        description=(
            "Automated Alpaca PAPER preview: server builds and persists the approval "
            "packet (server-owned decision identity + preview hash + market-data "
            "as-of) and returns an approval_token. No broker call. Requires "
            "correlation_id, snapshot_id, market_data_asof, market_data_source, and "
            "exactly one approved ceiling (max_notional or max_qty)."
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
