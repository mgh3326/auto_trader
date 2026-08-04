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
    selected: list[Path] = []
    symbols: set[str] = set()
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
        if (
            market not in selected_markets
            or symbol not in symbols
            and len(symbols) >= max_symbols
        ):
            continue
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
        symbols.add(symbol)
    bars: list[Bar] = []
    for path in selected:
        table = pq.read_table(path)
        bars.extend(
            bar
            for bar in bars_from_table(table)
            if window_start <= bar.session_date <= window_end
        )
    return bars
