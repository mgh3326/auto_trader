#!/usr/bin/env python3
"""Import the KRX after-market (16:00-20:00 KST) eligibility list (#925).

The input is the KRX-published list of after-market eligible issues that the
operator downloaded (CSV/TSV with a 종목코드/단축코드/symbol/code column, or
one code per line; UTF-8 or CP949). The import replaces the whole
``krx_after_market_eligibility`` snapshot. Dry-run by default; pass --commit
only after reviewing the dry-run counts. There is no scheduler.

    uv run python scripts/import_krx_after_market_eligibility.py \\
        --file krx_after_list.csv --source "<KRX notice URL or file name>" \\
        [--asof 2026-09-29T16:00:00+09:00] [--commit]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import io
from pathlib import Path

from app.core.cli import setup_logging_and_sentry

_KST = dt.timezone(dt.timedelta(hours=9))
_CODE_HEADERS = ("종목코드", "단축코드", "symbol", "code")


def parse_krx_after_list_text(text: str) -> list[str]:
    """Extract raw issue codes from the list file text (no normalization)."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    delimiter = "\t" if "\t" in lines[0] else ","
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
    header = [cell.strip().lstrip("﻿").lower() for cell in rows[0]]
    for name in _CODE_HEADERS:
        if name.lower() in header:
            index = header.index(name.lower())
            codes = []
            for row in rows[1:]:
                if index >= len(row) or not row[index].strip():
                    raise ValueError(f"row without a code column value: {row!r}")
                codes.append(row[index].strip())
            return codes
    if all(len(row) == 1 for row in rows):
        return [row[0].strip().lstrip("﻿") for row in rows]
    raise ValueError(
        "no code column found; expected one of "
        f"{', '.join(_CODE_HEADERS)} or one code per line"
    )


def _read_text(path: Path) -> str:
    payload = path.read_bytes()
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return payload.decode("cp949")


def _parse_asof(value: str | None) -> dt.datetime:
    if value is None:
        return dt.datetime.now(_KST)
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid --asof: {value!r}") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--asof must carry a UTC offset")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replace the KRX after-market eligibility list (dry-run by default)."
    )
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument(
        "--source",
        required=True,
        help="Citation for the list (KRX notice URL or downloaded file name).",
    )
    parser.add_argument(
        "--asof",
        type=_parse_asof,
        default=None,
        help="ISO timestamp with offset the list is current as of (default: now).",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Persist the replacement. Default is a dry-run rollback.",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    from app.core.db import AsyncSessionLocal
    from app.services.kr_symbol_universe_service import (
        replace_krx_after_market_list,
    )

    codes = parse_krx_after_list_text(_read_text(args.file))
    asof = args.asof if args.asof is not None else _parse_asof(None)
    async with AsyncSessionLocal() as session:
        result = await replace_krx_after_market_list(
            session, symbols=codes, list_asof=asof, list_source=args.source
        )
        if args.commit:
            await session.commit()
        else:
            await session.rollback()
    print(
        f"KRX after-market list (dry_run={not args.commit}): "
        f"listed={result.listed} previous_rows={result.previous_rows} "
        f"unknown={len(result.unknown_symbols)} "
        f"listed_non_stock={len(result.listed_non_stock)} "
        f"asof={result.list_asof.isoformat()} source={result.list_source}"
    )
    if result.unknown_symbols:
        print(f"  not in active universe: {', '.join(result.unknown_symbols[:20])}")
    if result.listed_non_stock:
        print(
            "  listed but not STOCK (read as not tradable): "
            f"{', '.join(result.listed_non_stock[:20])}"
        )
    print("committed." if args.commit else "--dry-run: no rows written.")
    return 0


async def main() -> int:
    setup_logging_and_sentry(service_name="import-krx-after-market-eligibility")
    return await run(parse_args())


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
