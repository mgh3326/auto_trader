"""Venue-isolated crypto adapter for UTC-calendar fixture wiring.

No API in this module accepts more than one venue. A view is bound to exactly
one of ``upbit_krw`` or ``binance_usdt`` and raises on a mismatched manifest
entry or row. This is a code guard against cross-sectional mixing of the
documented biased/unbiased delisted-history universes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import pyarrow as pa
from holdout_guard import (
    HOLDOUT_END,
    HOLDOUT_START,
    HoldoutPolicy,
    assert_date_not_holdout,
)
from loader import ManifestEntry
from market_adapters.common import ContractBackedCorpusAdapter, parse_utc_timestamp
from market_adapters.costs import CostModel
from terminal_events import TerminalEvent, force_exit_delisted_holdings

__all__ = [
    "BINANCE_USDT_COST",
    "CRYPTO_CALENDAR",
    "CRYPTO_HOLDOUT_POLICY",
    "CRYPTO_SCHEMA_CONTRACT_PATH",
    "CryptoBar",
    "CryptoPrelistingBarsUnavailable",
    "CryptoSessionDateMismatchError",
    "CryptoTerminalPriceUnavailable",
    "CryptoVenueAdapter",
    "CryptoVenueMixError",
    "CryptoVenueView",
    "CryptoVenue",
    "UPBIT_KRW_COST",
]

CryptoVenue = Literal["upbit_krw", "binance_usdt"]
_SUPPORTED_VENUES: frozenset[str] = frozenset({"upbit_krw", "binance_usdt"})
CRYPTO_CALENDAR = "24_7_UTC"
CRYPTO_SCHEMA_CONTRACT_PATH = (
    Path(__file__).resolve().parent / "contracts" / "crypto-corpus-v1.schema.json"
)
# This literal is a guard-only path; this module never reads this corpus root.
CRYPTO_HOLDOUT_POLICY = HoldoutPolicy(
    holdout_dir=Path("/Users/mgh3326/work/herdr-artifacts/crypto-corpus-v1/holdout/"),
    start=HOLDOUT_START,
    end=HOLDOUT_END,
)
UPBIT_KRW_COST = CostModel(fee_bp=5, slippage_bp_per_side=10)
BINANCE_USDT_COST = CostModel(fee_bp=10, slippage_bp_per_side=10)


class CryptoVenueMixError(ValueError):
    """Rows or manifest entries attempted to cross a bound venue boundary."""


class CryptoSessionDateMismatchError(ValueError):
    """A declared UTC calendar day differs from the raw UTC timestamp date."""


class CryptoTerminalPriceUnavailable(RuntimeError):
    """A delisted held symbol has no earlier valid price for liquidation."""


class CryptoPrelistingBarsUnavailable(LookupError):
    """Raised only by callers that demand a pre-listing bar instead of no bar."""


def _assert_supported_venue(venue: str) -> CryptoVenue:
    if venue not in _SUPPORTED_VENUES:
        raise CryptoVenueMixError(
            f"unsupported crypto venue {venue!r}; expected one of "
            f"{sorted(_SUPPORTED_VENUES)!r}"
        )
    return venue  # type: ignore[return-value]


@dataclass(frozen=True)
class CryptoBar:
    symbol: str
    venue: CryptoVenue
    timestamp_utc: datetime
    session_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    trading_value: float


@dataclass(frozen=True)
class CryptoVenueView:
    """A single-venue immutable bar view; mixed construction is rejected."""

    venue: CryptoVenue
    bars: tuple[CryptoBar, ...]
    holdout_policy: HoldoutPolicy = CRYPTO_HOLDOUT_POLICY

    def __post_init__(self) -> None:
        _assert_supported_venue(self.venue)
        mixed = sorted({bar.venue for bar in self.bars if bar.venue != self.venue})
        if mixed:
            raise CryptoVenueMixError(
                f"venue view={self.venue!r} cannot expose mixed rows {mixed!r}"
            )

    def bars_available_at(
        self, symbol: str, session_date: date
    ) -> tuple[CryptoBar, ...]:
        """Return prior/current bars only; pre-listing gets an empty sequence.

        Empty is intentional. It is not a synthetic zero bar or an imputed
        pre-listing history that could distort a cross-sectional universe.
        """
        allowed_session = assert_date_not_holdout(
            session_date,
            policy=self.holdout_policy,
        )
        return tuple(
            bar
            for bar in self.bars
            if bar.symbol == symbol and bar.session_date <= allowed_session
        )

    def require_last_valid_bar(self, symbol: str, session_date: date) -> CryptoBar:
        """Return the latest real bar on/before ``session_date`` or fail loudly."""
        eligible = self.bars_available_at(symbol, session_date)
        if not eligible:
            raise CryptoPrelistingBarsUnavailable(
                f"no listed bar for {symbol!r} at/before {session_date.isoformat()}"
            )
        return max(eligible, key=lambda bar: bar.session_date)

    def liquidate_delisted(
        self,
        *,
        session_date: date,
        held_symbols: set[str],
        delisted_as_of: frozenset[str],
    ) -> tuple[set[str], list[TerminalEvent]]:
        """Emit explicit delist exits at each held symbol's last valid close."""
        allowed_session = assert_date_not_holdout(
            session_date,
            policy=self.holdout_policy,
        )
        last_close_by_symbol: dict[str, float] = {}
        for symbol in held_symbols & set(delisted_as_of):
            try:
                last_close_by_symbol[symbol] = self.require_last_valid_bar(
                    symbol, allowed_session
                ).close
            except CryptoPrelistingBarsUnavailable as exc:
                raise CryptoTerminalPriceUnavailable(
                    f"cannot liquidate delisted {symbol!r} without a last valid bar"
                ) from exc
        return force_exit_delisted_holdings(
            session_date=allowed_session,
            held_symbols=held_symbols,
            delisted_as_of=delisted_as_of,
            last_close_by_symbol=last_close_by_symbol,
        )


@dataclass(frozen=True)
class CryptoVenueAdapter:
    """Adapter whose manifest, table, and resulting view are one venue only."""

    venue: CryptoVenue
    holdout_policy: HoldoutPolicy = CRYPTO_HOLDOUT_POLICY

    def __post_init__(self) -> None:
        _assert_supported_venue(self.venue)

    @property
    def corpus(self) -> ContractBackedCorpusAdapter:
        return ContractBackedCorpusAdapter(
            contract_path=CRYPTO_SCHEMA_CONTRACT_PATH,
            holdout_policy=self.holdout_policy,
        )

    @property
    def cost_model(self) -> CostModel:
        if self.venue == "upbit_krw":
            return UPBIT_KRW_COST
        return BINANCE_USDT_COST

    def load_manifest(self, manifest_path: Path | str) -> list[ManifestEntry]:
        entries = self.corpus.load_manifest(manifest_path)
        for entry in entries:
            self._assert_entry_venue(entry)
        return entries

    def load_shard(
        self,
        artifact_root: Path | str,
        entry: ManifestEntry,
        *,
        allowed_window_start: date | str | None = None,
        allowed_window_end: date | str | None = None,
    ) -> CryptoVenueView:
        self._assert_entry_venue(entry)
        table = self.corpus.load_shard(
            artifact_root,
            entry,
            allowed_window_start=allowed_window_start,
            allowed_window_end=allowed_window_end,
        )
        return self.view_from_table(table)

    def view_from_table(self, table: pa.Table) -> CryptoVenueView:
        """Validate one table and refuse even one row from another venue."""
        self.corpus.validate_table_schema(table, "ohlcv")
        data = table.to_pydict()
        bars: list[CryptoBar] = []
        for i in range(table.num_rows):
            row_venue = _assert_supported_venue(str(data["venue"][i]))
            if row_venue != self.venue:
                raise CryptoVenueMixError(
                    f"row {i} venue={row_venue!r} cannot enter {self.venue!r} view"
                )
            raw_utc = parse_utc_timestamp(data["timestamp_utc"][i])
            derived = raw_utc.date()  # 24/7 UTC calendar day; no session calendar.
            self.corpus.assert_date_allowed(derived)
            try:
                declared = date.fromisoformat(data["session_date"][i])
            except (TypeError, ValueError) as exc:
                raise CryptoSessionDateMismatchError(
                    f"row {i} session_date must be ISO UTC date, got "
                    f"{data['session_date'][i]!r}"
                ) from exc
            if declared != derived:
                raise CryptoSessionDateMismatchError(
                    f"row {i} session_date={declared.isoformat()} differs from "
                    f"UTC timestamp date {derived.isoformat()}"
                )
            bars.append(
                CryptoBar(
                    symbol=str(data["symbol"][i]),
                    venue=row_venue,
                    timestamp_utc=raw_utc,
                    session_date=derived,
                    open=float(data["open"][i]),
                    high=float(data["high"][i]),
                    low=float(data["low"][i]),
                    close=float(data["close"][i]),
                    volume=float(data["volume"][i]),
                    trading_value=float(data["trading_value"][i]),
                )
            )
        return CryptoVenueView(
            venue=self.venue,
            bars=tuple(bars),
            holdout_policy=self.holdout_policy,
        )

    def _assert_entry_venue(self, entry: ManifestEntry) -> None:
        if entry.market != self.venue:
            raise CryptoVenueMixError(
                f"manifest entry market={entry.market!r} cannot enter "
                f"{self.venue!r} adapter"
            )
