"""Canonical composed portfolio snapshot read model.

This module is intentionally read-only.  It serializes the bounded home
projection once into the process-shared portfolio cache and provides the MCP
position projection from that same payload.  Sellable quantity is excluded
from the cache schema; order paths have their own fresh broker authority.
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.core.symbol import to_db_symbol, to_upbit_symbol
from app.mcp_server.tooling.shared import (
    canonical_account_id,
)
from app.schemas.invest_home import InvestHomeResponse

PORTFOLIO_SNAPSHOT_SCHEMA_VERSION = 1


def _is_forbidden_snapshot_field(key: object) -> bool:
    normalized = "".join(char for char in str(key).lower() if char.isalnum())
    return "sellable" in normalized or normalized in {
        "pendingsellquantity",
        "pendingquantity",
    }


def portfolio_snapshot_scope(
    *,
    user_id: int,
    include_paper: bool,
    paper_sources: frozenset[str] | None,
) -> str:
    paper_key = "*" if paper_sources is None else ",".join(sorted(paper_sources))
    digest = hashlib.sha256(paper_key.encode("utf-8")).hexdigest()[:16]
    return f"user:{int(user_id)}:paper:{int(include_paper)}:{digest}"


def _strip_sellable_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_sellable_fields(item)
            for key, item in value.items()
            if not _is_forbidden_snapshot_field(key)
        }
    if isinstance(value, list):
        return [_strip_sellable_fields(item) for item in value]
    return value


HELD_KEY_MARKETS = frozenset({"kr", "us", "crypto"})


def held_key_symbol(market: str, symbol: str) -> str:
    """Canonical held-key symbol for one market.

    This is the single market-aware seam every held-key projection goes
    through -- the snapshot serializer, the cached-payload reader, the manual
    DB reader and the service-level manual fallback -- so calendar/relation
    lookups never compare two dialects of the same holding.

    KR/US use the repository's DB spelling (``BRK-B``/``BRK/B`` -> ``BRK.B``)
    via ``to_db_symbol``.  Crypto uses the Upbit market key (``BTC`` ->
    ``KRW-BTC``) via ``to_upbit_symbol``; ``to_db_symbol`` must never touch a
    crypto symbol because it would rewrite the market separator
    (``KRW-BTC`` -> ``KRW.BTC``).
    """

    normalized = str(symbol).strip()
    if not normalized:
        return ""
    if str(market).lower() == "crypto":
        return to_upbit_symbol(normalized)
    return to_db_symbol(normalized).upper()


def _held_pairs_from_response(response: InvestHomeResponse) -> list[list[str]]:
    all_holdings = [*response.holdings, *response.meta.hiddenHoldings]
    pairs: set[tuple[str, str]] = set()
    for holding in all_holdings:
        market = str(holding.market).lower()
        if holding.quantity <= 0 or market not in HELD_KEY_MARKETS:
            continue
        symbol = held_key_symbol(market, holding.symbol)
        if symbol:
            pairs.add((market, symbol))
    return [[market, symbol] for market, symbol in sorted(pairs)]


def serialize_portfolio_snapshot(response: InvestHomeResponse) -> dict[str, Any]:
    """Build the cache payload and remove all sellable fields recursively."""

    raw_response = _strip_sellable_fields(response.model_dump(mode="json"))
    return {
        "schema_version": PORTFOLIO_SNAPSHOT_SCHEMA_VERSION,
        "held_pairs": _held_pairs_from_response(response),
        "response": raw_response,
    }


def deserialize_portfolio_snapshot(payload: dict[str, Any]) -> InvestHomeResponse:
    if payload.get("schema_version") != PORTFOLIO_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("portfolio snapshot schema version is invalid")
    raw_response = payload.get("response")
    if not isinstance(raw_response, dict):
        raise ValueError("portfolio snapshot response is invalid")
    # Re-validate the no-sellable cache contract before exposing it to callers.
    if _contains_sellable_key(raw_response):
        raise ValueError("portfolio snapshot contains forbidden sellable field")
    return InvestHomeResponse.model_validate(raw_response)


def held_pairs_from_portfolio_snapshot(
    payload: dict[str, Any],
) -> list[tuple[str, str]]:
    raw_pairs = payload.get("held_pairs")
    if not isinstance(raw_pairs, list):
        raise ValueError("portfolio snapshot held_pairs is invalid")
    pairs: set[tuple[str, str]] = set()
    for raw_pair in raw_pairs:
        if not isinstance(raw_pair, list | tuple) or len(raw_pair) != 2:
            raise ValueError("portfolio snapshot held pair is invalid")
        market, symbol = raw_pair
        if not isinstance(market, str) or not isinstance(symbol, str):
            raise ValueError("portfolio snapshot held pair values are invalid")
        normalized_market = market.lower()
        if normalized_market not in HELD_KEY_MARKETS:
            continue
        # Re-normalize on read: a payload written by an older process may still
        # carry a broker-specific spelling.
        normalized_symbol = held_key_symbol(normalized_market, symbol)
        if normalized_symbol:
            pairs.add((normalized_market, normalized_symbol))
    return sorted(pairs)


def _contains_sellable_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            _is_forbidden_snapshot_field(key) or _contains_sellable_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_sellable_key(item) for item in value)
    return False


def _mcp_symbol(symbol: str, instrument_type: str) -> str:
    if instrument_type == "equity_us":
        return to_db_symbol(symbol.strip()).upper()
    if instrument_type == "crypto":
        return to_upbit_symbol(symbol)
    return symbol.strip().upper()


def _mcp_account_details(
    *,
    holding: Any,
    account_by_id: dict[str, Any],
    accounts_by_source: dict[str, list[Any]],
) -> tuple[str, str]:
    source_defaults = {
        "kis": ("kis", "기본 계좌"),
        "upbit": ("upbit", "기본 계좌"),
        "toss_api": ("toss", "Toss"),
        "toss_manual": ("toss", "Toss 수동"),
        "pension_manual": ("samsung_pension", "삼성 연금"),
        "isa_manual": ("isa", "ISA"),
        "kis_manual": ("kis", "기본 계좌"),
        "upbit_manual": ("upbit", "기본 계좌"),
    }
    source = str(holding.source)
    default_account, default_name = source_defaults.get(
        source, (str(holding.accountId), "기본 계좌")
    )
    if source in {"kis", "upbit", "toss_api"}:
        return default_account, default_name

    account = account_by_id.get(str(holding.accountId))
    if account is None:
        source_accounts = accounts_by_source.get(source, [])
        if len(source_accounts) == 1:
            account = source_accounts[0]
    if source in {
        "toss_manual",
        "pension_manual",
        "isa_manual",
        "kis_manual",
        "upbit_manual",
    }:
        account_name = (
            str(account.displayName).strip()
            if account is not None and str(account.displayName).strip()
            else default_name
        )
        broker = {
            "toss_manual": "toss",
            "pension_manual": "samsung",
            "isa_manual": "toss",
            "kis_manual": "kis",
            "upbit_manual": "upbit",
        }.get(source, default_account)
        return canonical_account_id(broker, account_name), account_name
    return (
        default_account,
        account.displayName if account is not None else default_name,
    )


def _mcp_profit_loss(holding: Any) -> float | None:
    source = str(holding.source)
    if source in {
        "upbit",
        "toss_manual",
        "pension_manual",
        "isa_manual",
        "kis_manual",
        "upbit_manual",
    }:
        return None
    if str(holding.market) == "US":
        if holding.pnlNative is not None:
            return float(holding.pnlNative)
        if (
            holding.pnlKrw is not None
            and holding.valueKrw is not None
            and holding.valueNative is not None
            and holding.valueNative > 0
        ):
            # KIS/Toss Home readers retain broker P/L in native units only
            # after converting it to KRW. Reuse the observed FX ratio to
            # recover that exact native amount instead of recomputing from
            # rounded valueNative-costBasis.
            fx_rate = holding.valueKrw / holding.valueNative
            if fx_rate > 0:
                return float(holding.pnlKrw / fx_rate)
        if holding.valueNative is None or holding.costBasis is None:
            return None
        return float(holding.valueNative - holding.costBasis)
    return float(holding.pnlKrw) if holding.pnlKrw is not None else None


def _mcp_profit_rate(holding: Any) -> float | None:
    source = str(holding.source)
    if source in {
        "upbit",
        "toss_manual",
        "pension_manual",
        "isa_manual",
        "kis_manual",
        "upbit_manual",
    }:
        return None
    if holding.pnlRate is None:
        return None
    if source == "toss_api":
        # Toss Home's reader carries the broker percentage-point unit already.
        return float(holding.pnlRate)
    # InvestHome stores ratios (0.00714 == 0.714%); the legacy MCP contract
    # exposes percentage points (0.714). Keep the conversion at this seam.
    return float(holding.pnlRate * 100.0)


def portfolio_snapshot_to_mcp_positions(
    response: InvestHomeResponse,
) -> list[dict[str, Any]]:
    """Project the canonical home holdings into the bounded MCP position shape."""

    accounts = {account.accountId: account for account in response.accounts}
    accounts_by_source: dict[str, list[Any]] = {}
    for account in response.accounts:
        accounts_by_source.setdefault(str(account.source), []).append(account)
    instrument_by_market = {
        "KR": "equity_kr",
        "US": "equity_us",
        "CRYPTO": "crypto",
    }
    source_defaults = {
        "kis": ("kis", "kis_api"),
        "upbit": ("upbit", "upbit_api"),
        "toss_api": ("toss", "toss_api"),
        "toss_manual": ("toss", "manual"),
        "pension_manual": ("samsung", "manual"),
        "isa_manual": ("isa", "manual"),
        "kis_manual": ("kis", "manual"),
        "upbit_manual": ("upbit", "manual"),
    }
    positions: list[dict[str, Any]] = []
    projected_holdings = [*response.holdings, *response.meta.hiddenHoldings]
    seen_holding_ids: set[str] = set()
    for holding in projected_holdings:
        if holding.holdingId in seen_holding_ids:
            continue
        seen_holding_ids.add(holding.holdingId)
        instrument_type = instrument_by_market.get(str(holding.market), "other")
        if instrument_type == "other" or holding.quantity <= 0:
            continue
        broker, source = source_defaults.get(
            holding.source, (holding.source, holding.source)
        )
        account_id, account_name = _mcp_account_details(
            holding=holding,
            account_by_id=accounts,
            accounts_by_source=accounts_by_source,
        )
        current_price = None
        if holding.valueNative is not None and holding.quantity > 0:
            current_price = holding.valueNative / holding.quantity
        positions.append(
            {
                "account": account_id,
                "account_name": account_name,
                "broker": broker,
                "source": source,
                "instrument_type": instrument_type,
                "market": str(holding.market).lower(),
                "symbol": _mcp_symbol(holding.symbol, instrument_type),
                "name": holding.displayName or holding.symbol,
                "quantity": float(holding.quantity),
                "avg_buy_price": float(holding.averageCost or 0.0),
                "current_price": current_price,
                "evaluation_amount": (
                    float(holding.valueNative)
                    if holding.valueNative is not None
                    else None
                ),
                "profit_loss": _mcp_profit_loss(holding),
                "profit_rate": _mcp_profit_rate(holding),
            }
        )
    return positions


async def fetch_uncached_portfolio_snapshot_payload(
    *,
    user_id: int,
    include_paper: bool,
    paper_sources: frozenset[str] | None,
) -> dict[str, Any]:
    """Compose all read-only sources for a cache owner request."""

    from app.core.config import settings
    from app.core.db import AsyncSessionLocal
    from app.services.invest_home_readers import (
        AlpacaPaperHomeReader,
        KISHomeReader,
        KISMockHomeReader,
        ManualHomeReader,
        SafeKISClient,
        TossApiHomeReader,
        UpbitHomeReader,
    )
    from app.services.invest_home_service import InvestHomeService
    from app.services.invest_quote_service import InvestQuoteService

    async with AsyncSessionLocal() as db:
        kis_client = SafeKISClient()
        quote_service = InvestQuoteService(kis_client, db)
        service = InvestHomeService(
            kis_reader=KISHomeReader(db),
            upbit_reader=UpbitHomeReader(db),
            manual_reader=ManualHomeReader(db, quote_service=quote_service),
            toss_api_reader=(
                TossApiHomeReader()
                if bool(getattr(settings, "toss_api_enabled", False))
                else None
            ),
            paper_readers=[KISMockHomeReader(), AlpacaPaperHomeReader()],
        )
        response = await service._get_home_uncached(
            user_id=user_id,
            include_paper=include_paper,
            paper_sources=paper_sources,
        )
    return serialize_portfolio_snapshot(response)


__all__ = [
    "PORTFOLIO_SNAPSHOT_SCHEMA_VERSION",
    "deserialize_portfolio_snapshot",
    "fetch_uncached_portfolio_snapshot_payload",
    "held_key_symbol",
    "held_pairs_from_portfolio_snapshot",
    "portfolio_snapshot_scope",
    "portfolio_snapshot_to_mcp_positions",
    "serialize_portfolio_snapshot",
]
