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
    "kis": frozenset({"fetch_my_stocks", "fetch_my_us_stocks"}),
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
        rows = await kis.fetch_my_stocks(is_mock=False)
        return _rows_to_map(rows, "pdno", "hldg_qty", lambda s: s.strip().upper())
    rows = await kis.fetch_my_us_stocks(is_mock=False, exchange="NASD")
    return _rows_to_map(
        rows, "ovrs_pdno", "ovrs_cblc_qty", lambda s: to_db_symbol(s.strip()).upper()
    )


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


async def upbit_holdings(scope: AccountScope) -> dict[str, Decimal] | None:
    from app.services.upbit_wallet_status import ACCOUNTS_PATH, _authed_get

    payload = await _authed_get(ACCOUNTS_PATH)
    if not isinstance(payload, list):
        return None
    held: dict[str, Decimal] = {}
    for item in payload:
        if not isinstance(item, dict) or item.get("currency") in (None, "KRW"):
            continue
        balance, locked = _qty(item.get("balance", "0")), _qty(item.get("locked", "0"))
        if balance is None or locked is None:
            return None
        held[to_upbit_symbol(str(item["currency"]))] = balance + locked
    return held


async def broker_holdings(scope: AccountScope) -> dict[str, Decimal] | None:
    """Holdings of one account, or None when the read fails or is partial."""
    try:
        if scope.broker == "kis":
            from app.services.brokers.kis.client import KISClient

            return await kis_holdings(
                scope, read_only_view("kis", KISClient(is_mock=False))
            )
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
