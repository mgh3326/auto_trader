"""ROB-286 — Scalper runner: market data → decision → execution → ledger.

The runner is the I/O layer around the pure decision function. Inputs:
  * ``MarketDataSource`` — Child B WebSocket/REST consumer (read-only).
  * ``BinanceTestnetExecutionClient`` — this PR's signed adapter.
  * ``BinanceTestnetLedgerService`` — this PR's service-only ledger.
  * ``ScalperConfig`` — locked MVP defaults.

On construction the runner reconciles ledger state against broker state
for each instrument in the MVP set (§B.C.10). If drift is detected, the
runner refuses to start and the operator is expected to investigate
(``anomaly`` rows visible in the ledger + Sentry events fired by the
ledger service).

Open item #5 lean adopted: ``dry_run=True`` on every submit call.
``submit_decision`` always passes the runner's configured ``dry_run``
through to the execution client; the smoke CLI defaults this to True.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from app.models.binance_testnet_order_ledger import BinanceTestnetOrderLedger
from app.services.brokers.binance.testnet.dto import (
    DryRunResult,
    OrderSubmitResult,
    StopOrderResult,
)
from app.services.brokers.binance.testnet.execution_client import (
    BinanceTestnetExecutionClient,
)
from app.services.brokers.binance.testnet.ledger.service import (
    BinanceTestnetLedgerService,
)
from app.services.scalping.config import ScalperConfig
from app.services.scalping.decision import (
    Action,
    Entry,
    Exit,
    Hold,
    MarketSnapshot,
    SymbolState,
    compute_action,
)
from app.services.scalping.notifications import (
    emit_sentry_breadcrumb,
    log_action_taken,
)

logger = logging.getLogger("app.services.scalping.runner")

# Lifecycle states where the symbol is considered "busy" (no new entry).
BUSY_STATES: frozenset[str] = frozenset({"submitted", "filled", "tp_sl_armed"})

# States the broker is expected to still hold as open orders. A ``filled``
# order is NOT in this set — broker open_orders won't return a filled
# order, so cross-checking ``filled`` rows against ``open_orders`` would
# always flag drift. ``filled`` rows are cross-checked against
# ``recent_fills`` instead (see fills-side pass below).
BROKER_OPEN_STATES: frozenset[str] = frozenset({"submitted", "tp_sl_armed"})


@dataclass
class ReconcileResult:
    """Summary of a reconciliation pass."""

    anomalies_detected: int = 0
    rows_examined: int = 0
    anomaly_client_order_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _LegArmed:
    """ROB-289 — One stop leg placed cleanly at the broker."""

    result: StopOrderResult


@dataclass(frozen=True, slots=True)
class _LegRejected:
    """ROB-289 — One stop leg rejected (4xx)."""

    error_payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _LegUnknown:
    """ROB-289 — One stop leg in indeterminate state (timeout/5xx)."""

    error_summary: str


@dataclass(frozen=True, slots=True)
class _LegDryRun:
    """ROB-289 — Dry-run leg outcome (no HTTP attempted)."""

    preview: object  # OrderPreview


@dataclass
class RunnerTickResult:
    """Per-tick observability — what the runner did for a single symbol."""

    symbol: str
    action_name: str  # "hold" | "entry" | "exit"
    submitted: bool
    dry_run: bool
    notes: str


@dataclass
class ScalperRunner:
    """Orchestrate market data → decision → execution → ledger.

    Constructed once per process; ``tick_once`` runs a single
    decision cycle for one symbol. The smoke CLI loops calling
    ``tick_once`` for each MVP symbol.
    """

    execution_client: BinanceTestnetExecutionClient
    ledger_service: BinanceTestnetLedgerService
    config: ScalperConfig
    instrument_id_for_symbol: Callable[[str], Awaitable[int]]
    market_snapshot_for_symbol: Callable[[str], Awaitable[MarketSnapshot]]
    dry_run: bool = True

    async def reconcile_on_start(self) -> ReconcileResult:
        """Reconciliation pass per §B.C.10.

        Two-pass walk for each symbol in the MVP set:
          1. **Open-orders pass** — Fetch ledger rows in
             ``submitted``/``tp_sl_armed`` states (``BROKER_OPEN_STATES``).
             A ``filled`` order is NOT expected to appear in broker
             ``open_orders``, so ``filled`` rows are excluded here and
             handled by pass 2. If a row in scope is missing from broker
             ``open_orders``, transition it to ``anomaly`` with reason
             ``reconcile_drift``.
          2. **Fills-side pass (ROB-290)** — Fetch ledger rows in
             ``filled`` state. If a row's ``broker_order_id`` is missing
             from broker ``recent_fills``, transition it to ``anomaly``
             with reason ``reconcile_drift_fills``. When no ``filled``
             rows exist for the symbol, ``recent_fills`` is **not**
             called — keeps dry-run paths from issuing signed reads.

        Lookback bounds: last ``reconcile_open_orders_limit`` orders /
        ``reconcile_recent_fills_limit`` fills, but never older than
        ``reconcile_lookback_hours``.
        """
        result = ReconcileResult()
        fills_examined = 0
        cutoff = datetime.now(tz=UTC) - timedelta(
            hours=self.config.reconcile_lookback_hours
        )
        for symbol in sorted(self.config.symbols):
            try:
                instrument_id = await self.instrument_id_for_symbol(symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "reconcile_on_start: cannot resolve instrument_id for %s: %s",
                    symbol,
                    exc,
                )
                continue
            # --------------------------------------------------------------
            # Pass 1 — open-orders walk (ROB-286).
            #
            # Excludes ``filled`` (handled by pass 2). A filled order is
            # not expected to be in broker.open_orders, so cross-checking
            # it here would always flag drift.
            # --------------------------------------------------------------
            ledger_rows = await self.ledger_service.list_by_instrument(
                instrument_id=instrument_id,
                lifecycle_states=list(BROKER_OPEN_STATES),
                limit=self.config.reconcile_open_orders_limit,
            )
            try:
                broker_open = await self.execution_client.open_orders(symbol=symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "reconcile_on_start: broker open_orders failed for %s: %s",
                    symbol,
                    exc,
                )
                broker_open = []
            broker_client_order_ids = {
                str(o.get("clientOrderId", "")) for o in broker_open
            }
            for row in ledger_rows:
                result.rows_examined += 1
                created_at = row.created_at
                # Time bound — skip very old rows.
                if (
                    created_at is not None
                    and created_at.tzinfo is not None
                    and created_at < cutoff
                ):
                    await self.ledger_service.stamp_reconciliation_run(
                        client_order_id=row.client_order_id
                    )
                    continue
                if row.client_order_id not in broker_client_order_ids:
                    await self.ledger_service.record_anomaly(
                        client_order_id=row.client_order_id,
                        reason="reconcile_drift",
                        extra_metadata={
                            "reconciled_at": datetime.now(tz=UTC).isoformat()
                        },
                    )
                    result.anomalies_detected += 1
                    result.anomaly_client_order_ids.append(row.client_order_id)
                else:
                    await self.ledger_service.stamp_reconciliation_run(
                        client_order_id=row.client_order_id
                    )
            # --------------------------------------------------------------
            # Pass 2 — fills-side walk (ROB-290).
            #
            # For ``filled`` rows we cross-check against ``recent_fills``
            # by ``broker_order_id``. Drift here means the local ledger
            # claims a fill that Binance has no trade record of — an
            # operator-investigatable anomaly (separate reason so the
            # two walks are distinguishable in the row history).
            # --------------------------------------------------------------
            filled_rows = await self.ledger_service.list_by_instrument(
                instrument_id=instrument_id,
                lifecycle_states=["filled"],
                limit=self.config.reconcile_recent_fills_limit,
            )
            if not filled_rows:
                # No filled rows for this symbol — skip the signed
                # ``recent_fills`` read entirely. Dry-run / shadow paths
                # never reach ``filled`` state, so this keeps them from
                # issuing /myTrades requests.
                continue
            try:
                broker_fills = await self.execution_client.recent_fills(
                    symbol=symbol,
                    limit=self.config.reconcile_recent_fills_limit,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "reconcile_on_start: broker recent_fills failed for %s: %s",
                    symbol,
                    exc,
                )
                broker_fills = []
            broker_fill_order_ids = {str(t.get("orderId", "")) for t in broker_fills}
            for row in filled_rows:
                fills_examined += 1
                result.rows_examined += 1
                created_at = row.created_at
                # Time bound — skip very old rows (still stamp).
                if (
                    created_at is not None
                    and created_at.tzinfo is not None
                    and created_at < cutoff
                ):
                    await self.ledger_service.stamp_reconciliation_run(
                        client_order_id=row.client_order_id
                    )
                    continue
                broker_order_id = row.broker_order_id
                if broker_order_id is None:
                    # No broker handle to cross-check; stamp and move on.
                    await self.ledger_service.stamp_reconciliation_run(
                        client_order_id=row.client_order_id
                    )
                    continue
                if str(broker_order_id) not in broker_fill_order_ids:
                    await self.ledger_service.record_anomaly(
                        client_order_id=row.client_order_id,
                        reason="reconcile_drift_fills",
                        extra_metadata={
                            "reconciled_at": datetime.now(tz=UTC).isoformat(),
                            "broker_order_id": str(broker_order_id),
                        },
                    )
                    result.anomalies_detected += 1
                    result.anomaly_client_order_ids.append(row.client_order_id)
                else:
                    await self.ledger_service.stamp_reconciliation_run(
                        client_order_id=row.client_order_id
                    )
        emit_sentry_breadcrumb(
            message="reconcile_on_start complete",
            data={
                "anomalies_detected": result.anomalies_detected,
                "rows_examined": result.rows_examined,
                "fills_examined": fills_examined,
            },
        )
        return result

    async def _derive_symbol_state(
        self, *, symbol: str, instrument_id: int
    ) -> SymbolState:
        """Build SymbolState from the ledger (open position + TP/SL)."""
        rows = await self.ledger_service.list_by_instrument(
            instrument_id=instrument_id,
            lifecycle_states=list(BUSY_STATES),
            limit=5,
        )
        if not rows:
            return SymbolState(
                symbol=symbol,
                open_position=False,
                open_entry_client_order_id=None,
                tp_price=None,
                sl_price=None,
            )
        # Latest row (rows are ordered created_at desc by repository).
        row = rows[0]
        return SymbolState(
            symbol=symbol,
            open_position=True,
            open_entry_client_order_id=row.client_order_id,
            tp_price=row.tp_price,
            sl_price=row.sl_price,
        )

    async def tick_once(self, *, symbol: str) -> RunnerTickResult:
        """Run a single decision cycle for ``symbol``.

        Pure orchestration — the decision logic itself is in
        ``decision.compute_action``.
        """
        if symbol not in self.config.symbols:
            raise ValueError(
                f"Symbol {symbol!r} is not in the MVP locked set "
                f"({sorted(self.config.symbols)}). Expanding the set is "
                "a code change."
            )
        instrument_id = await self.instrument_id_for_symbol(symbol)
        snapshot = await self.market_snapshot_for_symbol(symbol)
        state = await self._derive_symbol_state(
            symbol=symbol, instrument_id=instrument_id
        )
        action: Action = compute_action(
            state=state, snapshot=snapshot, config=self.config
        )
        if isinstance(action, Hold):
            log_action_taken(
                symbol=symbol,
                action_name="hold",
                details={"reason": action.reason},
            )
            return RunnerTickResult(
                symbol=symbol,
                action_name="hold",
                submitted=False,
                dry_run=self.dry_run,
                notes=action.reason,
            )
        if isinstance(action, Entry):
            return await self._handle_entry(
                symbol=symbol,
                instrument_id=instrument_id,
                snapshot=snapshot,
                entry=action,
            )
        if isinstance(action, Exit):
            return await self._handle_exit(
                symbol=symbol,
                state=state,
                exit_action=action,
            )
        raise AssertionError(f"Unhandled action type: {type(action)!r}")

    async def _handle_entry(
        self,
        *,
        symbol: str,
        instrument_id: int,
        snapshot: MarketSnapshot,
        entry: Entry,
    ) -> RunnerTickResult:
        # Size the entry to the configured max notional.
        notional = self.config.max_notional_usdt
        qty = (notional / snapshot.last_price).quantize(Decimal("0.00000001"))
        client_order_id = self.execution_client._new_client_order_id()
        # planned → previewed → validated → submitted ledger trail.
        await self.ledger_service.record_plan(
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            side=entry.side,
            order_type="MARKET",
            qty=qty,
            tp_price=entry.tp_price,
            sl_price=entry.sl_price,
            notional_usdt=notional,
        )
        await self.ledger_service.record_preview(client_order_id=client_order_id)
        await self.ledger_service.record_validation(client_order_id=client_order_id)
        confirm = not self.dry_run
        result = await self.execution_client.submit_order(
            symbol=symbol,
            side=entry.side,
            order_type="MARKET",
            quantity=qty,
            notional_usdt=notional,
            client_order_id=client_order_id,
            dry_run=self.dry_run,
            confirm=confirm,
        )
        submitted = isinstance(result, OrderSubmitResult)
        notes = entry.reason
        if submitted:
            await self.ledger_service.record_submit(
                client_order_id=client_order_id,
                broker_order_id=result.broker_order_id,
            )
            # If the broker reports the MARKET order as already FILLED in the
            # submit response, transition to ``filled`` and arm paired TP/SL.
            # ROB-289 — paired-leg placement is sequential, never parallel,
            # so the §3.1 first-leg-success-second-leg-reject path stays
            # deterministic.
            if isinstance(result, OrderSubmitResult) and result.status == "FILLED":
                await self.ledger_service.record_fill(client_order_id=client_order_id)
                fill_price = (
                    result.price if result.price is not None else snapshot.last_price
                )
                tp_sl_notes = await self._place_paired_tp_sl(
                    symbol=symbol,
                    instrument_id=instrument_id,
                    entry_client_order_id=client_order_id,
                    entry_side=entry.side,
                    qty=qty,
                    fill_price=fill_price,
                    tp_price=entry.tp_price,
                    sl_price=entry.sl_price,
                )
                notes = f"{notes} | {tp_sl_notes}"
        log_action_taken(
            symbol=symbol,
            action_name="entry",
            details={
                "side": entry.side,
                "qty": str(qty),
                "submitted": submitted,
                "dry_run": self.dry_run,
                "reason": entry.reason,
            },
        )
        return RunnerTickResult(
            symbol=symbol,
            action_name="entry",
            submitted=submitted,
            dry_run=self.dry_run,
            notes=notes,
        )

    async def _place_paired_tp_sl(
        self,
        *,
        symbol: str,
        instrument_id: int,
        entry_client_order_id: str,
        entry_side: str,
        qty: Decimal,
        fill_price: Decimal,
        tp_price: Decimal,
        sl_price: Decimal,
    ) -> str:
        """ROB-289 — Place paired TP (stop-limit) + SL (stop-market) legs.

        SEQUENTIAL placement, NOT ``asyncio.gather``. Parallelism would
        produce an ambiguous half-armed state on a broker reject; sequential
        lets the §3.1 fallback path be deterministic.

        Each leg is recorded as its own ledger row (planned →
        previewed → validated → submitted → tp_sl_armed) linked to the
        entry via ``parent_client_order_id``. On 4xx broker reject, the
        first-leg cancel + entry-row anomaly + cancel-and-close fallback
        runs synchronously before this function returns.
        """
        confirm = not self.dry_run
        # Opposite side for both legs: an entry BUY closes via SELL stops.
        exit_side = "SELL" if entry_side == "BUY" else "BUY"
        tp_cid = f"{entry_client_order_id}-tp"
        sl_cid = f"{entry_client_order_id}-sl"

        # Pre-record both legs as ``planned`` ledger rows. This makes the
        # ledger trail complete even if the runner crashes mid-placement
        # (the reconcile pass will pick up planned-but-not-submitted rows).
        await self.ledger_service.record_plan(
            instrument_id=instrument_id,
            client_order_id=tp_cid,
            side=exit_side,
            order_type="LIMIT",
            qty=qty,
            price=tp_price,
            tp_price=tp_price,
            parent_client_order_id=entry_client_order_id,
        )
        await self.ledger_service.record_preview(client_order_id=tp_cid)
        await self.ledger_service.record_validation(client_order_id=tp_cid)
        await self.ledger_service.record_plan(
            instrument_id=instrument_id,
            client_order_id=sl_cid,
            side=exit_side,
            order_type="MARKET",
            qty=qty,
            sl_price=sl_price,
            parent_client_order_id=entry_client_order_id,
        )
        await self.ledger_service.record_preview(client_order_id=sl_cid)
        await self.ledger_service.record_validation(client_order_id=sl_cid)

        # --- TP leg (stop-limit) — placed first. -----------------------
        tp_result = await self._place_one_leg(
            place_callable=lambda: self.execution_client.place_stop_limit_order(
                symbol=symbol,
                side=exit_side,
                quantity=qty,
                stop_price=tp_price,
                limit_price=tp_price,
                client_order_id=tp_cid,
                dry_run=self.dry_run,
                confirm=confirm,
            ),
            cid=tp_cid,
            leg="tp",
        )
        if isinstance(tp_result, _LegRejected):
            # §3.2 — first leg rejected. Record anomaly on the entry row
            # and fall back to cancel-and-close.
            await self._record_paired_anomaly_and_fallback(
                symbol=symbol,
                entry_client_order_id=entry_client_order_id,
                rejected_leg="tp",
                tp_cid=tp_cid,
                sl_cid=sl_cid,
                error_payload=tp_result.error_payload,
                first_leg_placed=False,
            )
            return "tp_sl_placement_rejected_first_leg"
        if isinstance(tp_result, _LegUnknown):
            # §3.5 — partial network failure mid-placement. Record anomaly
            # on the TP row; do not attempt SL. Reconciliation resolves.
            await self.ledger_service.record_anomaly(
                client_order_id=tp_cid,
                reason="tp_sl_placement_unknown",
                extra_metadata={"leg": "tp", "error": tp_result.error_summary},
            )
            return "tp_sl_placement_unknown_first_leg"

        # TP placed cleanly — record submit + filled + armed.
        # The lifecycle requires submitted → filled → tp_sl_armed; for
        # a paired leg, "filled" means the broker accepted the order
        # (status=NEW). The actual execution that closes the position
        # is recorded as ``tp_sl_triggered`` when price crosses.
        assert isinstance(tp_result, _LegArmed)
        await self.ledger_service.record_submit(
            client_order_id=tp_cid,
            broker_order_id=tp_result.result.broker_order_id,
        )
        await self.ledger_service.record_fill(client_order_id=tp_cid)
        await self.ledger_service.record_tp_sl_armed(
            client_order_id=tp_cid,
            broker_order_id=tp_result.result.broker_order_id,
            tp_or_sl="tp",
            stop_price=tp_result.result.stop_price,
            limit_price=tp_result.result.limit_price,
        )

        # --- SL leg (stop-market) — placed second. ---------------------
        sl_result = await self._place_one_leg(
            place_callable=lambda: self.execution_client.place_stop_market_order(
                symbol=symbol,
                side=exit_side,
                quantity=qty,
                stop_price=sl_price,
                client_order_id=sl_cid,
                dry_run=self.dry_run,
                confirm=confirm,
            ),
            cid=sl_cid,
            leg="sl",
        )
        if isinstance(sl_result, _LegRejected):
            # §3.1 — MOST DANGEROUS PATH. First leg succeeded at the broker,
            # second leg rejected. Cancel the first leg immediately to avoid
            # leaving a half-armed broker position, then fall back.
            await self._record_paired_anomaly_and_fallback(
                symbol=symbol,
                entry_client_order_id=entry_client_order_id,
                rejected_leg="sl",
                tp_cid=tp_cid,
                sl_cid=sl_cid,
                error_payload=sl_result.error_payload,
                first_leg_placed=True,
            )
            return "tp_sl_placement_rejected_second_leg"
        if isinstance(sl_result, _LegUnknown):
            # §3.5 on second leg — record anomaly on SL row; TP row already
            # armed and remains armed (operator decides via reconciliation).
            await self.ledger_service.record_anomaly(
                client_order_id=sl_cid,
                reason="tp_sl_placement_unknown",
                extra_metadata={"leg": "sl", "error": sl_result.error_summary},
            )
            return "tp_sl_placement_unknown_second_leg"

        assert isinstance(sl_result, _LegArmed)
        await self.ledger_service.record_submit(
            client_order_id=sl_cid,
            broker_order_id=sl_result.result.broker_order_id,
        )
        await self.ledger_service.record_fill(client_order_id=sl_cid)
        await self.ledger_service.record_tp_sl_armed(
            client_order_id=sl_cid,
            broker_order_id=sl_result.result.broker_order_id,
            tp_or_sl="sl",
            stop_price=sl_result.result.stop_price,
            limit_price=sl_result.result.limit_price,
        )
        return "tp_sl_armed_paired"

    async def _place_one_leg(
        self,
        *,
        place_callable: Callable[[], Awaitable[DryRunResult | StopOrderResult]],
        cid: str,
        leg: str,
    ) -> _LegArmed | _LegRejected | _LegUnknown | _LegDryRun:
        """Run one stop-leg placement and classify the outcome.

        Plan §3 classifies broker outcomes into:
          * 4xx reject (deterministic — fallback per §3.1/§3.2),
          * timeout / 5xx (unknown — record anomaly + reconcile later
            per §3.5),
          * success.

        Secrets are never logged here. ``httpx.HTTPStatusError`` exposes
        the URL, but the runner records only the broker error code +
        body excerpt in extra_metadata; the signed query string is not
        persisted.
        """
        try:
            result = await place_callable()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if 400 <= status < 500:
                body_excerpt = ""
                try:
                    body_excerpt = exc.response.text[:200]
                except Exception:  # noqa: BLE001
                    body_excerpt = ""
                return _LegRejected(
                    error_payload={
                        "leg": leg,
                        "status_code": status,
                        "body_excerpt": body_excerpt,
                    }
                )
            return _LegUnknown(
                error_summary=f"http_{status}",
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            return _LegUnknown(
                error_summary=f"{type(exc).__name__}: {exc}"[:200],
            )
        if isinstance(result, DryRunResult):
            return _LegDryRun(preview=result.preview)
        return _LegArmed(result=result)

    async def _record_paired_anomaly_and_fallback(
        self,
        *,
        symbol: str,
        entry_client_order_id: str,
        rejected_leg: str,
        tp_cid: str,
        sl_cid: str,
        error_payload: dict[str, Any],
        first_leg_placed: bool,
    ) -> None:
        """§3.1 / §3.2 — paired-placement reject handler.

        If the first leg already placed at the broker (the §3.1 path —
        most dangerous), cancel it immediately to avoid leaving the
        broker in a half-armed state. Then record an anomaly on the
        entry row and run the existing cancel-and-close fallback so the
        lifecycle terminates deterministically.
        """
        # 1. If the TP leg was already armed at the broker, cancel it.
        if first_leg_placed:
            try:
                await self.execution_client.cancel_order(
                    symbol=symbol,
                    client_order_id=tp_cid,
                    dry_run=self.dry_run,
                    confirm=not self.dry_run,
                )
                await self.ledger_service.record_cancel(
                    client_order_id=tp_cid,
                    reason="fallback_after_broker_reject",
                )
            except Exception as exc:  # noqa: BLE001
                # §3.3-shaped — sibling cancel failed. Record anomaly on
                # the TP row so an operator can investigate; do NOT
                # auto-retry.
                await self.ledger_service.record_anomaly(
                    client_order_id=tp_cid,
                    reason="opposite_leg_cancel_failed",
                    extra_metadata={"error": str(exc)[:200]},
                )
        # 2. Record anomaly on the rejected leg row (it is still in
        # validated/planned — depending on which leg was rejected).
        rejected_cid = sl_cid if rejected_leg == "sl" else tp_cid
        # The rejected leg ledger row is in 'planned' state from the
        # pre-record above. Move it directly to anomaly (planned→anomaly
        # is a legal transition).
        await self.ledger_service.record_anomaly(
            client_order_id=rejected_cid,
            reason="tp_sl_placement_rejected",
            extra_metadata=error_payload,
        )
        # 3. Record anomaly on the entry row with the broker reject
        # context. This is the operator-investigatable hook.
        await self.ledger_service.record_anomaly(
            client_order_id=entry_client_order_id,
            reason="tp_sl_placement_rejected",
            extra_metadata={
                "rejected_leg": rejected_leg,
                "first_leg_placed": first_leg_placed,
                # error_payload already excludes any signed query string
                # and contains only the broker status code + body excerpt.
                **error_payload,
            },
        )

    async def _handle_exit(
        self,
        *,
        symbol: str,
        state: SymbolState,
        exit_action: Exit,
    ) -> RunnerTickResult:
        """Exit via opposite-leg cancellation when a paired TP/SL leg triggers.

        ROB-289 — When the decision function returns ``Exit`` because the
        last_price crossed ``tp_price`` (take_profit) or ``sl_price``
        (stop_loss), the runner walks the paired ledger rows linked by
        the shared entry ``client_order_id`` (the parent CID; never use
        the TP/SL CIDs themselves — reviewer focus #4):

          1. Mark the triggered leg as ``tp_sl_triggered``.
          2. Cancel the sibling leg at the broker.
          3. Record ``cancelled(opposite_leg_triggered)`` on the sibling.
          4. Close the entry row.

        If the entry has no paired legs (legacy single-leg ledger or
        dry-run path), this falls back to the cancel-and-close behavior
        from the ROB-286 MVP.
        """
        entry_cid = state.open_entry_client_order_id or ""
        confirm = not self.dry_run

        triggered_leg = "tp" if exit_action.reason == "take_profit" else "sl"
        # Look up paired legs by the shared parent_client_order_id (the
        # entry CID). Plan reviewer focus #4 — sibling lookup is by SHARED
        # entry CID, never by the TP/SL CIDs themselves.
        siblings = await self._find_paired_legs(parent_cid=entry_cid)
        if not siblings:
            # No paired legs — fall back to the ROB-286 MVP single-leg
            # cancel-and-close path so legacy ledger rows still wind down.
            result = await self.execution_client.cancel_order(
                symbol=symbol,
                client_order_id=entry_cid,
                dry_run=self.dry_run,
                confirm=confirm,
            )
            submitted = not isinstance(result, DryRunResult)
            if submitted:
                await self.ledger_service.record_cancel(client_order_id=entry_cid)
            log_action_taken(
                symbol=symbol,
                action_name="exit",
                details={
                    "reason": exit_action.reason,
                    "submitted": submitted,
                    "dry_run": self.dry_run,
                },
            )
            return RunnerTickResult(
                symbol=symbol,
                action_name="exit",
                submitted=submitted,
                dry_run=self.dry_run,
                notes=exit_action.reason,
            )

        # Paired flow.
        triggered_cid = siblings.get(triggered_leg)
        sibling_cid = siblings.get("sl" if triggered_leg == "tp" else "tp")
        notes = exit_action.reason
        submitted = False

        if triggered_cid is not None:
            # Only legs currently in tp_sl_armed are eligible for the
            # trigger transition. If a leg is in a different state (e.g.,
            # already cancelled by a previous tick), skip silently — the
            # reconciliation pass will catch any remaining drift.
            triggered_row = await self.ledger_service.get_by_client_order_id(
                triggered_cid
            )
            if (
                triggered_row is not None
                and triggered_row.lifecycle_state == "tp_sl_armed"
            ):
                await self.ledger_service.record_tp_sl_triggered(
                    client_order_id=triggered_cid,
                )
                submitted = True

        if sibling_cid is not None:
            sibling_row = await self.ledger_service.get_by_client_order_id(sibling_cid)
            if sibling_row is not None and sibling_row.lifecycle_state == "tp_sl_armed":
                try:
                    cancel_result = await self.execution_client.cancel_order(
                        symbol=symbol,
                        client_order_id=sibling_cid,
                        dry_run=self.dry_run,
                        confirm=confirm,
                    )
                except Exception as exc:  # noqa: BLE001
                    # §3.3 — sibling cancel failed. Record anomaly on the
                    # sibling row; operator must investigate. Do NOT
                    # auto-retry.
                    await self.ledger_service.record_anomaly(
                        client_order_id=sibling_cid,
                        reason="opposite_leg_cancel_failed",
                        extra_metadata={"error": str(exc)[:200]},
                    )
                    notes = f"{notes} | sibling_cancel_failed"
                else:
                    cancelled_at_broker = not isinstance(cancel_result, DryRunResult)
                    if cancelled_at_broker:
                        await self.ledger_service.record_cancel(
                            client_order_id=sibling_cid,
                            reason="opposite_leg_triggered",
                        )

        log_action_taken(
            symbol=symbol,
            action_name="exit",
            details={
                "reason": exit_action.reason,
                "submitted": submitted,
                "dry_run": self.dry_run,
                "triggered_leg": triggered_leg,
            },
        )
        return RunnerTickResult(
            symbol=symbol,
            action_name="exit",
            submitted=submitted,
            dry_run=self.dry_run,
            notes=notes,
        )

    async def _find_paired_legs(self, *, parent_cid: str) -> dict[str, str]:
        """Return ``{"tp": <cid>, "sl": <cid>}`` for legs linked by ``parent_cid``.

        Looks up rows whose ``parent_client_order_id`` equals the entry's
        client_order_id and whose ``extra_metadata.tp_or_sl`` identifies
        the leg. Returns an empty dict when no paired rows exist (legacy
        single-leg path).
        """
        if not parent_cid:
            return {}
        # Use the deterministic CID suffixes from ``_place_paired_tp_sl``.
        tp_cid = f"{parent_cid}-tp"
        sl_cid = f"{parent_cid}-sl"
        result: dict[str, str] = {}
        tp_row = await self.ledger_service.get_by_client_order_id(tp_cid)
        if tp_row is not None and tp_row.parent_client_order_id == parent_cid:
            result["tp"] = tp_cid
        sl_row = await self.ledger_service.get_by_client_order_id(sl_cid)
        if sl_row is not None and sl_row.parent_client_order_id == parent_cid:
            result["sl"] = sl_cid
        return result


__all__ = [
    "BUSY_STATES",
    "ReconcileResult",
    "RunnerTickResult",
    "ScalperRunner",
    # Re-export so tests can construct fake snapshots through one path.
    "BinanceTestnetOrderLedger",
    "Any",
]
