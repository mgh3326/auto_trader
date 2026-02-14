"""Shared utilities, normalizers, and constants for MCP tools."""

from __future__ import annotations

import datetime
import logging
from typing import Any

import pandas as pd

from app.core.symbol import to_db_symbol
from app.mcp_server.env_utils import _env_int
from app.mcp_server.tick_size import adjust_tick_size_kr
from app.models.manual_holdings import MarketType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MCP_USER_ID = _env_int("MCP_USER_ID", 1)
MCP_DCA_USER_ID = _env_int("MCP_DCA_USER_ID", MCP_USER_ID)
DEFAULT_ACCOUNT_KEYS = {"default", "default_account", "기본계좌", "기본_계좌"}
INSTRUMENT_TO_MARKET = {
    "equity_kr": "kr",
    "equity_us": "us",
    "crypto": "crypto",
}
ACCOUNT_FILTER_ALIASES = {
    "kis": {"kis", "korea_investment", "한국투자", "한국투자증권"},
    "upbit": {"upbit", "업비트"},
    "toss": {"toss", "토스"},
    "samsung_pension": {"samsung_pension", "samsung_pension_account"},
    "isa": {"isa"},
}
UPBIT_TICKER_BATCH_SIZE = 50
DEFAULT_MINIMUM_VALUES: dict[str, float] = {
    "equity_kr": 5000.0,
    "equity_us": 10.0,
    "crypto": 5000.0,
}

# ---------------------------------------------------------------------------
# Symbol Detection
# ---------------------------------------------------------------------------


def is_korean_equity_code(symbol: str) -> bool:
    s = symbol.strip().upper()
    return len(s) == 6 and s.isalnum()


def is_crypto_market(symbol: str) -> bool:
    s = symbol.strip().upper()
    return s.startswith("KRW-") or s.startswith("USDT-")


def is_us_equity_symbol(symbol: str) -> bool:
    s = symbol.strip().upper()
    return (not is_crypto_market(s)) and any(c.isalpha() for c in s)


# ---------------------------------------------------------------------------
# Symbol Normalization
# ---------------------------------------------------------------------------


def normalize_symbol_input(symbol: str | int, market: str | None = None) -> str:
    s = str(symbol).strip()
    if market is None:
        if s.isdigit() and len(s) <= 6:
            s = s.zfill(6)
    elif market.lower() in (
        "kr",
        "krx",
        "korea",
        "kospi",
        "kosdaq",
        "kis",
        "equity_kr",
        "naver",
    ):
        if s.isdigit():
            s = s.zfill(6)
    return s


# ---------------------------------------------------------------------------
# Market Normalization
# ---------------------------------------------------------------------------


def normalize_market(market: str | None) -> str | None:
    if not market:
        return None
    normalized = market.strip().lower()
    if not normalized:
        return None
    mapping = {
        "crypto": "crypto",
        "upbit": "crypto",
        "krw": "crypto",
        "usdt": "crypto",
        "kr": "equity_kr",
        "krx": "equity_kr",
        "korea": "equity_kr",
        "kospi": "equity_kr",
        "kosdaq": "equity_kr",
        "kis": "equity_kr",
        "equity_kr": "equity_kr",
        "us": "equity_us",
        "usa": "equity_us",
        "nyse": "equity_us",
        "nasdaq": "equity_us",
        "yahoo": "equity_us",
        "equity_us": "equity_us",
    }
    return mapping.get(normalized)


def resolve_market_type(symbol: str, market: str | None) -> tuple[str, str]:
    market_type = normalize_market(market)

    if market_type == "crypto":
        symbol = symbol.upper()
        if not is_crypto_market(symbol):
            raise ValueError("crypto symbols must include KRW-/USDT- prefix")
        return "crypto", symbol

    if market_type == "equity_kr":
        if not is_korean_equity_code(symbol):
            raise ValueError("korean equity symbols must be 6 alphanumeric characters")
        return "equity_kr", symbol

    if market_type == "equity_us":
        symbol = symbol.upper()
        if is_crypto_market(symbol):
            raise ValueError("us equity symbols must not include KRW-/USDT- prefix")
        if not is_us_equity_symbol(symbol):
            raise ValueError("invalid US equity symbol")
        return "equity_us", symbol

    if is_crypto_market(symbol):
        return "crypto", symbol.upper()

    if is_korean_equity_code(symbol):
        return "equity_kr", symbol

    if is_us_equity_symbol(symbol):
        return "equity_us", symbol

    raise ValueError("Unsupported symbol format")


# ---------------------------------------------------------------------------
# Value Normalization
# ---------------------------------------------------------------------------


def normalize_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, pd.Timedelta):
        return value.total_seconds()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    return value


def normalize_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {str(key): normalize_value(value) for key, value in row.items()}
        for row in df.to_dict(orient="records")
    ]


# ---------------------------------------------------------------------------
# Type Conversion
# ---------------------------------------------------------------------------


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def to_optional_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def to_optional_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except Exception:
        return None


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Error Payload
# ---------------------------------------------------------------------------


def error_payload(
    *,
    source: str,
    message: str,
    symbol: str | None = None,
    instrument_type: str | None = None,
    query: str | None = None,
    suggestion: str | None = None,
    details: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": message, "source": source}
    if symbol is not None:
        payload["symbol"] = symbol
    if instrument_type is not None:
        payload["instrument_type"] = instrument_type
    if query is not None:
        payload["query"] = query
    if suggestion is not None:
        payload["suggestion"] = suggestion
    if details is not None:
        payload["details"] = details
    return payload


# ---------------------------------------------------------------------------
# Account Normalization
# ---------------------------------------------------------------------------


def normalize_account_key(value: str | None) -> str:
    if not value:
        return ""
    normalized = value.strip().lower()
    return "".join(ch if ch.isalnum() else "_" for ch in normalized).strip("_")


def canonical_account_id(broker: str, account_name: str | None) -> str:
    broker_key = normalize_account_key(broker)
    account_key = normalize_account_key(account_name)

    if not account_key or account_key in DEFAULT_ACCOUNT_KEYS:
        return broker_key

    raw_name = (account_name or "").strip().lower()
    if "isa" in account_key:
        return "isa"
    if ("samsung" in account_key and "pension" in account_key) or (
        "삼성" in raw_name and "연금" in raw_name
    ):
        return "samsung_pension"

    return account_key


def normalize_account_filter(account: str | None) -> str | None:
    key = normalize_account_key(account)
    if not key:
        return None
    for canonical, aliases in ACCOUNT_FILTER_ALIASES.items():
        if key == canonical or key in aliases:
            return canonical
    return key


def match_account_filter(position: dict[str, Any], account_filter: str | None) -> bool:
    if not account_filter:
        return True

    account_keys = {
        normalize_account_filter(position.get("account")),
        normalize_account_filter(position.get("broker")),
        normalize_account_filter(position.get("account_name")),
    }
    account_keys.discard(None)

    account_keys.add(
        canonical_account_id(
            str(position.get("broker", "")),
            str(position.get("account_name", "")),
        )
    )

    return account_filter in account_keys


# ---------------------------------------------------------------------------
# Holdings Helpers
# ---------------------------------------------------------------------------


def parse_holdings_market_filter(market: str | None) -> str | None:
    if market is None or not market.strip():
        return None
    market_type = normalize_market(market)
    if market_type is None:
        raise ValueError("market must be one of: kr, us, crypto")
    return market_type


def manual_market_to_instrument_type(market_type: MarketType) -> str:
    if market_type == MarketType.KR:
        return "equity_kr"
    if market_type == MarketType.US:
        return "equity_us"
    if market_type == MarketType.CRYPTO:
        return "crypto"
    raise ValueError(f"Unsupported market type: {market_type}")


def instrument_to_manual_market_type(market_type: str | None) -> MarketType | None:
    if market_type == "equity_kr":
        return MarketType.KR
    if market_type == "equity_us":
        return MarketType.US
    if market_type == "crypto":
        return MarketType.CRYPTO
    return None


def normalize_position_symbol(symbol: str, instrument_type: str) -> str:
    normalized = symbol.strip().upper()
    if instrument_type == "crypto" and normalized and "-" not in normalized:
        return f"KRW-{normalized}"
    if instrument_type == "equity_us":
        return to_db_symbol(normalized).upper()
    return normalized


def position_to_output(position: dict[str, Any]) -> dict[str, Any]:
    output = {
        "symbol": position["symbol"],
        "name": position["name"],
        "market": position["market"],
        "quantity": position["quantity"],
        "avg_buy_price": position["avg_buy_price"],
        "current_price": position["current_price"],
        "evaluation_amount": position["evaluation_amount"],
        "profit_loss": position["profit_loss"],
        "profit_rate": position["profit_rate"],
    }
    if "price_error" in position:
        output["price_error"] = position["price_error"]
    return output


def value_for_minimum_filter(position: dict[str, Any]) -> float:
    evaluation_amount = position.get("evaluation_amount")
    if evaluation_amount is not None:
        return to_float(evaluation_amount, default=0.0)

    if position.get("current_price") is None:
        return float("inf")

    quantity = to_float(position.get("quantity"))
    current_price = to_float(position.get("current_price"))
    return quantity * current_price


def format_filter_threshold(value: float) -> str:
    return f"{value:g}"


def build_holdings_summary(
    positions: list[dict[str, Any]], include_current_price: bool
) -> dict[str, Any]:
    total_buy_amount = round(
        sum(
            to_float(position.get("avg_buy_price")) * to_float(position.get("quantity"))
            for position in positions
        ),
        2,
    )

    if not include_current_price:
        return {
            "total_buy_amount": total_buy_amount,
            "total_evaluation": None,
            "total_profit_loss": None,
            "total_profit_rate": None,
            "position_count": len(positions),
            "weights": None,
        }

    total_evaluation = round(
        sum(to_float(position.get("evaluation_amount")) for position in positions),
        2,
    )
    total_profit_loss = round(
        sum(to_float(position.get("profit_loss")) for position in positions),
        2,
    )
    total_profit_rate = (
        round((total_profit_loss / total_buy_amount) * 100, 2)
        if total_buy_amount > 0
        else None
    )

    weights: list[dict[str, Any]] = []
    if total_evaluation > 0:
        for position in positions:
            evaluation = to_float(position.get("evaluation_amount"))
            if evaluation <= 0:
                continue
            weights.append(
                {
                    "symbol": position.get("symbol"),
                    "name": position.get("name"),
                    "weight_pct": round((evaluation / total_evaluation) * 100, 2),
                }
            )
        weights.sort(key=lambda item: to_float(item.get("weight_pct")), reverse=True)

    return {
        "total_buy_amount": total_buy_amount,
        "total_evaluation": total_evaluation,
        "total_profit_loss": total_profit_loss,
        "total_profit_rate": total_profit_rate,
        "position_count": len(positions),
        "weights": weights,
    }


def is_position_symbol_match(
    *,
    position_symbol: str,
    query_symbol: str,
    instrument_type: str,
) -> bool:
    if instrument_type == "crypto":
        pos_norm = normalize_position_symbol(position_symbol, "crypto")
        query_norm = normalize_position_symbol(query_symbol, "crypto")
        if pos_norm == query_norm:
            return True
        pos_base = pos_norm.split("-", 1)[-1]
        query_base = query_norm.split("-", 1)[-1]
        return pos_base == query_base

    if instrument_type == "equity_us":
        return (
            to_db_symbol(position_symbol).upper() == to_db_symbol(query_symbol).upper()
        )

    return position_symbol.upper() == query_symbol.upper()


def recalculate_profit_fields(position: dict[str, Any]) -> None:
    current_price = position.get("current_price")
    quantity = to_float(position.get("quantity"))
    avg_buy_price = to_float(position.get("avg_buy_price"))

    if current_price is None or quantity <= 0:
        position["current_price"] = None
        position["evaluation_amount"] = None
        position["profit_loss"] = None
        position["profit_rate"] = None
        return

    current_price = to_float(current_price)
    position["current_price"] = current_price
    position["evaluation_amount"] = round(current_price * quantity, 2)

    if avg_buy_price > 0:
        profit_loss = (current_price - avg_buy_price) * quantity
        position["profit_loss"] = round(profit_loss, 2)
        position["profit_rate"] = round(
            ((current_price - avg_buy_price) / avg_buy_price) * 100, 2
        )
    else:
        position["profit_loss"] = None
        position["profit_rate"] = None


# ---------------------------------------------------------------------------
# Recommendation Builder
# ---------------------------------------------------------------------------


def build_recommendation_for_equity(
    analysis: dict[str, Any],
    market_type: str,
) -> dict[str, Any] | None:
    quote = analysis.get("quote")
    indicators = analysis.get("indicators")
    sr = analysis.get("support_resistance")
    opinions = analysis.get("opinions", {})
    consensus = opinions.get("consensus")
    valuation = analysis.get("valuation", {})

    if not quote:
        return None

    current_price = quote.get("price") or quote.get("current_price")
    if not current_price:
        return None

    recommendation: dict[str, Any] = {
        "action": "hold",
        "confidence": "low",
        "buy_zones": [],
        "sell_targets": [],
        "stop_loss": None,
        "reasoning": "",
    }

    reasoning_parts: list[str] = []
    score = 0
    max_score = 0

    if indicators:
        indicators_dict = indicators.get("indicators", indicators)
        rsi_data = indicators_dict.get("rsi")
        rsi = None
        if isinstance(rsi_data, dict):
            rsi = rsi_data.get("14") or rsi_data.get(14)
        elif isinstance(rsi_data, (int, float)):
            rsi = rsi_data

        if rsi:
            max_score += 2
            if rsi < 30:
                score += 2
                reasoning_parts.append(f"RSI {rsi:.1f} (oversold)")
            elif rsi < 40:
                score += 1
                reasoning_parts.append(f"RSI {rsi:.1f} (bearish)")
            elif rsi > 70:
                score -= 2
                reasoning_parts.append(f"RSI {rsi:.1f} (overbought)")
            elif rsi > 60:
                score -= 1
                reasoning_parts.append(f"RSI {rsi:.1f} (bullish)")

    if consensus:
        buy_count = consensus.get("buy_count", 0)
        sell_count = consensus.get("sell_count", 0)
        strong_buy_count = consensus.get("strong_buy_count", 0)
        total = consensus.get("total_count", 0)

        if total > 0:
            max_score += 2
            buy_ratio = buy_count / total
            sell_ratio = sell_count / total

            if buy_ratio > 0.6:
                score += 2
                if strong_buy_count > 0 and strong_buy_count >= buy_count / 2:
                    reasoning_parts.append(
                        f"Analyst consensus strong bullish ({buy_count} buy, {strong_buy_count} strong buy vs {sell_count} sell)"
                    )
                else:
                    reasoning_parts.append(
                        f"Analyst consensus bullish ({buy_count} buy vs {sell_count} sell)"
                    )
            elif buy_ratio > 0.4:
                score += 1
                reasoning_parts.append(
                    f"Analyst consensus moderate ({buy_count} buy vs {sell_count} sell)"
                )
            elif sell_ratio > 0.6:
                score -= 2
                reasoning_parts.append(
                    f"Analyst consensus bearish ({sell_count} sell vs {buy_count} buy)"
                )
            elif sell_ratio > 0.4:
                score -= 1
                reasoning_parts.append(
                    f"Analyst consensus cautious ({sell_count} sell vs {buy_count} buy)"
                )

    if score >= 2:
        recommendation["action"] = "buy"
        recommendation["confidence"] = "high" if score >= 3 else "medium"
    elif score <= -2:
        recommendation["action"] = "sell"
        recommendation["confidence"] = "high" if score <= -3 else "medium"
    else:
        recommendation["action"] = "hold"
        recommendation["confidence"] = "low"

    buy_zones_indicators: list[dict[str, Any]] = []

    if indicators:
        indicators_dict = indicators.get("indicators", indicators)
        bb = indicators_dict.get("bollinger") or indicators_dict.get("bollinger_bands")
        if bb and isinstance(bb, dict):
            lower = bb.get("lower") or bb.get("lb")
            if lower:
                buy_zones_indicators.append(
                    {
                        "price": float(lower),
                        "type": "bollinger_lower",
                        "reasoning": "BB lower band",
                    }
                )

    buy_zones_supports: list[dict[str, Any]] = []

    if sr:
        supports = sr.get("supports") or []
        for s in supports[:3]:
            price = s.get("price")
            if price and isinstance(price, (int, float)) and price < current_price:
                buy_zones_supports.append(
                    {
                        "price": float(price),
                        "type": "support",
                        "reasoning": f"Support at {price}",
                    }
                )

    if indicators and sr and len(buy_zones_indicators) < 3:
        indicators_dict = indicators.get("indicators", indicators)
        bb = indicators_dict.get("bollinger") or indicators_dict.get("bollinger_bands")
        if bb and isinstance(bb, dict):
            lower = bb.get("lower") or bb.get("lb")
            if lower:
                lower_price = float(lower)
                lower_price_diff = abs(lower_price - current_price)
                if (
                    lower_price < current_price
                    and lower_price_diff < current_price * 0.05
                ):
                    buy_zones_indicators.append(
                        {
                            "price": lower_price,
                            "type": "bollinger_lower_near",
                            "reasoning": f"BB lower near ({lower_price_diff / current_price * 100:.1f}% below)",
                        }
                    )

    if sr:
        supports = sr.get("supports") or []
        for s in supports:
            price = s.get("price")
            if price and isinstance(price, (int, float)) and price < current_price:
                price_diff = current_price - float(price)
                if price_diff < current_price * 0.05 and len(buy_zones_indicators) < 3:
                    buy_zones_indicators.append(
                        {
                            "price": float(price),
                            "type": "support_near",
                            "reasoning": f"Near support ({price_diff / current_price * 100:.1f}% below)",
                        }
                    )
                    break

    all_buy_zones = buy_zones_indicators + buy_zones_supports
    all_buy_zones.sort(key=lambda z: z["price"])
    recommendation["buy_zones"] = all_buy_zones[:3]

    sell_targets: list[dict[str, Any]] = []

    if sr:
        resistances = sr.get("resistances") or []
        for r in resistances[:2]:
            price = r.get("price")
            if price and isinstance(price, (int, float)) and price > current_price:
                sell_targets.append(
                    {
                        "price": float(price),
                        "type": "resistance",
                        "reasoning": f"Resistance at {price}",
                    }
                )

    if consensus:
        avg_target = consensus.get("avg_target_price")
        max_target = consensus.get("max_target_price")
        if avg_target:
            sell_targets.append(
                {
                    "price": float(avg_target),
                    "type": "consensus_avg",
                    "reasoning": "Analyst consensus average target",
                }
            )
        if max_target:
            sell_targets.append(
                {
                    "price": float(max_target),
                    "type": "consensus_max",
                    "reasoning": "Analyst consensus max target",
                }
            )

    sell_targets.sort(key=lambda t: t["price"])
    recommendation["sell_targets"] = sell_targets[:3]

    stop_loss: float | None = None

    if sr:
        supports = sr.get("supports") or []
        for s in supports:
            price = s.get("price")
            if price and isinstance(price, (int, float)) and price < current_price:
                candidate = float(price) * 0.98
                stop_loss = candidate
                break

    if stop_loss is None:
        if valuation:
            low_52w = valuation.get("low_52w")
            if low_52w:
                stop_loss = float(low_52w)
            else:
                stop_loss = float(current_price) * 0.92
        else:
            stop_loss = float(current_price) * 0.92

    if market_type == "equity_kr":
        stop_loss = adjust_tick_size_kr(stop_loss)

    recommendation["stop_loss"] = stop_loss

    if reasoning_parts:
        recommendation["reasoning"] = "; ".join(reasoning_parts)
    else:
        recommendation["reasoning"] = "Insufficient data for detailed reasoning"

    return recommendation


__all__ = [
    # Constants
    "MCP_USER_ID",
    "MCP_DCA_USER_ID",
    "DEFAULT_ACCOUNT_KEYS",
    "INSTRUMENT_TO_MARKET",
    "ACCOUNT_FILTER_ALIASES",
    "UPBIT_TICKER_BATCH_SIZE",
    "DEFAULT_MINIMUM_VALUES",
    # Symbol detection
    "is_korean_equity_code",
    "is_crypto_market",
    "is_us_equity_symbol",
    # Symbol normalization
    "normalize_symbol_input",
    # Market normalization
    "normalize_market",
    "resolve_market_type",
    # Value normalization
    "normalize_value",
    "normalize_rows",
    # Type conversion
    "to_float",
    "to_optional_float",
    "to_optional_int",
    "to_int",
    # Error payload
    "error_payload",
    # Account normalization
    "normalize_account_key",
    "canonical_account_id",
    "normalize_account_filter",
    "match_account_filter",
    # Holdings helpers
    "parse_holdings_market_filter",
    "manual_market_to_instrument_type",
    "instrument_to_manual_market_type",
    "normalize_position_symbol",
    "position_to_output",
    "value_for_minimum_filter",
    "format_filter_threshold",
    "build_holdings_summary",
    "is_position_symbol_match",
    "recalculate_profit_fields",
    # Recommendation builder
    "build_recommendation_for_equity",
    # Logger
    "logger",
]
