"""Venue-isolated crypto adapter for sealed crypto-corpus-v1 wiring.

No API in this module accepts more than one venue. A view is bound to exactly
one of ``upbit_krw`` or ``binance_usdt_spot`` and raises on a mismatched
manifest entry or row. Venue strings match the sealed corpus exactly — they
are never shortened or cross-venue flattened.

This adapter is a **daily (1d)** harness binding:

* every row's ``frequency`` value must equal ``CRYPTO_ADAPTER_FREQUENCY``
  (``"1d"``); hourly (or other) input raises ``CryptoFrequencyMismatchError``
* ``frequency`` is carried on every ``CryptoBar``
* every public path that returns OHLCV data enforces the sealed **label**
  gate (``policy_from_parquet_metadata``) — unlabeled tables fail closed

Bar time is ``open_time_utc`` (inclusive). ``close_time_utc`` is the exclusive
end from the sealed builder. Session date is the UTC calendar day of the open.
``volume`` is ``base_volume``; ``quote_volume`` keeps its venue quote currency
via ``quote_currency`` (KRW or USDT). There is no unitless ``trading_value``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
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

from research.crypto_corpus.policy import (
    VenuePolicy,
    policy_from_parquet_metadata,
)

__all__ = [
    "BINANCE_USDT_COST",
    "CRYPTO_ADAPTER_FREQUENCY",
    "CRYPTO_ADAPTER_PUBLIC_LOAD_ENTRYPOINTS",
    "CRYPTO_CALENDAR",
    "CRYPTO_HOLDOUT_POLICY",
    "CRYPTO_SCHEMA_CONTRACT_PATH",
    "QUOTE_CURRENCY_BY_VENUE",
    "CryptoBar",
    "CryptoFrequencyMismatchError",
    "CryptoPrelistingBarsUnavailable",
    "CryptoSessionDateMismatchError",
    "CryptoTerminalPriceUnavailable",
    "CryptoVenueAdapter",
    "CryptoVenueMixError",
    "CryptoVenueView",
    "CryptoVenue",
    "UPBIT_KRW_COST",
    "assert_crypto_table_labeled",
    "assert_crypto_table_frequency",
]

CryptoVenue = Literal["upbit_krw", "binance_usdt_spot"]
QuoteCurrency = Literal["KRW", "USDT"]
CryptoFrequency = Literal["1d"]
_SUPPORTED_VENUES: frozenset[str] = frozenset({"upbit_krw", "binance_usdt_spot"})
QUOTE_CURRENCY_BY_VENUE: dict[str, QuoteCurrency] = {
    "upbit_krw": "KRW",
    "binance_usdt_spot": "USDT",
}
# This adapter binds daily bars only. Hourly (1h) sealed files must not enter.
CRYPTO_ADAPTER_FREQUENCY: CryptoFrequency = "1d"
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

# Complete public surface that can return OHLCV table/bars. Keep this list
# honest: every entry must enforce the label gate (and frequency for bar paths).
CRYPTO_ADAPTER_PUBLIC_LOAD_ENTRYPOINTS: tuple[str, ...] = (
    "CryptoVenueAdapter.load_shard",
    "CryptoVenueAdapter.view_from_table",
    "CryptoVenueAdapter.corpus.load_shard",
)


class CryptoVenueMixError(ValueError):
    """Rows or manifest entries attempted to cross a bound venue boundary."""


class CryptoSessionDateMismatchError(ValueError):
    """UTC open-day does not match a declared session or close boundary."""


class CryptoFrequencyMismatchError(ValueError):
    """A row frequency is not the adapter's bound daily frequency."""


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


def _as_utc_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            # Sealed parquet is tz=UTC; a naive value is still refused to avoid
            # local re-anchoring.
            raise CryptoSessionDateMismatchError(
                "open/close_time_utc must be timezone-aware UTC"
            )
        return value.astimezone(UTC)
    return parse_utc_timestamp(value)


def assert_crypto_table_labeled(
    table: pa.Table,
    *,
    expected_venue: str,
) -> VenuePolicy:
    """Refuse unlabeled or mislabeled tables (fail-closed; never empty-filter)."""
    return policy_from_parquet_metadata(
        table.schema.metadata,
        expected_venue=expected_venue,
    )


def assert_crypto_table_frequency(
    table: pa.Table,
    *,
    expected: str = CRYPTO_ADAPTER_FREQUENCY,
) -> None:
    """Refuse any row whose frequency value is not the adapter binding.

    Silent filtering of non-matching rows is forbidden — one bad value raises.
    """
    if "frequency" not in table.column_names:
        raise CryptoFrequencyMismatchError(
            "crypto OHLCV table missing frequency column"
        )
    values = table.column("frequency").to_pylist()
    for i, raw in enumerate(values):
        if raw is None or str(raw) != expected:
            raise CryptoFrequencyMismatchError(
                f"row {i} frequency={raw!r} is not adapter frequency "
                f"{expected!r}; refusing non-daily input into the daily harness"
            )


@dataclass(frozen=True)
class CryptoBar:
    """Single-venue crypto bar with explicit base/quote volume units."""

    symbol: str
    venue: CryptoVenue
    frequency: CryptoFrequency
    open_time_utc: datetime
    close_time_utc: datetime
    session_date: date
    open: float
    high: float
    low: float
    close: float
    base_volume: float
    quote_volume: float
    quote_currency: QuoteCurrency

    @property
    def volume(self) -> float:
        """Base-asset volume (sealed ``base_volume``)."""
        return self.base_volume

    @property
    def timestamp_utc(self) -> datetime:
        """Bar time = inclusive open (sealed bar-time decision)."""
        return self.open_time_utc


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
        bad_freq = sorted(
            {
                bar.frequency
                for bar in self.bars
                if bar.frequency != CRYPTO_ADAPTER_FREQUENCY
            }
        )
        if bad_freq:
            raise CryptoFrequencyMismatchError(
                f"venue view cannot expose non-daily frequencies {bad_freq!r}"
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


class _LabeledCryptoCorpusFacade:
    """Public corpus surface: shared guards + label + frequency on load_shard.

    Callers must not use the unwrapped ``ContractBackedCorpusAdapter`` for
    crypto OHLCV: this facade is the only ``.corpus`` object the adapter
    exposes, so residual ``corpus.load_shard`` cannot skip the label gate.
    """

    def __init__(
        self,
        *,
        venue: CryptoVenue,
        holdout_policy: HoldoutPolicy,
    ) -> None:
        self._venue = venue
        self._inner = ContractBackedCorpusAdapter(
            contract_path=CRYPTO_SCHEMA_CONTRACT_PATH,
            holdout_policy=holdout_policy,
        )

    @property
    def holdout_policy(self) -> HoldoutPolicy:
        return self._inner.holdout_policy

    @property
    def contract_path(self) -> Path:
        return self._inner.contract_path

    def contract(self) -> dict:
        return self._inner.contract()

    def arrow_schema_for(self, dataset: str) -> pa.Schema:
        return self._inner.arrow_schema_for(dataset)

    def validate_table_schema(self, table: pa.Table, dataset: str) -> None:
        self._inner.validate_table_schema(table, dataset)

    def assert_path_allowed(self, path: Path | str) -> Path:
        return self._inner.assert_path_allowed(path)

    def assert_date_allowed(self, value: date | datetime | str) -> date:
        return self._inner.assert_date_allowed(value)

    def assert_range_allowed(
        self,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> tuple[date, date]:
        return self._inner.assert_range_allowed(start, end)

    def load_manifest(self, manifest_path: Path | str) -> list[ManifestEntry]:
        return self._inner.load_manifest(manifest_path)

    def load_shard(
        self,
        artifact_root: Path | str,
        entry: ManifestEntry,
        *,
        allowed_window_start: date | str | None = None,
        allowed_window_end: date | str | None = None,
    ) -> pa.Table:
        table = self._inner.load_shard(
            artifact_root,
            entry,
            allowed_window_start=allowed_window_start,
            allowed_window_end=allowed_window_end,
        )
        # Label gate BEFORE returning any rows (BLOCKER-2).
        assert_crypto_table_labeled(table, expected_venue=self._venue)
        # Frequency value gate on the raw table path too (BLOCKER-1 residual).
        if entry.dataset == "ohlcv":
            assert_crypto_table_frequency(table)
        return table


@dataclass(frozen=True)
class CryptoVenueAdapter:
    """Adapter whose manifest, table, and resulting view are one venue only."""

    venue: CryptoVenue
    holdout_policy: HoldoutPolicy = CRYPTO_HOLDOUT_POLICY

    def __post_init__(self) -> None:
        _assert_supported_venue(self.venue)

    @property
    def corpus(self) -> _LabeledCryptoCorpusFacade:
        return _LabeledCryptoCorpusFacade(
            venue=self.venue,
            holdout_policy=self.holdout_policy,
        )

    @property
    def cost_model(self) -> CostModel:
        if self.venue == "upbit_krw":
            return UPBIT_KRW_COST
        return BINANCE_USDT_COST

    @property
    def quote_currency(self) -> QuoteCurrency:
        return QUOTE_CURRENCY_BY_VENUE[self.venue]

    @property
    def frequency(self) -> CryptoFrequency:
        return CRYPTO_ADAPTER_FREQUENCY

    def load_manifest(self, manifest_path: Path | str) -> list[ManifestEntry]:
        """Load manifest JSON only (no OHLCV rows). Venue entries are checked."""
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
        """Public OHLCV load: holdout + SHA + schema + **label** + frequency."""
        self._assert_entry_venue(entry)
        # corpus.load_shard enforces label + frequency before returning table.
        table = self.corpus.load_shard(
            artifact_root,
            entry,
            allowed_window_start=allowed_window_start,
            allowed_window_end=allowed_window_end,
        )
        return self.view_from_table(table)

    def view_from_table(self, table: pa.Table) -> CryptoVenueView:
        """Validate one table: schema + label + frequency + venue isolation.

        Public bar-construction path. Unlabeled metadata or any non-``1d``
        frequency value raises — never a silent filter or empty success.
        """
        self.corpus.validate_table_schema(table, "ohlcv")
        # Label gate (BLOCKER-2): stripped metadata must not produce bars.
        assert_crypto_table_labeled(table, expected_venue=self.venue)
        # Frequency value gate (BLOCKER-1): 1h must not enter daily adapter.
        assert_crypto_table_frequency(table)

        data = table.to_pydict()
        bars: list[CryptoBar] = []
        for i in range(table.num_rows):
            row_venue = _assert_supported_venue(str(data["venue"][i]))
            if row_venue != self.venue:
                raise CryptoVenueMixError(
                    f"row {i} venue={row_venue!r} cannot enter {self.venue!r} view"
                )
            freq = str(data["frequency"][i])
            if freq != CRYPTO_ADAPTER_FREQUENCY:
                # Defense in depth after table-level assert.
                raise CryptoFrequencyMismatchError(
                    f"row {i} frequency={freq!r} is not {CRYPTO_ADAPTER_FREQUENCY!r}"
                )
            open_utc = _as_utc_datetime(data["open_time_utc"][i])
            close_utc = _as_utc_datetime(data["close_time_utc"][i])
            if close_utc <= open_utc:
                raise CryptoSessionDateMismatchError(
                    f"row {i} close_time_utc must be exclusive end after open_time_utc"
                )
            derived = open_utc.date()  # 24/7 UTC calendar day of bar open.
            self.corpus.assert_date_allowed(derived)
            bars.append(
                CryptoBar(
                    symbol=str(data["symbol"][i]),
                    venue=row_venue,
                    frequency=CRYPTO_ADAPTER_FREQUENCY,
                    open_time_utc=open_utc,
                    close_time_utc=close_utc,
                    session_date=derived,
                    open=float(data["open"][i]),
                    high=float(data["high"][i]),
                    low=float(data["low"][i]),
                    close=float(data["close"][i]),
                    base_volume=float(data["base_volume"][i]),
                    quote_volume=float(data["quote_volume"][i]),
                    quote_currency=QUOTE_CURRENCY_BY_VENUE[row_venue],
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
