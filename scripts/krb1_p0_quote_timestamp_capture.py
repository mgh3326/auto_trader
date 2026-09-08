#!/usr/bin/env python3
"""Intraday GET-only quote timestamp capture + ROB-1121 witness (ROB-1172 AC5).

Captures, per symbol, the broker's own raw timestamp fields
(``stck_bsop_date`` / ``stck_cntg_hour``) alongside the wrapper's freshness claim
(``price_as_of`` / ``price_freshness`` / ``is_stale_price``). The wrapper values are
stored labelled as *non-evidence*: ``compute_is_stale`` compares
``as_of.date() != trading_date``, so intraday it cannot report stale, and
``price_as_of`` may carry the local clock rather than a broker timestamp. When the
wrapper claims ``fresh`` while the raw fields are absent or disagree, the capture
records that contradiction as a witness.

GET-only (KIS quotation reads). No order, preview, cancel, DB write, or scheduler
surface; full completed-session sweeps belong to
``scripts/krb1_p0_completed_session_oneshot.py``.

Usage::

    ENV_FILE=.env.prod uv run python -m scripts.krb1_p0_quote_timestamp_capture \\
      --symbols 005930 000660
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from pathlib import Path

from app.services.brokers.kis.client import KISClient
from app.services.krb1_evidence_chain import EvidenceChainError
from app.services.krb1_metadata_authority import compute_raw_payload_sha256
from app.services.krb1_p0_journal import canonical_json_bytes
from app.services.krb1_quote_timestamp_capture import (
    QUOTE_CAPTURE_STREAM_ID,
    WrapperFreshnessAnnotation,
    append_quote_capture,
    build_quote_timestamp_capture,
)

DEFAULT_STORE_DIR = Path("var/research/krb1/p0_gate_evidence")
CAPTURE_FILENAME = "quote_timestamp_capture.jsonl"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "GET-only KIS quote timestamp capture; wrapper freshness is recorded "
            "as non-evidence together with a ROB-1121 tautology witness"
        )
    )
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--store-dir", type=Path, default=DEFAULT_STORE_DIR)
    parser.add_argument(
        "--no-append",
        action="store_true",
        help="Print captures without appending them to the evidence chain.",
    )
    parser.add_argument("--compact", action="store_true")
    return parser.parse_args(argv)


async def run(
    *,
    symbols: list[str],
    store_dir: Path,
    append: bool,
) -> dict[str, object]:
    client = KISClient()
    capture_path = store_dir / CAPTURE_FILENAME
    captures: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    for symbol in symbols:
        try:
            payload = await client.inquire_price_raw_evidence(symbol, "J")
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
            errors.append(
                {
                    "symbol": symbol,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            continue
        wrapper: WrapperFreshnessAnnotation | None = None
        try:
            quote = await client.inquire_price(symbol, "J")
        except Exception as exc:  # noqa: BLE001 - the wrapper is optional context
            errors.append(
                {
                    "symbol": symbol,
                    "error_type": type(exc).__name__,
                    "message": f"wrapper quote unavailable: {exc}",
                }
            )
        else:
            wrapper = _wrapper_annotation(quote)
        capture = build_quote_timestamp_capture(
            symbol=symbol,
            raw_payload=payload,
            captured_at=dt.datetime.now(dt.UTC),
            wrapper=wrapper,
            raw_payload_sha256=compute_raw_payload_sha256(
                canonical_json_bytes(dict(payload))
            ),
        )
        entry: dict[str, object] = {"capture": capture.as_canonical()}
        if append:
            try:
                entry["appended"] = append_quote_capture(capture_path, capture)
            except EvidenceChainError as exc:
                entry["appended"] = None
                entry["append_error"] = str(exc)
        captures.append(entry)
    return {
        "schema_version": "krb1.p0_3.quote_timestamp_capture.v1",
        "read_only": True,
        "http_method": "GET",
        "wrapper_fields_are_evidence": False,
        "evidence_stream_id": QUOTE_CAPTURE_STREAM_ID,
        "evidence_path": str(capture_path),
        "status": "captured" if captures else "fail_closed",
        "captures": captures,
        "errors": errors,
    }


def _wrapper_annotation(frame: object) -> WrapperFreshnessAnnotation | None:
    """Project the wrapper frame's freshness claim, if it exposes one."""
    try:
        record = frame.iloc[0].to_dict()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - wrapper shape is not a contract here
        return None
    as_of = record.get("price_as_of") or record.get("date")
    return WrapperFreshnessAnnotation(
        price_as_of=str(as_of) if as_of is not None else None,
        price_freshness=(
            str(record["price_freshness"])
            if record.get("price_freshness") is not None
            else None
        ),
        is_stale_price=(
            bool(record["is_stale_price"])
            if record.get("is_stale_price") is not None
            else None
        ),
    )


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = await run(
            symbols=list(args.symbols),
            store_dir=args.store_dir,
            append=not args.no_append,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed with the raw reason
        result = {
            "schema_version": "krb1.p0_3.quote_timestamp_capture.v1",
            "status": "fail_closed",
            "reason": "quote_timestamp_capture_failed",
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
