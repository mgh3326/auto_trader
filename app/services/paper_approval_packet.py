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
from datetime import datetime, timedelta
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
        missing_source_timestamp  — market_data_asof absent on an automated packet
        naive_source_timestamp    — market_data_asof is tz-naive
        future_source_timestamp   — market_data_asof is in the future relative to now
        missing_market_data_source — market_data_source label absent
        stale_quote               — market_data_asof older than the allowed max age
        account_mode_mismatch     — packet.account_mode != expected (alpaca_paper)
        missing_preview_hash      — preview_payload_hash absent when a submit hash is checked
        preview_hash_mismatch     — submit canonical hash != packet.preview_payload_hash
        server_key_mismatch       — client_order_id != server-derived key for the canonical intent
        caller_id_mismatch        — caller-supplied client_order_id != server-derived key
        order_symbol_mismatch     — submit symbol != packet.execution_symbol
        order_side_mismatch       — submit side != packet.side
        order_asset_class_mismatch — submit asset_class != packet.execution_asset_class
        order_type_mismatch       — submit order type != packet.execution_order_type
        order_tif_mismatch        — submit TIF != packet.execution_time_in_force
        order_missing_size        — submit has neither qty nor notional
        notional_exceeds_max      — submit notional (or qty*limit) > packet.max_notional
        qty_exceeds_max           — submit qty > packet.max_qty
        source_filled_qty_unknown — sell source filled_qty missing/unparseable/non-positive
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

    # ROB-842: Alpaca-complete boundary fields. Optional so ROB-91 producers stay
    # valid; the Alpaca submit coordinator requires the market-data + hash fields
    # via its verifiers (missing values fail-close with a stable reason code).
    account_mode: str = "alpaca_paper"
    origin: Literal["manual", "automated"] = "manual"
    market_data_asof: datetime | None = None
    market_data_source: str | None = None
    preview_payload_hash: str | None = None
    # ROB-842 blocker 4: server-owned snapshot identity folded into the
    # idempotency key so distinct decisions never collide on economics alone.
    snapshot_id: str | None = None
    execution_order_type: Literal["limit", "market"] | None = None
    execution_time_in_force: str | None = None
    # ROB-842: trusted reference price (from the server-observed market snapshot),
    # used to bound notional for qty/market orders that carry no limit price.
    reference_price: Decimal | None = None

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

    # ROB-842 blocker 5: only a confirmed real holding may back a sell. A source
    # with missing / unparseable / non-positive filled_qty fails closed.
    source_filled_qty_raw = _get_attr(source, "filled_qty")
    if source_filled_qty_raw is None:
        raise PaperApprovalPacketError(
            code="source_filled_qty_unknown",
            message=(
                "sell source has no filled_qty; a confirmed holding is required "
                "before selling"
            ),
        )
    try:
        source_filled_qty = Decimal(str(source_filled_qty_raw))
    except Exception as exc:
        raise PaperApprovalPacketError(
            code="source_filled_qty_unknown",
            message=f"sell source filled_qty is unparseable: {source_filled_qty_raw!r}",
        ) from exc
    if source_filled_qty <= 0:
        raise PaperApprovalPacketError(
            code="source_filled_qty_unknown",
            message=f"sell source filled_qty is non-positive: {source_filled_qty}",
        )
    if packet.max_qty is not None and packet.max_qty > source_filled_qty:
        raise PaperApprovalPacketError(
            code="qty_exceeds_source",
            message=(
                f"sell max_qty {packet.max_qty} exceeds buy source filled_qty "
                f"{source_filled_qty}"
            ),
        )


# ---------------------------------------------------------------------------
# Verifier: market-data source freshness (ROB-842)
# ---------------------------------------------------------------------------
def verify_packet_market_data(
    packet: PaperApprovalPacket,
    *,
    now: datetime,
    max_age: timedelta,
) -> None:
    """Reject packets whose market-data source timestamp is missing or stale.

    Distinct from ``verify_packet_freshness`` (which bounds ``expires_at``): this
    guards the *as-of* time of the quote the decision was made against, so an
    otherwise-unexpired packet built on a stale quote is fail-closed.

    Raises:
        PaperApprovalPacketError(code='naive_now')                if now is tz-naive.
        PaperApprovalPacketError(code='missing_source_timestamp') if asof is None.
        PaperApprovalPacketError(code='naive_source_timestamp')   if asof is tz-naive.
        PaperApprovalPacketError(code='stale_quote')              if now - asof > max_age.
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise PaperApprovalPacketError(
            code="naive_now",
            message="now must be timezone-aware; use a caller-supplied UTC clock",
        )
    asof = packet.market_data_asof
    if asof is None:
        raise PaperApprovalPacketError(
            code="missing_source_timestamp",
            message="market_data_asof is required for an automated submit packet",
        )
    if asof.tzinfo is None or asof.tzinfo.utcoffset(asof) is None:
        raise PaperApprovalPacketError(
            code="naive_source_timestamp",
            message="market_data_asof must be timezone-aware",
        )
    if not (packet.market_data_source or "").strip():
        raise PaperApprovalPacketError(
            code="missing_market_data_source",
            message="market_data_source label is required for an automated packet",
        )
    if asof > now:
        raise PaperApprovalPacketError(
            code="future_source_timestamp",
            message=(
                f"market_data_asof is in the future: asof={asof.isoformat()}, "
                f"now={now.isoformat()}"
            ),
        )
    if now - asof > max_age:
        raise PaperApprovalPacketError(
            code="stale_quote",
            message=(
                f"market data is stale: asof={asof.isoformat()}, now={now.isoformat()}, "
                f"max_age={max_age}"
            ),
        )


# ---------------------------------------------------------------------------
# Verifier: account mode (ROB-842)
# ---------------------------------------------------------------------------
def verify_packet_account_mode(
    packet: PaperApprovalPacket,
    *,
    expected: str = "alpaca_paper",
) -> None:
    """Raise if the packet account_mode does not match the expected paper mode.

    Raises:
        PaperApprovalPacketError(code='account_mode_mismatch').
    """
    if packet.account_mode != expected:
        raise PaperApprovalPacketError(
            code="account_mode_mismatch",
            message=(
                f"packet account_mode {packet.account_mode!r} does not match "
                f"expected {expected!r}"
            ),
        )


# ---------------------------------------------------------------------------
# Verifier: preview↔submit payload hash (ROB-842)
# ---------------------------------------------------------------------------
def verify_preview_submit_hash(
    packet: PaperApprovalPacket,
    *,
    submit_hash: str,
) -> None:
    """Raise if the submit canonical hash differs from the packet's preview hash.

    Raises:
        PaperApprovalPacketError(code='missing_preview_hash')  if packet has no hash.
        PaperApprovalPacketError(code='preview_hash_mismatch') if hashes differ.
    """
    if not packet.preview_payload_hash:
        raise PaperApprovalPacketError(
            code="missing_preview_hash",
            message="packet has no preview_payload_hash to verify against",
        )
    if packet.preview_payload_hash != submit_hash:
        raise PaperApprovalPacketError(
            code="preview_hash_mismatch",
            message=(
                "submit payload hash does not match the previewed payload hash: "
                f"preview={packet.preview_payload_hash!r}, submit={submit_hash!r}"
            ),
        )


# ---------------------------------------------------------------------------
# Verifier: server-derived client_order_id / caller-id bypass guard (ROB-842)
# ---------------------------------------------------------------------------
def verify_server_derived_key(
    packet: PaperApprovalPacket,
    *,
    server_key: str,
    caller_client_order_id: str | None = None,
) -> None:
    """Enforce the server-derived idempotency key for the canonical intent.

    The packet's ``client_order_id`` must equal the server-derived key for the
    canonical submit payload, and any caller-supplied client_order_id must equal
    that same server-derived value — an automated request cannot inject an id that
    bypasses the server-derived claim key.

    Raises:
        PaperApprovalPacketError(code='server_key_mismatch') if packet id != server key.
        PaperApprovalPacketError(code='caller_id_mismatch')  if caller id != server key.
    """
    if packet.client_order_id != server_key:
        raise PaperApprovalPacketError(
            code="server_key_mismatch",
            message=(
                f"packet client_order_id {packet.client_order_id!r} is not the "
                f"server-derived key {server_key!r} for this canonical intent"
            ),
        )
    if caller_client_order_id is not None and caller_client_order_id != server_key:
        raise PaperApprovalPacketError(
            code="caller_id_mismatch",
            message=(
                f"caller-supplied client_order_id {caller_client_order_id!r} does not "
                f"match the server-derived key {server_key!r}"
            ),
        )


# ---------------------------------------------------------------------------
# Verifier: order-within-packet authority binding (ROB-842 blocker 3)
# ---------------------------------------------------------------------------
def _canonical_decimal(canonical: dict[str, Any], key: str) -> Decimal | None:
    raw = canonical.get(key)
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except Exception as exc:
        raise PaperApprovalPacketError(
            code="order_size_unverifiable",
            message=f"canonical {key!r} is not a parseable number: {raw!r}",
        ) from exc


def verify_order_within_packet(
    packet: PaperApprovalPacket,
    canonical: dict[str, Any],
) -> None:
    """Bind the submit order to the server-owned packet's authority.

    Every economic field of the submit (symbol, side, asset class, order type,
    TIF, qty/notional, limit price) must match the packet, and the order size must
    fall within the packet's approved ceiling (``max_notional``/``max_qty``). This
    is what stops a self-consistent caller-fabricated canonical (hash-matches
    itself) from executing beyond the server-approved decision.

    Raises PaperApprovalPacketError with one of the order_* / *_exceeds_max codes.
    """
    if str(canonical.get("symbol") or "") != packet.execution_symbol:
        raise PaperApprovalPacketError(
            code="order_symbol_mismatch",
            message=(
                f"submit symbol {canonical.get('symbol')!r} != packet "
                f"execution_symbol {packet.execution_symbol!r}"
            ),
        )
    if str(canonical.get("side") or "") != packet.side:
        raise PaperApprovalPacketError(
            code="order_side_mismatch",
            message=f"submit side {canonical.get('side')!r} != packet side {packet.side!r}",
        )
    if str(canonical.get("asset_class") or "") != packet.execution_asset_class:
        raise PaperApprovalPacketError(
            code="order_asset_class_mismatch",
            message=(
                f"submit asset_class {canonical.get('asset_class')!r} != packet "
                f"execution_asset_class {packet.execution_asset_class!r}"
            ),
        )
    if (
        packet.execution_order_type is not None
        and str(canonical.get("type") or "") != packet.execution_order_type
    ):
        raise PaperApprovalPacketError(
            code="order_type_mismatch",
            message=(
                f"submit order type {canonical.get('type')!r} != packet "
                f"execution_order_type {packet.execution_order_type!r}"
            ),
        )
    if (
        packet.execution_time_in_force is not None
        and str(canonical.get("time_in_force") or "") != packet.execution_time_in_force
    ):
        raise PaperApprovalPacketError(
            code="order_tif_mismatch",
            message=(
                f"submit time_in_force {canonical.get('time_in_force')!r} != packet "
                f"execution_time_in_force {packet.execution_time_in_force!r}"
            ),
        )

    qty = _canonical_decimal(canonical, "qty")
    notional = _canonical_decimal(canonical, "notional")
    limit_price = _canonical_decimal(canonical, "limit_price")
    if qty is None and notional is None:
        raise PaperApprovalPacketError(
            code="order_missing_size",
            message="submit canonical has neither qty nor notional",
        )

    if packet.max_notional is not None:
        effective_notional = notional
        if effective_notional is None and qty is not None:
            # Prefer the order's own limit price; fall back to the packet's trusted
            # reference price so a market/qty order is still bounded by notional.
            px = limit_price if limit_price is not None else packet.reference_price
            if px is not None:
                effective_notional = qty * px
        if effective_notional is None:
            raise PaperApprovalPacketError(
                code="order_size_unverifiable",
                message="cannot bound order notional against packet.max_notional",
            )
        if effective_notional > packet.max_notional:
            raise PaperApprovalPacketError(
                code="notional_exceeds_max",
                message=(
                    f"submit notional {effective_notional} exceeds packet "
                    f"max_notional {packet.max_notional}"
                ),
            )

    if packet.max_qty is not None:
        if qty is None:
            raise PaperApprovalPacketError(
                code="order_size_unverifiable",
                message="packet has a qty ceiling but submit provided no qty",
            )
        if qty > packet.max_qty:
            raise PaperApprovalPacketError(
                code="qty_exceeds_max",
                message=f"submit qty {qty} exceeds packet max_qty {packet.max_qty}",
            )


__all__ = [
    "PaperApprovalPacket",
    "PaperApprovalPacketError",
    "verify_order_within_packet",
    "verify_packet_account_mode",
    "verify_packet_freshness",
    "verify_packet_idempotency",
    "verify_packet_market_data",
    "verify_preview_submit_hash",
    "verify_sell_packet_source",
    "verify_server_derived_key",
]
