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
from scripts.b0x.crypto import shadow as shadow_lane
from scripts.b0x.crypto import sidecar as sidecar_lane
from scripts.b0x.derivation import DerivationResult, derive_orders
from scripts.b0x.envelope import (
    SHADOW_ENVELOPE_NOT_APPLIED,
    Envelope,
    assert_envelope_locked,
    load_envelope,
)
from scripts.b0x.labels import (
    CROSS_QUOTE_RATIO_TRANSFER,
    SHADOW_SYNTHETIC_FILL,
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
from scripts.b0x.state import B0XPosition, LaneAccountState
from scripts.b0x.table_source import (
    DEFAULT_TABLE_DIR,
    PolicyTable,
    TableUnavailable,
    load_policy_table,
)

MARKET = "crypto"


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


#: Binding = version string + section citation, NOT the whole-file sha (the
#: file is amended in place as the operator signs off further sections;
#: re-stamping every record on each amendment would be noise). The sha below
#: is carried as a reference/reproducibility field only — see contract
#: preamble: "결속 = 버전 문자열 v1.3 + 인용 절 (전체파일 sha … 는 참고값)".
CONTRACT_CITATION = "b0x-experiment-contract-v1 v1.3 (2026-08-09, operator-confirmed)"
CONTRACT_FILE = "~/work/herdr-inbox/b0x-experiment-contract-v1-20260808.md"
CONTRACT_FILE_SHA256_REFERENCE_ONLY = (
    "0125e2ea96b1a54cf0b0a50e6ed85ae1f3a72e7870abe727d2734dbe20e19b1f"
)
#: operator_contract.yaml HEAD at last verification — PR #33 (B0-X 3-surface
#: registration: kis_mock/alpaca_paper_lab/binance_demo). Update alongside
#: any re-verification of the account-map gate.
ACCOUNT_MAP_SHA = "3f402919fca5b68bda187e8e521fc886aefb022a"


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
        "contract": CONTRACT_CITATION,
        "contract_file": CONTRACT_FILE,
        "contract_file_sha256_reference_only": CONTRACT_FILE_SHA256_REFERENCE_ONLY,
        "account_map_sha": ACCOUNT_MAP_SHA,
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
    lines += [
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

    envelope = load_envelope(MARKET)
    assert_envelope_locked(envelope)
    labels = header_labels(extra=(SHADOW_SYNTHETIC_FILL,))
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
        decision = kill_switch_module.evaluate(state=state, envelope=envelope)
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


def _sidecar_state(
    stored: dict[str, Any] | None,
    *,
    fresh: sidecar_lane.FreshTruth,
    now: dt.datetime,
) -> LaneAccountState:
    """B0-X-attributed book for the sidecar, cross-checked against fresh truth."""

    stored = stored or {}
    today = now.astimezone(dt.UTC).date().isoformat()
    same_day = stored.get("utc_day") == today
    positions = tuple(
        B0XPosition(
            symbol=symbol,
            quantity=Decimal(row["quantity"]),
            average_price=Decimal(row["average_price"]),
            invested_notional=Decimal(row["invested_notional"]),
            entry_count=int(row["entry_count"]),
        )
        for symbol, row in sorted((stored.get("positions") or {}).items())
    )
    return LaneAccountState(
        lane=sidecar_lane.LANE,
        quote_currency=sidecar_lane.QUOTE_ASSET,
        cash=fresh.quote_free,
        positions=positions,
        new_entry_symbols_today=(
            tuple(sorted(stored.get("new_entry_symbols_today") or []))
            if same_day
            else ()
        ),
        realized_pnl_today=(
            Decimal(stored.get("realized_pnl_today", "0")) if same_day else Decimal("0")
        ),
        open_order_keys=tuple(sorted(stored.get("open_order_keys") or [])),
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
    labels = header_labels(extra=(CROSS_QUOTE_RATIO_TRANSFER,))
    lane = sidecar_lane.LANE
    outcome = CycleOutcome(lane=lane, at=now)

    with writer_lock(lane=lane, root=Path(out_dir).expanduser()):
        ledger = ObservationLedger(lane=lane, root=Path(out_dir).expanduser())
        ledger.ensure()
        state_path = ledger.lane_dir / "attributed_book.json"

        record = base_record(
            market=MARKET, lane=lane, now=now, envelope=envelope, labels=labels
        )
        record["policy"] = {
            "version": policy.version,
            "policy_hash": policy.policy_hash,
            "canonical": policy.canonical,
        }
        record["symbols"] = dict(sidecar_lane.B0X_SIDECAR_SYMBOLS)
        record["confirm"] = confirm

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

            state = _sidecar_state(load_json_state(state_path), fresh=fresh, now=now)
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
