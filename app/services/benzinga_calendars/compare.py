"""Benzinga vs Finnhub earnings comparison (hk 6513).

Pure function for the replacement verdict the operator asked for: how much
extra coverage Benzinga provides over Finnhub for the same symbols and window,
and — only for items both providers observed — whether they agree on the time
of day. Nothing here calls the network or the database; both row lists are
already-normalized earnings rows.

Time-of-day is compared on the closed hint vocabulary
(``before_open``/``after_close``/``during_market``/``unknown``), which is the
only time signal Hinzhub rows carry. A pair where either side is ``unknown`` is
counted separately and never reported as agreement.
"""

from __future__ import annotations

from typing import Any

from app.core.symbol import to_db_symbol

_TIME_ALIASES = {
    "bmo": "before_open",
    "amc": "after_close",
    "dmh": "during_market",
    "dmt": "during_market",
    "before_open": "before_open",
    "after_close": "after_close",
    "during_market": "during_market",
}


def normalize_hint(row: dict[str, Any]) -> str:
    """Closed time hint for one earnings row (``hour`` or ``time_hint``)."""
    for key in ("time_hint", "hour"):
        raw = row.get(key)
        if isinstance(raw, str):
            token = raw.strip().lower()
            if token:
                return _TIME_ALIASES.get(token, "unknown")
    return "unknown"


def _key(row: dict[str, Any]) -> tuple[str, str] | None:
    symbol = row.get("symbol")
    raw_date = row.get("date")
    if not isinstance(symbol, str) or not isinstance(raw_date, str):
        return None
    return (to_db_symbol(symbol.strip()).upper(), raw_date.strip())


def _index(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = _key(row)
        if key is None:
            continue
        indexed.setdefault(key, row)
    return indexed


def compare_earnings(
    finnhub_rows: list[dict[str, Any]],
    benzinga_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Coverage and time agreement between two earnings row lists."""
    finnhub = _index(finnhub_rows)
    benzinga = _index(benzinga_rows)

    finnhub_symbols = {symbol for symbol, _ in finnhub}
    benzinga_symbols = {symbol for symbol, _ in benzinga}

    co_observed = sorted(set(finnhub) & set(benzinga))
    agree = 0
    unknown_either = 0
    differences: list[dict[str, Any]] = []
    for key in co_observed:
        finnhub_hint = normalize_hint(finnhub[key])
        benzinga_hint = normalize_hint(benzinga[key])
        if "unknown" in (finnhub_hint, benzinga_hint):
            unknown_either += 1
            continue
        if finnhub_hint == benzinga_hint:
            agree += 1
        else:
            differences.append(
                {
                    "symbol": key[0],
                    "date": key[1],
                    "finnhub_hint": finnhub_hint,
                    "benzinga_hint": benzinga_hint,
                }
            )

    return {
        "finnhub": {"items": len(finnhub), "symbols": len(finnhub_symbols)},
        "benzinga": {"items": len(benzinga), "symbols": len(benzinga_symbols)},
        "union_items": len(set(finnhub) | set(benzinga)),
        "only_finnhub_items": len(set(finnhub) - set(benzinga)),
        "only_benzinga_items": len(set(benzinga) - set(finnhub)),
        "coverage": {
            "both_symbols": sorted(finnhub_symbols & benzinga_symbols),
            "only_finnhub_symbols": sorted(finnhub_symbols - benzinga_symbols),
            "only_benzinga_symbols": sorted(benzinga_symbols - finnhub_symbols),
        },
        "co_observed_items": len(co_observed),
        "time_agreement": {
            "agree": agree,
            "differ": len(differences),
            "unknown_either": unknown_either,
        },
        "time_differences": differences,
        "basis": "stored_rows_only; time compared only on co-observed items",
    }


__all__ = ["compare_earnings", "normalize_hint"]
