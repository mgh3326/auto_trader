"""US daily-bar adapter: raw UTC timestamps and XNYS-derived session dates.

This is fixture-only contract wiring. It intentionally does not fetch corpus
data or execute a backtest. A source row must carry both raw ``timestamp_utc``
and a declared session_date; disagreement with America/New_York derivation is
a loud contract failure, preventing a KST-anchored daily candle from silently
shifting every US row one day backward.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pyarrow as pa
from holdout_guard import HOLDOUT_END, HOLDOUT_START, HoldoutPolicy
from loader import ManifestEntry
from market_adapters.common import ContractBackedCorpusAdapter, parse_utc_timestamp
from market_adapters.costs import CostModel
from membership import MembershipRow, membership_rows_from_table
from terminal_events import TerminalEvent, force_exit_delisted_holdings

__all__ = [
    "US_ADAPTER",
    "US_CALENDAR",
    "US_DEFAULT_COST",
    "US_HOLDOUT_POLICY",
    "US_SCHEMA_CONTRACT_PATH",
    "US_SESSION_TIMEZONE",
    "US_SLIPPAGE_SENSITIVITY_COST",
    "USBar",
    "USCalendarError",
    "USMarketAdapter",
    "USSessionDateMismatchError",
    "derive_us_session_date",
    "is_xnys_early_close",
    "is_xnys_session",
]

US_CALENDAR = "XNYS"
US_SESSION_TIMEZONE = ZoneInfo("America/New_York")
US_SCHEMA_CONTRACT_PATH = (
    Path(__file__).resolve().parent / "contracts" / "us-corpus-v1.schema.json"
)
# This literal is a guard-only path; this module never reads this corpus root.
US_HOLDOUT_POLICY = HoldoutPolicy(
    holdout_dir=Path("/Users/mgh3326/work/herdr-artifacts/us-corpus-v1/holdout/"),
    start=HOLDOUT_START,
    end=HOLDOUT_END,
)
US_DEFAULT_COST = CostModel(fee_bp=0, slippage_bp_per_side=10)
US_SLIPPAGE_SENSITIVITY_COST = CostModel(fee_bp=0, slippage_bp_per_side=5)


class USCalendarError(ValueError):
    """A derived US session is not an XNYS trading session."""


class USSessionDateMismatchError(ValueError):
    """Declared session_date differs from raw-UTC → ET derivation."""


@lru_cache(maxsize=1)
def _xnys_calendar():
    return xcals.get_calendar(US_CALENDAR)


def derive_us_session_date(timestamp_utc: datetime | str) -> date:
    """Derive the US calendar date from an unmodified UTC source timestamp."""
    raw_utc = parse_utc_timestamp(timestamp_utc)
    return raw_utc.astimezone(US_SESSION_TIMEZONE).date()


def is_xnys_session(session_date: date) -> bool:
    """Return whether ``session_date`` is a regular XNYS session day."""
    return bool(_xnys_calendar().is_session(session_date))


def is_xnys_early_close(session_date: date) -> bool:
    """Return whether an open XNYS session closes before 16:00 ET."""
    calendar = _xnys_calendar()
    if not calendar.is_session(session_date):
        return False
    close_et = (
        calendar.session_close(session_date)
        .to_pydatetime()
        .astimezone(US_SESSION_TIMEZONE)
    )
    return close_et.timetz().replace(tzinfo=None) < time(16, 0)


@dataclass(frozen=True)
class USBar:
    """US bar preserving raw UTC time alongside its ET-derived session date."""

    symbol: str
    timestamp_utc: datetime
    session_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    trading_value: float
    market: str


@dataclass(frozen=True)
class USMarketAdapter:
    """Bind the US inferred contract to shared holdout/SHA/schema guards."""

    holdout_policy: HoldoutPolicy = US_HOLDOUT_POLICY

    @property
    def corpus(self) -> ContractBackedCorpusAdapter:
        return ContractBackedCorpusAdapter(
            contract_path=US_SCHEMA_CONTRACT_PATH,
            holdout_policy=self.holdout_policy,
        )

    def load_manifest(self, manifest_path: Path | str) -> list[ManifestEntry]:
        return self.corpus.load_manifest(manifest_path)

    def load_shard(
        self,
        artifact_root: Path | str,
        entry: ManifestEntry,
        *,
        allowed_window_start: date | str | None = None,
        allowed_window_end: date | str | None = None,
    ) -> list[USBar]:
        """Load an OHLCV shard through shared guards, then enforce ET dates."""
        table = self.corpus.load_shard(
            artifact_root,
            entry,
            allowed_window_start=allowed_window_start,
            allowed_window_end=allowed_window_end,
        )
        return self.bars_from_table(table)

    def bars_from_table(self, table: pa.Table) -> list[USBar]:
        """Validate rows, derive ET dates, and preserve the raw UTC instant."""
        self.corpus.validate_table_schema(table, "ohlcv")
        data = table.to_pydict()
        bars: list[USBar] = []
        for i in range(table.num_rows):
            raw_utc = parse_utc_timestamp(data["timestamp_utc"][i])
            derived = derive_us_session_date(raw_utc)
            self.corpus.assert_date_allowed(derived)
            try:
                declared = date.fromisoformat(data["session_date"][i])
            except (TypeError, ValueError) as exc:
                raise USSessionDateMismatchError(
                    f"row {i} session_date must be ISO date, got "
                    f"{data['session_date'][i]!r}"
                ) from exc
            if declared != derived:
                raise USSessionDateMismatchError(
                    f"row {i} session_date={declared.isoformat()} differs from "
                    f"UTC timestamp {raw_utc.isoformat()} derived ET date "
                    f"{derived.isoformat()}"
                )
            if not is_xnys_session(derived):
                raise USCalendarError(
                    f"row {i} ET-derived session_date {derived.isoformat()} "
                    "is not an XNYS session"
                )
            market = str(data["market"][i])
            if market != "US":
                raise ValueError(f"row {i} market must be 'US', got {market!r}")
            bars.append(
                USBar(
                    symbol=str(data["symbol"][i]),
                    timestamp_utc=raw_utc,
                    session_date=derived,
                    open=float(data["open"][i]),
                    high=float(data["high"][i]),
                    low=float(data["low"][i]),
                    close=float(data["close"][i]),
                    volume=float(data["volume"][i]),
                    trading_value=float(data["trading_value"][i]),
                    market=market,
                )
            )
        return bars

    def membership_from_table(self, table: pa.Table) -> list[MembershipRow]:
        """Reuse shared PIT membership parsing after US-contract validation."""
        self.corpus.validate_table_schema(table, "membership")
        return membership_rows_from_table(table)

    def terminalize_delisted(
        self,
        *,
        session_date: date,
        held_symbols: set[str],
        delisted_as_of: frozenset[str],
        last_close_by_symbol: dict[str, float],
    ) -> tuple[set[str], list[TerminalEvent]]:
        """Reuse the shared explicit terminal-event path for US delists."""
        self.corpus.assert_date_allowed(session_date)
        return force_exit_delisted_holdings(
            session_date=session_date,
            held_symbols=held_symbols,
            delisted_as_of=delisted_as_of,
            last_close_by_symbol=last_close_by_symbol,
        )


US_ADAPTER = USMarketAdapter()
