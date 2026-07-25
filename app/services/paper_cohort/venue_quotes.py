"""Read-only, venue-specific quote and sizing evidence for ROB-849."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

import httpx

from app.services.brokers.alpaca.config import AlpacaPaperSettings
from app.services.brokers.binance.rest_client import BinancePublicRestClient
from app.services.crypto_execution_mapping import (
    map_binance_public_spot_to_alpaca_paper,
)
from app.services.paper_cohort.contracts import PaperCohortError
from app.services.paper_cohort.signals import VenueQuote

_ALPACA_DATA_HOST = "https://data.alpaca.markets"


def _positive(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PaperCohortError("venue_quote_provider_error") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise PaperCohortError("venue_quote_provider_error")
    return parsed


def _timestamp(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaperCohortError("venue_quote_provider_error") from exc
    if parsed.tzinfo is None:
        raise PaperCohortError("venue_quote_provider_error")
    return parsed


class AlpacaCryptoQuoteClient:
    """Exact-host GET-only client; no account or order endpoints are exposed."""

    def __init__(
        self,
        *,
        data_client: httpx.AsyncClient | None = None,
        asset_client: httpx.AsyncClient | None = None,
        alpaca_settings: AlpacaPaperSettings | None = None,
    ) -> None:
        configured = alpaca_settings or AlpacaPaperSettings.from_app_settings()
        headers = {
            "APCA-API-KEY-ID": configured.api_key,
            "APCA-API-SECRET-KEY": configured.api_secret,
        }
        self._owns_data_client = data_client is None
        self._owns_asset_client = asset_client is None
        self._data = data_client or httpx.AsyncClient(
            base_url=_ALPACA_DATA_HOST, headers=headers
        )
        self._assets = asset_client or httpx.AsyncClient(
            base_url=configured.base_url, headers=headers
        )

    async def quote(self, symbol: str) -> VenueQuote:
        try:
            quote_response = await self._data.get(
                "/v1beta3/crypto/us/latest/quotes", params={"symbols": symbol}
            )
            asset_response = await self._assets.get(
                f"/v2/assets/{symbol.replace('/', '')}"
            )
            quote_response.raise_for_status()
            asset_response.raise_for_status()
            quote_payload = quote_response.json()["quotes"][symbol]
            asset_payload = asset_response.json()
            min_qty = _positive(asset_payload["min_order_size"])
            ask_price = _positive(quote_payload["ap"])
            return VenueQuote(
                venue="alpaca",
                symbol=symbol,
                bid_price=_positive(quote_payload["bp"]),
                ask_price=ask_price,
                bid_qty=_positive(quote_payload["bs"]),
                ask_qty=_positive(quote_payload["as"]),
                fetched_at=_timestamp(quote_payload["t"]),
                qty_increment=_positive(asset_payload["min_trade_increment"]),
                min_qty=min_qty,
                min_notional=min_qty * ask_price,
            )
        except (KeyError, TypeError, httpx.HTTPError) as exc:
            raise PaperCohortError("venue_quote_provider_error") from exc

    async def aclose(self) -> None:
        if self._owns_data_client:
            await self._data.aclose()
        if self._owns_asset_client:
            await self._assets.aclose()


class ProductionVenueQuoteProvider:
    def __init__(
        self,
        binance: BinancePublicRestClient,
        alpaca: AlpacaCryptoQuoteClient,
    ) -> None:
        self._binance = binance
        self._alpaca = alpaca

    async def get_quote(self, venue: str, symbol: str) -> VenueQuote:
        if venue == "alpaca":
            try:
                execution_symbol = map_binance_public_spot_to_alpaca_paper(
                    symbol
                ).execution_symbol
            except ValueError as exc:
                raise PaperCohortError("unsupported_capability") from exc
            return await self._alpaca.quote(execution_symbol)
        if venue != "binance":
            raise PaperCohortError("unsupported_capability")
        try:
            ticker = await self._binance.book_ticker(symbol)
            return VenueQuote(
                venue="binance",
                symbol=symbol,
                bid_price=ticker.bid_price,
                ask_price=ticker.ask_price,
                bid_qty=ticker.bid_qty,
                ask_qty=ticker.ask_qty,
                fetched_at=ticker.fetched_at,
            )
        except PaperCohortError:
            raise
        except Exception as exc:
            raise PaperCohortError("venue_quote_provider_error") from exc


__all__ = ["AlpacaCryptoQuoteClient", "ProductionVenueQuoteProvider"]
