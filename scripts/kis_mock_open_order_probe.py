#!/usr/bin/env python3
"""Read-only KIS mock open-order probe (ROB-1007).

Answers one question: does KIS mock (VTTC8001R domestic / VTTS3035R
overseas) ever return order-history rows with a non-zero remaining/unfilled
quantity, or does mock always resolve orders to a terminal state
immediately? This informs whether ``kis_mock_reconciliation_run``'s
time-based "stale" demotion is even reachable from real mock broker
behavior.

This script is default-disabled and has NO order, modify, or cancel code
path — it calls only the existing read-only
``inquire_daily_order_domestic``/``inquire_daily_order_overseas`` client
methods with ``is_mock=True`` (KIS TR VTTC8001R / VTTS3035R). It does not add
new TR wiring.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

PROBE_ENABLE_ENV = "KIS_MOCK_OPEN_ORDER_PROBE_ENABLED"
REQUIRED_MOCK_ENV: tuple[str, ...] = (
    "KIS_MOCK_APP_KEY",
    "KIS_MOCK_APP_SECRET",
    "KIS_MOCK_ACCOUNT_NO",
)
_REDACTED = "[REDACTED]"
_SENSITIVE_TEXT = re.compile(
    r"(?i)(authorization|bearer|access[_ -]?token|app[_ -]?(?:key|secret)|"
    r"account(?:[_ -]?(?:no|number))?|cano)\s*[:=]\s*[^,\s}]+"
)


def probe_enabled(environ: Mapping[str, str] | None = None) -> bool:
    import os

    value = (environ or os.environ).get(PROBE_ENABLE_ENV, "")
    return value.strip().lower() == "true"


def missing_mock_credential_names(
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    import os

    values = environ or os.environ
    return [name for name in REQUIRED_MOCK_ENV if not values.get(name, "").strip()]


def redact_row(
    row: Mapping[str, Any], *, secret_values: tuple[str, ...]
) -> dict[str, Any]:
    """Redact known account-identifying keys plus any credential echoes.

    Reuses the existing broker-key redaction helper (same convention as
    ``scripts/kis_mock_us_cash_probe.py``) rather than inventing a second
    redaction scheme.
    """
    from app.services.brokers.kiwoom.normalization import redact_broker_response

    def scrub(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(k): scrub(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [scrub(item) for item in value]
        if not isinstance(value, str):
            return value
        redacted = value
        for secret in secret_values:
            if secret:
                redacted = redacted.replace(secret, _REDACTED)
                redacted = redacted.replace(secret.replace("-", ""), _REDACTED)
        return _SENSITIVE_TEXT.sub(_REDACTED, redacted)

    return scrub(redact_broker_response(dict(row)))


def _domestic_row_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    remaining = row.get("rmn_qty")
    return {
        "order_no": row.get("odno"),
        "symbol": row.get("pdno"),
        "order_qty": row.get("ord_qty"),
        "filled_qty": row.get("tot_ccld_qty"),
        "remaining_qty": remaining,
        "rejected_qty": row.get("rjct_qty"),
        "cancel_flag": row.get("cncl_yn"),
        "order_status": (
            "open_remaining" if _is_nonzero(remaining) else "no_remaining_qty"
        ),
    }


def _overseas_row_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    remaining = row.get("nccs_qty") or row.get("rmn_qty")
    return {
        "order_no": row.get("odno"),
        "symbol": row.get("pdno") or row.get("ovrs_pdno"),
        "order_qty": row.get("ord_qty"),
        "filled_qty": row.get("ft_ccld_qty") or row.get("tot_ccld_qty"),
        "remaining_qty": remaining,
        "cancel_flag": row.get("cncl_yn"),
        "order_status": (
            "open_remaining" if _is_nonzero(remaining) else "no_remaining_qty"
        ),
    }


def _is_nonzero(value: Any) -> bool:
    if value in (None, ""):
        return False
    try:
        return float(value) != 0
    except (TypeError, ValueError):
        return False


async def run_probe(*, market: str, start_date: str, end_date: str) -> int:
    if not probe_enabled():
        print(
            f"[probe] disabled: set {PROBE_ENABLE_ENV}=true to permit read-only TR calls"
        )
        return 0

    missing = missing_mock_credential_names()
    if missing:
        print(
            f"[probe] missing KIS mock credentials: {', '.join(missing)} (names only)"
        )
        return 3

    from app.services.brokers.kis.client import KISClient

    client = KISClient(is_mock=True)
    secret_values = tuple(
        value
        for value in (
            str(client._settings.kis_account_no or ""),
            str(client._settings.kis_app_key or ""),
            str(client._settings.kis_app_secret or ""),
        )
        if value
    )

    exit_code = 0
    markets = ("kr", "us") if market == "both" else (market,)
    for mkt in markets:
        try:
            if mkt == "kr":
                rows = await client.inquire_daily_order_domestic(
                    start_date=start_date,
                    end_date=end_date,
                    is_mock=True,
                )
                summarize = _domestic_row_summary
                tr_id = "VTTC8001R"
            else:
                rows = await client.inquire_daily_order_overseas(
                    start_date=start_date,
                    end_date=end_date,
                    is_mock=True,
                )
                summarize = _overseas_row_summary
                tr_id = "VTTS3035R"
        except Exception as exc:  # noqa: BLE001 - diagnostic script, no fake success
            print(
                json.dumps(
                    {
                        "market": mkt,
                        "tr_id": "VTTC8001R" if mkt == "kr" else "VTTS3035R",
                        "rt_cd": None,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "verdict": "request_failed",
                    },
                    ensure_ascii=False,
                )
            )
            exit_code = max(exit_code, 1)
            continue

        summaries = [
            redact_row(summarize(row), secret_values=secret_values) for row in rows
        ]
        has_remaining = any(
            row["order_status"] == "open_remaining" for row in summaries
        )
        print(
            json.dumps(
                {
                    "market": mkt,
                    "tr_id": tr_id,
                    "rt_cd": "0",
                    "row_count": len(summaries),
                    "any_row_has_remaining_qty": has_remaining,
                    "rows": summaries,
                },
                ensure_ascii=False,
                default=str,
            )
        )
    return exit_code


def build_arg_parser() -> argparse.ArgumentParser:
    today = datetime.now(UTC).strftime("%Y%m%d")
    week_ago = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y%m%d")
    parser = argparse.ArgumentParser(
        prog="kis_mock_open_order_probe",
        description=(
            "ROB-1007 KIS mock open-order read-only TR probe "
            "(default-disabled, no mutation)"
        ),
    )
    parser.add_argument(
        "--market",
        choices=("kr", "us", "both"),
        default="kr",
        help="Which mock order-history TR to call (default kr)",
    )
    parser.add_argument(
        "--start-date", default=week_ago, help="YYYYMMDD (default: 7 days ago)"
    )
    parser.add_argument("--end-date", default=today, help="YYYYMMDD (default: today)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        return asyncio.run(
            run_probe(
                market=args.market,
                start_date=args.start_date,
                end_date=args.end_date,
            )
        )
    except KeyboardInterrupt:
        print("[probe] interrupted")
        return 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
