"""Closed vocabularies, record types and the frozen-rules pin (hk 6036).

The migration ``20261125_hk6036_upbit_shadow`` repeats these vocabularies as
literals; a test keeps the two in step.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from research.upbit_intraday import rules as R

RULES_VERSION = "upbit-intraday-v1"
#: ``code_rules_sha256`` of ``research/experiments/manifests/hk-6035-...json``.
FROZEN_RULES_SHA256 = "fbac66aad94a758d863962bcef26bc2e7b6f15afd8b2d5e95868d9806fbe03a3"

FEE_RATE_PER_SIDE = Decimal("0.0005")
#: A closed bar is read only after Upbit has had this long to finish it.
SETTLE = timedelta(seconds=10)
#: A market-alert observation later than this after the signal time says nothing
#: about the state at the signal time.
MARKET_EVENT_MAX_LAG = timedelta(minutes=30)
#: Slots in the evaluated panel (2 days). One candle page (200 rows) covers it.
PANEL_SLOTS = 192
#: Newest closed bars evaluated per run; the rest of the panel is lookback.
DEFAULT_CATCHUP_BARS = 4
#: Every evaluated bar keeps a full 96-slot window for its 24h quote volume.
MAX_CATCHUP_BARS = PANEL_SLOTS - R.SLOTS_PER_DAY

DISPOSITIONS = ("signal", "excluded")
REASON_MARKET_EVENT_WARNING = "market_event_warning"
REASON_MARKET_EVENT_CAUTION = "market_event_caution"
REASON_MARKET_EVENT_UNKNOWN = "market_event_unknown"
REASON_MARKET_EVENT_STALE = "market_event_stale"
REASON_LIQUIDITY_BELOW_FLOOR = "liquidity_below_floor"
REASON_COOLDOWN_UNVERIFIED = "cooldown_unverified"
#: Order is the order reasons appear on a row.
EXCLUSION_REASONS = (
    REASON_MARKET_EVENT_WARNING,
    REASON_MARKET_EVENT_CAUTION,
    REASON_MARKET_EVENT_UNKNOWN,
    REASON_MARKET_EVENT_STALE,
    REASON_LIQUIDITY_BELOW_FLOOR,
    REASON_COOLDOWN_UNVERIFIED,
)

ENTRY_NEXT_BAR_OPEN = "next_bar_open"
ENTRY_SOURCES = (ENTRY_NEXT_BAR_OPEN,)
EXIT_BAR_CLOSE = "exit_bar_close"
EXIT_LAST_TRADED_CLOSE = "last_traded_close"
EXIT_BASES = (EXIT_BAR_CLOSE, EXIT_LAST_TRADED_CLOSE)
MISSING_NEXT_BAR_NO_TRADES = "next_bar_no_trades"
MISSING_MARKET_UNAVAILABLE = "market_unavailable"
MISSING_REASONS = (MISSING_NEXT_BAR_NO_TRADES, MISSING_MARKET_UNAVAILABLE)

HORIZONS = R.HORIZONS


class FrozenRulesChanged(RuntimeError):
    pass


def rules_sha256() -> str:
    return hashlib.sha256(Path(R.__file__).read_bytes()).hexdigest()


def verify_frozen_rules() -> None:
    """Refuse to record under ``RULES_VERSION`` if the rules file changed."""
    actual = rules_sha256()
    if actual != FROZEN_RULES_SHA256:
        raise FrozenRulesChanged(f"frozen code changed: rules.py sha256 {actual}")


def selected_config_ids() -> frozenset[str]:
    """The three configurations chosen on the develop half (``frozen_selection.json``)."""
    path = Path(R.__file__).with_name("frozen_selection.json")
    selected = json.loads(path.read_text())["selected"]
    return frozenset(
        R.Config(v["family"], int(v["n"]), float(v["x"])).id for v in selected.values()
    )


@dataclass(frozen=True)
class Candidate:
    """One signal, or one candidate held back by an exclusion, on one closed bar."""

    rules_version: str
    rules_sha256: str
    market: str
    family: str
    config_id: str
    n_bars: int
    x_multiple: Decimal
    direction: int
    selected_config: bool
    signal_bar_open_at: datetime
    signal_time: datetime
    first_received_at: datetime
    disposition: str
    exclusion_reasons: tuple[str, ...]
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    quote_volume: Decimal
    ref_level: Decimal
    breakout_distance: Decimal
    median_quote_volume: Decimal
    volume_ratio: Decimal
    quote_volume_24h: Decimal
    liquidity_bucket: str
    fee_rate_per_side: Decimal
    roundtrip_cost_rate: Decimal
    signal_tick_krw: Decimal
    market_event_state: str
    market_event_caution_types: tuple[str, ...]
    market_event_observed_at: datetime | None


@dataclass(frozen=True)
class PendingCandidate:
    """What outcome resolution needs from a recorded candidate."""

    candidate_id: int | None
    market: str
    config_id: str
    direction: int
    disposition: str
    signal_bar_open_at: datetime
    roundtrip_cost_rate: Decimal
    resolved_horizons: frozenset[int] = frozenset()


@dataclass(frozen=True)
class VirtualTrade:
    """A virtual round trip under the frozen exit rule. Never a fill."""

    candidate_id: int | None
    market: str
    config_id: str
    disposition: str
    horizon_bars: int
    entry_bar_open_at: datetime
    exit_bar_open_at: datetime
    entry_source: str | None
    virtual_entry_price: Decimal | None
    exit_basis: str | None
    virtual_exit_price: Decimal | None
    entry_tick_krw: Decimal | None
    exit_tick_krw: Decimal | None
    gross_return: Decimal | None
    roundtrip_cost_rate: Decimal
    net_return: Decimal | None
    mae: Decimal | None
    mfe: Decimal | None
    missing_reason: str | None
    outcome_received_at: datetime
