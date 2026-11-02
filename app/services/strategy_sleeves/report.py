"""Read-only per-sleeve report (ROB-1470).

Per sleeve: budget, cash used, P&L and trades (the sleeve book), the safety
state, and the canary statistics of the ROB-1468 design (``canary``): the
operator's pre-registration, the signal cohort marked at H, and the statistical
verdict state. Safety and the statistical verdict are separate fields; neither
reads the other. Totals are per currency only.

``load_report`` reads the database only; ``build_report_row`` and
``render_text`` are pure so a fixture can pin the output.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.strategy_sleeves.book import SleeveBook, compute_sleeve_book
from app.services.strategy_sleeves.canary import (
    CohortSummary,
    HorizonMark,
    build_cohort,
    episodes_of,
    safety_state,
    statistical_verdict,
)
from app.services.strategy_sleeves.marks import (
    HorizonFn,
    MarkFn,
    horizon_closes,
    latest_close_marks,
)
from app.services.strategy_sleeves.store import (
    history,
    list_sleeves,
    load_sleeve_inputs,
    prereg_of,
    sleeve_symbol,
)
from app.services.strategy_sleeves.vocabulary import CANARY_RULES, SleeveState


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = format(value.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def _pct(value: Decimal | None) -> str | None:
    return None if value is None else _text(value.quantize(Decimal("0.0001")))


def first_live_at(versions: Sequence[Any]) -> datetime | None:
    return next((v.set_at for v in versions if v.state == SleeveState.LIVE), None)


def budget_at_signal(versions: Sequence[Any], signal_at: datetime) -> Decimal:
    """The budget in effect when the signal was raised (version 1 if earlier)."""
    chosen = versions[0]
    for version in versions:
        if version.set_at is not None and version.set_at <= signal_at:
            chosen = version
    return Decimal(str(chosen.budget_cap))


def build_report_row(
    *,
    sleeve: Any,
    versions: Sequence[Any],
    book: SleeveBook,
    cohort: CohortSummary,
    diagnostics: Mapping[str, int],
    now: datetime,
) -> dict[str, Any]:
    current = versions[-1]
    rule = CANARY_RULES.get(sleeve.rule_id)
    budget = Decimal(str(current.budget_cap))
    loss_limit = Decimal(str(current.loss_limit))
    prereg = prereg_of(current)
    loss_reached = None if book.loss is None else book.loss >= loss_limit
    budget_left = None if book.used is None else budget - book.used
    verdict, final_date = statistical_verdict(
        prereg_complete=prereg.complete,
        first_live_at=first_live_at(versions),
        max_duration_days=prereg.max_duration_days,
        now=now,
    )
    return {
        "sleeve_id": sleeve.id,
        "sleeve_key": sleeve.sleeve_key,
        "rule_id": sleeve.rule_id,
        "account_mode": sleeve.account_mode,
        "market": sleeve.market,
        "currency": sleeve.currency,
        "state": current.state,
        "version": current.version,
        "set_by": current.set_by,
        "set_at": current.set_at.isoformat() if current.set_at else None,
        "budget_cap": _text(budget),
        "used": _text(book.used),
        "budget_left": _text(budget_left),
        "held_cost_basis": _text(book.held_cost_basis),
        "open_order_notional": _text(book.open_order_notional),
        "unbooked_fill_notional": _text(book.unbooked_fill_notional),
        "realized_pnl": _text(book.realized_pnl),
        "unrealized_pnl": _text(book.unrealized_pnl),
        "fees": _text(book.fees),
        "net_pnl": _text(book.net_pnl),
        "loss_limit": _text(loss_limit),
        "loss_limit_reached": loss_reached,
        "safety_state": safety_state(
            state=current.state,
            loss_limit_reached=loss_reached,
            budget_left=budget_left,
        ),
        "trades": book.trades,
        "unmatched_sell_quantity": _text(book.unmatched_sell_quantity),
        "missing_marks": list(book.missing_marks),
        "positions": [
            {
                "symbol": p.symbol,
                "quantity": _text(p.quantity),
                "cost_basis": _text(p.cost_basis),
                "mark": _text(p.mark),
                "unrealized_pnl": _text(p.unrealized_pnl),
            }
            for p in book.positions
        ],
        "canary": {
            "design_section": rule.design_section if rule else None,
            "design_metric": rule.metric if rule else None,
            "preregistration": {
                "metric": prereg.metric,
                "min_effect_pct_of_budget": _text(prereg.min_effect_pct),
                "horizon_sessions": prereg.horizon_sessions,
                "max_duration_days": prereg.max_duration_days,
                "complete": prereg.complete,
            },
            "verdict_state": verdict,
            "final_date": final_date.isoformat() if final_date else None,
            "sample_unit": "episode (a signal plus its adds) marked at H",
            "signals": cohort.signals,
            "episodes": cohort.episodes,
            "matured": cohort.matured,
            "matured_unfilled": cohort.matured_unfilled,
            "open": cohort.open,
            "unmeasured": cohort.unmeasured,
            "mean_result_pct_of_budget": _pct(cohort.mean_result_pct_of_budget),
            "baseline": "not_recorded",
            "mean_d_pct_of_budget": None,
        },
        "attribution_skipped": dict(sorted(diagnostics.items())),
        "versions": len(versions),
    }


def currency_totals(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    """Totals per currency; KRW and USD are never added together."""
    sums: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {
            "budget_cap": Decimal("0"),
            "used": Decimal("0"),
            "net_pnl": Decimal("0"),
        }
    )
    unknown: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        bucket = sums[row["currency"]]
        for field in ("budget_cap", "used", "net_pnl"):
            if row[field] is None:
                unknown[row["currency"]].add(field)
            else:
                bucket[field] += Decimal(row[field])
    return {
        currency: {
            field: ("unknown" if field in unknown[currency] else _text(value) or "0")
            for field, value in values.items()
        }
        for currency, values in sorted(sums.items())
    }


def render_text(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "no sleeves\n"
    lines: list[str] = []
    for row in rows:
        canary = row["canary"]
        prereg = canary["preregistration"]
        lines.append(
            f"[{row['sleeve_id']}] {row['sleeve_key']}  {row['rule_id']}  "
            f"{row['account_mode']}/{row['market']} {row['currency']}  "
            f"state={row['state']} v{row['version']}"
        )
        lines.append(
            f"  budget={row['budget_cap']} used={row['used']} "
            f"left={row['budget_left']} (held={row['held_cost_basis']} "
            f"open={row['open_order_notional']} "
            f"unbooked={row['unbooked_fill_notional']})"
        )
        lines.append(
            f"  pnl net={row['net_pnl']} realized={row['realized_pnl']} "
            f"unrealized={row['unrealized_pnl']} fees={row['fees']}  "
            f"loss_limit={row['loss_limit']} "
            f"reached={row['loss_limit_reached']}"
        )
        lines.append(
            f"  safety={row['safety_state']} trades={row['trades']} "
            f"unmatched_sell_qty={row['unmatched_sell_quantity']} "
            f"missing_marks={','.join(row['missing_marks']) or '-'}"
        )
        lines.append(
            f"  prereg metric={prereg['metric'] or '-'} "
            f"min_effect={prereg['min_effect_pct_of_budget'] or '-'}% "
            f"H={prereg['horizon_sessions'] or '-'} "
            f"max_days={prereg['max_duration_days'] or '-'}"
        )
        lines.append(
            f"  canary §{canary['design_section']} verdict={canary['verdict_state']} "
            f"final={canary['final_date'] or '-'} signals={canary['signals']} "
            f"episodes={canary['episodes']} "
            f"matured={canary['matured']} (unfilled={canary['matured_unfilled']}) "
            f"open={canary['open']} unmeasured={canary['unmeasured']} "
            f"mean_result={canary['mean_result_pct_of_budget'] or '-'}% "
            f"baseline={canary['baseline']}"
        )
        for position in row["positions"]:
            lines.append(
                f"    {position['symbol']} qty={position['quantity']} "
                f"cost={position['cost_basis']} mark={position['mark']} "
                f"upnl={position['unrealized_pnl']}"
            )
        if row["attribution_skipped"]:
            skipped = ", ".join(
                f"{k}={v}" for k, v in row["attribution_skipped"].items()
            )
            lines.append(f"  attribution skipped: {skipped}")
    for currency, totals in currency_totals(rows).items():
        lines.append(
            f"total {currency}: budget={totals['budget_cap']} used={totals['used']} "
            f"net_pnl={totals['net_pnl']}"
        )
    return "\n".join(lines) + "\n"


async def load_report(
    session: AsyncSession,
    *,
    now: datetime,
    mark_fn: MarkFn = latest_close_marks,
    horizon_fn: HorizonFn = horizon_closes,
    sleeve_key: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sleeve in await list_sleeves(session):
        if sleeve_key is not None and sleeve.sleeve_key != sleeve_key:
            continue
        versions = await history(session, sleeve.id)
        if not versions:
            continue
        inputs = await load_sleeve_inputs(session, sleeve)
        unmarked = compute_sleeve_book(
            orders=inputs.orders, fills=inputs.fills, marks={}
        )
        held = [p.symbol for p in unmarked.positions]
        marks = await mark_fn(session, sleeve.market, held, now) if held else {}
        marks = {sleeve_symbol(k, sleeve.market): v for k, v in marks.items()}
        book = compute_sleeve_book(
            orders=inputs.orders,
            fills=inputs.fills,
            marks=marks,
            unattributed_rungs=inputs.unattributed_rungs,
        )

        episodes = episodes_of(inputs.orders, inputs.fills)
        horizon = prereg_of(versions[-1]).horizon_sessions
        raw_marks = (
            await horizon_fn(
                session,
                sleeve.market,
                [(e.episode_key, e.symbol, e.start_at) for e in episodes],
                horizon,
                now,
            )
            if horizon is not None and episodes
            else {}
        )
        cohort = build_cohort(
            orders=inputs.orders,
            fills=inputs.fills,
            marks={k: HorizonMark(*mark) for k, mark in raw_marks.items()},
            budget_at={
                e.episode_key: budget_at_signal(versions, e.start_at) for e in episodes
            },
        )
        rows.append(
            build_report_row(
                sleeve=sleeve,
                versions=versions,
                book=book,
                cohort=cohort,
                diagnostics=inputs.diagnostics,
                now=now,
            )
        )
    return rows
