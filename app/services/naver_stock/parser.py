from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.naver_stock.types import NaverStockRow, NaverThemeRow, ParseResult

_DROP_KEYS = {
    "cookie",
    "cookies",
    "headers",
    "html",
    "body",
    "content",
    "comment",
    "comments",
    "discussion",
    "message",
    "post",
    "author",
    "authorid",
    "author_id",
    "userid",
    "user_id",
    "usernickname",
    "nickname",
    "writer",
    "tracking",
    "trackingid",
    "session",
    "token",
}

_LIST_KEYS = ("stocks", "items", "itemList", "list", "result", "data", "contents")


def _norm_key(key: str) -> str:
    return "".join(ch.lower() for ch in key if ch.isalnum() or ch == "_")


def sanitize_raw_payload(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            key_str = str(key)
            normalized = _norm_key(key_str)
            if normalized in _DROP_KEYS or any(
                term in normalized
                for term in (
                    "cookie",
                    "header",
                    "author",
                    "user",
                    "comment",
                    "discussion",
                    "html",
                )
            ):
                continue
            safe_child = sanitize_raw_payload(child, depth=depth + 1)
            if safe_child is not None:
                sanitized[key_str] = safe_child
        return sanitized
    if isinstance(value, list):
        return [
            item
            for item in (sanitize_raw_payload(v, depth=depth + 1) for v in value[:20])
            if item is not None
        ]
    if isinstance(value, str):
        if "<html" in value.lower() or "<body" in value.lower() or len(value) > 500:
            return None
        return value
    if isinstance(value, int | float | bool) or value is None:
        return value
    return str(value)


def _find_rows(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    for key in _LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, Mapping)]
        if isinstance(value, Mapping):
            nested = _find_rows(value)
            if nested:
                return nested
    for value in payload.values():
        if isinstance(value, Mapping):
            nested = _find_rows(value)
            if nested:
                return nested
    return []


def _first(row: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _str(row: Mapping[str, Any], keys: Iterable[str]) -> str | None:
    value = _first(row, keys)
    return str(value).strip() if value is not None else None


def _decimal(row: Mapping[str, Any], keys: Iterable[str]) -> Decimal | None:
    value = _first(row, keys)
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).replace(",", "").replace("%", ""))
    except (InvalidOperation, ValueError):
        return None


def _int(row: Mapping[str, Any], keys: Iterable[str]) -> int | None:
    value = _first(row, keys)
    if value is None:
        return None
    try:
        return int(Decimal(str(value).replace(",", "")))
    except (InvalidOperation, ValueError):
        return None


def _symbol(row: Mapping[str, Any]) -> str | None:
    value = _str(
        row, ("itemCode", "itemcode", "stockCode", "symbol", "code", "reutersCode")
    )
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits.zfill(6) if digits and len(digits) <= 6 else value.upper()


def _stock_from_row(row: Mapping[str, Any], rank_fallback: int) -> NaverStockRow | None:
    symbol = _symbol(row)
    if not symbol:
        return None
    return NaverStockRow(
        symbol=symbol,
        name=_str(row, ("stockName", "itemName", "itemname", "name", "nm")),
        rank=_int(row, ("rank", "rn", "no", "ranking")) or rank_fallback,
        price=_decimal(
            row, ("closePrice", "nowPrice", "nowVal", "price", "tradePrice")
        ),
        change_amount=_decimal(
            row,
            ("compareToPreviousClosePrice", "changeAmount", "changeVal", "prevChange"),
        ),
        change_rate=_decimal(
            row,
            ("fluctuationsRatio", "prevChangeRate", "changeRate", "rate", "diffRate"),
        ),
        volume=_int(
            row, ("accumulatedTradingVolume", "tradeVolume", "quant", "volume")
        ),
        trade_value=_decimal(
            row,
            ("accumulatedTradingValue", "tradeAmount", "tradingValue", "tradeValue"),
        ),
        market_cap=_decimal(row, ("marketValue", "marketSum", "marketCap")),
        raw_payload=sanitize_raw_payload(row) or {},
    )


def parse_domestic_stock_default(payload: Any) -> ParseResult[NaverStockRow]:
    rows = _find_rows(payload)
    warnings: list[str] = []
    parsed: list[NaverStockRow] = []
    if not rows:
        return ParseResult(rows=(), warnings=("no stock rows found",))
    for idx, row in enumerate(rows, start=1):
        parsed_row = _stock_from_row(row, idx)
        if parsed_row is None:
            warnings.append(f"stock row {idx} missing symbol")
            continue
        parsed.append(parsed_row)
    return ParseResult(rows=tuple(parsed), warnings=tuple(warnings))


def parse_theme_stocklist(payload: Any) -> ParseResult[NaverStockRow]:
    return parse_domestic_stock_default(payload)


def _leader_symbols(row: Mapping[str, Any]) -> tuple[dict[str, str | None], ...]:
    raw = _first(row, ("leaderSymbols", "topStocks", "stockList", "stocks"))
    leaders: list[dict[str, str | None]] = []
    if isinstance(raw, list):
        for item in raw[:5]:
            if not isinstance(item, Mapping):
                continue
            leaders.append(
                {
                    "symbol": _symbol(item),
                    "name": _str(item, ("stockName", "itemName", "itemname", "name")),
                }
            )
        return tuple(leaders)

    # Current Naver upjong/theme list rows expose leaders as
    # "rank,itemcode,itemname|rank,itemcode,itemname".
    leading_item = _str(row, ("leadingItem",))
    if leading_item:
        for item in leading_item.split("|")[:5]:
            parts = [part.strip() for part in item.split(",")]
            if len(parts) >= 3:
                leaders.append(
                    {"symbol": _symbol({"itemcode": parts[1]}), "name": parts[2]}
                )
        return tuple(leaders)

    return ()


def parse_upjong_theme_list(
    payload: Any, *, event_kind: str
) -> ParseResult[NaverThemeRow]:
    if event_kind not in {"theme", "upjong"}:
        raise ValueError("event_kind must be 'theme' or 'upjong'")
    rows = _find_rows(payload)
    if not rows:
        return ParseResult(rows=(), warnings=("no theme/upjong rows found",))
    parsed: list[NaverThemeRow] = []
    warnings: list[str] = []
    for idx, row in enumerate(rows, start=1):
        theme_no = (
            _str(row, ("themeNo", "themeCode", "no")) if event_kind == "theme" else None
        )
        upjong_code = (
            _str(row, ("upjongCode", "bizCode", "code", "itemCode", "no"))
            if event_kind == "upjong"
            else None
        )
        key = theme_no if event_kind == "theme" else upjong_code
        name = _str(row, ("themeName", "upjongName", "itemname", "itemName", "name"))
        if not key or not name:
            warnings.append(f"{event_kind} row {idx} missing key/name")
            continue
        parsed.append(
            NaverThemeRow(
                event_kind=event_kind,
                source_key=key,
                name=name,
                rank=_int(row, ("rank", "rn", "no")) or idx,
                naver_theme_no=theme_no,
                naver_upjong_code=upjong_code,
                change_rate=_decimal(
                    row, ("fluctuationsRatio", "changeRate", "rate", "diffRate")
                ),
                trade_value=_decimal(
                    row, ("accumulatedTradingValue", "tradingValue", "tradeValue")
                ),
                market_cap=_decimal(
                    row, ("marketValue", "marketSum", "marketCap", "totalMarketSum")
                ),
                stock_count=_int(row, ("stockCount", "count", "itemCount")),
                leader_symbols=_leader_symbols(row),
                raw_payload=sanitize_raw_payload(row) or {},
            )
        )
    return ParseResult(rows=tuple(parsed), warnings=tuple(warnings))
