"""US daily-bar adapter for sealed us-corpus-v1 wiring.

Sealed US parquet carries ``symbol``, ``session_date`` (timestamp[ms] naive
XNYS calendar day), OHLCV, and ``volume``. It has **no** raw UTC timestamp
column and **no** exchange-reported ``trading_value``.

``trading_value`` resolution is ABSENT_DECLARED: this adapter never populates
a field of that name. Synthesizing ``close × volume`` under ``price_mode=
adjusted`` would invent a false dollar turnover comparable to KR exchange
``value`` — that is forbidden.

``market='US'`` is injected on the bar object only; it is not a parquet column.
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
from market_adapters.common import ContractBackedCorpusAdapter
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
    "US_TRADING_VALUE_RESOLUTION",
    "USBar",
    "USCalendarError",
    "USMarketAdapter",
    "USNullCellError",
    "is_xnys_early_close",
    "is_xnys_session",
]

US_CALENDAR = "XNYS"
US_SESSION_TIMEZONE = ZoneInfo("America/New_York")
US_SCHEMA_CONTRACT_PATH = (
    Path(__file__).resolve().parent / "contracts" / "us-corpus-v1.schema.json"
)
US_TRADING_VALUE_RESOLUTION = "ABSENT_DECLARED"
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


class USNullCellError(ValueError):
    """A required sealed-corpus cell was null (schema nullable, value refused)."""


@lru_cache(maxsize=1)
def _xnys_calendar():
    return xcals.get_calendar(US_CALENDAR)


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


def _require_cell(value: object, *, field: str, row: int) -> object:
    if value is None:
        raise USNullCellError(f"row {row} required field {field!r} is null")
    return value


def _session_date_from_cell(value: object, *, row: int) -> date:
    value = _require_cell(value, field="session_date", row=row)
    if type(value) is date:
        return value
    if isinstance(value, datetime):
        return value.date()
    if type(value) is str:
        return date.fromisoformat(value)
    raise USNullCellError(
        f"row {row} session_date has unsupported type {type(value)!r}"
    )


@dataclass(frozen=True)
class USBar:
    """US bar from sealed corpus fields only — no trading_value, no invented UTC."""

    symbol: str
    session_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    market: str
    price_mode: str


@dataclass(frozen=True)
class USMarketAdapter:
    """Bind the US sealed contract to shared holdout/SHA/schema guards."""

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
        """Load an OHLCV shard through shared guards, then map sealed columns."""
        table = self.corpus.load_shard(
            artifact_root,
            entry,
            allowed_window_start=allowed_window_start,
            allowed_window_end=allowed_window_end,
        )
        return self.bars_from_table(table)

    def bars_from_table(self, table: pa.Table) -> list[USBar]:
        """Validate sealed schema and build bars without inventing turnover."""
        self.corpus.validate_table_schema(table, "ohlcv")
        if "trading_value" in table.column_names:
            # Defense in depth: contract forbidden_columns should already refuse.
            raise ValueError(
                "US sealed corpus must not carry trading_value; "
                "synthesis is forbidden (US_TRADING_VALUE_RESOLUTION="
                f"{US_TRADING_VALUE_RESOLUTION})"
            )
        price_mode = "adjusted"
        meta = table.schema.metadata or {}
        for key, raw in meta.items():
            k = key.decode() if isinstance(key, bytes) else str(key)
            if k == "price_mode":
                price_mode = raw.decode() if isinstance(raw, bytes) else str(raw)
        data = table.to_pydict()
        bars: list[USBar] = []
        for i in range(table.num_rows):
            session = _session_date_from_cell(data["session_date"][i], row=i)
            self.corpus.assert_date_allowed(session)
            if not is_xnys_session(session):
                raise USCalendarError(
                    f"row {i} session_date {session.isoformat()} is not an XNYS session"
                )
            bars.append(
                USBar(
                    symbol=str(_require_cell(data["symbol"][i], field="symbol", row=i)),
                    session_date=session,
                    open=float(_require_cell(data["open"][i], field="open", row=i)),
                    high=float(_require_cell(data["high"][i], field="high", row=i)),
                    low=float(_require_cell(data["low"][i], field="low", row=i)),
                    close=float(_require_cell(data["close"][i], field="close", row=i)),
                    volume=int(_require_cell(data["volume"][i], field="volume", row=i)),
                    market="US",
                    price_mode=price_mode,
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
