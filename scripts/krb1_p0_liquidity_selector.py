#!/usr/bin/env python3
"""Read-only CLI for the deterministic KR-B1 P0-3 liquidity selector.

The command performs SELECT queries plus KIS quotation GETs only. It has no
order, preview, cancel, journal, scheduler, or database-write surface.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from decimal import Decimal

import exchange_calendars as xcals
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.services.brokers.kis.client import KISClient
from app.services.krb1_p0_liquidity_selector import (
    CandleRow,
    CompletedBarEvidence,
    Market,
    QuoteTimestampEvidence,
    SelectorInput,
    UniverseRow,
    select_krb1_p0_liquidity_candidates,
)


def _date_arg(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only KR-B1 P0-3 KOSPI/KOSDAQ liquidity selector; "
            "any missing evidence fails closed"
        )
    )
    parser.add_argument(
        "--as-of-session",
        type=_date_arg,
        required=True,
        help="Completed KRX session date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--target-session",
        type=_date_arg,
        help=(
            "Order-lifetime measurement session (YYYY-MM-DD). "
            "Defaults to the next XKRX session."
        ),
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact JSON instead of indented JSON.",
    )
    return parser.parse_args(argv)


def _next_krx_session(as_of_session: dt.date) -> dt.date:
    calendar = xcals.get_calendar("XKRX")
    next_session = calendar.next_session(as_of_session)
    return next_session.date()


def _exact_int(value: object, *, field: str, symbol: str) -> int:
    if isinstance(value, bool | float) or value is None:
        raise ValueError(f"{field} is not an exact integer for {symbol}")
    decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    integral = decimal.to_integral_value()
    if decimal != integral:
        raise ValueError(f"{field} is not an exact integer for {symbol}")
    return int(integral)


async def _load_db_input(
    session: AsyncSession,
    *,
    as_of_session: dt.date,
    target_session: dt.date,
) -> SelectorInput:
    # Must be the first statement in this transaction. It makes accidental SQL
    # mutation fail at the database boundary in addition to the CLI having none.
    await session.execute(
        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    )
    count_rows = (
        await session.execute(
            text(
                """
                SELECT exchange, count(*) AS row_count
                FROM public.kr_symbol_universe
                WHERE exchange IN ('KOSPI', 'KOSDAQ')
                GROUP BY exchange
                ORDER BY exchange
                """
            )
        )
    ).mappings()
    expected_counts: dict[Market, int] = {
        str(row["exchange"]): int(row["row_count"]) for row in count_rows
    }  # type: ignore[misc]

    universe_result = await session.execute(
        text(
            """
            SELECT symbol, name, exchange, is_active, security_type,
                   is_common_share, listing_status, list_date,
                   krx_trading_suspended, toss_master_updated_at
            FROM public.kr_symbol_universe
            WHERE exchange IN ('KOSPI', 'KOSDAQ')
            ORDER BY exchange, symbol
            """
        )
    )
    universe_rows = tuple(
        UniverseRow(
            symbol=str(row["symbol"]),
            name=str(row["name"]),
            exchange=str(row["exchange"]),
            is_active=bool(row["is_active"]),
            security_type=(
                str(row["security_type"]) if row["security_type"] is not None else None
            ),
            is_common_share=(
                bool(row["is_common_share"])
                if row["is_common_share"] is not None
                else None
            ),
            listing_status=(
                str(row["listing_status"])
                if row["listing_status"] is not None
                else None
            ),
            list_date=row["list_date"],
            krx_trading_suspended=(
                bool(row["krx_trading_suspended"])
                if row["krx_trading_suspended"] is not None
                else None
            ),
            metadata_source=(
                "toss_openapi" if row["toss_master_updated_at"] is not None else None
            ),
            metadata_as_of=row["toss_master_updated_at"],
        )
        for row in universe_result.mappings().all()
    )

    candle_result = await session.execute(
        text(
            """
            SELECT (time AT TIME ZONE 'Asia/Seoul')::date AS session_date,
                   symbol, venue, open, high, low, close, volume, value,
                   source, ingested_at
            FROM public.kr_candles_1d
            WHERE venue = 'KRX'
              AND (time AT TIME ZONE 'Asia/Seoul')::date = :session_date
            ORDER BY symbol, time
            """
        ),
        {"session_date": as_of_session},
    )
    candle_rows = tuple(
        CandleRow(
            session_date=row["session_date"],
            symbol=str(row["symbol"]),
            venue=str(row["venue"]),
            open=_exact_int(row["open"], field="open", symbol=str(row["symbol"])),
            high=_exact_int(row["high"], field="high", symbol=str(row["symbol"])),
            low=_exact_int(row["low"], field="low", symbol=str(row["symbol"])),
            close=_exact_int(row["close"], field="close", symbol=str(row["symbol"])),
            volume=_exact_int(row["volume"], field="volume", symbol=str(row["symbol"])),
            value=_exact_int(row["value"], field="value", symbol=str(row["symbol"])),
            source=str(row["source"]),
            ingested_at=row["ingested_at"],
        )
        for row in candle_result.mappings().all()
    )
    await session.rollback()
    return SelectorInput(
        as_of_session=as_of_session,
        target_session=target_session,
        expected_universe_counts=expected_counts,
        universe_rows=universe_rows,
        candle_rows=candle_rows,
    )


def _rank_head_symbols(result: dict[str, object]) -> list[str]:
    market_results = result.get("market_results")
    if not isinstance(market_results, dict):
        return []
    symbols: list[str] = []
    for market in ("KOSPI", "KOSDAQ"):
        market_result = market_results.get(market)
        if not isinstance(market_result, dict):
            continue
        head = market_result.get("pre_reference_rank_head")
        if not isinstance(head, dict):
            continue
        universe_row = head.get("universe_row")
        if isinstance(universe_row, dict) and isinstance(
            universe_row.get("symbol"), str
        ):
            symbols.append(universe_row["symbol"])
    return symbols


async def _fetch_raw_upstream_evidence(
    *,
    symbols: list[str],
    as_of_session: dt.date,
) -> tuple[
    tuple[CompletedBarEvidence, ...],
    tuple[QuoteTimestampEvidence, ...],
    list[dict[str, str]],
]:
    completed: list[CompletedBarEvidence] = []
    quotes: list[QuoteTimestampEvidence] = []
    errors: list[dict[str, str]] = []
    client = KISClient()
    for symbol in symbols:
        try:
            daily = await client.inquire_daily_itemchartprice_raw_evidence(
                symbol, as_of_session, "J"
            )
            completed.append(
                CompletedBarEvidence(
                    symbol=symbol,
                    endpoint=str(daily.get("endpoint") or ""),
                    tr_id=str(daily.get("tr_id") or ""),
                    raw_business_date=daily.get("stck_bsop_date"),
                    raw_close=daily.get("stck_clpr"),
                    raw_volume=daily.get("acml_vol"),
                    raw_value=daily.get("acml_tr_pbmn"),
                    observed_at=dt.datetime.now(dt.UTC),
                )
            )
        except Exception as exc:
            errors.append(
                {
                    "symbol": symbol,
                    "source": "kis_daily_raw",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
        try:
            quote = await client.inquire_price_raw_evidence(symbol, "J")
            quotes.append(
                QuoteTimestampEvidence(
                    symbol=symbol,
                    endpoint=str(quote.get("endpoint") or ""),
                    tr_id=str(quote.get("tr_id") or ""),
                    raw_symbol=quote.get("stck_shrn_iscd"),
                    raw_business_date=quote.get("stck_bsop_date"),
                    raw_execution_time=quote.get("stck_cntg_hour"),
                    raw_last_price=quote.get("stck_prpr"),
                )
            )
        except Exception as exc:
            errors.append(
                {
                    "symbol": symbol,
                    "source": "kis_quote_raw",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
    return tuple(completed), tuple(quotes), errors


def _unavailable_reference_source() -> dict[str, object]:
    return {
        "status": "unprovable",
        "reason": "authoritative_target_session_reference_exception_source_not_wired",
        "required_scope": "all_pre_reference_eligible_symbols",
        "fallback_forbidden": True,
    }


async def run(
    *,
    as_of_session: dt.date,
    target_session: dt.date,
) -> dict[str, object]:
    async with AsyncSessionLocal() as session:
        selector_input = await _load_db_input(
            session,
            as_of_session=as_of_session,
            target_session=target_session,
        )

    initial = select_krb1_p0_liquidity_candidates(selector_input)
    head_symbols = _rank_head_symbols(initial)
    completed, quotes, upstream_errors = await _fetch_raw_upstream_evidence(
        symbols=head_symbols,
        as_of_session=as_of_session,
    )
    final_input = SelectorInput(
        as_of_session=selector_input.as_of_session,
        target_session=selector_input.target_session,
        expected_universe_counts=selector_input.expected_universe_counts,
        universe_rows=selector_input.universe_rows,
        candle_rows=selector_input.candle_rows,
        # Intentionally empty until an authoritative, target-session-effective
        # source is wired. This is not operator-overridable.
        reference_exception_evidence=(),
        completed_bar_evidence=completed,
        quote_timestamp_evidence=quotes,
    )
    result = select_krb1_p0_liquidity_candidates(final_input)
    result["source_availability"] = {
        "reference_price_exception": _unavailable_reference_source(),
        "raw_upstream_errors": upstream_errors,
    }
    result["diagnostic_raw_observations"] = {
        "not_a_selection": True,
        "symbols": head_symbols,
        "completed_bar_evidence": [
            {
                "symbol": row.symbol,
                "endpoint": row.endpoint,
                "tr_id": row.tr_id,
                "stck_bsop_date": row.raw_business_date,
                "stck_clpr": row.raw_close,
                "acml_vol": row.raw_volume,
                "acml_tr_pbmn": row.raw_value,
                "observed_at": row.observed_at.isoformat(),
            }
            for row in completed
        ],
        "quote_timestamp_evidence": [
            {
                "symbol": row.symbol,
                "endpoint": row.endpoint,
                "tr_id": row.tr_id,
                "stck_shrn_iscd": row.raw_symbol,
                "stck_bsop_date": row.raw_business_date,
                "stck_cntg_hour": row.raw_execution_time,
                "stck_prpr": row.raw_last_price,
            }
            for row in quotes
        ],
    }
    return result


def _data_load_failure(
    *,
    as_of_session: dt.date,
    target_session: dt.date,
    exc: Exception,
) -> dict[str, object]:
    return {
        "schema_version": "krb1.p0_3.liquidity_selector.evidence.v1",
        "status": "fail_closed",
        "read_only": True,
        "fallback_used": False,
        "as_of_session": as_of_session.isoformat(),
        "target_session": target_session.isoformat(),
        "selected_candidates": [],
        "fail_close_reasons": [
            {
                "scope": "global",
                "gate": "input_data_load",
                "reason": "selector_input_data_unprovable",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        ],
    }


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    target_session = args.target_session or _next_krx_session(args.as_of_session)
    try:
        result = await run(
            as_of_session=args.as_of_session,
            target_session=target_session,
        )
    except Exception as exc:
        result = _data_load_failure(
            as_of_session=args.as_of_session,
            target_session=target_session,
            exc=exc,
        )
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=None if args.compact else 2,
            sort_keys=True,
        )
    )
    return 0 if result.get("status") == "selected" else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
