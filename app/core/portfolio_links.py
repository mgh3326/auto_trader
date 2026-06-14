from urllib.parse import quote

from app.core.config import settings

# Shared market aliases for position detail links
_KR_ALIASES = frozenset(
    {"kr", "krx", "korea", "kospi", "kosdaq", "kis", "equity_kr", "naver"}
)
_US_ALIASES = frozenset({"us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"})
_CRYPTO_ALIASES = frozenset({"crypto", "upbit", "krw", "usdt", "btc"})


def normalize_position_market_type(market_type: str | None) -> str | None:
    """Normalize a market string to 'kr', 'us', or 'crypto' for portfolio links."""
    if not market_type:
        return None
    m = market_type.strip().lower()
    if m in _CRYPTO_ALIASES:
        return "crypto"
    if m in _KR_ALIASES:
        return "kr"
    if m in _US_ALIASES:
        return "us"
    return None


def build_position_detail_url(
    symbol: str | None, market_type: str | None
) -> str | None:
    """Build a URL to the symbol detail page in the /invest web UI.

    Example: https://mgh3326.duckdns.org/invest/stocks/kr/005930
    (구 /portfolio/positions/... 는 410 Gone — ROB-558에서 교체)
    """
    normalized_symbol = str(symbol or "").strip()
    normalized_market = normalize_position_market_type(market_type)

    if not normalized_symbol or normalized_market is None:
        return None

    encoded_symbol = quote(normalized_symbol, safe="-._~")
    return f"{settings.public_base_url.rstrip('/')}/invest/stocks/{normalized_market}/{encoded_symbol}"

