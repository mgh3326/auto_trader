#!/usr/bin/env python3
"""hk 6345: READ-ONLY Go(JSONL) ↔ research 분봉 표 느슨한 전환 비교.

    uv run python -m scripts.compare_go_py_minutes --market kr|us --date YYYY-MM-DD \\
        --go-jsonl PATH [--symbols 005930 000660]

``--go-jsonl`` is the one-row-per-line file ``market-tap`` writes (fillwire
``internal/marketminute`` byte contract). Only rows whose ``session_date`` is
``--date``, whose venue belongs to ``--market`` and (with ``--symbols``) whose symbol
was requested are compared.

The Python side is the research table's rows written by the session collector
(``batch_id`` starting ``session-minutes-``). The same table also receives Go ingest
rows (hk 6343) and promotion/backfill rows; those are excluded and counted, so Go's
own rows can never stand in for Python coverage. The batch-id rule is a convention:
the ingest accepts a producer-supplied ``batch_id``, so a producer that chose a
``session-minutes-`` id would still pass.

Reads run in one ``READ ONLY`` transaction with an 8 s statement timeout. SELECT only:
writes nothing, registers nothing, calls no broker.

Exit 0 = PASS (nothing blocking; listed items do not change the exit code),
3 = BLOCK (a whole session or symbol is missing on one side),
4 = INCONCLUSIVE (no comparable evidence: an empty side, no overlapping bars, or a
requested symbol with no rows on either side; never read as coverage),
1 = the Python read failed, 2 = usage or an unreadable Go file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
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
EXIT_INCONCLUSIVE = 4

# 세션 수집기가 쓰는 batch_id 접두어. `session_minutes_collector.BATCH_PREFIX` 와 같아야 하며
# 테스트가 값을 고정한다. 이 스크립트는 수집기 모듈을 import 하지 않는다(브로커 패키지 적재 방지).
PY_COLLECTOR_BATCH_PREFIX = "session-minutes"

_SELECT_KR = text(
    "SELECT time_utc, session_date_kst AS session_date, symbol, venue, "
    "session_segment, source, open, high, low, close, volume, value, batch_id "
    "FROM research.kr_candles_1m "
    "WHERE session_date_kst = :session_date "
    "AND (CAST(:symbols AS text[]) IS NULL OR symbol = ANY(CAST(:symbols AS text[])))"
)
_SELECT_US = text(
    "SELECT time_utc, session_date, symbol, venue, session_segment, source, "
    "open, high, low, close, volume, value, batch_id "
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


def _requested_symbols(market: str, parts: Sequence[str]) -> list[str]:
    tokens = (p for chunk in parts for p in chunk.split(","))
    return sorted({s for s in (_normalize_symbol(market, t) for t in tokens) if s})


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--market", required=True, choices=("kr", "us"))
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--go-jsonl", required=True, type=Path, metavar="PATH")
    parser.add_argument("--symbols", nargs="+", default=None)
    return parser.parse_args(argv)


def _load_go(
    path: Path,
    market: str,
    session: date,
    symbols: Sequence[str] | None,
) -> tuple[list[cmp.MinuteBar], dict[str, int]]:
    """Go JSONL 을 읽어 (비교 대상 행, 통계). 형식 오류는 ValueError.

    ``symbols`` 가 주어지면 그 종목 행만 남기고 나머지는 ``go_rows_other_symbol`` 로 센다.
    """
    venues = _MARKET_VENUES[market]
    wanted = set(symbols) if symbols is not None else None
    bars: list[cmp.MinuteBar] = []
    stats = {
        "go_lines": 0,
        "go_rows_compared": 0,
        "go_rows_other_scope": 0,
        "go_rows_other_symbol": 0,
    }
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
        if wanted is not None and bar.symbol not in wanted:
            stats["go_rows_other_symbol"] += 1
            continue
        bars.append(bar)
    stats["go_rows_compared"] = len(bars)
    return bars, stats


async def _read_python(
    market: str, session: date, symbols: list[str] | None
) -> tuple[list[cmp.MinuteBar], int]:
    """(세션 수집기 행, 제외된 다른 쓰기자의 행 수). SELECT 만, READ ONLY 트랜잭션."""
    async with engine.connect() as conn:
        async with conn.begin():
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            await conn.execute(text("SET LOCAL statement_timeout = '8s'"))
            result = await conn.execute(
                _STATEMENTS[market],
                {"session_date": session, "symbols": symbols},
            )
            rows = [dict(row._mapping) for row in result]
    collector_rows, excluded = split_collector_rows(rows)
    return [cmp.parse_python_row(row) for row in collector_rows], excluded


def split_collector_rows(
    rows: Sequence[Mapping[str, object]],
) -> tuple[list[Mapping[str, object]], int]:
    """세션 수집기 행만 남기고 (남은 행, 제외 행 수)를 돌려준다. 순수 함수."""
    prefix = PY_COLLECTOR_BATCH_PREFIX + "-"
    collector = [
        row for row in rows if str(row.get("batch_id") or "").startswith(prefix)
    ]
    return collector, len(rows) - len(collector)


def build_report(
    market: str,
    session: date,
    go: Sequence[cmp.MinuteBar],
    py: Sequence[cmp.MinuteBar],
    stats: dict[str, int],
    *,
    requested_symbols: Sequence[str] | None = None,
) -> dict[str, object]:
    report = cmp.compare_minutes(go, py, requested_symbols=requested_symbols)
    sessions = [asdict(s) for s in report.sessions]
    return {
        "status": "ok",
        "market": market,
        "session_date": session.isoformat(),
        "verdict": report.verdict,
        "inconclusive_reasons": list(report.inconclusive_reasons),
        "requested_symbols_absent_on_both": list(
            report.requested_symbols_absent_on_both
        ),
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


_VERDICT_EXIT = {
    cmp.PASS: EXIT_PASS,
    cmp.BLOCK: EXIT_BLOCK,
    cmp.INCONCLUSIVE: EXIT_INCONCLUSIVE,
}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging_and_sentry(service_name="compare-go-py-minutes")

    symbols: list[str] | None = None
    if args.symbols is not None:
        symbols = _requested_symbols(args.market, args.symbols)
        if not symbols:
            print(json.dumps({"status": "invalid_symbols"}))
            return EXIT_USAGE

    try:
        go, stats = _load_go(args.go_jsonl, args.market, args.date, symbols)
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "invalid_go_jsonl", "error_class": type(exc).__name__}
            )
        )
        return EXIT_USAGE

    async def _go() -> int:
        try:
            py, excluded = await _read_python(args.market, args.date, symbols)
        except Exception as exc:  # noqa: BLE001 - a failed read must be visible
            print(
                json.dumps({"status": "unavailable", "error_class": type(exc).__name__})
            )
            return EXIT_READ_FAILED
        finally:
            await engine.dispose()
        stats["py_rows_excluded_non_collector"] = excluded
        report = build_report(
            args.market,
            args.date,
            go,
            py,
            stats,
            requested_symbols=symbols,
        )
        print(json.dumps(report, sort_keys=True, default=str))
        return _VERDICT_EXIT[str(report["verdict"])]

    return asyncio.run(_go())


if __name__ == "__main__":
    sys.exit(main())
