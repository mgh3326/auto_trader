#!/usr/bin/env python3
"""Read-only CLI for the deterministic KR-B1 P0-3 liquidity selector.

The command performs SELECT queries plus KIS quotation GETs only. It has no
order, preview, cancel, journal, scheduler, or database-write surface.

Gate evidence is read from the append-only chains produced by
``scripts/krb1_p0_metadata_snapshot_capture.py`` (AC1) and
``scripts/krb1_p0_completed_session_oneshot.py`` (AC2). Reference-price exception
evidence comes from the fail-closed adapter (AC4), which has no success branch
while no authoritative KRX/KIS source is wired.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import exchange_calendars as xcals
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.services.brokers.kis.client import KISClient
from app.services.krb1_completion_finality import fetch_provider_finality
from app.services.krb1_completion_manifest import (
    CompletionManifest,
    load_latest_completion_manifest,
)
from app.services.krb1_evidence_chain import EvidenceChainError
from app.services.krb1_metadata_authority import (
    MetadataAuthoritySnapshot,
    compute_raw_payload_sha256,
    load_latest_metadata_snapshot,
)
from app.services.krb1_p0_journal import canonical_json_bytes
from app.services.krb1_p0_liquidity_selector import (
    CandleRow,
    CompletedBarEvidence,
    Market,
    QuoteTimestampCapture,
    SelectorInput,
    UniverseRow,
    select_krb1_p0_liquidity_candidates,
)
from app.services.krb1_quote_timestamp_capture import build_quote_timestamp_capture
from app.services.krb1_reference_exception_adapter import (
    fetch_reference_price_exceptions,
)

DEFAULT_STORE_DIR = Path("var/research/krb1/p0_gate_evidence")
METADATA_SNAPSHOT_FILENAME = "toss_metadata_snapshot.jsonl"
COMPLETION_MANIFEST_FILENAME = "completion_manifest.jsonl"
MARKETS: tuple[Market, Market] = ("KOSPI", "KOSDAQ")


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
        "--decision-at",
        type=_aware_datetime_arg,
        required=True,
        help=(
            "Decision clock (ISO-8601 with offset). Every evidence clock must be "
            "at or before this instant; later evidence cannot prove this decision."
        ),
    )
    parser.add_argument("--store-dir", type=Path, default=DEFAULT_STORE_DIR)
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
    decision_at: dt.datetime,
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
            # Retrieval provenance only (ROB-1172 D4). This names the DB column we
            # read; it is not a provider assertion, and the authority gate no
            # longer accepts it as one.
            db_sync_source=(
                "db.kr_symbol_universe.toss_master_updated_at"
                if row["toss_master_updated_at"] is not None
                else None
            ),
            db_sync_observed_at=row["toss_master_updated_at"],
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
        decision_at=decision_at,
        expected_universe_counts=expected_counts,
        universe_rows=universe_rows,
        candle_rows=candle_rows,
    )


def _load_gate_evidence(
    store_dir: Path, *, as_of_session: dt.date
) -> tuple[
    tuple[MetadataAuthoritySnapshot, ...],
    tuple[CompletionManifest, ...],
    list[dict[str, str]],
]:
    """Read append-only gate evidence. A verification failure is never ignored."""
    snapshots: list[MetadataAuthoritySnapshot] = []
    manifests: list[CompletionManifest] = []
    errors: list[dict[str, str]] = []
    for market in MARKETS:
        try:
            snapshot = load_latest_metadata_snapshot(
                store_dir / METADATA_SNAPSHOT_FILENAME, market=market
            )
        except (EvidenceChainError, KeyError, ValueError) as exc:
            errors.append(
                {
                    "market": market,
                    "stream": "toss_metadata_snapshot",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
        else:
            if snapshot is not None:
                snapshots.append(snapshot)
        try:
            manifest = load_latest_completion_manifest(
                store_dir / COMPLETION_MANIFEST_FILENAME,
                market=market,
                session_date=as_of_session,
            )
        except (EvidenceChainError, KeyError, ValueError) as exc:
            errors.append(
                {
                    "market": market,
                    "stream": "completion_manifest",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
        else:
            if manifest is not None:
                manifests.append(manifest)
    return tuple(snapshots), tuple(manifests), errors


def _rank_head_symbols(result: dict[str, object]) -> list[str]:
    market_results = result.get("market_results")
    if not isinstance(market_results, dict):
        return []
    symbols: list[str] = []
    for market in MARKETS:
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
    tuple[QuoteTimestampCapture, ...],
    list[dict[str, str]],
]:
    completed: list[CompletedBarEvidence] = []
    quotes: list[QuoteTimestampCapture] = []
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
                    # 🔴 Provider-origin identity only. The KIS daily response has
                    # no symbol field, so this stays None and the gate fails
                    # closed; the requested symbol must never be injected here.
                    raw_symbol=daily.get("stck_shrn_iscd"),
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
                build_quote_timestamp_capture(
                    symbol=symbol,
                    raw_payload=quote,
                    captured_at=dt.datetime.now(dt.UTC),
                    raw_payload_sha256=compute_raw_payload_sha256(
                        canonical_json_bytes(dict(quote))
                    ),
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


async def run(
    *,
    as_of_session: dt.date,
    target_session: dt.date,
    decision_at: dt.datetime,
    store_dir: Path,
) -> dict[str, object]:
    async with AsyncSessionLocal() as session:
        selector_input = await _load_db_input(
            session,
            as_of_session=as_of_session,
            target_session=target_session,
            decision_at=decision_at,
        )

    snapshots, manifests, evidence_errors = _load_gate_evidence(
        store_dir, as_of_session=as_of_session
    )
    selector_input = SelectorInput(
        as_of_session=selector_input.as_of_session,
        target_session=selector_input.target_session,
        decision_at=selector_input.decision_at,
        expected_universe_counts=selector_input.expected_universe_counts,
        universe_rows=selector_input.universe_rows,
        candle_rows=selector_input.candle_rows,
        metadata_snapshots=snapshots,
        completion_manifests=manifests,
    )
    initial = select_krb1_p0_liquidity_candidates(selector_input)
    head_symbols = _rank_head_symbols(initial)
    completed, quotes, upstream_errors = await _fetch_raw_upstream_evidence(
        symbols=head_symbols,
        as_of_session=as_of_session,
    )
    # Both adapters are fail-closed stubs: they return nothing and name the unwired
    # source. Neither is operator-overridable.
    reference_fetch = fetch_reference_price_exceptions(
        symbols=head_symbols,
        target_session=target_session,
        decision_at=decision_at,
    )
    finality_fetch = fetch_provider_finality(
        market="KOSPI",
        session_date=as_of_session,
        decision_at=decision_at,
    )
    final_input = SelectorInput(
        as_of_session=selector_input.as_of_session,
        target_session=selector_input.target_session,
        decision_at=selector_input.decision_at,
        expected_universe_counts=selector_input.expected_universe_counts,
        universe_rows=selector_input.universe_rows,
        candle_rows=selector_input.candle_rows,
        reference_price_exception_records=reference_fetch.records,
        completed_bar_evidence=completed,
        quote_timestamp_evidence=quotes,
        metadata_snapshots=snapshots,
        completion_manifests=manifests,
        reference_source_unavailable_reason=reference_fetch.reason,
        finality_source_unavailable_reason=finality_fetch.reason,
    )
    result = select_krb1_p0_liquidity_candidates(final_input)
    result["source_availability"] = {
        "reference_price_exception": reference_fetch.as_evidence(),
        "provider_daily_finality": finality_fetch.as_evidence(),
        "raw_upstream_errors": upstream_errors,
        "gate_evidence_stream_errors": evidence_errors,
        "metadata_snapshot_markets": sorted(row.market for row in snapshots),
        "completion_manifest_markets": sorted(row.market for row in manifests),
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
        "quote_timestamp_evidence": [row.as_canonical() for row in quotes],
    }
    return result


def _data_load_failure(
    *,
    as_of_session: dt.date,
    target_session: dt.date,
    decision_at: dt.datetime,
    exc: Exception,
) -> dict[str, object]:
    return {
        "schema_version": "krb1.p0_3.liquidity_selector.evidence.v2",
        "status": "fail_closed",
        "read_only": True,
        "fallback_used": False,
        "as_of_session": as_of_session.isoformat(),
        "target_session": target_session.isoformat(),
        "decision_at": decision_at.isoformat(),
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
            decision_at=args.decision_at,
            store_dir=args.store_dir,
        )
    except Exception as exc:
        result = _data_load_failure(
            as_of_session=args.as_of_session,
            target_session=target_session,
            decision_at=args.decision_at,
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
