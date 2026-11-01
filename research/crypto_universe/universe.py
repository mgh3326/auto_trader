"""Point-in-time crypto pair universe (pure, no I/O beyond explicit table files).

One row per (venue, symbol) with the first and last UTC day on which the pair
actually traded (a daily bar with ``trades > 0`` / an Upbit daily candle).
The table is the survivorship-safe authority for "which pairs could be traded
on day ``t``": a pair listed after ``t`` is absent, a pair delisted after
``t`` is present.

Rule (inclusive dates)::

    live pair     : first_trade_date <= t
    any other pair: first_trade_date <= t <= last_trade_date
    pair with no trade at all: never

Queries are only answerable inside ``[coverage_start, coverage_end]``. A query
after ``coverage_end`` raises: the table cannot know which pairs were delisted
after its own data ends, so it fails closed instead of guessing.

Listing episodes: traded days are split into episodes wherever more than
``EPISODE_SPLIT_GAP_DAYS`` consecutive calendar days have no trade (a delisting
followed by a relisting, or a symbol reused for a new asset such as Binance
``LUNAUSDT`` for Terra classic and later LUNA 2.0). A pair is tradeable only
inside an episode; the last episode of a live pair is open-ended. Shorter
no-trade gaps stay inside an episode and are counted in ``gap_days``.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path
from typing import Literal, get_args

VenueLiteral = Literal["binance_usdt_spot", "upbit_krw"]
StatusLiteral = Literal["live", "delisted", "not_trading_other", "no_trades"]

VENUES: tuple[str, ...] = get_args(VenueLiteral)
STATUSES: tuple[str, ...] = get_args(StatusLiteral)

# Exchange states that mean the pair no longer trades.  ``ABSENT`` is our own
# marker for a pair that the exchange no longer lists at all.
DELISTED_EXCHANGE_STATES = frozenset({"BREAK", "ABSENT", "DELISTED"})
LIVE_EXCHANGE_STATES = frozenset({"TRADING", "ACTIVE"})

# Binance leveraged tokens existed only on these underlyings; a bare suffix
# test would misflag real assets such as JUP or SYRUP.
_LEVERAGED_UNDERLYINGS = frozenset(
    {
        "1INCH", "AAVE", "ADA", "BCH", "BNB", "BTC", "DOT", "EOS", "ETH", "FIL",
        "LINK", "LTC", "SUSHI", "SXP", "TRX", "UNI", "XLM", "XRP", "XTZ", "YFI",
    }
)  # fmt: skip
_LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
STABLE_BASES = frozenset(
    {
        "BUSD",
        "USDC",
        "TUSD",
        "USDP",
        "PAX",
        "FDUSD",
        "DAI",
        "USDS",
        "USDSB",
        "SUSD",
        "UST",
        "USTC",
        "USD1",
        "EUR",
        "EURI",
        "AEUR",
        "GBP",
        "AUD",
        "TRY",
        "BRL",
        "RUB",
        "UAH",
        "NGN",
        "ZAR",
        "IDRT",
        "BIDR",
        "BKRW",
        "XUSD",
        "USDE",
        "RLUSD",
        "PAXG",
    }
)


EPISODE_SPLIT_GAP_DAYS = 7

Episode = tuple[date, date]


class OutsideCoverageError(ValueError):
    """The query date lies after the table's last covered day."""


def classify_status(exchange_status: str, first_trade: date | None) -> StatusLiteral:
    """Map a raw exchange state to the closed status vocabulary."""
    if first_trade is None:
        return "no_trades"
    if exchange_status in LIVE_EXCHANGE_STATES:
        return "live"
    if exchange_status in DELISTED_EXCHANGE_STATES:
        return "delisted"
    return "not_trading_other"


def is_leveraged_token(base_asset: str) -> bool:
    """Binance leveraged tokens (``BTCUP``, ``ETHDOWN``, ``BNBBULL``, ``BULL``)."""
    if base_asset in {"BULL", "BEAR"}:
        return True
    return any(
        base_asset.endswith(suffix)
        and base_asset[: -len(suffix)] in _LEVERAGED_UNDERLYINGS
        for suffix in _LEVERAGED_SUFFIXES
    )


@dataclass(frozen=True)
class PairListing:
    venue: VenueLiteral
    symbol: str
    base_asset: str
    quote_asset: str
    first_trade_date: date | None
    last_trade_date: date | None
    status: StatusLiteral
    exchange_status: str
    trading_days: int
    gap_days: int
    last_trade_censored: bool
    source: str
    episodes: tuple[Episode, ...] = ()

    def __post_init__(self) -> None:
        if self.venue not in VENUES:
            raise ValueError(f"unknown venue {self.venue!r}")
        if self.status not in STATUSES:
            raise ValueError(f"{self.symbol}: unknown status {self.status!r}")
        if (self.first_trade_date is None) != (self.last_trade_date is None):
            raise ValueError(f"{self.symbol}: first/last trade must both be set")
        if self.first_trade_date is None:
            if self.status != "no_trades":
                raise ValueError(f"{self.symbol}: no trades but status {self.status}")
        elif self.last_trade_date is not None:
            if self.last_trade_date < self.first_trade_date:
                raise ValueError(f"{self.symbol}: last trade before first trade")
            if self.status == "no_trades":
                raise ValueError(f"{self.symbol}: has trades but status no_trades")
        if self.trading_days < 0 or self.gap_days < 0:
            raise ValueError(f"{self.symbol}: negative day counts")
        if self.first_trade_date is None:
            if self.episodes:
                raise ValueError(f"{self.symbol}: episodes without trades")
        else:
            if not self.episodes:
                raise ValueError(f"{self.symbol}: traded pair needs episodes")
            if (
                self.episodes[0][0] != self.first_trade_date
                or self.episodes[-1][1] != self.last_trade_date
            ):
                raise ValueError(f"{self.symbol}: episodes disagree with first/last")
            if any(end < start for start, end in self.episodes):
                raise ValueError(f"{self.symbol}: episode ends before start")
            for prev, nxt in zip(self.episodes, self.episodes[1:], strict=False):
                if nxt[0] <= prev[1]:
                    raise ValueError(f"{self.symbol}: episodes overlap or unsorted")

    @property
    def is_leveraged_token(self) -> bool:
        return is_leveraged_token(self.base_asset)

    @property
    def is_stable_base(self) -> bool:
        return self.base_asset in STABLE_BASES

    def tradeable_on(self, t: date) -> bool:
        if self.first_trade_date is None or t < self.first_trade_date:
            return False
        last_start = self.episodes[-1][0]
        if self.status == "live" and t >= last_start:
            return True  # the current listing has no end yet
        return any(start <= t <= end for start, end in self.episodes)


TABLE_COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(PairListing)) + (
    "is_leveraged_token",
    "is_stable_base",
)


def _date_or_none(value: str) -> date | None:
    return None if value == "" else date.fromisoformat(value)


def format_episodes(episodes: Sequence[Episode]) -> str:
    return ";".join(f"{a.isoformat()}..{b.isoformat()}" for a, b in episodes)


def parse_episodes(text: str) -> tuple[Episode, ...]:
    if text == "":
        return ()
    out = []
    for part in text.split(";"):
        start, sep, end = part.partition("..")
        if not sep:
            raise ValueError(f"malformed episode {part!r}")
        out.append((date.fromisoformat(start), date.fromisoformat(end)))
    return tuple(out)


def _bool(value: str) -> bool:
    if value not in ("true", "false"):
        raise ValueError(f"not a boolean: {value!r}")
    return value == "true"


@dataclass(frozen=True)
class PitUniverse:
    """All listings of one or more venues plus the covered day range."""

    coverage_start: date
    coverage_end: date
    listings: tuple[PairListing, ...]

    def __post_init__(self) -> None:
        if self.coverage_end < self.coverage_start:
            raise ValueError("coverage_end before coverage_start")
        seen: set[tuple[str, str]] = set()
        for listing in self.listings:
            key = (listing.venue, listing.symbol)
            if key in seen:
                raise ValueError(f"duplicate listing {key}")
            seen.add(key)
            if (
                listing.last_trade_date is not None
                and listing.last_trade_date > self.coverage_end
            ):
                raise ValueError(f"{key}: last trade after coverage_end")

    def universe_as_of(
        self, t: date, venue: VenueLiteral | None = None
    ) -> frozenset[str]:
        """Symbols tradeable on UTC day ``t``; fails closed past coverage."""
        if t > self.coverage_end:
            raise OutsideCoverageError(
                f"{t.isoformat()} is after coverage_end {self.coverage_end.isoformat()}"
            )
        return frozenset(
            x.symbol
            for x in self.listings
            if (venue is None or x.venue == venue) and x.tradeable_on(t)
        )

    def for_venue(self, venue: VenueLiteral) -> tuple[PairListing, ...]:
        return tuple(x for x in self.listings if x.venue == venue)

    # -- table I/O ---------------------------------------------------------

    def to_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(TABLE_COLUMNS)
        for x in sorted(self.listings, key=lambda r: (r.venue, r.symbol)):
            writer.writerow(
                [
                    x.venue,
                    x.symbol,
                    x.base_asset,
                    x.quote_asset,
                    x.first_trade_date.isoformat() if x.first_trade_date else "",
                    x.last_trade_date.isoformat() if x.last_trade_date else "",
                    x.status,
                    x.exchange_status,
                    x.trading_days,
                    x.gap_days,
                    "true" if x.last_trade_censored else "false",
                    x.source,
                    format_episodes(x.episodes),
                    "true" if x.is_leveraged_token else "false",
                    "true" if x.is_stable_base else "false",
                ]
            )
        return buf.getvalue()

    def sha256(self) -> str:
        return hashlib.sha256(self.to_csv().encode()).hexdigest()

    @staticmethod
    def listings_from_csv(text: str) -> tuple[PairListing, ...]:
        reader = csv.DictReader(io.StringIO(text))
        if tuple(reader.fieldnames or ()) != TABLE_COLUMNS:
            raise ValueError(f"unexpected table columns {reader.fieldnames}")
        out: list[PairListing] = []
        for row in reader:
            listing = PairListing(
                venue=row["venue"],  # type: ignore[arg-type]
                symbol=row["symbol"],
                base_asset=row["base_asset"],
                quote_asset=row["quote_asset"],
                first_trade_date=_date_or_none(row["first_trade_date"]),
                last_trade_date=_date_or_none(row["last_trade_date"]),
                status=row["status"],  # type: ignore[arg-type]
                exchange_status=row["exchange_status"],
                trading_days=int(row["trading_days"]),
                gap_days=int(row["gap_days"]),
                last_trade_censored=_bool(row["last_trade_censored"]),
                source=row["source"],
                episodes=parse_episodes(row["episodes"]),
            )
            if _bool(row["is_leveraged_token"]) != listing.is_leveraged_token:
                raise ValueError(f"{listing.symbol}: is_leveraged_token mismatch")
            if _bool(row["is_stable_base"]) != listing.is_stable_base:
                raise ValueError(f"{listing.symbol}: is_stable_base mismatch")
            out.append(listing)
        return tuple(out)

    @classmethod
    def load(
        cls, table_path: str | Path, coverage_start: date, coverage_end: date
    ) -> PitUniverse:
        text = Path(table_path).read_text(encoding="utf-8")
        return cls(coverage_start, coverage_end, cls.listings_from_csv(text))


def derive_listing(
    *,
    venue: VenueLiteral,
    symbol: str,
    base_asset: str,
    quote_asset: str,
    traded_days: Iterable[date],
    exchange_status: str,
    coverage_end: date,
    source: str,
) -> PairListing:
    """Build one listing from the set of UTC days that had at least one trade.

    ``last_trade_censored`` is true when the last trade is on the last covered
    day: the pair may have traded later, outside the data.
    """
    days: Sequence[date] = sorted(set(traded_days))
    if any(d > coverage_end for d in days):
        raise ValueError(f"{symbol}: traded day after coverage_end")
    first = days[0] if days else None
    last = days[-1] if days else None
    episodes: list[Episode] = []
    for day in days:
        if episodes and (day - episodes[-1][1]).days - 1 <= EPISODE_SPLIT_GAP_DAYS:
            episodes[-1] = (episodes[-1][0], day)
        else:
            episodes.append((day, day))
    return PairListing(
        venue=venue,
        symbol=symbol,
        base_asset=base_asset,
        quote_asset=quote_asset,
        first_trade_date=first,
        last_trade_date=last,
        status=classify_status(exchange_status, first),
        exchange_status=exchange_status,
        trading_days=len(days),
        gap_days=sum((end - start).days + 1 for start, end in episodes) - len(days),
        last_trade_censored=last is not None and last == coverage_end,
        source=source,
        episodes=tuple(episodes),
    )
