from __future__ import annotations

import datetime as dt
from typing import Literal
from zoneinfo import ZoneInfo

from app.services.market_events.freshness_service import STALE_AFTER_HOURS

DataState = Literal["fresh", "partial", "stale", "missing", "fallback"]

# ROB-281: KR schedule slot taxonomy.
# pre_market_repair fires at 07:40 KST and targets the prior day's NXT-final
# (it does not produce same-day data). krx_preliminary fires at 16:20 KST after
# KRX regular session, nxt_final at 20:20 KST after NXT after-market.
KRSessionSlot = Literal["pre_market_repair", "krx_preliminary", "nxt_final"]

_KST = ZoneInfo("Asia/Seoul")
_TZ_BY_MARKET = {"kr": _KST, "us": ZoneInfo("America/New_York")}
_PARTIAL_MAX_LEN = (
    5  # closes_window length < 5 → partial (week_change_rate not computable)
)


def today_trading_date(market: str, *, now: dt.datetime | None = None) -> dt.date:
    """Most recent business day in the market's timezone.

    NOTE: First-slice does NOT use exchange holiday calendar. KIS daily candles
    already collapse Korean public holidays into the previous trading day close,
    so this approximation is safe for snapshot freshness classification.
    """
    tz = _TZ_BY_MARKET.get(market, _TZ_BY_MARKET["kr"])
    now_local = (now or dt.datetime.now(dt.UTC)).astimezone(tz)
    candidate = now_local.date()
    while candidate.weekday() >= 5:
        candidate -= dt.timedelta(days=1)
    return candidate


def _prior_weekday(d: dt.date) -> dt.date:
    prior = d - dt.timedelta(days=1)
    while prior.weekday() >= 5:
        prior -= dt.timedelta(days=1)
    return prior


def classify_kr_session_slot(now: dt.datetime) -> KRSessionSlot:
    """Return the KR schedule slot most recently fired at-or-before ``now``.

    Slot boundaries (KST, both endpoints inclusive on the start):

    ====================  ==========================================
    KST window            slot
    ====================  ==========================================
    00:00 – 07:39         ``nxt_final`` (prior day's slot still authoritative)
    07:40 – 16:19         ``pre_market_repair``
    16:20 – 20:19         ``krx_preliminary``
    20:20 – 23:59         ``nxt_final``
    ====================  ==========================================

    Weekends and KR holidays are out of scope here; callers should gate
    actionable use on ``exchange_calendars.get_calendar("XKRX").is_session``.
    Naive ``now`` is treated as UTC and converted to KST.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    kst = now.astimezone(_KST)
    hm = (kst.hour, kst.minute)
    if hm >= (20, 20):
        return "nxt_final"
    if hm >= (16, 20):
        return "krx_preliminary"
    if hm >= (7, 40):
        return "pre_market_repair"
    return "nxt_final"


def expected_kr_baseline_date(now: dt.datetime) -> dt.date:
    """The KR trading date for which a snapshot is EXPECTED to exist at ``now``.

    Critically, in the 07:40 – 16:19 KST window (pre-market repair, before
    today's 16:20 KRX preliminary has fired), the expected baseline is the
    PRIOR trading day — not today. Using raw :func:`today_trading_date` here
    would mark a fresh prior-day partition as stale just because the clock
    rolled past midnight, surfacing a misleading "1일 지연" label even when
    the previous NXT-final ran successfully.

    Resolution:

    * Before 16:20 KST → prior trading weekday.
    * 16:20 KST or later → today (rolled back to weekday).

    Holidays are not consulted here for the same reasons as
    :func:`today_trading_date`; daily candles upstream already collapse KR
    public holidays into the prior trading day close.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    kst = now.astimezone(_KST)
    today_kst = kst.date()
    while today_kst.weekday() >= 5:
        today_kst -= dt.timedelta(days=1)
    if (kst.hour, kst.minute) >= (16, 20):
        return today_kst
    return _prior_weekday(today_kst)


def kr_session_label_for_partition(
    partition_computed_at: dt.datetime | None,
) -> str | None:
    """User-facing KR session label for a snapshot's ``computed_at``.

    Maps the KST time-of-day at which the partition was computed to a token:

    * ``16:20 – 20:19 KST`` → ``"KRX preliminary"``
    * ``20:20 – 23:59 KST`` → ``"NXT final"``
    * ``00:00 – 06:59 KST`` → ``"NXT final"`` (overnight tail of prior day's run)
    * ``07:40 – 16:19 KST`` → ``"NXT final"`` (repair window targets prior NXT-final)

    Returns ``None`` for the rare 07:00 – 07:39 KST gap (between overnight
    rollover and the pre-market repair slot) or when ``partition_computed_at``
    is ``None``. Callers in that case fall back to the existing
    :func:`format_kst_as_of_label` without appending a session token.
    """
    if partition_computed_at is None:
        return None
    if partition_computed_at.tzinfo is None:
        partition_computed_at = partition_computed_at.replace(tzinfo=dt.UTC)
    kst = partition_computed_at.astimezone(_KST)
    hm = (kst.hour, kst.minute)
    if (16, 20) <= hm < (20, 20):
        return "KRX preliminary"
    if hm >= (20, 20):
        return "NXT final"
    if hm < (7, 0):
        return "NXT final"
    if (7, 40) <= hm < (16, 20):
        return "NXT final"
    return None


def classify_state(
    *,
    snapshot_date: dt.date,
    computed_at: dt.datetime,
    closes_window_len: int,
    today_trading_date_value: dt.date,
    now: dt.datetime,
) -> DataState:
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=dt.UTC)
    age_hours = (now - computed_at).total_seconds() / 3600.0
    if snapshot_date != today_trading_date_value or age_hours >= STALE_AFTER_HOURS:
        return "stale"
    if closes_window_len < 2:
        return "missing"  # not really usable; treat as absent
    if closes_window_len < _PARTIAL_MAX_LEN:
        return "partial"
    return "fresh"


_PRIORITY: dict[DataState, int] = {
    "missing": 0,
    "fallback": 1,
    "stale": 2,
    "partial": 3,
    "fresh": 4,
}


def aggregate_states(states: list[DataState]) -> DataState:
    if not states:
        return "missing"
    has_missing = "missing" in states
    has_fresh_or_partial = any(s in {"fresh", "partial"} for s in states)
    if has_missing and has_fresh_or_partial:
        return "fallback"
    return min(states, key=lambda s: _PRIORITY[s])


def classify_investor_flow_partition(
    *,
    snapshot_date: dt.date,
    collected_at: dt.datetime | None,
    today_trading_date_value: dt.date,
    now: dt.datetime,
) -> DataState:
    """Classify an investor_flow_snapshots partition's freshness.

    Mirrors classify_state() but without closes_window_len (investor_flow rows have
    no candle window). Primary staleness check: snapshot_date must match
    today_trading_date_value (which already rolls weekends back to Friday).
    Secondary age guard: mirrors classify_state() to catch orphaned partitions
    stamped with today's trading date but written long ago. Stays in this module
    so KST/trading-date logic lives in one place per ROB-277 §D4.
    """
    if snapshot_date != today_trading_date_value:
        return "stale"
    if collected_at is not None:
        age_hours = (now - collected_at).total_seconds() / 3600.0
        if age_hours >= STALE_AFTER_HOURS:
            return "stale"
    return "fresh"


def compute_overall_state(
    *,
    primary_state: DataState,
    dependency_states: list[DataState],
) -> DataState:
    """Aggregate primary + dependency states per ROB-277 §D1.c.

    NOT the same as aggregate_states(): when primary is fresh but a dependency is
    stale/missing, the user-visible overall is "stale" (conservative), not
    "fallback".
    """
    if primary_state in {"missing", "stale"}:
        return primary_state
    if any(s in {"missing", "stale"} for s in dependency_states):
        return "stale"
    if any(s == "partial" for s in dependency_states):
        return "partial"
    return primary_state


def format_kst_as_of_label(
    *,
    snapshot_date: dt.date,
    computed_at: dt.datetime | None,
) -> str:
    """Format a Korean 'as-of' label for the data basis.

    With computed_at: "YYYY.MM.DD HH:MM 기준" in KST.
    Without computed_at: "YYYY.MM.DD 장마감 기준" (end-of-day partition).
    """
    if computed_at is None:
        return f"{snapshot_date.strftime('%Y.%m.%d')} 장마감 기준"
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=dt.UTC)
    kst = computed_at.astimezone(ZoneInfo("Asia/Seoul"))
    return kst.strftime("%Y.%m.%d %H:%M 기준")
