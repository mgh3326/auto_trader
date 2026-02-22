from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

import app.services.brokers.upbit.client as upbit_service
import app.services.brokers.yahoo.client as yahoo_service
from app.core.symbol import to_db_symbol
from app.models.manual_holdings import MarketType
from app.services.brokers.kis.client import KISClient
from app.services.manual_holdings_service import ManualHoldingsService
from app.services.upbit_symbol_universe_service import get_active_upbit_markets
from app.services.us_symbol_universe_service import (
    USSymbolUniverseLookupError,
    get_us_exchange_by_symbol,
)

logger = logging.getLogger(__name__)

_MARKET_ALL = "ALL"
_MARKET_KR = "KR"
_MARKET_US = "US"
_MARKET_CRYPTO = "CRYPTO"
_MARKET_ORDER = {_MARKET_KR: 0, _MARKET_US: 1, _MARKET_CRYPTO: 2}
_UPBIT_PRICE_BATCH_SIZE = 50


def _to_float(value: Any, *, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_rate(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None

    if abs(rate) > 2:
        return rate / 100.0
    return rate


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


class PortfolioOverviewService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.manual_holdings_service = ManualHoldingsService(db)

    async def get_overview(
        self,
        *,
        user_id: int,
        market: str = _MARKET_ALL,
        account_keys: list[str] | None = None,
        q: str | None = None,
    ) -> dict[str, Any]:
        market_filter = str(market or _MARKET_ALL).strip().upper()
        if market_filter not in {
            _MARKET_ALL,
            _MARKET_KR,
            _MARKET_US,
            _MARKET_CRYPTO,
        }:
            market_filter = _MARKET_ALL

        selected_account_keys = [
            str(item).strip() for item in (account_keys or []) if str(item).strip()
        ]
        selected_account_set = set(selected_account_keys)
        q_filter = (q or "").strip().lower() or None

        components: list[dict[str, Any]] = []
        warnings: list[str] = []
        enforce_upbit_universe = True

        try:
            active_upbit_markets = await get_active_upbit_markets(quote_currency=None)
        except Exception as exc:
            logger.warning("Failed to load active Upbit markets: %s", exc)
            warnings.append(f"Upbit universe lookup failed: {exc}")
            active_upbit_markets = None
            enforce_upbit_universe = False

        if active_upbit_markets is not None:
            active_upbit_markets = {
                str(market).strip().upper()
                for market in active_upbit_markets
                if str(market).strip()
            }

        kis_client = KISClient()
        components.extend(await self._collect_kis_components(kis_client, warnings))
        components.extend(
            await self._collect_upbit_components(
                warnings,
                active_upbit_markets=active_upbit_markets,
                enforce_upbit_universe=enforce_upbit_universe,
            )
        )
        components.extend(
            await self._collect_manual_components(
                user_id,
                warnings,
                active_upbit_markets=active_upbit_markets,
                enforce_upbit_universe=enforce_upbit_universe,
            )
        )

        if enforce_upbit_universe and active_upbit_markets is not None:
            components = [
                item
                for item in components
                if item["market_type"] != _MARKET_CRYPTO
                or item["symbol"] in active_upbit_markets
            ]

        await self._fill_missing_prices(
            kis_client,
            components,
            warnings,
            active_upbit_markets=active_upbit_markets,
            enforce_upbit_universe=enforce_upbit_universe,
        )

        facets = self._build_account_facets(components)
        filtered_components = self._filter_components(
            components,
            market_filter=market_filter,
            selected_account_keys=selected_account_set,
        )
        positions = self._aggregate_positions(filtered_components)

        if q_filter:
            positions = [
                position
                for position in positions
                if q_filter in position["symbol"].lower()
                or q_filter in position["name"].lower()
            ]

        summary = {
            "total_positions": len(positions),
            "by_market": {
                _MARKET_KR: sum(1 for p in positions if p["market_type"] == _MARKET_KR),
                _MARKET_US: sum(1 for p in positions if p["market_type"] == _MARKET_US),
                _MARKET_CRYPTO: sum(
                    1 for p in positions if p["market_type"] == _MARKET_CRYPTO
                ),
            },
        }

        deduped_warnings = list(dict.fromkeys(item for item in warnings if item))
        return {
            "success": True,
            "as_of": datetime.now(UTC).isoformat(),
            "filters": {
                "market": market_filter,
                "account_keys": selected_account_keys,
                "q": q,
            },
            "summary": summary,
            "facets": {"accounts": facets},
            "positions": positions,
            "warnings": deduped_warnings,
        }

    async def _collect_kis_components(
        self,
        kis_client: KISClient,
        warnings: list[str],
    ) -> list[dict[str, Any]]:
        components: list[dict[str, Any]] = []

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
                profit_rate = _normalize_rate(stock.get("evlu_pfls_rt"))

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
            logger.warning("Failed to fetch KIS KR holdings: %s", exc)
            warnings.append(f"KIS KR holdings fetch failed: {exc}")

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
                profit_rate = _normalize_rate(stock.get("evlu_pfls_rt"))

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
            logger.warning("Failed to fetch KIS US holdings: %s", exc)
            warnings.append(f"KIS US holdings fetch failed: {exc}")

        return [item for item in components if item["symbol"]]

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
            logger.warning("Failed to fetch Upbit holdings: %s", exc)
            warnings.append(f"Upbit holdings fetch failed: {exc}")
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

        symbols: list[str] = []
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

            symbols.append(symbol)
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

        if not symbols:
            return components

        price_map = await self._fetch_upbit_prices_resilient(
            symbols,
            warnings,
            stage="collect_upbit_components",
            active_upbit_markets=tradable_set,
            enforce_upbit_universe=enforce_upbit_universe,
        )

        for item in components:
            symbol = item["symbol"]
            current_price = price_map.get(symbol)
            if current_price is None:
                continue
            item["current_price"] = float(current_price)
            self._recalculate_component(item)

        return components

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
            logger.warning("Failed to fetch manual holdings: %s", exc)
            warnings.append(f"Manual holdings fetch failed: {exc}")
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

    async def _fill_missing_prices(
        self,
        kis_client: KISClient,
        components: list[dict[str, Any]],
        warnings: list[str],
        active_upbit_markets: set[str] | None = None,
        enforce_upbit_universe: bool = True,
    ) -> None:
        active_upbit_set = (
            {
                str(market).strip().upper()
                for market in active_upbit_markets
                if str(market).strip()
            }
            if active_upbit_markets is not None
            else None
        )

        kr_symbols = sorted(
            {
                item["symbol"]
                for item in components
                if item["market_type"] == _MARKET_KR and item["current_price"] is None
            }
        )
        us_symbol_targets: dict[str, set[str]] = {}
        for item in components:
            if item["market_type"] != _MARKET_US or item["current_price"] is not None:
                continue
            raw_symbol = str(item.get("symbol") or "").strip().upper()
            if not raw_symbol:
                continue
            normalized_symbol = to_db_symbol(raw_symbol).upper()
            if not normalized_symbol:
                continue
            us_symbol_targets.setdefault(normalized_symbol, set()).add(raw_symbol)

        us_symbols = sorted(us_symbol_targets)
        crypto_symbols = sorted(
            {
                item["symbol"]
                for item in components
                if item["market_type"] == _MARKET_CRYPTO
                and item.get("source") == "manual"
                and item["current_price"] is None
                and (
                    not enforce_upbit_universe
                    or active_upbit_set is None
                    or item["symbol"] in active_upbit_set
                )
            }
        )

        for symbol in kr_symbols:
            try:
                frame = await kis_client.inquire_price(symbol)
                if frame.empty:
                    continue
                price = _to_float(frame.iloc[-1].get("close"), default=0.0)
                if price <= 0:
                    continue
                self._apply_price(components, _MARKET_KR, symbol, price)
            except Exception as exc:
                logger.warning("Failed to fetch KIS KR price for %s: %s", symbol, exc)
                warnings.append(f"KIS KR price fetch failed for {symbol}: {exc}")

        valid_us_symbols: list[str] = []
        if us_symbols:
            for normalized_symbol in us_symbols:
                try:
                    await get_us_exchange_by_symbol(normalized_symbol, db=self.db)
                except USSymbolUniverseLookupError as exc:
                    raw_symbols = sorted(
                        us_symbol_targets.get(normalized_symbol, {normalized_symbol})
                    )
                    logger.info(
                        "Skipping invalid US symbol before price fetch symbols=%s normalized=%s reason=%s",
                        ",".join(raw_symbols),
                        normalized_symbol,
                        exc,
                    )
                    continue
                valid_us_symbols.append(normalized_symbol)

        if valid_us_symbols:
            for symbol in valid_us_symbols:
                frame = await yahoo_service.fetch_price(symbol)
                if frame.empty:
                    raise ValueError(
                        f"US price fetch failed for {symbol}: empty response"
                    )

                price = _to_float(frame.iloc[-1].get("close"), default=0.0)
                if price <= 0:
                    raise ValueError(
                        f"US price fetch failed for {symbol}: non-positive close price"
                    )

                for target_symbol in us_symbol_targets.get(symbol, {symbol}):
                    self._apply_price(components, _MARKET_US, target_symbol, price)

        if crypto_symbols:
            price_map = await self._fetch_upbit_prices_resilient(
                crypto_symbols,
                warnings,
                stage="manual_crypto",
                active_upbit_markets=active_upbit_set,
                enforce_upbit_universe=enforce_upbit_universe,
            )
            for symbol, price in price_map.items():
                if price is None:
                    continue
                self._apply_price(components, _MARKET_CRYPTO, symbol, float(price))

    async def _fetch_upbit_prices_resilient(
        self,
        symbols: list[str],
        warnings: list[str],
        stage: str,
        active_upbit_markets: set[str] | None = None,
        enforce_upbit_universe: bool = True,
    ) -> dict[str, float]:
        unique_symbols: list[str] = []
        seen: set[str] = set()
        for symbol in symbols:
            normalized = str(symbol or "").strip().upper()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique_symbols.append(normalized)

        if not unique_symbols:
            return {}

        recovered_prices: dict[str, float] = {}
        symbols_to_recover = list(unique_symbols)

        try:
            initial_prices = await upbit_service.fetch_multiple_current_prices(
                unique_symbols
            )
        except Exception as exc:
            logger.warning(
                "Failed initial Upbit price batch fetch (%s): %s",
                stage,
                exc,
            )
        else:
            for symbol, price in initial_prices.items():
                if price is None:
                    continue
                normalized = str(symbol).strip().upper()
                if not normalized:
                    continue
                recovered_prices[normalized] = float(price)

            symbols_to_recover = [
                symbol for symbol in unique_symbols if symbol not in recovered_prices
            ]
            if not symbols_to_recover:
                return recovered_prices

        filtered_symbols: list[str] = []
        if enforce_upbit_universe:
            tradable_set = active_upbit_markets
            if tradable_set is None:
                tradable_set = await get_active_upbit_markets(quote_currency=None)
            tradable_set = {
                str(market).strip().upper()
                for market in tradable_set
                if str(market).strip()
            }

            for symbol in symbols_to_recover:
                if symbol in tradable_set:
                    filtered_symbols.append(symbol)
                else:
                    logger.info(
                        "Skipping non-tradable Upbit symbol (%s): %s",
                        stage,
                        symbol,
                    )
        else:
            filtered_symbols = list(symbols_to_recover)

        if not filtered_symbols:
            return recovered_prices
        failed_symbols: list[str] = []

        for index in range(0, len(filtered_symbols), _UPBIT_PRICE_BATCH_SIZE):
            batch = filtered_symbols[index : index + _UPBIT_PRICE_BATCH_SIZE]
            if not batch:
                continue
            try:
                batch_prices = await upbit_service.fetch_multiple_current_prices(batch)
                normalized_batch_prices: dict[str, float] = {}
                for symbol, price in batch_prices.items():
                    if price is None:
                        continue
                    normalized = str(symbol).strip().upper()
                    if not normalized:
                        continue
                    normalized_batch_prices[normalized] = float(price)
                    recovered_prices[normalized] = float(price)

                missing_symbols = [
                    symbol for symbol in batch if symbol not in normalized_batch_prices
                ]
                failed_symbols.extend(missing_symbols)
            except Exception as exc:
                logger.warning(
                    "Failed Upbit price batch retry (%s, size=%d): %s",
                    stage,
                    len(batch),
                    exc,
                )
                failed_symbols.extend(batch)

        failed_symbols = list(dict.fromkeys(failed_symbols))
        for symbol in failed_symbols:
            try:
                single_prices = await upbit_service.fetch_multiple_current_prices(
                    [symbol]
                )
            except Exception as exc:
                warnings.append(
                    f"Upbit price fetch failed ({stage}) for {symbol}: {exc}"
                )
                continue

            single_price: float | None = None
            for fetched_symbol, fetched_price in single_prices.items():
                normalized = str(fetched_symbol).strip().upper()
                if normalized != symbol or fetched_price is None:
                    continue
                single_price = float(fetched_price)
                break

            if single_price is None:
                warnings.append(
                    f"Upbit price fetch failed ({stage}) for {symbol}: empty response"
                )
                continue
            recovered_prices[symbol] = single_price

        return recovered_prices

    def _apply_price(
        self,
        components: list[dict[str, Any]],
        market_type: str,
        symbol: str,
        price: float,
    ) -> None:
        for item in components:
            if item["market_type"] != market_type or item["symbol"] != symbol:
                continue
            if item["current_price"] is not None:
                continue
            item["current_price"] = price
            self._recalculate_component(item)

    def _recalculate_component(self, component: dict[str, Any]) -> None:
        quantity = _to_float(component.get("quantity"))
        avg_price = _to_float(component.get("avg_price"))
        current_price = component.get("current_price")

        if current_price is None:
            return

        current = float(current_price)
        evaluation = quantity * current
        cost_basis = quantity * avg_price
        profit_loss = evaluation - cost_basis
        profit_rate = (profit_loss / cost_basis) if cost_basis > 0 else 0.0

        component["evaluation"] = evaluation
        component["profit_loss"] = profit_loss
        component["profit_rate"] = profit_rate

    def _build_account_facets(
        self, components: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        facets: dict[str, dict[str, Any]] = {}

        for item in components:
            account_key = item["account_key"]
            facet = facets.setdefault(
                account_key,
                {
                    "account_key": account_key,
                    "broker": item["broker"],
                    "account_name": item["account_name"],
                    "source": item["source"],
                    "market_types": set(),
                },
            )
            facet["market_types"].add(item["market_type"])

        result = []
        for facet in facets.values():
            result.append(
                {
                    "account_key": facet["account_key"],
                    "broker": facet["broker"],
                    "account_name": facet["account_name"],
                    "source": facet["source"],
                    "market_types": sorted(
                        facet["market_types"],
                        key=lambda value: _MARKET_ORDER.get(value, 999),
                    ),
                }
            )

        return sorted(
            result,
            key=lambda item: (
                0 if item["source"] == "live" else 1,
                item["broker"],
                item["account_name"],
            ),
        )

    def _filter_components(
        self,
        components: list[dict[str, Any]],
        *,
        market_filter: str,
        selected_account_keys: set[str],
    ) -> list[dict[str, Any]]:
        filtered = components
        if market_filter != _MARKET_ALL:
            filtered = [
                item for item in filtered if item["market_type"] == market_filter
            ]
        if selected_account_keys:
            filtered = [
                item
                for item in filtered
                if item["account_key"] in selected_account_keys
            ]
        return filtered

    def _aggregate_positions(
        self, components: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        by_key: dict[tuple[str, str], dict[str, Any]] = {}

        for item in components:
            key = (item["market_type"], item["symbol"])
            row = by_key.setdefault(
                key,
                {
                    "market_type": item["market_type"],
                    "symbol": item["symbol"],
                    "name": item["name"],
                    "components": [],
                },
            )

            if not row["name"] and item["name"]:
                row["name"] = item["name"]

            row["components"].append(
                {
                    "account_key": item["account_key"],
                    "broker": item["broker"],
                    "account_name": item["account_name"],
                    "source": item["source"],
                    "quantity": item["quantity"],
                    "avg_price": item["avg_price"],
                    "current_price": item["current_price"],
                    "evaluation": item["evaluation"],
                    "profit_loss": item["profit_loss"],
                    "profit_rate": item["profit_rate"],
                }
            )

        rows: list[dict[str, Any]] = []
        for row in by_key.values():
            components_list = row["components"]
            quantity = sum(_to_float(item.get("quantity")) for item in components_list)
            if quantity <= 0:
                continue

            avg_numerator = sum(
                _to_float(item.get("quantity")) * _to_float(item.get("avg_price"))
                for item in components_list
            )
            avg_price = avg_numerator / quantity if quantity > 0 else 0.0

            current_price = self._pick_current_price(components_list)
            cost_basis = sum(
                _to_float(item.get("quantity")) * _to_float(item.get("avg_price"))
                for item in components_list
            )

            # If a canonical current price is available, recalculate position totals from
            # full quantity to avoid undercount when some account components are missing
            # per-component evaluation/profit fields.
            if current_price is not None:
                evaluation = quantity * current_price
                profit_loss = evaluation - cost_basis
            else:
                evaluation = sum(
                    _to_float(item.get("evaluation"), default=0.0)
                    for item in components_list
                    if item.get("evaluation") is not None
                )
                profit_loss = sum(
                    _to_float(item.get("profit_loss"), default=0.0)
                    for item in components_list
                    if item.get("profit_loss") is not None
                )

            profit_rate = (profit_loss / cost_basis) if cost_basis > 0 else 0.0

            rows.append(
                {
                    "market_type": row["market_type"],
                    "symbol": row["symbol"],
                    "name": row["name"] or row["symbol"],
                    "quantity": quantity,
                    "avg_price": avg_price,
                    "current_price": current_price,
                    "evaluation": evaluation,
                    "profit_loss": profit_loss,
                    "profit_rate": profit_rate,
                    "components": components_list,
                }
            )

        return sorted(
            rows,
            key=lambda item: (
                _MARKET_ORDER.get(item["market_type"], 999),
                item["symbol"],
            ),
        )

    def _pick_current_price(self, components: list[dict[str, Any]]) -> float | None:
        live_component = next(
            (
                item
                for item in components
                if item.get("source") == "live"
                and item.get("current_price") is not None
            ),
            None,
        )
        if live_component is not None:
            return float(live_component["current_price"])

        for item in components:
            if item.get("current_price") is not None:
                return float(item["current_price"])
        return None
