"""hk 7119 — read-only broker holdings for the daily-loss CLI (ESC 703687).

The CLI (not the monitor package) cross-checks the ledger position against
broker holdings. Brokers are reached only through :func:`read_only_view`,
which exposes the methods in ``ALLOWED_METHODS`` and nothing else (the
``research/data_plane/broker_adapters`` pattern). Broker modules are imported
lazily inside the read functions. Any failed, partial or unparseable read is
``None`` (``holdings_unverified`` -> freeze).
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.symbol import to_db_symbol, to_upbit_symbol
from app.services.daily_loss_monitor.accounts import AccountScope

ALLOWED_METHODS: Mapping[str, frozenset[str]] = {
    # The same KIS balance read as fetch_my_stocks / fetch_my_us_stocks, with
    # the pagination-completion evidence those wrappers discard.
    "kis": frozenset({"fetch_my_stocks_with_completion"}),
    "toss": frozenset({"holdings", "aclose"}),
}


class BrokerMethodRefusedError(AttributeError):
    """The CLI tried to reach a broker method outside ``ALLOWED_METHODS``."""


def read_only_view(broker: str, target: Any) -> Any:
    allowed = ALLOWED_METHODS[broker]

    class _ReadOnlyView:
        __slots__ = ()

        def __getattr__(self, name: str) -> Any:
            if name not in allowed:
                raise BrokerMethodRefusedError(f"{broker}: {name!r} is not allowed")
            return getattr(target, name)

        def __setattr__(self, name: str, value: Any) -> None:
            raise BrokerMethodRefusedError(f"{broker}: the view is read-only")

    return _ReadOnlyView()


def _qty(value: Any) -> Decimal | None:
    try:
        qty = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return qty if qty.is_finite() and qty >= 0 else None


def _rows_to_map(rows: Any, symbol_field: str, qty_field: str, key) -> dict | None:
    if not isinstance(rows, list):
        return None
    held: dict[str, Decimal] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get(symbol_field):
            return None
        qty = _qty(row.get(qty_field))
        if qty is None:
            return None
        symbol = key(str(row[symbol_field]))
        held[symbol] = held.get(symbol, Decimal(0)) + qty
    return held


async def kis_holdings(scope: AccountScope, kis: Any) -> dict[str, Decimal] | None:
    if scope.instrument_type == "equity_kr":
        result = await kis.fetch_my_stocks_with_completion(is_mock=False)
        fields = ("pdno", "hldg_qty", lambda s: s.strip().upper())
    else:
        result = await kis.fetch_my_stocks_with_completion(
            is_mock=False, is_overseas=True, exchange_code="NASD", currency_code="USD"
        )
        fields = (
            "ovrs_pdno",
            "ovrs_cblc_qty",
            lambda s: to_db_symbol(s.strip()).upper(),
        )
    if not isinstance(result, dict) or result.get("complete") is not True:
        return None
    return _rows_to_map(result.get("rows"), *fields)


async def toss_holdings(scope: AccountScope, toss: Any) -> dict[str, Decimal] | None:
    country = "KR" if scope.instrument_type == "equity_kr" else "US"
    result = await toss.holdings()
    held: dict[str, Decimal] = {}
    for item in result.items:
        if item.market_country != country:
            continue
        symbol = str(item.symbol).strip().upper()
        if country == "US":
            symbol = to_db_symbol(symbol).upper()
        qty = _qty(item.quantity)
        if qty is None:
            return None
        held[symbol] = held.get(symbol, Decimal(0)) + qty
    return held


UPBIT_OPEN_ORDERS_PATH = "/v1/orders/open"
_UPBIT_OPEN_PAGE = 100


def upbit_account_map(payload: Any) -> dict[str, Decimal] | None:
    """Strict ``/v1/accounts`` parse: any malformed item makes it unverified."""
    if not isinstance(payload, list):
        return None
    held: dict[str, Decimal] = {}
    for item in payload:
        if not isinstance(item, dict):
            return None
        currency = item.get("currency")
        if not isinstance(currency, str) or not currency.strip():
            return None
        if "balance" not in item or "locked" not in item:
            return None
        balance, locked = _qty(item["balance"]), _qty(item["locked"])
        if balance is None or locked is None:
            return None
        if currency.strip().upper() == "KRW":
            continue
        held[to_upbit_symbol(currency.strip())] = balance + locked
    return held


def upbit_open_orders_clear(payload: Any) -> bool:
    """True only for a complete open-order list with no partial executions.

    The Upbit reconciler books only done/cancel orders, so a partially
    executed open order is a fill the ledger cannot have yet.
    """
    if not isinstance(payload, list) or len(payload) >= _UPBIT_OPEN_PAGE:
        return False
    for order in payload:
        if not isinstance(order, dict):
            return False
        executed = _qty(order.get("executed_volume"))
        if executed is None or executed > 0:
            return False
    return True


async def upbit_holdings(scope: AccountScope) -> dict[str, Decimal] | None:
    from app.services.upbit_wallet_status import ACCOUNTS_PATH, _authed_get

    if not upbit_open_orders_clear(await _authed_get(UPBIT_OPEN_ORDERS_PATH)):
        return None
    return upbit_account_map(await _authed_get(ACCOUNTS_PATH))


async def broker_holdings(scope: AccountScope) -> dict[str, Decimal] | None:
    """Holdings of one account, or None when the read fails or is partial."""
    try:
        if scope.broker == "kis":
            from app.services.brokers.kis.client import KISClient

            account = KISClient(is_mock=False)._account
            return await kis_holdings(scope, read_only_view("kis", account))
        if scope.broker == "toss":
            from app.services.brokers.toss.client import TossReadClient

            toss = read_only_view("toss", TossReadClient.from_settings())
            try:
                return await toss_holdings(scope, toss)
            finally:
                await toss.aclose()
        if scope.broker == "upbit":
            return await upbit_holdings(scope)
    except Exception:  # noqa: BLE001 - a failed read is unverified
        return None
    return None
