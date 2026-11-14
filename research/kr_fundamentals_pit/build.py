"""Convert matched statements into fact and derived rows."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

METRICS = {
    "revenue": (
        "ifrs-full_Revenue",
        "ifrs_Revenue",
        "ifrs-full_RevenueFromContractsWithCustomers",
    ),
    "operating_income": ("dart_OperatingIncomeLoss",),
    "net_income": ("ifrs-full_ProfitLoss", "ifrs_ProfitLoss"),
    "equity": ("ifrs-full_Equity", "ifrs_Equity"),
    "total_assets": ("ifrs-full_Assets", "ifrs_Assets"),
    "shares": ("ifrs-full_NumberOfSharesOutstanding",),
}


def amount(raw):
    if not isinstance(raw, str) or not raw.strip() or raw.strip() == "-":
        return None
    try:
        value = Decimal(raw.replace(",", "").strip())
        return str(value) if value.is_finite() else None
    except InvalidOperation:
        return None


def build_rows(reports):
    facts, derived = [], []
    for report in reports:
        available = (
            datetime.strptime(report["rcept_date"], "%Y%m%d")
            .replace(
                hour=23,
                minute=59,
                second=59,
                microsecond=999999,
                tzinfo=ZoneInfo("Asia/Seoul"),
            )
            .isoformat()
        )
        common = {
            key: report[key]
            for key in (
                "symbol",
                "corp_code",
                "business_year",
                "report_code",
                "period",
                "history",
                "rcept_no",
                "amendment",
                "statement_basis",
            )
        }
        common.update(
            available_at=available, available_at_basis="latest_amendment_rcept_date_eod"
        )
        accounts = report["accounts"]
        for row in accounts:
            facts.append(
                {
                    **common,
                    "account_id": row.get("account_id"),
                    "account_name": row.get("account_nm"),
                    "statement": row.get("sj_div"),
                    "amount": amount(row.get("thstrm_amount")),
                    "cumulative_amount": amount(row.get("thstrm_add_amount")),
                    "currency": row.get("currency"),
                }
            )
        metrics = {}
        for name, ids in METRICS.items():
            flow = name not in ("equity", "total_assets", "shares")
            statement_types = ("BS",) if not flow else ("IS", "CIS")
            # For interim reports (반기/분기) DART carries the 3-month amount in
            # thstrm_amount and the year-to-date amount in thstrm_add_amount;
            # for annual reports thstrm_amount is the annual amount. period_basis
            # describes the "value" field; "cumulative_value" keeps the YTD figure.
            period_basis = (
                ("annual" if report["report_code"] == "11011" else "quarter")
                if flow
                else "instant"
            )
            matches = [
                row
                for row in accounts
                if row.get("account_id") in ids and row.get("sj_div") in statement_types
            ]
            values = [
                (
                    amount(row.get("thstrm_amount")),
                    amount(row.get("thstrm_add_amount")),
                    row.get("currency"),
                )
                for row in matches
            ]
            # Identical rows (e.g. the same account on IS and CIS) collapse to
            # one; only differing values make the metric ambiguous.
            valid = list(dict.fromkeys(v for v in values if v[0] is not None))
            if len(valid) == 1:
                metrics[name] = {
                    "value": valid[0][0],
                    "cumulative_value": valid[0][1] if flow else None,
                    "period_basis": period_basis,
                    "reason": None,
                    "currency": valid[0][2],
                    "available_at": available,
                    "statement_basis": report["statement_basis"],
                }
            else:
                reason = (
                    "ambiguous_account"
                    if len(valid) > 1
                    else ("missing_amount" if matches else "account_not_found")
                )
                metrics[name] = {
                    "value": None,
                    "cumulative_value": None,
                    "period_basis": period_basis,
                    "reason": reason,
                    "currency": None,
                    "available_at": available,
                    "statement_basis": report["statement_basis"],
                }
        derived.append({**common, "metrics": metrics})
    return facts, derived
