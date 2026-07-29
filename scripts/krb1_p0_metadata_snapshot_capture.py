#!/usr/bin/env python3
"""Capture an authoritative Toss metadata snapshot append-only (ROB-1172 AC1).

Read/GET-only: one read-only DB transaction for the universe scope plus Toss
``GET /api/v1/stocks`` batches. The raw response body is hashed as received, and
the row preserves both clocks that matter:

* ``metadata_as_of`` — when the authority's statement was current;
* ``retrieved_at`` — when we fetched it.

The selector gate then requires ``metadata_as_of <= retrieved_at <= decision_at``,
so a snapshot captured after the decision can never justify that decision. Run
this *before* ``--decision-at``; running it later fails closed by design.

Toss reads stay behind the existing ``TOSS_API_ENABLED`` gate — when Toss is
disabled this command fails closed and appends nothing. No order, preview,
cancel, DB write, or scheduler surface is reachable.

Usage::

    ENV_FILE=.env.prod uv run python -m scripts.krb1_p0_metadata_snapshot_capture \\
      --as-of-session 2026-07-29 \\
      --decision-at 2026-07-29T18:00:00+09:00
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.services.brokers.toss.client import TossReadClient
from app.services.krb1_evidence_chain import EvidenceChainError
from app.services.krb1_gate_result import KST
from app.services.krb1_metadata_authority import (
    METADATA_SNAPSHOT_STREAM_ID,
    SymbolMetadata,
    append_metadata_snapshot,
)
from app.services.krb1_p0_journal import canonical_json_bytes

DEFAULT_STORE_DIR = Path("var/research/krb1/p0_gate_evidence")
SNAPSHOT_FILENAME = "toss_metadata_snapshot.jsonl"
TOSS_AUTHORITATIVE_SOURCE = "toss_openapi"
BATCH_SIZE = 200
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
            "Append-only authoritative Toss metadata snapshot capture; "
            "a post-decision capture cannot prove pre-decision state"
        )
    )
    parser.add_argument("--as-of-session", type=_date_arg, required=True)
    parser.add_argument("--decision-at", type=_aware_datetime_arg, required=True)
    parser.add_argument("--store-dir", type=Path, default=DEFAULT_STORE_DIR)
    parser.add_argument("--compact", action="store_true")
    return parser.parse_args(argv)


async def _load_universe(
    session: AsyncSession,
) -> dict[str, list[SymbolMetadata]]:
    await session.execute(
        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    )
    result = await session.execute(
        text(
            """
            SELECT symbol, exchange, security_type, is_common_share,
                   listing_status, list_date, krx_trading_suspended
            FROM public.kr_symbol_universe
            WHERE exchange IN ('KOSPI', 'KOSDAQ')
            ORDER BY exchange, symbol
            """
        )
    )
    universe: dict[str, list[SymbolMetadata]] = {market: [] for market in MARKETS}
    for row in result.mappings().all():
        universe[str(row["exchange"])].append(
            SymbolMetadata(
                symbol=str(row["symbol"]),
                exchange=str(row["exchange"]),
                security_type=(
                    str(row["security_type"])
                    if row["security_type"] is not None
                    else None
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
            )
        )
    await session.rollback()
    return universe


async def _fetch_raw_master(
    client: TossReadClient, symbols: list[str]
) -> tuple[bytes, int, list[dict[str, str]]]:
    """Return the concatenated canonical raw payload and per-batch errors."""
    payloads: list[object] = []
    errors: list[dict[str, str]] = []
    for start in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[start : start + BATCH_SIZE]
        try:
            payloads.append(await client.stocks_raw(batch))
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
            errors.append(
                {
                    "batch_start_symbol": batch[0],
                    "batch_size": str(len(batch)),
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
    raw = canonical_json_bytes({"batches": payloads})
    return raw, len(payloads), errors


async def run(
    *,
    as_of_session: dt.date,
    decision_at: dt.datetime,
    store_dir: Path,
    now: dt.datetime,
) -> dict[str, object]:
    base = {
        "schema_version": "krb1.p0_3.metadata_snapshot_capture.v1",
        "as_of_session": as_of_session.isoformat(),
        "decision_at": decision_at.isoformat(),
        "read_only": True,
        "evidence_stream_id": METADATA_SNAPSHOT_STREAM_ID,
    }
    if now > decision_at:
        return {
            **base,
            "status": "fail_closed",
            "reason": "metadata_capture_started_after_decision_at",
            "observed_now_kst": now.astimezone(KST).isoformat(),
            "note": "late capture is not proof of the state at decision_at",
        }

    async with AsyncSessionLocal() as session:
        universe = await _load_universe(session)

    try:
        client = TossReadClient.from_settings()
    except Exception as exc:  # noqa: BLE001 - disabled/misconfigured Toss
        return {
            **base,
            "status": "fail_closed",
            "reason": "toss_authoritative_master_source_unavailable",
            "error_type": type(exc).__name__,
            "required_env_keys": ["TOSS_API_ENABLED"],
        }

    snapshot_path = store_dir / SNAPSHOT_FILENAME
    markets: dict[str, object] = {}
    for market in MARKETS:
        rows = tuple(universe[market])
        symbols = [row.symbol for row in rows]
        raw_payload, batch_count, errors = await _fetch_raw_master(client, symbols)
        if errors or not batch_count:
            markets[market] = {
                "status": "fail_closed",
                "reason": "toss_master_payload_incomplete",
                "batch_count": batch_count,
                "error_count": len(errors),
                "error_examples": errors[:20],
            }
            continue
        retrieved_at = dt.datetime.now(dt.UTC)
        if retrieved_at > decision_at:
            markets[market] = {
                "status": "fail_closed",
                "reason": "metadata_retrieval_clock_after_decision_at",
                "retrieved_at": retrieved_at.isoformat(),
            }
            continue
        try:
            snapshot = append_metadata_snapshot(
                snapshot_path,
                source=TOSS_AUTHORITATIVE_SOURCE,
                market=market,
                rows=rows,
                raw_payload=raw_payload,
                # No server-side authority clock exists on this endpoint, so the
                # retrieval clock is the authority clock: the payload was current
                # at the moment the authority returned it. Recorded explicitly
                # rather than inferred.
                metadata_as_of=retrieved_at,
                retrieved_at=retrieved_at,
            )
        except EvidenceChainError as exc:
            markets[market] = {
                "status": "fail_closed",
                "reason": "metadata_snapshot_append_rejected",
                "message": str(exc),
            }
            continue
        markets[market] = {
            "status": "captured",
            "authority_clock_source": "http_retrieval",
            "batch_count": batch_count,
            "snapshot": snapshot.as_evidence(),
        }
    captured = all(
        isinstance(item, dict) and item.get("status") == "captured"
        for item in markets.values()
    )
    return {
        **base,
        "status": "captured" if captured else "fail_closed",
        "evidence_path": str(snapshot_path),
        "markets": markets,
    }


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = await run(
            as_of_session=args.as_of_session,
            decision_at=args.decision_at,
            store_dir=args.store_dir,
            now=dt.datetime.now(dt.UTC),
        )
    except Exception as exc:  # noqa: BLE001 - fail closed with the raw reason
        result = {
            "schema_version": "krb1.p0_3.metadata_snapshot_capture.v1",
            "status": "fail_closed",
            "reason": "metadata_snapshot_capture_failed",
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
