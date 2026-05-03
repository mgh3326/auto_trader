"""Bounded, frozen paper approval packet and deterministic verifiers (ROB-91).

This module is intentionally pure and side-effect free:
- No broker calls, no DB writes, no scheduler/network work.
- Verifiers never read the clock themselves — callers supply wall-clock via `now=`.
- Verifiers consume already-fetched ledger snapshots from the caller.

Usage pattern (producer side):
    packet = PaperApprovalPacket(
        signal_source="preopen_briefing",
        artifact_id=uuid.uuid4(),
        signal_symbol="KRW-BTC",
        signal_venue="upbit",
        execution_symbol="BTC/USD",
        execution_venue="alpaca_paper",
        execution_asset_class="crypto",
        side="buy",
        max_notional=Decimal("10"),
        qty_source="notional_estimate",
        expected_lifecycle_step="previewed",
        lifecycle_correlation_id="corr-abc",
        client_order_id="buy-001",
        expires_at=datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC),
    )

Usage pattern (pre-submit caller):
    caller_now = ...  # timezone-aware wall clock supplied by the caller
    verify_packet_freshness(packet, now=caller_now)
    await verify_packet_idempotency(packet, ledger=svc)
    await verify_sell_packet_source(packet, ledger=svc)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

if TYPE_CHECKING:
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

# ---------------------------------------------------------------------------
# Pre-submit lifecycle steps allowed in a packet
# ---------------------------------------------------------------------------
_PRE_SUBMIT_STEPS: frozenset[str] = frozenset(
    {"planned", "previewed", "validated", "submitted"}
)

# ---------------------------------------------------------------------------
# Allowed qty_source values for sell packets (ledger/reconcile-derived only)
# ---------------------------------------------------------------------------
_SELL_QTY_SOURCES: frozenset[str] = frozenset(
    {
        "ledger_filled_qty",
        "ledger_position_snapshot",
        "reconcile_filled_qty",
        "reconcile_position_snapshot",
    }
)

# ---------------------------------------------------------------------------
# Reconciled buy states: conservative set — position must be confirmed
# ---------------------------------------------------------------------------
_RECONCILED_BUY_STATES: frozenset[str] = frozenset(
    {
        "position_reconciled",
        "closed",
        "final_reconciled",
        # Include filled only for conservative completeness; callers with
        # position_reconciled rows are preferred.
        "filled",
    }
)


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------
class PaperApprovalPacketError(ValueError):
    """Raised by packet verifiers with a stable machine-readable code.

    Stable code values:
        stale_packet              — expires_at is in the past relative to now
        naive_now                 — caller supplied a naive (tz-unaware) now
        duplicate_client_order_id — client_order_id already executed in ledger
        missing_source_order      — sell packet has no prior buy in ledger
        multiple_source_orders    — sell packet has >1 buy execution rows
        source_not_reconciled     — buy source not in a reconciled lifecycle state
        wrong_symbol              — execution_symbol doesn't match buy source row
        qty_exceeds_source        — sell max_qty > source buy filled_qty
        invalid_qty_source        — sell qty_source not in allowed ledger values
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)

    def __repr__(self) -> str:
        return f"PaperApprovalPacketError(code={self.code!r}, message={str(self)!r})"


# ---------------------------------------------------------------------------
# Packet schema
# ---------------------------------------------------------------------------
class PaperApprovalPacket(BaseModel):
    """Frozen, bounded approval packet for a single Alpaca Paper order leg.

    Exactly one of max_notional or max_qty must be provided and positive.
    expires_at must be timezone-aware.
    Unknown fields are forbidden.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_source: str
    artifact_id: uuid.UUID
    signal_symbol: str
    signal_venue: Literal["upbit"]
    execution_symbol: str
    execution_venue: Literal["alpaca_paper"]
    execution_asset_class: Literal["crypto", "us_equity"]
    side: Literal["buy", "sell"]
    max_notional: Decimal | None = None
    max_qty: Decimal | None = None
    qty_source: str
    expected_lifecycle_step: str
    lifecycle_correlation_id: str
    client_order_id: str
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def _require_aware_expires_at(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            raise ValueError("expires_at must be timezone-aware")
        return v

    @field_validator("expected_lifecycle_step")
    @classmethod
    def _require_pre_submit_step(cls, v: str) -> str:
        if v not in _PRE_SUBMIT_STEPS:
            raise ValueError(
                f"expected_lifecycle_step must be one of {sorted(_PRE_SUBMIT_STEPS)}; got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _require_exactly_one_max_guard(self) -> PaperApprovalPacket:
        has_notional = self.max_notional is not None
        has_qty = self.max_qty is not None
        if has_notional and has_qty:
            raise ValueError("Provide exactly one of max_notional or max_qty, not both")
        if not has_notional and not has_qty:
            raise ValueError("Provide exactly one of max_notional or max_qty")
        if has_notional and self.max_notional <= Decimal("0"):
            raise ValueError("max_notional must be positive")
        if has_qty and self.max_qty <= Decimal("0"):
            raise ValueError("max_qty must be positive")
        return self

    @model_validator(mode="after")
    def _validate_crypto_symbol_mapping(self) -> PaperApprovalPacket:
        if self.signal_venue == "upbit" and self.execution_asset_class == "crypto":
            from app.services.crypto_execution_mapping import (
                CryptoExecutionMappingError,
                map_upbit_to_alpaca_paper,
            )

            try:
                mapping = map_upbit_to_alpaca_paper(self.signal_symbol)
            except CryptoExecutionMappingError as exc:
                raise ValueError(
                    f"signal_symbol {self.signal_symbol!r} is not supported for Upbit→Alpaca Paper mapping: {exc}"
                ) from exc
            if mapping.execution_symbol != self.execution_symbol:
                raise ValueError(
                    f"execution_symbol {self.execution_symbol!r} does not match "
                    f"expected {mapping.execution_symbol!r} for signal {self.signal_symbol!r}"
                )
        return self


# ---------------------------------------------------------------------------
# Verifier: freshness
# ---------------------------------------------------------------------------
def verify_packet_freshness(packet: PaperApprovalPacket, *, now: datetime) -> None:
    """Raise PaperApprovalPacketError if the packet is expired or now is naive.

    Args:
        packet: The approval packet to verify.
        now: Caller-supplied wall clock. Must be timezone-aware.

    Raises:
        PaperApprovalPacketError(code='naive_now') if now has no tzinfo.
        PaperApprovalPacketError(code='stale_packet') if now >= packet.expires_at.
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise PaperApprovalPacketError(
            code="naive_now",
            message="now must be timezone-aware; use a caller-supplied UTC clock",
        )
    if now >= packet.expires_at:
        raise PaperApprovalPacketError(
            code="stale_packet",
            message=(
                f"packet expired: expires_at={packet.expires_at.isoformat()}, "
                f"now={now.isoformat()}"
            ),
        )


# ---------------------------------------------------------------------------
# Verifier: idempotency
# ---------------------------------------------------------------------------
async def verify_packet_idempotency(
    packet: PaperApprovalPacket,
    *,
    ledger: AlpacaPaperLedgerService,
) -> None:
    """Raise PaperApprovalPacketError if client_order_id already executed.

    Calls ledger.find_executed_by_client_order_id to check for a prior
    execution row in an executed lifecycle state.

    Raises:
        PaperApprovalPacketError(code='duplicate_client_order_id').
    """
    existing = await ledger.find_executed_by_client_order_id(packet.client_order_id)
    if existing is not None:
        raise PaperApprovalPacketError(
            code="duplicate_client_order_id",
            message=(
                f"client_order_id {packet.client_order_id!r} already has an execution "
                f"row in state {existing.lifecycle_state!r}"
            ),
        )


# ---------------------------------------------------------------------------
# Verifier: sell source order
# ---------------------------------------------------------------------------
def _get_attr(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


async def verify_sell_packet_source(
    packet: PaperApprovalPacket,
    *,
    ledger: AlpacaPaperLedgerService,
) -> None:
    """For sell packets, verify exactly one prior reconciled buy source exists.

    Buy packets pass without any source check.

    For sell packets:
    - qty_source must be a ledger/reconcile-derived value.
    - Calls ledger.list_by_correlation_id to retrieve the correlation scope.
    - Exactly one buy execution row must exist.
    - That buy row must be in a reconciled state (position_reconciled / filled / closed / final_reconciled).
    - execution_symbol must match between packet and source row.
    - packet.max_qty (if set) must not exceed the source buy filled_qty.

    Raises:
        PaperApprovalPacketError with one of the stable codes:
            invalid_qty_source, missing_source_order, multiple_source_orders,
            source_not_reconciled, wrong_symbol, qty_exceeds_source.
    """
    if packet.side == "buy":
        return

    # Sell packet: validate qty_source first
    if packet.qty_source not in _SELL_QTY_SOURCES:
        raise PaperApprovalPacketError(
            code="invalid_qty_source",
            message=(
                f"sell packet qty_source {packet.qty_source!r} is not allowed; "
                f"must be one of {sorted(_SELL_QTY_SOURCES)}"
            ),
        )

    rows = await ledger.list_by_correlation_id(packet.lifecycle_correlation_id)

    # Filter to buy execution rows
    buy_exec_rows = [
        row
        for row in rows
        if str(_get_attr(row, "side") or "").lower() == "buy"
        and str(_get_attr(row, "record_kind") or "") == "execution"
    ]

    if not buy_exec_rows:
        raise PaperApprovalPacketError(
            code="missing_source_order",
            message=(
                f"no buy execution row found for lifecycle_correlation_id "
                f"{packet.lifecycle_correlation_id!r}"
            ),
        )

    if len(buy_exec_rows) > 1:
        raise PaperApprovalPacketError(
            code="multiple_source_orders",
            message=(
                f"expected exactly one buy execution row for correlation "
                f"{packet.lifecycle_correlation_id!r}; found {len(buy_exec_rows)}"
            ),
        )

    source = buy_exec_rows[0]
    source_state = str(_get_attr(source, "lifecycle_state") or "")
    if source_state not in _RECONCILED_BUY_STATES:
        raise PaperApprovalPacketError(
            code="source_not_reconciled",
            message=(
                f"buy source lifecycle_state is {source_state!r}; "
                f"must be in {sorted(_RECONCILED_BUY_STATES)}"
            ),
        )

    source_symbol = str(_get_attr(source, "execution_symbol") or "")
    if source_symbol != packet.execution_symbol:
        raise PaperApprovalPacketError(
            code="wrong_symbol",
            message=(
                f"sell packet execution_symbol {packet.execution_symbol!r} does not match "
                f"buy source execution_symbol {source_symbol!r}"
            ),
        )

    if packet.max_qty is not None:
        source_filled_qty_raw = _get_attr(source, "filled_qty")
        if source_filled_qty_raw is not None:
            try:
                source_filled_qty = Decimal(str(source_filled_qty_raw))
            except Exception:
                source_filled_qty = None
            if source_filled_qty is not None and packet.max_qty > source_filled_qty:
                raise PaperApprovalPacketError(
                    code="qty_exceeds_source",
                    message=(
                        f"sell max_qty {packet.max_qty} exceeds buy source filled_qty "
                        f"{source_filled_qty}"
                    ),
                )


__all__ = [
    "PaperApprovalPacket",
    "PaperApprovalPacketError",
    "verify_packet_freshness",
    "verify_packet_idempotency",
    "verify_sell_packet_source",
]
