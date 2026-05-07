"""Built-in deterministic alias dictionaries for the news entity matcher (ROB-130).

Data-only module. Keep entries narrow and high-signal. Each entry is a (symbol,
market, canonical_name, alias_terms) tuple. `alias_terms` are matched
case-insensitively against title + summary + joined keywords. Korean terms are
matched as substrings; English terms are matched on word boundaries.

These dictionaries are intentionally a small, high-precision set covering the
acceptance-criteria examples (AMZN, 005930, BTC) plus the most-traded peers.
Long-tail mapping is delegated to the DB symbol universe + `stock_aliases`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AliasEntry:
    symbol: str          # canonical DB form (e.g. "005930", "AMZN", "BTC")
    market: str          # "kr" | "us" | "crypto"
    canonical_name: str  # display name
    aliases: tuple[str, ...]  # case-insensitive substring/word-boundary terms


KR_ALIASES: tuple[AliasEntry, ...] = (
    AliasEntry("005930", "kr", "삼성전자", ("삼성전자", "삼전", "Samsung Electronics")),
    AliasEntry("000660", "kr", "SK하이닉스", ("SK하이닉스", "하이닉스", "닉스", "SK Hynix")),
    AliasEntry("035420", "kr", "NAVER", ("네이버", "NAVER")),
    AliasEntry("035720", "kr", "카카오", ("카카오",)),
    AliasEntry("323410", "kr", "카카오뱅크", ("카카오뱅크",)),
    AliasEntry("377300", "kr", "카카오페이", ("카카오페이",)),
    AliasEntry("207940", "kr", "삼성바이오로직스", ("삼성바이오", "삼성바이오로직스")),
    AliasEntry("005380", "kr", "현대차", ("현대차", "현대자동차", "Hyundai Motor")),
    AliasEntry("005490", "kr", "POSCO홀딩스", ("POSCO", "포스코")),
    AliasEntry("373220", "kr", "LG에너지솔루션", ("LG에너지솔루션", "LG엔솔")),
)

US_ALIASES: tuple[AliasEntry, ...] = (
    AliasEntry("AAPL", "us", "Apple", ("Apple", "AAPL", "애플")),
    AliasEntry("AMZN", "us", "Amazon", ("Amazon", "AMZN", "아마존")),
    AliasEntry("NVDA", "us", "Nvidia", ("Nvidia", "NVDA", "엔비디아")),
    AliasEntry("TSLA", "us", "Tesla", ("Tesla", "TSLA", "테슬라")),
    AliasEntry("META", "us", "Meta", ("Meta Platforms", "META", "메타")),
    AliasEntry("GOOGL", "us", "Alphabet", ("Alphabet", "Google", "GOOGL", "GOOG", "구글")),
    AliasEntry("MSFT", "us", "Microsoft", ("Microsoft", "MSFT", "마이크로소프트")),
    AliasEntry("AMD", "us", "AMD", ("AMD", "Advanced Micro")),
    AliasEntry("AVGO", "us", "Broadcom", ("Broadcom", "AVGO")),
    AliasEntry("BRK.B", "us", "Berkshire Hathaway B", ("Berkshire Hathaway",)),
)

CRYPTO_ALIASES: tuple[AliasEntry, ...] = (
    AliasEntry("BTC", "crypto", "Bitcoin", ("Bitcoin", "BTC", "비트코인", "KRW-BTC")),
    AliasEntry("ETH", "crypto", "Ethereum", ("Ethereum", "ETH", "이더리움", "KRW-ETH")),
    AliasEntry("SOL", "crypto", "Solana", ("Solana", "SOL", "솔라나", "KRW-SOL")),
    AliasEntry("XRP", "crypto", "Ripple", ("Ripple", "XRP", "리플", "KRW-XRP")),
    AliasEntry("DOGE", "crypto", "Dogecoin", ("Dogecoin", "DOGE", "도지코인", "KRW-DOGE")),
)

ALL_ALIASES: tuple[AliasEntry, ...] = KR_ALIASES + US_ALIASES + CRYPTO_ALIASES
