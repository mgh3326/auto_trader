"""Upbit per-currency deposit/withdraw wallet status (hk 6372).

Read-only view of ``GET /v1/status/wallet``. Upbit returns one row per
(currency, network) pair — a multi-chain asset such as USDT appears once per
supported ``net_type`` — so consumers get per-network rows plus a per-currency
rollup. ``wallet_state`` uses the documented closed vocabulary
``working`` / ``withdraw_only`` / ``deposit_only`` / ``paused`` /
``unsupported``; anything missing or unrecognized parses to ``unknown`` and is
never treated as ``working``.

Upbit warns the data may lag the real service status by minutes and is for
reference only (https://docs.upbit.com/kr/v1.5.9/reference/입출금-현황).

This module deliberately does not import ``app.services.brokers.upbit``: that
package ``__init__`` imports order submission, and this surface must stay
order-path-free (enforced by ``tests/services/test_upbit_wallet_status.py``).
The JWT signing below mirrors ``brokers/upbit/client.py::_request_with_auth``
for ``GET`` requests without query parameters (no ``query_hash`` needed).
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import jwt

from app.core.async_rate_limiter import get_limiter
from app.core.config import settings
from app.services.brokers.credential_firewall import (
    assert_broker_credentials_allowed,
)

logger = logging.getLogger(__name__)

UPBIT_REST = "https://api.upbit.com"
WALLET_STATUS_PATH = "/v1/status/wallet"
ACCOUNTS_PATH = "/v1/accounts"

#: Documented ``wallet_state`` values
#: (https://docs.upbit.com/kr/v1.5.9/reference/입출금-현황). ``unsupported``
#: means 입출금 미지원 — neither direction is possible, like ``paused``.
KNOWN_WALLET_STATES = frozenset(
    {"working", "withdraw_only", "deposit_only", "paused", "unsupported"}
)
WALLET_STATE_UNKNOWN = "unknown"

#: Closed hold-reason vocabulary for plan/screen consumers. ``None`` from
#: :func:`hold_reason_for_rows` means no hold — every known network row is
#: ``working`` often enough that both directions are verifiably possible.
HOLD_DEPOSIT_WITHDRAW_PAUSED = "deposit_withdraw_paused"
HOLD_WITHDRAW_ONLY = "withdraw_only"
HOLD_DEPOSIT_ONLY = "deposit_only"
HOLD_WALLET_STATUS_UNKNOWN = "wallet_status_unknown"
HOLD_REASONS = frozenset(
    {
        HOLD_DEPOSIT_WITHDRAW_PAUSED,
        HOLD_WITHDRAW_ONLY,
        HOLD_DEPOSIT_ONLY,
        HOLD_WALLET_STATUS_UNKNOWN,
    }
)

_DEPOSIT_CAPABLE = frozenset({"working", "deposit_only"})
_WITHDRAW_CAPABLE = frozenset({"working", "withdraw_only"})

_MAX_ATTEMPTS = 3


class WalletStatusError(RuntimeError):
    """The wallet-status or accounts request failed or returned a bad shape."""


@dataclass(frozen=True, slots=True)
class WalletNetworkRow:
    """One parsed ``/v1/status/wallet`` row — a single currency+network pair."""

    currency: str
    wallet_state: str
    net_type: str | None
    network_name: str | None
    block_state: str | None
    block_height: int | None
    block_updated_at: str | None
    block_elapsed_minutes: int | None


@dataclass(frozen=True, slots=True)
class CurrencyWalletStatus:
    """Per-currency rollup of its network rows plus the closed hold reason."""

    currency: str
    rows: tuple[WalletNetworkRow, ...]
    hold_reason: str | None


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _opt_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_wallet_status(payload: object) -> tuple[WalletNetworkRow, ...]:
    """Strictly parse the ``/v1/status/wallet`` JSON array.

    Rows that are not objects or lack a usable ``currency`` cannot be
    attributed to a coin and are dropped. A missing or unrecognized
    ``wallet_state`` becomes ``unknown`` — never ``working``.
    """
    if not isinstance(payload, list):
        raise WalletStatusError("wallet status response must be a JSON list")
    rows: list[WalletNetworkRow] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        currency = item.get("currency")
        if not isinstance(currency, str) or not currency.strip():
            continue
        state = item.get("wallet_state")
        wallet_state = (
            state
            if isinstance(state, str) and state in KNOWN_WALLET_STATES
            else WALLET_STATE_UNKNOWN
        )
        rows.append(
            WalletNetworkRow(
                currency=currency.strip().upper(),
                wallet_state=wallet_state,
                net_type=_opt_str(item.get("net_type")),
                network_name=_opt_str(item.get("network_name")),
                block_state=_opt_str(item.get("block_state")),
                block_height=_opt_int(item.get("block_height")),
                block_updated_at=_opt_str(item.get("block_updated_at")),
                block_elapsed_minutes=_opt_int(item.get("block_elapsed_minutes")),
            )
        )
    return tuple(rows)


def group_by_currency(
    rows: Sequence[WalletNetworkRow],
) -> dict[str, tuple[WalletNetworkRow, ...]]:
    grouped: dict[str, list[WalletNetworkRow]] = {}
    for row in rows:
        grouped.setdefault(row.currency, []).append(row)
    return {currency: tuple(items) for currency, items in grouped.items()}


def hold_reason_for_rows(rows: Sequence[WalletNetworkRow]) -> str | None:
    """Closed hold reason for one currency's network rows.

    ``None`` means no hold: known rows verifiably allow both deposit and
    withdrawal (any single working network suffices — the operator picks the
    network). Any ``unknown`` row poisons the answer to
    ``wallet_status_unknown`` unless the known rows already prove both
    directions, because an unparsed row might be the only working network.
    """
    if not rows:
        return HOLD_WALLET_STATUS_UNKNOWN
    known = [row for row in rows if row.wallet_state != WALLET_STATE_UNKNOWN]
    deposit_ok = any(row.wallet_state in _DEPOSIT_CAPABLE for row in known)
    withdraw_ok = any(row.wallet_state in _WITHDRAW_CAPABLE for row in known)
    if deposit_ok and withdraw_ok:
        return None
    if len(known) != len(rows):
        return HOLD_WALLET_STATUS_UNKNOWN
    if withdraw_ok:
        return HOLD_WITHDRAW_ONLY
    if deposit_ok:
        return HOLD_DEPOSIT_ONLY
    return HOLD_DEPOSIT_WITHDRAW_PAUSED


def currency_statuses(
    rows: Sequence[WalletNetworkRow],
) -> dict[str, CurrencyWalletStatus]:
    """Per-currency rollup for every currency present in ``rows``."""
    return {
        currency: CurrencyWalletStatus(
            currency=currency,
            rows=items,
            hold_reason=hold_reason_for_rows(items),
        )
        for currency, items in group_by_currency(rows).items()
    }


def status_for_currency(
    statuses: dict[str, CurrencyWalletStatus], currency: str
) -> CurrencyWalletStatus:
    """Status for ``currency``; absent currencies are ``wallet_status_unknown``."""
    key = currency.strip().upper()
    found = statuses.get(key)
    if found is not None:
        return found
    return CurrencyWalletStatus(
        currency=key, rows=(), hold_reason=HOLD_WALLET_STATUS_UNKNOWN
    )


def held_currencies(accounts_payload: object) -> list[str]:
    """Currencies with a nonzero ``/v1/accounts`` balance (KRW excluded).

    Rows without a usable currency or balance are skipped; an unparseable
    balance counts as held so the coin's wallet status is still reported.
    """
    if not isinstance(accounts_payload, list):
        raise WalletStatusError("accounts response must be a JSON list")
    held: list[str] = []
    for item in accounts_payload:
        if not isinstance(item, dict):
            continue
        currency = item.get("currency")
        if not isinstance(currency, str) or not currency.strip():
            continue
        code = currency.strip().upper()
        if code == "KRW":
            continue
        try:
            balance = Decimal(str(item.get("balance", "0")))
            locked = Decimal(str(item.get("locked", "0")))
        except (InvalidOperation, ValueError):
            held.append(code)
            continue
        if balance + locked > 0:
            held.append(code)
    return sorted(set(held))


def _rate_limit_for(api_key: str) -> tuple[int, float]:
    limit_config = settings.upbit_api_rate_limits.get(api_key)
    if limit_config is None:
        return settings.upbit_rate_limit_rate, settings.upbit_rate_limit_period
    return (
        int(limit_config.get("rate", settings.upbit_rate_limit_rate)),
        float(limit_config.get("period", settings.upbit_rate_limit_period)),
    )


async def _authed_get(path: str) -> Any:
    """JWT-authenticated GET for the two read-only account endpoints used here."""
    assert_broker_credentials_allowed("Upbit wallet status request")
    api_key = f"GET {path}"
    rate, period = _rate_limit_for(api_key)
    limiter = await get_limiter("upbit", api_key, rate=rate, period=period)
    url = f"{UPBIT_REST}{path}"

    for attempt in range(_MAX_ATTEMPTS):
        await limiter.acquire(
            blocking_callback=lambda w: logger.warning(
                "[upbit-wallet] rate limit wait: %.3fs (path=%s)", w, path
            )
        )
        token = jwt.encode(
            {"access_key": settings.upbit_access_key, "nonce": str(uuid.uuid4())},
            settings.upbit_secret_key,
        )
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                response = await client.get(
                    url, headers={"Authorization": f"Bearer {token}"}
                )
        except httpx.RequestError as exc:
            if attempt + 1 >= _MAX_ATTEMPTS:
                raise WalletStatusError(
                    f"{path}: request failed: {exc.__class__.__name__}"
                ) from exc
            await asyncio.sleep(0.5 * (2**attempt))
            continue
        if response.status_code == 429 and attempt + 1 < _MAX_ATTEMPTS:
            await asyncio.sleep(0.5 * (2**attempt) + random.uniform(0, 0.1))
            continue
        if response.status_code != 200:
            raise WalletStatusError(f"{path}: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise WalletStatusError(f"{path}: invalid JSON") from exc
    raise WalletStatusError(f"{path}: retries exhausted")


async def fetch_wallet_status() -> tuple[WalletNetworkRow, ...]:
    """Fetch and strictly parse ``GET /v1/status/wallet``."""
    return parse_wallet_status(await _authed_get(WALLET_STATUS_PATH))


async def fetch_accounts() -> list[str]:
    """Fetch ``GET /v1/accounts`` and return held (non-KRW, nonzero) currencies."""
    return held_currencies(await _authed_get(ACCOUNTS_PATH))
