#!/usr/bin/env python3
"""Read-only Toss parity diagnostic for /invest screener consecutive_gainers.

Consumes an operator-provided Toss symbol export/list and compares it with the
local invest_screener_snapshots read model. This script never fetches Toss live
and never writes to the database.

Example:
    uv run python -m scripts.diagnose_invest_screener_toss_parity \
      --market kr \
      --preset consecutive_gainers \
      --toss-symbols-file /path/to/toss_symbols.csv \
      --limit 80
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_SUPPORTED_PRESETS = {"consecutive_gainers"}
_SENSITIVE_HEADER_RE = re.compile(
    r"(cookie|authorization|x[-_]?csrf|token|secret|password|session)", re.I
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(bearer\s+[A-Za-z0-9._~+/-]+|cookie\s*:|authorization\s*:|token=|secret=|password=|session=)",
    re.I,
)
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9:._-]{1,24}$")
_NON_COMMON_STOCK_NOTE = (
    "ETF/MMF/TDF/preferred-stock exclusion is not applied in this P0 diagnostic "
    "because the current snapshot/universe read model has no reliable instrument_type."
)


def _strip_exchange_prefix(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    return text.split(":", maxsplit=1)[-1].strip()


def _redact_sensitive(value: Any) -> str:
    text = str(value or "")
    return _SENSITIVE_VALUE_RE.sub("[REDACTED]", text)


def _reject_if_sensitive(label: str, value: Any) -> None:
    text = str(value or "")
    if _SENSITIVE_HEADER_RE.search(label) or _SENSITIVE_VALUE_RE.search(text):
        raise ValueError(
            "Toss export must not contain cookies, headers, tokens, or secrets; "
            "remove sensitive fields and retry."
        )


def _normalize_toss_row(raw: dict[str, Any], fallback_rank: int) -> dict[str, Any] | None:
    for key, value in raw.items():
        _reject_if_sensitive(key, value)

    symbol = _strip_exchange_prefix(
        raw.get("symbol")
        or raw.get("code")
        or raw.get("ticker")
        or raw.get("종목코드")
        or raw.get("티커")
    )
    if not symbol:
        return None
    if not _SYMBOL_RE.match(symbol):
        raise ValueError(f"Invalid symbol in Toss export: {_redact_sensitive(symbol)}")

    rank_raw = raw.get("rank") or raw.get("순위") or fallback_rank
    try:
        rank = int(rank_raw)
    except (TypeError, ValueError):
        rank = fallback_rank

    return {
        "rank": rank,
        "symbol": symbol,
        "name": str(raw.get("name") or raw.get("종목명") or "").strip() or None,
        "week_change_rate": _to_float_or_none(
            raw.get("week_change_rate") or raw.get("C_주가등락률_1W")
        ),
        "consecutive_up_days": _to_int_or_none(
            raw.get("consecutive_up_days") or raw.get("주가_연속_상승")
        ),
    }


def _to_float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).strip().replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


def _to_int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).strip().replace(",", "")))
    except (TypeError, ValueError):
        return None


def load_toss_symbols(path: Path) -> list[dict[str, Any]]:
    """Load Toss rows from CSV/JSON without accepting any auth material."""
    raw_text = path.read_text(encoding="utf-8-sig")
    _reject_if_sensitive("file", raw_text)

    if path.suffix.lower() == ".json":
        parsed = json.loads(raw_text)
        if isinstance(parsed, dict):
            rows = parsed.get("results") or parsed.get("rows") or parsed.get("symbols") or []
        else:
            rows = parsed
        if not isinstance(rows, list):
            raise ValueError("JSON Toss export must be a list or contain rows/results/symbols")
        raw_rows = [r if isinstance(r, dict) else {"symbol": r} for r in rows]
    else:
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        first_line = lines[0] if lines else ""
        # Plain one-symbol-per-line exports are common when an operator copies a
        # Toss rank list manually. Treat files without delimiters as symbol lists
        # instead of letting DictReader misinterpret the first symbol as a header.
        if first_line and "," not in first_line and "\t" not in first_line:
            raw_rows = [{"symbol": line} for line in lines]
        else:
            reader = csv.DictReader(raw_text.splitlines())
            if not reader.fieldnames:
                raw_rows = []
            else:
                for field in reader.fieldnames:
                    _reject_if_sensitive(field, "")
                raw_rows = list(reader)

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, row in enumerate(raw_rows, start=1):
        item = _normalize_toss_row(row, idx)
        if item is None or item["symbol"] in seen:
            continue
        seen.add(item["symbol"])
        normalized.append(item)
    return normalized


def build_parity_report(
    auto_rows: list[dict[str, Any]], toss_rows: list[dict[str, Any]], *, limit: int
) -> dict[str, Any]:
    auto_rank = {row["symbol"]: idx for idx, row in enumerate(auto_rows, start=1)}
    toss_rank = {row["symbol"]: int(row.get("rank") or idx) for idx, row in enumerate(toss_rows, start=1)}
    auto_symbols = set(auto_rank)
    toss_symbols = set(toss_rank)

    missing = sorted(toss_symbols - auto_symbols, key=lambda symbol: toss_rank[symbol])
    extra = sorted(auto_symbols - toss_symbols, key=lambda symbol: auto_rank[symbol])
    overlap = sorted(auto_symbols & toss_symbols, key=lambda symbol: toss_rank[symbol])

    auto_by_symbol = {row["symbol"]: row for row in auto_rows}
    rank_deltas = [
        {
            "symbol": symbol,
            "tossRank": toss_rank[symbol],
            "autoTraderRank": auto_rank[symbol],
            "delta": auto_rank[symbol] - toss_rank[symbol],
            "autoTraderMetrics": _metrics_for(auto_by_symbol.get(symbol, {})),
        }
        for symbol in overlap
        if auto_rank[symbol] != toss_rank[symbol]
    ]
    rank_deltas.sort(key=lambda item: abs(int(item["delta"])), reverse=True)

    return {
        "preset": "consecutive_gainers",
        "limit": limit,
        "autoTraderCount": len(auto_rows),
        "tossCount": len(toss_rows),
        "overlapCount": len(overlap),
        "missingFromAutoTrader": [
            {"symbol": symbol, "tossRank": toss_rank[symbol]} for symbol in missing
        ],
        "extraInAutoTrader": [
            {
                "symbol": symbol,
                "autoTraderRank": auto_rank[symbol],
                "autoTraderMetrics": _metrics_for(auto_by_symbol.get(symbol, {})),
            }
            for symbol in extra
        ],
        "topRankDeltas": rank_deltas[:20],
        "notes": [_NON_COMMON_STOCK_NOTE],
    }


def _metrics_for(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "week_change_rate": row.get("week_change_rate"),
        "consecutive_up_days": row.get("consecutive_up_days"),
        "change_rate": row.get("change_rate"),
        "snapshot_date": row.get("snapshot_date"),
        "_screener_snapshot_state": row.get("_screener_snapshot_state"),
    }


async def load_auto_trader_rows(
    session: AsyncSession, *, market: str, limit: int
) -> list[dict[str, Any]]:
    """Load qualifying rows using the same single-partition semantics as the API.

    Mirrors _load_consecutive_gainers_from_snapshots: resolves the latest
    snapshot_date first, then qualifies only within that partition. This prevents
    stale historical rows from appearing in parity comparisons when the latest
    partition has no qualifiers.
    """
    from app.models.invest_screener_snapshot import InvestScreenerSnapshot
    from app.services.invest_screener_snapshots.freshness import (
        classify_state,
        today_trading_date,
    )

    today = today_trading_date(market)
    now = datetime.now(UTC)

    # Resolve the latest snapshot partition (mirrors production serving semantics).
    latest_date_stmt = sa.select(
        sa.func.max(InvestScreenerSnapshot.snapshot_date)
    ).where(InvestScreenerSnapshot.market == market)
    latest_date_result = await session.execute(latest_date_stmt)
    latest_snapshot_date = latest_date_result.scalar_one_or_none()
    if latest_snapshot_date is None:
        return []

    stmt = (
        sa.select(InvestScreenerSnapshot)
        .where(
            InvestScreenerSnapshot.market == market,
            InvestScreenerSnapshot.snapshot_date == latest_snapshot_date,
            InvestScreenerSnapshot.consecutive_up_days >= 5,
            InvestScreenerSnapshot.week_change_rate >= 0,
        )
        .order_by(
            InvestScreenerSnapshot.week_change_rate.desc().nullslast(),
            InvestScreenerSnapshot.consecutive_up_days.desc(),
            InvestScreenerSnapshot.change_rate.desc().nullslast(),
            InvestScreenerSnapshot.symbol.asc(),
        )
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for snap in result.scalars().all():
        if snap.symbol in seen:
            continue
        seen.add(snap.symbol)
        state = classify_state(
            snapshot_date=snap.snapshot_date,
            computed_at=snap.computed_at,
            closes_window_len=len(snap.closes_window or []),
            today_trading_date_value=today,
            now=now,
        )
        rows.append(
            {
                "symbol": snap.symbol,
                "week_change_rate": float(snap.week_change_rate)
                if snap.week_change_rate is not None
                else None,
                "consecutive_up_days": snap.consecutive_up_days,
                "change_rate": float(snap.change_rate)
                if snap.change_rate is not None
                else None,
                "snapshot_date": snap.snapshot_date.isoformat(),
                "_screener_snapshot_state": state,
            }
        )
    # Rows already ordered by SQL ORDER BY; no Python re-sort needed.
    return rows


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only Toss parity diagnostic for /invest screener."
    )
    parser.add_argument("--market", choices=["kr"], default="kr")
    parser.add_argument("--preset", choices=sorted(_SUPPORTED_PRESETS), default="consecutive_gainers")
    parser.add_argument("--toss-symbols-file", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=80)
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.limit <= 0 or args.limit > 500:
        raise ValueError("--limit must be between 1 and 500")

    from app.core.cli import setup_logging_and_sentry
    from app.core.db import AsyncSessionLocal

    setup_logging_and_sentry(service_name="diagnose-invest-screener-toss-parity")

    try:
        toss_rows = load_toss_symbols(args.toss_symbols_file)
        async with AsyncSessionLocal() as session:
            auto_rows = await load_auto_trader_rows(
                session, market=args.market, limit=args.limit
            )
        report = build_parity_report(auto_rows, toss_rows, limit=args.limit)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("diagnose_invest_screener_toss_parity crashed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
