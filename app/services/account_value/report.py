"""Deterministic rendering of measured and unmeasured performance rows."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ReportRow:
    account: str
    market: str
    sleeve: str | None
    start_value: Decimal | None
    end_value: Decimal | None
    flows: Decimal | None
    return_rate: Decimal | None
    benchmark: Decimal | None
    reasons: tuple[str, ...] = ()
    basis: str = "modified_dietz"
    fx_evidence: str = "-"


def _amount(value: Decimal | None) -> str:
    return "unknown" if value is None else str(value)


def _percent(value: Decimal | None) -> str:
    return "unknown" if value is None else f"{value * 100:.2f}%"


def render(rows: list[ReportRow]) -> str:
    lines = [
        "account | market | sleeve | start | end | flows | return | benchmark | excess | basis | fx | unmeasured"
    ]
    for row in sorted(rows, key=lambda r: (r.account, r.market, r.sleeve or "")):
        excess = (
            None
            if row.return_rate is None or row.benchmark is None
            else row.return_rate - row.benchmark
        )
        lines.append(
            " | ".join(
                (
                    row.account,
                    row.market,
                    row.sleeve or "-",
                    _amount(row.start_value),
                    _amount(row.end_value),
                    _amount(row.flows),
                    _percent(row.return_rate),
                    _percent(row.benchmark),
                    _percent(excess),
                    row.basis,
                    row.fx_evidence,
                    ", ".join(row.reasons) if row.reasons else "-",
                )
            )
        )
    return "\n".join(lines)


def snapshot_value(
    holdings: list[dict], cash: dict, reasons: list[str], currency: str
) -> Decimal | None:
    """Only value a complete observation denominated in one currency."""
    from decimal import InvalidOperation

    if reasons or set(cash) != {currency} or cash[currency] is None:
        return None
    try:
        result = Decimal(cash[currency])
        if not result.is_finite():
            return None
        for holding in holdings:
            if holding.get("currency") != currency or holding.get("mark") is None:
                return None
            quantity = Decimal(holding["quantity"])
            mark = Decimal(holding["mark"])
            if (
                not quantity.is_finite()
                or not mark.is_finite()
                or quantity < 0
                or mark <= 0
            ):
                return None
            if not holding.get("mark_source") or not holding.get("mark_asof"):
                return None
            result += quantity * mark
        return result
    except (KeyError, TypeError, InvalidOperation, ValueError):
        return None


def _rate(item, currency):
    if currency == "KRW":
        return Decimal(1)
    if currency != "USD":
        return None
    quote = getattr(item, "fx", {}).get("USD/KRW", {})
    try:
        rate = Decimal(quote["rate"])
        if (
            not quote.get("source")
            or not quote.get("asof")
            or not rate.is_finite()
            or rate <= 0
        ):
            return None
        return rate
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


def account_total(item):
    if item.reasons or not item.cash:
        return None
    result = Decimal(0)
    for currency, amount in item.cash.items():
        rate = _rate(item, currency)
        if amount is None or rate is None:
            return None
        try:
            value = Decimal(amount)
            if not value.is_finite():
                return None
            result += value * rate
        except (ValueError, TypeError, ArithmeticError):
            return None
    for holding in item.holdings:
        currency = holding.get("currency")
        amount = snapshot_value([holding], {currency: "0"}, [], currency)
        rate = _rate(item, currency)
        if amount is None or rate is None:
            return None
        result += amount * rate
    return result


def account_row(
    account,
    market,
    start,
    end,
    snapshots,
    flows,
    benchmark_prices,
    *,
    flows_complete=False,
):
    from datetime import timedelta

    from app.services.account_value.returns import (
        Flow,
        benchmark_return,
        modified_dietz,
    )

    by_day = {item.day: item for item in snapshots}
    reasons = {reason for item in snapshots for reason in item.reasons}
    if market != "account":
        currency = "USD" if market == "us" else "KRW"
        ignored = {"cash_allocation_unknown", "cash_unknown"}
        values = {
            day: snapshot_value(
                item.holdings,
                {currency: "0"},
                [r for r in item.reasons if r not in ignored],
                currency,
            )
            for day, item in by_day.items()
        }
        reasons.difference_update(ignored)
        if any(
            start + timedelta(days=i) not in by_day
            for i in range((end - start).days + 1)
        ):
            reasons.add("missing_snapshot_day")
        reasons.add("trade_flows_unverified")
        benchmark = benchmark_return(market, *benchmark_prices)
        if benchmark.reason:
            reasons.add(benchmark.reason)
        return ReportRow(
            account,
            market,
            None,
            values.get(start),
            values.get(end),
            None,
            None,
            benchmark.value,
            tuple(sorted(reasons)),
            "holdings_only",
        )
    values = {day: account_total(item) for day, item in by_day.items()}
    if any(
        start + timedelta(days=i) not in by_day for i in range((end - start).days + 1)
    ):
        reasons.add("missing_snapshot_day")
    converted = []
    result = None
    if not flows_complete:
        reasons.add("flows_not_confirmed")
    elif any(item.day in {start, end} for item in flows):
        reasons.add("boundary_flow_time_unknown")
    else:
        for flow in flows:
            item = by_day.get(flow.day)
            rate = _rate(item, flow.currency) if item is not None else None
            if rate is None:
                reasons.add("flow_fx_unknown")
                break
            converted.append(Flow(flow.day, flow.amount * rate))
        else:
            result = modified_dietz(start, end, values, converted)
            if result.reason:
                reasons.add(result.reason)
    reasons.add("account_benchmark_unavailable")
    evidence = []
    for label, day in (("start", start), ("end", end)):
        item = by_day.get(day)
        quote = getattr(item, "fx", {}).get("USD/KRW") if item else None
        if quote:
            evidence.append(
                f"{label}:{quote.get('rate')}@{quote.get('source')}:{quote.get('asof')}"
            )
    return ReportRow(
        account,
        market,
        None,
        values.get(start),
        values.get(end),
        sum((flow.amount for flow in converted), Decimal(0)) if result else None,
        result.value if result else None,
        None,
        tuple(sorted(reasons)),
        "modified_dietz_account_krw",
        ";".join(evidence) or "-",
    )
