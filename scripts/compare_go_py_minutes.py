#!/usr/bin/env python3
"""hk 6345: READ-ONLY Go(JSONL) ↔ research 분봉 표 느슨한 전환 비교.

    uv run python -m scripts.compare_go_py_minutes --market kr|us --date YYYY-MM-DD \\
        --go-jsonl PATH [--symbols 005930 000660]

``--go-jsonl`` is the one-row-per-line file ``market-tap`` writes (fillwire
``internal/marketminute`` byte contract). Only rows whose ``session_date`` is
``--date`` and whose venue belongs to ``--market`` are compared. The Python side is
``research.kr_candles_1m`` (``session_date_kst``) or ``research.us_candles_1m``
(``session_date``), read in one ``READ ONLY`` transaction with an 8 s statement
timeout. SELECT only: writes nothing, registers nothing, calls no broker.

Exit 0 = PASS (nothing blocking; listed items do not change the exit code),
3 = BLOCK (a whole session or symbol is missing on one side),
1 = the Python read failed, 2 = usage or an unreadable Go file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path

from sqlalchemy import text

from app.core.cli import setup_logging_and_sentry
from app.core.db import engine
from app.core.symbol import to_db_symbol
from app.services.research_candles import go_py_compare as cmp

EXIT_PASS = 0
EXIT_BLOCK = 3
EXIT_READ_FAILED = 1
EXIT_USAGE = 2

_SELECT_KR = text(
    "SELECT time_utc, session_date_kst AS session_date, symbol, venue, "
    "session_segment, source, open, high, low, close, volume, value "
    "FROM research.kr_candles_1m "
    "WHERE session_date_kst = :session_date "
    "AND (CAST(:symbols AS text[]) IS NULL OR symbol = ANY(CAST(:symbols AS text[])))"
)
_SELECT_US = text(
    "SELECT time_utc, session_date, symbol, venue, session_segment, source, "
    "open, high, low, close, volume, value "
    "FROM research.us_candles_1m "
    "WHERE session_date = :session_date "
    "AND (CAST(:symbols AS text[]) IS NULL OR symbol = ANY(CAST(:symbols AS text[])))"
)
_STATEMENTS = {"kr": _SELECT_KR, "us": _SELECT_US}
_MARKET_VENUES = {"kr": cmp.KR_VENUES, "us": cmp.US_VENUES}


def _normalize_symbol(market: str, raw: str) -> str | None:
    """CLI 입력 심볼을 DB 형식으로. 변환은 app.core.symbol 만 쓴다(AGENTS 하드룰 8)."""
    value = raw.strip().upper()
    if not value:
        return None
    if market == "kr":
        return value.zfill(6) if value.isdigit() else value
    return to_db_symbol(value)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--market", required=True, choices=("kr", "us"))
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--go-jsonl", required=True, type=Path, metavar="PATH")
    parser.add_argument("--symbols", nargs="+", default=None)
    return parser.parse_args(argv)


def _load_go(
    path: Path, market: str, session: date
) -> tuple[list[cmp.MinuteBar], dict]:
    """Go JSONL 을 읽어 (거래일·시장에 맞는 행, 통계). 형식 오류는 ValueError."""
    venues = _MARKET_VENUES[market]
    bars: list[cmp.MinuteBar] = []
    stats = {"go_lines": 0, "go_rows_compared": 0, "go_rows_other_scope": 0}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        stats["go_lines"] += 1
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {number}: not JSON") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"line {number}: not a JSON object")
        try:
            bar = cmp.parse_go_row(raw)
        except cmp.CompareInputError as exc:
            raise ValueError(f"line {number}: {exc}") from exc
        if bar.session_date != session or bar.venue not in venues:
            stats["go_rows_other_scope"] += 1
            continue
        bars.append(bar)
    stats["go_rows_compared"] = len(bars)
    return bars, stats


async def _read_python(
    market: str, session: date, symbols: list[str] | None
) -> list[cmp.MinuteBar]:
    async with engine.connect() as conn:
        async with conn.begin():
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            await conn.execute(text("SET LOCAL statement_timeout = '8s'"))
            result = await conn.execute(
                _STATEMENTS[market],
                {"session_date": session, "symbols": symbols},
            )
            rows = [dict(row._mapping) for row in result]
    return [cmp.parse_python_row(row) for row in rows]


def build_report(
    market: str,
    session: date,
    go: Sequence[cmp.MinuteBar],
    py: Sequence[cmp.MinuteBar],
    stats: dict,
) -> dict[str, object]:
    report = cmp.compare_minutes(go, py)
    sessions = [asdict(s) for s in report.sessions]
    return {
        "status": "ok",
        "market": market,
        "session_date": session.isoformat(),
        "verdict": report.verdict,
        "input": {**stats, "py_rows": len(py)},
        "blocking": {
            "missing_symbols_on_go": list(report.missing_symbols_on_go),
            "missing_symbols_on_py": list(report.missing_symbols_on_py),
            "missing_sessions_on_go": [
                list(map(str, s)) for s in report.missing_sessions_on_go
            ],
            "missing_sessions_on_py": [
                list(map(str, s)) for s in report.missing_sessions_on_py
            ],
        },
        "listed": {
            "counts_outside_tolerance": [
                list(map(str, s)) for s in report.counts_outside_tolerance
            ],
            "matched_bars": report.matched_bars,
            "ohlcv_equal": report.ohlcv_equal,
            "ohlcv_mismatch": report.ohlcv_mismatch,
            "ohlcv_mismatch_keys": list(report.ohlcv_mismatch_keys),
            "value_equal": report.value_equal,
            "value_differs": report.value_differs,
            "value_one_side_null": report.value_one_side_null,
            "segment_mismatch": report.segment_mismatch,
            "source_mismatch": report.source_mismatch,
            "rows_missing_on_go": report.rows_missing_on_go,
            "rows_missing_on_py": report.rows_missing_on_py,
            "partial_gap_keys_on_go": list(report.partial_gap_keys_on_go),
            "partial_gap_keys_on_py": list(report.partial_gap_keys_on_py),
            "duplicate_go_keys": report.duplicate_go_keys,
            "duplicate_py_keys": report.duplicate_py_keys,
            "latency": dict(report.latency),
        },
        "sessions": sessions,
        "cutover": [asdict(row) for row in cmp.cutover_table(report)],
        "failure_code_map": {
            code: {"hk6299": mapped, "note": note}
            for code, (mapped, note) in cmp.FAILURE_CODE_MAP.items()
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging_and_sentry(service_name="compare-go-py-minutes")

    try:
        go, stats = _load_go(args.go_jsonl, args.market, args.date)
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "invalid_go_jsonl", "error_class": type(exc).__name__}
            )
        )
        return EXIT_USAGE

    symbols: list[str] | None = None
    if args.symbols:
        parts = (p for chunk in args.symbols for p in chunk.split(","))
        symbols = sorted(
            {s for s in (_normalize_symbol(args.market, p) for p in parts) if s}
        )

    if symbols is not None:
        wanted = set(symbols)
        go = [bar for bar in go if bar.symbol in wanted]

    async def _go() -> int:
        try:
            py = await _read_python(args.market, args.date, symbols)
        except Exception as exc:  # noqa: BLE001 - a failed read must be visible
            print(
                json.dumps({"status": "unavailable", "error_class": type(exc).__name__})
            )
            return EXIT_READ_FAILED
        finally:
            await engine.dispose()
        report = build_report(args.market, args.date, go, py, stats)
        print(json.dumps(report, sort_keys=True, default=str))
        return EXIT_BLOCK if report["verdict"] == cmp.BLOCK else EXIT_PASS

    return asyncio.run(_go())


if __name__ == "__main__":
    sys.exit(main())
