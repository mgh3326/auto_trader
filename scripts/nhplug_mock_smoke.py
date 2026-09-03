#!/usr/bin/env python
"""NHPLUG mock smoke CLI for bounded reads and operator-run mirror lifecycle.

``order-test`` is an offline preview. ``full`` is the only mutation mode and
requires both NHPLUG gates, ``--confirm-read``, ``--confirm``, and has a wired
finally-cancel before its one-share limit buy can be submitted.

Examples (only after an operator has created the dedicated three-key file):

    NHPLUG_MOCK_ENABLED=true uv run python -m scripts.nhplug_mock_smoke
    NHPLUG_MOCK_ENABLED=true uv run python -m scripts.nhplug_mock_smoke \
      --mode account --confirm-read
    NHPLUG_MOCK_ENABLED=true uv run python -m scripts.nhplug_mock_smoke \
      --mode order-test --symbol 005930 --price 1

Credential values, tokens, account numbers, and broker response bodies are
never printed.  A rejected mock quote exits non-zero rather than faking success.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.services.brokers.nhplug.account_guard import MockAccountAllowlist
from app.services.brokers.nhplug.auth import NHPlugAuthClient
from app.services.brokers.nhplug.client import ALLOWED_READONLY_PATHS, NHPlugMockClient
from app.services.brokers.nhplug.errors import (
    NHPlugMockBrokerRejected,
    NHPlugMockDisabled,
)
from app.services.brokers.nhplug.inquiry import NHDomesticInquiryClient
from app.services.brokers.nhplug.orders import NHDomesticOrderClient

DEFAULT_ENV_FILE = Path(".env.nhplug-mock.native")
REQUIRED_ENV_KEYS = (
    "NHPLUG_APP_KEY",
    "NHPLUG_APP_SECRET",
    "NHPLUG_MOCK_ACCOUNT_NO",
)


class SmokeConfigurationError(RuntimeError):
    """A value-redacted smoke setup error suitable for operator output."""


@dataclass(frozen=True, slots=True)
class ScopedCredentials:
    """Exactly the three values read from the dedicated minimal file."""

    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    account_no: str = field(repr=False)


def _load_minimal_env(path: Path) -> ScopedCredentials:
    """Read exactly three values, rejecting production-like or expanded files."""

    if "prod" in path.name.lower():
        raise SmokeConfigurationError(
            "refusing an env file whose filename contains 'prod'"
        )
    if "prod" in os.getenv("ENV_FILE", "").lower():
        raise SmokeConfigurationError(
            "refusing to run while ENV_FILE names a production file"
        )
    if not path.is_file():
        raise SmokeConfigurationError(
            "dedicated env file is required; expected keys: "
            + ", ".join(REQUIRED_ENV_KEYS)
        )

    values: dict[str, str] = {}
    unexpected: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SmokeConfigurationError(
                "dedicated env file contains a malformed assignment"
            )
        key, _, raw_value = line.partition("=")
        key = key.strip()
        if key not in REQUIRED_ENV_KEYS:
            unexpected.append(key or "<blank>")
            continue
        if key in values:
            raise SmokeConfigurationError(f"dedicated env file repeats key: {key}")
        values[key] = raw_value.strip().strip('"').strip("'")

    if unexpected:
        raise SmokeConfigurationError(
            "dedicated env file must contain only the required keys; unexpected: "
            + ", ".join(sorted(unexpected))
        )
    missing = [key for key in REQUIRED_ENV_KEYS if not values.get(key)]
    if missing:
        raise SmokeConfigurationError(
            "dedicated env file is missing required keys: " + ", ".join(missing)
        )
    return ScopedCredentials(
        app_key=values["NHPLUG_APP_KEY"],
        app_secret=values["NHPLUG_APP_SECRET"],
        account_no=values["NHPLUG_MOCK_ACCOUNT_NO"],
    )


def _emit(payload: dict[str, Any]) -> None:
    """Output only a curated, value-redacted diagnostic record."""

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _response_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Expose only non-sensitive response shape and business code."""

    response_code = payload.get("rsp_cd")
    return {
        "rsp_cd": response_code if isinstance(response_code, str) else "unknown",
        "output_sections": sorted(
            key for key in payload if isinstance(key, str) and key.startswith("Output")
        ),
    }


def _failure_payload(*, mode: str, error: Exception) -> dict[str, Any]:
    """Translate failures without rendering request bodies or credential values."""

    result: dict[str, Any] = {
        "mode": mode,
        "status": "failed",
        "error_type": type(error).__name__,
    }
    if isinstance(error, NHPlugMockBrokerRejected):
        result["broker_response_code"] = error.response_code
    elif isinstance(error, httpx.HTTPStatusError):
        result["http_status"] = error.response.status_code
    elif isinstance(error, (SmokeConfigurationError, NHPlugMockDisabled)):
        # These messages are constructed solely from key names and fixed text.
        result["reason"] = str(error)
    else:
        result["reason"] = (
            "read-only smoke request was rejected before a successful result"
        )
    return result


async def run(args: argparse.Namespace) -> int:
    """Run one bounded read-only mode; all exceptions become sanitized output."""

    try:
        credentials = _load_minimal_env(Path(args.env_file))
        if os.getenv("NHPLUG_MOCK_ENABLED", "").strip().lower() != "true":
            raise NHPlugMockDisabled(
                "NHPLUG_MOCK_ENABLED=true is required; mock reads default to disabled"
            )

        if args.mode == "preflight":
            _emit(
                {
                    "mode": "preflight",
                    "status": "ready",
                    "network_calls": 0,
                    "required_env_keys": list(REQUIRED_ENV_KEYS),
                    "readonly_path_count": len(ALLOWED_READONLY_PATHS),
                    "confirm_read_required_for_network": True,
                }
            )
            return 0

        if not args.confirm_read:
            raise SmokeConfigurationError(
                "--confirm-read is required before a read-only network request"
            )

        auth = NHPlugAuthClient(
            app_key=credentials.app_key,
            app_secret=credentials.app_secret,
        )
        client = NHPlugMockClient(
            app_key=credentials.app_key,
            app_secret=credentials.app_secret,
            token_provider=auth.get_access_token,
        )
        account_payload = await client.list_accounts()
        allowlist = MockAccountAllowlist.from_acctinfo_response(
            payload=account_payload,
            configured_account_no=credentials.account_no,
        )
        client.bind_account_allowlist(allowlist)
        account_summary = {
            "verified_mock_account_count": allowlist.allowed_count,
            "account_type_counts": dict(allowlist.account_type_counts),
        }

        if args.mode == "account":
            balance_payload = await client.fetch_balance(act_no=credentials.account_no)
            _emit(
                {
                    "mode": "account",
                    "status": "ok",
                    "account_verification": account_summary,
                    "balance": _response_summary(balance_payload),
                }
            )
            return 0

        if args.mode == "order-test":
            _emit(
                {
                    "mode": "order-test",
                    "status": "preview",
                    "network_calls": 0,
                    "symbol": args.symbol,
                    "quantity": args.quantity,
                    "price": args.price,
                    "required_confirm_for_full": True,
                }
            )
            return 0

        if args.mode == "full":
            if not args.confirm:
                _emit(
                    {
                        "mode": "full",
                        "status": "stopped",
                        "reason": "--confirm is required; no order sent",
                    }
                )
                return 0
            order_client = NHDomesticOrderClient(
                app_key=credentials.app_key,
                app_secret=credentials.app_secret,
                token_provider=auth.get_access_token,
                account_allowlist=allowlist,
            )
            inquiry = NHDomesticInquiryClient(client)
            order_no: str | None = None
            try:
                accepted = await order_client.place_buy_order(
                    symbol=args.symbol, quantity=args.quantity, price=args.price
                )
                order_no = next(
                    (
                        str(accepted[key])
                        for key in ("orr_no", "ord_no", "order_no")
                        if accepted.get(key) not in (None, "")
                    ),
                    None,
                )
                if order_no is None:
                    raise SmokeConfigurationError(
                        "accepted order has no readable broker order id; manual reconcile required"
                    )
            finally:
                if order_no is not None:
                    await order_client.cancel_order(
                        symbol=args.symbol, original_order_no=order_no
                    )
            evidence = await inquiry.daily_order_execution(
                trade_date=args.trade_date, act_no=credentials.account_no
            )
            _emit(
                {
                    "mode": "full",
                    "status": "ok",
                    "cancel_wired": True,
                    "reconcile": _response_summary(evidence),
                }
            )
            return 0

        quote_payload = await client.fetch_quote(
            symbol=args.symbol,
            market=args.market,
        )
        _emit(
            {
                "mode": "quote",
                "status": "ok",
                "account_verification": account_summary,
                "quote": _response_summary(quote_payload),
            }
        )
        return 0
    except Exception as error:  # noqa: BLE001 - CLI must return a safe failure report.
        _emit(_failure_payload(mode=args.mode, error=error))
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument(
        "--mode",
        choices=("preflight", "account", "quote", "order-test", "full"),
        default="preflight",
    )
    parser.add_argument(
        "--confirm-read",
        action="store_true",
        help="required for account or quote network requests; preflight is always offline",
    )
    parser.add_argument("--symbol", default="005930")
    parser.add_argument("--market", default="KRX")
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--price", type=int, default=1)
    parser.add_argument(
        "--trade-date", default=__import__("datetime").date.today().strftime("%Y%m%d")
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="required for full mock order/cancel lifecycle",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
