"""Guarded Alpaca paper submit/cancel MCP tools (ROB-73).

Adapter-specific paper-only side-effect tools.  Both default to a
no-broker-call state and require an explicit ``confirm=True`` flag to
invoke ``AlpacaPaperBrokerService.submit_order`` / ``cancel_order``.

These tools are NOT generic.  They never route through ``place_order`` /
``cancel_order`` / ``modify_order``.  There is no parameter that can
switch the underlying service to the live endpoint.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.alpaca_paper_preview import (
    ALPACA_PAPER_CRYPTO_MAX_NOTIONAL_USD,
    PreviewOrderInput,
)
from app.services.alpaca_paper_ledger_service import (
    KNOWN_OPEN_BROKER_STATUSES,
    AlpacaPaperLedgerService,
    normalize_known_broker_order_status,
)
from app.services.alpaca_paper_market_evidence import (
    MarketEvidence,
    MarketEvidenceError,
    load_market_evidence,
)
from app.services.alpaca_paper_submit_service import (
    AlpacaPaperSubmitCoordinator,
    build_canonical_payload,
    canonical_hash,
    derive_client_order_id,
)
from app.services.brokers.alpaca.service import AlpacaPaperBrokerService
from app.services.crypto_execution_mapping import map_alpaca_paper_to_upbit
from app.services.paper_approval_packet import PaperApprovalPacket

if TYPE_CHECKING:
    from fastmcp import FastMCP


ALPACA_PAPER_MUTATING_TOOL_NAMES: set[str] = {
    "alpaca_paper_submit_order",
    "alpaca_paper_cancel_order",
}

SUBMIT_MAX_QTY: Decimal = Decimal("5")
SUBMIT_MAX_NOTIONAL_USD: Decimal = Decimal("1000")
ORDER_ID_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
ORDER_ID_RESERVED_VALUES = frozenset({"all", "order", "orders", "bulk", "cancel"})
_MANUAL_PACKET_TTL_SECONDS = 120
_MANUAL_QUOTE_MAX_AGE = timedelta(minutes=5)

ServiceFactory = Callable[[], AlpacaPaperBrokerService]
SessionFactory = Callable[[], async_sessionmaker[AsyncSession]]


def _default_service_factory() -> AlpacaPaperBrokerService:
    return AlpacaPaperBrokerService()


def _default_session_factory() -> async_sessionmaker[AsyncSession]:
    return AsyncSessionLocal  # type: ignore[return-value]


_service_factory: ServiceFactory = _default_service_factory
_session_factory: SessionFactory = _default_session_factory


def set_alpaca_paper_orders_session_factory(factory: SessionFactory) -> None:
    global _session_factory
    _session_factory = factory


def reset_alpaca_paper_orders_session_factory() -> None:
    global _session_factory
    _session_factory = _default_session_factory


def set_alpaca_paper_orders_service_factory(factory: ServiceFactory) -> None:
    global _service_factory
    _service_factory = factory


def reset_alpaca_paper_orders_service_factory() -> None:
    global _service_factory
    _service_factory = _default_service_factory


def _model_to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, list | tuple):
        return [_model_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {k: _model_to_jsonable(v) for k, v in value.items()}
    return value


def _canonical_payload(validated: PreviewOrderInput) -> dict[str, Any]:
    """Canonical submit payload — shared with the ROB-842 application service."""
    return build_canonical_payload(
        symbol=validated.symbol,
        side=validated.side,
        type=validated.type,
        time_in_force=validated.time_in_force,
        qty=validated.qty,
        notional=validated.notional,
        limit_price=validated.limit_price,
        asset_class=validated.asset_class,
    )


def _derive_client_order_id(payload: dict[str, Any]) -> str:
    """Server-derived deterministic client_order_id (shared, single source)."""
    return derive_client_order_id(payload)


def _validate_exact_order_id(order_id: str) -> str:
    """Return a safe single-order id path segment or fail closed.

    Alpaca cancel uses ``DELETE /v2/orders/{order_id}``. Keep the id as a
    single opaque path segment so values cannot normalize into bulk endpoints
    such as ``/v2/orders`` or add query/fragment/filter semantics.
    """
    stripped = (order_id or "").strip()
    if not stripped:
        raise ValueError("order_id is required")
    if stripped.lower() in ORDER_ID_RESERVED_VALUES:
        raise ValueError("order_id must be an exact Alpaca paper order id")
    if not ORDER_ID_SAFE_SEGMENT_RE.fullmatch(stripped):
        raise ValueError("order_id must be a safe single path segment")
    return stripped


def _manual_ceiling(
    validated: PreviewOrderInput,
) -> tuple[Decimal | None, Decimal | None]:
    """Server hard-cap NOTIONAL ceiling for a manual order (never caller-supplied).

    Always a max_notional so that qty/market orders are bounded by
    ``packet.reference_price × qty`` (the trusted price), not merely a qty cap —
    a qty order at a high reference price must still respect the notional hard cap
    (ROB-842 G2). The separate qty<=5 equity cap is enforced by the tool itself.
    """
    hard_notional = (
        ALPACA_PAPER_CRYPTO_MAX_NOTIONAL_USD
        if validated.asset_class == "crypto"
        else SUBMIT_MAX_NOTIONAL_USD
    )
    return hard_notional, None


def _build_manual_packet(
    validated: PreviewOrderInput,
    canonical: dict[str, Any],
    coid: str,
    evidence: MarketEvidence,
) -> PaperApprovalPacket:
    """Server-built manual operator packet (origin='manual', server-derived key).

    Carries server-observed market evidence (as-of/source/reference price) from a
    trusted snapshot, so the coordinator applies the same market/order checks it
    applies to automated submits — no origin-based bypass.
    """
    if validated.asset_class == "crypto":
        signal_symbol = map_alpaca_paper_to_upbit(validated.symbol)
    else:
        signal_symbol = validated.symbol
    max_notional, max_qty = _manual_ceiling(validated)
    return PaperApprovalPacket(
        signal_source="manual_operator",
        artifact_id=uuid.uuid4(),
        signal_symbol=signal_symbol,
        signal_venue="upbit",
        execution_symbol=validated.symbol,
        execution_venue="alpaca_paper",
        execution_asset_class=validated.asset_class,
        side=validated.side,
        max_notional=max_notional,
        max_qty=max_qty,
        qty_source="manual_operator",
        expected_lifecycle_step="previewed",
        lifecycle_correlation_id=coid,
        client_order_id=coid,
        expires_at=datetime.now(UTC) + timedelta(seconds=_MANUAL_PACKET_TTL_SECONDS),
        account_mode="alpaca_paper",
        origin="manual",
        market_data_asof=evidence.market_data_asof,
        market_data_source=evidence.market_data_source,
        preview_payload_hash=canonical_hash(canonical),
        execution_order_type=validated.type,
        execution_time_in_force=validated.time_in_force,
        reference_price=evidence.price,
    )


async def alpaca_paper_submit_order(
    symbol: str,
    side: str,
    type: str,  # noqa: A002
    quote_snapshot_id: int | None = None,
    qty: Decimal | None = None,
    notional: Decimal | None = None,
    time_in_force: str | None = None,
    limit_price: Decimal | None = None,
    asset_class: str = "us_equity",
    confirm: bool = False,
) -> dict[str, Any]:
    """Submit a single Alpaca PAPER order (us_equity or narrow crypto).

    Defaults to ``confirm=False`` which performs no broker call.

    This is the MANUAL operator tool. It carries no caller-selectable origin,
    client_order_id, or claim mode: the idempotency key is server-derived from the
    canonical order. ``confirm=True`` requires an opaque, server-issued
    ``quote_snapshot_id`` (a trusted ``market_quote_snapshots`` row) so the real
    broker POST — routed through the SAME durable packet + ledger atomic-claim
    coordinator as the automated path — is validated against server-observed
    market evidence and (for sells) the live position. Duplicate manual intents
    POST exactly once, a deterministic broker rejection is terminal, and an
    uncertain outcome is reconciled — never re-POSTed. There is no direct-POST
    fallback and this behaviour does not depend on the automated feature flag.
    """
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

    submit_notional_cap = (
        ALPACA_PAPER_CRYPTO_MAX_NOTIONAL_USD
        if validated.asset_class == "crypto"
        else SUBMIT_MAX_NOTIONAL_USD
    )
    if (
        validated.asset_class != "crypto"
        and validated.qty is not None
        and validated.qty > SUBMIT_MAX_QTY
    ):
        raise ValueError(f"qty {validated.qty} exceeds submit cap ({SUBMIT_MAX_QTY})")
    if validated.notional is not None and validated.notional > submit_notional_cap:
        raise ValueError(
            f"notional {validated.notional} exceeds submit cap ({submit_notional_cap})"
        )
    if (
        validated.qty is not None
        and validated.limit_price is not None
        and validated.qty * validated.limit_price > submit_notional_cap
    ):
        raise ValueError(
            f"estimated_cost {validated.qty * validated.limit_price} "
            f"exceeds submit cap ({submit_notional_cap})"
        )

    canonical = _canonical_payload(validated)
    coid = _derive_client_order_id(canonical)

    if confirm is not True:
        return {
            "success": True,
            "account_mode": "alpaca_paper",
            "source": "alpaca_paper",
            "submitted": False,
            "blocked_reason": "confirmation_required",
            "order_request": canonical,
            "client_order_id": coid,
        }

    # confirm=True — route the real broker POST through the durable boundary. A
    # server-observed market snapshot is REQUIRED (no origin bypass of market
    # evidence).
    if quote_snapshot_id is None:
        return {
            "success": False,
            "account_mode": "alpaca_paper",
            "source": "alpaca_paper",
            "submitted": False,
            "status": "rejected",
            "reason_code": "missing_market_evidence",
            "client_order_id": coid,
            "message": "confirm=True requires a trusted quote_snapshot_id",
        }

    now = datetime.now(UTC)
    async with _session_factory()() as db:
        try:
            evidence = await load_market_evidence(
                db,
                quote_snapshot_id,
                execution_symbol=validated.symbol,
                asset_class=validated.asset_class,
                now=now,
                max_age=_MANUAL_QUOTE_MAX_AGE,
            )
        except MarketEvidenceError as exc:
            return {
                "success": False,
                "account_mode": "alpaca_paper",
                "source": "alpaca_paper",
                "submitted": False,
                "status": "rejected",
                "reason_code": exc.code,
                "client_order_id": coid,
                "message": str(exc),
            }
        packet = _build_manual_packet(validated, canonical, coid, evidence)
        ledger = AlpacaPaperLedgerService(db)
        coordinator = AlpacaPaperSubmitCoordinator(ledger, _service_factory)
        outcome = await coordinator.submit(packet, submit_canonical=canonical)

    return {
        "success": outcome.success,
        "account_mode": "alpaca_paper",
        "source": "alpaca_paper",
        "submitted": outcome.submitted,
        "status": outcome.status,
        "reason_code": outcome.reason_code,
        "order": outcome.order,
        "client_order_id": outcome.client_order_id,
        "message": outcome.message,
    }


async def alpaca_paper_cancel_order(
    order_id: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Cancel exactly one Alpaca PAPER order by id."""
    stripped = _validate_exact_order_id(order_id)

    if confirm is not True:
        return {
            "success": True,
            "account_mode": "alpaca_paper",
            "source": "alpaca_paper",
            "cancelled": False,
            "blocked_reason": "confirmation_required",
            "target_order_id": stripped,
        }

    service = _service_factory()
    await service.cancel_order(stripped)

    # A DELETE 204 only ACCEPTS the cancel request; it is not a terminal
    # `canceled`. Read the order back and act on its actual status (ROB-842 H1).
    order_payload: Any = None
    read_back_status = "ok"
    broker_status: str | None = None
    client_order_id: str | None = None
    try:
        order = await service.get_order(stripped)
        order_payload = _model_to_jsonable(order)
        if isinstance(order_payload, dict):
            broker_status = order_payload.get("status")
            client_order_id = order_payload.get("client_order_id")
    except Exception:  # noqa: BLE001 — read-back is best-effort
        read_back_status = "unavailable"

    # `canceled` is the ONLY status that confirms the cancel released the order.
    # pending_cancel / new / accepted / any open state, and an unavailable/unknown
    # read-back, keep the sell reservation. A fill that raced the cancel is reported
    # truthfully but also keeps its hold until a later position read proves that the
    # fill is reflected. Other known terminal states can safely release the hold.
    normalized_status = normalize_known_broker_order_status(broker_status)
    cancel_confirmed = read_back_status == "ok" and normalized_status == "canceled"
    reservation_released = False
    lifecycle_synced = False
    status_can_release = (
        normalized_status is not None
        and normalized_status not in KNOWN_OPEN_BROKER_STATUSES
        and normalized_status != "filled"
    )
    if client_order_id and read_back_status == "ok" and status_can_release:
        try:
            async with _session_factory()() as db:
                ledger = AlpacaPaperLedgerService(db)
                normalized_payload = dict(order_payload)
                normalized_payload["status"] = normalized_status
                await ledger.record_status(client_order_id, normalized_payload)
                if cancel_confirmed:
                    await ledger.record_cancel(
                        client_order_id, cancel_status="canceled"
                    )
                    reservation_released = True
                lifecycle_synced = True
        except Exception:  # noqa: BLE001 — non-boundary orders have no ledger row
            lifecycle_synced = False

    return {
        "success": True,
        "account_mode": "alpaca_paper",
        "source": "alpaca_paper",
        # honest: cancel_requested was accepted; cancelled is True only once the
        # broker confirms the terminal `canceled` status.
        "cancel_requested": True,
        "cancelled": cancel_confirmed,
        "order_status": broker_status,
        "cancelled_order_id": stripped,
        "order": order_payload,
        "read_back_status": read_back_status,
        "reservation_released": reservation_released,
        "lifecycle_synced": lifecycle_synced,
    }


def register_alpaca_paper_orders_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="alpaca_paper_submit_order",
        description=(
            "MANUAL operator submit for a single Alpaca PAPER us_equity or narrow "
            "crypto order. Defaults to confirm=False which validates and returns "
            "the request WITHOUT calling the broker. confirm=True REQUIRES an "
            "opaque, server-issued quote_snapshot_id (a trusted market_quote_"
            "snapshots row) and routes the real POST through the SAME server-owned "
            "packet + ledger atomic-claim coordinator as the automated path: market "
            "evidence + (for sells) the live position are verified, duplicate "
            "intents POST exactly once, a deterministic broker rejection is "
            "terminal, and an uncertain outcome is reconciled (never re-POSTed). "
            "The idempotency key is server-derived — there is no caller "
            "client_order_id or origin. Paper endpoint only; live endpoint cannot "
            "be selected. Strict caps: us_equity qty<=5/notional<=$1000/"
            "qty*limit_price<=$1000; crypto is buy/sell limit-only, allowlisted, "
            "and capped at $50."
        ),
    )(alpaca_paper_submit_order)
    _ = mcp.tool(
        name="alpaca_paper_cancel_order",
        description=(
            "Cancel exactly ONE Alpaca PAPER order by order_id. "
            "Defaults to confirm=False which returns the target order_id WITHOUT "
            "calling the broker. Use confirm=True to actually cancel. "
            "No bulk/all/by-symbol/by-status options. Paper endpoint only."
        ),
    )(alpaca_paper_cancel_order)


__all__ = [
    "ALPACA_PAPER_MUTATING_TOOL_NAMES",
    "SUBMIT_MAX_NOTIONAL_USD",
    "SUBMIT_MAX_QTY",
    "ORDER_ID_SAFE_SEGMENT_RE",
    "alpaca_paper_cancel_order",
    "alpaca_paper_submit_order",
    "register_alpaca_paper_orders_tools",
    "reset_alpaca_paper_orders_service_factory",
    "set_alpaca_paper_orders_service_factory",
]
