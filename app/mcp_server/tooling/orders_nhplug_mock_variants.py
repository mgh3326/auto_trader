"""NHPLUG (NH namuh) Stage 2 mock-account MCP tools (#711).

Operator decision 2026-09-25: mock account only — balance/position, open-order
and fill reads, KRX **limit** order place/modify/cancel, ledger and reconcile.
Excluded: every live account, market orders, scheduler registration, and
account assignment.

Registration: DEFAULT profile only, and only when ``settings.nhplug_mock_enabled``
is true.  No lane allowlist names these tools (assignment is a separate
decision).

Safety layers (each independently tested):

- Master gate ``NHPLUG_MOCK_ENABLED`` re-read at every client dispatch.
- Per-call double gate: every mutation needs ``dry_run=False`` AND
  ``confirm=True``; the tool checks it before building any client, and the
  client dispatcher checks it again before token or socket I/O.  The MCP
  schema uses ``StrictBool``/``StrictInt`` so JSON ``0``/``1``/``"true"`` are
  rejected at validation instead of being coerced into booleans.
- ``order_type`` must be ``limit`` and a limit price is required; market
  orders are refused with ``error_code="limit_orders_only"``.
- Account: fresh ``/n2/acctinfo`` verification per call; only ``acct_type=03``.
- Reads report ``unknown`` rather than an empty success when a listing is
  incomplete or error-shaped.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Literal

from pydantic import StrictBool, StrictInt

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.brokers.nhplug.auth import NHPlugAuthClient
from app.services.brokers.nhplug.client import NHPlugMockClient
from app.services.nhplug_mock import operations
from app.services.nhplug_mock.ledger_service import NHPlugMockLedgerService

if TYPE_CHECKING:
    from fastmcp import FastMCP

NHPLUG_MOCK_MUTATION_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "nhplug_mock_place_order",
        "nhplug_mock_modify_order",
        "nhplug_mock_cancel_order",
    }
)
NHPLUG_MOCK_TOOL_NAMES: frozenset[str] = NHPLUG_MOCK_MUTATION_TOOL_NAMES | {
    "nhplug_mock_preview_order",
    "nhplug_mock_get_positions",
    "nhplug_mock_get_open_orders",
    "nhplug_mock_get_order_history",
    "nhplug_mock_reconcile_orders",
}

# One OAuth client per APP KEY per process: the vendor raises a security alert
# on every token reissue, so tokens are reused for their lifetime.
_AUTH_CLIENTS: dict[str, NHPlugAuthClient] = {}


def _credentials() -> operations.NHPlugMockCredentials | None:
    app_key = settings.nhplug_app_key
    app_secret = settings.nhplug_app_secret
    account_no = settings.nhplug_mock_account_no
    if not all(
        isinstance(value, str) and value.strip()
        for value in (app_key, app_secret, account_no)
    ):
        return None
    return operations.NHPlugMockCredentials(
        app_key=str(app_key).strip(),
        app_secret=str(app_secret).strip(),
        account_no=str(account_no).strip(),
    )


def _config_error() -> dict[str, Any] | None:
    if not settings.nhplug_mock_enabled or _credentials() is None:
        missing = [
            name
            for name, value in (
                ("NHPLUG_MOCK_ENABLED", "x" if settings.nhplug_mock_enabled else ""),
                ("NHPLUG_APP_KEY", settings.nhplug_app_key),
                ("NHPLUG_APP_SECRET", settings.nhplug_app_secret),
                ("NHPLUG_MOCK_ACCOUNT_NO", settings.nhplug_mock_account_no),
            )
            if not (isinstance(value, str) and value.strip())
        ]
        return {
            "success": False,
            "source": operations.SOURCE,
            "account_mode": operations.ACCOUNT_MODE,
            "error_code": "nhplug_mock_config_invalid",
            "error": "NHPLUG mock is not configured; missing: " + ", ".join(missing),
        }
    return None


def _auth_client(credentials: operations.NHPlugMockCredentials) -> NHPlugAuthClient:
    fingerprint = hashlib.sha256(credentials.app_key.encode("utf-8")).hexdigest()
    client = _AUTH_CLIENTS.get(fingerprint)
    if client is None:
        client = NHPlugAuthClient(
            app_key=credentials.app_key, app_secret=credentials.app_secret
        )
        _AUTH_CLIENTS[fingerprint] = client
    return client


async def _verified_client() -> NHPlugMockClient:
    credentials = _credentials()
    if credentials is None:
        raise RuntimeError("NHPLUG mock credentials are not configured")
    return await operations.open_verified_client(
        credentials, token_provider=_auth_client(credentials).get_access_token
    )


@asynccontextmanager
async def _ledger() -> AsyncIterator[NHPlugMockLedgerService]:
    async with AsyncSessionLocal() as session:
        yield NHPlugMockLedgerService(session)


def _confirm_error(tool: str) -> dict[str, Any]:
    return {
        "success": False,
        "source": operations.SOURCE,
        "account_mode": operations.ACCOUNT_MODE,
        "error_code": "confirm_required",
        "error": f"{tool} requires confirm=True when dry_run=False.",
        "dispatch_started": False,
    }


async def _read_with_client(reader: Any) -> dict[str, Any]:
    try:
        client = await _verified_client()
    except Exception as exc:  # noqa: BLE001 - value-free failure surface
        return {
            "success": False,
            "source": operations.SOURCE,
            "account_mode": operations.ACCOUNT_MODE,
            "error_code": "account_verification_failed",
            "error": f"mock account verification failed: {type(exc).__name__}",
        }
    return await reader(client)


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="nhplug_mock_preview_order",
        description=(
            "Preview an NH namuh MOCK KRX limit order offline (no network, no "
            "ledger). Limit orders only; market orders are refused."
        ),
    )
    async def nhplug_mock_preview_order(
        symbol: str,
        side: Literal["buy", "sell"],
        quantity: StrictInt,
        price: StrictInt | None = None,
        order_type: str = "limit",
    ) -> dict[str, Any]:
        if (
            refusal := operations.validate_place_request(
                side=side,
                symbol=symbol,
                quantity=quantity,
                price=price,
                order_type=order_type,
            )
        ) is not None:
            return refusal
        assert price is not None
        return operations.preview_place(
            side=side, symbol=symbol, quantity=quantity, price=price
        )

    @mcp.tool(
        name="nhplug_mock_place_order",
        description=(
            "Place an NH namuh MOCK account KRX limit order. dry_run defaults to "
            "True; sending needs dry_run=False AND confirm=True. Market orders "
            "are refused."
        ),
    )
    async def nhplug_mock_place_order(
        symbol: str,
        side: Literal["buy", "sell"],
        quantity: StrictInt,
        price: StrictInt | None = None,
        order_type: str = "limit",
        dry_run: StrictBool = True,
        confirm: StrictBool = False,
        strategy: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if (
            refusal := operations.validate_place_request(
                side=side,
                symbol=symbol,
                quantity=quantity,
                price=price,
                order_type=order_type,
            )
        ) is not None:
            return refusal
        if dry_run is not False:
            assert price is not None
            return operations.preview_place(
                side=side, symbol=symbol, quantity=quantity, price=price
            )
        if confirm is not True:
            return _confirm_error("nhplug_mock_place_order")
        if (guard := _config_error()) is not None:
            return guard
        async with _ledger() as ledger:
            return await operations.place_limit_order(
                client_factory=_verified_client,
                ledger=ledger,
                side=side,
                symbol=symbol,
                quantity=quantity,
                price=price,
                order_type=order_type,
                dry_run=False,
                confirm=True,
                strategy=strategy,
                reason=reason,
            )

    @mcp.tool(
        name="nhplug_mock_modify_order",
        description=(
            "Modify the limit price (and optionally reduce the quantity) of an "
            "open NH namuh MOCK order. dry_run defaults to True; sending needs "
            "dry_run=False AND confirm=True."
        ),
    )
    async def nhplug_mock_modify_order(
        order_id: str,
        symbol: str,
        new_price: StrictInt | None = None,
        new_quantity: StrictInt | None = None,
        order_type: str = "limit",
        dry_run: StrictBool = True,
        confirm: StrictBool = False,
    ) -> dict[str, Any]:
        if dry_run is False:
            if confirm is not True:
                return _confirm_error("nhplug_mock_modify_order")
            guard = _config_error()
            if guard is not None:
                return guard
        async with _ledger() as ledger:
            return await operations.modify_limit_order(
                client_factory=_verified_client,
                ledger=ledger,
                order_id=order_id,
                symbol=symbol,
                new_price=new_price,
                new_quantity=new_quantity,
                order_type=order_type,
                dry_run=dry_run,
                confirm=confirm,
            )

    @mcp.tool(
        name="nhplug_mock_cancel_order",
        description=(
            "Cancel an open NH namuh MOCK order (full remainder by default). "
            "dry_run defaults to True; sending needs dry_run=False AND confirm=True."
        ),
    )
    async def nhplug_mock_cancel_order(
        order_id: str,
        symbol: str,
        cancel_quantity: StrictInt | None = None,
        dry_run: StrictBool = True,
        confirm: StrictBool = False,
    ) -> dict[str, Any]:
        if dry_run is False:
            if confirm is not True:
                return _confirm_error("nhplug_mock_cancel_order")
            guard = _config_error()
            if guard is not None:
                return guard
        async with _ledger() as ledger:
            return await operations.cancel_order(
                client_factory=_verified_client,
                ledger=ledger,
                order_id=order_id,
                symbol=symbol,
                cancel_quantity=cancel_quantity,
                dry_run=dry_run,
                confirm=confirm,
            )

    @mcp.tool(
        name="nhplug_mock_get_positions",
        description="Read NH namuh MOCK account cash and KR positions (read-only).",
    )
    async def nhplug_mock_get_positions() -> dict[str, Any]:
        if (guard := _config_error()) is not None:
            return guard
        return await _read_with_client(operations.get_positions)

    @mcp.tool(
        name="nhplug_mock_get_open_orders",
        description=(
            "Read NH namuh MOCK open orders from two independent listings. "
            "open_orders_state is present | none_confirmed | unknown; an empty "
            "or error-shaped response is never reported as none_confirmed."
        ),
    )
    async def nhplug_mock_get_open_orders(
        order_date: str | None = None,
    ) -> dict[str, Any]:
        if (guard := _config_error()) is not None:
            return guard
        date = order_date or operations.today_order_date()

        async def read(client: NHPlugMockClient) -> dict[str, Any]:
            async with _ledger() as ledger:
                return await operations.get_open_orders(
                    client, order_date=date, ledger=ledger
                )

        return await _read_with_client(read)

    @mcp.tool(
        name="nhplug_mock_get_order_history",
        description=(
            "Read the NH namuh MOCK daily order/fill listing (read-only). "
            "scope: all | filled | open."
        ),
    )
    async def nhplug_mock_get_order_history(
        order_date: str | None = None,
        scope: Literal["all", "filled", "open"] = "all",
    ) -> dict[str, Any]:
        if (guard := _config_error()) is not None:
            return guard
        date = order_date or operations.today_order_date()

        async def read(client: NHPlugMockClient) -> dict[str, Any]:
            return await operations.get_order_history(
                client, order_date=date, scope=scope
            )

        return await _read_with_client(read)

    @mcp.tool(
        name="nhplug_mock_reconcile_orders",
        description=(
            "Reconcile the NH namuh MOCK ledger against two broker listings. "
            "dry_run defaults to True (plan only). Fills and terminal states are "
            "written only from verified broker evidence."
        ),
    )
    async def nhplug_mock_reconcile_orders(
        order_date: str | None = None,
        dry_run: StrictBool = True,
    ) -> dict[str, Any]:
        if (guard := _config_error()) is not None:
            return guard
        date = order_date or operations.today_order_date()

        async def read(client: NHPlugMockClient) -> dict[str, Any]:
            async with _ledger() as ledger:
                return await operations.reconcile_orders(
                    client, ledger, order_date=date, dry_run=dry_run is not False
                )

        return await _read_with_client(read)
