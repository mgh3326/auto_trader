"""B0-X cycle orchestration — one kickoff = one observation.

Contract §5: *v1 = 수동 kickoff … 사이클 커맨드 1회 실행 = 표 확인 → 주문 파생 →
제출 → reconcile → 관측 기록.* There is no scheduler registration anywhere in
this package; a cycle happens because an operator ran the CLI.

Both lanes share the same skeleton, and the order of the steps is the safety
property:

    writer lock  →  table (or zero orders)  →  account state  →  kill switch
                 →  derive  →  act  →  record

The lock comes first so two processes cannot both read "flat" and both submit.
The table gate comes before the account read so an unusable table costs nothing
and, more importantly, cannot be worked around by whatever the account happens
to look like.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
    SpotDemoDryRunResult,
)
from scripts.b0x import kill_switch as kill_switch_module
from scripts.b0x.contract import (
    SIDECAR_SCOPE,
    account_map_stamp,
    contract_stamp,
)
from scripts.b0x.crypto import shadow as shadow_lane
from scripts.b0x.crypto import sidecar as sidecar_lane
from scripts.b0x.derivation import DerivationResult, derive_orders
from scripts.b0x.envelope import (
    CRYPTO_SHADOW_MARKET_KEY,
    SHADOW_ENVELOPE_NOT_APPLIED,
    Envelope,
    assert_envelope_locked,
    load_envelope,
)
from scripts.b0x.labels import (
    CROSS_QUOTE_RATIO_TRANSFER,
    SHADOW_SYNTHETIC_FILL,
    account_history_labels,
    header_labels,
    render_header,
)
from scripts.b0x.ledger import (
    DEFAULT_OBSERVATION_DIR,
    ObservationLedger,
    load_json_state,
    store_json_state,
    writer_lock,
)
from scripts.b0x.state import LaneAccountState
from scripts.b0x.table_source import (
    DEFAULT_TABLE_DIR,
    PolicyTable,
    TableUnavailable,
    load_policy_table,
)

MARKET = "crypto"

#: contract §4 assigns the shadow lane a synthetic, KRW-denominated book
#: (``shadow.QUOTE_CURRENCY``). Before SHADOW-ALIGN (2026-08-11), this lane
#: was evaluated against the single "crypto" envelope column shared with the
#: sidecar, whose ``daily_loss_kill`` is USDT-denominated
#: (``CRYPTO_SIDECAR_ENVELOPE.quote_currency``) — ``kill_switch.evaluate``
#: correctly failed that comparison closed (``CurrencyMismatchKill``, see
#: ``scripts.b0x.kill_switch.CurrencyMismatchKill``) rather than silently
#: comparing a KRW P&L against a USDT threshold, but the practical effect was
#: that *every* shadow cycle hit this reason code (confirmed against the
#: real environment 2026-08-10T19:02Z). The lane now loads
#: ``envelope.CRYPTO_SHADOW_ENVELOPE`` (``quote_currency="KRW"``,
#: SHADOW-ALIGN) instead, so this exception branch should no longer fire in
#: normal operation — it stays as the same fail-closed backstop
#: ``CurrencyMismatchKill`` always was, not dead code: if the two currencies
#: are ever misaligned again (e.g. a future edit hands this lane the wrong
#: envelope), the cycle still degrades to zero orders with an auditable
#: reason instead of crashing or silently miscomparing.
SHADOW_KILL_CURRENCY_MISMATCH_REASON: str = "kill_switch_currency_mismatch"


@dataclass
class CycleOutcome:
    lane: str
    at: dt.datetime
    zero_order_reason: str | None = None
    table_hash: str | None = None
    table_generated_at: str | None = None
    table_age_seconds: int | None = None
    derivation: DerivationResult | None = None
    record: dict[str, Any] = field(default_factory=dict)
    artifact_path: Path | None = None
    contaminated: bool = False
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


def base_record(
    *,
    market: str,
    lane: str,
    now: dt.datetime,
    envelope: Envelope,
    labels: tuple[str, ...],
) -> dict[str, Any]:
    """Market-agnostic observation-record skeleton, shared by every lane."""

    return {
        "schema": "b0x.observation.v1",
        "lane": lane,
        "market": market,
        "at": now.isoformat(),
        "labels": list(labels),
        "envelope": envelope.canonical(),
        "contract": contract_stamp(),
        "account_map": account_map_stamp(),
    }


def render_cycle_report(record: dict[str, Any], *, labels: tuple[str, ...]) -> str:
    lines = [
        f"# B0-X {record['lane']} — {record['at']}",
        "",
        render_header(labels),
        "",
    ]
    if record.get("zero_order_reason"):
        lines += [
            f"**ZERO ORDERS** — `{record['zero_order_reason']}`",
            "",
            f"detail: {record.get('zero_order_detail', '')}",
            "",
        ]
    contract = record.get("contract") or {}
    account_map = record.get("account_map") or {}
    account_map_line = (
        f"account map: `{account_map.get('repo', '-')}@"
        f"{account_map.get('commit', '-')}` "
        f"canonical=`{account_map.get('canonical_surface', '-')}`"
    )
    if reason := account_map.get("commit_reason"):
        account_map_line += f" reason=`{reason}`"
    lines += [
        f"contract: `{contract.get('version', '-')}` "
        f"({', '.join(contract.get('clauses') or {}) or '-'}) · "
        f"file sha256 (reference only): `{contract.get('file_sha256_reference_only', '-')}`",
        account_map_line,
    ]
    if record.get("sidecar_scope"):
        lines.append(f"sidecar scope: `{record['sidecar_scope']}`")
    lines += [
        "",
        f"policy_table_hash: `{record.get('policy_table_hash', '-')}`",
        f"account_state_hash: `{record.get('account_state_hash', '-')}`",
        f"cycle_id: `{record.get('cycle_id', '-')}`",
        f"derivation_hash: `{record.get('derivation_hash', '-')}`",
        "",
        f"orders derived: {len(record.get('orders') or [])} · "
        f"skipped legs: {len(record.get('skipped') or [])}",
        "",
    ]
    orders = record.get("orders") or []
    if orders:
        lines += [
            "| # | symbol | side | leg | table price | ratio | notional | qty frac |",
            "|---:|---|---|---|---:|---:|---:|---:|",
        ]
        for order in orders:
            lines.append(
                f"| {order['sequence']} | {order['symbol']} | {order['side']} | "
                f"{order['leg']} | {order['table_price']} | {order['price_ratio']} | "
                f"{order['notional'] or '-'} | {order['quantity_fraction'] or '-'} |"
            )
        lines.append("")
    skipped = record.get("skipped") or []
    if skipped:
        lines += ["| symbol | leg | reason | detail |", "|---|---|---|---|"]
        for skip in skipped:
            lines.append(
                f"| {skip['symbol']} | {skip['leg']} | `{skip['reason']}` | {skip['detail']} |"
            )
        lines.append("")
    if record.get("kill_switch"):
        lines += [f"kill switch: `{record['kill_switch']}`", ""]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Shadow lane (본선)
# ---------------------------------------------------------------------------


async def run_shadow_cycle(
    *,
    now: dt.datetime,
    table_dir: Path = DEFAULT_TABLE_DIR,
    out_dir: Path = DEFAULT_OBSERVATION_DIR,
) -> CycleOutcome:
    """One Upbit shadow-sim cycle. Places zero real orders on any venue."""

    # SHADOW-ALIGN: the shadow lane's own KRW-denominated envelope, not the
    # sidecar's USDT one — see the note above ``CRYPTO_SHADOW_ENVELOPE`` in
    # ``scripts.b0x.envelope`` for the conversion. ``MARKET`` ("crypto")
    # remains the *policy table* market key (both lanes read the same table)
    # and the record's ``market`` field — unrelated to which envelope binds
    # the kill switch, so it is deliberately left unchanged everywhere else
    # in this function.
    envelope = load_envelope(CRYPTO_SHADOW_MARKET_KEY)
    assert_envelope_locked(envelope)
    labels = header_labels(lane=shadow_lane.LANE, extra=(SHADOW_SYNTHETIC_FILL,))
    lane = shadow_lane.LANE
    outcome = CycleOutcome(lane=lane, at=now)

    with writer_lock(lane=lane, root=Path(out_dir).expanduser()):
        ledger = ObservationLedger(lane=lane, root=Path(out_dir).expanduser())
        ledger.ensure()
        portfolio_path = ledger.lane_dir / "portfolio.json"

        stored = load_json_state(portfolio_path)
        portfolio = (
            shadow_lane.VirtualPortfolio.from_json(stored)
            if stored
            else shadow_lane.VirtualPortfolio.seed(now=now)
        )
        day_rolled = portfolio.roll_utc_day(now=now)

        record = base_record(
            market=MARKET, lane=lane, now=now, envelope=envelope, labels=labels
        )
        record["envelope_application"] = SHADOW_ENVELOPE_NOT_APPLIED
        record["touch_rule"] = {
            "id": shadow_lane.TOUCH_RULE_ID,
            "statement": shadow_lane.TOUCH_RULE_STATEMENT,
            "fee_rate": format(shadow_lane.UPBIT_KRW_FEE_RATE, "f"),
            "bar_period": shadow_lane.BAR_PERIOD,
        }
        record["utc_day_rolled"] = day_rolled
        record["seed_cash"] = format(portfolio.seed_cash, "f")

        # --- settle resting orders against completed bars (the touch rule) ---
        symbols_with_orders = [order.symbol for order in portfolio.open_orders]
        bars = (
            await shadow_lane.fetch_bars(symbols_with_orders, now=now)
            if symbols_with_orders
            else {}
        )
        fills = shadow_lane.apply_fills(portfolio, bars)
        record["fills"] = [fill.to_json() for fill in fills]
        record["bars_evaluated"] = {
            symbol: len(rows) for symbol, rows in sorted(bars.items())
        }

        # --- table gate ---
        table, unavailable = _table_or_reason(now=now, table_dir=Path(table_dir))
        if table is None:
            assert unavailable is not None
            record["zero_order_reason"] = unavailable.reason
            record["zero_order_detail"] = unavailable.detail
            record["orders"] = []
            record["skipped"] = []
            # Contract §2-2: no silent reuse. The resting book from the last
            # good table is cancelled rather than left working on stale intent.
            record["cancelled_stale_orders"] = len(portfolio.open_orders)
            portfolio.open_orders = []
            outcome.zero_order_reason = unavailable.reason
            store_json_state(portfolio_path, portfolio.to_json())
            ledger.record_cycle(record)
            outcome.record = record
            outcome.artifact_path = ledger.write_artifact(
                name=f"{now.strftime('%Y%m%dT%H%M%SZ')}-cycle.md",
                content=render_cycle_report(record, labels=labels),
            )
            return outcome

        outcome.table_hash = table.policy_table_hash
        outcome.table_generated_at = table.generated_at.isoformat()
        outcome.table_age_seconds = int(table.age.total_seconds())
        record["policy_table_hash"] = table.policy_table_hash
        record["policy_table_path"] = str(table.path)
        record["policy_table_generated_at"] = table.generated_at.isoformat()
        record["policy_table_age_seconds"] = int(table.age.total_seconds())

        state = portfolio.account_state()
        try:
            decision = kill_switch_module.evaluate(state=state, envelope=envelope)
        except kill_switch_module.CurrencyMismatchKill as exc:
            record["zero_order_reason"] = SHADOW_KILL_CURRENCY_MISMATCH_REASON
            record["zero_order_detail"] = str(exc)
            record["orders"] = []
            record["skipped"] = []
            # Contract §2-2: no silent reuse. A kill decision this lane cannot
            # safely evaluate is treated the same as an unusable table — the
            # resting book from the last good cycle is cancelled rather than
            # left working on intent nothing here re-validated.
            record["cancelled_stale_orders"] = len(portfolio.open_orders)
            portfolio.open_orders = []
            outcome.zero_order_reason = SHADOW_KILL_CURRENCY_MISMATCH_REASON
            store_json_state(portfolio_path, portfolio.to_json())
            ledger.record_cycle(record)
            outcome.record = record
            outcome.artifact_path = ledger.write_artifact(
                name=f"{now.strftime('%Y%m%dT%H%M%SZ')}-cycle.md",
                content=render_cycle_report(record, labels=labels),
            )
            return outcome
        derivation = derive_orders(
            table=table,
            state=state,
            envelope=envelope,
            kill_switch=decision,
            lane_universe=None,
            apply_envelope=False,  # contract §4 footnote
        )

        if decision.tripped:
            portfolio.open_orders = []
            notice = decision.operator_notice(lane=lane)
            if notice:
                ledger.record_notice(at=now, text=notice, lane=lane)
                record["operator_notice"] = notice
            placed: list[shadow_lane.VirtualOrder] = []
        else:
            placed = shadow_lane.place_derived_orders(
                portfolio,
                derivation.orders,
                now=now,
                cycle_id=derivation.cycle_id,
            )

        record.update(
            {
                "cycle_id": derivation.cycle_id,
                "account_state_hash": derivation.account_state_hash,
                "derivation_hash": derivation.derivation_hash(),
                "orders": [order.canonical() for order in derivation.orders],
                "skipped": [skip.canonical() for skip in derivation.skipped],
                "kill_switch": decision.canonical(),
                "virtual_orders_placed": [order.to_json() for order in placed],
                "portfolio_after": portfolio.to_json(),
                "real_orders": 0,
                "live_contact": 0,
            }
        )

        store_json_state(portfolio_path, portfolio.to_json())
        ledger.record_cycle(record)
        outcome.derivation = derivation
        outcome.record = record
        outcome.artifact_path = ledger.write_artifact(
            name=f"{now.strftime('%Y%m%dT%H%M%SZ')}-cycle.md",
            content=render_cycle_report(record, labels=labels),
        )
        return outcome


# ---------------------------------------------------------------------------
# Sidecar lane (Binance Spot Demo)
# ---------------------------------------------------------------------------


#: Contract v1.5 ①: the sidecar's realized-P&L input has no source. Fills are
#: not reconciled back into an attributed book on this lane (see
#: ``scripts.b0x.crypto.sidecar``'s "Known limitation" section), and the file
#: this number used to be loaded from never existed, so it was always zero
#: anyway. Recording the absence beats recording a zero that reads like a
#: measurement: with no P&L source the 일 손실 5 USDT kill cannot fire here.
SIDECAR_REALIZED_PNL_UNAVAILABLE: str = (
    "realized_pnl_today has no source on this lane — fills are not reconciled "
    "into an attributed book (사이드카 = 매수측 체결충실도 표본, 계약 v1.3 ②), "
    "so the 일 손실 kill cannot fire. Not a measured zero."
)


def _sidecar_state(*, fresh: sidecar_lane.FreshTruth) -> LaneAccountState:
    """Sidecar account state, derived entirely from this cycle's broker read.

    Contract v1.5 ①. There is no persisted book to load: ``positions`` is empty
    **by construction**, not by omission — the v1 attribution rule makes every
    sellable base balance ``foreign`` (a B0-X fill included), so this lane never
    owns an attributed position to average down or sell, which is exactly the
    buy-side-only scope contract v1.3 ② assigned it. The §4 caps do not depend
    on that emptiness: they read :meth:`FreshTruth.broker_truth`, which counts
    the account's sellable balances and B0-X's own resting orders directly.
    """

    return LaneAccountState(
        lane=sidecar_lane.LANE,
        quote_currency=sidecar_lane.QUOTE_ASSET,
        cash=fresh.quote_free,
        broker_truth=fresh.broker_truth(),
        positions=(),
        realized_pnl_today=Decimal("0"),
        open_order_keys=(),
        foreign_open_order_count=len(fresh.foreign_open_orders),
        foreign_position_symbols=fresh.foreign_base_assets,
    )


async def run_sidecar_cycle(
    *,
    now: dt.datetime,
    table_dir: Path = DEFAULT_TABLE_DIR,
    out_dir: Path = DEFAULT_OBSERVATION_DIR,
    confirm: bool = False,
    client: Any | None = None,
) -> CycleOutcome:
    """One Binance Spot Demo sidecar cycle.

    ``confirm=False`` (the default) dispatches zero mutation HTTP: the ROB-298
    client returns a dry-run sentinel instead. Read-only fresh-truth calls do
    happen — the account map requires them before any use of this assignment.
    """

    envelope = load_envelope(MARKET)
    assert_envelope_locked(envelope)
    sidecar_lane.assert_sidecar_enabled()
    policy = sidecar_lane.build_policy(envelope)
    lane = sidecar_lane.LANE
    labels = header_labels(
        lane=lane,
        extra=(*account_history_labels(lane), CROSS_QUOTE_RATIO_TRANSFER),
    )
    outcome = CycleOutcome(lane=lane, at=now)

    with writer_lock(lane=lane, root=Path(out_dir).expanduser()):
        ledger = ObservationLedger(lane=lane, root=Path(out_dir).expanduser())
        ledger.ensure()

        record = base_record(
            market=MARKET, lane=lane, now=now, envelope=envelope, labels=labels
        )
        record["realized_pnl_source"] = SIDECAR_REALIZED_PNL_UNAVAILABLE
        record["policy"] = {
            "version": policy.version,
            "policy_hash": policy.policy_hash,
            "canonical": policy.canonical,
        }
        record["symbols"] = dict(sidecar_lane.B0X_SIDECAR_SYMBOLS)
        record["confirm"] = confirm
        # Contract v1.3 ②: this lane samples buy-side fill fidelity only. The
        # sell side is unreachable here (the v1 attribution rule halts the lane
        # on its own first fill), so it is observed on the Upbit shadow lane.
        # Stamped per-cycle so no artifact can be read as a round-trip result.
        record["sidecar_scope"] = SIDECAR_SCOPE

        owns_client = client is None
        base_url = sidecar_lane.base_url()
        record["base_url_host"] = httpx.URL(base_url).host
        if owns_client:
            client = BinanceSpotDemoExecutionClient.from_env()

        try:
            fresh = await sidecar_lane.read_fresh_truth(client)
            record["fresh_truth"] = fresh.status_only()
            outcome.contaminated = fresh.contaminated
            if fresh.contaminated:
                record["contaminated"] = True

            table, unavailable = _table_or_reason(now=now, table_dir=Path(table_dir))
            if table is None:
                assert unavailable is not None
                record["zero_order_reason"] = unavailable.reason
                record["zero_order_detail"] = unavailable.detail
                record["orders"] = []
                record["skipped"] = []
                record["submitted"] = []
                outcome.zero_order_reason = unavailable.reason
                ledger.record_cycle(record)
                outcome.record = record
                outcome.artifact_path = ledger.write_artifact(
                    name=f"{now.strftime('%Y%m%dT%H%M%SZ')}-cycle.md",
                    content=render_cycle_report(record, labels=labels),
                )
                return outcome

            outcome.table_hash = table.policy_table_hash
            outcome.table_generated_at = table.generated_at.isoformat()
            outcome.table_age_seconds = int(table.age.total_seconds())
            record["policy_table_hash"] = table.policy_table_hash
            record["policy_table_path"] = str(table.path)
            record["policy_table_generated_at"] = table.generated_at.isoformat()
            record["policy_table_age_seconds"] = int(table.age.total_seconds())

            state = _sidecar_state(fresh=fresh)
            record["broker_truth"] = state.broker_truth.canonical()
            decision = kill_switch_module.evaluate(state=state, envelope=envelope)
            derivation = derive_orders(
                table=table,
                state=state,
                envelope=envelope,
                kill_switch=decision,
                lane_universe=frozenset(sidecar_lane.B0X_SIDECAR_SYMBOLS),
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
                notice = decision.operator_notice(lane=lane)
                if notice:
                    ledger.record_notice(at=now, text=notice, lane=lane)
                    record["operator_notice"] = notice
                record["planned"] = []
                record["blocked"] = []
                record["submitted"] = []
                record["cancelled"] = await _cancel_b0x_open_orders(
                    client, fresh, confirm=confirm
                )
            else:
                venue_symbols = sorted(
                    {
                        sidecar_lane.B0X_SIDECAR_SYMBOLS[order.symbol]
                        for order in derivation.orders
                        if order.symbol in sidecar_lane.B0X_SIDECAR_SYMBOLS
                    }
                )
                filters: dict[str, sidecar_lane.SymbolFilters] = {}
                prices: dict[str, Decimal] = {}
                for symbol in venue_symbols:
                    filters[symbol] = await sidecar_lane.fetch_symbol_filters(
                        base_url=base_url, symbol=symbol
                    )
                    prices[symbol] = await sidecar_lane.fetch_reference_price(
                        base_url=base_url, symbol=symbol
                    )

                planned, blocked = sidecar_lane.plan_orders(
                    derivation.orders,
                    envelope=envelope,
                    filters=filters,
                    reference_prices=prices,
                    base_balances=fresh.base_balances,
                )
                record["planned"] = [order.to_json() for order in planned]
                record["blocked"] = [order.to_json() for order in blocked]

                if fresh.contaminated:
                    record["submitted"] = []
                    record["submission_blocked"] = (
                        "CONTAMINATED — venue state B0-X did not create; refusing "
                        "to submit (contract §2-3)"
                    )
                else:
                    record["submitted"] = await sidecar_lane.submit_planned(
                        client,
                        planned,
                        envelope=envelope,
                        fresh_truth=fresh,
                        confirm=confirm,
                    )

            record["live_contact"] = 0
            record["real_orders"] = 0  # Demo venue only; no live account touched
            ledger.record_cycle(record)
            outcome.record = record
            outcome.artifact_path = ledger.write_artifact(
                name=f"{now.strftime('%Y%m%dT%H%M%SZ')}-cycle.md",
                content=render_cycle_report(record, labels=labels),
            )
            return outcome
        finally:
            if owns_client and hasattr(client, "aclose"):
                await client.aclose()


async def _cancel_b0x_open_orders(
    client: Any, fresh: sidecar_lane.FreshTruth, *, confirm: bool
) -> list[dict[str, Any]]:
    """Contract §2-4: on a kill, outstanding B0-X orders are cancelled.

    Only orders carrying the B0-X ``clientOrderId`` prefix are touched — a
    foreign order belongs to whoever wrote it (mock/CLAUDE.md §4: 다른 전략의
    미체결 행을 취소하지 않는다).
    """

    cancelled: list[dict[str, Any]] = []
    for symbol, orders in sorted(fresh.open_orders.items()):
        for order in orders:
            if not str(order.client_order_id).startswith(
                sidecar_lane.CLIENT_ORDER_ID_PREFIX
            ):
                continue
            result = await client.cancel_order(
                symbol=symbol,
                client_order_id=order.client_order_id,
                confirm=confirm,
            )
            dispatched = not isinstance(result, SpotDemoDryRunResult)
            cancelled.append(
                {
                    "symbol": symbol,
                    "client_order_id": order.client_order_id,
                    "dispatched": dispatched,
                    "status": getattr(result, "status", None) if dispatched else None,
                }
            )
    return cancelled


__all__ = [
    "CycleOutcome",
    "base_record",
    "render_cycle_report",
    "run_shadow_cycle",
    "run_sidecar_cycle",
]
