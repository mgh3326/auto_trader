"""One manual B0-X US cycle for the dedicated Alpaca paper lab.

Safety order is deliberate:

    writer lock → US RTH → table → lab fresh truth → ratio kill → derive
                → plan → explicit-confirm submit/cancel → observation

The RTH/table gates run before any account read.  The account read uses only
``alpaca_paper_lab`` tooling; it has no default-account fallback.  A normal
CLI invocation passes ``confirm=False`` and therefore never invokes an order
preview, submit, or cancellation surface.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.mcp_server.tooling.market_session import US_SESSION_REGULAR, us_market_session
from scripts.b0x.cycle import base_record, render_cycle_report
from scripts.b0x.derivation import DerivationResult, derive_orders
from scripts.b0x.envelope import Envelope, assert_envelope_locked, load_envelope
from scripts.b0x.kill_switch import MissingNavForRatioKill, evaluate
from scripts.b0x.labels import header_labels
from scripts.b0x.ledger import DEFAULT_OBSERVATION_DIR, ObservationLedger, writer_lock
from scripts.b0x.state import LaneAccountState
from scripts.b0x.table_source import (
    DEFAULT_TABLE_DIR,
    PolicyTable,
    TableUnavailable,
    load_policy_table,
)
from scripts.b0x.us import alpaca
from scripts.b0x.us.contract import account_map_stamp, contract_stamp
from scripts.policy_table.core.trust_labels import CROSS_MARKET_TRANSFER_UNVALIDATED

MARKET = "us"
LANE = alpaca.LANE
OUTSIDE_RTH_REASON = "outside_us_regular_session"
FRESH_TRUTH_UNAVAILABLE_REASON = "lab_fresh_truth_unavailable"
REALIZED_PNL_UNAVAILABLE_REASON = "realized_pnl_unavailable"
MISSING_NAV_FOR_RATIO_KILL_REASON = "missing_nav_for_ratio_kill"
INVALID_US_TABLE_SIZING_REASON = "invalid_us_table_sizing"

US_REALIZED_PNL_UNAVAILABLE = (
    "realized_pnl_today has no dedicated source when a b0xu execution exists "
    "today; this cycle fails closed rather than treating it as a measured zero."
)


@dataclass
class UsCycleOutcome:
    lane: str
    at: dt.datetime
    zero_order_reason: str | None = None
    table_hash: str | None = None
    table_generated_at: str | None = None
    table_age_seconds: int | None = None
    derivation: DerivationResult | None = None
    record: dict[str, Any] = field(default_factory=dict)
    artifact_path: Path | None = None
    exit_code: int = 0

    @property
    def order_count(self) -> int:
        return 0 if self.derivation is None else len(self.derivation.orders)


def _table_or_reason(
    *, now: dt.datetime, table_dir: Path
) -> tuple[PolicyTable | None, TableUnavailable | None]:
    result = load_policy_table(market=MARKET, now=now, table_dir=table_dir)
    if isinstance(result, TableUnavailable):
        return None, result
    return result, None


def _us_table_sizing_error(*, table: PolicyTable, envelope: Envelope) -> str | None:
    """Validate the table-owned USD selection before any lab account read.

    The generic derivation core intentionally retains legacy market fallbacks.
    This US lane must not silently take its envelope ceiling when the US table
    omitted the explicitly generated ``new_entry_notional_usd`` selection.
    """

    config = table.config
    if config.get("quote_currency") != alpaca.QUOTE_CURRENCY:
        return (
            "policy table quote_currency must be USD for alpaca_paper_lab; "
            f"got {config.get('quote_currency')!r}"
        )
    raw_selected = config.get("new_entry_notional_usd")
    try:
        selected = Decimal(str(raw_selected))
    except Exception:  # noqa: BLE001 - a malformed table must close the lane
        return "policy table new_entry_notional_usd is missing or malformed"
    if not selected.is_finite() or not (
        alpaca.US_NEW_ENTRY_NOTIONAL_MIN <= selected <= envelope.per_order_notional
    ):
        return (
            "policy table new_entry_notional_usd must be inside the signed "
            f"${format(alpaca.US_NEW_ENTRY_NOTIONAL_MIN, 'f')}-${format(envelope.per_order_notional, 'f')} band"
        )
    return None


def broker_state(*, fresh: alpaca.FreshTruth) -> LaneAccountState:
    """Attribution-scoped state plus account-wide broker truth for US.

    Positions and own pending orders are read from the same current lab
    snapshot.  Foreign residue remains foreign and contaminates the cycle; it
    is never repurposed as B0-X inventory.  The generic cap inputs are still
    account-wide by contract, so foreign *sellable* positions consume a slot.
    """

    if fresh.realized_pnl_today is None:
        raise alpaca.RealizedPnlUnavailable(US_REALIZED_PNL_UNAVAILABLE)
    return LaneAccountState(
        lane=LANE,
        quote_currency=alpaca.QUOTE_CURRENCY,
        cash=fresh.cash,
        broker_truth=fresh.broker_truth(),
        positions=fresh.own_positions,
        realized_pnl_today=fresh.realized_pnl_today,
        nav=fresh.nav,
        cumulative_deployment_readable=fresh.cumulative_deployment_readable,
        open_order_keys=tuple(order.broker_order_id for order in fresh.own_open_orders),
        foreign_open_order_count=len(fresh.foreign_open_orders),
        foreign_position_symbols=fresh.foreign_position_symbols,
        notes=fresh.position_linkage_failures,
    )


async def run_us_cycle(
    *,
    now: dt.datetime,
    table_dir: Path = DEFAULT_TABLE_DIR,
    out_dir: Path = DEFAULT_OBSERVATION_DIR,
    confirm: bool = False,
    readers: alpaca.LabReaders | None = None,
    submitter: Any = None,
    canceler: Any = None,
) -> UsCycleOutcome:
    """Run one US observation cycle; mutation requires explicit internal confirm.

    The command-line runner never passes ``confirm=True``.  The injection seams
    exist for offline fake/stub tests and a separately authorized future
    operator entrypoint, not to turn this module into an implicit broker call.
    """

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    envelope = load_envelope(MARKET)
    assert_envelope_locked(envelope)
    labels = header_labels(lane=LANE, extra=(CROSS_MARKET_TRANSFER_UNVALIDATED,))
    outcome = UsCycleOutcome(lane=LANE, at=now)

    with writer_lock(lane=LANE, root=Path(out_dir).expanduser()):
        ledger = ObservationLedger(lane=LANE, root=Path(out_dir).expanduser())
        ledger.ensure()
        record = base_record(
            market=MARKET,
            lane=LANE,
            now=now,
            envelope=envelope,
            labels=labels,
        )
        # US binding is v1.7 + clauses, not the older generic sidecar stamp.
        record["contract"] = contract_stamp()
        record["account_map"] = account_map_stamp(
            account_map_path=Path(table_dir).expanduser()
        )
        record["confirm"] = confirm

        def finish_zero(reason: str, detail: str) -> UsCycleOutcome:
            record["zero_order_reason"] = reason
            record["zero_order_detail"] = detail
            record["orders"] = []
            record["skipped"] = []
            record["planned"] = []
            record["blocked"] = []
            record["submitted"] = []
            outcome.zero_order_reason = reason
            ledger.record_cycle(record)
            outcome.record = record
            outcome.artifact_path = ledger.write_artifact(
                name=f"{now.strftime('%Y%m%dT%H%M%SZ')}-cycle.md",
                content=render_cycle_report(record, labels=labels),
            )
            return outcome

        # Cheapest zero-order gate: no table or account/ledger/broker read
        # happens outside XNYS regular session.
        session = us_market_session(now)
        record["us_market_session"] = session
        if session != US_SESSION_REGULAR:
            return finish_zero(
                OUTSIDE_RTH_REASON,
                f"now={now.isoformat()} session={session!r}; US RTH only",
            )

        table, unavailable = _table_or_reason(now=now, table_dir=Path(table_dir))
        if table is None:
            assert unavailable is not None
            return finish_zero(unavailable.reason, unavailable.detail)
        sizing_error = _us_table_sizing_error(table=table, envelope=envelope)
        if sizing_error is not None:
            return finish_zero(INVALID_US_TABLE_SIZING_REASON, sizing_error)

        outcome.table_hash = table.policy_table_hash
        outcome.table_generated_at = table.generated_at.isoformat()
        outcome.table_age_seconds = int(table.age.total_seconds())
        record.update(
            {
                "policy_table_hash": table.policy_table_hash,
                "policy_table_path": str(table.path),
                "policy_table_generated_at": table.generated_at.isoformat(),
                "policy_table_age_seconds": int(table.age.total_seconds()),
            }
        )

        try:
            fresh = await alpaca.read_fresh_truth(now=now, readers=readers)
        except Exception as exc:  # fail closed; never leak broker error bodies/secrets
            return finish_zero(
                FRESH_TRUTH_UNAVAILABLE_REASON,
                f"{type(exc).__name__}: alpaca_paper_lab read-only truth unavailable",
            )
        record["fresh_truth"] = fresh.status_only()

        try:
            state = broker_state(fresh=fresh)
        except alpaca.RealizedPnlUnavailable:
            return finish_zero(
                REALIZED_PNL_UNAVAILABLE_REASON, US_REALIZED_PNL_UNAVAILABLE
            )
        record["broker_truth"] = state.broker_truth.canonical()
        record["contaminated"] = state.contaminated
        record["contamination"] = {
            "foreign_open_order_count": state.foreign_open_order_count,
            "foreign_position_symbols": list(state.foreign_position_symbols),
            "position_linkage_failures": list(state.notes),
        }

        try:
            decision = evaluate(state=state, envelope=envelope)
        except MissingNavForRatioKill:
            return finish_zero(
                MISSING_NAV_FOR_RATIO_KILL_REASON,
                "portfolio_value unavailable/non-positive; NAV-ratio kill fails closed",
            )

        derivation = derive_orders(
            table=table,
            state=state,
            envelope=envelope,
            kill_switch=decision,
            lane_universe=None,
            apply_envelope=True,
        )
        outcome.derivation = derivation
        record.update(
            {
                "cycle_id": derivation.cycle_id,
                "account_state_hash": derivation.account_state_hash,
                "derivation_hash": derivation.derivation_hash(),
                "orders": [order.canonical() for order in derivation.orders],
                "skipped": [skip.canonical() for skip in derivation.skipped],
                "kill_switch": decision.canonical(),
            }
        )

        if decision.tripped:
            record["planned"] = []
            record["blocked"] = []
            record["submitted"] = []
            if confirm is True and canceler is not None:
                cancel_kwargs: dict[str, Any] = {"fresh": fresh, "confirm": True}
                cancel_kwargs["canceler"] = canceler
                record["cancelled"] = await alpaca.cancel_own_open_orders(
                    **cancel_kwargs
                )
            else:
                record["cancelled"] = []
                record["cancellation_skipped"] = (
                    "confirmation or approved injected lab canceler absent — "
                    "no cancellation call issued"
                )
        else:
            planned, blocked = alpaca.plan_orders(
                derivation.orders,
                envelope=envelope,
                cash=state.cash,
                held_quantities=fresh.sellable_owned_quantities(),
                invested_notional_by_symbol=fresh.invested_notional_by_symbol(),
                sell_source_client_order_ids=fresh.sell_source_client_order_ids,
            )
            record["planned"] = [order.to_json() for order in planned]
            record["blocked"] = [order.to_json() for order in blocked]
            submitted: list[dict[str, Any]] = []
            if state.contaminated:
                record["submission_skipped"] = (
                    "contaminated lab account state — foreign/unlinked residue blocks submit"
                )
            elif confirm is not True:
                record["submission_skipped"] = (
                    "confirm=False — plan only; no preview or broker mutation call issued"
                )
            elif submitter is None:
                record["submission_skipped"] = (
                    "no approved injected alpaca_paper_lab submitter — "
                    "no preview or broker mutation call issued"
                )
            else:
                for planned_order in planned:
                    submit_kwargs: dict[str, Any] = {
                        "planned": planned_order,
                        "table": table,
                        "confirm": True,
                        "broker_truth": state.broker_truth,
                    }
                    submit_kwargs["submitter"] = submitter
                    submitted.append(await alpaca.submit_planned_order(**submit_kwargs))
            record["submitted"] = submitted

        ledger.record_cycle(record)
        outcome.record = record
        outcome.artifact_path = ledger.write_artifact(
            name=f"{now.strftime('%Y%m%dT%H%M%SZ')}-cycle.md",
            content=render_cycle_report(record, labels=labels),
        )
        return outcome


__all__ = [
    "FRESH_TRUTH_UNAVAILABLE_REASON",
    "INVALID_US_TABLE_SIZING_REASON",
    "LANE",
    "MARKET",
    "MISSING_NAV_FOR_RATIO_KILL_REASON",
    "OUTSIDE_RTH_REASON",
    "REALIZED_PNL_UNAVAILABLE_REASON",
    "US_REALIZED_PNL_UNAVAILABLE",
    "UsCycleOutcome",
    "broker_state",
    "run_us_cycle",
]
