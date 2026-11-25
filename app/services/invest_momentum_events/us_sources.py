"""hk 6510: US intraday movers sources (Alpaca screener, KIS overseas ranking).

Two providers, both official REST endpoints. Each served surface says which one
answered, because the ``surface`` of a run row and of a snapshot row is
``us:<source>:<session>:<side>``.

* **Alpaca** (``SOURCE_ALPACA``) — the paper-account market-data credentials the
  manual paper runner already uses (``scripts/mock_data_sources.py``), two GET
  endpoints on ``https://data.alpaca.markets``:

  - ``/v1beta1/screener/stocks/movers`` — gainers and losers, ``top`` per side.
    ``change`` / ``percent_change`` are vs the previous regular close.
    Documented as resetting at the market open ("Until then, it shows the
    previous market day's movers", docs.alpaca.markets/us/reference/movers-1,
    read 2026-11-25), so a pre-open capture states the provider's own
    ``last_updated`` in ``raw_payload`` instead of claiming it is pre-market
    movement.
  - ``/v1beta1/screener/stocks/most-actives`` — volume and trade count for the
    current trading day (SIP).

  Neither payload carries a dollar volume or an asset class; those are NULL with
  a closed reason in ``field_reasons``, never 0.

* **KIS** (``SOURCE_KIS``) — 해외주식 상승률/하락률, TR ``HHDFS76290000``,
  path ``/uapi/overseas-stock/v1/ranking/updown-rate`` (official sample
  ``examples_llm/overseas_stock/updown_rate/updown_rate.py``; parameters EXCD,
  NDAY, GUBN, VOL_RANG, AUTH, KEYB; output2 row: ``symb``, ``name``, ``last``,
  ``sign``, ``diff``, ``rate``, ``tvol``, ``rank``). NDAY=0 (당일), GUBN=1 상승 /
  0 하락. One call per exchange (NAS/NYS/AMS); the row keeps its exchange in
  ``market_type``. The payload carries no dollar volume.

Nothing here writes, orders or touches the screener pick log, and the module
imports no order/ledger/approval code.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx

from app.core.symbol import to_db_symbol
from app.services.invest_momentum_events.intraday import (
    STATUS_ERROR,
    STATUS_OK,
    SurfaceOutcome,
    classify_error,
)

SOURCE_ALPACA = "alpaca_screener"
SOURCE_KIS = "kis_overseas_ranking"
SOURCES = (SOURCE_ALPACA, SOURCE_KIS)

#: Alpaca market-data host (the trading ``paper-api`` host is not used here).
ALPACA_DATA_HOST = "https://data.alpaca.markets"
ALPACA_MOVERS_PATH = "/v1beta1/screener/stocks/movers"
ALPACA_MOST_ACTIVES_PATH = "/v1beta1/screener/stocks/most-actives"
#: Same env pairs ``scripts/mock_data_refresh`` uses for the paper-data account.
ALPACA_KEY_ENV = "ALPACA_DATA_API_KEY_ID"
ALPACA_SECRET_ENV = "ALPACA_DATA_API_SECRET_KEY"
ALPACA_TOP_MOVERS = 20
ALPACA_TOP_ACTIVES = 50

#: KIS overseas ranking exchanges and the direction code of GUBN.
KIS_EXCHANGES = ("NAS", "NYS", "AMS")
KIS_GUBN_UP = "1"
KIS_GUBN_DOWN = "0"
KIS_NDAY_TODAY = "0"
#: ``VOL_RANG`` 1 = 100 shares or more (drops no-trade rows without hiding size).
KIS_VOL_RANG = "1"
#: A single page per exchange, so a surface never paginates mid-run.
KIS_MAX_ROWS_PER_EXCHANGE = 30

#: Closed reasons for a NULL field. ``not_in_payload`` = the provider does not
#: send this field at all; ``unavailable`` = sent but unreadable; the asset-type
#: reason is ``not_in_symbol_universe``.
REASON_NOT_IN_PAYLOAD = "not_in_payload"
REASON_UNAVAILABLE = "unavailable"
REASON_NOT_IN_SYMBOL_UNIVERSE = "not_in_symbol_universe"
REASON_SOURCE_UNAVAILABLE = "source_unavailable"

#: How a row's dollar volume was obtained (``raw_payload["trade_value_basis"]``).
#: ``provider`` = the payload reports the amount; ``price_times_volume`` = the same
#: row's reported price and volume multiplied (exact decimal arithmetic, no
#: estimate); a row with neither left ``trade_value`` NULL with a reason.
BASIS_PROVIDER = "provider"
BASIS_PRICE_TIMES_VOLUME = "price_times_volume"

#: Closed asset-type vocabulary (contract, hk 6510).
ASSET_COMMON_STOCK = "common_stock"
ASSET_ETF = "etf"
ASSET_LEVERAGED_ETF = "leveraged_etf"
ASSET_RIGHT_OR_WARRANT = "right_or_warrant"
ASSET_UNKNOWN = "unknown"
ASSET_TYPES = (
    ASSET_COMMON_STOCK,
    ASSET_ETF,
    ASSET_LEVERAGED_ETF,
    ASSET_RIGHT_OR_WARRANT,
    ASSET_UNKNOWN,
)

#: ``us_symbol_universe.security_type`` → asset class (Toss master vocabulary).
_COMMON_STOCK_SECURITY_TYPES = frozenset(
    {"STOCK", "FOREIGN_STOCK", "DEPOSITARY_RECEIPT", "REIT"}
)
_FUND_SECURITY_TYPES = frozenset({"ETF", "FOREIGN_ETF", "ETN", "INFRASTRUCTURE_FUND"})
_WARRANT_SECURITY_TYPES = frozenset({"STOCK_WARRANTS"})

_LEVERAGED_NAME_TOKENS = (
    "2x",
    "3x",
    "4x",
    "ultra",
    "inverse",
    "leveraged",
    "bull",
    "bear",
    "-1x",
    "-2x",
    "-3x",
)
_WARRANT_NAME_TOKENS = (
    "warrant",
    "right",
    "unit",
    "preferred",
    "depositary",
)


class AlpacaScreenerReader(Protocol):
    async def screener_movers(self, *, top: int) -> Mapping[str, Any]: ...

    async def screener_most_actives(self, *, top: int) -> Mapping[str, Any]: ...


class KisOverseasRankingReader(Protocol):
    async def inquire_overseas_updown_rate(
        self,
        exchange_code: str,
        *,
        direction: str,
        nday: str = KIS_NDAY_TODAY,
        vol_rang: str = KIS_VOL_RANG,
        keyb: str = "",
    ) -> Mapping[str, Any]: ...


# ------------------------------------------------------------------- credentials


def _credential(value: str | None, name: str) -> str:
    """Header-safe credential; the error names the variable, never the value."""
    text = (value or "").strip()
    if not text or not text.isascii() or any(ord(c) < 33 for c in text):
        raise ValueError(f"missing or invalid {name}")
    return text


def alpaca_data_credentials(
    env: Mapping[str, str] | None = None,
) -> tuple[str, str] | None:
    """``(key_id, secret)`` from the paper-data env pairs, or ``None`` if unset."""
    source = env if env is not None else os.environ
    key = (source.get(ALPACA_KEY_ENV) or "").strip()
    secret = (source.get(ALPACA_SECRET_ENV) or "").strip()
    if not key and not secret:
        return None
    return _credential(key, ALPACA_KEY_ENV), _credential(secret, ALPACA_SECRET_ENV)


class AlpacaScreenerClient:
    """Exact-host, GET-only Alpaca screener client (read-only market data)."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        base_url: str = ALPACA_DATA_HOST,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if base_url.rstrip("/") != ALPACA_DATA_HOST:
            raise ValueError("alpaca screener host must be the market-data host")
        self._headers = {
            "APCA-API-KEY-ID": _credential(api_key, ALPACA_KEY_ENV),
            "APCA-API-SECRET-KEY": _credential(api_secret, ALPACA_SECRET_ENV),
        }
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=ALPACA_DATA_HOST, headers=self._headers, follow_redirects=False
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, path: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        response = await self._client.get(path, params=dict(params))
        # The base URL is fixed; a redirect is the one way a verified request
        # could reach another host, so it is refused before the body is read.
        if response.is_redirect:
            raise ValueError("redirect refused")
        response.raise_for_status()
        media = response.headers.get("content-type", "").split(";")[0].strip()
        if media != "application/json":
            raise ValueError("non-json response")
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("non-object response")
        return payload

    async def screener_movers(
        self, *, top: int = ALPACA_TOP_MOVERS
    ) -> Mapping[str, Any]:
        return await self._get(ALPACA_MOVERS_PATH, {"top": max(int(top), 1)})

    async def screener_most_actives(
        self, *, top: int = ALPACA_TOP_ACTIVES
    ) -> Mapping[str, Any]:
        return await self._get(ALPACA_MOST_ACTIVES_PATH, {"top": max(int(top), 1)})


# --------------------------------------------------------------------- parsing


@dataclass(frozen=True)
class UsRow:
    """One ranking row before the asset class lookup."""

    source: str
    symbol: str
    rank: int
    market_type: str
    name: str | None = None
    price: Decimal | None = None
    change_amount: Decimal | None = None
    change_rate: Decimal | None = None
    volume: int | None = None
    trade_value: Decimal | None = None
    trade_value_basis: str | None = None
    reasons: Mapping[str, str] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def normalize_us_symbol(value: object) -> str | None:
    """Alpaca/KIS symbol -> the DB dot form (``BRK.B``), or ``None``."""
    raw = str(value or "").strip()
    if not raw or any(ch.isspace() for ch in raw):
        return None
    symbol = (to_db_symbol(raw) or "").upper()
    if not symbol or len(symbol) > 20:
        return None
    if not all(ch.isalnum() or ch in ".-" for ch in symbol):
        return None
    return symbol


def _dollar_volume(
    price: Decimal | None, volume: int | None, provided: Decimal | None
) -> tuple[Decimal | None, str | None]:
    """Dollar volume: provider value, else the row's own price x volume."""
    if provided is not None:
        return provided, BASIS_PROVIDER
    if price is not None and volume is not None:
        return price * volume, BASIS_PRICE_TIMES_VOLUME
    return None, None


def _rows_from_list(
    items: Sequence[Any],
    *,
    build,
    limit: int,
) -> tuple[list[UsRow], int]:
    """Parse rows in payload order; ``(rows, skipped)`` with distinct symbols."""
    rows: list[UsRow] = []
    seen: set[str] = set()
    skipped = 0
    for position, item in enumerate(items, start=1):
        row = build(item, position)
        if row is None or row.symbol in seen:
            skipped += 1
            continue
        seen.add(row.symbol)
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows, skipped


def _alpaca_mover_rows(
    payload: Mapping[str, Any], side: str
) -> tuple[list[UsRow], int]:
    key = "gainers" if side == "up" else "losers"
    items = payload.get(key)
    if not isinstance(items, list):
        raise ValueError("missing ranking list")
    response_meta = {
        "last_updated": payload.get("last_updated"),
        "market_type": payload.get("market_type"),
    }

    def build(item: Any, position: int) -> UsRow | None:
        if not isinstance(item, Mapping):
            return None
        symbol = normalize_us_symbol(item.get("symbol"))
        if symbol is None:
            return None
        return UsRow(
            source=SOURCE_ALPACA,
            symbol=symbol,
            rank=position,
            market_type="US",
            price=_decimal(item.get("price")),
            change_amount=_decimal(item.get("change")),
            change_rate=_decimal(item.get("percent_change")),
            reasons={
                "volume": REASON_NOT_IN_PAYLOAD,
                "trade_value": REASON_NOT_IN_PAYLOAD,
            },
            raw={"row": dict(item), "response": response_meta},
        )

    return _rows_from_list(items, build=build, limit=ALPACA_TOP_MOVERS)


def classify_alpaca_movers(payload: object, side: str) -> SurfaceOutcome:
    """Alpaca ``movers`` payload -> ``up``/``down`` outcome (rank = list order)."""
    if not isinstance(payload, Mapping):
        return SurfaceOutcome(STATUS_ERROR, "bad_response")
    try:
        rows, skipped = _alpaca_mover_rows(payload, side)
    except ValueError:
        return SurfaceOutcome(STATUS_ERROR, "unparseable")
    if rows:
        return SurfaceOutcome(STATUS_OK, None, tuple(rows))
    # Only this side's own list decides empty vs unusable: rows that failed to
    # parse (``skipped``) mean the ranking came back non-empty but unusable.
    own = payload.get("gainers" if side == "up" else "losers")
    if own == [] and skipped == 0:
        return SurfaceOutcome("empty", "no_rows")
    return SurfaceOutcome(STATUS_ERROR, "unparseable")


def classify_alpaca_most_actives(payload: object) -> SurfaceOutcome:
    if not isinstance(payload, Mapping):
        return SurfaceOutcome(STATUS_ERROR, "bad_response")
    items = payload.get("most_actives")
    if not isinstance(items, list):
        return SurfaceOutcome(STATUS_ERROR, "unparseable")
    response_meta = {"last_updated": payload.get("last_updated")}

    def build(item: Any, position: int) -> UsRow | None:
        if not isinstance(item, Mapping):
            return None
        symbol = normalize_us_symbol(item.get("symbol"))
        if symbol is None:
            return None
        return UsRow(
            source=SOURCE_ALPACA,
            symbol=symbol,
            rank=position,
            market_type="US",
            volume=_int(item.get("volume")),
            reasons={
                "price": REASON_NOT_IN_PAYLOAD,
                "change_amount": REASON_NOT_IN_PAYLOAD,
                "change_rate": REASON_NOT_IN_PAYLOAD,
                "trade_value": REASON_NOT_IN_PAYLOAD,
            },
            raw={"row": dict(item), "response": response_meta},
        )

    rows, _ = _rows_from_list(items, build=build, limit=ALPACA_TOP_ACTIVES)
    if rows:
        return SurfaceOutcome(STATUS_OK, None, tuple(rows))
    if items == []:
        return SurfaceOutcome("empty", "no_rows")
    return SurfaceOutcome(STATUS_ERROR, "unparseable")


def classify_kis_updown(payload: object, exchange: str) -> SurfaceOutcome:
    """KIS ``updown-rate`` payload -> outcome for one exchange (rank from row)."""
    if not isinstance(payload, Mapping):
        return SurfaceOutcome(STATUS_ERROR, "bad_response")
    items = payload.get("output2")
    if not isinstance(items, list):
        return SurfaceOutcome(STATUS_ERROR, "unparseable")
    summary = payload.get("output1")
    response_meta = {
        "stat": summary.get("stat") if isinstance(summary, Mapping) else None,
        "zdiv": summary.get("zdiv") if isinstance(summary, Mapping) else None,
        "exchange": exchange,
    }

    def build(item: Any, position: int) -> UsRow | None:
        if not isinstance(item, Mapping):
            return None
        symbol = normalize_us_symbol(item.get("symb"))
        if symbol is None:
            return None
        rank = _int(item.get("rank")) or position
        price = _decimal(item.get("last"))
        volume = _int(item.get("tvol"))
        trade_value, basis = _dollar_volume(price, volume, _decimal(item.get("tamt")))
        reasons: dict[str, str] = {}
        if price is None:
            reasons["price"] = REASON_UNAVAILABLE
        if volume is None:
            reasons["volume"] = REASON_UNAVAILABLE
        if trade_value is None:
            reasons["trade_value"] = REASON_NOT_IN_PAYLOAD
        name = str(item.get("name") or "").strip() or None
        change_amount = _decimal(item.get("diff"))
        change_rate = _decimal(item.get("rate"))
        for key, value in (
            ("name", name),
            ("change_amount", change_amount),
            ("change_rate", change_rate),
        ):
            if value is None:
                reasons[key] = REASON_UNAVAILABLE
        return UsRow(
            source=SOURCE_KIS,
            symbol=symbol,
            rank=rank,
            market_type=exchange,
            name=name,
            price=price,
            change_amount=change_amount,
            change_rate=change_rate,
            volume=volume,
            trade_value=trade_value,
            trade_value_basis=basis,
            reasons=reasons,
            raw={"row": dict(item), "response": response_meta},
        )

    rows, _ = _rows_from_list(items, build=build, limit=KIS_MAX_ROWS_PER_EXCHANGE)
    if rows:
        return SurfaceOutcome(STATUS_OK, None, tuple(rows))
    if items == []:
        return SurfaceOutcome("empty", "no_rows")
    return SurfaceOutcome(STATUS_ERROR, "unparseable")


# --------------------------------------------------------------- asset classes


@dataclass(frozen=True)
class SymbolMaster:
    """The columns of ``us_symbol_universe`` an asset class is derived from."""

    security_type: str | None = None
    leverage_factor: Decimal | None = None
    is_common_stock: bool | None = None
    name_en: str | None = None


def _name_has(name: str | None, tokens: Sequence[str]) -> bool:
    text = (name or "").lower()
    return any(token in text for token in tokens)


def _leveraged(master: SymbolMaster) -> bool:
    factor = master.leverage_factor
    # 2x / 3x and inverse (-1x) products both carry decay risk; only exactly 1
    # is a plain fund.
    if factor is not None and factor.is_finite() and factor != 1:
        return True
    return _name_has(master.name_en, _LEVERAGED_NAME_TOKENS)


def derive_asset_type(master: SymbolMaster) -> str:
    """Closed asset class from the US symbol master row (never a guess).

    ``security_type`` is the Toss master vocabulary. A row without one falls back
    to ``is_common_stock`` (NASDAQ Trader classification): ``False`` means "not a
    common share", and the name tokens separate a warrant/right from a fund.
    """
    security_type = (master.security_type or "").strip().upper()
    if security_type in _WARRANT_SECURITY_TYPES:
        return ASSET_RIGHT_OR_WARRANT
    if security_type in _FUND_SECURITY_TYPES:
        return ASSET_LEVERAGED_ETF if _leveraged(master) else ASSET_ETF
    if security_type in _COMMON_STOCK_SECURITY_TYPES:
        return ASSET_COMMON_STOCK
    if security_type:
        return ASSET_UNKNOWN
    if master.is_common_stock is True:
        return ASSET_COMMON_STOCK
    if master.is_common_stock is False:
        # "not a common share" is not fund evidence: the NASDAQ Trader flag is
        # also False for preferreds, depositary shares, notes and trusts. Only a
        # warrant/right name is positive evidence here; everything else stays
        # unknown rather than claiming an ETF.
        if _name_has(master.name_en, _WARRANT_NAME_TOKENS):
            return ASSET_RIGHT_OR_WARRANT
        return ASSET_UNKNOWN
    return ASSET_UNKNOWN


__all__ = [
    "ALPACA_DATA_HOST",
    "ALPACA_KEY_ENV",
    "ALPACA_SECRET_ENV",
    "ASSET_TYPES",
    "BASIS_PRICE_TIMES_VOLUME",
    "BASIS_PROVIDER",
    "ASSET_UNKNOWN",
    "AlpacaScreenerClient",
    "AlpacaScreenerReader",
    "KIS_EXCHANGES",
    "KIS_GUBN_DOWN",
    "KIS_GUBN_UP",
    "KIS_MAX_ROWS_PER_EXCHANGE",
    "KIS_NDAY_TODAY",
    "KIS_VOL_RANG",
    "KisOverseasRankingReader",
    "REASON_NOT_IN_PAYLOAD",
    "REASON_NOT_IN_SYMBOL_UNIVERSE",
    "REASON_SOURCE_UNAVAILABLE",
    "REASON_UNAVAILABLE",
    "SOURCE_ALPACA",
    "SOURCE_KIS",
    "SOURCES",
    "SymbolMaster",
    "UsRow",
    "alpaca_data_credentials",
    "classify_alpaca_most_actives",
    "classify_alpaca_movers",
    "classify_error",
    "classify_kis_updown",
    "derive_asset_type",
    "normalize_us_symbol",
]
