"""Reconcile daily holdings against authoritative fills before measuring returns."""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from app.services.account_value.report import ReportRow, snapshot_value
from app.services.account_value.returns import benchmark_return
from app.services.strategy_sleeves.store import sleeve_symbol


def _quantities(snapshot):
    quantities = {}
    for holding in snapshot.holdings:
        symbol = holding.get("symbol")
        try:
            quantity = Decimal(holding["quantity"])
        except (KeyError, TypeError, InvalidOperation, ValueError):
            return None
        if (
            not symbol
            or symbol in quantities
            or not quantity.is_finite()
            or quantity < 0
        ):
            return None
        quantities[symbol] = quantity
    return quantities


def market_row(
    account, market, start, end, snapshots, fills, prices, *, series_break=False
):
    by_day = {row.day: row for row in snapshots}
    currency = "USD" if market == "us" else "KRW"
    ignored = {"cash_unknown", "cash_allocation_unknown"}
    reasons = {
        reason for item in snapshots for reason in item.reasons if reason not in ignored
    }
    values = {
        day: snapshot_value(
            row.holdings,
            {currency: "0"},
            [r for r in row.reasons if r not in ignored],
            currency,
        )
        for day, row in by_day.items()
    }
    benchmark = benchmark_return(market, *prices)
    if benchmark.reason:
        reasons.add(benchmark.reason)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    if any(day not in by_day for day in days):
        reasons.add("missing_snapshot_day")
    if series_break:
        reasons.add("series_break")
    trade_flows = []
    if not reasons.difference(
        {"benchmark_missing", "benchmark_invalid", "benchmark_total_return_unverified"}
    ):
        for left_day, right_day in zip(days, days[1:], strict=False):
            left, right = by_day[left_day], by_day[right_day]
            before, after = _quantities(left), _quantities(right)
            if before is None or after is None or left.observed_at >= right.observed_at:
                reasons.add("flow_incomplete:holdings")
                break
            changes = defaultdict(Decimal)
            for fill in fills:
                if not left.observed_at < fill.filled_at <= right.observed_at:
                    continue
                symbol = sleeve_symbol(
                    fill.symbol,
                    {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}[market],
                )
                if (
                    fill.currency != currency
                    or fill.fee_amount is None
                    or (fill.fee_amount != 0 and fill.fee_currency != currency)
                ):
                    reasons.add(f"flow_incomplete:{symbol}")
                    continue
                quantity, notional, fee = (
                    fill.filled_qty,
                    fill.filled_notional,
                    fill.fee_amount,
                )
                if (
                    not all(x.is_finite() for x in (quantity, notional, fee))
                    or quantity <= 0
                    or notional <= 0
                    or fee < 0
                    or fill.side not in {"buy", "sell"}
                ):
                    reasons.add(f"flow_incomplete:{symbol}")
                    continue
                changes[symbol] += quantity if fill.side == "buy" else -quantity
                trade_flows.append(
                    (
                        fill.filled_at,
                        notional + fee if fill.side == "buy" else -notional + fee,
                    )
                )
            for symbol in before.keys() | after.keys() | changes.keys():
                if (
                    after.get(symbol, Decimal(0)) - before.get(symbol, Decimal(0))
                    != changes[symbol]
                ):
                    reasons.add(f"flow_incomplete:{symbol}")
    result = None
    blocking = reasons.difference(
        {"benchmark_missing", "benchmark_invalid", "benchmark_total_return_unverified"}
    )
    if not blocking:
        opening, closing = values.get(start), values.get(end)
        if opening is None or closing is None:
            reasons.add("valuation_unknown")
        else:
            start_at, end_at = by_day[start].observed_at, by_day[end].observed_at
            duration = Decimal(str((end_at - start_at).total_seconds()))
            weighted = sum(
                (
                    amount * Decimal(str((end_at - at).total_seconds())) / duration
                    for at, amount in trade_flows
                ),
                Decimal(0),
            )
            capital = opening + weighted
            if capital <= 0:
                reasons.add("nonpositive_capital")
            else:
                result = (
                    closing
                    - opening
                    - sum((amount for _, amount in trade_flows), Decimal(0))
                ) / capital
    return ReportRow(
        account,
        market,
        None,
        values.get(start),
        values.get(end),
        sum((amount for _, amount in trade_flows), Decimal(0))
        if not blocking
        else None,
        result,
        benchmark.value,
        tuple(sorted(reasons)),
        "holdings_only",
    )
