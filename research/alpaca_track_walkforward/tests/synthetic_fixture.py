"""Compatibility exports for the canonical deterministic H4 corpus.

The original AC27 fixture is now an operator-runnable source module because
the terminal artifact must reproduce the same pinned corpus outside pytest.
Tests keep this import name while consuming the single canonical definition.
"""

from synthetic_corpus import (
    DAY_MS,
    N_SYMBOLS,
    absolute_day_index,
    build_bars_by_symbol,
    close_for,
    make_minute_bars_provider,
    make_universe_snapshot_provider,
    symbol_names,
)

__all__ = [
    "DAY_MS",
    "N_SYMBOLS",
    "absolute_day_index",
    "build_bars_by_symbol",
    "close_for",
    "make_minute_bars_provider",
    "make_universe_snapshot_provider",
    "symbol_names",
]
