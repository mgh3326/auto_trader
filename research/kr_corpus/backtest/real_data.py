"""Read-only bounded loader for the finalized KR main snapshot.

The collector's real manifest is a summary object; per-file integrity lives in
``checksums.sha256``.  This loader verifies selected bytes against that ledger
before parsing and refuses holdout paths/dates.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

try:
    from .holdout_guard import assert_path_not_holdout, assert_range_not_holdout
except ImportError:  # pragma: no cover - legacy flat-module test entrypoint
    from holdout_guard import assert_path_not_holdout, assert_range_not_holdout
try:
    from .pit import Bar, bars_from_table
except ImportError:  # pragma: no cover - legacy flat-module test entrypoint
    from pit import Bar, bars_from_table

__all__ = [
    "RealMainStageBInput",
    "load_real_main_bars",
    "load_real_main_stage_b_input",
]


@dataclass(frozen=True)
class RealMainStageBInput:
    """Verified main-corpus input plus the evidence needed to judge it."""

    bars: tuple[Bar, ...]
    market_sessions: Mapping[str, tuple[date, ...]]
    coverage: Mapping[str, Any]


def _checksum_index(path: Path) -> dict[str, str]:
    index: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", maxsplit=1)
        index[relative] = digest
    return index


def _checked_bytes(
    *,
    root: Path,
    path: Path,
    checksums: Mapping[str, str],
    kind: str,
) -> bytes:
    relative = path.relative_to(root).as_posix()
    expected = checksums.get(relative)
    if expected is None:
        raise ValueError(f"{kind} missing checksum ledger row: {relative}")
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"checksum mismatch: {relative}")
    return data


def _load_market_sessions(
    *,
    root: Path,
    checksums: Mapping[str, str],
    window_start: date,
    window_end: date,
    markets: Iterable[str],
) -> dict[str, tuple[date, ...]]:
    """Load the signed XKRX sequence without deriving sessions from one symbol."""
    preflight_bytes = _checked_bytes(
        root=root,
        path=root / "preflight.json",
        checksums=checksums,
        kind="market-session reference",
    )
    try:
        payload = json.loads(preflight_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError("market-session reference is not valid JSON") from exc
    if payload.get("session_calendar") != "XKRX":
        raise ValueError("market-session reference must name the XKRX calendar")
    raw_sessions = payload.get("sessions")
    if not isinstance(raw_sessions, list) or not all(
        isinstance(value, str) for value in raw_sessions
    ):
        raise ValueError("market-session reference has no string session sequence")

    start = window_start.isoformat()
    end = window_end.isoformat()
    selected_raw_sessions = tuple(
        value for value in raw_sessions if start <= value <= end
    )
    try:
        sessions = tuple(date.fromisoformat(value) for value in selected_raw_sessions)
    except ValueError as exc:
        raise ValueError(
            "market-session reference contains an invalid session"
        ) from exc
    if not sessions:
        raise ValueError("market-session reference has no sessions in run window")
    if tuple(sorted(sessions)) != sessions or len(set(sessions)) != len(sessions):
        raise ValueError(
            "market-session reference must be strictly ascending and unique"
        )
    return dict.fromkeys(sorted(set(markets)), sessions)


def load_real_main_bars(
    *,
    artifact_root: Path,
    run_id: str,
    window_start: date,
    window_end: date,
    markets: Iterable[str],
    max_symbols: int,
) -> list[Bar]:
    """Load only the bars from the verified Stage-B main-corpus input."""
    return list(
        load_real_main_stage_b_input(
            artifact_root=artifact_root,
            run_id=run_id,
            window_start=window_start,
            window_end=window_end,
            markets=markets,
            max_symbols=max_symbols,
        ).bars
    )


def load_real_main_stage_b_input(
    *,
    artifact_root: Path,
    run_id: str,
    window_start: date,
    window_end: date,
    markets: Iterable[str],
    max_symbols: int,
) -> RealMainStageBInput:
    """Select symbols once, then load every in-window yearly partition for each."""
    assert_range_not_holdout(window_start, window_end)
    if max_symbols < 1:
        raise ValueError("max_symbols must be positive and explicit")
    root = artifact_root.expanduser().resolve() / "runs" / run_id
    assert_path_not_holdout(root)
    checksum_path = root / "checksums.sha256"
    checksums = _checksum_index(checksum_path)
    selected_markets = frozenset(markets)
    if not selected_markets:
        raise ValueError("at least one market must be explicit")
    files = sorted((root / "dataset").glob("market=*/year=*/ticker=*.parquet"))
    symbols_by_market: dict[str, set[str]] = {
        market: set() for market in selected_markets
    }
    partitions_by_key: dict[tuple[str, str], list[tuple[int, Path]]] = {}
    for path in files:
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
        key = (market, symbol)
        symbols_by_market[market].add(symbol)
        partitions_by_key.setdefault(key, []).append((year, path))

    missing_markets = sorted(
        market for market, symbols in symbols_by_market.items() if not symbols
    )
    if missing_markets:
        raise ValueError(
            f"requested market has no bounded corpus data: {missing_markets}"
        )

    # Select identities, rather than parquet files: every selected identity then
    # loads its complete in-window partition set.
    candidates_by_market = {
        market: sorted(symbols) for market, symbols in symbols_by_market.items()
    }
    selected_keys: list[tuple[str, str]] = []
    cursors = dict.fromkeys(sorted(selected_markets), 0)
    while len(selected_keys) < max_symbols:
        progressed = False
        for market in sorted(selected_markets):
            candidates = candidates_by_market[market]
            while cursors[market] < len(candidates):
                symbol = candidates[cursors[market]]
                cursors[market] += 1
                selected_keys.append((market, symbol))
                progressed = True
                break
            if len(selected_keys) >= max_symbols:
                break
        if not progressed:
            break

    market_sessions = _load_market_sessions(
        root=root,
        checksums=checksums,
        window_start=window_start,
        window_end=window_end,
        markets=selected_markets,
    )
    bars: list[Bar] = []
    symbol_coverage: dict[tuple[str, str], dict[str, Any]] = {
        key: {"bar_count": 0, "years": set()} for key in selected_keys
    }
    for market, symbol in selected_keys:
        partitions = sorted(
            partitions_by_key[(market, symbol)],
            key=lambda item: (item[0], item[1].name),
        )
        for _, path in partitions:
            assert_path_not_holdout(path)
            _checked_bytes(
                root=root,
                path=path,
                checksums=checksums,
                kind="selected parquet",
            )
            table = pq.read_table(path)
            partition_bars = [
                bar
                for bar in bars_from_table(table)
                if window_start <= bar.session_date <= window_end
            ]
            if any(
                bar.market != market or bar.symbol != symbol for bar in partition_bars
            ):
                raise ValueError(
                    f"parquet partition identity mismatch: {path.relative_to(root)}"
                )
            bars.extend(partition_bars)
            coverage = symbol_coverage[(market, symbol)]
            coverage["bar_count"] += len(partition_bars)
            coverage["years"].update(bar.session_date.year for bar in partition_bars)

    market_coverage: dict[str, dict[str, Any]] = {}
    for market in sorted(selected_markets):
        selected_symbols = sorted(
            symbol
            for selected_market, symbol in selected_keys
            if selected_market == market
        )
        symbols: dict[str, dict[str, Any]] = {}
        for symbol in selected_symbols:
            coverage = symbol_coverage[(market, symbol)]
            years = sorted(coverage["years"])
            symbols[symbol] = {
                "bar_count": coverage["bar_count"],
                "year_count": len(years),
                "years": years,
            }
        years = sorted(
            year for coverage in symbols.values() for year in coverage["years"]
        )
        unique_years = sorted(set(years))
        market_coverage[market] = {
            "symbol_count": len(selected_symbols),
            "bar_count": sum(coverage["bar_count"] for coverage in symbols.values()),
            "year_count": len(unique_years),
            "years": unique_years,
            "symbols": symbols,
        }
    coverage: dict[str, Any] = {
        "session_reference": {
            "source": "preflight.json",
            "calendar": "XKRX",
            "markets": {
                market: {
                    "session_count": len(sessions),
                    "first_session": sessions[0].isoformat(),
                    "last_session": sessions[-1].isoformat(),
                }
                for market, sessions in sorted(market_sessions.items())
            },
        },
        "markets": market_coverage,
    }
    return RealMainStageBInput(
        bars=tuple(bars),
        market_sessions=market_sessions,
        coverage=coverage,
    )
