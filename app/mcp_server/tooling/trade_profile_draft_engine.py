from __future__ import annotations

import inspect
import math
from collections.abc import Iterable
from typing import Any, Literal, TypedDict, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.analysis_tool_handlers import (
    get_fear_greed_index_impl,
    screen_stocks_impl,
)
from app.mcp_server.tooling.fundamentals_sources_binance import (
    _fetch_funding_rate_batch,
)
from app.mcp_server.tooling.market_data_indicators import (
    _compute_indicators,
    _fetch_ohlcv_for_indicators,
)
from app.mcp_server.tooling.portfolio_cash import get_cash_balance_impl
from app.mcp_server.tooling.portfolio_holdings import _collect_portfolio_positions
from app.mcp_server.tooling.shared import (
    MCP_USER_ID,
    normalize_market,
    to_optional_float,
)
from app.models.trade_profile import AssetProfile, MarketFilter, TierRuleParam
from app.models.trading import InstrumentType

_SUPPORTED_MARKETS = [
    InstrumentType.crypto.value,
    InstrumentType.equity_kr.value,
    InstrumentType.equity_us.value,
]
_FILTER_ORDER = {
    "kill_switch": 0,
    "regime": 1,
    "regime_ema200": 1,
    "fear_greed": 2,
    "funding_rate": 3,
}
_BUY_ENTRY_PREDICATE_KEYS = frozenset(
    {
        "rsi14_max",
        "stoch_rsi_k_max",
        "adx_max",
        "macd_cross_required",
        "obv_rising_required",
    }
)
_BUY_NUMERIC_PREDICATE_KEYS = (
    "rsi14_max",
    "stoch_rsi_k_max",
    "adx_max",
)
_BUY_BOOLEAN_PREDICATE_KEYS = (
    "macd_cross_required",
    "obv_rising_required",
)


class BuyPredicateEvaluation(TypedDict):
    status: Literal["passed", "conditions_not_met", "missing_indicator"]
    triggers: list[str]
    reason: str | None


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


def _resolve_markets(instrument_type: str | None) -> list[str]:
    if instrument_type is None:
        return list(_SUPPORTED_MARKETS)
    normalized = normalize_market(instrument_type)
    if normalized is None:
        raise ValueError(
            f"instrument_type must be one of: kr, us, crypto (got {instrument_type!r})"
        )
    return [normalized]


async def _load_profiles_and_rules(
    market: str,
) -> tuple[list[dict[str, Any]], dict[tuple[int, str], dict[str, dict[str, Any]]]]:
    instrument = InstrumentType(market)
    async with _session_factory()() as db:
        profile_stmt = (
            select(AssetProfile)
            .where(
                AssetProfile.user_id == MCP_USER_ID,
                AssetProfile.instrument_type == instrument,
            )
            .order_by(AssetProfile.symbol.asc(), AssetProfile.tier.asc())
        )
        profile_result = await db.execute(profile_stmt)
        profile_rows = list(profile_result.scalars().all())

        rule_stmt = select(TierRuleParam).where(
            TierRuleParam.user_id == MCP_USER_ID,
            TierRuleParam.instrument_type == instrument,
        )
        rule_result = await db.execute(rule_stmt)
        rule_rows = list(rule_result.scalars().all())

    profiles = [
        {
            "symbol": row.symbol,
            "instrument_type": row.instrument_type.value,
            "tier": row.tier,
            "profile": row.profile,
            "buy_allowed": row.buy_allowed,
            "sell_mode": row.sell_mode,
            "sector": row.sector,
            "tags": row.tags,
            "max_position_pct": (
                float(row.max_position_pct)
                if row.max_position_pct is not None
                else None
            ),
            "note": row.note,
        }
        for row in profile_rows
    ]
    tier_rules: dict[tuple[int, str], dict[str, dict[str, Any]]] = {}
    for row in rule_rows:
        tier_rules.setdefault((row.tier, row.profile), {})[row.param_type] = row.params
    return profiles, tier_rules


async def _load_market_filters_db(market: str) -> list[dict[str, Any]]:
    instrument = InstrumentType(market)
    async with _session_factory()() as db:
        stmt = (
            select(MarketFilter)
            .where(
                MarketFilter.user_id == MCP_USER_ID,
                MarketFilter.instrument_type == instrument,
            )
            .order_by(MarketFilter.filter_name.asc())
        )
        result = await db.execute(stmt)
        rows = list(result.scalars().all())
    return [
        {
            "filter_name": row.filter_name,
            "params": row.params,
            "enabled": row.enabled,
        }
        for row in rows
    ]


async def _load_holdings(market: str) -> list[dict[str, Any]]:
    market_arg = {"crypto": "crypto", "equity_kr": "kr", "equity_us": "us"}[market]
    positions, _, _, _ = await _collect_portfolio_positions(
        account=None,
        market=market_arg,
        include_current_price=True,
    )
    return positions


async def _load_cash_balance(market: str) -> dict[str, Any]:
    response = await get_cash_balance_impl()
    target_account = {
        "crypto": "upbit",
        "equity_kr": "kis_domestic",
        "equity_us": "kis_overseas",
    }[market]
    for account in response.get("accounts", []):
        if account.get("account") == target_account:
            return cast(dict[str, Any], account)
    currency = "USD" if market == "equity_us" else "KRW"
    return {
        "account": target_account,
        "balance": 0.0,
        "orderable": 0.0,
        "currency": currency,
    }


async def _load_buy_universe(market: str) -> list[dict[str, Any]]:
    market_arg = {"crypto": "crypto", "equity_kr": "kr", "equity_us": "us"}[market]
    typed_market_arg: Literal["kr", "us", "crypto"] = cast(
        Literal["kr", "us", "crypto"], market_arg
    )
    sort_by = "trade_amount" if market == "crypto" else "volume"
    response = await screen_stocks_impl(
        market=typed_market_arg,
        sort_by=sort_by,
        limit=30,
    )
    return cast(list[dict[str, Any]], response.get("results", []))


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _normalize_symbol_for_market(symbol: Any, market: str) -> str:
    candidate = str(symbol or "").strip().upper()
    if not candidate:
        return ""
    if market == InstrumentType.equity_kr.value:
        digits = "".join(ch for ch in candidate if ch.isdigit())
        if digits and len(digits) <= 6:
            return digits.zfill(6)
    return candidate


def _candidate_symbol_from_row(row: dict[str, Any], market: str) -> str:
    for key in ("symbol", "code", "market"):
        value = row.get(key)
        if value is None:
            continue
        normalized = _normalize_symbol_for_market(value, market)
        if normalized:
            return normalized
    return ""


def _normalize_profile_rows(
    market: str, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        symbol = _normalize_symbol_for_market(row.get("symbol"), market)
        if not symbol:
            continue
        normalized = dict(row)
        normalized["symbol"] = symbol
        normalized_rows.append(normalized)
    return normalized_rows


def _normalize_holding_rows(
    market: str, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        symbol = _normalize_symbol_for_market(row.get("symbol"), market)
        if not symbol:
            continue
        normalized = dict(row)
        normalized["symbol"] = symbol
        normalized_rows.append(normalized)
    return normalized_rows


def _normalize_buy_universe_rows(
    market: str, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        symbol = _candidate_symbol_from_row(row, market)
        if not symbol:
            continue
        normalized = dict(row)
        normalized["symbol"] = symbol
        normalized_rows.append(normalized)
    return normalized_rows


def _normalize_indicator_map(
    market: str, indicator_map: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    normalized_map: dict[str, dict[str, Any]] = {}
    for symbol, payload in indicator_map.items():
        normalized_symbol = _normalize_symbol_for_market(symbol, market)
        if not normalized_symbol:
            continue
        normalized_map[normalized_symbol] = payload
    return normalized_map


async def _load_indicator_map(
    market: str, symbols: Iterable[str]
) -> dict[str, dict[str, Any]]:
    indicator_map: dict[str, dict[str, Any]] = {}
    market_arg = {
        "crypto": "crypto",
        "equity_kr": "equity_kr",
        "equity_us": "equity_us",
    }[market]
    normalized_symbols = [
        _normalize_symbol_for_market(symbol, market)
        for symbol in symbols
        if str(symbol).strip()
    ]
    for symbol in normalized_symbols:
        try:
            df = await _fetch_ohlcv_for_indicators(symbol, market_arg, count=250)
        except Exception:
            indicator_map[symbol] = {}
            continue
        if df.empty:
            indicator_map[symbol] = {}
            continue
        try:
            computed = _compute_indicators(
                df,
                ["rsi", "stoch_rsi", "macd", "adx", "atr", "obv", "ema"],
            )
        except Exception:
            indicator_map[symbol] = {}
            continue

        rsi_block = computed.get("rsi", {}) if isinstance(computed, dict) else {}
        ema_block = computed.get("ema", {}) if isinstance(computed, dict) else {}
        ema200 = (
            to_optional_float(ema_block.get("200"))
            if isinstance(ema_block, dict)
            else None
        )
        last_close = (
            to_optional_float(df["close"].iloc[-1]) if "close" in df.columns else None
        )
        indicator_map[symbol] = {
            "rsi": to_optional_float(rsi_block.get("14"))
            if isinstance(rsi_block, dict)
            else None,
            "stoch_rsi": computed.get("stoch_rsi", {}),
            "macd": computed.get("macd", {}),
            "adx": computed.get("adx", {}),
            "atr": computed.get("atr", {}),
            "obv": computed.get("obv", {}),
            "ema": computed.get("ema", {}),
            "ema200": ema200,
            "price_above_ema200": (
                last_close >= ema200
                if last_close is not None and ema200 is not None
                else None
            ),
        }
    return indicator_map


async def _load_fear_greed() -> dict[str, Any] | None:
    try:
        response = await get_fear_greed_index_impl(days=1)
    except Exception:
        return None
    if "error" in response:
        return None
    return cast(dict[str, Any], response)


def _funding_rate_key_for_symbol(symbol: str) -> str:
    return str(symbol).split("-", 1)[-1].upper()


async def _load_funding_rates(symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
    base_symbols = sorted(
        {
            _funding_rate_key_for_symbol(str(symbol))
            for symbol in symbols
            if str(symbol).strip()
        }
    )
    if not base_symbols:
        return {}
    try:
        rows = await _fetch_funding_rate_batch(base_symbols)
    except Exception:
        return {}
    return {str(row.get("symbol", "")).upper(): row for row in rows}


async def _load_market_inputs(market: str) -> dict[str, Any]:
    profiles, tier_rules = await _load_profiles_and_rules(market)
    market_filters = await _load_market_filters_db(market)
    holdings = _normalize_holding_rows(market, await _load_holdings(market))
    cash = await _load_cash_balance(market)
    buy_universe = _normalize_buy_universe_rows(
        market, await _load_buy_universe(market)
    )
    profiles = _normalize_profile_rows(market, profiles)
    indicator_symbols = (
        {str(profile.get("symbol", "")) for profile in profiles}
        | {str(item.get("symbol", "")) for item in buy_universe}
        | {str(item.get("symbol", "")) for item in holdings}
    )
    indicator_map = await _load_indicator_map(
        market, [symbol for symbol in indicator_symbols if symbol]
    )
    fear_greed = (
        await _load_fear_greed() if market != InstrumentType.equity_us.value else None
    )
    funding_rates = (
        await _load_funding_rates(indicator_symbols)
        if market == InstrumentType.crypto.value
        else {}
    )
    return {
        "profiles": profiles,
        "tier_rules": tier_rules,
        "market_filters": market_filters,
        "holdings": holdings,
        "cash": cash,
        "buy_universe": buy_universe,
        "indicator_map": indicator_map,
        "fear_greed": fear_greed,
        "funding_rates": funding_rates,
    }


def _lookup_rule_bundle(
    tier_rules: dict[Any, Any],
    *,
    symbol: str,
    tier: int,
    profile: str,
) -> dict[str, dict[str, Any]]:
    direct_key = (symbol, tier, profile)
    if direct_key in tier_rules:
        return cast(dict[str, dict[str, Any]], tier_rules[direct_key])
    fallback_key = (tier, profile)
    if fallback_key in tier_rules:
        return cast(dict[str, dict[str, Any]], tier_rules[fallback_key])
    return {}


def _current_fear_greed_value(payload: dict[str, Any] | None) -> float | None:
    if not payload:
        return None
    current = payload.get("current")
    if isinstance(current, dict):
        return to_optional_float(current.get("value"))
    return to_optional_float(payload.get("value"))


def _canonical_filter_name(filter_name: Any) -> str:
    normalized = str(filter_name or "").strip().lower()
    if normalized == "regime":
        return "regime_ema200"
    return normalized


def _normalize_filter_entries(filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        filters,
        key=lambda item: _FILTER_ORDER.get(
            _canonical_filter_name(item.get("filter_name")),
            99,
        ),
    )


def _market_filter_result() -> dict[str, Any]:
    return {
        "kill_switch": False,
        "filters_applied": [],
        "warnings": [],
        "buy_filters": [],
    }


def _extract_btc_change_rate(buy_universe: list[dict[str, Any]]) -> float | None:
    for row in buy_universe:
        if str(row.get("symbol", "")).upper() != "KRW-BTC":
            continue
        return to_optional_float(row.get("change_rate"))
    return None


def _evaluate_kill_switch(
    market: str,
    params: dict[str, Any],
    buy_universe: list[dict[str, Any]],
) -> tuple[bool, list[str]]:
    warnings: list[str] = []
    threshold = to_optional_float(params.get("btc_drop_24h_pct"))
    if threshold is None:
        return False, warnings
    if market != InstrumentType.crypto.value:
        return False, warnings
    btc_change_rate = _extract_btc_change_rate(buy_universe)
    if btc_change_rate is None:
        warnings.append("kill_switch btc_drop_24h_pct metric unavailable")
        return False, warnings
    return btc_change_rate <= -abs(threshold), warnings


def _evaluate_market_filters(
    market: str,
    filters: list[dict[str, Any]],
    *,
    buy_universe: list[dict[str, Any]],
    fear_greed: dict[str, Any] | None,
    funding_rates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    result = _market_filter_result()
    for filter_row in _normalize_filter_entries(filters):
        if not filter_row.get("enabled", True):
            continue

        raw_filter_name = str(filter_row.get("filter_name", "")).strip().lower()
        filter_name = _canonical_filter_name(raw_filter_name)
        params = cast(dict[str, Any], filter_row.get("params") or {})
        result["filters_applied"].append(filter_name or raw_filter_name)

        if filter_name == "kill_switch":
            kill_switch, warnings = _evaluate_kill_switch(market, params, buy_universe)
            result["warnings"].extend(warnings)
            if kill_switch:
                result["kill_switch"] = True
                break
            continue

        if filter_name == "fear_greed":
            if fear_greed is None:
                result["warnings"].append("fear_greed data unavailable")
            result["buy_filters"].append({"filter_name": filter_name, "params": params})
            continue

        if filter_name == "funding_rate":
            if market == InstrumentType.crypto.value and not funding_rates:
                result["warnings"].append("funding_rate data unavailable")
            result["buy_filters"].append({"filter_name": filter_name, "params": params})
            continue

        if filter_name == "regime_ema200":
            result["buy_filters"].append({"filter_name": filter_name, "params": params})
            continue

        result["warnings"].append(
            f"Unknown filter_name '{filter_row.get('filter_name')}' ignored"
        )
    return result


def _holding_by_symbol(holdings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("symbol")): item
        for item in holdings
        if str(item.get("symbol", "")).strip()
    }


def _buy_universe_by_symbol(
    candidates: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("symbol")): item
        for item in candidates
        if str(item.get("symbol", "")).strip()
    }


def _safe_float(value: Any) -> float | None:
    return to_optional_float(value)


def _safe_rsi(indicators: dict[str, Any]) -> float | None:
    return _safe_float(indicators.get("rsi"))


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    numeric = to_optional_float(value)
    if numeric is not None:
        return numeric != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "allow", "on", "above"}


def _regime_gate_allows(params: Any, price_above_ema200: bool) -> bool:
    if isinstance(params, dict) and ("above" in params or "below" in params):
        branch = params.get("above") if price_above_ema200 else params.get("below")
        if branch is None:
            return True
        return _is_truthy(branch)

    if isinstance(params, bool):
        return price_above_ema200 is params

    numeric = to_optional_float(params)
    if numeric is not None:
        return price_above_ema200 if numeric > 0 else not price_above_ema200

    text = str(params or "").strip().lower()
    if text in {"above", "bull", "bullish"}:
        return price_above_ema200
    if text in {"below", "bear", "bearish"}:
        return not price_above_ema200
    return True


def _regime_skip_reason(indicators: dict[str, Any]) -> str | None:
    if "price_above_ema200" not in indicators:
        return "regime_ema200 requires price_above_ema200 data"
    price_above_ema200 = indicators.get("price_above_ema200")
    if price_above_ema200 is None:
        return "regime_ema200 requires price_above_ema200 data"
    return None


def _evaluate_buy_filters_for_symbol(
    market: str,
    filters: list[dict[str, Any]],
    *,
    symbol: str,
    indicators: dict[str, Any],
    fear_greed: dict[str, Any] | None,
    funding_rates: dict[str, dict[str, Any]],
) -> str | None:
    for filter_row in filters:
        filter_name = _canonical_filter_name(filter_row.get("filter_name"))
        params = cast(dict[str, Any], filter_row.get("params") or {})

        if filter_name == "regime_ema200":
            reason = _regime_skip_reason(indicators)
            if reason is not None:
                return reason
            price_above_ema200 = bool(indicators.get("price_above_ema200"))
            if not _regime_gate_allows(params, price_above_ema200):
                side = "above" if price_above_ema200 else "below"
                return f"regime_ema200 blocked buy while price is {side} EMA200"

        elif filter_name == "fear_greed":
            if fear_greed is None:
                continue
            threshold = to_optional_float(params.get("extreme_greed"))
            current_value = _current_fear_greed_value(fear_greed)
            if threshold is None or current_value is None:
                continue
            if current_value >= threshold:
                return f"fear_greed blocked buy: {current_value} >= {threshold}"

        elif filter_name == "funding_rate":
            if market != InstrumentType.crypto.value or not funding_rates:
                continue
            threshold = to_optional_float(params.get("hot_threshold"))
            if threshold is None:
                continue
            row = funding_rates.get(_funding_rate_key_for_symbol(symbol)) or {}
            current_rate = to_optional_float(row.get("current_funding_rate_pct"))
            if current_rate is None:
                continue
            if current_rate >= threshold:
                return f"funding_rate blocked buy: {current_rate} >= {threshold}"

    return None


def _indicator_missing_reason(label: str) -> str:
    return f"missing indicator data for {label}"


def get_active_buy_signal_predicates(params: Any) -> tuple[str, ...]:
    if not isinstance(params, dict) or not params:
        return ()

    active: list[str] = []
    for key in _BUY_NUMERIC_PREDICATE_KEYS:
        if to_optional_float(params.get(key)) is not None:
            active.append(key)
    for key in _BUY_BOOLEAN_PREDICATE_KEYS:
        if _is_truthy(params.get(key)):
            active.append(key)
    return tuple(active)


def _classify_buy_rule_params(
    params: Any,
) -> Literal["missing", "active", "no_active"]:
    if not isinstance(params, dict) or not params:
        return "missing"
    if get_active_buy_signal_predicates(params):
        return "active"
    return "no_active"


def _evaluate_buy_rule_predicates(
    buy_params: dict[str, Any],
    indicators: dict[str, Any],
    *,
    active_predicates: tuple[str, ...],
) -> BuyPredicateEvaluation:
    triggers: list[str] = []

    for predicate in active_predicates:
        if predicate == "rsi14_max":
            rsi_limit = to_optional_float(buy_params.get("rsi14_max"))
            rsi = _safe_rsi(indicators)
            if rsi_limit is None:
                continue
            if rsi is None:
                return {
                    "status": "missing_indicator",
                    "triggers": [],
                    "reason": _indicator_missing_reason("rsi14_max"),
                }
            if rsi > rsi_limit:
                return {
                    "status": "conditions_not_met",
                    "triggers": [],
                    "reason": "buy conditions not met",
                }
            triggers.append("rsi14_max")
            continue

        if predicate == "stoch_rsi_k_max":
            stoch_limit = to_optional_float(buy_params.get("stoch_rsi_k_max"))
            stoch_rsi = cast(dict[str, Any], indicators.get("stoch_rsi") or {})
            stoch_k = to_optional_float(stoch_rsi.get("k"))
            if stoch_limit is None:
                continue
            if stoch_k is None:
                return {
                    "status": "missing_indicator",
                    "triggers": [],
                    "reason": _indicator_missing_reason("stoch_rsi_k_max"),
                }
            if stoch_k > stoch_limit:
                return {
                    "status": "conditions_not_met",
                    "triggers": [],
                    "reason": "buy conditions not met",
                }
            triggers.append("stoch_rsi_k_max")
            continue

        if predicate == "adx_max":
            adx_limit = to_optional_float(buy_params.get("adx_max"))
            adx_block = cast(dict[str, Any], indicators.get("adx") or {})
            adx_value = to_optional_float(adx_block.get("adx"))
            if adx_limit is None:
                continue
            if adx_value is None:
                return {
                    "status": "missing_indicator",
                    "triggers": [],
                    "reason": _indicator_missing_reason("adx_max"),
                }
            if adx_value > adx_limit:
                return {
                    "status": "conditions_not_met",
                    "triggers": [],
                    "reason": "buy conditions not met",
                }
            triggers.append("adx_max")
            continue

        if predicate == "macd_cross_required":
            macd_block = cast(dict[str, Any], indicators.get("macd") or {})
            histogram = to_optional_float(macd_block.get("histogram"))
            if histogram is None:
                return {
                    "status": "missing_indicator",
                    "triggers": [],
                    "reason": _indicator_missing_reason("macd_cross_required"),
                }
            if histogram <= 0:
                return {
                    "status": "conditions_not_met",
                    "triggers": [],
                    "reason": "buy conditions not met",
                }
            triggers.append("macd_cross_required")
            continue

        if predicate == "obv_rising_required":
            obv_block = cast(dict[str, Any], indicators.get("obv") or {})
            obv_value = to_optional_float(obv_block.get("obv"))
            signal_value = to_optional_float(obv_block.get("signal"))
            if obv_value is None or signal_value is None:
                return {
                    "status": "missing_indicator",
                    "triggers": [],
                    "reason": _indicator_missing_reason("obv_rising_required"),
                }
            if obv_value <= signal_value:
                return {
                    "status": "conditions_not_met",
                    "triggers": [],
                    "reason": "buy conditions not met",
                }
            triggers.append("obv_rising_required")

    return {"status": "passed", "triggers": triggers, "reason": None}


def _ema200_budget_factor(
    buy_params: dict[str, Any], indicators: dict[str, Any]
) -> float:
    factor = buy_params.get("ema200_budget_factor")
    if isinstance(factor, dict):
        branch = "above" if indicators.get("price_above_ema200") else "below"
        branch_value = to_optional_float(factor.get(branch))
        return branch_value if branch_value is not None else 1.0
    scalar = to_optional_float(factor)
    return scalar if scalar is not None else 1.0


def _market_regime_budget_factor(
    filters: list[dict[str, Any]], indicators: dict[str, Any]
) -> tuple[float, str | None]:
    factor = 1.0
    for filter_row in filters:
        if _canonical_filter_name(filter_row.get("filter_name")) != "regime_ema200":
            continue

        params = filter_row.get("params")
        if not isinstance(params, dict) or "budget_factor" not in params:
            continue

        reason = _regime_skip_reason(indicators)
        if reason is not None:
            return 1.0, reason

        if bool(indicators.get("price_above_ema200")):
            continue

        budget_factor = to_optional_float(params.get("budget_factor"))
        if budget_factor is None:
            continue
        factor *= budget_factor

    return factor, None


def _buy_amount(
    common_params: dict[str, Any],
    buy_params: dict[str, Any],
    cash: dict[str, Any],
    indicators: dict[str, Any],
    *,
    market_regime_budget_factor: float = 1.0,
) -> float:
    orderable = to_optional_float(cash.get("orderable"))
    balance = to_optional_float(cash.get("balance"))
    available_cash = orderable if orderable is not None else balance or 0.0
    cash_reserve_pct = to_optional_float(common_params.get("cash_reserve_pct")) or 0.0
    allocatable_cash = max(available_cash * (1 - cash_reserve_pct / 100), 0.0)

    max_positions = int(to_optional_float(common_params.get("max_positions")) or 1)
    if max_positions <= 0:
        max_positions = 1
    per_position_budget = allocatable_cash / max_positions

    position_size_pct = to_optional_float(buy_params.get("position_size_pct")) or 100.0
    amount = per_position_budget * (position_size_pct / 100)
    amount *= market_regime_budget_factor
    amount *= _ema200_budget_factor(buy_params, indicators)
    return round(amount, 2)


def _skip(symbol: str, reason: str) -> dict[str, str]:
    return {"symbol": symbol, "reason": reason}


def _build_buy_drafts(
    market: str,
    profiles: list[dict[str, Any]],
    *,
    tier_rules: dict[Any, Any],
    holdings: list[dict[str, Any]],
    cash: dict[str, Any],
    buy_universe: list[dict[str, Any]],
    indicator_map: dict[str, dict[str, Any]],
    market_filters: list[dict[str, Any]],
    fear_greed: dict[str, Any] | None,
    funding_rates: dict[str, dict[str, Any]],
    action_type: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if action_type == "sell":
        return [], []

    drafts: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    holdings_map = _holding_by_symbol(holdings)
    universe_map = _buy_universe_by_symbol(buy_universe)

    for profile in profiles:
        if not profile.get("buy_allowed"):
            continue
        symbol = str(profile.get("symbol", ""))
        if not symbol:
            continue

        if universe_map.get(symbol) is None:
            skipped.append(_skip(symbol, "not in buy universe"))
            continue

        indicators = indicator_map.get(symbol) or {}
        filter_reason = _evaluate_buy_filters_for_symbol(
            market,
            market_filters,
            symbol=symbol,
            indicators=indicators,
            fear_greed=fear_greed,
            funding_rates=funding_rates,
        )
        if filter_reason is not None:
            skipped.append(_skip(symbol, filter_reason))
            continue

        market_budget_factor, budget_skip_reason = _market_regime_budget_factor(
            market_filters, indicators
        )
        if budget_skip_reason is not None:
            skipped.append(_skip(symbol, budget_skip_reason))
            continue

        rule_bundle = _lookup_rule_bundle(
            tier_rules,
            symbol=symbol,
            tier=int(profile.get("tier") or 0),
            profile=str(profile.get("profile", "")),
        )
        common_params = cast(dict[str, Any], rule_bundle.get("common") or {})
        buy_params_raw = rule_bundle.get("buy")
        buy_params_state = _classify_buy_rule_params(buy_params_raw)
        if buy_params_state == "missing":
            skipped.append(_skip(symbol, "buy rule params missing"))
            continue
        if buy_params_state == "no_active":
            skipped.append(_skip(symbol, "no active buy predicates"))
            continue
        buy_params = cast(dict[str, Any], buy_params_raw)
        active_predicates = get_active_buy_signal_predicates(buy_params)
        if not active_predicates:
            skipped.append(_skip(symbol, "no active buy predicates"))
            continue
        evaluation = _evaluate_buy_rule_predicates(
            buy_params,
            indicators,
            active_predicates=active_predicates,
        )
        if evaluation["reason"] is not None:
            skipped.append(_skip(symbol, evaluation["reason"]))
            continue
        triggers = evaluation["triggers"]
        if not triggers:
            skipped.append(_skip(symbol, "no active buy predicates"))
            continue

        amount = _buy_amount(
            common_params,
            buy_params,
            cash,
            indicators,
            market_regime_budget_factor=market_budget_factor,
        )
        rsi = _safe_rsi(indicators)
        draft: dict[str, Any] = {
            "symbol": symbol,
            "instrument_type": market,
            "profile": profile.get("profile"),
            "tier": profile.get("tier"),
            "triggers": triggers,
            "suggested_amount": amount,
            "amount": amount,
            "price_type": "market",
            "rsi": rsi,
        }
        if holdings_map.get(symbol) is None:
            dca_stages = int(to_optional_float(buy_params.get("dca_stages")) or 1)
            if dca_stages < 1:
                dca_stages = 1
            draft["dca_stage"] = f"1/{dca_stages}"
        drafts.append(draft)

    drafts.sort(key=lambda item: to_optional_float(item.get("rsi")) or 999.0)
    return drafts, skipped


def _evaluate_sell_rules(
    sell_params: dict[str, Any],
    quantity: float,
    indicators: dict[str, Any],
    *,
    market: str,
) -> tuple[float | None, list[str], str | None]:
    if not sell_params:
        return None, [], None

    rsi = _safe_rsi(indicators)
    full_threshold = to_optional_float(sell_params.get("take_profit_full_rsi"))
    if full_threshold is not None:
        if rsi is None:
            return None, [], _indicator_missing_reason("take_profit_full_rsi")
        if rsi >= full_threshold:
            return quantity, ["take_profit_full_rsi"], None

    partial_threshold = to_optional_float(sell_params.get("take_profit_partial_rsi"))
    if partial_threshold is not None:
        if rsi is None:
            return None, [], _indicator_missing_reason("take_profit_partial_rsi")
        if rsi >= partial_threshold:
            partial_qty = quantity * 0.5
            if market in {
                InstrumentType.equity_kr.value,
                InstrumentType.equity_us.value,
            }:
                normalized_qty = float(math.floor(partial_qty))
                if normalized_qty < 1:
                    return None, [], "partial sell quantity below 1 share"
                return normalized_qty, ["take_profit_partial_rsi"], None
            return round(partial_qty, 8), ["take_profit_partial_rsi"], None

    fallback_threshold = to_optional_float(sell_params.get("rsi14_min"))
    if fallback_threshold is not None:
        if rsi is None:
            return None, [], _indicator_missing_reason("rsi14_min")
        if rsi >= fallback_threshold:
            return quantity, ["rsi14_min"], None

    return None, [], None


def _risk_metadata(rule_bundle: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    stop_params = cast(dict[str, Any], rule_bundle.get("stop") or {})
    if not stop_params:
        return None
    return {"stop": stop_params}


def _build_sell_drafts(
    market: str,
    profiles: list[dict[str, Any]],
    *,
    tier_rules: dict[Any, Any],
    holdings: list[dict[str, Any]],
    indicator_map: dict[str, dict[str, Any]],
    action_type: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if action_type == "buy":
        return [], []

    drafts: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    holdings_map = _holding_by_symbol(holdings)

    for profile in profiles:
        symbol = str(profile.get("symbol", ""))
        if not symbol:
            continue
        holding = holdings_map.get(symbol)
        quantity = to_optional_float((holding or {}).get("quantity"))
        if holding is None or quantity is None or quantity <= 0:
            continue

        sell_mode = str(profile.get("sell_mode") or "any").strip().lower()
        if sell_mode == "none":
            continue

        indicators = indicator_map.get(symbol) or {}
        rsi = _safe_rsi(indicators)
        if profile.get("profile") == "exit":
            drafts.append(
                {
                    "symbol": symbol,
                    "instrument_type": market,
                    "profile": "exit",
                    "tier": profile.get("tier"),
                    "triggers": ["exit_profile"],
                    "suggested_qty": quantity,
                    "quantity": quantity,
                    "price_type": "market",
                    "rsi": rsi,
                }
            )
            continue

        rule_bundle = _lookup_rule_bundle(
            tier_rules,
            symbol=symbol,
            tier=int(profile.get("tier") or 0),
            profile=str(profile.get("profile", "")),
        )
        rebalance_params = cast(dict[str, Any], rule_bundle.get("rebalance") or {})
        if sell_mode == "rebalance_only" and not rebalance_params:
            skipped.append(_skip(symbol, "rebalance_only requires rebalance params"))
            continue

        sell_params = cast(dict[str, Any], rule_bundle.get("sell") or {})
        suggested_qty, triggers, skip_reason = _evaluate_sell_rules(
            sell_params,
            quantity,
            indicators,
            market=market,
        )
        if skip_reason is not None:
            skipped.append(_skip(symbol, skip_reason))
            continue
        if suggested_qty is None or suggested_qty <= 0:
            continue

        draft: dict[str, Any] = {
            "symbol": symbol,
            "instrument_type": market,
            "profile": profile.get("profile"),
            "tier": profile.get("tier"),
            "triggers": triggers,
            "suggested_qty": suggested_qty,
            "quantity": suggested_qty,
            "price_type": "market",
            "rsi": rsi,
        }
        risk_metadata = _risk_metadata(rule_bundle)
        if risk_metadata is not None:
            draft["risk_metadata"] = risk_metadata
        drafts.append(draft)

    drafts.sort(
        key=lambda item: to_optional_float(item.get("rsi")) or -999.0,
        reverse=True,
    )
    return drafts, skipped


async def prepare_trade_draft_impl(
    instrument_type: str | None = None,
    action_type: str = "all",
    dry_run: bool = True,
) -> dict[str, Any]:
    normalized_action_type = (action_type or "all").strip().lower()
    if normalized_action_type not in {"all", "buy", "sell"}:
        raise ValueError("action_type must be one of: all, buy, sell")

    markets_output: list[dict[str, Any]] = []
    for market in _resolve_markets(instrument_type):
        inputs = cast(dict[str, Any], await _maybe_await(_load_market_inputs(market)))
        profiles = _normalize_profile_rows(
            market, cast(list[dict[str, Any]], inputs.get("profiles") or [])
        )
        tier_rules = cast(dict[Any, Any], inputs.get("tier_rules") or {})
        market_filters = cast(list[dict[str, Any]], inputs.get("market_filters") or [])
        holdings = _normalize_holding_rows(
            market, cast(list[dict[str, Any]], inputs.get("holdings") or [])
        )
        cash = cast(dict[str, Any], inputs.get("cash") or {})
        buy_universe = _normalize_buy_universe_rows(
            market, cast(list[dict[str, Any]], inputs.get("buy_universe") or [])
        )
        indicator_map = _normalize_indicator_map(
            market,
            cast(dict[str, dict[str, Any]], inputs.get("indicator_map") or {}),
        )
        fear_greed = cast(dict[str, Any] | None, inputs.get("fear_greed"))
        funding_rates = cast(
            dict[str, dict[str, Any]], inputs.get("funding_rates") or {}
        )

        market_filter_result = _evaluate_market_filters(
            market,
            market_filters,
            buy_universe=buy_universe,
            fear_greed=fear_greed,
            funding_rates=funding_rates,
        )
        warnings = list(cast(list[str], market_filter_result["warnings"]))
        skipped: list[dict[str, Any]] = []
        if market_filter_result["kill_switch"]:
            markets_output.append(
                {
                    "instrument_type": market,
                    "filters_applied": cast(
                        list[str], market_filter_result["filters_applied"]
                    ),
                    "kill_switch": True,
                    "buy_drafts": [],
                    "sell_drafts": [],
                    "skipped": skipped,
                    "warnings": warnings,
                }
            )
            continue

        buy_drafts, skipped_buy = _build_buy_drafts(
            market,
            profiles,
            tier_rules=tier_rules,
            holdings=holdings,
            cash=cash,
            buy_universe=buy_universe,
            indicator_map=indicator_map,
            market_filters=cast(
                list[dict[str, Any]], market_filter_result["buy_filters"]
            ),
            fear_greed=fear_greed,
            funding_rates=funding_rates,
            action_type=normalized_action_type,
        )
        sell_drafts, skipped_sell = _build_sell_drafts(
            market,
            profiles,
            tier_rules=tier_rules,
            holdings=holdings,
            indicator_map=indicator_map,
            action_type=normalized_action_type,
        )
        skipped.extend(skipped_buy)
        skipped.extend(skipped_sell)

        markets_output.append(
            {
                "instrument_type": market,
                "filters_applied": cast(
                    list[str], market_filter_result["filters_applied"]
                ),
                "kill_switch": False,
                "buy_drafts": buy_drafts,
                "sell_drafts": sell_drafts,
                "skipped": skipped,
                "warnings": warnings,
            }
        )

    return {
        "success": True,
        "action_type": normalized_action_type,
        "dry_run": dry_run,
        "markets": markets_output,
    }


__all__ = ["prepare_trade_draft_impl"]
