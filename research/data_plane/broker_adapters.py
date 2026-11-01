"""Read-only adapters from the app's broker clients to ``BrokerDailyClient``.

The app's broker clients are order-capable objects: ``KISClient`` carries the
domestic and overseas order clients, and ``TossReadClient`` has
``place_order``/``modify_order``/``cancel_order``. This module never hands such
an object to research code. Each client is wrapped at construction in a view
that answers only the methods in ``ALLOWED_METHODS`` (daily charts and
close) and raises ``BrokerMethodRefusedError`` for every other attribute. An
allowed name is answered with a plain wrapper function, not the client's bound
method, so the view has no attribute (such as ``__self__``) that leads back to
the client. This is a guard against accidental or careless use, not a sandbox:
Python introspection (closures, coroutine frames) can still reach any object,
and the static tests forbid such access anywhere in the package. A static test
pins that this is the only module of the package that imports app broker code,
that it imports it lazily, and that it reaches the clients only through the
view.

One research budget slot is one broker HTTP request: the KR chart is read with
``max_pages=1`` and the US chart with ``max_iterations=1``; Toss ``candles`` is
a single page. The app retries a request once on an expired token, which is
the only way one slot can cost two requests.

The app modules are imported only when ``open_broker_clients`` runs (the
collector on the host that has the broker credentials). Importing the package
or the CLI loads no app, httpx, database or Redis module.

The real clients are asynchronous; one private event loop per CLI run drives
them, so the clients' Redis token caches and HTTP pools stay bound to one loop.
Token reuse is the app's own: the KIS and Toss token managers are shared
through Redis, so this process does not issue a token that would replace the
one serving uses.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, TypeVar

from . import sources
from .bar_sources import SOURCE_KIS, SOURCE_TOSS, SourceBar
from .contract import market_spec

T = TypeVar("T")

# The complete surface research may touch on each app client.
ALLOWED_METHODS: Mapping[str, frozenset[str]] = {
    SOURCE_KIS: frozenset(
        {
            "inquire_daily_itemchartprice_unclamped",  # KR FHKST03010100
            "inquire_overseas_daily_price_unclamped",  # US HHDFS76240000
            "close",
        }
    ),
    SOURCE_TOSS: frozenset({"candles", "aclose"}),  # GET /api/v1/candles
}

# US universe venues (NASDAQ Trader names) -> KIS overseas exchange codes.
# KIS lists NYSE American, NYSE Arca and Cboe BZX listings under AMEX (AMS).
# An unmapped venue is that instrument's gap, never a guess.
KIS_US_EXCHANGE: Mapping[str, str] = {
    "NASDAQ": "NASD",
    "NYSE": "NYSE",
    "NYSE American": "AMEX",
    "NYSE Arca": "AMEX",
    "Cboe BZX": "AMEX",
}
# One KIS overseas page (no end-date parameter: the page ends at the latest
# bar, so a recent partition day is inside it).
KIS_US_PAGE_ROWS = 100
KR_MARKET_CODE = "J"  # KRX


def kr_calendar_days(sessions: int) -> int:
    """Calendar days that hold ``sessions`` KRX sessions in one chart page
    (KIS answers at most 100 rows per request)."""
    return max(14, math.ceil(sessions * 7 / 5) + 30)


class BrokerMethodRefusedError(AttributeError):
    """Research tried to reach a broker method outside ``ALLOWED_METHODS``."""


def read_only_view(broker: str, target: Any) -> Any:
    """An object exposing only ``ALLOWED_METHODS[broker]`` of ``target``."""
    allowed = ALLOWED_METHODS[broker]

    class _ReadOnlyBrokerView:
        __slots__ = ()

        def __getattr__(self, name: str) -> Any:
            if name not in allowed:
                raise BrokerMethodRefusedError(
                    f"{broker}: {name!r} is not an allowed research method"
                )

            def call(*args: Any, **kwargs: Any) -> Any:
                return getattr(target, name)(*args, **kwargs)

            call.__name__ = call.__qualname__ = name
            return call

        def __setattr__(self, name: str, value: Any) -> None:
            raise BrokerMethodRefusedError(f"{broker}: the view is read-only")

        def __repr__(self) -> str:
            return f"<read-only {broker} view: {sorted(allowed)}>"

    return _ReadOnlyBrokerView()


class AsyncRunner:
    """Runs the async client calls of one CLI run on one private loop."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()

    def __call__(self, awaitable: Awaitable[T]) -> T:
        return self._loop.run_until_complete(awaitable)

    def close(self) -> None:
        if not self._loop.is_closed():
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()


def _day(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    to_pydatetime = getattr(value, "to_pydatetime", None)  # pandas Timestamp
    if callable(to_pydatetime):
        return to_pydatetime().date()
    return date.fromisoformat(str(value)[:10])


def _volume(value: Any) -> int | str | None:
    if value is None:
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        if value.is_integer():
            return int(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if (
        isinstance(value, Decimal)
        and value.is_finite()
        and value == value.to_integral()
    ):
        return int(value)
    return sources.dec(value)


def _price(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return sources.dec(value)


def frame_bars(frame: Any) -> list[SourceBar]:
    """KIS daily DataFrame (date, open, high, low, close, volume[, value])."""
    out = []
    for rec in frame.to_dict("records"):
        out.append(
            SourceBar(
                day=_day(rec["date"]),
                open=_price(rec.get("open")),
                high=_price(rec.get("high")),
                low=_price(rec.get("low")),
                close=_price(rec.get("close")),
                volume=_volume(rec.get("volume")),
            )
        )
    return out


class KisDailyClient:
    """``BrokerDailyClient`` over the KIS daily charts."""

    def __init__(self, client: Any, run: Callable[[Awaitable[Any]], Any]) -> None:
        self._kis = read_only_view(SOURCE_KIS, client)
        self._run = run

    def daily_bars(
        self,
        *,
        market: str,
        instrument: str,
        venue: str,
        end: date,
        count: int,
        adjusted: bool,
    ) -> Sequence[SourceBar]:
        if market == "kr":
            frame = self._run(
                self._kis.inquire_daily_itemchartprice_unclamped(
                    code=instrument,
                    market=KR_MARKET_CODE,
                    n=count,
                    adj=adjusted,
                    period="D",
                    end_date=end,
                    per_call_days=kr_calendar_days(count),
                    max_pages=1,
                )
            )
            return frame_bars(frame)
        if market == "us":
            exchange = KIS_US_EXCHANGE.get(venue)
            if exchange is None:
                raise sources.SourceError(f"kis: no exchange code for venue {venue!r}")
            frame = self._run(
                self._kis.inquire_overseas_daily_price_unclamped(
                    symbol=instrument,
                    exchange_code=exchange,
                    n=KIS_US_PAGE_ROWS,
                    period="D",
                    max_iterations=1,
                    adjusted=adjusted,
                )
            )
            bars = [b for b in frame_bars(frame) if b.day <= end]
            return bars[-count:]
        raise ValueError(market)

    def close(self) -> None:
        self._run(self._kis.close())


class TossDailyClient:
    """``BrokerDailyClient`` over Toss ``/api/v1/candles`` (``interval=1d``)."""

    def __init__(self, client: Any, run: Callable[[Awaitable[Any]], Any]) -> None:
        self._toss = read_only_view(SOURCE_TOSS, client)
        self._run = run

    def daily_bars(
        self,
        *,
        market: str,
        instrument: str,
        venue: str,
        end: date,
        count: int,
        adjusted: bool,
    ) -> Sequence[SourceBar]:
        tz = market_spec(market).tz
        before = datetime.combine(end + timedelta(days=1), time.min, tz).isoformat()
        page = self._run(
            self._toss.candles(
                instrument,
                interval="1d",
                count=count,
                before=before,
                adjusted=adjusted,
            )
        )
        out = []
        for c in page.candles:
            stamp = datetime.fromisoformat(str(c.timestamp))
            if stamp.tzinfo is None:
                raise sources.SourceError("toss candle timestamp without an offset")
            out.append(
                SourceBar(
                    # the candle's own calendar date, as the app's toss_ohlcv
                    day=stamp.date(),
                    open=_price(c.open_price),
                    high=_price(c.high_price),
                    low=_price(c.low_price),
                    close=_price(c.close_price),
                    volume=_volume(c.volume),
                )
            )
        return [b for b in out if b.day <= end]

    def close(self) -> None:
        self._run(self._toss.aclose())


@dataclass
class BrokerClients:
    clients: dict[str, Any] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)
    runner: AsyncRunner | None = None

    def close(self) -> None:
        for client in self.clients.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001,S110 - closing must not mask the run's result
                pass
        if self.runner is not None:
            self.runner.close()


def open_broker_clients(names: Sequence[str]) -> BrokerClients:
    """Construct the read-only broker clients named in ``names``.

    A broker whose client cannot be built (no credentials, gate off, settings
    error) is reported in ``unavailable`` by exception class only — never by
    message, which may echo configuration values.
    """
    out = BrokerClients(runner=AsyncRunner())
    for name in names:
        try:
            if name == SOURCE_KIS:
                from app.services.brokers.kis.client import KISClient

                out.clients[name] = KisDailyClient(KISClient(), out.runner)
            elif name == SOURCE_TOSS:
                from app.services.brokers.toss.client import TossReadClient

                out.clients[name] = TossDailyClient(
                    TossReadClient.from_settings(), out.runner
                )
        except Exception as exc:  # noqa: BLE001
            out.unavailable[name] = f"client_unavailable:{type(exc).__name__}"
    return out
