"""Assemble a ``daily-plan-bundle/v1`` body from rows already read from the database.

Pure: rows, calendar facts and the cutoff go in, a canonical JSON body comes
out. No database, broker, network or order code, and no clock.

Every input carries its own times and is either present or an explicit
``{"unavailable": <closed reason>}``. An unknown never becomes zero, a neutral
direction or a missing key (design §7.1, §7.2). No source stores a news or
disclosure direction and ``app/**`` runs no model, so items leave ``direction``
out and the comparison reports ``direction_unclassified``.
"""

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.symbol import to_db_symbol
from app.services.daily_plan.allocation import CURRENCY, build_comparison
from app.services.daily_plan.bundle import (
    SCHEMA_VERSION,
    canonical_bundle,
    canonical_json,
    decimal_string,
)
from app.services.daily_plan.vocabulary import (
    RANKING_A_INPUTS,
    RANKING_B_ADDED_INPUTS,
)
from app.services.halt_detection import classify_ohlcv_frame
from app.services.mechanical_loss_cut.rules import is_parking_instrument
from app.services.technical_math import (
    calculate_bollinger,
    calculate_fibonacci,
    calculate_rsi,
    calculate_volume_profile,
    cluster_price_levels,
    collect_support_resistance_price_levels,
    split_support_resistance_levels,
)

ASSEMBLER_VERSION = "daily-plan-assembler/v1"
_KST = ZoneInfo("Asia/Seoul")

# Same windows as the quick analysis projection (analysis_quick), so the bundle
# and the session read the same RSI and support levels.
RSI_BAR_COUNT = 250
SUPPORT_BAR_COUNT = 60
FLOW_SESSION_COUNT = 5
_SUPPORT_TOLERANCE = 0.02
_MARKET_TYPE = "equity_kr"
_DISTANCE_STEP = Decimal("0.0001")

CANDLE_SOURCE = "public.kr_candles_1d"
SCREENER_SOURCE = "public.invest_screener_snapshots"
VALUATION_SOURCE = "public.market_valuation_snapshots:naver_finance"
FLOW_SOURCE = "public.investor_flow_snapshots:naver_finance"
NEWS_SOURCE = "public.news_articles+symbol_news_relevance"
DISCLOSURE_SOURCE = "public.market_events:dart,naver_disclosure"
ACCOUNT_SOURCE = "review.account_value_snapshots"

# The closed candidate rule (design Q10). It is hashed into every bundle, so a
# changed cap or order is visible as a different rule.
CANDIDATE_RULE: dict[str, Any] = {
    "version": "daily-plan-candidates/v1",
    "screener": {
        "table": SCREENER_SOURCE,
        "partition": "previous_session",
        "ranks": [
            {
                "name": "turnover_leaders",
                "key": "daily_turnover",
                "order": "desc",
                "minimum": None,
                "cap": 40,
            },
            {
                "name": "gainers",
                "key": "change_rate",
                "order": "desc",
                "minimum": None,
                "cap": 20,
            },
            {
                "name": "consecutive_up",
                "key": "consecutive_up_days",
                "order": "desc",
                "minimum": 3,
                "cap": 20,
            },
            {
                "name": "support_proximity",
                "key": "dist_to_support_pct",
                "order": "asc",
                "minimum": None,
                "cap": 20,
            },
        ],
        "tie_break": ["daily_turnover desc", "symbol asc"],
    },
    "pick_log": {
        "table": "review.screener_pick_log",
        "admission": "admitted",
        "window": "previous_session_date_00:00_kst..bundle_closed_at",
        "per_source_cap": 20,
        "total_cap": 60,
        "order": ["recorded_at desc", "selection_seq asc", "symbol asc"],
    },
    "holdings": {
        "table": ACCOUNT_SOURCE,
        "brokers": ["kis", "toss"],
        "cap": None,
    },
    "exclude": ["non_six_digit_symbol", "parking_instrument"],
}

_SYMBOL = re.compile(r"[0-9]{6}")
_TIMED_PRECISION = frozenset({"second", "minute", "datetime"})
# Writer reasons that make an account row's cash untrustworthy.
_BAD_CASH_REASONS = frozenset(
    {"invalid_broker_balance", "duplicate_cash", "cash_duplicate_currency"}
)


@dataclass(frozen=True, slots=True)
class SessionFacts:
    """Calendar facts for one plan day, supplied by the reader."""

    session_date: date
    previous_session: date
    # The last FLOW_SESSION_COUNT sessions ending at previous_session, ascending.
    flow_sessions: tuple[date, ...]
    # Regular close of every session date the rows below refer to. A date that is
    # absent is not a confirmed session.
    closes: Mapping[date, datetime]

    @property
    def stale_before(self) -> datetime:
        return self.closes[self.previous_session]


@dataclass(frozen=True, slots=True)
class CandleBar:
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: str
    ingested_at: datetime


@dataclass(frozen=True, slots=True)
class ScreenerRow:
    row_id: int
    symbol: str
    snapshot_date: date
    daily_turnover: Decimal | None
    change_rate: Decimal | None
    consecutive_up_days: int | None
    dist_to_support_pct: Decimal | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ValuationRow:
    row_id: int
    snapshot_date: date
    pbr: Decimal | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class FlowRow:
    row_id: int
    snapshot_date: date
    foreign_net: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NewsRow:
    article_id: int
    link_first_seen_at: datetime
    status: str
    judged_at: datetime | None
    published_at: datetime | None
    published_precision: str | None
    first_received_at: datetime | None
    revised_at: datetime | None


@dataclass(frozen=True, slots=True)
class DisclosureRow:
    event_id: int
    source: str
    event_date: date
    published_at: datetime | None
    published_precision: str | None
    provider_created_at: datetime | None
    # DART keeps the receipt clock only in the raw payload (KST, zone-less).
    receipt_text: str | None
    first_received_at: datetime | None
    revised_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PickRow:
    row_id: int
    call_id: str
    source: str
    symbol: str
    selection_seq: int | None
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class AccountRow:
    row_id: int
    account: str
    day: date
    observed_at: datetime
    created_at: datetime
    holdings: Sequence[Any]
    cash: Mapping[str, Any]
    reasons: Sequence[str]


@dataclass(frozen=True, slots=True)
class AccountBinding:
    """The opaque plan reference of one stored account series."""

    account_ref: str
    account_mode: str


@dataclass(frozen=True, slots=True)
class CandidateSources:
    """Rows the candidate rule reads."""

    screener_partition: Sequence[ScreenerRow]
    screener_latest_date: date | None
    picks: Sequence[PickRow]
    accounts: Sequence[AccountRow]


@dataclass(frozen=True, slots=True)
class Selection:
    symbols: Mapping[str, tuple[str, ...]]
    call_ids: Mapping[str, tuple[str, ...]]
    holders: Mapping[str, tuple[str, ...]]
    sources: Mapping[str, Mapping[str, Any]]
    excluded: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class SymbolInputs:
    security_type: str | None = None
    bars: Sequence[CandleBar] = field(default_factory=tuple)
    screener: ScreenerRow | None = None
    valuation: ValuationRow | None = None
    flows: Sequence[FlowRow] = field(default_factory=tuple)
    news: Sequence[NewsRow] = field(default_factory=tuple)
    disclosures: Sequence[DisclosureRow] = field(default_factory=tuple)


def plan_symbol(value: Any) -> str | None:
    if type(value) is not str:
        return None
    return to_db_symbol(value.strip().upper())


def _rankable(value: Decimal | int | None) -> bool:
    return value is not None and (type(value) is int or value.is_finite())


def _broker(account: str) -> str:
    return account.split(":", 1)[0]


def _number(value: Any) -> Decimal | None:
    if type(value) not in (str, int, Decimal):
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _revision(row_id: int | str, *values: Any) -> str:
    return f"{row_id}:{hashlib.sha256(canonical_json(list(values))).hexdigest()}"


def _unavailable(reason: str, source: str) -> dict[str, Any]:
    return {"unavailable": reason, "source_name": source}


def _present(
    value: Decimal,
    *,
    event_at: datetime,
    observed_at: datetime,
    source: str,
    revision_id: str,
) -> dict[str, Any]:
    return {
        "value": decimal_string(value),
        "event_at": event_at,
        "first_observed_at": observed_at,
        "source_name": source,
        "revision_id": revision_id,
    }


def _time_reason(
    event_at: datetime | None,
    observed_at: datetime,
    *,
    updated: bool,
    closed_at: datetime,
    stale_before: datetime,
) -> str | None:
    """Why a stored value may not be used at the cutoff (design §7.1).

    ``observed_at`` is when the row appeared. A row updated afterwards has no
    proven first observation of its current value.
    """
    if observed_at > closed_at:
        return "observed_after_cutoff"
    if updated or event_at is None:
        return "observed_time_unproven"
    if event_at > closed_at:
        return "observed_after_cutoff"
    if observed_at < event_at:
        # Seen before the session it describes had ended: not that value.
        return "input_invalid"
    if event_at < stale_before:
        return "input_stale"
    return None


def _account_rows(rows: Sequence[AccountRow], day: date) -> dict[str, list[AccountRow]]:
    """KIS and Toss series, newest day first."""
    series: dict[str, list[AccountRow]] = {}
    for row in sorted(rows, key=lambda item: (item.account, item.day, item.row_id)):
        if _broker(row.account) in ("kis", "toss") and row.day <= day:
            series.setdefault(row.account, []).insert(0, row)
    return series


def _holding_symbols(row: AccountRow) -> list[str]:
    symbols = []
    for holding in row.holdings:
        if not isinstance(holding, Mapping) or holding.get("currency") != CURRENCY:
            continue
        quantity = _number(holding.get("quantity"))
        symbol = plan_symbol(holding.get("symbol"))
        if symbol and quantity is not None and quantity > 0:
            symbols.append(symbol)
    return symbols


def select_candidates(
    rows: CandidateSources,
    *,
    facts: SessionFacts,
    closed_at: datetime,
    bindings: Mapping[str, AccountBinding],
) -> Selection:
    """Apply the closed candidate rule to what was written by the cutoff."""
    members: dict[str, set[str]] = {}
    excluded: dict[str, set[str]] = {name: set() for name in CANDIDATE_RULE["exclude"]}
    status: dict[str, dict[str, Any]] = {}

    def eligible(symbol: str) -> bool:
        if not _SYMBOL.fullmatch(symbol):
            excluded["non_six_digit_symbol"].add(symbol)
        elif is_parking_instrument(symbol, _MARKET_TYPE):
            excluded["parking_instrument"].add(symbol)
        else:
            return True
        return False

    def source_state(name: str, reason: str | None, count: int, **extra: Any) -> None:
        state = "ok" if reason is None else "partial" if count else "unavailable"
        status[name] = {"status": state, "reason": reason, "symbols": count, **extra}

    # Screener: only the previous session's partition, and only rows whose
    # current values were all written by the cutoff.
    partition = [
        row
        for row in rows.screener_partition
        if row.snapshot_date == facts.previous_session
    ]
    usable = [
        row
        for row in partition
        if row.created_at <= closed_at and row.updated_at <= closed_at
    ]
    ranked_symbols: set[str] = set()
    for rank in CANDIDATE_RULE["screener"]["ranks"]:
        key, sign = rank["key"], -1 if rank["order"] == "desc" else 1
        ordered = sorted(
            (
                row
                for row in usable
                if _rankable(getattr(row, key))
                and (rank["minimum"] is None or getattr(row, key) >= rank["minimum"])
            ),
            key=lambda row: (
                sign * getattr(row, key),
                -(row.daily_turnover if _rankable(row.daily_turnover) else 0),
                row.symbol,
                row.row_id,
            ),
        )
        taken = 0
        for row in ordered:
            symbol = plan_symbol(row.symbol)
            if taken == rank["cap"]:
                break
            if symbol and eligible(symbol):
                members.setdefault(symbol, set()).add(f"screener:{rank['name']}")
                ranked_symbols.add(symbol)
                taken += 1
    if usable:
        reason = None
    elif partition:
        reason = "observed_after_cutoff"
    elif rows.screener_latest_date is not None:
        reason = "input_stale"
    else:
        reason = "input_missing"
    source_state(
        "screener",
        reason,
        len(ranked_symbols),
        partition_date=facts.previous_session.isoformat(),
    )

    # Pick log: admitted rows since the previous session began.
    window_start = datetime.combine(facts.previous_session, time(), _KST)
    in_window = [row for row in rows.picks if row.recorded_at >= window_start]
    observed = sorted(
        (row for row in in_window if row.recorded_at <= closed_at),
        key=lambda row: (
            -row.recorded_at.timestamp(),
            row.selection_seq is None,
            row.selection_seq or 0,
            row.symbol,
            row.source,
            row.row_id,
        ),
    )
    caps = CANDIDATE_RULE["pick_log"]
    per_source: Counter[str] = Counter()
    taken_symbols: set[str] = set()
    calls: dict[str, set[str]] = {}
    for row in observed:
        symbol = plan_symbol(row.symbol)
        if not symbol or not eligible(symbol):
            continue
        name = f"pick_log:{row.source}"
        if name not in members.get(symbol, set()):
            if per_source[row.source] >= caps["per_source_cap"]:
                continue
            if symbol not in taken_symbols and len(taken_symbols) >= caps["total_cap"]:
                continue
            per_source[row.source] += 1
            taken_symbols.add(symbol)
            members.setdefault(symbol, set()).add(name)
        calls.setdefault(symbol, set()).add(row.call_id)
    if observed:
        reason = None
    else:
        reason = "observed_after_cutoff" if in_window else "input_missing"
    source_state("pick_log", reason, len(taken_symbols))

    # Holdings: each account's newest snapshot taken by the cutoff. Never capped.
    series = _account_rows(rows.accounts, facts.session_date)
    holders: dict[str, set[str]] = {}
    held: set[str] = set()
    reasons: set[str] = set()
    for account, history in series.items():
        latest = next(
            (
                row
                for row in history
                if row.observed_at <= closed_at and row.created_at <= closed_at
            ),
            None,
        )
        if latest is None:
            reasons.add("observed_after_cutoff")
            continue
        if latest.observed_at < facts.stale_before:
            reasons.add("input_stale")
        if "holdings_completeness_unknown" in latest.reasons:
            reasons.add("input_invalid")
        binding = bindings.get(account)
        for symbol in _holding_symbols(latest):
            if not eligible(symbol):
                continue
            members.setdefault(symbol, set()).add("holdings")
            held.add(symbol)
            if binding:
                holders.setdefault(symbol, set()).add(binding.account_ref)
    if not series:
        reasons.add("input_missing")
    source_state(
        "holdings",
        next(
            (
                code
                for code in (
                    "input_missing",
                    "observed_after_cutoff",
                    "input_invalid",
                    "input_stale",
                )
                if code in reasons
            ),
            None,
        ),
        len(held),
    )
    return Selection(
        symbols={key: tuple(sorted(value)) for key, value in sorted(members.items())},
        call_ids={key: tuple(sorted(value)) for key, value in calls.items()},
        holders={key: tuple(sorted(value)) for key, value in holders.items()},
        sources=status,
        excluded={name: len(symbols) for name, symbols in excluded.items()},
    )


def _candle_inputs(
    bars: Sequence[CandleBar], facts: SessionFacts, closed_at: datetime
) -> dict[str, Any]:
    """Last close, RSI-14 and support distance from completed daily bars."""
    window = sorted(
        (bar for bar in bars if bar.session <= facts.previous_session),
        key=lambda bar: (bar.session, bar.ingested_at, bar.source),
    )[-RSI_BAR_COUNT:]

    def result(
        price_reason: str | None,
        price: dict[str, Any],
        rsi: dict[str, Any] | None = None,
        distance: dict[str, Any] | None = None,
        *,
        halted: bool = False,
    ) -> dict[str, Any]:
        return {
            "halted_suspect": halted,
            "last_close": (
                decimal_string(window[-1].close)
                if price_reason is None
                else {"unavailable": price_reason}
            ),
            "price": price,
            "rsi_14": rsi or _unavailable(price_reason, CANDLE_SOURCE),
            "dist_to_support_pct": distance
            or _unavailable(price_reason, CANDLE_SOURCE),
        }

    if not window:
        return result("input_missing", _unavailable("input_missing", CANDLE_SOURCE))
    last = window[-1]
    event_at = facts.closes.get(last.session)
    # The bars have no created_at: ``ingested_at`` is the last write of the
    # current value, so it can only be later than the first observation.
    price_reason = _time_reason(
        event_at,
        last.ingested_at,
        updated=False,
        closed_at=closed_at,
        stale_before=facts.stale_before,
    )
    valid = len({bar.session for bar in window}) == len(window) and all(
        all(
            number.is_finite() and number > 0
            for number in (bar.open, bar.high, bar.low, bar.close)
        )
        and bar.volume.is_finite()
        and bar.volume >= 0
        for bar in window
    )
    if price_reason in (None, "input_stale") and not valid:
        price_reason = "input_invalid"
    frame = None
    halted = False
    if valid:
        frame = pd.DataFrame(
            [
                {
                    "date": bar.session,
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": float(bar.volume),
                }
                for bar in window
            ]
        )
        halted = classify_ohlcv_frame(frame).suspected
        if halted and price_reason is None:
            price_reason = "input_invalid"
    times = (
        {"event_at": event_at, "first_observed_at": last.ingested_at}
        if event_at is not None and max(event_at, last.ingested_at) <= closed_at
        else {}
    )
    if price_reason:
        # The publish validator names a stale price ``price_stale``.
        code = "price_stale" if price_reason == "input_stale" else price_reason
        price = {**_unavailable(code, CANDLE_SOURCE), **times}
        return result(price_reason, price, halted=halted)

    revision = _revision(
        f"{CANDLE_SOURCE}:{last.session.isoformat()}",
        [
            [
                bar.session.isoformat(),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.source,
            ]
            for bar in window
        ],
    )
    price = {
        **times,
        "source_name": CANDLE_SOURCE,
        "venue": "KRX",
        "candle_sources": sorted({bar.source for bar in window}),
        "revision_id": revision,
    }

    def derived(used: Sequence[CandleBar], value: Decimal | None) -> dict[str, Any]:
        observed_at = max(bar.ingested_at for bar in used)
        if observed_at > closed_at:
            return _unavailable("observed_after_cutoff", CANDLE_SOURCE)
        if value is None:
            return _unavailable("input_missing", CANDLE_SOURCE)
        return _present(
            value,
            event_at=event_at,
            observed_at=observed_at,
            source=CANDLE_SOURCE,
            revision_id=revision,
        )

    rsi = calculate_rsi(frame["close"])["14"]
    return result(
        None,
        price,
        derived(window, None if rsi is None else Decimal(str(rsi))),
        derived(
            window[-SUPPORT_BAR_COUNT:],
            _support_distance(frame.tail(SUPPORT_BAR_COUNT), last.close),
        ),
    )


def _support_distance(frame: pd.DataFrame, close: Decimal) -> Decimal | None:
    """Percent from the close down to the nearest support, as quick analysis does."""
    current = float(close)
    try:
        profile = calculate_volume_profile(frame, bins=20)
    except (ValueError, OverflowError):
        profile = None
    levels = collect_support_resistance_price_levels(
        calculate_fibonacci(frame, current, _MARKET_TYPE),
        profile,
        {"bollinger": calculate_bollinger(frame["close"])},
    )
    supports, _ = split_support_resistance_levels(
        cluster_price_levels(
            levels, tolerance_pct=_SUPPORT_TOLERANCE, market_type=_MARKET_TYPE
        ),
        current,
    )
    if not supports:
        return None
    support = Decimal(str(supports[0]["price"]))
    return ((close - support) / close * 100).quantize(_DISTANCE_STEP)


def _snapshot_input(
    row: ScreenerRow | ValuationRow | None,
    name: str,
    source: str,
    facts: SessionFacts,
    closed_at: datetime,
    *,
    minimum_exclusive: bool,
) -> dict[str, Any]:
    if row is None:
        return _unavailable("input_missing", source)
    event_at = facts.closes.get(row.snapshot_date)
    reason = _time_reason(
        event_at,
        row.created_at,
        updated=row.updated_at > row.created_at,
        closed_at=closed_at,
        stale_before=facts.stale_before,
    )
    value = getattr(row, name)
    if reason is None:
        if value is None:
            reason = "input_missing"
        elif not value.is_finite() or value < 0 or (minimum_exclusive and not value):
            reason = "input_invalid"
    if reason:
        return _unavailable(reason, source)
    return _present(
        value,
        event_at=event_at,
        observed_at=row.created_at,
        source=source,
        revision_id=_revision(row.row_id, row.snapshot_date.isoformat(), name, value),
    )


def _flow_input(
    rows: Sequence[FlowRow], facts: SessionFacts, closed_at: datetime
) -> dict[str, Any]:
    """Foreign net share quantity summed over the last confirmed sessions."""
    by_date = {row.snapshot_date: row for row in rows}
    if not by_date:
        return _unavailable("input_missing", FLOW_SOURCE)
    if facts.previous_session not in by_date:
        return _unavailable("input_stale", FLOW_SOURCE)
    used = [by_date.get(day) for day in facts.flow_sessions]
    if len(used) != FLOW_SESSION_COUNT or any(row is None for row in used):
        return _unavailable("input_missing", FLOW_SOURCE)
    reasons = {
        _time_reason(
            facts.closes.get(row.snapshot_date),
            row.created_at,
            updated=row.updated_at > row.created_at,
            closed_at=closed_at,
            # Older sessions of the sum are expected to be older.
            stale_before=facts.closes[facts.flow_sessions[0]],
        )
        for row in used
    }
    for reason in ("observed_after_cutoff", "observed_time_unproven", "input_invalid"):
        if reason in reasons:
            return _unavailable(reason, FLOW_SOURCE)
    if any(row.foreign_net is None for row in used):
        return _unavailable("input_missing", FLOW_SOURCE)
    total = Decimal(sum(row.foreign_net for row in used))
    return {
        **_present(
            total,
            event_at=facts.stale_before,
            observed_at=max(row.created_at for row in used),
            source=FLOW_SOURCE,
            revision_id=_revision(
                ",".join(str(row.row_id) for row in used),
                [[row.snapshot_date.isoformat(), row.foreign_net] for row in used],
            ),
        ),
        "unit": "shares",
    }


@dataclass(frozen=True, slots=True)
class _Item:
    key: int
    kind: str
    event_at: datetime | None
    # When the item existed in the database, and its proven first observation.
    appeared_at: datetime
    observed_at: datetime | None
    in_window: bool


def _items_input(
    items: Sequence[_Item], source: str, closed_at: datetime
) -> dict[str, Any]:
    """Items known by the cutoff. No source records a direction."""
    included = []
    late = stale = False
    for item in sorted(items, key=lambda entry: (entry.kind, entry.key)):
        if item.appeared_at > closed_at or (
            item.event_at is not None and item.event_at > closed_at
        ):
            late = True
            continue
        if not item.in_window:
            stale = True
            continue
        unproven = {"unavailable": "observed_time_unproven"}
        observed_at = item.observed_at
        if observed_at is not None and observed_at > closed_at:
            observed_at = None
        included.append(
            {
                "event_at": item.event_at or unproven,
                "first_observed_at": observed_at or unproven,
                "source_name": item.kind,
                "revision_id": _revision(
                    item.key, item.kind, item.event_at, observed_at
                ),
            }
        )
    if included:
        return {"items": included, "source_name": source}
    # No row is not proof that nothing happened.
    reason = (
        "observed_after_cutoff" if late else "input_stale" if stale else "input_missing"
    )
    return _unavailable(reason, source)


def _news_items(
    rows: Sequence[NewsRow], facts: SessionFacts, closed_at: datetime
) -> list[_Item]:
    items = []
    for row in rows:
        # Only a relevance judgment that existed at the cutoff removes an item.
        if (
            row.status == "excluded"
            and row.judged_at is not None
            and row.judged_at <= closed_at
        ):
            continue
        event_at = (
            row.published_at
            if row.published_precision in ("second", "minute")
            else None
        )
        appeared_at = max(
            row.link_first_seen_at, row.first_received_at or row.link_first_seen_at
        )
        proven = row.first_received_at is not None and row.revised_at is None
        items.append(
            _Item(
                key=row.article_id,
                kind="news_articles",
                event_at=event_at,
                appeared_at=appeared_at,
                observed_at=appeared_at if proven else None,
                in_window=(event_at or appeared_at) >= facts.stale_before,
            )
        )
    return items


def _disclosure_time(row: DisclosureRow) -> datetime | None:
    if row.published_at is not None and row.published_precision in _TIMED_PRECISION:
        return row.published_at
    if row.provider_created_at is not None:
        return row.provider_created_at
    if row.receipt_text and len(row.receipt_text) >= 19:
        try:
            return datetime.fromisoformat(row.receipt_text).replace(tzinfo=_KST)
        except ValueError:
            return None
    return None


def _disclosure_items(
    rows: Sequence[DisclosureRow], facts: SessionFacts
) -> list[_Item]:
    items = []
    for row in rows:
        event_at = _disclosure_time(row)
        updated = row.updated_at > row.created_at or row.revised_at is not None
        items.append(
            _Item(
                key=row.event_id,
                kind=f"market_events:{row.source}",
                event_at=event_at,
                appeared_at=row.created_at,
                observed_at=(
                    None if updated else row.first_received_at or row.created_at
                ),
                in_window=(
                    event_at >= facts.stale_before
                    if event_at is not None
                    else row.event_date >= facts.previous_session
                ),
            )
        )
    return items


def _cash(
    history: Sequence[AccountRow], facts: SessionFacts, closed_at: datetime
) -> dict[str, Any]:
    if not history:
        return _unavailable("input_missing", ACCOUNT_SOURCE)
    row = history[0]
    observed_at = max(row.observed_at, row.created_at)
    if observed_at > closed_at:
        return _unavailable("observed_after_cutoff", ACCOUNT_SOURCE)
    if _BAD_CASH_REASONS.intersection(row.reasons):
        return _unavailable("input_invalid", ACCOUNT_SOURCE)
    raw = row.cash.get(CURRENCY)
    if raw is None:
        return _unavailable("input_missing", ACCOUNT_SOURCE)
    value = _number(raw)
    if value is None or value < 0:
        return _unavailable("input_invalid", ACCOUNT_SOURCE)
    if row.observed_at < facts.stale_before:
        return _unavailable("input_stale", ACCOUNT_SOURCE)
    return _present(
        value,
        event_at=row.observed_at,
        observed_at=observed_at,
        source=ACCOUNT_SOURCE,
        revision_id=_revision(row.row_id, row.day.isoformat(), CURRENCY, value),
    )


def _parking(
    history: Sequence[AccountRow], facts: SessionFacts, closed_at: datetime
) -> list[dict[str, Any]] | dict[str, Any]:
    """Parking holdings. No source records a sell cost, so none is usable yet."""
    if not history:
        return _unavailable("input_missing", ACCOUNT_SOURCE)
    row = history[0]
    observed_at = max(row.observed_at, row.created_at)
    if observed_at > closed_at:
        return _unavailable("observed_after_cutoff", ACCOUNT_SOURCE)
    holdings = []
    for holding in row.holdings:
        if not isinstance(holding, Mapping) or holding.get("currency") != CURRENCY:
            continue
        symbol = plan_symbol(holding.get("symbol"))
        if not symbol or not is_parking_instrument(symbol, _MARKET_TYPE):
            continue
        quantity = _number(holding.get("quantity"))
        price = _number(holding.get("mark"))
        mark_close = None
        if type(holding.get("mark_asof")) is str:
            try:
                marked = datetime.fromisoformat(holding["mark_asof"])
            except ValueError:
                marked = None
            if marked is not None and marked.utcoffset() is not None:
                mark_close = facts.closes.get(marked.astimezone(_KST).date())
        # The older of the two facts decides whether the entry is stale.
        event_at = min(row.observed_at, mark_close or row.observed_at)
        if quantity is None or quantity < 0:
            reason = "input_invalid"
        elif event_at < facts.stale_before:
            reason = "input_stale"
        else:
            reason = "input_missing"
        unknown = {"unavailable": "input_missing"}
        holdings.append(
            {
                "symbol": symbol,
                "quantity": (
                    {"unavailable": "input_invalid"}
                    if quantity is None or quantity < 0
                    else decimal_string(quantity)
                ),
                "price": (
                    unknown
                    if price is None or price <= 0 or mark_close is None
                    else decimal_string(price)
                ),
                "sell_cost_bps": unknown,
                "unavailable": reason,
                "event_at": event_at,
                "first_observed_at": observed_at,
                "source_name": ACCOUNT_SOURCE,
                "revision_id": _revision(row.row_id, symbol, quantity, price),
            }
        )
    return sorted(holdings, key=lambda item: (item["symbol"], item["revision_id"]))


def build_bundle(
    *,
    facts: SessionFacts,
    closed_at: datetime,
    cutoff_at: datetime,
    selection: Selection,
    inputs: Mapping[str, SymbolInputs],
    accounts: Sequence[AccountRow],
    bindings: Mapping[str, AccountBinding],
) -> dict[str, Any]:
    """Return the canonical (JSON-native) bundle body for one plan day."""
    if closed_at > cutoff_at:
        raise ValueError("bundle_closed_after_cutoff")
    series = _account_rows(accounts, facts.session_date)
    routable = {
        binding.account_ref: binding.account_mode for binding in bindings.values()
    }
    rows = []
    for symbol, sources in selection.symbols.items():
        data = inputs.get(symbol, SymbolInputs())
        candles = _candle_inputs(data.bars, facts, closed_at)
        rows.append(
            {
                "row_kind": "candidate",
                "market": "kr",
                "symbol": symbol,
                "account_ref": None,
                "security_type": data.security_type,
                "candidate_sources": sources,
                "candidate_call_ids": selection.call_ids.get(symbol, ()),
                "held_account_refs": selection.holders.get(symbol, ()),
                "routable_account_modes": routable,
                "halted_suspect": candles["halted_suspect"],
                "last_close": candles["last_close"],
                "sources": {"price": candles["price"]},
                "ranking_inputs": {
                    "dist_to_support_pct": candles["dist_to_support_pct"],
                    "rsi_14": candles["rsi_14"],
                    "daily_turnover": _snapshot_input(
                        data.screener,
                        "daily_turnover",
                        SCREENER_SOURCE,
                        facts,
                        closed_at,
                        minimum_exclusive=False,
                    ),
                    "pbr": _snapshot_input(
                        data.valuation,
                        "pbr",
                        VALUATION_SOURCE,
                        facts,
                        closed_at,
                        minimum_exclusive=True,
                    ),
                    "foreign_net": _flow_input(data.flows, facts, closed_at),
                    "news": _items_input(
                        _news_items(data.news, facts, closed_at),
                        NEWS_SOURCE,
                        closed_at,
                    ),
                    "disclosures": _items_input(
                        _disclosure_items(data.disclosures, facts),
                        DISCLOSURE_SOURCE,
                        closed_at,
                    ),
                },
            }
        )

    account_facts = []
    for account, binding in sorted(
        bindings.items(), key=lambda item: item[1].account_ref
    ):
        history = series.get(account, [])
        account_facts.append(
            {
                "account_ref": binding.account_ref,
                "account_mode": binding.account_mode,
                "currency": CURRENCY,
                "cash": _cash(history, facts, closed_at),
                "parking": _parking(history, facts, closed_at),
                "snapshot_reasons": (
                    sorted(history[0].reasons)
                    if history and history[0].created_at <= closed_at
                    else []
                ),
            }
        )
    unidentified = [
        {"broker": _broker(account), "reason": "account_unidentified"}
        for account in sorted(series)
        if account not in bindings
    ]
    screener_down = selection.sources["screener"]["status"] == "unavailable"
    body = {
        "schema_version": SCHEMA_VERSION,
        "assembler_version": ASSEMBLER_VERSION,
        "market": "kr",
        "calendar": "XKRX",
        "session_date": facts.session_date.isoformat(),
        "previous_session_date": facts.previous_session.isoformat(),
        "cutoff_at": cutoff_at,
        "bundle_closed_at": closed_at,
        "stale_before": facts.stale_before,
        "candidate_rule": CANDIDATE_RULE,
        "candidate_rule_sha256": hashlib.sha256(
            canonical_json(CANDIDATE_RULE)
        ).hexdigest(),
        "candidate_set_sha256": hashlib.sha256(
            canonical_json(
                [
                    [symbol, sources]
                    for symbol, sources in sorted(selection.symbols.items())
                ]
            )
        ).hexdigest(),
        "candidate_sources": dict(selection.sources),
        "excluded_candidates": dict(selection.excluded),
        "gap_reason": "candidate_source_unavailable" if screener_down else None,
        "accounts": account_facts,
        "accounts_unidentified": unidentified,
        "rows": rows,
    }
    return json.loads(canonical_bundle(body))


def input_tallies(body: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """Present and unknown counts as the stored comparison will read them."""
    cutoff = datetime.fromisoformat(body["bundle_closed_at"])
    view = build_comparison(
        bundle=body,
        bundle_sha256=None,
        candidates={row["symbol"]: {} for row in body["rows"]},
        account_facts=body["accounts"],
        max_adopt=0,
        cutoff_at=cutoff,
        stale_before=datetime.fromisoformat(body["stale_before"]),
        evaluated_at=cutoff,
    )
    names = RANKING_A_INPUTS + RANKING_B_ADDED_INPUTS
    tallies: dict[str, Counter[str]] = {
        name: Counter() for name in (*names, "price", "cash", "parking")
    }
    for candidate in view["candidates"]:
        for name in names:
            record = candidate["inputs"][name]
            tallies[name][record["reason"] or "present"] += 1
        tallies["price"][candidate["entry"]["reference_price_reason"] or "present"] += 1
    for account in view["account_facts"]:
        tallies["cash"][account["cash_reason"] or "present"] += 1
        tallies["parking"].update(["present"] * len(account["parking"]))
        for holding in account["parking_unusable"]:
            tallies["parking"][holding["reason"]] += 1
    return {name: dict(sorted(counts.items())) for name, counts in tallies.items()}
