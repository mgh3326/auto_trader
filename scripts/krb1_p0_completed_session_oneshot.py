#!/usr/bin/env python3
"""One-shot completed-session raw sweep + completion manifest (ROB-1172 AC2/AC5).

Read/GET-only: SELECT queries inside a ``REPEATABLE READ READ ONLY`` transaction
plus KIS daily quotation GETs. There is no order, preview, cancel, journal, DB
write, or scheduler surface, and nothing here registers a recurring task — the
sweep is invoked manually, once, after the KRX daily completion cutoff
(15:35 KST). Running it before the cutoff fails closed: a forming session cannot
produce completion evidence.

The only write is an append to the service-layer evidence chain
(:mod:`app.services.krb1_evidence_chain`).

Usage::

    ENV_FILE=.env.prod uv run python -m scripts.krb1_p0_completed_session_oneshot \\
      --as-of-session 2026-07-29 \\
      --decision-at 2026-07-29T18:00:00+09:00
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.services.brokers.kis.client import KISClient
from app.services.krb1_completion_manifest import (
    COMPLETION_MANIFEST_STREAM_ID,
    KRX_DAILY_COMPLETION_CUTOFF,
    CompletionManifest,
    DbDailyBar,
    RawDailyBar,
    append_completion_manifest,
    build_completion_manifest,
)
from app.services.krb1_gate_result import KST, kst_datetime

DEFAULT_STORE_DIR = Path("var/research/krb1/p0_gate_evidence")
MANIFEST_FILENAME = "completion_manifest.jsonl"
MARKETS = ("KOSPI", "KOSDAQ")


def _date_arg(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _aware_datetime_arg(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("decision-at must include a UTC offset")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scheduleless one-shot KIS raw daily sweep that produces an "
            "exact-reconcile completion manifest; missing evidence fails closed"
        )
    )
    parser.add_argument("--as-of-session", type=_date_arg, required=True)
    parser.add_argument(
        "--decision-at",
        type=_aware_datetime_arg,
        required=True,
        help="Decision clock (ISO-8601 with offset). Evidence must precede it.",
    )
    parser.add_argument("--store-dir", type=Path, default=DEFAULT_STORE_DIR)
    parser.add_argument(
        "--max-symbols",
        type=int,
        default=None,
        help=(
            "Diagnostic partial sweep. A partial sweep can never produce a "
            "proven manifest; the uncovered symbols are recorded as missing."
        ),
    )
    parser.add_argument(
        "--no-append",
        action="store_true",
        help="Compute the manifest without appending it to the evidence chain.",
    )
    parser.add_argument("--compact", action="store_true")
    return parser.parse_args(argv)


def _exact_int(value: object, *, field: str, symbol: str) -> int:
    if isinstance(value, bool | float) or value is None:
        raise ValueError(f"{field} is not an exact integer for {symbol}")
    decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    integral = decimal.to_integral_value()
    if decimal != integral:
        raise ValueError(f"{field} is not an exact integer for {symbol}")
    return int(integral)


async def _load_scope(
    session: AsyncSession, *, as_of_session: dt.date
) -> tuple[dict[str, list[str]], dict[str, list[DbDailyBar]]]:
    await session.execute(
        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    )
    universe_result = await session.execute(
        text(
            """
            SELECT symbol, exchange
            FROM public.kr_symbol_universe
            WHERE exchange IN ('KOSPI', 'KOSDAQ')
              AND is_active IS TRUE
              AND listing_status = 'ACTIVE'
              AND list_date IS NOT NULL
              AND list_date <= :as_of
            ORDER BY exchange, symbol
            """
        ),
        {"as_of": as_of_session},
    )
    universe: dict[str, list[str]] = {market: [] for market in MARKETS}
    for row in universe_result.mappings().all():
        universe[str(row["exchange"])].append(str(row["symbol"]))

    candle_result = await session.execute(
        text(
            """
            SELECT c.symbol, u.exchange, c.venue,
                   (c.time AT TIME ZONE 'Asia/Seoul')::date AS session_date,
                   c.open, c.high, c.low, c.close, c.volume, c.value
            FROM public.kr_candles_1d AS c
            JOIN public.kr_symbol_universe AS u ON u.symbol = c.symbol
            WHERE c.venue = 'KRX'
              AND u.exchange IN ('KOSPI', 'KOSDAQ')
              AND (c.time AT TIME ZONE 'Asia/Seoul')::date = :session_date
            ORDER BY u.exchange, c.symbol
            """
        ),
        {"session_date": as_of_session},
    )
    db_bars: dict[str, list[DbDailyBar]] = {market: [] for market in MARKETS}
    for row in candle_result.mappings().all():
        symbol = str(row["symbol"])
        db_bars[str(row["exchange"])].append(
            DbDailyBar(
                symbol=symbol,
                session_date=row["session_date"],
                venue=str(row["venue"]),
                open=_exact_int(row["open"], field="open", symbol=symbol),
                high=_exact_int(row["high"], field="high", symbol=symbol),
                low=_exact_int(row["low"], field="low", symbol=symbol),
                close=_exact_int(row["close"], field="close", symbol=symbol),
                volume=_exact_int(row["volume"], field="volume", symbol=symbol),
                value=_exact_int(row["value"], field="value", symbol=symbol),
            )
        )
    await session.rollback()
    return universe, db_bars


async def _sweep_raw_daily(
    client: KISClient,
    *,
    symbols: list[str],
    as_of_session: dt.date,
) -> tuple[list[RawDailyBar], list[dict[str, str]]]:
    raw_bars: list[RawDailyBar] = []
    errors: list[dict[str, str]] = []
    for symbol in symbols:
        try:
            payload = await client.inquire_daily_itemchartprice_raw_evidence(
                symbol, as_of_session, "J"
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never silently dropped
            errors.append(
                {
                    "symbol": symbol,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            continue
        raw_bars.append(
            RawDailyBar(
                symbol=symbol,
                endpoint=str(payload.get("endpoint") or ""),
                tr_id=str(payload.get("tr_id") or ""),
                # Provider-origin identity only; absent in this TR (F-02).
                raw_symbol=payload.get("stck_shrn_iscd"),
                raw_business_date=payload.get("stck_bsop_date"),
                raw_open=payload.get("stck_oprc"),
                raw_high=payload.get("stck_hgpr"),
                raw_low=payload.get("stck_lwpr"),
                raw_close=payload.get("stck_clpr"),
                raw_volume=payload.get("acml_vol"),
                raw_value=payload.get("acml_tr_pbmn"),
                observed_at=dt.datetime.now(dt.UTC),
                rt_cd=payload.get("rt_cd"),
            )
        )
    return raw_bars, errors


def _cutoff_gate(
    *, as_of_session: dt.date, now: dt.datetime
) -> dict[str, object] | None:
    cutoff = kst_datetime(as_of_session, KRX_DAILY_COMPLETION_CUTOFF)
    if now >= cutoff:
        return None
    return {
        "status": "fail_closed",
        "reason": "completed_session_raw_collection_before_daily_completion_cutoff",
        "required_at_or_after_kst": cutoff.isoformat(),
        "observed_now_kst": now.astimezone(KST).isoformat(),
        "note": "a forming session cannot produce completed-session evidence",
    }


async def run(
    *,
    as_of_session: dt.date,
    decision_at: dt.datetime,
    store_dir: Path,
    max_symbols: int | None,
    append: bool,
    now: dt.datetime,
) -> dict[str, object]:
    blocked = _cutoff_gate(as_of_session=as_of_session, now=now)
    if blocked is not None:
        return {
            "schema_version": "krb1.p0_3.completed_session_oneshot.v1",
            "as_of_session": as_of_session.isoformat(),
            "decision_at": decision_at.isoformat(),
            "read_only": True,
            "scheduleless_one_shot": True,
            **blocked,
        }
    if now > decision_at:
        return {
            "schema_version": "krb1.p0_3.completed_session_oneshot.v1",
            "as_of_session": as_of_session.isoformat(),
            "decision_at": decision_at.isoformat(),
            "read_only": True,
            "scheduleless_one_shot": True,
            "status": "fail_closed",
            "reason": "sweep_started_after_decision_at",
            "observed_now_kst": now.astimezone(KST).isoformat(),
        }

    async with AsyncSessionLocal() as session:
        universe, db_bars = await _load_scope(session, as_of_session=as_of_session)

    manifest_path = store_dir / MANIFEST_FILENAME
    client = KISClient()
    markets: dict[str, object] = {}
    for market in MARKETS:
        symbols = universe[market]
        swept = symbols if max_symbols is None else symbols[:max_symbols]
        raw_bars, errors = await _sweep_raw_daily(
            client, symbols=swept, as_of_session=as_of_session
        )
        manifest = build_completion_manifest(
            market=market,
            session_date=as_of_session,
            universe_symbols=symbols,
            raw_bars=raw_bars,
            db_bars=db_bars[market],
            finalized_at=dt.datetime.now(dt.UTC),
            decision_at=decision_at,
        )
        stored: CompletionManifest | None = None
        if append:
            stored = append_completion_manifest(manifest_path, manifest)
        markets[market] = {
            "manifest": (stored or manifest).as_evidence(),
            "diagnostic_partial_sweep": max_symbols is not None,
            "swept_symbol_count": len(swept),
            "upstream_error_count": len(errors),
            "upstream_error_examples": errors[:20],
        }
    return {
        "schema_version": "krb1.p0_3.completed_session_oneshot.v1",
        "as_of_session": as_of_session.isoformat(),
        "decision_at": decision_at.isoformat(),
        "read_only": True,
        "scheduleless_one_shot": True,
        "appended": append,
        "evidence_stream_id": COMPLETION_MANIFEST_STREAM_ID,
        "evidence_path": str(manifest_path),
        "status": "captured",
        "markets": markets,
    }


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = await run(
            as_of_session=args.as_of_session,
            decision_at=args.decision_at,
            store_dir=args.store_dir,
            max_symbols=args.max_symbols,
            append=not args.no_append,
            now=dt.datetime.now(dt.UTC),
        )
    except Exception as exc:  # noqa: BLE001 - fail closed with the raw reason
        result = {
            "schema_version": "krb1.p0_3.completed_session_oneshot.v1",
            "status": "fail_closed",
            "reason": "completed_session_sweep_failed",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=None if args.compact else 2,
            sort_keys=True,
        )
    )
    return 0 if result.get("status") == "captured" else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
