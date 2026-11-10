"""Read stored valuations and assemble the manual performance report."""

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from app.models.account_value import AccountExternalFlow, AccountValueSnapshot
from app.models.execution_ledger import ExecutionLedger, execution_ledger_in_effect
from app.models.trading import InstrumentType
from app.services.account_value.market_return import market_row
from app.services.account_value.report import ReportRow, account_row
from app.services.execution_ledger.supersede import AUTHORITATIVE_SOURCES
from app.services.strategy_sleeves.book import compute_sleeve_book
from app.services.strategy_sleeves.store import (
    history,
    list_sleeves,
    load_sleeve_inputs,
)


def _marks(snapshot):
    marks = {}
    for holding in snapshot.holdings:
        try:
            mark = Decimal(holding["mark"])
            if (
                not mark.is_finite()
                or mark <= 0
                or not holding.get("mark_source")
                or not holding.get("mark_asof")
            ):
                continue
            marks[holding["symbol"]] = mark
        except (TypeError, KeyError, InvalidOperation):
            continue
    return marks


def sleeve_row(
    sleeve,
    versions,
    inputs,
    start_snapshot,
    end_snapshot,
    benchmark,
    *,
    complete_days=True,
    resolved_account=None,
):
    account = {"kis_live": "kis", "toss_live": "toss", "upbit": "upbit"}.get(
        sleeve.account_mode, sleeve.account_mode
    )
    account = resolved_account or f"{account}:identity_unknown"
    market = {"equity_kr": "kr", "equity_us": "us", "crypto": "crypto"}[sleeve.market]
    reasons = []
    result = None
    if not complete_days or start_snapshot is None or end_snapshot is None:
        reasons.append("sleeve_snapshot_missing")
    elif inputs.diagnostics:
        reasons.append("sleeve_attribution_diagnostics")
    elif any(
        {
            "holdings_completeness_unknown",
            "duplicate_holding_symbol",
            "invalid_broker_holding",
            "invalid_broker_balance",
        }.intersection(snapshot.reasons)
        for snapshot in (start_snapshot, end_snapshot)
    ):
        reasons.append("sleeve_holdings_completeness_unknown")
    else:
        start_at, end_at = start_snapshot.observed_at, end_snapshot.observed_at
        applicable = [version for version in versions if version.set_at <= start_at]
        changes = [
            version for version in versions if start_at < version.set_at <= end_at
        ]
        if not applicable or changes or applicable[-1].budget_cap <= 0:
            reasons.append("sleeve_period_budget_unavailable")
        elif start_at >= end_at:
            reasons.append("sleeve_snapshot_order_invalid")
        else:
            books = []
            for snapshot in (start_snapshot, end_snapshot):
                book = compute_sleeve_book(
                    orders=inputs.orders,
                    fills=[
                        fill
                        for fill in inputs.fills
                        if fill.filled_at <= snapshot.observed_at
                    ],
                    marks=_marks(snapshot),
                    unattributed_rungs=inputs.unattributed_rungs,
                )
                books.append(book)
            if any(
                book.net_pnl is None
                or book.unmatched_sell_quantity
                or book.unmeasured_orders
                or book.unbooked_fill_notional
                for book in books
            ):
                reasons.append("sleeve_attribution_or_marks_unknown")
            else:
                assert books[0].net_pnl is not None and books[1].net_pnl is not None
                result = (books[1].net_pnl - books[0].net_pnl) / applicable[
                    -1
                ].budget_cap
    if benchmark.value is None:
        reasons.append(benchmark.reason or "benchmark_unknown")
    return ReportRow(
        account,
        market,
        sleeve.sleeve_key,
        None,
        None,
        None,
        result,
        benchmark.value,
        tuple(sorted(reasons)),
        "sleeve_attributed_pnl_over_budget",
    )


async def load_weekly_report(session, start, end, *, flows_complete=False):
    snapshots = (
        (
            await session.execute(
                select(AccountValueSnapshot)
                .where(
                    AccountValueSnapshot.day >= start, AccountValueSnapshot.day <= end
                )
                .order_by(
                    AccountValueSnapshot.account,
                    AccountValueSnapshot.market,
                    AccountValueSnapshot.day,
                )
            )
        )
        .scalars()
        .all()
    )
    flows = (
        (
            await session.execute(
                select(AccountExternalFlow).where(
                    AccountExternalFlow.day >= start, AccountExternalFlow.day <= end
                )
            )
        )
        .scalars()
        .all()
    )
    groups = {}
    for snapshot in snapshots:
        groups.setdefault((snapshot.account, snapshot.market), []).append(snapshot)
    known = (
        await session.execute(
            select(AccountValueSnapshot.account, AccountValueSnapshot.market)
            .where(AccountValueSnapshot.day <= end)
            .distinct()
        )
    ).all()
    for account, market in known:
        groups.setdefault((account, market), [])
    broken = set()
    for account, market in groups:
        prefix = account.rsplit(":", 1)[0]
        identities = {
            candidate
            for candidate, candidate_market in known
            if candidate_market == market and candidate.rsplit(":", 1)[0] == prefix
        }
        if len(identities) > 1 or account.endswith(":identity_unknown"):
            broken.add((account, market))
    benchmarks = {}
    for market in {market for _, market in groups} | {"kr", "us", "crypto"}:
        benchmarks[market] = await _benchmark(session, market, start, end)
    rows = []
    for (account, market), observations in groups.items():
        matching = [
            flow for flow in flows if flow.account == account and flow.market == market
        ]
        if market == "account":
            row = account_row(
                account,
                market,
                start,
                end,
                observations,
                matching,
                benchmarks[market],
                flows_complete=flows_complete,
            )
            if (account, market) in broken:
                row = replace(
                    row, reasons=tuple(sorted(set(row.reasons) | {"series_break"}))
                )
        else:
            ledger_fills = []
            by_day = {item.day: item for item in observations}
            if start in by_day and end in by_day and (account, market) not in broken:
                broker = account.split(":", 1)[0]
                instrument = {
                    "kr": InstrumentType.equity_kr,
                    "us": InstrumentType.equity_us,
                    "crypto": InstrumentType.crypto,
                }[market]
                query = (
                    select(ExecutionLedger)
                    .where(
                        execution_ledger_in_effect(),
                        ExecutionLedger.broker == broker,
                        ExecutionLedger.account_mode == "live",
                        ExecutionLedger.instrument_type == instrument,
                        ExecutionLedger.source.in_(AUTHORITATIVE_SOURCES),
                        ExecutionLedger.filled_at > by_day[start].observed_at,
                        ExecutionLedger.filled_at <= by_day[end].observed_at,
                    )
                    .order_by(ExecutionLedger.filled_at, ExecutionLedger.id)
                )
                ledger_fills = (await session.execute(query)).scalars().all()
            row = market_row(
                account,
                market,
                start,
                end,
                observations,
                ledger_fills,
                benchmarks[market],
                series_break=(account, market) in broken,
            )
        rows.append(row)
    from app.services.account_value.returns import benchmark_return

    for sleeve in await list_sleeves(session):
        account = {"kis_live": "kis", "toss_live": "toss", "upbit": "upbit"}.get(
            sleeve.account_mode, sleeve.account_mode
        )
        market = {"equity_kr": "kr", "equity_us": "us", "crypto": "crypto"}[
            sleeve.market
        ]
        matching_accounts = [
            name
            for name, candidate_market in groups
            if candidate_market == market
            and name.startswith(account + ":")
            and not name.endswith(":identity_unknown")
        ]
        resolved_account = matching_accounts[0] if len(matching_accounts) == 1 else None
        by_day = {
            snapshot.day: snapshot
            for snapshot in groups.get((resolved_account, market), [])
        }
        rows.append(
            sleeve_row(
                sleeve,
                await history(session, sleeve.id),
                await load_sleeve_inputs(session, sleeve),
                by_day.get(start),
                by_day.get(end),
                benchmark_return(market, *benchmarks[market]),
                resolved_account=resolved_account,
                complete_days=all(
                    start + timedelta(days=i) in by_day
                    for i in range((end - start).days + 1)
                ),
            )
        )
    return rows


async def _benchmark(session, market, start, end):
    if market != "kr":
        return None, None
    from app.services.daily_candles.repository import DailyCandlesRepository, MarketKey

    rows = await DailyCandlesRepository(session=session).fetch_range(
        market=MarketKey.KR,
        symbol="069500",
        partition="KRX",
        start=datetime.combine(start, time.min, tzinfo=UTC),
        end=datetime.combine(end, time.max, tzinfo=UTC),
    )
    prices = []
    for day in (start, end):
        matches = [row for row in rows if row.time_utc.date() == day]
        prices.append(Decimal(str(matches[0].close)) if len(matches) == 1 else None)
    return tuple(prices)
