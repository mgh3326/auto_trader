"""Read-only bounded loader for the finalized KR main snapshot.

The collector's real manifest is a summary object; per-file integrity lives in
``checksums.sha256``.  This loader verifies selected bytes against that ledger
before parsing and refuses holdout paths/dates.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq

try:
    from .holdout_guard import assert_path_not_holdout, assert_range_not_holdout
except ImportError:  # pragma: no cover - legacy flat-module test entrypoint
    from holdout_guard import assert_path_not_holdout, assert_range_not_holdout
try:
    from .pit import Bar, bars_from_table
except ImportError:  # pragma: no cover - legacy flat-module test entrypoint
    from pit import Bar, bars_from_table

__all__ = ["load_real_main_bars"]


def _checksum_index(path: Path) -> dict[str, str]:
    index: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", maxsplit=1)
        index[relative] = digest
    return index


def load_real_main_bars(
    *,
    artifact_root: Path,
    run_id: str,
    window_start: date,
    window_end: date,
    markets: Iterable[str],
    max_symbols: int,
) -> list[Bar]:
    """Load an explicit bounded selection from main-only real data."""
    assert_range_not_holdout(window_start, window_end)
    if max_symbols < 1:
        raise ValueError("max_symbols must be positive and explicit")
    root = artifact_root.expanduser().resolve() / "runs" / run_id
    assert_path_not_holdout(root)
    checksum_path = root / "checksums.sha256"
    checksums = _checksum_index(checksum_path)
    selected_markets = frozenset(markets)
    files = sorted((root / "dataset").glob("market=*/year=*/ticker=*.parquet"))
    candidates_by_market: dict[str, list[Path]] = {
        market: [] for market in selected_markets
    }
    for path in files:
        relative = path.relative_to(root).as_posix()
        parts = path.parts
        market = next(
            part.split("=", 1)[1] for part in parts if part.startswith("market=")
        )
        year = int(
            next(part.split("=", 1)[1] for part in parts if part.startswith("year="))
        )
        if year < window_start.year or year > window_end.year:
            continue
        symbol = path.stem.split("=", 1)[1]
        if market not in selected_markets:
            continue
        candidates_by_market[market].append(path)

    missing_markets = sorted(
        market for market, paths in candidates_by_market.items() if not paths
    )
    if missing_markets:
        raise ValueError(
            f"requested market has no bounded corpus data: {missing_markets}"
        )

    # Deterministic round-robin selection prevents the first glob partition
    # from exhausting the quota for one market and silently dropping another.
    candidates_by_market = {
        market: sorted(paths, key=lambda path: path.name)
        for market, paths in candidates_by_market.items()
    }
    selected: list[Path] = []
    selected_keys: set[tuple[str, str]] = set()
    cursors = dict.fromkeys(sorted(selected_markets), 0)
    while len(selected) < max_symbols:
        progressed = False
        for market in sorted(selected_markets):
            candidates = candidates_by_market[market]
            while cursors[market] < len(candidates):
                path = candidates[cursors[market]]
                cursors[market] += 1
                symbol = path.stem.split("=", 1)[1]
                key = (market, symbol)
                if key in selected_keys:
                    continue
                relative = path.relative_to(root).as_posix()
                expected = checksums.get(relative)
                if expected is None:
                    raise ValueError(
                        f"selected parquet missing checksum ledger row: {relative}"
                    )
                data = path.read_bytes()
                actual = hashlib.sha256(data).hexdigest()
                if actual != expected:
                    raise ValueError(f"checksum mismatch: {relative}")
                selected.append(path)
                selected_keys.add(key)
                progressed = True
                break
            if len(selected) >= max_symbols:
                break
        if not progressed:
            break
    bars: list[Bar] = []
    for path in selected:
        table = pq.read_table(path)
        bars.extend(
            bar
            for bar in bars_from_table(table)
            if window_start <= bar.session_date <= window_end
        )
    return bars
