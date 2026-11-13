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
                    "currency": row.get("currency"),
                }
            )
        metrics = {}
        for name, ids in METRICS.items():
            statement_types = (
                ("BS",)
                if name in ("equity", "total_assets", "shares")
                else ("IS", "CIS")
            )
            matches = [
                row
                for row in accounts
                if row.get("account_id") in ids and row.get("sj_div") in statement_types
            ]
            values = [
                (amount(row.get("thstrm_amount")), row.get("currency"))
                for row in matches
            ]
            valid = [v for v in values if v[0] is not None]
            if len(valid) == 1:
                metrics[name] = {
                    "value": valid[0][0],
                    "reason": None,
                    "currency": valid[0][1],
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
                    "reason": reason,
                    "currency": None,
                    "available_at": available,
                    "statement_basis": report["statement_basis"],
                }
        derived.append({**common, "metrics": metrics})
    return facts, derived
