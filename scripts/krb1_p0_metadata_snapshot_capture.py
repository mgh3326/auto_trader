#!/usr/bin/env python3
"""Capture an authoritative Toss metadata snapshot append-only (ROB-1172 AC1/A2).

Read/GET-only: one read-only DB transaction for the universe scope plus Toss
``GET /api/v1/stocks`` batches. The raw response body is hashed as received, and
two *different* clocks are kept apart:

* the **provider** clock — publication time and effective session, extracted only
  from named provider response fields; and
* the **retrieval** clock — when we fetched it.

🔴 The retrieval clock never stands in for the provider clock. An earlier version
of this command set ``metadata_as_of = retrieved_at`` and labelled it
``authority_clock_source=http_retrieval``; that made a 07-28-vintage master body
retrieved on 07-29 look authoritative for 07-29, which is the staleness the gate
exists to catch (ROB-1172 correction 08:33). Now, when the provider sends no
clock, this command appends nothing and fails closed with
``provider_authority_clock_absent``.

No field in the wired Toss projection is known to carry such a clock
(``TossStockInfo`` has none and ``parse_toss_response`` unwraps the envelope to a
bare row list), so this command is expected to fail closed until a provider
contract is verified and declared in
``app.services.krb1_metadata_authority.PROVIDER_PUBLISHED_AT_FIELDS`` /
``PROVIDER_EFFECTIVE_SESSION_FIELDS``. That is a reviewed change, not a config
toggle.

Toss reads stay behind the existing ``TOSS_API_ENABLED`` gate. No order, preview,
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
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.services import krb1_metadata_authority
from app.services.brokers.toss.client import TossReadClient
from app.services.krb1_evidence_chain import EvidenceChainError
from app.services.krb1_gate_result import KST
from app.services.krb1_metadata_authority import (
    METADATA_SNAPSHOT_STREAM_ID,
    PROVIDER_AUTHORITY_CLOCK_ABSENT,
    PROVIDER_EFFECTIVE_SESSION_FIELDS,
    PROVIDER_PUBLISHED_AT_FIELDS,
    ProviderAuthorityClock,
    SymbolMetadata,
    append_metadata_snapshot,
    extract_provider_authority_clock,
)
from app.services.krb1_p0_journal import canonical_json_bytes

DEFAULT_STORE_DIR = Path("var/research/krb1/p0_gate_evidence")
SNAPSHOT_FILENAME = "toss_metadata_snapshot.jsonl"
TOSS_AUTHORITATIVE_SOURCE = "toss_openapi"
BATCH_SIZE = 200
MARKETS = ("KOSPI", "KOSDAQ")


def _utc_now() -> dt.datetime:
    """The local retrieval clock. Injectable so runs are reproducible in tests."""
    return dt.datetime.now(dt.UTC)


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
) -> tuple[bytes, int, list[dict[str, str]], ProviderAuthorityClock | None]:
    """Return the concatenated raw payload, batch count, errors, provider clock.

    The provider clock is only ever *extracted* from the payload. There is no
    branch that derives it from the local clock.
    """
    payloads: list[object] = []
    errors: list[dict[str, str]] = []
    provider_clock: ProviderAuthorityClock | None = None
    for start in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[start : start + BATCH_SIZE]
        try:
            payload = await client.stocks_raw(batch)
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
            errors.append(
                {
                    "batch_start_symbol": batch[0],
                    "batch_size": str(len(batch)),
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            continue
        payloads.append(payload)
        if provider_clock is None:
            # Allowlists are read at call time from the service module so the
            # declared provider contract stays one reviewed constant, not a
            # duplicated literal here.
            provider_clock = extract_provider_authority_clock(
                payload,
                published_at_fields=krb1_metadata_authority.PROVIDER_PUBLISHED_AT_FIELDS,
                effective_session_fields=(
                    krb1_metadata_authority.PROVIDER_EFFECTIVE_SESSION_FIELDS
                ),
            )
    raw = canonical_json_bytes({"batches": payloads})
    return raw, len(payloads), errors, provider_clock


async def run(
    *,
    as_of_session: dt.date,
    decision_at: dt.datetime,
    store_dir: Path,
    now: dt.datetime,
    clock: Callable[[], dt.datetime] = _utc_now,
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
        raw_payload, batch_count, errors, provider_clock = await _fetch_raw_master(
            client, symbols
        )
        if errors or not batch_count:
            markets[market] = {
                "status": "fail_closed",
                "reason": "toss_master_payload_incomplete",
                "batch_count": batch_count,
                "error_count": len(errors),
                "error_examples": errors[:20],
            }
            continue
        # 🔴 No provider clock, no snapshot. The retrieval clock below is recorded
        # as a retrieval clock only; it is never promoted to authority.
        if provider_clock is None:
            markets[market] = {
                "status": "fail_closed",
                "reason": PROVIDER_AUTHORITY_CLOCK_ABSENT,
                "batch_count": batch_count,
                "declared_provider_published_at_fields": sorted(
                    PROVIDER_PUBLISHED_AT_FIELDS
                ),
                "declared_provider_effective_session_fields": sorted(
                    PROVIDER_EFFECTIVE_SESSION_FIELDS
                ),
                "note": (
                    "consumer retrieval time is a different clock from provider "
                    "publication/effective time and cannot substitute for it"
                ),
                "appended": False,
            }
            continue
        retrieved_at = clock()
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
                provider_clock=provider_clock,
                retrieved_at=retrieved_at,
            )
        except (EvidenceChainError, ValueError) as exc:
            markets[market] = {
                "status": "fail_closed",
                "reason": "metadata_snapshot_append_rejected",
                "message": str(exc),
            }
            continue
        markets[market] = {
            "status": "captured",
            "provider_clock_fields": {
                "published_at": provider_clock.published_at_field,
                "effective_session": provider_clock.effective_session_field,
            },
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
