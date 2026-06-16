from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.services.krx import classify_etf_category

ASSET_CLASS_LABELS = {
    "us_equity": "미국주식",
    "kr_equity": "한국주식",
    "crypto": "코인",
    "cash": "현금",
    "other": "기타",
}

_KR_ETF_OTHER_CATEGORIES = {
    "인도",
    "일본",
    "중국",
    "채권",
    "금",
    "원유",
}


def _to_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _round_money(value: float) -> float:
    return round(value, 2)


def _round_pct(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _build_etf_lookup(etf_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for row in etf_rows:
        for key in ("short_code", "code"):
            symbol = _normalize_symbol(row.get(key))
            if symbol:
                lookup[symbol] = row
    return lookup


def _position_value_krw(position: dict[str, Any], usd_krw: float) -> float | None:
    value = position.get("evaluation_amount")
    if value is None:
        return None
    amount = _to_float(value)
    if amount <= 0:
        return None
    if position.get("instrument_type") == "equity_us":
        return amount * usd_krw
    return amount


def _profit_loss_krw(position: dict[str, Any], usd_krw: float) -> float | None:
    value = position.get("profit_loss")
    if value is None:
        return None
    amount = _to_float(value)
    if position.get("instrument_type") == "equity_us":
        return amount * usd_krw
    return amount


def _surface_asset_class(position: dict[str, Any]) -> str:
    instrument_type = str(position.get("instrument_type") or "")
    if instrument_type == "equity_us":
        return "us_equity"
    if instrument_type == "equity_kr":
        return "kr_equity"
    if instrument_type == "crypto":
        return "crypto"
    return "other"


def _effective_asset_class(
    position: dict[str, Any],
    etf_lookup: dict[str, dict[str, Any]],
) -> tuple[str, str | None, dict[str, Any] | None]:
    surface = _surface_asset_class(position)
    if surface != "kr_equity":
        return surface, None, None

    symbol = _normalize_symbol(position.get("symbol"))
    etf = etf_lookup.get(symbol)
    if etf is None:
        return surface, None, None

    categories = classify_etf_category(
        str(etf.get("name") or position.get("name") or ""),
        str(etf.get("index_name") or ""),
    )
    if "미국주식" in categories:
        return "us_equity", "kr_etf_category:미국주식", etf
    if "코스피200" in categories or "코스닥150" in categories:
        return "kr_equity", "kr_etf_category:한국지수", etf
    if any(category in _KR_ETF_OTHER_CATEGORIES for category in categories):
        return "other", "kr_etf_category:" + ",".join(categories), etf
    return "kr_equity", "kr_etf_category:" + ",".join(categories), etf


def _weight_status(
    *,
    weight_pct: float,
    target_pct: float | None,
    drift_threshold_pct: float,
) -> tuple[float | None, str | None]:
    if target_pct is None:
        return None, None
    drift = round(weight_pct - target_pct, 2)
    if drift >= drift_threshold_pct:
        return drift, "overweight"
    if drift <= -drift_threshold_pct:
        return drift, "underweight"
    return drift, "neutral"


def _normalize_account_id(account_id: str, broker: str | None) -> str:
    """Normalize broker-specific account sub-types to canonical IDs (ROB-589)."""
    if str(broker or "").lower() == "kis":
        return "kis"
    return account_id


def build_portfolio_allocation(
    *,
    positions: list[dict[str, Any]],
    cash_accounts: list[dict[str, Any]],
    usd_krw: float,
    etf_rows: list[dict[str, Any]],
    include_cash: bool,
    include_positions: bool,
    target_weights: dict[str, float] | None = None,
    drift_threshold_pct: float = 5.0,
) -> dict[str, Any]:
    target_weights = target_weights or {}
    etf_lookup = _build_etf_lookup(etf_rows)
    warnings: list[dict[str, Any]] = []
    lookthrough: list[dict[str, Any]] = []
    class_totals: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "value_krw": 0.0,
            "direct_value_krw": 0.0,
            "lookthrough_value_krw": 0.0,
            "cash_value_krw": 0.0,
            "profit_loss_krw": 0.0,
        }
    )
    currency_totals: dict[str, dict[str, float]] = defaultdict(
        lambda: {"value_krw": 0.0}
    )
    account_totals: dict[str, dict[str, Any]] = {}
    valued_position_count = 0
    unvalued_position_count = 0
    output_positions: list[dict[str, Any]] = []

    for position in positions:
        value_krw = _position_value_krw(position, usd_krw)
        if value_krw is None:
            unvalued_position_count += 1
            warnings.append(
                {
                    "source": "allocation",
                    "symbol": position.get("symbol"),
                    "reason": "position_value_unavailable",
                }
            )
            continue

        valued_position_count += 1
        surface_class = _surface_asset_class(position)
        effective_class, rule, etf = _effective_asset_class(position, etf_lookup)
        profit_loss = _profit_loss_krw(position, usd_krw)
        totals = class_totals[effective_class]
        totals["value_krw"] += value_krw
        if surface_class == effective_class:
            totals["direct_value_krw"] += value_krw
        else:
            totals["lookthrough_value_krw"] += value_krw
        if profit_loss is not None:
            totals["profit_loss_krw"] += profit_loss

        # Currency rollup: equity_us is USD-based, others (KR/Crypto) are KRW-based.
        currency = "USD" if position.get("instrument_type") == "equity_us" else "KRW"
        currency_totals[currency]["value_krw"] += value_krw

        # ROB-589: Normalize kis_domestic/overseas to 'kis' so they merge with positions.
        raw_account_id = str(position.get("account") or "unknown")
        account_id = _normalize_account_id(raw_account_id, position.get("broker"))

        account = account_totals.setdefault(
            account_id,
            {
                "account": account_id,
                "broker": position.get("broker"),
                "account_name": position.get("account_name"),
                "value_krw": 0.0,
                "profit_loss_krw": 0.0,
                "asset_classes": defaultdict(float),
            },
        )
        account["value_krw"] += value_krw
        account["asset_classes"][effective_class] += value_krw
        if profit_loss is not None:
            account["profit_loss_krw"] += profit_loss

        if surface_class != effective_class:
            lookthrough.append(
                {
                    "symbol": position.get("symbol"),
                    "name": position.get("name") or (etf or {}).get("name"),
                    "account": account_id,
                    "surface_asset_class": surface_class,
                    "effective_asset_class": effective_class,
                    "value_krw": _round_money(value_krw),
                    "rule": rule,
                }
            )

        if include_positions:
            row = dict(position)
            row["surface_asset_class"] = surface_class
            row["effective_asset_class"] = effective_class
            row["value_krw"] = _round_money(value_krw)
            row["account"] = account_id
            output_positions.append(row)

    cash_rows: list[dict[str, Any]] = []
    if include_cash:
        for cash in cash_accounts:
            currency = str(cash.get("currency") or "KRW").upper()
            balance = _to_float(cash.get("balance"))
            value_krw = balance * usd_krw if currency == "USD" else balance
            if value_krw <= 0:
                continue
            totals = class_totals["cash"]
            totals["value_krw"] += value_krw
            totals["cash_value_krw"] += value_krw

            currency_totals[currency]["value_krw"] += value_krw

            # ROB-589: Merge KIS cash wallets into canonical 'kis' account.
            raw_account_id = str(cash.get("account") or "cash")
            account_id = _normalize_account_id(raw_account_id, cash.get("broker"))

            account = account_totals.setdefault(
                account_id,
                {
                    "account": account_id,
                    "broker": cash.get("broker"),
                    "account_name": cash.get("account_name"),
                    "value_krw": 0.0,
                    "profit_loss_krw": 0.0,
                    "asset_classes": defaultdict(float),
                },
            )
            account["value_krw"] += value_krw
            account["asset_classes"]["cash"] += value_krw
            cash_rows.append(
                {**cash, "account": account_id, "value_krw": _round_money(value_krw)}
            )

    total_value = sum(row["value_krw"] for row in class_totals.values())
    invested_value = total_value - class_totals["cash"]["value_krw"]

    asset_classes = []
    for asset_class, totals in sorted(class_totals.items()):
        value = totals["value_krw"]
        if value <= 0:
            continue
        weight = (value / total_value) * 100 if total_value else 0.0
        target = target_weights.get(asset_class)
        drift, status = _weight_status(
            weight_pct=weight,
            target_pct=target,
            drift_threshold_pct=drift_threshold_pct,
        )
        asset_classes.append(
            {
                "asset_class": asset_class,
                "label": ASSET_CLASS_LABELS[asset_class],
                "value_krw": _round_money(value),
                "weight_pct": _round_pct(weight),
                "direct_value_krw": _round_money(totals["direct_value_krw"]),
                "lookthrough_value_krw": _round_money(totals["lookthrough_value_krw"]),
                "cash_value_krw": _round_money(totals["cash_value_krw"]),
                "profit_loss_krw": _round_money(totals["profit_loss_krw"]),
                "target_weight_pct": target,
                "drift_pct": drift,
                "weight_status": status,
            }
        )
    asset_classes.sort(key=lambda row: row["value_krw"], reverse=True)

    by_currency = []
    for currency, totals in sorted(currency_totals.items()):
        value = totals["value_krw"]
        if value <= 0:
            continue
        weight = (value / total_value) * 100 if total_value else 0.0
        by_currency.append(
            {
                "currency": currency,
                "value_krw": _round_money(value),
                "weight_pct": _round_pct(weight),
                "fx_conversion_needed": currency == "USD",
            }
        )
    by_currency.sort(key=lambda row: row["value_krw"], reverse=True)

    accounts = []
    for account in account_totals.values():
        account_value = account["value_krw"]
        accounts.append(
            {
                "account": account["account"],
                "broker": account["broker"],
                "account_name": account["account_name"],
                "value_krw": _round_money(account_value),
                "weight_pct": _round_pct(
                    (account_value / total_value) * 100 if total_value else 0.0
                ),
                "profit_loss_krw": _round_money(account.get("profit_loss_krw", 0.0)),
                "asset_classes": [
                    {
                        "asset_class": key,
                        "value_krw": _round_money(value),
                        "weight_pct": _round_pct(
                            (value / total_value) * 100 if total_value else 0.0
                        ),
                    }
                    for key, value in sorted(account["asset_classes"].items())
                    if value > 0
                ],
            }
        )
    accounts.sort(key=lambda row: row["value_krw"], reverse=True)

    return {
        "currency": {"base": "KRW", "usd_krw": usd_krw},
        "summary": {
            "total_value_krw": _round_money(total_value),
            "invested_value_krw": _round_money(invested_value),
            "cash_value_krw": _round_money(class_totals["cash"]["value_krw"]),
            "valued_position_count": valued_position_count,
            "unvalued_position_count": unvalued_position_count,
        },
        "asset_classes": asset_classes,
        "by_currency": by_currency,
        "accounts": accounts,
        "lookthrough": lookthrough,
        "positions": output_positions,
        "cash": cash_rows if include_cash else [],
        "warnings": warnings,
    }

