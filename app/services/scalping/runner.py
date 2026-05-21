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

from app.models.binance_testnet_order_ledger import BinanceTestnetOrderLedger
from app.services.brokers.binance.testnet.dto import DryRunResult, OrderSubmitResult
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


@dataclass
class ReconcileResult:
    """Summary of a reconciliation pass."""

    anomalies_detected: int = 0
    rows_examined: int = 0
    anomaly_client_order_ids: list[str] = field(default_factory=list)


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
             ``submitted``/``filled``/``tp_sl_armed`` states. If a row is
             missing from broker ``open_orders``, transition it to
             ``anomaly`` with reason ``reconcile_drift``.
          2. **Fills-side pass (ROB-290)** — Fetch ledger rows in
             ``filled`` state. If a row's ``broker_order_id`` is missing
             from broker ``recent_fills``, transition it to ``anomaly``
             with reason ``reconcile_drift_fills``.

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
            # --------------------------------------------------------------
            ledger_rows = await self.ledger_service.list_by_instrument(
                instrument_id=instrument_id,
                lifecycle_states=list(BUSY_STATES),
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
        if submitted:
            await self.ledger_service.record_submit(
                client_order_id=client_order_id,
                broker_order_id=result.broker_order_id,
            )
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
            notes=entry.reason,
        )

    async def _handle_exit(
        self,
        *,
        symbol: str,
        state: SymbolState,
        exit_action: Exit,
    ) -> RunnerTickResult:
        """Exit via cancel-and-close on the entry's client_order_id.

        Spot has no native OCO on testnet (per §B.C.2 / open item #6) so the
        MVP simply cancels the open order rather than placing a paired sell.
        Production-tier TP/SL placement is a follow-up that uses two ledger
        rows tied by parent_client_order_id.
        """
        cid = state.open_entry_client_order_id or ""
        confirm = not self.dry_run
        result = await self.execution_client.cancel_order(
            symbol=symbol,
            client_order_id=cid,
            dry_run=self.dry_run,
            confirm=confirm,
        )
        submitted = not isinstance(result, DryRunResult)
        if submitted:
            await self.ledger_service.record_cancel(client_order_id=cid)
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


__all__ = [
    "BUSY_STATES",
    "ReconcileResult",
    "RunnerTickResult",
    "ScalperRunner",
    # Re-export so tests can construct fake snapshots through one path.
    "BinanceTestnetOrderLedger",
    "Any",
]
