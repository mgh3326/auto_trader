"""Single application service for Alpaca Paper submit (ROB-842).

Routes every automated Alpaca Paper submit through one server-side boundary:

    packet + hash verification  →  existing-ledger atomic claim  →  broker POST

so that a stale/incomplete/mismatched intent can never reach the broker, and a
sequential *or* concurrent duplicate of the same intent results in **exactly one**
broker HTTP submit. The winner of the atomic claim performs the broker call; every
other caller replays the winner's stored result or, while the winner is still
in-flight, returns a structured ``idempotency_in_progress`` without re-POSTing.

This module reuses the existing native ``AlpacaPaperLedgerService`` /
``review.alpaca_paper_order_ledger`` unique lifecycle record — it introduces no new
idempotency store, table, column, or Alembic migration. It never imports or calls a
live Alpaca endpoint or live-credential resolver: the only broker surface it can
build is ``AlpacaPaperBrokerService`` (paper-host-pinned in its constructor).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.models.trading import InstrumentType
from app.services.alpaca_paper_ledger_service import (
    AlpacaPaperLedgerService,
    is_inflight_execution,
)
from app.services.brokers.alpaca.schemas import OrderRequest
from app.services.paper_approval_packet import (
    PaperApprovalPacket,
    PaperApprovalPacketError,
    verify_order_within_packet,
    verify_packet_account_mode,
    verify_packet_freshness,
    verify_packet_market_data,
    verify_preview_submit_hash,
    verify_sell_packet_source,
    verify_server_derived_key,
)

if TYPE_CHECKING:
    from app.services.brokers.alpaca.service import AlpacaPaperBrokerService

BrokerFactory = Callable[[], "AlpacaPaperBrokerService"]

# Default bound on how old the market-data source timestamp may be at submit time.
DEFAULT_QUOTE_MAX_AGE = timedelta(minutes=5)
# Default bounded wait for an in-flight winner before returning in-progress.
DEFAULT_INFLIGHT_MAX_POLLS = 3
DEFAULT_INFLIGHT_POLL_INTERVAL_S = 0.05


# ---------------------------------------------------------------------------
# Shared canonical payload + server-derived key/hash helpers
# ---------------------------------------------------------------------------
def build_canonical_payload(
    *,
    symbol: str,
    side: str,
    type: str,  # noqa: A002
    time_in_force: str | None,
    qty: Decimal | None,
    notional: Decimal | None,
    limit_price: Decimal | None,
    asset_class: str,
) -> dict[str, Any]:
    """Build the canonical, order-independent submit payload.

    Identical shape to the historical ROB-73 canonical payload so derived
    client_order_ids are stable across the preview and submit surfaces.
    """
    return {
        "symbol": symbol,
        "side": side,
        "type": type,
        "time_in_force": time_in_force,
        "qty": str(qty) if qty is not None else None,
        "notional": str(notional) if notional is not None else None,
        "limit_price": str(limit_price) if limit_price is not None else None,
        "asset_class": asset_class,
    }


def _canonical_blob(canonical: dict[str, Any]) -> bytes:
    return json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_hash(canonical: dict[str, Any]) -> str:
    """Full SHA-256 hex digest of the canonical payload (preview↔submit hash)."""
    return hashlib.sha256(_canonical_blob(canonical)).hexdigest()


def derive_client_order_id(canonical: dict[str, Any]) -> str:
    """Deterministic server-derived client_order_id for a canonical payload.

    Preserves the historical ``rob73-``/``rob74-crypto-`` prefixes and 16-char
    digest so existing ledger rows and manual smoke ids remain compatible.

    NOTE: this economics-only key is for the manual operator smoke tool. Automated
    submits use ``derive_automated_key`` which folds in server-owned decision
    identity so distinct decisions never collide on economics alone.
    """
    digest = canonical_hash(canonical)[:16]
    prefix = "rob74-crypto" if canonical.get("asset_class") == "crypto" else "rob73"
    return f"{prefix}-{digest}"


def derive_automated_key(
    *,
    correlation_id: str,
    snapshot_id: str | None,
    canonical: dict[str, Any],
) -> str:
    """Server-owned idempotency key for an automated submit (ROB-842 blocker 4).

    Folds the server-owned decision identity (correlation_id + snapshot_id) into
    the key alongside the economic canonical. Two distinct decisions with identical
    economics get different keys (no permanent collision); a retry of the *same*
    decision gets the same key (dedup). The caller supplies correlation/snapshot as
    decision identity but can never inject the final key directly.
    """
    identity = json.dumps(
        {"c": correlation_id, "s": snapshot_id, "canonical": canonical},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:20]
    prefix = "rob842a-crypto" if canonical.get("asset_class") == "crypto" else "rob842a"
    return f"{prefix}-{digest}"


_ASSET_CLASS_TO_INSTRUMENT: dict[str, InstrumentType] = {
    "crypto": InstrumentType.crypto,
    "us_equity": InstrumentType.equity_us,
}


def _instrument_type_for(asset_class: str | None) -> InstrumentType:
    return _ASSET_CLASS_TO_INSTRUMENT.get(
        asset_class or "us_equity", InstrumentType.equity_us
    )


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SubmitOutcome:
    """Result of routing one submit through the boundary.

    status:
        submitted                — this call won the claim and POSTed to the broker.
        replayed                 — a prior completed submit's stored result is returned;
                                   no broker call was made.
        recovered                — an in-flight claim was reconciled from broker
                                   evidence (submit had reached the broker but the DB
                                   write was lost); booked without re-POSTing.
        idempotency_in_progress  — another caller owns the in-flight submit; bounded
                                   wait + broker reconcile found nothing to book; no
                                   broker call.
        rejected                 — packet/hash/key/order verification failed before any
                                   claim; no broker call.
    """

    status: str
    client_order_id: str
    broker_called: bool
    reason_code: str | None = None
    order: dict[str, Any] | None = None
    message: str | None = None

    @property
    def submitted(self) -> bool:
        return self.status == "submitted"


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------
class AlpacaPaperSubmitCoordinator:
    """Server-side submit boundary composing packet verify + claim + broker POST."""

    def __init__(
        self,
        ledger: AlpacaPaperLedgerService,
        broker_factory: BrokerFactory,
        *,
        now_fn: Callable[[], datetime] | None = None,
        quote_max_age: timedelta = DEFAULT_QUOTE_MAX_AGE,
        expected_account_mode: str = "alpaca_paper",
        inflight_max_polls: int = DEFAULT_INFLIGHT_MAX_POLLS,
        inflight_poll_interval_s: float = DEFAULT_INFLIGHT_POLL_INTERVAL_S,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._ledger = ledger
        self._broker_factory = broker_factory
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._quote_max_age = quote_max_age
        self._expected_account_mode = expected_account_mode
        self._inflight_max_polls = max(1, int(inflight_max_polls))
        self._inflight_poll_interval_s = float(inflight_poll_interval_s)
        self._sleep_fn = sleep_fn or asyncio.sleep

    async def submit(
        self,
        packet: PaperApprovalPacket,
        *,
        submit_canonical: dict[str, Any],
        caller_client_order_id: str | None = None,
    ) -> SubmitOutcome:
        """Verify, claim and (for the winner only) submit the packet's order."""
        coid = packet.client_order_id

        # --- Fail-close verification (all before any claim / broker call) -------
        try:
            server_key = derive_automated_key(
                correlation_id=packet.lifecycle_correlation_id,
                snapshot_id=packet.snapshot_id,
                canonical=submit_canonical,
            )
            verify_server_derived_key(
                packet,
                server_key=server_key,
                caller_client_order_id=caller_client_order_id,
            )
            verify_order_within_packet(packet, submit_canonical)
            verify_preview_submit_hash(
                packet, submit_hash=canonical_hash(submit_canonical)
            )
            now = self._now_fn()
            verify_packet_freshness(packet, now=now)
            verify_packet_market_data(packet, now=now, max_age=self._quote_max_age)
            verify_packet_account_mode(packet, expected=self._expected_account_mode)
            await verify_sell_packet_source(packet, ledger=self._ledger)
        except PaperApprovalPacketError as exc:
            return SubmitOutcome(
                status="rejected",
                client_order_id=coid,
                broker_called=False,
                reason_code=exc.code,
                message=str(exc),
            )

        # --- Idempotency fast-path (avoid a claim attempt when possible) --------
        # Force a fresh READ COMMITTED snapshot: the session uses
        # expire_on_commit=False, so a prior same-session submit's committed row
        # would otherwise be read back stale from the identity map.
        self._ledger.session.expire_all()
        existing = await self._ledger.find_executed_by_client_order_id(coid)
        if existing is not None:
            if is_inflight_execution(existing):
                return await self._resolve_inflight(coid)
            return self._replay_outcome(existing)

        # --- Atomic claim: only the winner POSTs to the broker ------------------
        claim = await self._ledger.claim_submit(
            client_order_id=coid,
            lifecycle_correlation_id=packet.lifecycle_correlation_id,
            execution_symbol=packet.execution_symbol,
            execution_venue=packet.execution_venue,
            instrument_type=_instrument_type_for(packet.execution_asset_class),
            side=packet.side,
            order_type=str(submit_canonical.get("type") or "limit"),
            time_in_force=submit_canonical.get("time_in_force"),
            requested_qty=_to_decimal(submit_canonical.get("qty")),
            requested_notional=_to_decimal(submit_canonical.get("notional")),
            requested_price=_to_decimal(submit_canonical.get("limit_price")),
            preview_payload=dict(submit_canonical),
        )
        if not claim.won:
            row = claim.row
            if row is not None and not is_inflight_execution(row):
                return self._replay_outcome(row)
            return await self._resolve_inflight(coid)

        # --- Winner: exactly one broker HTTP submit -----------------------------
        broker = self._broker_factory()
        request = OrderRequest(
            symbol=submit_canonical["symbol"],
            side=submit_canonical["side"],
            type=submit_canonical["type"],
            qty=_to_decimal(submit_canonical.get("qty")),
            notional=_to_decimal(submit_canonical.get("notional")),
            time_in_force=submit_canonical.get("time_in_force") or "day",
            limit_price=_to_decimal(submit_canonical.get("limit_price")),
            stop_price=None,
            client_order_id=coid,
        )
        order = await broker.submit_order(request)
        order_dict = _order_to_dict(order)
        await self._ledger.record_submit(coid, order_dict, raw_response=order_dict)
        return SubmitOutcome(
            status="submitted",
            client_order_id=coid,
            broker_called=True,
            order=order_dict,
        )

    # ------------------------------------------------------------------
    async def _resolve_inflight(self, client_order_id: str) -> SubmitOutcome:
        """Resolve an in-flight claim without ever re-POSTing.

        1. Bounded local wait for a concurrent winner's committed record_submit.
        2. If still unresolved, reconcile against the broker by client_order_id —
           a submit that reached the broker but crashed before the DB write is
           recovered here (booked from broker evidence), so it never stays
           permanently ``idempotency_in_progress``.
        3. Only if the broker has no such order do we return in-progress.
        """
        for _ in range(self._inflight_max_polls):
            # New READ COMMITTED snapshot each poll so a concurrent winner's
            # committed record_submit becomes visible.
            self._ledger.session.expire_all()
            row = await self._ledger.find_executed_by_client_order_id(client_order_id)
            if row is not None and not is_inflight_execution(row):
                return self._replay_outcome(row)
            await self._sleep_fn(self._inflight_poll_interval_s)

        recovered = await self._reconcile_inflight_via_broker(client_order_id)
        if recovered is not None:
            return recovered

        return SubmitOutcome(
            status="idempotency_in_progress",
            client_order_id=client_order_id,
            broker_called=False,
            reason_code="idempotency_in_progress",
            message="another caller owns the in-flight submit for this intent",
        )

    async def _reconcile_inflight_via_broker(
        self, client_order_id: str
    ) -> SubmitOutcome | None:
        """Book a crashed-after-success submit from broker evidence. Never POSTs.

        Returns a ``recovered`` outcome if the broker has an order for this
        client_order_id (submit succeeded but the DB write was lost), else None.
        """
        broker = self._broker_factory()
        getter = getattr(broker, "get_order_by_client_order_id", None)
        if getter is None:
            return None
        order = await getter(client_order_id)
        if order is None:
            return None
        order_dict = _order_to_dict(order)
        await self._ledger.record_submit(
            client_order_id, order_dict, raw_response=order_dict
        )
        return SubmitOutcome(
            status="recovered",
            client_order_id=client_order_id,
            broker_called=False,
            reason_code="recovered_via_broker_lookup",
            order=order_dict,
            message="crashed-after-success submit recovered from broker evidence",
        )

    def _replay_outcome(self, row: Any) -> SubmitOutcome:
        stored = None
        raw = getattr(row, "raw_responses", None)
        if isinstance(raw, dict):
            stored = raw.get("submit")
        return SubmitOutcome(
            status="replayed",
            client_order_id=str(getattr(row, "client_order_id", "")),
            broker_called=False,
            reason_code="duplicate_submit_replayed",
            order=stored,
            message=(
                f"replayed stored submit result in state "
                f"{getattr(row, 'lifecycle_state', None)!r}"
            ),
        )


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _order_to_dict(order: Any) -> dict[str, Any]:
    if hasattr(order, "model_dump"):
        return order.model_dump(mode="json")
    if isinstance(order, dict):
        return order
    raise TypeError(f"unexpected broker order type: {type(order)!r}")


__all__ = [
    "AlpacaPaperSubmitCoordinator",
    "BrokerFactory",
    "DEFAULT_INFLIGHT_MAX_POLLS",
    "DEFAULT_INFLIGHT_POLL_INTERVAL_S",
    "DEFAULT_QUOTE_MAX_AGE",
    "SubmitOutcome",
    "build_canonical_payload",
    "canonical_hash",
    "derive_automated_key",
    "derive_client_order_id",
]
