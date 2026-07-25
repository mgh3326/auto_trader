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
    KNOWN_OPEN_BROKER_STATUSES,
    LIFECYCLE_SUBMITTED,
    AlpacaPaperLedgerService,
    _redact_sensitive_text,
    is_inflight_execution,
    normalize_known_broker_order_status,
)
from app.services.brokers.alpaca.exceptions import AlpacaPaperRequestError
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


def _extract_and_sanitize_error_body(exc: Exception) -> str:
    """Extract, sanitize, and truncate the error body from AlpacaPaperRequestError.

    1. Excerpts the raw response body from HTTP status prefix.
    2. Limits incoming body size to 2000 characters before regex matching to avoid engine load.
    3. Sanitizes sensitive text using existing ledger utility.
    4. Conservatively masks any remaining alphanumeric tokens of length 20+ containing digits
       to protect secrets, keys, and high-entropy tokens, while preserving UUIDs and general non-digit identifiers.
    5. Truncates to 500 characters.
    """
    import re

    msg = str(exc)
    body = msg
    if msg.startswith("HTTP "):
        parts = msg.split(": ", 1)
        if len(parts) > 1:
            body = parts[1]

    # Pre-truncate to 2000 characters to bound regex evaluation time
    body_short = body[:2000]

    redacted = _redact_sensitive_text(body_short) or ""

    def replace_token(match) -> str:
        val = match.group(0)
        # Only mask tokens that contain at least one digit
        if any(c.isdigit() for c in val):
            return "[MASKED_TOKEN]"
        return val

    masked = re.sub(r"[A-Za-z0-9]{20,}", replace_token, redacted)
    return masked[:500]


# Default bound on how old the market-data source timestamp may be at submit time.
DEFAULT_QUOTE_MAX_AGE = timedelta(minutes=5)
# Default bounded wait for an in-flight winner before returning in-progress.
DEFAULT_INFLIGHT_MAX_POLLS = 3
DEFAULT_INFLIGHT_POLL_INTERVAL_S = 0.05

# ROB-842 public success contract — only these statuses are a success.
SUCCESS_STATUSES: frozenset[str] = frozenset({"submitted", "replayed", "recovered"})


def _sell_submit_lifecycle_override(status: Any) -> str | None:
    """Retain a sell hold until broker truth is safe to release.

    Open/partial, filled-without-position-proof, and unknown/unparseable statuses
    all remain submitted. Only a recognized non-fill terminal status may derive a
    terminal lifecycle directly from submit/recovery evidence.
    """
    normalized = normalize_known_broker_order_status(status)
    if (
        normalized is None
        or normalized in KNOWN_OPEN_BROKER_STATUSES
        or normalized == "filled"
    ):
        return LIFECYCLE_SUBMITTED
    return None


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

    @property
    def success(self) -> bool:
        """Public success contract: a real broker-side success or a faithful
        replay/recovery of one. failed / rejected / idempotency_in_progress are
        NOT success."""
        return self.status in SUCCESS_STATUSES


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

    def _server_key_for(
        self, packet: PaperApprovalPacket, canonical: dict[str, Any]
    ) -> str:
        """Origin-aware server-derived claim key.

        Automated submits fold server-owned decision identity into the key;
        manual operator submits use the economics-only key.
        """
        if packet.origin == "automated":
            return derive_automated_key(
                correlation_id=packet.lifecycle_correlation_id,
                snapshot_id=packet.snapshot_id,
                canonical=canonical,
            )
        return derive_client_order_id(canonical)

    async def submit(
        self,
        packet: PaperApprovalPacket,
        *,
        submit_canonical: dict[str, Any],
        caller_client_order_id: str | None = None,
    ) -> SubmitOutcome:
        """Verify, claim and (for the winner only) submit the packet's order.

        This is the single boundary every real broker POST passes through —
        manual and automated alike. Duplicate intents (sequential or concurrent)
        POST exactly once; everyone else replays the winner's success, replays a
        terminal failure, recovers a crashed-after-success submit, or ends
        in-flight — never a second POST.
        """
        coid = packet.client_order_id

        # --- (1) Immutable binding checks — time-independent, run for ALL calls --
        # token/key/hash/account binding must pass before we replay OR reject, so a
        # tampered duplicate never replays and a valid duplicate never gets a
        # time-dependent rejection.
        try:
            server_key = self._server_key_for(packet, submit_canonical)
            verify_server_derived_key(
                packet,
                server_key=server_key,
                caller_client_order_id=caller_client_order_id,
            )
            verify_order_within_packet(packet, submit_canonical)
            verify_preview_submit_hash(
                packet, submit_hash=canonical_hash(submit_canonical)
            )
            verify_packet_account_mode(packet, expected=self._expected_account_mode)
        except PaperApprovalPacketError as exc:
            return SubmitOutcome(
                status="rejected",
                client_order_id=coid,
                broker_called=False,
                reason_code=exc.code,
                message=str(exc),
            )

        # --- (2) Replay a prior terminal/success BEFORE any time-dependent check-
        # Force a fresh READ COMMITTED snapshot (session is expire_on_commit=False).
        # A completed or terminally-failed order replays its ORIGINAL result even
        # after the packet's freshness window has elapsed.
        self._ledger.session.expire_all()
        existing = await self._ledger.get_execution_by_client_order_id(coid)
        if existing is not None:
            if is_inflight_execution(existing):
                return await self._resolve_inflight(coid)
            return self._resolved_outcome(existing)

        # --- (2b) Exact source authority for a new automated SELL (ROB-845) -----
        # Legacy source-less packets remain disabled. A source-bound packet must
        # reload the exact native BUY execution before freshness, position,
        # reservation, claim, or broker work. Terminal results above still replay.
        if packet.origin == "automated" and packet.side == "sell":
            source_id = (packet.source_client_order_id or "").strip()
            decision_hash = (packet.decision_identity_hash or "").strip()
            if not source_id or not decision_hash:
                return SubmitOutcome(
                    status="rejected",
                    client_order_id=coid,
                    broker_called=False,
                    reason_code="automated_sell_disabled",
                    message="automated sell requires verified native buy authority",
                )
            requested_qty = _to_decimal(submit_canonical.get("qty"))
            if (
                requested_qty is None
                or not requested_qty.is_finite()
                or requested_qty <= 0
            ):
                return SubmitOutcome(
                    status="rejected",
                    client_order_id=coid,
                    broker_called=False,
                    reason_code="source_qty_required",
                    message="automated sell requires a finite positive qty",
                )
            try:
                await verify_sell_packet_source(
                    packet,
                    ledger=self._ledger,
                    requested_qty=requested_qty,
                )
            except PaperApprovalPacketError as exc:
                return SubmitOutcome(
                    status="rejected",
                    client_order_id=coid,
                    broker_called=False,
                    reason_code=exc.code,
                    message=str(exc),
                )

        # --- (3) Time-dependent evidence — NEW (not-yet-claimed) submits only ----
        # No origin bypass: every new submit must carry server-observed market
        # evidence and pass freshness. Sells additionally require live position.
        try:
            now = self._now_fn()
            verify_packet_freshness(packet, now=now)
            verify_packet_market_data(packet, now=now, max_age=self._quote_max_age)
        except PaperApprovalPacketError as exc:
            return SubmitOutcome(
                status="rejected",
                client_order_id=coid,
                broker_called=False,
                reason_code=exc.code,
                message=str(exc),
            )

        if packet.side == "sell":
            return await self._submit_sell(packet, submit_canonical, coid)

        # --- (4) Buy: atomic claim; only the winner POSTs -----------------------
        claim = await self._ledger.claim_submit(
            client_order_id=coid,
            lifecycle_correlation_id=packet.lifecycle_correlation_id,
            execution_symbol=packet.execution_symbol,
            execution_venue=packet.execution_venue,
            execution_asset_class=packet.execution_asset_class,
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
                return self._resolved_outcome(row)
            return await self._resolve_inflight(coid)

        return await self._winner_submit(packet, submit_canonical)

    async def _submit_sell(
        self, packet: PaperApprovalPacket, submit_canonical: dict[str, Any], coid: str
    ) -> SubmitOutcome:
        """Sell path: fresh live position + cross-process reservation, then claim.

        The current position is re-read from the broker, then availability
        (position minus already-reserved open sells) and the atomic claim are
        computed under one account+symbol advisory lock so two *different* sell
        intents cannot both consume the same shares.
        """
        requested = _to_decimal(submit_canonical.get("qty"))
        if requested is None or not requested.is_finite() or requested <= 0:
            return self._sell_reject(
                packet, "sell_qty_invalid", "sell qty missing or non-positive"
            )

        broker = self._broker_factory()
        getter = getattr(broker, "get_position", None)
        if getter is None:  # pragma: no cover - defensive
            return self._sell_reject(
                packet, "position_unavailable", "broker cannot read positions"
            )

        try:
            # Serialize broker evidence, lifecycle transitions, availability and
            # the new claim under one account+symbol transaction lock.
            await self._ledger.acquire_sell_reservation_lock(
                account_mode=packet.account_mode,
                execution_symbol=packet.execution_symbol,
            )

            status_evidence = await self._load_open_sell_statuses(packet, broker)
            broker_symbol = _broker_position_symbol(packet.execution_symbol)
            try:
                position = await getter(broker_symbol)
            except AlpacaPaperRequestError as exc:
                return self._sell_reject(
                    packet,
                    "position_unavailable",
                    f"position read failed (HTTP {getattr(exc, 'status_code', None)})",
                )
            if position is None:
                causal = await self._stage_causally_safe_statuses(
                    status_evidence,
                    position_qty=Decimal("0"),
                    position_available=Decimal("0"),
                )
                if not causal:
                    return self._sell_reject(
                        packet,
                        "position_reconciliation_pending",
                        "flat position cannot reconcile a filled sell without baseline evidence",
                    )
                await self._ledger.session.commit()
                return self._sell_reject(
                    packet, "position_flat", "no current position to sell"
                )
            if not _symbols_match(
                getattr(position, "symbol", None), packet.execution_symbol
            ):
                return self._sell_reject(
                    packet,
                    "position_symbol_mismatch",
                    f"position symbol {getattr(position, 'symbol', None)!r} != {packet.execution_symbol!r}",
                )
            pos_qty = _to_decimal(getattr(position, "qty", None))
            if pos_qty is None or not pos_qty.is_finite():
                return self._sell_reject(
                    packet, "position_malformed", "position qty missing/non-finite"
                )
            raw_available = getattr(position, "qty_available", None)
            if raw_available is None:
                return self._sell_reject(
                    packet,
                    "position_available_unavailable",
                    "position qty_available is required for a sell",
                )
            pos_available = _to_decimal(raw_available)
            if (
                pos_available is None
                or not pos_available.is_finite()
                or pos_available < 0
            ):
                return self._sell_reject(
                    packet,
                    "position_available_malformed",
                    "position qty_available is non-finite or negative",
                )

            causal = await self._stage_causally_safe_statuses(
                status_evidence,
                position_qty=pos_qty,
                position_available=pos_available,
            )
            if not causal:
                return self._sell_reject(
                    packet,
                    "position_reconciliation_pending",
                    "filled sell is not yet reflected in broker position evidence",
                )
            if pos_qty <= 0:
                await self._ledger.session.commit()
                return self._sell_reject(
                    packet, "position_flat", "current position qty is non-positive"
                )

            claim = await self._ledger.reserve_sell_and_claim(
                client_order_id=coid,
                lifecycle_correlation_id=packet.lifecycle_correlation_id,
                execution_symbol=packet.execution_symbol,
                execution_venue=packet.execution_venue,
                execution_asset_class=packet.execution_asset_class,
                instrument_type=_instrument_type_for(packet.execution_asset_class),
                account_mode=packet.account_mode,
                requested_qty=requested,
                position_qty=pos_qty,
                position_available=pos_available,
                order_type=str(submit_canonical.get("type") or "limit"),
                time_in_force=submit_canonical.get("time_in_force"),
                requested_price=_to_decimal(submit_canonical.get("limit_price")),
                preview_payload=dict(submit_canonical),
                source_client_order_id=packet.source_client_order_id,
            )
            # A concurrent same-token winner may have completed while this caller
            # waited on the sell lock. Its durable execution outcome outranks the
            # now-changed source/account availability observed by this loser.
            if not claim.won and claim.row is not None:
                if not is_inflight_execution(claim.row):
                    return self._resolved_outcome(claim.row)
                return await self._resolve_inflight(coid)
            if claim.source_reason_code is not None:
                source_message = (
                    f"sell qty {requested} exceeds exact source availability "
                    f"{claim.source_available}"
                    if claim.source_reason_code == "qty_exceeds_source_available"
                    else "exact buy source authority became unavailable during claim"
                )
                return self._sell_reject(
                    packet,
                    claim.source_reason_code,
                    source_message,
                )
            if claim.insufficient:
                return self._sell_reject(
                    packet,
                    "qty_exceeds_available",
                    f"sell qty {requested} exceeds available {claim.available} "
                    f"(position {pos_qty}, broker available {pos_available}, "
                    "minus reserved open sells)",
                )
            if not claim.won:
                return await self._resolve_inflight(coid)

            return await self._winner_submit(packet, submit_canonical)
        finally:
            if self._ledger.session.in_transaction():
                await self._ledger.session.rollback()

    async def _winner_submit(
        self, packet: PaperApprovalPacket, submit_canonical: dict[str, Any]
    ) -> SubmitOutcome:
        coid = packet.client_order_id

        # --- Re-verify freshness at the moment of send --------------------------
        # All the claim/position/DB awaits are done; the wall clock may have moved
        # past the packet's freshness window since the initial check. Re-check with
        # the CURRENT clock immediately before the broker POST. On failure, book a
        # terminal outcome (no re-POST) and reject.
        try:
            now = self._now_fn()
            verify_packet_freshness(packet, now=now)
            verify_packet_market_data(packet, now=now, max_age=self._quote_max_age)
        except PaperApprovalPacketError as exc:
            try:
                await self._ledger.record_submit_failure(
                    coid,
                    order_status="rejected",
                    error_summary=f"stale_at_send: {exc.code}",
                )
            except Exception:  # noqa: BLE001 - best-effort; claim row still guards
                pass
            return SubmitOutcome(
                status="rejected",
                client_order_id=coid,
                broker_called=False,
                reason_code=exc.code,
                message=f"stale at send: {exc}",
            )

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
        try:
            order = await broker.submit_order(request)
        except AlpacaPaperRequestError as exc:
            status = getattr(exc, "status_code", None)
            if status is not None and 400 <= status < 500:
                # Deterministic client rejection — terminal. Book it so retries
                # replay the failure instead of re-POSTing.
                try:
                    body_excerpt = _extract_and_sanitize_error_body(exc)
                    await self._ledger.record_submit_failure(
                        coid,
                        order_status="rejected",
                        error_summary=f"broker_rejected: HTTP {status} \u2014 {body_excerpt}",
                    )
                except Exception:  # noqa: BLE001 - persistence best-effort
                    # Even if we cannot persist the terminal outcome, the in-flight
                    # claim row blocks any re-POST (retries end in-flight).
                    return SubmitOutcome(
                        status="failed",
                        client_order_id=coid,
                        broker_called=True,
                        reason_code="broker_rejected_unpersisted",
                        message=f"broker rejected (HTTP {status}); terminal record failed",
                    )
                return SubmitOutcome(
                    status="failed",
                    client_order_id=coid,
                    broker_called=True,
                    reason_code="broker_rejected",
                    message=f"broker rejected the order (HTTP {status})",
                )
            # Uncertain outcome (5xx / connection): the order may or may not exist.
            # Reconcile by client_order_id; never re-POST.
            return await self._resolve_uncertain(coid)

        order_dict = _order_to_dict(order)
        sell_lifecycle_override = (
            _sell_submit_lifecycle_override(order_dict.get("status"))
            if packet.side == "sell"
            else None
        )
        await self._ledger.record_submit(
            coid,
            order_dict,
            raw_response=order_dict,
            lifecycle_state_override=sell_lifecycle_override,
        )
        if packet.side == "sell" and sell_lifecycle_override is None:
            return SubmitOutcome(
                status="failed",
                client_order_id=coid,
                broker_called=True,
                reason_code="broker_terminal_status",
                order=order_dict,
                message=(
                    f"broker returned terminal status {order_dict.get('status')!r}"
                ),
            )
        return SubmitOutcome(
            status="submitted",
            client_order_id=coid,
            broker_called=True,
            order=order_dict,
        )

    async def _load_open_sell_statuses(
        self, packet: PaperApprovalPacket, broker: Any
    ) -> list[tuple[Any, dict[str, Any], str]]:
        """Load only recognized broker statuses for currently reserved sells.

        For each open sell, look it up by client_order_id and retain only explicitly
        recognized statuses. Broker error / missing / unknown / unparseable status
        is skipped, leaving the reservation intact. The caller later applies known
        terminal transitions only after validating position evidence under the
        account+symbol lock.
        """
        getter = getattr(broker, "get_order_by_client_order_id", None)
        if getter is None:
            return []
        self._ledger.session.expire_all()
        try:
            open_rows = await self._ledger.list_open_sells(
                account_mode=packet.account_mode,
                execution_symbol=packet.execution_symbol,
            )
        except Exception:  # noqa: BLE001 - keep reservations on read failure
            return []
        evidence: list[tuple[Any, dict[str, Any], str]] = []
        for row in open_rows:
            coid = getattr(row, "client_order_id", None)
            if not coid:
                continue
            try:
                order = await getter(coid)
            except AlpacaPaperRequestError:
                continue  # broker error -> keep reserved (fail-close)
            if order is None:
                continue  # unknown at broker -> keep reserved (fail-close)
            try:
                order_dict = _order_to_dict(order)
            except (TypeError, ValueError):
                continue
            normalized_status = normalize_known_broker_order_status(
                order_dict.get("status")
            )
            if normalized_status is None:
                continue  # unknown/unparseable status -> keep reserved
            order_dict["status"] = normalized_status
            evidence.append((row, order_dict, normalized_status))
        return evidence

    async def _stage_causally_safe_statuses(
        self,
        evidence: list[tuple[Any, dict[str, Any], str]],
        *,
        position_qty: Decimal,
        position_available: Decimal,
    ) -> bool:
        """Stage terminal transitions without releasing an unreflected fill."""
        for row, order_dict, status in evidence:
            if status in KNOWN_OPEN_BROKER_STATUSES:
                continue
            if status == "filled" and not _filled_is_reflected(
                row,
                order_dict,
                position_qty=position_qty,
                position_available=position_available,
            ):
                return False
            try:
                await self._ledger.record_status(
                    row.client_order_id, order_dict, commit=False
                )
            except Exception:  # noqa: BLE001 - rollback keeps every hold fail-closed
                return False
        return True

    def _sell_reject(
        self, packet: PaperApprovalPacket, code: str, message: str
    ) -> SubmitOutcome:
        return SubmitOutcome(
            status="rejected",
            client_order_id=packet.client_order_id,
            broker_called=False,
            reason_code=code,
            message=message,
        )

    async def _resolve_uncertain(self, client_order_id: str) -> SubmitOutcome:
        """Reconcile an uncertain winner outcome (5xx/timeout). Never re-POSTs."""
        recovered = await self._reconcile_inflight_via_broker(client_order_id)
        if recovered is not None:
            return recovered
        return SubmitOutcome(
            status="idempotency_in_progress",
            client_order_id=client_order_id,
            broker_called=True,
            reason_code="idempotency_in_progress",
            message="submit outcome uncertain; broker has no order yet — not re-POSTing",
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
            # committed record_submit / record_submit_failure becomes visible.
            self._ledger.session.expire_all()
            row = await self._ledger.get_execution_by_client_order_id(client_order_id)
            if row is not None and not is_inflight_execution(row):
                return self._resolved_outcome(row)
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
        claim_row = await self._ledger.get_execution_by_client_order_id(client_order_id)
        is_sell = claim_row is not None and claim_row.side == "sell"
        sell_lifecycle_override = (
            _sell_submit_lifecycle_override(order_dict.get("status"))
            if is_sell
            else None
        )
        await self._ledger.record_submit(
            client_order_id,
            order_dict,
            raw_response=order_dict,
            lifecycle_state_override=sell_lifecycle_override,
        )
        if is_sell and sell_lifecycle_override is None:
            return SubmitOutcome(
                status="failed",
                client_order_id=client_order_id,
                broker_called=False,
                reason_code="broker_terminal_status_recovered",
                order=order_dict,
                message=(
                    "broker lookup returned terminal status "
                    f"{order_dict.get('status')!r}"
                ),
            )
        return SubmitOutcome(
            status="recovered",
            client_order_id=client_order_id,
            broker_called=False,
            reason_code="recovered_via_broker_lookup",
            order=order_dict,
            message="crashed-after-success submit recovered from broker evidence",
        )

    def _resolved_outcome(self, row: Any) -> SubmitOutcome:
        """Map an already-resolved execution row to a replay outcome.

        A terminal ``anomaly`` row replays the deterministic broker failure; any
        other resolved row replays the stored success.
        """
        state = str(getattr(row, "lifecycle_state", "") or "")
        coid = str(getattr(row, "client_order_id", ""))
        if state == "anomaly":
            return SubmitOutcome(
                status="failed",
                client_order_id=coid,
                broker_called=False,
                reason_code="broker_rejected_replayed",
                order=None,
                message=str(getattr(row, "error_summary", None) or "terminal failure"),
            )
        return self._replay_outcome(row)

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


def _filled_is_reflected(
    row: Any,
    order: dict[str, Any],
    *,
    position_qty: Decimal,
    position_available: Decimal,
) -> bool:
    """Prove a newly-filled sell is reflected relative to its claim baseline."""
    filled_qty = _to_decimal(order.get("filled_qty") or order.get("filled_quantity"))
    snapshot = getattr(row, "position_snapshot", None)
    if (
        filled_qty is None
        or not filled_qty.is_finite()
        or filled_qty <= 0
        or not isinstance(snapshot, dict)
        or snapshot.get("snapshot_kind") != "sell_claim_baseline"
    ):
        return False
    baseline_qty = _to_decimal(snapshot.get("qty"))
    baseline_available = _to_decimal(snapshot.get("qty_available"))
    if (
        baseline_qty is None
        or baseline_available is None
        or not baseline_qty.is_finite()
        or not baseline_available.is_finite()
    ):
        return False
    reflected_qty = baseline_qty - filled_qty
    reflected_available = baseline_available - filled_qty
    if reflected_qty < 0 or reflected_available < 0:
        return False
    return position_qty <= reflected_qty and position_available <= reflected_available


def _normalize_symbol(symbol: str | None) -> str:
    return (symbol or "").replace("/", "").replace("-", "").strip().upper()


def _symbols_match(a: str | None, b: str | None) -> bool:
    return _normalize_symbol(a) == _normalize_symbol(b) and _normalize_symbol(a) != ""


def _broker_position_symbol(execution_symbol: str) -> str:
    """Alpaca positions key crypto by the slashless symbol (BTC/USD -> BTCUSD)."""
    return (execution_symbol or "").replace("/", "").strip()


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
    "SUCCESS_STATUSES",
    "SubmitOutcome",
    "build_canonical_payload",
    "canonical_hash",
    "derive_automated_key",
    "derive_client_order_id",
]
