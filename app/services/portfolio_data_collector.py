"""Portfolio data collection — broker/manual asset fetch helpers.

Extracted from PortfolioOverviewService to isolate broker-API side-effects
from aggregation / price-fill logic.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

import app.services.brokers.upbit.client as upbit_service
from app.core.normalizers import to_float as _to_float
from app.models.manual_holdings import MarketType
from app.services.brokers.kis.client import KISClient
from app.services.manual_holdings_service import ManualHoldingsService
from app.services.upbit_symbol_universe_service import get_active_upbit_markets

# Market-type constants (kept in sync with portfolio_overview_service.py)
_MARKET_KR = "KR"
_MARKET_US = "US"
_MARKET_CRYPTO = "CRYPTO"

logger = logging.getLogger(__name__)


def _kis_percent_to_decimal(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value) / 100.0
    except (TypeError, ValueError):
        return None


def _normalize_market_type(value: Any) -> str | None:
    if isinstance(value, MarketType):
        normalized = value.value.upper()
    elif value is None:
        return None
    else:
        normalized = str(value).strip().upper()

    if normalized == "COIN":
        return _MARKET_CRYPTO
    if normalized in {_MARKET_KR, _MARKET_US, _MARKET_CRYPTO}:
        return normalized
    return None


def _normalize_symbol(symbol: str, market_type: str) -> str:
    normalized = str(symbol or "").strip().upper()
    if market_type == _MARKET_CRYPTO:
        if "-" in normalized:
            return normalized
        return f"KRW-{normalized}"
    return normalized


def _log_broker_failure(
    broker_name: str,
    exc: Exception,
    warnings: list[str],
) -> None:
    """Log a broker fetch failure and append a user-facing warning string."""
    logger.warning("Failed to fetch %s holdings: %s", broker_name, exc)
    warnings.append(f"{broker_name} holdings fetch failed: {exc}")


class PortfolioDataCollector:
    """Responsible for fetching raw holding data from each broker."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.manual_holdings_service = ManualHoldingsService(db)

    # ------------------------------------------------------------------
    # Public entry-point (KIS KR + US gathered in parallel)
    # ------------------------------------------------------------------

    async def _collect_kis_components(
        self,
        kis_client: KISClient,
        warnings: list[str],
    ) -> list[dict[str, Any]]:
        collection_results = await asyncio.gather(
            self._collect_kis_kr_components(kis_client, warnings),
            self._collect_kis_us_components(kis_client, warnings),
        )

        components: list[dict[str, Any]] = []
        for result_components, result_warnings in collection_results:
            components.extend(result_components)
            warnings.extend(result_warnings)

        return [item for item in components if item["symbol"]]

    # ------------------------------------------------------------------
    # KIS KR
    # ------------------------------------------------------------------

    async def _collect_kis_kr_components(
        self,
        kis_client: KISClient,
        warnings: list[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        components: list[dict[str, Any]] = []
        local_warnings: list[str] = []
        try:
            kr_stocks = await kis_client.fetch_my_stocks()
            for stock in kr_stocks:
                quantity = _to_float(stock.get("hldg_qty"))
                if quantity <= 0:
                    continue

                avg_price = _to_float(stock.get("pchs_avg_pric"))
                current_price = _to_float(stock.get("prpr"), default=0.0) or None
                evaluation = _to_float(stock.get("evlu_amt"), default=0.0) or None
                profit_loss = _to_float(stock.get("evlu_pfls_amt"), default=0.0)
                profit_rate = _kis_percent_to_decimal(stock.get("evlu_pfls_rt"))

                components.append(
                    {
                        "market_type": _MARKET_KR,
                        "symbol": _normalize_symbol(
                            str(stock.get("pdno", "")), _MARKET_KR
                        ),
                        "name": str(
                            stock.get("prdt_name") or stock.get("pdno") or ""
                        ).strip(),
                        "account_key": "live:kis",
                        "broker": "kis",
                        "account_name": "KIS 실계좌",
                        "source": "live",
                        "quantity": quantity,
                        "avg_price": avg_price,
                        "current_price": current_price,
                        "evaluation": evaluation,
                        "profit_loss": profit_loss,
                        "profit_rate": profit_rate,
                    }
                )
        except Exception as exc:
            _log_broker_failure("KIS KR", exc, local_warnings)
        return components, local_warnings

    # ------------------------------------------------------------------
    # KIS US
    # ------------------------------------------------------------------

    async def _collect_kis_us_components(
        self,
        kis_client: KISClient,
        warnings: list[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        components: list[dict[str, Any]] = []
        local_warnings: list[str] = []
        try:
            us_stocks = await kis_client.fetch_my_us_stocks()
            for stock in us_stocks:
                quantity = _to_float(stock.get("ovrs_cblc_qty"))
                if quantity <= 0:
                    continue

                avg_price = _to_float(stock.get("pchs_avg_pric"))
                current_price = _to_float(stock.get("now_pric2"), default=0.0) or None
                evaluation = (
                    _to_float(stock.get("ovrs_stck_evlu_amt"), default=0.0) or None
                )
                profit_loss = _to_float(stock.get("frcr_evlu_pfls_amt"), default=0.0)
                profit_rate = _kis_percent_to_decimal(stock.get("evlu_pfls_rt"))

                components.append(
                    {
                        "market_type": _MARKET_US,
                        "symbol": _normalize_symbol(
                            str(stock.get("ovrs_pdno", "")), _MARKET_US
                        ),
                        "name": str(
                            stock.get("ovrs_item_name") or stock.get("ovrs_pdno") or ""
                        ).strip(),
                        "account_key": "live:kis",
                        "broker": "kis",
                        "account_name": "KIS 실계좌",
                        "source": "live",
                        "quantity": quantity,
                        "avg_price": avg_price,
                        "current_price": current_price,
                        "evaluation": evaluation,
                        "profit_loss": profit_loss,
                        "profit_rate": profit_rate,
                    }
                )
        except Exception as exc:
            _log_broker_failure("KIS US", exc, local_warnings)
        return components, local_warnings

    # ------------------------------------------------------------------
    # Upbit
    # ------------------------------------------------------------------

    async def _collect_upbit_components(
        self,
        warnings: list[str],
        active_upbit_markets: set[str] | None = None,
        enforce_upbit_universe: bool = True,
    ) -> list[dict[str, Any]]:
        components: list[dict[str, Any]] = []

        try:
            coins = await upbit_service.fetch_my_coins()
        except Exception as exc:
            _log_broker_failure("Upbit", exc, warnings)
            return components

        tradable_set: set[str] | None = None
        if enforce_upbit_universe:
            tradable_set = active_upbit_markets
            if tradable_set is None:
                tradable_set = await get_active_upbit_markets(quote_currency=None)
            tradable_set = {
                str(market).strip().upper()
                for market in tradable_set
                if str(market).strip()
            }

        for coin in coins:
            currency = str(coin.get("currency", "")).strip().upper()
            if not currency or currency == "KRW":
                continue

            unit_currency = str(coin.get("unit_currency") or "KRW").strip().upper()
            symbol = _normalize_symbol(f"{unit_currency}-{currency}", _MARKET_CRYPTO)
            if tradable_set is not None and symbol not in tradable_set:
                logger.info("Skipping non-tradable Upbit holding symbol=%s", symbol)
                continue
            quantity = _to_float(coin.get("balance")) + _to_float(coin.get("locked"))
            if quantity <= 0:
                continue

            components.append(
                {
                    "market_type": _MARKET_CRYPTO,
                    "symbol": symbol,
                    "name": symbol,
                    "account_key": "live:upbit",
                    "broker": "upbit",
                    "account_name": "Upbit 실계좌",
                    "source": "live",
                    "quantity": quantity,
                    "avg_price": _to_float(coin.get("avg_buy_price")),
                    "current_price": None,
                    "evaluation": None,
                    "profit_loss": None,
                    "profit_rate": None,
                }
            )

        # Price fill for Upbit components is handled by PortfolioOverviewService
        # (_fetch_upbit_prices_resilient / _fill_missing_crypto_prices).
        return components

    # ------------------------------------------------------------------
    # Manual holdings
    # ------------------------------------------------------------------

    async def _collect_manual_components(
        self,
        user_id: int,
        warnings: list[str],
        active_upbit_markets: set[str] | None = None,
        enforce_upbit_universe: bool = True,
    ) -> list[dict[str, Any]]:
        try:
            holdings = await self.manual_holdings_service.get_holdings_by_user(user_id)
        except Exception as exc:
            _log_broker_failure("Manual", exc, warnings)
            return []

        components: list[dict[str, Any]] = []
        tradable_crypto_symbols = active_upbit_markets
        if tradable_crypto_symbols is not None:
            tradable_crypto_symbols = {
                str(market).strip().upper()
                for market in tradable_crypto_symbols
                if str(market).strip()
            }
        for holding in holdings:
            market_type = _normalize_market_type(getattr(holding, "market_type", None))
            if market_type is None:
                continue

            symbol = _normalize_symbol(getattr(holding, "ticker", ""), market_type)
            if not symbol:
                continue

            if market_type == _MARKET_CRYPTO and enforce_upbit_universe:
                if tradable_crypto_symbols is None:
                    tradable_crypto_symbols = await get_active_upbit_markets(
                        quote_currency=None
                    )
                    tradable_crypto_symbols = {
                        str(market).strip().upper()
                        for market in tradable_crypto_symbols
                        if str(market).strip()
                    }
                if symbol not in tradable_crypto_symbols:
                    logger.info(
                        "Skipping non-tradable manual CRYPTO holding symbol=%s",
                        symbol,
                    )
                    continue

            broker_account = getattr(holding, "broker_account", None)
            broker_value = getattr(broker_account, "broker_type", "manual")
            if hasattr(broker_value, "value"):
                broker_value = broker_value.value
            broker = str(broker_value or "manual").strip().lower()

            account_id = getattr(broker_account, "id", None)
            account_name = str(
                getattr(broker_account, "account_name", "기본 계좌") or "기본 계좌"
            )
            account_key = (
                f"manual:{account_id}" if account_id is not None else "manual:unknown"
            )

            quantity = _to_float(getattr(holding, "quantity", Decimal("0")))
            if quantity <= 0:
                continue

            components.append(
                {
                    "market_type": market_type,
                    "symbol": symbol,
                    "name": str(getattr(holding, "display_name", None) or symbol),
                    "account_key": account_key,
                    "broker": broker,
                    "account_name": account_name,
                    "source": "manual",
                    "quantity": quantity,
                    "avg_price": _to_float(getattr(holding, "avg_price", Decimal("0"))),
                    "current_price": None,
                    "evaluation": None,
                    "profit_loss": None,
                    "profit_rate": None,
                }
            )

        return components

    # ------------------------------------------------------------------
    # Task runner (isolates warnings per collection task)
    # ------------------------------------------------------------------

    async def _run_collection_task(
        self,
        func: Any,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Run a collection task and return its results and warnings."""
        local_warnings: list[str] = []
        try:
            result = await func(*args, warnings=local_warnings, **kwargs)
        except Exception as exc:
            logger.warning("Collection task failed: %s", exc)
            local_warnings.append(str(exc))
            result = []
        return result, local_warnings
