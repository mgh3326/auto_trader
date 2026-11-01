"""KR/US daily-bar sources behind one interface, and the per-market order.

A ``BarSource`` answers, for one instrument and one partition day, the bars of
one *vintage*:

``raw``       the bar of the partition day as traded (no later adjustment).
``adjusted``  the adjusted bars of the last ``window`` sessions ending at the
              partition day, as the source publishes them on that day. A
              split or dividend shows up as changed values for the overlapping
              dates between two vintages, so the adjustment history is itself
              point-in-time.

Sources, in the default order for KR and US:

``kis``    KIS daily chart (KR ``FHKST03010100`` with ``FID_ORG_ADJ_PRC``; US
           ``HHDFS76240000`` with ``MODP``), through ``broker_adapters``.
``toss``   Toss ``/api/v1/candles`` with ``interval=1d`` and ``adjusted``.
``yahoo``  the public Yahoo chart v8 fallback.

The collector serves a whole partition from one source: the first source in
the order whose coverage passes the floor. A source that is not configured,
raises, or covers too little is recorded in the manifest's
``source_attempts`` and the next one is tried; ``served_by`` names the winner.
Mixing sources inside one partition is avoided on purpose — two sources'
adjustment conventions differ.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol

from . import sources
from .budget import BudgetError, ResearchBudget

VINTAGE_RAW = "raw"
VINTAGE_ADJUSTED = "adjusted"
VINTAGES = (VINTAGE_RAW, VINTAGE_ADJUSTED)

SOURCE_KIS = "kis"
SOURCE_TOSS = "toss"
SOURCE_YAHOO = "yahoo"
BROKER_SOURCES = (SOURCE_KIS, SOURCE_TOSS)
BAR_SOURCES = (SOURCE_KIS, SOURCE_TOSS, SOURCE_YAHOO)
# KIS serves daily bars and real orders in this repo, Toss batch prices; both
# have daily charts. The public source is always last.
DEFAULT_SOURCE_ORDER: Mapping[str, tuple[str, ...]] = {
    "kr": (SOURCE_KIS, SOURCE_TOSS, SOURCE_YAHOO),
    "us": (SOURCE_KIS, SOURCE_TOSS, SOURCE_YAHOO),
}
DEFAULT_ADJUSTED_WINDOW = 20


class SourceUnavailableError(RuntimeError):
    """The source cannot serve this run at all (not configured, gate off)."""


@dataclass(frozen=True)
class SourceBar:
    day: date
    open: str | None
    high: str | None
    low: str | None
    close: str | None
    volume: int | str | None


@dataclass(frozen=True)
class BarAnswer:
    bars: tuple[SourceBar, ...]
    currency: str | None
    # per-day extras a source keeps on raw rows (yahoo: adj_close)
    extra: Mapping[date, str | None] = field(default_factory=dict)


class BarSource(Protocol):
    name: str
    source_id: str  # the row's ``source``
    version: str

    def adjustment_basis(self, market: str, vintage: str) -> str: ...

    def bars(
        self,
        market: str,
        instrument: str,
        venue: str,
        day: date,
        vintage: str,
        window: int,
    ) -> BarAnswer | str: ...


def check_source_order(market: str, order: Sequence[str]) -> tuple[str, ...]:
    if market not in DEFAULT_SOURCE_ORDER:
        raise ValueError(f"bar sources apply to kr/us, not {market!r}")
    names = tuple(order)
    if not names:
        raise ValueError("bar source order is empty")
    unknown = [n for n in names if n not in BAR_SOURCES]
    if unknown:
        raise ValueError(f"unknown bar sources {unknown}; expected from {BAR_SOURCES}")
    if len(set(names)) != len(names):
        raise ValueError(f"bar source repeated in {list(names)}")
    return names


# --------------------------------------------------------------------------
# broker clients (implemented by broker_adapters against the app clients, and
# by fakes in tests)


class BrokerDailyClient(Protocol):
    """Read-only daily bars from one broker, synchronous.

    Returns the bars with ``day <= end`` (at most ``count``, newest last) of
    one price basis. Raises on transport/auth/contract failure.
    """

    def daily_bars(
        self,
        *,
        market: str,
        instrument: str,
        venue: str,
        end: date,
        count: int,
        adjusted: bool,
    ) -> Sequence[SourceBar]: ...


BROKER_META: Mapping[str, Mapping[str, str]] = {
    SOURCE_KIS: {
        "source_id": "kis_daily_chart",
        "version": "kr-FHKST03010100/us-HHDFS76240000",
        "kr_raw": "kis FID_ORG_ADJ_PRC=1 (unadjusted, as traded)",
        "kr_adjusted": "kis FID_ORG_ADJ_PRC=0 (adjusted as of the observation day)",
        "us_raw": "kis MODP=0 (unadjusted, as traded)",
        "us_adjusted": "kis MODP=1 (adjusted as of the observation day)",
    },
    SOURCE_TOSS: {
        "source_id": "toss_candles_1d",
        "version": "api-v1",
        "kr_raw": "toss adjusted=false (unadjusted, as traded)",
        "kr_adjusted": "toss adjusted=true (adjusted as of the observation day)",
        "us_raw": "toss adjusted=false (unadjusted, as traded)",
        "us_adjusted": "toss adjusted=true (adjusted as of the observation day)",
    },
}
CURRENCY = {"kr": "KRW", "us": "USD"}


class BrokerBarSource:
    """A broker daily-chart source; every client call takes a budget slot."""

    def __init__(
        self, name: str, client: BrokerDailyClient, budget: ResearchBudget
    ) -> None:
        if name not in BROKER_SOURCES:
            raise ValueError(f"not a broker source: {name!r}")
        if budget.spec.broker != name:
            raise ValueError(f"budget for {budget.spec.broker} given to {name}")
        self.name = name
        self.source_id = BROKER_META[name]["source_id"]
        self.version = BROKER_META[name]["version"]
        self._client = client
        self._budget = budget

    def adjustment_basis(self, market: str, vintage: str) -> str:
        return BROKER_META[self.name][f"{market}_{vintage}"]

    def bars(
        self,
        market: str,
        instrument: str,
        venue: str,
        day: date,
        vintage: str,
        window: int,
    ) -> BarAnswer | str:
        try:
            self._budget.acquire()
        except BudgetError as exc:  # a conflicting budget on the shared window
            raise SourceUnavailableError(str(exc)) from exc
        got = self._client.daily_bars(
            market=market,
            instrument=instrument,
            venue=venue,
            end=day,
            count=1 if vintage == VINTAGE_RAW else window,
            adjusted=vintage == VINTAGE_ADJUSTED,
        )
        bars = sorted((b for b in got if b.day <= day), key=lambda b: b.day)
        if len({b.day for b in bars}) != len(bars):
            raise sources.SourceError(f"{self.name}: two bars for one day")
        return BarAnswer(tuple(bars), CURRENCY[market])


# --------------------------------------------------------------------------
# Yahoo chart v8 (the public fallback)

_ADJ_QUANT = Decimal("1e-8")


def _scaled(value: str | None, factor: Decimal) -> str | None:
    if value is None:
        return None
    return format((Decimal(value) * factor).quantize(_ADJ_QUANT).normalize(), "f")


class YahooBarSource:
    """Wraps the collector's per-run Yahoo chart cache (one fetch per instrument
    serves the raw and adjusted vintages and corporate actions)."""

    name = SOURCE_YAHOO
    source_id = sources.YAHOO_SOURCE
    version = sources.YAHOO_VERSION

    def __init__(self, chart: Callable[[str, str], sources.YahooChart | str]) -> None:
        self._chart = chart

    def adjustment_basis(self, market: str, vintage: str) -> str:
        if vintage == VINTAGE_RAW:
            return (
                "yahoo chart v8 quote of the observation day (split-adjusted "
                "series; a day's own bar is as traded)"
            )
        return (
            "yahoo chart v8 OHLC x (adjclose/close): split and dividend adjusted "
            "as of the observation day (derived)"
        )

    def bars(
        self,
        market: str,
        instrument: str,
        venue: str,
        day: date,
        vintage: str,
        window: int,
    ) -> BarAnswer | str:
        chart = self._chart(instrument, venue)
        if isinstance(chart, str):
            return chart
        pick = [b for b in chart.bars if b.day <= day]
        if vintage == VINTAGE_RAW:
            pick = [b for b in pick if b.day == day]
            return BarAnswer(
                tuple(
                    SourceBar(b.day, b.open, b.high, b.low, b.close, b.volume)
                    for b in pick
                ),
                chart.currency,
                {b.day: b.adj_close for b in pick},
            )
        out = []
        for b in pick[-window:]:
            if b.close is None or b.adj_close is None or Decimal(b.close) == 0:
                out.append(SourceBar(b.day, None, None, None, None, b.volume))
                continue
            factor = Decimal(b.adj_close) / Decimal(b.close)
            out.append(
                SourceBar(
                    b.day,
                    _scaled(b.open, factor),
                    _scaled(b.high, factor),
                    _scaled(b.low, factor),
                    _scaled(b.close, factor),
                    b.volume,
                )
            )
        return BarAnswer(tuple(out), chart.currency)
