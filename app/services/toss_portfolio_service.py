from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

import sentry_sdk

from app.services.brokers.toss.client import TossReadClient
from app.services.brokers.toss.dto import TossSellableQuantity
from app.services.toss_portfolio_snapshot_cache import TossPortfolioSnapshotCache
from app.services.toss_sellable_cache import TossSellableCache

logger = logging.getLogger(__name__)


class TossPortfolioClient(Protocol):
    async def holdings(self) -> Any: ...
    async def sellable_quantity(self, *, symbol: str) -> Any: ...
    async def buying_power(self, *, currency: str) -> Any: ...
    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class TossPortfolioPosition:
    account: str
    account_name: str
    broker: str
    source: str
    instrument_type: str
    market: str
    symbol: str
    name: str
    quantity: Decimal
    avg_buy_price: Decimal
    current_price: Decimal
    evaluation_amount: Decimal | None
    profit_loss: Decimal | None
    profit_rate: Decimal | None
    sellable_quantity: Decimal | None


@dataclass(frozen=True)
class TossPortfolioSnapshot:
    positions: list[TossPortfolioPosition]
    cash_krw: Decimal | None = None
    cash_usd: Decimal | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class TossCashSnapshot:
    cash_krw: Decimal | None = None
    cash_usd: Decimal | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)


def _instrument_type_for_market_country(market_country: str) -> str:
    normalized = market_country.strip().upper()
    if normalized == "KR":
        return "equity_kr"
    if normalized == "US":
        return "equity_us"
    raise ValueError(f"Unsupported Toss marketCountry: {market_country}")


def _market_for_instrument_type(instrument_type: str) -> str:
    if instrument_type == "equity_kr":
        return "kr"
    if instrument_type == "equity_us":
        return "us"
    raise ValueError(f"Unsupported Toss instrument_type: {instrument_type}")


def _decimal_dict_value(raw: dict[str, Any], key: str) -> Decimal | None:
    value = raw.get(key)
    return value if isinstance(value, Decimal) else None


def _injected_client_snapshot_scope(client: Any) -> str | None:
    """Hashed, non-secret scope for an explicitly injected client, if trusted.

    ROB-1310 R10 (B5): the process-shared snapshot cache's default scope is
    derived from the process's own configured settings
    (``_snapshot_cache_scope``) and is only correct for a client
    ``fetch_toss_portfolio_snapshot`` created itself from those same
    settings. An explicitly injected client (a different account, a
    differently-configured client, or a test fake) has no relationship to
    that scope at all. Such a client may still opt into sharing the
    process-global cache, but only by declaring its own trustworthy identity
    through the ``snapshot_scope_identity`` attribute/property (see
    ``TossReadClient.snapshot_scope_identity``). A client without one --
    including every fake/mock that predates this protocol -- returns
    ``None`` here and must not be scoped at all; see the caller for the
    bypass this triggers. The identity is always hashed before use: the raw
    string must never appear in a Redis key, log, or error.
    """
    identity = getattr(client, "snapshot_scope_identity", None)
    if not isinstance(identity, str) or not identity.strip():
        return None
    return hashlib.sha256(identity.strip().encode("utf-8")).hexdigest()[:16]


async def fetch_toss_cash_snapshot(
    *,
    client: TossPortfolioClient | None = None,
) -> TossCashSnapshot:
    created_client = client is None
    active_client: TossPortfolioClient = client or TossReadClient.from_settings()

    try:
        with sentry_sdk.start_span(
            op="invest.home.toss_api.phase",
            name="invest.home.toss_api.buying_power",
        ) as span:
            span.set_data("currency_count", 2)
            buying_power_results = await asyncio.gather(
                active_client.buying_power(currency="KRW"),
                active_client.buying_power(currency="USD"),
                return_exceptions=True,
            )
            span.set_data(
                "error_count",
                sum(
                    1
                    for result in buying_power_results
                    if isinstance(result, BaseException)
                ),
            )
        cash_krw: Decimal | None = None
        cash_usd: Decimal | None = None
        errors: list[dict[str, Any]] = []
        for currency, result in zip(("KRW", "USD"), buying_power_results, strict=True):
            if isinstance(result, BaseException):
                # ROB-1310 R9 (B3): never persist the raw broker/provider
                # exception text -- this list flows into the shared Redis
                # cash cache payload (fetch_cash_payload) and is replayed
                # verbatim on every later cache hit. A fixed, sanitized code
                # is the only thing that may end up there or in the log.
                logger.warning("Toss buying_power fetch failed for %s", currency)
                errors.append(
                    {
                        "source": "toss_api",
                        "stage": "buying_power",
                        "currency": currency,
                        "error": "toss_buying_power_unavailable",
                    }
                )
                continue
            if result.currency == "KRW":
                cash_krw = result.cash_buying_power
            elif result.currency == "USD":
                cash_usd = result.cash_buying_power

        return TossCashSnapshot(
            cash_krw=cash_krw,
            cash_usd=cash_usd,
            errors=errors,
        )
    finally:
        if created_client:
            await active_client.aclose()


async def _fetch_toss_portfolio_snapshot_uncached(
    *,
    active_client: TossPortfolioClient,
    need_sellable: bool,
    need_cash: bool,
    sellable_cache: TossSellableCache | None = None,
) -> TossPortfolioSnapshot:
    # ROB-707: the cash (buying-power) snapshot is independent of holdings, so
    # kick it off concurrently with the holdings/sellable chain instead of
    # awaiting it serially after the position loop. Output is unchanged; only
    # the wall-clock overlap changes. Drained/cancelled in the finally if the
    # holdings chain raises before we await it.
    # ROB-810: callers that discard cash (MCP get_holdings) pass need_cash=False
    # so the ACCOUNT 1-TPS buying_power fanout (~3.1s) is skipped entirely.
    cash_task: asyncio.Future | None = (
        asyncio.ensure_future(fetch_toss_cash_snapshot(client=active_client))
        if need_cash
        else None
    )

    try:
        with sentry_sdk.start_span(
            op="invest.home.toss_api.phase",
            name="invest.home.toss_api.holdings",
        ) as span:
            holdings = await active_client.holdings()
            span.set_data("position_count", len(holdings.items))

        errors: list[dict[str, Any]] = []

        if need_sellable and sellable_cache is not None:
            # ROB-701: only cache-MISS symbols hit the ORDER_INFO (6 TPS)
            # /sellable-quantity endpoint; hits reuse the cached value. Re-wrap
            # hits as TossSellableQuantity so the position-build loop below is
            # unchanged.
            cache_read = await sellable_cache.read_many(
                [item.symbol for item in holdings.items]
            )
            hits = cache_read.values
            miss_indices = [i for i, hit in enumerate(hits) if hit is None]
            with sentry_sdk.start_span(
                op="invest.home.toss_api.phase",
                name="invest.home.toss_api.sellable_quantity",
            ) as span:
                span.set_data("position_count", len(holdings.items))
                span.set_data("cache_miss_count", len(miss_indices))
                fetched = await asyncio.gather(
                    *[
                        active_client.sellable_quantity(symbol=holdings.items[i].symbol)
                        for i in miss_indices
                    ],
                    return_exceptions=True,
                )
                span.set_data(
                    "error_count",
                    sum(1 for result in fetched if isinstance(result, BaseException)),
                )
            fetched_by_index: dict[int, Any] = dict(
                zip(miss_indices, fetched, strict=True)
            )
            successful_values = {
                holdings.items[index].symbol: result.sellable_quantity
                for index, result in fetched_by_index.items()
                if not isinstance(result, BaseException)
            }
            # Cache ONLY successful fetches — a transient error must not poison
            # the cache (next load retries). One pipeline avoids a Redis N+1.
            await sellable_cache.put_many(
                successful_values,
                expected_generations=cache_read.generations,
            )
            paired: list[tuple[Any, Any]] = []
            for index, item in enumerate(holdings.items):
                if index in fetched_by_index:
                    paired.append((item, fetched_by_index[index]))
                else:
                    paired.append(
                        (item, TossSellableQuantity(sellable_quantity=hits[index]))
                    )
        elif need_sellable:
            with sentry_sdk.start_span(
                op="invest.home.toss_api.phase",
                name="invest.home.toss_api.sellable_quantity",
            ) as span:
                span.set_data("position_count", len(holdings.items))
                sellable_results = await asyncio.gather(
                    *[
                        active_client.sellable_quantity(symbol=item.symbol)
                        for item in holdings.items
                    ],
                    return_exceptions=True,
                )
                span.set_data(
                    "error_count",
                    sum(
                        1
                        for result in sellable_results
                        if isinstance(result, BaseException)
                    ),
                )
            paired = list(zip(holdings.items, sellable_results, strict=True))
        else:
            # ROB-685: caller does not consume sellable_quantity — skip the
            # per-holding GET /sellable-quantity (ORDER_INFO, 6 TPS) fanout that
            # otherwise serializes to ~6/sec and dominates wall time.
            paired = [(item, None) for item in holdings.items]

        positions: list[TossPortfolioPosition] = []
        for item, sellable_result in paired:
            sellable_quantity: Decimal | None = None
            if isinstance(sellable_result, BaseException):
                errors.append(
                    {
                        "source": "toss_api",
                        "stage": "sellable_quantity",
                        "symbol": item.symbol,
                        "error": str(sellable_result),
                    }
                )
            elif sellable_result is not None:
                sellable_quantity = sellable_result.sellable_quantity

            instrument_type = _instrument_type_for_market_country(item.market_country)
            positions.append(
                TossPortfolioPosition(
                    account="toss",
                    account_name="Toss",
                    broker="toss",
                    source="toss_api",
                    instrument_type=instrument_type,
                    market=_market_for_instrument_type(instrument_type),
                    symbol=item.symbol.strip().upper(),
                    name=item.name or item.symbol,
                    quantity=item.quantity,
                    avg_buy_price=item.average_purchase_price,
                    current_price=item.last_price,
                    evaluation_amount=_decimal_dict_value(item.market_value, "amount"),
                    profit_loss=_decimal_dict_value(item.profit_loss, "amount"),
                    profit_rate=_decimal_dict_value(item.profit_loss, "rate"),
                    sellable_quantity=sellable_quantity,
                )
            )

        if cash_task is not None:
            cash_snapshot = await cash_task
            errors.extend(cash_snapshot.errors)
            cash_krw = cash_snapshot.cash_krw
            cash_usd = cash_snapshot.cash_usd
        else:
            cash_krw = None
            cash_usd = None

        return TossPortfolioSnapshot(
            positions=positions,
            cash_krw=cash_krw,
            cash_usd=cash_usd,
            errors=errors,
        )
    finally:
        # ROB-707: if the holdings/sellable chain raised before we awaited the
        # cash task, cancel and drain it so it never touches a closed client
        # (and never leaks a pending task). fetch_toss_cash_snapshot swallows
        # per-currency errors internally, so this only fires on holdings-chain
        # failure.
        if cash_task is not None and not cash_task.done():
            cash_task.cancel()
            with contextlib.suppress(BaseException):
                await cash_task


def _position_to_snapshot_cache(position: TossPortfolioPosition) -> dict[str, Any]:
    """Serialize only the non-sellable portfolio read model."""
    return {
        "account": position.account,
        "account_name": position.account_name,
        "broker": position.broker,
        "source": position.source,
        "instrument_type": position.instrument_type,
        "market": position.market,
        "symbol": position.symbol,
        "name": position.name,
        "quantity": str(position.quantity),
        "avg_buy_price": str(position.avg_buy_price),
        "current_price": str(position.current_price),
        "evaluation_amount": (
            str(position.evaluation_amount)
            if position.evaluation_amount is not None
            else None
        ),
        "profit_loss": (
            str(position.profit_loss) if position.profit_loss is not None else None
        ),
        "profit_rate": (
            str(position.profit_rate) if position.profit_rate is not None else None
        ),
    }


def _decimal_from_snapshot_cache(raw: Any, *, field: str) -> Decimal:
    if raw is None:
        raise ValueError(f"Toss snapshot cache missing {field}")
    try:
        value = Decimal(str(raw))
    except (TypeError, ValueError, ArithmeticError) as exc:
        # decimal.InvalidOperation derives from ArithmeticError, not
        # ValueError: without it a corrupt cached decimal escapes this
        # converter untyped and reads as an upstream failure downstream.
        raise ValueError(f"Toss snapshot cache has invalid {field}") from exc
    if not value.is_finite():
        raise ValueError(f"Toss snapshot cache has non-finite {field}")
    return value


def _optional_decimal_from_snapshot_cache(raw: Any, *, field: str) -> Decimal | None:
    if raw is None:
        return None
    return _decimal_from_snapshot_cache(raw, field=field)


def _contains_forbidden_sellable_key(value: Any) -> bool:
    """Reject every sellability spelling before parsing a cached position."""

    if isinstance(value, dict):
        for key, item in value.items():
            normalized = "".join(char for char in str(key).lower() if char.isalnum())
            if "sellable" in normalized or normalized in {
                "pendingsellquantity",
                "pendingquantity",
            }:
                return True
            if _contains_forbidden_sellable_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_sellable_key(item) for item in value)
    return False


def _position_from_snapshot_cache(raw: Any) -> TossPortfolioPosition:
    if not isinstance(raw, dict):
        raise ValueError("Toss snapshot cache position is not an object")
    # Any sellability key, including a null value or a nested legacy spelling,
    # is a contract violation.  It must not become reconstructible order
    # authority through a future parser change.
    if _contains_forbidden_sellable_key(raw):
        raise ValueError("Toss snapshot cache contains forbidden sellable field")
    try:
        return TossPortfolioPosition(
            account=str(raw["account"]),
            account_name=str(raw["account_name"]),
            broker=str(raw["broker"]),
            source=str(raw["source"]),
            instrument_type=str(raw["instrument_type"]),
            market=str(raw["market"]),
            symbol=str(raw["symbol"]),
            name=str(raw["name"]),
            quantity=_decimal_from_snapshot_cache(
                raw.get("quantity"), field="quantity"
            ),
            avg_buy_price=_decimal_from_snapshot_cache(
                raw.get("avg_buy_price"), field="avg_buy_price"
            ),
            current_price=_decimal_from_snapshot_cache(
                raw.get("current_price"), field="current_price"
            ),
            evaluation_amount=_optional_decimal_from_snapshot_cache(
                raw.get("evaluation_amount"), field="evaluation_amount"
            ),
            profit_loss=_optional_decimal_from_snapshot_cache(
                raw.get("profit_loss"), field="profit_loss"
            ),
            profit_rate=_optional_decimal_from_snapshot_cache(
                raw.get("profit_rate"), field="profit_rate"
            ),
            sellable_quantity=None,
        )
    except KeyError as exc:
        raise ValueError("Toss snapshot cache position is incomplete") from exc


def _positions_from_snapshot_cache(
    payload: dict[str, Any],
) -> list[TossPortfolioPosition]:
    raw_positions = payload.get("positions")
    if not isinstance(raw_positions, list):
        raise ValueError("Toss snapshot cache positions payload is invalid")
    return [_position_from_snapshot_cache(raw) for raw in raw_positions]


def _cash_from_snapshot_cache(payload: dict[str, Any]) -> TossCashSnapshot:
    raw_errors = payload.get("errors", [])
    if not isinstance(raw_errors, list):
        raise ValueError("Toss cash snapshot cache errors payload is invalid")
    return TossCashSnapshot(
        cash_krw=_optional_decimal_from_snapshot_cache(
            payload.get("cash_krw"), field="cash_krw"
        ),
        cash_usd=_optional_decimal_from_snapshot_cache(
            payload.get("cash_usd"), field="cash_usd"
        ),
        errors=[item for item in raw_errors if isinstance(item, dict)],
    )


async def fetch_toss_portfolio_snapshot(
    *,
    need_sellable: bool = False,
    need_cash: bool = True,
    sellable_cache: TossSellableCache | None = None,
    client: TossPortfolioClient | None = None,
    snapshot_cache: TossPortfolioSnapshotCache | None = None,
    use_shared_snapshot: bool | None = None,
) -> TossPortfolioSnapshot:
    """Fetch the Toss portfolio read model.

    General reads default to the process-shared Redis snapshot and never call
    the broker sellable endpoint.  ``need_sellable=True`` is an explicit
    broker-adjacent opt-in and always bypasses the shared snapshot/cache so a
    caller cannot accidentally use stale data for an order decision.
    """
    created_client = client is None
    active_client: TossPortfolioClient = client or TossReadClient.from_settings()

    if use_shared_snapshot is None:
        use_shared_snapshot = created_client and not need_sellable
    if need_sellable:
        use_shared_snapshot = False

    positions_scope = "positions"
    cash_scope = "cash"
    if use_shared_snapshot and not created_client and snapshot_cache is None:
        # ROB-1310 R10 (B5): an explicitly injected client with no explicit
        # snapshot_cache would otherwise fall through to the process-global,
        # settings-derived singleton below -- a scope that has nothing to do
        # with this client's actual identity. Only a client that declares a
        # trustworthy scope may share that singleton (namespaced further by
        # its own hashed identity, never the global scope alone); a client
        # without one must not share/fall back at all.
        injected_scope = _injected_client_snapshot_scope(active_client)
        if injected_scope is None:
            use_shared_snapshot = False
        else:
            positions_scope = f"{injected_scope}:positions"
            cash_scope = f"{injected_scope}:cash"

    try:
        if use_shared_snapshot:
            if snapshot_cache is None:
                from app.services.toss_portfolio_snapshot_cache import (
                    get_shared_portfolio_snapshot_cache,
                )

                snapshot_cache = get_shared_portfolio_snapshot_cache()

            async def fetch_positions_payload() -> dict[str, Any]:
                snapshot = await _fetch_toss_portfolio_snapshot_uncached(
                    active_client=active_client,
                    need_sellable=False,
                    need_cash=False,
                )
                return {
                    "positions": [
                        _position_to_snapshot_cache(position)
                        for position in snapshot.positions
                    ]
                }

            position_task = asyncio.create_task(
                snapshot_cache.get_or_fetch(positions_scope, fetch_positions_payload)
            )

            async def fetch_cash_payload() -> dict[str, Any]:
                cash = await fetch_toss_cash_snapshot(client=active_client)
                return {
                    "cash_krw": (
                        str(cash.cash_krw) if cash.cash_krw is not None else None
                    ),
                    "cash_usd": (
                        str(cash.cash_usd) if cash.cash_usd is not None else None
                    ),
                    "errors": cash.errors,
                }

            cash_task: asyncio.Task[dict[str, Any]] | None = None
            if need_cash:
                cash_task = asyncio.create_task(
                    snapshot_cache.get_or_fetch(cash_scope, fetch_cash_payload)
                )

            try:
                # ROB-1310: an upstream fetch failure is *not* corrupt-cache
                # evidence. Only a deserialization failure may CAS-delete and
                # re-enter the shared singleflight; a broker/Redis fetch error
                # propagates after a single fanout so an outage is not
                # amplified into repeated holdings/buying_power fanouts.
                positions: list[TossPortfolioPosition] | None = None
                for attempt in range(2):
                    positions_payload = (
                        await position_task
                        if attempt == 0
                        else await snapshot_cache.get_or_fetch(
                            positions_scope, fetch_positions_payload
                        )
                    )
                    try:
                        positions = _positions_from_snapshot_cache(positions_payload)
                        break
                    except Exception as exc:  # noqa: BLE001 — corrupt cache only
                        logger.warning(
                            "Toss portfolio snapshot cache payload invalid (%s)",
                            type(exc).__name__,
                        )
                        if isinstance(positions_payload, dict):
                            await snapshot_cache.delete(
                                positions_scope,
                                expected_payload=positions_payload,
                            )
                if positions is None:
                    raise ValueError(
                        "Toss portfolio snapshot recovery payload is invalid"
                    )

                cash_snapshot = TossCashSnapshot()
                cash_errors: list[dict[str, Any]] = []
                if cash_task is not None:
                    parsed_cash: TossCashSnapshot | None = None
                    for attempt in range(2):
                        cash_payload = (
                            await cash_task
                            if attempt == 0
                            else await snapshot_cache.get_or_fetch(
                                cash_scope, fetch_cash_payload
                            )
                        )
                        try:
                            parsed_cash = _cash_from_snapshot_cache(cash_payload)
                            break
                        except Exception as exc:  # noqa: BLE001 — corrupt cache only
                            logger.warning(
                                "Toss cash snapshot cache payload invalid (%s)",
                                type(exc).__name__,
                            )
                            if isinstance(cash_payload, dict):
                                await snapshot_cache.delete(
                                    cash_scope,
                                    expected_payload=cash_payload,
                                )
                    if parsed_cash is None:
                        # ROB-1310 R8: the positions above are already valid.
                        # Raising here made TossApiHomeReader degrade the whole
                        # Toss source to empty accounts/holdings over a
                        # cash-only corruption. Report cash as unknown -- None,
                        # never a fabricated 0 -- plus a sanitized error, and
                        # keep the reconstructed positions. Recovery stayed
                        # bounded at the single re-entry above.
                        logger.warning(
                            "Toss cash snapshot recovery payload is invalid; "
                            "keeping positions and reporting cash as unknown"
                        )
                        cash_errors.append(
                            {
                                "source": "toss_api",
                                "stage": "cash_snapshot",
                                "error": "invalid_cash_snapshot_payload",
                            }
                        )
                    else:
                        cash_snapshot = parsed_cash

                return TossPortfolioSnapshot(
                    positions=positions,
                    cash_krw=cash_snapshot.cash_krw,
                    cash_usd=cash_snapshot.cash_usd,
                    errors=[*cash_snapshot.errors, *cash_errors],
                )
            finally:
                # ROB-707 parity for the shared path: if the positions chain
                # raised before the cash task was awaited, cancel and drain it
                # so it never touches the client the outer finally closes and
                # never leaves an unretrieved task exception.
                for pending_task in (position_task, cash_task):
                    if pending_task is not None and not pending_task.done():
                        pending_task.cancel()
                        with contextlib.suppress(BaseException):
                            await pending_task

        return await _fetch_toss_portfolio_snapshot_uncached(
            active_client=active_client,
            need_sellable=need_sellable,
            need_cash=need_cash,
            sellable_cache=sellable_cache,
        )
    finally:
        if created_client:
            await active_client.aclose()
