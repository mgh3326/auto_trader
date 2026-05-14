"""Pure KR common/preferred-share pair helpers for read-only /invest cards."""

from __future__ import annotations

from dataclasses import dataclass

PREFERRED_SUFFIXES: tuple[str, ...] = ("우B", "우C", "우", "1우", "2우B", "3우B")
SEED_COMMON_PREFERRED_PAIRS: tuple[tuple[str, str, str, str], ...] = (
    ("005930", "삼성전자", "005935", "삼성전자우"),
)


@dataclass(frozen=True)
class KRSymbolRow:
    symbol: str
    name: str
    exchange: str | None = None


@dataclass(frozen=True)
class CommonPreferredPair:
    common_symbol: str
    common_name: str
    preferred_symbol: str
    preferred_name: str
    exchange: str | None = None
    mapping_source: str = "heuristic"


def preferred_base_name(name: str) -> str | None:
    normalized = (name or "").strip()
    for suffix in sorted(PREFERRED_SUFFIXES, key=len, reverse=True):
        if normalized.endswith(suffix) and len(normalized) > len(suffix):
            return normalized[: -len(suffix)]
    return None


def is_preferred_name(name: str) -> bool:
    return preferred_base_name(name) is not None


def discover_common_preferred_pairs(
    rows: list[KRSymbolRow],
    *,
    symbols: set[str] | None = None,
    include_seed: bool = True,
) -> list[CommonPreferredPair]:
    """Discover common/preferred pairs without DB writes or provider calls."""

    by_name = {row.name.strip(): row for row in rows if row.name.strip()}
    by_symbol = {row.symbol.strip(): row for row in rows if row.symbol.strip()}
    wanted = {s.strip() for s in symbols or set() if s.strip()}
    pairs: dict[tuple[str, str], CommonPreferredPair] = {}

    def _include(common_symbol: str, preferred_symbol: str) -> bool:
        return not wanted or common_symbol in wanted or preferred_symbol in wanted

    for row in rows:
        base = preferred_base_name(row.name)
        if not base:
            continue
        common = by_name.get(base)
        if common is None or common.symbol == row.symbol:
            continue
        if not _include(common.symbol, row.symbol):
            continue
        pairs[(common.symbol, row.symbol)] = CommonPreferredPair(
            common_symbol=common.symbol,
            common_name=common.name,
            preferred_symbol=row.symbol,
            preferred_name=row.name,
            exchange=row.exchange or common.exchange,
            mapping_source="heuristic_name_suffix",
        )

    if include_seed:
        for (
            common_symbol,
            common_name,
            preferred_symbol,
            preferred_name,
        ) in SEED_COMMON_PREFERRED_PAIRS:
            if not _include(common_symbol, preferred_symbol):
                continue
            common = by_symbol.get(common_symbol)
            preferred = by_symbol.get(preferred_symbol)
            pairs.setdefault(
                (common_symbol, preferred_symbol),
                CommonPreferredPair(
                    common_symbol=common_symbol,
                    common_name=common.name if common is not None else common_name,
                    preferred_symbol=preferred_symbol,
                    preferred_name=preferred.name
                    if preferred is not None
                    else preferred_name,
                    exchange=(preferred.exchange if preferred is not None else None)
                    or (common.exchange if common is not None else None),
                    mapping_source="seed_samsung_pair",
                ),
            )

    return sorted(
        pairs.values(), key=lambda pair: (pair.common_symbol, pair.preferred_symbol)
    )
