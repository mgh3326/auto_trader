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

_SUPPORTED_PRESETS = {"consecutive_gainers", "double_buy"}
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


def _normalize_toss_row(
    raw: dict[str, Any], fallback_rank: int
) -> dict[str, Any] | None:
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
            rows = (
                parsed.get("results")
                or parsed.get("rows")
                or parsed.get("symbols")
                or []
            )
        else:
            rows = parsed
        if not isinstance(rows, list):
            raise ValueError(
                "JSON Toss export must be a list or contain rows/results/symbols"
            )
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
    toss_rank = {
        row["symbol"]: int(row.get("rank") or idx)
        for idx, row in enumerate(toss_rows, start=1)
    }
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
        expected_baseline_date,
    )

    # ROB-438 follow-up: classify against the session-aware baseline (matches the
    # loaders / classify_state usage), so prior-day partitions in the pre-market
    # window aren't reported stale on the UTC calendar date.
    today = expected_baseline_date(market)
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


async def load_double_buy_rows_interpretation_a(
    session: AsyncSession, *, market: str, limit: int
) -> list[dict[str, Any]]:
    """Toss screenId=18 parity, Interpretation A (absolute net buy > 0).

    Mirrors the production loader at
    app/services/invest_view_model/double_buy_screener.py so diagnostic output
    is guaranteed to track the live serving path.
    """
    from app.services.invest_view_model.double_buy_screener import (
        load_double_buy_from_snapshots,
    )

    rows = await load_double_buy_from_snapshots(session, market=market, limit=limit)
    return rows or []


async def load_double_buy_rows_interpretation_b(
    session: AsyncSession, *, market: str, limit: int
) -> list[dict[str, Any]]:
    """Toss screenId=18 parity, Interpretation B (delta vs previous day > 0).

    Implemented ONLY in the diagnostic for verification purposes. Not wired into
    the production helper because Decision 1 locked the production rule to
    Interpretation A under the safer-fallback policy.
    """
    from app.models.invest_screener_snapshot import InvestScreenerSnapshot
    from app.models.investor_flow_snapshot import InvestorFlowSnapshot
    from app.models.kr_symbol_universe import KRSymbolUniverse
    from app.services.invest_view_model.screener_service import (
        _is_kr_toss_common_stock,
    )

    if market != "kr":
        return []

    latest_flow_stmt = sa.select(sa.func.max(InvestorFlowSnapshot.snapshot_date)).where(
        InvestorFlowSnapshot.market == "kr"
    )
    flow_date = (await session.execute(latest_flow_stmt)).scalar_one_or_none()
    if flow_date is None:
        return []

    prev_flow_stmt = sa.select(sa.func.max(InvestorFlowSnapshot.snapshot_date)).where(
        InvestorFlowSnapshot.market == "kr",
        InvestorFlowSnapshot.snapshot_date < flow_date,
    )
    prev_flow_date = (await session.execute(prev_flow_stmt)).scalar_one_or_none()
    if prev_flow_date is None:
        return []

    latest_price_stmt = sa.select(
        sa.func.max(InvestScreenerSnapshot.snapshot_date)
    ).where(InvestScreenerSnapshot.market == "kr")
    price_date = (await session.execute(latest_price_stmt)).scalar_one_or_none()
    if price_date is None:
        return []

    Prev = sa.orm.aliased(InvestorFlowSnapshot)
    candidate_stmt = (
        sa.select(
            InvestorFlowSnapshot.symbol,
            (InvestorFlowSnapshot.foreign_net - Prev.foreign_net).label(
                "foreign_delta"
            ),
            (InvestorFlowSnapshot.institution_net - Prev.institution_net).label(
                "institution_delta"
            ),
            InvestScreenerSnapshot.change_rate,
            InvestScreenerSnapshot.snapshot_date.label("price_snapshot_date"),
            InvestorFlowSnapshot.snapshot_date.label("flow_snapshot_date"),
        )
        .join(
            Prev,
            sa.and_(
                Prev.market == "kr",
                Prev.symbol == InvestorFlowSnapshot.symbol,
                Prev.snapshot_date == prev_flow_date,
            ),
        )
        .join(
            InvestScreenerSnapshot,
            sa.and_(
                InvestScreenerSnapshot.market == "kr",
                InvestScreenerSnapshot.symbol == InvestorFlowSnapshot.symbol,
                InvestScreenerSnapshot.snapshot_date == price_date,
            ),
        )
        .where(
            InvestorFlowSnapshot.market == "kr",
            InvestorFlowSnapshot.snapshot_date == flow_date,
            (InvestorFlowSnapshot.foreign_net - Prev.foreign_net) > 0,
            (InvestorFlowSnapshot.institution_net - Prev.institution_net) > 0,
            sa.func.coalesce(InvestScreenerSnapshot.change_rate, 0) >= 0,
        )
        .order_by(
            InvestScreenerSnapshot.change_rate.desc().nullslast(),
            InvestorFlowSnapshot.symbol.asc(),
            InvestorFlowSnapshot.source.asc(),
        )
        .limit(max(limit * 4, limit + 40))
    )
    result = await session.execute(candidate_stmt)
    candidate_rows = list(result.mappings().all())

    symbols = [r["symbol"] for r in candidate_rows]
    name_map: dict[str, str] = {}
    if symbols:
        names = await session.execute(
            sa.select(KRSymbolUniverse.symbol, KRSymbolUniverse.name).where(
                KRSymbolUniverse.symbol.in_(symbols),
                KRSymbolUniverse.is_active.is_(True),
            )
        )
        name_map = {row.symbol: row.name for row in names.all()}

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in candidate_rows:
        sym = r["symbol"]
        if sym in seen:
            continue
        if not _is_kr_toss_common_stock(sym, name_map.get(sym)):
            continue
        seen.add(sym)
        rows.append(
            {
                "symbol": sym,
                "foreign_delta": int(r["foreign_delta"])
                if r["foreign_delta"] is not None
                else None,
                "institution_delta": int(r["institution_delta"])
                if r["institution_delta"] is not None
                else None,
                "change_rate": float(r["change_rate"])
                if r["change_rate"] is not None
                else None,
                "price_snapshot_date": r["price_snapshot_date"].isoformat()
                if r["price_snapshot_date"]
                else None,
                "flow_snapshot_date": r["flow_snapshot_date"].isoformat()
                if r["flow_snapshot_date"]
                else None,
            }
        )
        if len(rows) >= limit:
            break
    return rows


def build_double_buy_parity_report(
    *,
    a_rows: list[dict[str, Any]] | None,
    b_rows: list[dict[str, Any]] | None,
    toss_rows: list[dict[str, Any]],
    limit: int,
    interpretation: str,
    current_date: str | None,
    prev_date: str | None,
) -> dict[str, Any]:
    """Compare Interpretation A and B against the Toss reference symbol set.

    Parameters
    ----------
    a_rows / b_rows:
        Loader output for each interpretation. Pass ``None`` for an
        interpretation that was not requested — the corresponding block in the
        report will be ``None`` so the JSON shape remains stable.
    interpretation:
        One of ``"a"``, ``"b"``, ``"both"``. Controls which blocks are
        populated regardless of whether non-empty rows were supplied; this
        keeps consumers' parsing logic predictable.
    """
    toss_symbols = {row["symbol"] for row in toss_rows}
    toss_rank = {
        row["symbol"]: int(row.get("rank") or idx)
        for idx, row in enumerate(toss_rows, start=1)
    }

    def _block(rows: list[dict[str, Any]] | None) -> dict[str, Any] | None:
        if rows is None:
            return None
        auto_symbols = {r["symbol"] for r in rows}
        auto_rank = {r["symbol"]: idx for idx, r in enumerate(rows, start=1)}
        overlap = sorted(auto_symbols & toss_symbols, key=lambda s: toss_rank[s])
        missing = sorted(toss_symbols - auto_symbols, key=lambda s: toss_rank[s])
        extra = sorted(auto_symbols - toss_symbols, key=lambda s: auto_rank[s])
        return {
            "count": len(rows),
            "overlapCount": len(overlap),
            "missingFromAutoTrader": [
                {"symbol": s, "tossRank": toss_rank[s]} for s in missing
            ],
            "extraInAutoTrader": [
                {"symbol": s, "autoTraderRank": auto_rank[s]} for s in extra
            ],
        }

    return {
        "preset": "double_buy",
        "limit": limit,
        "interpretation": interpretation,
        "currentSnapshotDate": current_date,
        "previousSnapshotDate": prev_date,
        "tossCount": len(toss_rows),
        "interpretationA": _block(a_rows) if interpretation in {"a", "both"} else None,
        "interpretationB": _block(b_rows) if interpretation in {"b", "both"} else None,
        "lockedInterpretation": "A",
        "note": (
            "Decision 1 locked to Interpretation A under safer-fallback rule "
            "(see plan Decision 1). Both interpretations always emitted to "
            "support live verification; if B materially outperforms A on a "
            "richer Toss reference set, switch the production helper body."
        ),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only Toss parity diagnostic for /invest screener."
    )
    parser.add_argument("--market", choices=["kr"], default="kr")
    parser.add_argument(
        "--preset", choices=sorted(_SUPPORTED_PRESETS), default="consecutive_gainers"
    )
    parser.add_argument("--toss-symbols-file", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument(
        "--interpretation",
        choices=("a", "b", "both"),
        default="both",
        help=(
            "double_buy preset only. Selects which interpretation(s) to evaluate. "
            "A = absolute net buy > 0 (locked production rule); "
            "B = delta vs previous trading day > 0 (diagnostic only)."
        ),
    )
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
        if args.preset == "consecutive_gainers":
            async with AsyncSessionLocal() as session:
                auto_rows = await load_auto_trader_rows(
                    session, market=args.market, limit=args.limit
                )
            report = build_parity_report(auto_rows, toss_rows, limit=args.limit)
        elif args.preset == "double_buy":
            from app.models.investor_flow_snapshot import InvestorFlowSnapshot

            async with AsyncSessionLocal() as session:
                # Resolve current/previous flow snapshot dates for report context.
                current_date_value = (
                    await session.execute(
                        sa.select(
                            sa.func.max(InvestorFlowSnapshot.snapshot_date)
                        ).where(InvestorFlowSnapshot.market == args.market)
                    )
                ).scalar_one_or_none()
                prev_date_value = None
                if current_date_value is not None:
                    prev_date_value = (
                        await session.execute(
                            sa.select(
                                sa.func.max(InvestorFlowSnapshot.snapshot_date)
                            ).where(
                                InvestorFlowSnapshot.market == args.market,
                                InvestorFlowSnapshot.snapshot_date < current_date_value,
                            )
                        )
                    ).scalar_one_or_none()

                a_rows: list[dict[str, Any]] | None = None
                b_rows: list[dict[str, Any]] | None = None
                if args.interpretation in {"a", "both"}:
                    a_rows = await load_double_buy_rows_interpretation_a(
                        session, market=args.market, limit=args.limit
                    )
                if args.interpretation in {"b", "both"}:
                    b_rows = await load_double_buy_rows_interpretation_b(
                        session, market=args.market, limit=args.limit
                    )
            report = build_double_buy_parity_report(
                a_rows=a_rows,
                b_rows=b_rows,
                toss_rows=toss_rows,
                limit=args.limit,
                interpretation=args.interpretation,
                current_date=current_date_value.isoformat()
                if current_date_value
                else None,
                prev_date=prev_date_value.isoformat() if prev_date_value else None,
            )
        else:  # pragma: no cover - argparse choices guard this branch
            raise ValueError(f"Unsupported preset: {args.preset}")
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("diagnose_invest_screener_toss_parity crashed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
