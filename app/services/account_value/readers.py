"""Read broker holdings through explicit method-limited views."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from app.core.symbol import to_db_symbol, to_upbit_symbol
from app.core.timezone import today_kst
from app.services.account_value.broker_views import read_only_view
from app.services.account_value.identity import configured_fingerprint
from app.services.account_value.snapshot import Holding, Observation


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


class BrokerReader:
    def __init__(self, session, account: str, market: str):
        self.session = session
        if account not in {"kis", "upbit", "toss"}:
            raise ValueError("unsupported broker")
        self.broker = account
        self.unknown_account = {
            "kis": "kis:kis_live:identity_unknown",
            "upbit": "upbit:upbit:identity_unknown",
            "toss": "toss:toss_live:identity_unknown",
        }[account]
        self.market = market

    async def read(self):
        now = datetime.now(UTC)
        reasons = []
        identity = None
        self.account = self.unknown_account
        holdings = []
        currency = "USD" if self.market == "us" else "KRW"
        cash = {currency: None}
        if self.broker == "upbit" and self.market == "crypto":
            from app.core.config import settings
            from app.services.brokers.upbit import client

            identity = configured_fingerprint(
                "upbit", "upbit", settings.upbit_access_key, settings.upbit_secret_key
            )
            view = read_only_view("upbit", client)
            rows = await view.fetch_my_coins()
            for row in rows:
                code = row.get("currency")
                balance, locked = (
                    _decimal(row.get("balance")),
                    _decimal(row.get("locked")),
                )
                if (
                    not code
                    or balance is None
                    or locked is None
                    or balance < 0
                    or locked < 0
                ):
                    reasons.append("invalid_broker_balance")
                    continue
                quantity = balance + locked
                if code == "KRW":
                    if cash["KRW"] is not None:
                        reasons.append("duplicate_cash")
                    cash["KRW"] = quantity
                elif quantity > 0:
                    holdings.append((to_upbit_symbol(code), quantity))
        elif self.broker == "kis" and self.market in {"kr", "us"}:
            from app.core.config import settings
            from app.services.brokers.kis.account_no import parse_kis_account_parts
            from app.services.invest_home_readers import SafeKISClient

            try:
                cano, product = parse_kis_account_parts(
                    settings.kis_account_no, is_mock=False
                )
                identity = configured_fingerprint(
                    "kis",
                    "kis_live",
                    cano + product + settings.kis_app_key,
                    settings.kis_app_secret,
                )
            except (ValueError, TypeError):
                identity = None
            client = SafeKISClient()
            view = read_only_view("kis", client.account)
            closer = read_only_view("kis_close", client)
            try:
                # The existing reader filters malformed quantities and caps pagination.
                reasons.append("holdings_completeness_unknown")
                rows = (
                    (await view.fetch_my_stocks())
                    if self.market == "kr"
                    else (await view.fetch_my_overseas_stocks())
                )
                for row in rows:
                    code = row.get("pdno" if self.market == "kr" else "ovrs_pdno")
                    quantity = _decimal(
                        row.get("hldg_qty" if self.market == "kr" else "ovrs_cblc_qty")
                    )
                    if not code or quantity is None or quantity < 0:
                        reasons.append("invalid_broker_holding")
                        continue
                    if quantity > 0:
                        holdings.append((to_db_symbol(str(code)).upper(), quantity))
                # This account's cash is not evidence of market allocation.
                reasons.append("cash_allocation_unknown")
                if self.market == "kr":
                    margin = await view.inquire_integrated_margin()
                    raw = (
                        margin.get("raw")
                        if isinstance(margin.get("raw"), dict)
                        else margin
                    )
                    cash["KRW"] = _decimal(raw.get("stck_cash_objt_amt"))
                else:
                    rows = await view.inquire_overseas_margin()
                    matches = [row for row in rows if row.get("crcy_cd") == "USD"]
                    if len(matches) == 1:
                        amount = _decimal(matches[0].get("frcr_dncl_amt1"))
                        # The existing reader coalesces absent fields to zero.
                        cash["USD"] = (
                            amount if amount is not None and amount > 0 else None
                        )
            finally:
                await closer.close()
        elif self.broker == "toss" and self.market in {"kr", "us"}:
            from app.services.brokers.toss.client import TossReadClient

            view = read_only_view("toss", TossReadClient.from_settings())
            try:
                accounts = await view.accounts()
                from app.core.config import settings

                selected = settings.toss_api_account_seq
                secret = settings.toss_api_client_secret
                if (
                    selected is not None
                    and any(account.account_seq == selected for account in accounts)
                    and secret is not None
                ):
                    identity = configured_fingerprint(
                        "toss", "toss_live", str(selected), secret.get_secret_value()
                    )
                response = await view.holdings()
                for item in response.items:
                    if item.market_country.lower() != self.market:
                        continue
                    quantity = _decimal(item.quantity)
                    if not item.symbol or quantity is None or quantity < 0:
                        reasons.append("invalid_broker_holding")
                        continue
                    if quantity > 0:
                        holdings.append((to_db_symbol(item.symbol).upper(), quantity))
            finally:
                await view.aclose()
            # Buying power is not evidence of allocated cash.
            reasons.append("cash_allocation_unknown")
        else:
            raise ValueError("unsupported account/market")
        if identity is None:
            reasons.append("account_identity_unknown")
        else:
            self.account = identity
        symbols = [symbol for symbol, _ in holdings]
        if len(symbols) != len(set(symbols)):
            reasons.append("duplicate_holding_symbol")
        marks = await self._marks(symbols, now)
        positions = tuple(
            Holding(symbol, qty, currency, *marks.get(symbol, (None, None, None)))
            for symbol, qty in holdings
        )
        return [
            Observation(
                self.account,
                self.market,
                today_kst(),
                now,
                positions,
                cash,
                tuple(reasons),
            )
        ]

    async def _marks(self, symbols, now):
        from app.services.daily_candles.read_service import latest_exchange_session
        from app.services.daily_candles.repository import (
            DailyCandlesRepository,
            MarketKey,
        )

        if not symbols:
            return {}
        market = {"kr": MarketKey.KR, "us": MarketKey.US, "crypto": MarketKey.CRYPTO}[
            self.market
        ]
        partition = {"kr": "KRX", "us": None, "crypto": "upbit_krw"}[self.market]
        last = (
            (now.date() - timedelta(days=1))
            if self.market == "crypto"
            else latest_exchange_session(
                {"kr": "XKRX", "us": "XNYS"}[self.market], now=now
            )
        )
        if last is None:
            return {}
        if self.market == "us":
            from datetime import time

            from sqlalchemy import text

            result = {}
            for symbol in symbols:
                query = await self.session.execute(
                    text(
                        "SELECT time, close, source FROM public.us_candles_1d "
                        "WHERE symbol = :symbol AND time >= :start AND time < :end "
                        "ORDER BY time, exchange, source"
                    ),
                    {
                        "symbol": symbol,
                        "start": datetime.combine(last, time.min, tzinfo=UTC),
                        "end": datetime.combine(
                            last + timedelta(days=1), time.min, tzinfo=UTC
                        ),
                    },
                )
                matches = query.all()
                if len(matches) == 1:
                    row = matches[0]
                    close = _decimal(row.close)
                    if close is not None and close > 0 and row.source:
                        result[symbol] = (
                            close,
                            f"daily_candles:{row.source}",
                            row.time,
                        )
            return result
        rows = await DailyCandlesRepository(session=self.session).fetch_recent_batch(
            market=market, symbols=symbols, partition=partition, count=5
        )
        result = {}
        for symbol in symbols:
            candidates = [
                row for row in rows.get(symbol, []) if row.time_utc.date() == last
            ]
            if len(candidates) != 1:
                continue
            row = candidates[0]
            close = _decimal(row.close)
            if close is not None and close > 0 and row.source:
                result[symbol] = (close, f"daily_candles:{row.source}", row.time_utc)
        return result


class CompleteBrokerReader:
    """Capture all markets and one account-wide view from the same run."""

    def __init__(self, session, broker: str):
        self.session = session
        self.broker = broker

    async def read(self):
        markets = {"kis": ("kr", "us"), "toss": ("kr", "us"), "upbit": ("crypto",)}[
            self.broker
        ]
        observations = []
        for market in markets:
            observations.extend(
                await BrokerReader(self.session, self.broker, market).read()
            )
        if len({item.account for item in observations}) != 1:
            raise ValueError("account identity changed during observation")
        if len({item.day for item in observations}) != 1:
            raise ValueError("observation crossed a date boundary")
        cash = {}
        positions = []
        reasons = set()
        for item in observations:
            positions.extend(item.holdings)
            reasons.update(
                reason for reason in item.reasons if reason != "cash_allocation_unknown"
            )
            for currency, amount in item.cash.items():
                if currency in cash:
                    reasons.add("cash_duplicate_currency")
                else:
                    cash[currency] = amount
        fx = {}
        if "USD" in cash or any(position.currency == "USD" for position in positions):
            try:
                quote = await _usd_krw_quote()
                rate = _decimal(quote.default_rate)
                fx_observed_at = datetime.now(UTC)
                if (
                    rate is None
                    or rate <= 0
                    or quote.valid_from is None
                    or quote.valid_from > fx_observed_at
                    or (
                        quote.valid_until is not None
                        and quote.valid_until < fx_observed_at
                    )
                ):
                    reasons.add("fx_unknown")
                else:
                    fx["USD/KRW"] = {
                        "rate": str(rate),
                        "source": quote.source,
                        "asof": quote.valid_from.isoformat(),
                    }
            except Exception:
                reasons.add("fx_unknown")
        observed_at = datetime.now(UTC)
        if today_kst() != observations[0].day:
            raise ValueError("observation crossed a date boundary")
        observations.append(
            Observation(
                observations[0].account,
                "account",
                observations[0].day,
                observed_at,
                tuple(positions),
                cash,
                tuple(sorted(reasons)),
                fx,
            )
        )
        return observations


async def _usd_krw_quote():
    from app.core.config import settings
    from app.services.brokers.toss.client import TossReadClient
    from app.services.exchange_rate_service import _parse_toss_usd_krw_quote

    if not settings.toss_api_enabled:
        raise ValueError("fx_unavailable")
    view = read_only_view("toss", TossReadClient.from_settings())
    try:
        raw = await view.exchange_rate(base_currency="USD", quote_currency="KRW")
        return _parse_toss_usd_krw_quote(raw)
    finally:
        await view.aclose()
