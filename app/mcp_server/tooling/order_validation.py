"""Order validation, price lookup, and preview logic."""

from __future__ import annotations

import datetime
import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

import app.services.brokers.upbit.client as upbit_service
import app.services.market_data as market_data_service
from app.core.config import settings
from app.mcp_server.caller_identity import get_caller_agent_id, get_caller_source
from app.mcp_server.tooling.kis_mock_ledger import _get_kis_mock_shadow_exposure
from app.mcp_server.tooling.market_data_quotes import (
    _fetch_quote_equity_kr,
    _fetch_quote_equity_us,
)
from app.mcp_server.tooling.market_session import (
    DATA_STATE_PREMARKET_UNAVAILABLE,
    kr_market_data_state,
)
from app.mcp_server.tooling.portfolio_cash import (
    extract_usd_orderable_from_row as _extract_usd_orderable_from_row,
)
from app.mcp_server.tooling.portfolio_cash import (
    get_cash_balance_impl,
)
from app.mcp_server.tooling.portfolio_cash import (
    select_usd_row_for_us_order as _select_usd_row_for_us_order,
)
from app.mcp_server.tooling.shared import logger
from app.mcp_server.tooling.shared import to_float as _to_float
from app.services.brokers.kis import (
    KISClient,
)
from app.services.brokers.upbit.client import (
    parse_upbit_account_row as _parse_upbit_account_row,
)


def _create_kis_client(*, is_mock: bool) -> KISClient:
    if is_mock:
        return KISClient(is_mock=True)
    return KISClient()


async def _call_kis(method: Any, *args: Any, is_mock: bool, **kwargs: Any) -> Any:
    if is_mock:
        return await method(*args, **kwargs, is_mock=True)
    return await method(*args, **kwargs)


_DEFENSIVE_TRIM_APPROVAL_REGEX = re.compile(r"^[A-Z]+-\d+$")
_DEFENSIVE_TRIM_CACHE_TTL_SECONDS = 60.0
_defensive_trim_success_cache: dict[str, float] = {}
_TRADER_AGENT_ID_DEFAULT = "6b2192cc-14fa-4335-b572-2fe1e0cb54a7"


@dataclass(frozen=True)
class DefensiveTrimContext:
    approval_issue_id: str
    requester_agent_id: str
    approval_verified_at: datetime.datetime


@dataclass(frozen=True)
class ScalpingExitContext:
    """Mock-only authorization to sell a scalping position below both the
    avg*1.01 floor and the current-price guard (stop-loss / take-profit /
    time-stop). Only constructible for is_mock=True orders gated by
    KIS_MOCK_SCALPING_ENABLED. Never threaded from any live/generic path.
    """

    strategy_id: str
    reason: str  # "stop_loss" | "take_profit" | "time_stop"


def evaluate_sell_price_guards(
    *,
    price: float,
    current_price: float,
    avg_price: float,
    defensive_trim_ctx: DefensiveTrimContext | None,
    scalping_exit_ctx: ScalpingExitContext | None,
    allow_loss_sell: bool = False,
) -> str | None:
    """Single source of truth for limit-sell price guards.

    Returns an error message if the price violates a guard, else None.

    Matrix:
      - scalping_exit_ctx present  -> both guards bypassed (mock scalping exit).
      - allow_loss_sell True       -> both guards bypassed (ROB-461 kis_mock equity
                                       practice: 손절 / stop-loss / loss rebalancing).
      - defensive_trim_ctx present -> floor bypassed, current-price guard enforced.
      - neither                    -> both guards enforced.
    """
    if scalping_exit_ctx is not None:
        return None
    # ROB-461 — kis_mock is a practice sandbox with no real money, so a loss-sell
    # (below avg*1.01, possibly below current) must be allowed there. The caller
    # scopes this to is_mock AND equity (never crypto/live); see _preview_sell /
    # _validate_sell_side. Operator-requested: mock must let 손절/스톱로스 be practiced.
    if allow_loss_sell:
        return None
    min_sell_price = avg_price * 1.01
    if price < min_sell_price and defensive_trim_ctx is None:
        return (
            f"Sell price {price} below minimum "
            f"(avg_buy_price * 1.01 = {min_sell_price:.0f})"
        )
    if price < current_price:
        return f"Sell price {price} below current price {current_price}"
    return None


def evaluate_market_sell_loss_guard(
    *,
    current_price: float,
    avg_price: float,
    allow_loss_sell: bool = False,
) -> str | None:
    """ROB-518: live market sells must not realize a loss by mistake.

    A market sell executes at ~current_price, so the avg*1.01 floor that guards
    limit sells is applied against the current price here. Mock equity keeps the
    ROB-461 allow_loss_sell bypass (손절 practice). defensive_trim/scalping_exit
    are limit-only by precondition and can never reach the market path. Unknown
    cost basis (avg_price <= 0) stays fail-open, matching the limit-guard
    semantics.
    """
    if allow_loss_sell:
        return None
    if avg_price <= 0:
        return None
    min_sell_price = avg_price * 1.01
    if current_price < min_sell_price:
        return (
            f"Live market sell blocked: current price {current_price} below "
            f"minimum (avg_buy_price * 1.01 = {round(min_sell_price, 4)}). "
            "Loss-selling is disabled on live accounts (ROB-518); use a "
            "defensive_trim limit order for a sanctioned trim."
        )
    return None


async def compute_sector_cluster_weights(
    *, market: str, account_ctx: dict[str, Any]
) -> dict[str, Any]:
    """Best-effort current portfolio weight by sector cluster (KRW).

    ROB-646 — reuses ``get_portfolio_allocation_impl(include_positions=True)`` for
    per-position KRW values + ``usd_krw``, joins each holding's sector label to
    ``sector_cluster_for``, and groups by sector cluster.

    The allocation impl does not currently populate ``sector`` / ``sector_name``
    on position rows, so for any position missing a label we fall back to a
    per-position universe→``symbol_sectors`` lookup via ``_lookup_symbol_sector_label``
    (best-effort, fails open). Without this enrichment the cap silently maps 0
    holdings in prod; with it, real cluster weights are aggregated.

    May raise on any data gap; the orchestrator (``evaluate_sector_concentration``)
    fails open.
    """
    from app.core.db import AsyncSessionLocal
    from app.mcp_server.tooling.portfolio_allocation import (
        get_portfolio_allocation_impl,
    )
    from app.services.trading_policy_service import sector_cluster_for

    # ROB-646 Finding 1: measure against the WHOLE portfolio consistently.
    # Both call sites pass account_ctx with only ``is_mock`` (no account/market
    # filter), so the denominator is the full cross-broker book for that
    # mock/live scope — the KIS and Toss buy paths now agree.
    alloc = await get_portfolio_allocation_impl(
        account=account_ctx.get("account"),
        market=account_ctx.get("market"),
        include_positions=True,
        is_mock=account_ctx.get("is_mock", False),
    )
    usd_krw = float(alloc.get("currency", {}).get("usd_krw") or 0.0)
    positions = alloc.get("positions") or []
    clusters: dict[str, float] = {}
    total = 0.0
    # Positions with no inline sector label need a universe lookup; defer them
    # so a portfolio of already-labelled holdings never opens a DB session.
    pending_lookups: list[tuple[str, str, float]] = []
    for pos in positions:
        value_krw = pos.get("value_krw")
        if value_krw is None:
            continue
        value = float(value_krw)
        total += value
        label = pos.get("sector") or pos.get("sector_name")
        if label is not None:
            cluster = sector_cluster_for(label)
            if cluster is not None:
                clusters[cluster] = clusters.get(cluster, 0.0) + value
            continue
        # ROB-646 Finding 1/5: resolve each holding via its OWN market, not the
        # buy's market, so a US holding is looked up in the US universe.
        pos_market = _position_market(pos) or (
            market if market in ("kr", "us") else None
        )
        symbol_val = pos.get("symbol")
        if pos_market and symbol_val:
            pending_lookups.append((str(symbol_val), pos_market, value))

    if pending_lookups:
        async with AsyncSessionLocal() as db:
            for symbol_val, pos_market, value in pending_lookups:
                label = await _lookup_symbol_sector_label(
                    db, symbol=symbol_val, market=pos_market
                )
                cluster = sector_cluster_for(label)
                if cluster is not None:
                    clusters[cluster] = clusters.get(cluster, 0.0) + value
    return {"clusters": clusters, "total_krw": total, "usd_krw": usd_krw}


async def resolve_symbol_cluster(*, symbol: str, market: str) -> str | None:
    """Resolve a symbol's sector cluster via the universe→symbol_sectors join.

    ROB-646 — best-effort; returns ``None`` on unknown symbol or missing sector.
    May raise on DB errors; the orchestrator fails open.
    """
    from app.core.db import AsyncSessionLocal
    from app.services.trading_policy_service import sector_cluster_for

    async with AsyncSessionLocal() as db:
        label = await _lookup_symbol_sector_label(db, symbol=symbol, market=market)
    return sector_cluster_for(label)


async def evaluate_sector_concentration(
    *,
    symbol: str,
    market: str,
    order_estimated_value: float | None,
    order_currency: str,
    account_ctx: dict[str, Any],
    _weights_provider: Any = compute_sector_cluster_weights,
    _cluster_resolver: Any = resolve_symbol_cluster,
) -> dict[str, Any]:
    """ROB-646 — fail-open sector-cluster concentration check for buy previews.

    Never raises, never blocks. ``verdict == "over"`` produces a soft ``warning``
    field only — Task 5 wires this into the buy-preview path without flipping any
    ``success`` flag. The broad ``except Exception`` catch-all is required by the
    fail-open contract; do not narrow it.
    """
    try:
        if market == "crypto":
            return {
                "verdict": "unknown",
                "fail_open": True,
                "reason": "crypto (no sectors)",
            }
        from app.services.trading_policy_service import get_policy_for

        policy = get_policy_for(market, "buy")
        cap = policy["thresholds"]["portfolio.sector_cluster_cap_pct"]["value"]

        cluster = await _cluster_resolver(symbol=symbol, market=market)
        if cluster is None:
            return {
                "verdict": "unknown",
                "fail_open": True,
                "reason": f"no sector-cluster mapping for {symbol}",
            }

        weights = await _weights_provider(market=market, account_ctx=account_ctx)
        total = float(weights.get("total_krw") or 0.0)
        if total <= 0:
            return {
                "verdict": "unknown",
                "fail_open": True,
                "reason": "empty portfolio total",
            }
        current_value = float(weights.get("clusters", {}).get(cluster, 0.0))
        current_pct = current_value / total * 100.0

        order_krw = _order_value_to_krw(
            order_estimated_value, order_currency, weights.get("usd_krw")
        )
        if order_krw is None:
            projected_pct = current_pct
        else:
            projected_pct = (current_value + order_krw) / (total + order_krw) * 100.0

        result: dict[str, Any] = {
            "verdict": "over" if projected_pct > float(cap) else "within",
            "cluster": cluster,
            "cap_pct": cap,
            "current_pct": round(current_pct, 2),
            "projected_pct": round(projected_pct, 2),
            "fail_open": False,
        }
        if result["verdict"] == "over":
            result["warning"] = (
                f"{cluster} projected {result['projected_pct']}% exceeds "
                f"sector-cluster cap {cap}%"
            )
        return result
    except Exception as exc:  # noqa: BLE001 — fail-open by contract
        return {"verdict": "unknown", "fail_open": True, "reason": str(exc)}


def _is_cached_approved(approval_issue_id: str) -> bool:
    expires_at = _defensive_trim_success_cache.get(approval_issue_id)
    if expires_at is None:
        return False
    if expires_at <= time.time():
        _defensive_trim_success_cache.pop(approval_issue_id, None)
        return False
    return True


def _cache_approved(approval_issue_id: str) -> None:
    _defensive_trim_success_cache[approval_issue_id] = (
        time.time() + _DEFENSIVE_TRIM_CACHE_TTL_SECONDS
    )


def _log_defensive_trim_bypass(
    *,
    symbol: str,
    market_type: str,
    price: float,
    current_price: float,
    avg_price: float,
    min_sell_price: float,
    defensive_trim_ctx: DefensiveTrimContext,
    phase: str,
) -> None:
    logger.warning(
        "defensive_trim_bypass_active: sell floor bypassed",
        extra={
            "symbol": symbol,
            "market_type": market_type,
            "price": price,
            "current_price": current_price,
            "avg_price": avg_price,
            "avg_buy_price": avg_price,
            "min_sell_price": min_sell_price,
            "min_floor": min_sell_price,
            "approval_issue_id": defensive_trim_ctx.approval_issue_id,
            "requester_agent_id": defensive_trim_ctx.requester_agent_id,
            "phase": phase,
        },
    )


def _log_scalping_exit_bypass(
    *,
    symbol: str,
    market_type: str,
    price: float,
    current_price: float,
    avg_price: float,
    scalping_exit_ctx: ScalpingExitContext,
    phase: str,
) -> None:
    logger.warning(
        "kis_mock_scalping_exit_bypass: sell guards bypassed",
        extra={
            "account_mode": "kis_mock",
            "symbol": symbol,
            "market_type": market_type,
            "price": price,
            "current_price": current_price,
            "avg_price": avg_price,
            "strategy_id": scalping_exit_ctx.strategy_id,
            "reason": scalping_exit_ctx.reason,
            "phase": phase,
        },
    )


def _log_mock_loss_sell_bypass(
    *,
    symbol: str,
    market_type: str,
    price: float,
    current_price: float,
    avg_price: float,
    phase: str,
) -> None:
    """ROB-461 — audit a plain kis_mock equity loss-sell bypassing the price guards."""
    logger.warning(
        "kis_mock_loss_sell_bypass: sell price guards bypassed (mock practice)",
        extra={
            "account_mode": "kis_mock",
            "symbol": symbol,
            "market_type": market_type,
            "price": price,
            "current_price": current_price,
            "avg_price": avg_price,
            "phase": phase,
        },
    )


async def _fetch_approval_issue_status(approval_issue_id: str) -> str | None:
    api_url = getattr(settings, "paperclip_api_url", None)
    api_key = getattr(settings, "paperclip_api_key", None)
    if not api_url or not api_key:
        logger.warning(
            "defensive_trim disabled: missing PAPERCLIP_API_URL or PAPERCLIP_API_KEY"
        )
        return None

    issue_api_url = f"{api_url.rstrip('/')}/api/issues/{approval_issue_id}"
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(issue_api_url, headers=headers)
    except Exception:
        return None

    if response.status_code != 200:
        return None

    try:
        payload = response.json()
    except ValueError:
        return None

    status = payload.get("status")
    return str(status) if status is not None else None


async def _validate_defensive_trim_preconditions(
    *,
    defensive_trim: bool,
    approval_issue_id: str | None,
    side: str,
    order_type: str,
) -> DefensiveTrimContext | None:
    """Validate defensive_trim gates using middleware-extracted caller identity."""
    if not defensive_trim:
        return None

    if side != "sell":
        raise ValueError(
            "defensive_trim requires side='sell' (buy orders always use existing path)"
        )
    if order_type != "limit":
        raise ValueError(
            "defensive_trim requires order_type='limit' (market orders are blocked)"
        )
    if not approval_issue_id:
        raise ValueError("defensive_trim=True requires approval_issue_id")
    if not _DEFENSIVE_TRIM_APPROVAL_REGEX.match(approval_issue_id):
        raise ValueError("approval_issue_id format invalid (expected e.g. 'ROB-164')")

    caller_agent_id = get_caller_agent_id()
    if not caller_agent_id:
        raise ValueError(
            "caller identity unavailable — defensive_trim requires authenticated MCP caller"
        )

    trader_agent_id = getattr(settings, "trader_agent_id", _TRADER_AGENT_ID_DEFAULT)
    if caller_agent_id != trader_agent_id:
        raise ValueError(
            "defensive_trim requires Trader agent caller "
            f"(got caller_agent_id={caller_agent_id})"
        )

    approval_status: str | None
    if _is_cached_approved(approval_issue_id):
        approval_status = "done"
    else:
        try:
            approval_status = await _fetch_approval_issue_status(approval_issue_id)
        except Exception:
            approval_status = None
        if approval_status == "done":
            _cache_approved(approval_issue_id)

    if approval_status != "done":
        raise ValueError(
            f"approval_issue_id {approval_issue_id} not found or not in 'done' status"
        )

    return DefensiveTrimContext(
        approval_issue_id=approval_issue_id,
        requester_agent_id=caller_agent_id,
        approval_verified_at=datetime.datetime.now(datetime.UTC),
    )


_SCALPING_EXIT_REASONS = frozenset({"stop_loss", "take_profit", "time_stop"})


def _resolve_scalping_exit_context(
    *,
    scalping_exit: bool,
    strategy_id: str | None,
    reason: str | None,
    side: str,
    order_type: str,
    is_mock: bool,
) -> ScalpingExitContext | None:
    """Fail-closed resolution of a mock scalping exit authorization.

    Returns None when not requested. Raises ValueError on any condition that
    would let a live/generic order acquire the bypass.
    """
    if not scalping_exit:
        return None
    if not settings.kis_mock_scalping_enabled:
        raise ValueError("scalping_exit requires KIS_MOCK_SCALPING_ENABLED=true")
    if not is_mock:
        raise ValueError("scalping_exit is only available for kis_mock orders")
    if side != "sell":
        raise ValueError("scalping_exit requires side='sell'")
    if order_type != "limit":
        raise ValueError("scalping_exit requires order_type='limit'")
    if not strategy_id:
        raise ValueError("scalping_exit requires strategy_id")
    resolved_reason = reason or "stop_loss"
    if resolved_reason not in _SCALPING_EXIT_REASONS:
        raise ValueError(f"invalid scalping_exit reason: {resolved_reason}")
    return ScalpingExitContext(strategy_id=strategy_id, reason=resolved_reason)


async def _premarket_nxt_price_for_kr(symbol: str) -> float | None:
    """ROB-463: live NXT price for a KR equity during pre-market, else None.

    Returns None (so the caller falls back to the KRX quote) when it is not the
    KR pre-market session, when the symbol has no NXT book, or on any fetch error
    — the order path must never be blocked by this best-effort price source.
    Priced as the NXT 예상체결가 (expected_price) when present, else the best
    bid/ask mid (or whichever single side exists).
    """
    if kr_market_data_state() != DATA_STATE_PREMARKET_UNAVAILABLE:
        return None
    try:
        book = await market_data_service.get_orderbook(symbol, "kr", venue="nxt")
    except Exception as exc:  # best-effort; never block the order on pricing
        logger.warning("NXT pre-market price fetch failed for %s: %s", symbol, exc)
        return None
    if book is None or book.is_empty_book:
        return None
    if book.expected_price:
        return float(book.expected_price)
    best_ask = book.asks[0].price if book.asks else None
    best_bid = book.bids[0].price if book.bids else None
    if best_ask and best_bid:
        return (best_ask + best_bid) / 2.0
    single = best_ask or best_bid
    return float(single) if single else None


async def _get_current_price_for_order(symbol: str, market_type: str) -> float | None:
    if market_type == "crypto":
        prices = await upbit_service.fetch_multiple_current_prices(
            [symbol], use_cache=False
        )
        return prices.get(symbol)
    if market_type == "equity_kr":
        # ROB-463: during KR pre-market, the KRX quote is the prior close — using
        # it as "current price" falsely rejected valid pre-market buys. Prefer the
        # live NXT orderbook price when available; otherwise fall back to KRX.
        nxt_price = await _premarket_nxt_price_for_kr(symbol)
        if nxt_price is not None:
            return nxt_price
        quote = await _fetch_quote_equity_kr(symbol)
        return float(quote.get("price")) if quote.get("price") else None

    quote = await _fetch_quote_equity_us(symbol)
    return float(quote.get("price")) if quote.get("price") else None


def _no_holdings_sell_message(symbol: str, market_type: str, is_mock: bool) -> str:
    """Disambiguate a sell-side holdings miss (ROB-420).

    For equities the order tools route only to the KIS subaccount, so a miss may
    mean the symbol is held in another (reference-only) broker subaccount rather
    than not held at all. Crypto routes to Upbit, so keep an Upbit-specific note.
    """
    if market_type == "crypto":
        return f"No holdings found for {symbol} on Upbit"
    channel = "kis_mock" if is_mock else "kis_live"
    if bool(getattr(settings, "toss_api_enabled", False)):
        return (
            f"No sellable holdings for {symbol} in the KIS subaccount that "
            f"{channel} routes to. Toss API and manual Samsung/legacy holdings "
            "are reference-only until their own live-order tools are enabled. "
            "Check get_holdings 'order_routable'/'account_mode'."
        )
    else:
        return (
            f"No sellable holdings for {symbol} in the KIS subaccount that "
            f"{channel} routes to. Holdings in other broker subaccounts "
            f"(e.g. toss/samsung) are reference-only and cannot be sold via this "
            f"channel — check get_holdings 'order_routable'/'account_mode'."
        )


def _order_value_to_krw(
    value: float | None, currency: str, usd_krw: Any
) -> float | None:
    """ROB-646 — convert an order's estimated value to KRW (best-effort).

    Returns ``None`` when the value is ``None`` or the currency is unsupported /
    FX rate unavailable. The caller (``evaluate_sector_concentration``) treats
    ``None`` as "skip the order from the projection" (fail-open).
    """
    if value is None:
        return None
    cur = (currency or "").upper()
    if cur in ("KRW", "₩", ""):
        return float(value)
    if cur in ("USD", "$"):
        rate = float(usd_krw or 0.0)
        return float(value) * rate if rate > 0 else None
    return None


def _position_market(pos: dict[str, Any]) -> str | None:
    """ROB-646 — map a portfolio position's asset class to its sector-lookup market.

    Returns ``kr`` / ``us`` for equities; ``None`` for crypto / cash / other
    (which have no sector cluster). Uses ``effective_asset_class`` (falls back to
    ``surface_asset_class``) from ``get_portfolio_allocation_impl``.
    """
    cls = pos.get("effective_asset_class") or pos.get("surface_asset_class")
    if cls == "kr_equity":
        return "kr"
    if cls == "us_equity":
        return "us"
    return None


async def _lookup_symbol_sector_label(
    db: Any, *, symbol: str, market: str
) -> str | None:
    """ROB-646 — return a symbol's sector label via the universe→symbol_sectors join.

    Best-effort read over existing tables. Returns ``None`` on unknown symbol,
    unknown market, or missing sector. Migration 0 — reads only.
    """
    from sqlalchemy import select

    from app.models.symbol_sectors import SymbolSector

    if market == "kr":
        from app.models.kr_symbol_universe import KRSymbolUniverse as Univ
    elif market == "us":
        from app.core.symbol import to_db_symbol
        from app.models.us_symbol_universe import USSymbolUniverse as Univ

        symbol = to_db_symbol(symbol)
    else:
        return None
    stmt = (
        select(SymbolSector.name_kr, SymbolSector.name_en)
        .join(Univ, Univ.sector_id == SymbolSector.id)
        .where(Univ.symbol == symbol)
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    if not row:
        return None
    return row[0] or row[1]


async def _get_holdings_for_order(
    symbol: str, market_type: str, is_mock: bool = False
) -> dict[str, Any] | None:
    if market_type == "crypto":
        coins = await upbit_service.fetch_my_coins()
        currency = symbol.replace("KRW-", "")
        for coin in coins:
            if coin.get("currency") == currency:
                parsed = _parse_upbit_account_row(coin)
                return {
                    "quantity": parsed["orderable_quantity"],
                    "total_quantity": parsed["total_quantity"],
                    "locked": parsed["locked"],
                    "avg_price": parsed["avg_buy_price"],
                }
        return None

    kis = _create_kis_client(is_mock=is_mock)
    if market_type == "equity_kr":
        stocks = await _call_kis(kis.fetch_my_stocks, is_mock=is_mock)
        for stock in stocks:
            stock_code = str(stock.get("pdno", "")).strip().upper()
            if stock_code != symbol.upper():
                continue
            return {
                "quantity": _to_float(stock.get("hldg_qty"), default=0.0),
                "avg_price": _to_float(stock.get("pchs_avg_pric"), default=0.0),
            }
        return None

    us_stocks = await _call_kis(kis.fetch_my_us_stocks, is_mock=is_mock)
    for stock in us_stocks:
        stock_code = str(stock.get("ovrs_pdno", "")).strip().upper()
        if stock_code != symbol.upper():
            continue
        return {
            "quantity": _to_float(stock.get("ovrs_cblc_qty"), default=0.0),
            "avg_price": _to_float(stock.get("pchs_avg_pric"), default=0.0),
        }
    return None


async def _live_kis_orderable(account_token: str) -> float:
    """live KIS 주문가능(orderable) — 단일 소스 = get_available_capital의 소스.

    ``account_token``: "kis_domestic" (KRW) | "kis_overseas" (USD).
    ROB-596 이후 ``get_cash_balance_impl``은 브로커 주문가능 필드(이미 미체결 매수를
    net한 실시간 값)를 추가 차감 없이 그대로 반환한다(double-count 방지). 따라서
    precheck가 get_available_capital과 동일한 orderable을 본다.
    """
    result = await get_cash_balance_impl(account=account_token, is_mock=False)
    for acc in result.get("accounts", []):
        if acc.get("account") == account_token:
            return float(acc.get("orderable") or 0.0)
    raise RuntimeError(f"{account_token} orderable not found in cash balance")


async def _live_kis_balance_breakdown(
    market_type: str, orderable: float
) -> dict[str, Any] | None:
    """ROB-625 — 잔액부족 에러 진단용 KIS 필드 breakdown (read-only, graceful).

    cash(현금)와 orderable(주문가능)이 어느 KIS 필드에서 왔는지, 둘 중 무엇이 주문을
    막았는지 운영자가 에러메시지만으로 판별하게 한다. equity_us 우선 구현(ROB-625
    재현 케이스). 조회 실패/미지원이면 ``None`` 을 반환해 잔액체크 자체는 절대 막지
    않는다. 잔액부족 에러 경로에서만 1회 호출되므로 추가 조회 비용은 무시 가능.

    ``orderable`` 은 차단 결정에 이미 쓰인 값을 그대로 받아 breakdown에 노출한다.
    재조회 스냅샷에서 다시 읽지 않으므로, 한 에러 detail 안에서 결정값(balance)과
    breakdown의 orderable이 race로 어긋나는 일이 없다.
    """
    if market_type != "equity_us":
        return None
    try:
        result = await get_cash_balance_impl(account="kis_overseas", is_mock=False)
    except Exception as exc:  # graceful: 진단 실패가 주문 가드를 깨면 안 됨
        logger.warning("balance breakdown 조회 실패 (graceful degrade): %s", exc)
        return None
    for acc in result.get("accounts", []):
        if acc.get("account") == "kis_overseas":
            return {
                # cash는 frcr_dncl_amt1 우선, 없으면 frcr_dncl_amt_2 폴백이라 값의
                # 출처를 단정할 수 없으므로 라벨에 두 필드를 모두 명시한다.
                "cash_balance": acc.get("balance"),
                "cash_field": "frcr_dncl_amt1/frcr_dncl_amt_2",
                "orderable": orderable,
                "orderable_field": "frcr_gnrl_ord_psbl_amt",
                "source": "kis_overseas.inquire_overseas_margin",
            }
    return None


def _format_balance_breakdown_suffix(breakdown: dict[str, Any], currency: str) -> str:
    """ROB-625 — KIS 필드 breakdown을 사람이 읽을 에러 접미사로 변환."""
    value_fmt = "{:,.2f}" if currency == "USD" else "{:,.0f}"

    def _fmt(value: Any) -> str:
        return value_fmt.format(value) if isinstance(value, (int, float)) else "unknown"

    return (
        " KIS field breakdown: "
        f"cash_balance({breakdown['cash_field']})={_fmt(breakdown.get('cash_balance'))}, "
        f"orderable({breakdown['orderable_field']})={_fmt(breakdown.get('orderable'))}, "
        f"source={breakdown['source']}."
    )


async def _get_balance_for_order(market_type: str, is_mock: bool = False) -> float:
    if market_type == "crypto":
        coins = await upbit_service.fetch_my_coins()
        for coin in coins:
            if coin.get("currency") == "KRW":
                return float(coin.get("balance", 0))
        return 0.0

    if market_type == "equity_kr":
        if is_mock:
            kis = _create_kis_client(is_mock=is_mock)
            cash_summary = await _call_kis(
                kis.inquire_domestic_cash_balance,
                is_mock=is_mock,
            )
            return float(cash_summary.get("stck_cash_ord_psbl_amt") or 0)
        # ROB-419/596 — live: broker orderable via the single source
        # (== get_available_capital). ROB-596 removed the extra pending-buy
        # subtraction; the broker field is already net (no double-count).
        return await _live_kis_orderable("kis_domestic")

    if not is_mock:
        # ROB-419/596 — live US: broker orderable via the single source
        # (already net of pending buys; no extra subtraction).
        return await _live_kis_orderable("kis_overseas")

    # mock US: KIS 모의투자엔 해외 orderable-cash 서비스가 없음(OPSQ0002). ROB-417
    # 조기 가드가 _check_balance_and_warn에서 선제 차단하므로 여기 도달은 방어적.
    kis = _create_kis_client(is_mock=is_mock)
    margin_data = await _call_kis(kis.inquire_overseas_margin, is_mock=is_mock)
    usd_row = _select_usd_row_for_us_order(margin_data)
    if usd_row is None:
        raise RuntimeError("USD margin data not found in KIS overseas margin")
    return _extract_usd_orderable_from_row(usd_row)


async def _record_order_history(
    symbol: str,
    side: str,
    order_type: str,
    quantity: float | None,
    price: float | None,
    amount: float,
    reason: str,
    dry_run: bool,
    error: str | None = None,
    defensive_trim: bool = False,
    approval_issue_id: str | None = None,
    requester_agent_id: str | None = None,
    caller_source: str | None = None,
) -> None:
    try:
        import redis.asyncio as redis_async

        redis_url = getattr(settings, "redis_url", None)
        if not redis_url:
            return

        redis = await redis_async.from_url(redis_url)
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        key = f"order_history:{today}"
        record = {
            "timestamp": timestamp,
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "quantity": quantity,
            "price": price,
            "amount": amount,
            "reason": reason,
            "dry_run": dry_run,
            "error": error,
            "defensive_trim": defensive_trim,
            "approval_issue_id": approval_issue_id,
            "requester_agent_id": requester_agent_id,
            "caller_source": caller_source or get_caller_source(),
        }

        await redis.rpush(key, json.dumps(record))
        await redis.expire(key, 86400)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Preview helpers (extracted from the monolithic _preview_order)
# ---------------------------------------------------------------------------


async def _preview_buy(
    *,
    symbol: str,
    order_type: str,
    quantity: float | None,
    price: float | None,
    current_price: float,
    market_type: str,
) -> dict[str, Any]:
    """Build a dry-run preview dict for a buy order."""
    result: dict[str, Any] = {
        "symbol": symbol,
        "side": "buy",
        "order_type": order_type,
        "current_price": current_price,
    }

    if order_type == "market":
        result["price"] = current_price
        if price is not None:
            estimated_value = _to_float(price, default=0.0)
        elif quantity is not None:
            estimated_value = current_price * quantity
        else:
            balance = await _get_balance_for_order(market_type)
            if market_type == "crypto":
                min_market_buy_amount = _to_float(
                    getattr(settings, "upbit_buy_amount", 0), default=0.0
                )
            else:
                min_market_buy_amount = 0.0
            estimated_value = (
                balance if balance >= min_market_buy_amount else min_market_buy_amount
            )

        if estimated_value <= 0:
            result["error"] = "order amount must be greater than 0"
            return result

        result["quantity"] = estimated_value / current_price
        result["estimated_value"] = estimated_value
        result["fee"] = estimated_value * 0.0005
        return result

    # Limit buy
    result["price"] = price
    if price is None:
        result["error"] = "price is required for limit buy orders"
        return result
    if price > current_price:
        result["error"] = f"Buy price {price} exceeds current price {current_price}"
        return result
    if quantity is None:
        result["error"] = "quantity is required for limit buy orders"
        return result

    estimated_value = price * quantity
    result["quantity"] = quantity
    result["estimated_value"] = estimated_value
    result["fee"] = estimated_value * 0.0005
    return result


async def _preview_sell(
    *,
    symbol: str,
    order_type: str,
    quantity: float | None,
    price: float | None,
    current_price: float,
    market_type: str,
    defensive_trim_ctx: DefensiveTrimContext | None = None,
    is_mock: bool = False,
    scalping_exit_ctx: ScalpingExitContext | None = None,
) -> dict[str, Any]:
    """Build a dry-run preview dict for a sell order."""
    result: dict[str, Any] = {
        "symbol": symbol,
        "side": "sell",
        "order_type": order_type,
        "current_price": current_price,
    }

    holdings = await _get_holdings_for_order(symbol, market_type, is_mock=is_mock)
    if not holdings:
        result["error"] = _no_holdings_sell_message(symbol, market_type, is_mock)
        return result

    avg_price = holdings["avg_price"]
    if order_type == "market":
        # ROB-518 — market sells used to skip the price guards entirely, letting
        # a live sell realize a loss in one call. Same allow_loss_sell scope as
        # the limit path (mock equity only; crypto/live stay guarded).
        allow_loss_sell = is_mock and market_type in ("equity_kr", "equity_us")
        guard_error = evaluate_market_sell_loss_guard(
            current_price=current_price,
            avg_price=avg_price,
            allow_loss_sell=allow_loss_sell,
        )
        if guard_error is not None:
            result["error"] = guard_error
            return result
        if allow_loss_sell and current_price < avg_price * 1.01:
            _log_mock_loss_sell_bypass(
                symbol=symbol,
                market_type=market_type,
                price=current_price,
                current_price=current_price,
                avg_price=avg_price,
                phase="preview",
            )
        order_quantity = holdings["quantity"]
        execution_price = current_price
        result["price"] = execution_price
    else:
        if price is None:
            result["error"] = "price is required for limit sell orders"
            return result
        # defensive_trim is live-only (Trader-agent + approval); allow_loss_sell is
        # mock-only (is_mock=True, equity). Orthogonal by design. If they ever
        # coexist, allow_loss_sell wins (it early-returns first in the guard), so the
        # logging elif chain below also gates defensive_trim on `not allow_loss_sell`.
        allow_loss_sell = is_mock and market_type in ("equity_kr", "equity_us")
        guard_error = evaluate_sell_price_guards(
            price=price,
            current_price=current_price,
            avg_price=avg_price,
            defensive_trim_ctx=defensive_trim_ctx,
            scalping_exit_ctx=scalping_exit_ctx,
            allow_loss_sell=allow_loss_sell,
        )
        if guard_error is not None:
            result["error"] = guard_error
            return result
        if scalping_exit_ctx is not None and price < avg_price * 1.01:
            _log_scalping_exit_bypass(
                symbol=symbol,
                market_type=market_type,
                price=price,
                current_price=current_price,
                avg_price=avg_price,
                scalping_exit_ctx=scalping_exit_ctx,
                phase="preview",
            )
        elif (
            not allow_loss_sell
            and price < avg_price * 1.01
            and defensive_trim_ctx is not None
        ):
            _log_defensive_trim_bypass(
                symbol=symbol,
                market_type=market_type,
                price=price,
                current_price=current_price,
                avg_price=avg_price,
                min_sell_price=avg_price * 1.01,
                defensive_trim_ctx=defensive_trim_ctx,
                phase="preview",
            )
        elif allow_loss_sell and (price < avg_price * 1.01 or price < current_price):
            _log_mock_loss_sell_bypass(
                symbol=symbol,
                market_type=market_type,
                price=price,
                current_price=current_price,
                avg_price=avg_price,
                phase="preview",
            )
        order_quantity = holdings["quantity"] if quantity is None else quantity
        execution_price = price
        result["price"] = execution_price
        # ROB-477: informational fill-risk surface. A sell limit above the
        # current price can miss the fill entirely if the market reverses.
        if current_price > 0 and price > current_price:
            distance_usd = price - current_price
            result.setdefault("warnings", []).append("sell_limit_above_market")
            result["fill_distance"] = {
                "distance_usd": round(distance_usd, 4),
                "distance_pct": round(distance_usd / current_price * 100.0, 4),
            }

    if defensive_trim_ctx is not None:
        result["defensive_trim"] = True
        result["approval_issue_id"] = defensive_trim_ctx.approval_issue_id

    estimated_value = execution_price * order_quantity
    realized_pnl = (execution_price - avg_price) * order_quantity

    result["quantity"] = order_quantity
    result["estimated_value"] = estimated_value
    result["fee"] = estimated_value * 0.0005
    result["realized_pnl"] = realized_pnl
    result["avg_buy_price"] = avg_price
    return result


async def _preview_order(
    symbol: str,
    side: str,
    order_type: str,
    quantity: float | None,
    price: float | None,
    current_price: float,
    market_type: str,
    defensive_trim_ctx: DefensiveTrimContext | None = None,
    is_mock: bool = False,
    scalping_exit_ctx: ScalpingExitContext | None = None,
) -> dict[str, Any]:
    """Validate order and return a dry-run simulation dict.

    Delegates to _preview_buy / _preview_sell for clarity.
    """
    if side == "buy":
        return await _preview_buy(
            symbol=symbol,
            order_type=order_type,
            quantity=quantity,
            price=price,
            current_price=current_price,
            market_type=market_type,
        )
    return await _preview_sell(
        symbol=symbol,
        order_type=order_type,
        quantity=quantity,
        price=price,
        current_price=current_price,
        market_type=market_type,
        defensive_trim_ctx=defensive_trim_ctx,
        is_mock=is_mock,
        scalping_exit_ctx=scalping_exit_ctx,
    )


# ---------------------------------------------------------------------------
# Helpers extracted from _place_order_impl
# ---------------------------------------------------------------------------


def _resolve_buy_quantity(
    *,
    amount: float | None,
    quantity: float | None,
    order_type: str,
    market_type: str,
    price: float | None,
    current_price: float,
) -> tuple[float | None, float | None]:
    """Convert amount to quantity for buy orders.

    Returns (resolved_quantity, resolved_price).
    resolved_price may be updated for crypto market buys.
    """
    if amount is None:
        return quantity, price

    if order_type == "market" and market_type == "crypto":
        return quantity, amount

    if order_type == "limit" and price is not None:
        qty = amount / price
        if market_type != "crypto":
            qty = int(qty)
        return qty, price

    if current_price <= 0:
        raise ValueError("Failed to get current price for amount conversion")
    qty = amount / current_price
    if qty <= 0:
        raise ValueError(
            f"Calculated quantity {qty} is <= 0. "
            f"Check amount ({amount}) and current price ({current_price})"
        )
    if market_type != "crypto":
        qty = int(qty)
        if qty == 0:
            raise ValueError(
                f"Calculated quantity {qty} is 0. "
                f"Amount {amount} is insufficient for 1 unit at price {current_price}"
            )
    return qty, price


async def _validate_sell_side(
    *,
    symbol: str,
    normalized_symbol: str,
    market_type: str,
    quantity: float | None,
    order_type: str,
    price: float | None,
    current_price: float,
    order_error_fn: Any,
    defensive_trim_ctx: DefensiveTrimContext | None = None,
    is_mock: bool = False,
    dry_run: bool = False,
    scalping_exit_ctx: ScalpingExitContext | None = None,
) -> tuple[float, float, dict[str, Any] | None]:
    """Validate sell-side: check holdings, locked, price constraints.

    Returns (order_quantity, avg_price, error_dict_or_None).
    """
    holdings = await _get_holdings_for_order(
        normalized_symbol,
        market_type,
        is_mock=is_mock,
    )
    if not holdings:
        return (
            0.0,
            0.0,
            order_error_fn(_no_holdings_sell_message(symbol, market_type, is_mock)),
        )

    available_quantity = _to_float(holdings.get("quantity"), default=0.0)
    locked_quantity = _to_float(holdings.get("locked"), default=0.0)

    if is_mock and market_type in ("equity_kr", "equity_us"):
        exposure = await _get_kis_mock_shadow_exposure(
            normalized_symbol=normalized_symbol,
            market_type=market_type,
        )
        if exposure.get("confidence") != "db_shadow_pending":
            message = (
                "KIS mock DB shadow pending confidence unknown; cannot verify "
                "sellable quantity without risking duplicate sell allocation."
            )
            if not dry_run:
                return 0.0, 0.0, order_error_fn(message)
            logger.warning(
                "KIS mock sell preview proceeding without shadow exposure: %s", message
            )
        reserved_qty = _to_float(exposure.get("sell_reserved_quantity"), default=0.0)
        if reserved_qty > 0:
            available_quantity = max(0.0, available_quantity - reserved_qty)
            locked_quantity += reserved_qty

    if quantity is not None and quantity > available_quantity:
        return (
            0.0,
            0.0,
            order_error_fn(
                f"Requested sell quantity {quantity} exceeds orderable balance {available_quantity}. "
                f"locked={locked_quantity} (in open orders, not sellable)."
            ),
        )

    order_quantity = available_quantity if quantity is None else quantity
    avg_price = _to_float(holdings.get("avg_price"), default=0.0)

    if order_type == "market":
        # ROB-518 — mirror the preview-side market loss guard on the execution
        # path (single behavior regardless of entry point).
        allow_loss_sell = is_mock and market_type in ("equity_kr", "equity_us")
        guard_error = evaluate_market_sell_loss_guard(
            current_price=current_price,
            avg_price=avg_price,
            allow_loss_sell=allow_loss_sell,
        )
        if guard_error is not None:
            return 0.0, 0.0, order_error_fn(guard_error)
        if allow_loss_sell and current_price < avg_price * 1.01:
            _log_mock_loss_sell_bypass(
                symbol=normalized_symbol,
                market_type=market_type,
                price=current_price,
                current_price=current_price,
                avg_price=avg_price,
                phase="execution",
            )

    if order_type == "limit" and price is not None:
        # defensive_trim is live-only (Trader-agent + approval); allow_loss_sell is
        # mock-only (is_mock=True, equity). Orthogonal by design. If they ever
        # coexist, allow_loss_sell wins (it early-returns first in the guard), so the
        # logging elif chain below also gates defensive_trim on `not allow_loss_sell`.
        allow_loss_sell = is_mock and market_type in ("equity_kr", "equity_us")
        guard_error = evaluate_sell_price_guards(
            price=price,
            current_price=current_price,
            avg_price=avg_price,
            defensive_trim_ctx=defensive_trim_ctx,
            scalping_exit_ctx=scalping_exit_ctx,
            allow_loss_sell=allow_loss_sell,
        )
        if guard_error is not None:
            return 0.0, 0.0, order_error_fn(guard_error)
        if scalping_exit_ctx is not None and price < avg_price * 1.01:
            _log_scalping_exit_bypass(
                symbol=normalized_symbol,
                market_type=market_type,
                price=price,
                current_price=current_price,
                avg_price=avg_price,
                scalping_exit_ctx=scalping_exit_ctx,
                phase="execution",
            )
        elif (
            not allow_loss_sell
            and price < avg_price * 1.01
            and defensive_trim_ctx is not None
        ):
            _log_defensive_trim_bypass(
                symbol=normalized_symbol,
                market_type=market_type,
                price=price,
                current_price=current_price,
                avg_price=avg_price,
                min_sell_price=avg_price * 1.01,
                defensive_trim_ctx=defensive_trim_ctx,
                phase="execution",
            )
        elif allow_loss_sell and (price < avg_price * 1.01 or price < current_price):
            _log_mock_loss_sell_bypass(
                symbol=normalized_symbol,
                market_type=market_type,
                price=price,
                current_price=current_price,
                avg_price=avg_price,
                phase="execution",
            )

    return order_quantity, avg_price, None


def _kis_mock_us_orderable_unsupported() -> bool:
    """KIS 모의투자가 해외(USD) orderable-cash 서비스를 제공하지 않는지 여부.

    OPSQ0002 "없는 서비스 코드" — 2026-05-27 live smoke로 확정. capability_matrix를
    권위 소스로 사용하므로, 미래에 US mock cash 어댑터가 생겨 account_cash_read=True가
    되면 이 가드는 자동으로 완화된다.
    """
    from app.services.us_dual_paper.capability_matrix import get_capability_matrix

    return get_capability_matrix().get("kis_mock", {}).get("account_cash_read") is False


async def _check_balance_and_warn(
    *,
    market_type: str,
    normalized_symbol: str,
    side: str,
    order_amount: float,
    dry_run: bool,
    order_error_fn: Any,
    is_mock: bool = False,
) -> tuple[str | None, dict[str, Any] | None]:
    """Pre-check cash balance for buy orders.

    Returns (warning_message_or_None, error_dict_or_None).
    If error_dict is not None, the caller should return it immediately.
    """
    # ROB-417 — KIS 모의투자는 해외 orderable-cash 서비스가 없어(OPSQ0002) US mock
    # 매수의 주문가능현금을 검증할 수 없다. capability_matrix 기반으로 KIS 호출 전
    # 결정적으로 fail-closed 처리하고, 구조적 미지원을 mock_unsupported로 명시한다.
    if (
        is_mock
        and market_type == "equity_us"
        and side == "buy"
        and _kis_mock_us_orderable_unsupported()
    ):
        message = (
            "US mock buy unsupported: KIS 모의투자 provides no overseas "
            "orderable-cash service (OPSQ0002), so orderable cash cannot be "
            "verified. Use alpaca_paper for US paper buys; kis_mock supports KR."
        )
        if dry_run:
            return f"Preview warning: {message}", None
        err = order_error_fn(message)
        err["mock_unsupported"] = True
        err["capability"] = "kis_mock_us_orderable_cash_unsupported"
        return None, err

    try:
        balance = await _get_balance_for_order(market_type, is_mock=is_mock)
    except Exception as balance_exc:
        error_detail = str(balance_exc) or balance_exc.__class__.__name__
        logger.error(
            "balance_precheck 조회 실패: stage=balance_query, market_type=%s, symbol=%s, side=%s, error=%s",
            market_type,
            normalized_symbol,
            side,
            error_detail,
        )
        if is_mock and market_type in ("equity_kr", "equity_us"):
            message = (
                "KIS mock balance precheck unavailable for "
                f"{normalized_symbol}: {error_detail}"
            )
            if dry_run:
                return (
                    "Preview warning: "
                    f"{message}; dry_run=True so no order was submitted.",
                    None,
                )
            return (
                None,
                order_error_fn(
                    f"{message}; refusing to submit without verified orderable cash."
                ),
            )
        raise

    if is_mock and market_type in ("equity_kr", "equity_us"):
        exposure = await _get_kis_mock_shadow_exposure(market_type=market_type)
        if exposure.get("confidence") != "db_shadow_pending":
            message = (
                "KIS mock DB shadow pending confidence unknown; cannot verify "
                "orderable cash without risking duplicate buy allocation."
            )
            if not dry_run:
                return None, order_error_fn(message)
            return f"Preview warning: {message}", None
        reserved_amount = _to_float(exposure.get("buy_reserved_amount"), default=0.0)
        if reserved_amount > 0:
            balance = max(0.0, balance - reserved_amount)

    if balance >= order_amount:
        return None, None

    logger.warning(
        "balance_precheck 경고: stage=insufficient_balance_precheck, market_type=%s, symbol=%s, side=%s, balance=%s, order_amount=%s",
        market_type,
        normalized_symbol,
        side,
        balance,
        order_amount,
    )

    # ROB-625 Phase 3 — 진단 가시성: 어떤 KIS 필드가 주문을 막았는지(cash vs orderable)
    # 노출한다. equity_us 우선, graceful None. live 경로에서만 의미가 있으므로
    # is_mock 이면 생략한다.
    breakdown = None
    if not is_mock:
        breakdown = await _live_kis_balance_breakdown(market_type, balance)

    currency = {"crypto": "KRW", "equity_kr": "KRW", "equity_us": "USD"}.get(
        market_type, "USD"
    )

    messages = {
        "crypto": (
            f"Insufficient KRW balance: {balance:,.0f} KRW < {order_amount:,.0f} KRW. "
            "Please deposit KRW from your bank account to Upbit, then retry."
        ),
        "equity_kr": (
            f"Insufficient KRW balance: {balance:,.0f} KRW < {order_amount:,.0f} KRW. "
            "Please deposit funds to your KIS domestic account, then retry."
        ),
        "equity_us": (
            f"Insufficient USD balance: {balance:,.2f} USD < {order_amount:,.2f} USD. "
            "Please deposit USD to your KIS overseas account, then retry."
        ),
    }
    message = messages.get(market_type, messages["equity_us"])
    if breakdown is not None:
        message += _format_balance_breakdown_suffix(breakdown, currency)

    # ROB-625 Phase 2 — dry_run도 live와 동일하게 잔액부족을 차단한다("dry_run 통과 →
    # live 거부" 갭 제거). 차단 플래그 + 구조화된 detail을 첨부하고, 호출자
    # (order_execution)가 dry_run이면 프리뷰 본문을 함께 유지해 운영자가 입금액을
    # 산정할 수 있게 한다. (mock 미지원/조회불가 등 다른 dry_run 경고 경로는 위에서
    # 이미 (warning, None)으로 반환되어 이 분기에 도달하지 않는다.)
    error = order_error_fn(message)
    error["insufficient_balance"] = True
    detail: dict[str, Any] = {
        "balance": balance,
        "order_amount": order_amount,
        "currency": currency,
        "shortfall": max(0.0, order_amount - balance),
    }
    if breakdown is not None:
        detail["breakdown"] = breakdown
    error["insufficient_balance_detail"] = detail
    return None, error
