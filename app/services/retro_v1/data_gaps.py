"""``data_gaps`` — what is missing or stale, from the checks that already exist.

No new probes: each signal below is an existing read-only assessment, reused as
is (or, where the existing helper is unsafe to call inside this snapshot, the same
rule written as one SELECT — see ``_latest_bar_time``). A datum that is fresh
produces no row; a gap row says which dataset, when it last updated and why it
counts as a gap. A signal that itself failed to read is an ``unknowns`` entry,
never "no gap", and does not hide the rows of the other signals.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.crypto_insight_snapshots.repository import (
    get_latest_crypto_insight,
    list_latest_crypto_insights,
)
from app.services.execution_ledger.kis_lots import compute_freshness
from app.services.execution_ledger.repository import ExecutionLedgerRepository
from app.services.krx_after_market_health import load_krx_after_market_health
from app.services.market_events.expected_sources import EXPECTED_SOURCES
from app.services.market_events.freshness_service import MarketEventsFreshnessService
from app.services.market_events.session_calendar import (
    Market,
    regular_session_bounds,
    trading_session_status,
)
from app.services.market_valuation_snapshots.freshness import valuation_state
from app.services.market_valuation_snapshots.repository import (
    MarketValuationSnapshotsRepository,
)
from app.services.research_reports.freshness import compute_research_reports_readiness
from app.services.retro_v1.envelope import (
    DEFAULT_LIMIT,
    KST,
    Row,
    Unknowns,
    build_response,
    check_limit,
    decode_cursor,
    guarded,
    iso,
    latest,
    params_digest,
    read_snapshot,
    safe_code,
)
from app.services.trade_journal.negative_class import load_negative_class_health

NEGATIVE_CLASS_MARKETS = ("kr", "us", "crypto")
_HEALTH_REASON = {
    "missing": "missing_data",
    "stale": "stale",
    "unknown": "incomplete_evidence",
}

#: Copy of ``_crypto_regime._REGIME_STALE_AFTER_SECONDS`` (the MCP regime view).
#: The reader may not import ``app.mcp_server``, so the rule is copied here and
#: ``tests/services/retro_v1/test_data_gaps_sources.py`` pins value and behaviour
#: against the original.
CRYPTO_REGIME_STALE_AFTER_SECONDS: Final = 24 * 3600
#: The regime view reports ``aggregate_oi`` as ``disabled`` (no provider is wired,
#: it emits no rows). Pinned against ``_crypto_regime.py`` by the same test.
CRYPTO_OI_DISABLED: Final = True
CRYPTO_OI_PROVIDER: Final = "coinglass"

#: (dataset, metric, provider) for the single-snapshot crypto indicators. The
#: TVL dataset is read separately: DefiLlama writes one row per protocol.
_CRYPTO_SINGLE: Final = (
    ("crypto_insight.fear_greed", "fear_greed", "alternative_me"),
    ("crypto_insight.stablecoin_supply", "stablecoin_supply", "defillama"),
    ("crypto_insight.breadth", "tv_crypto_breadth", "tradingview"),
)
CRYPTO_TVL_DATASET: Final = "crypto_insight.defi_tvl"
CRYPTO_TVL_METRIC: Final = "tvl"
CRYPTO_TVL_PROVIDER: Final = "defillama"

#: Latest bar time per table. One ``MAX`` over the whole table, not per symbol:
#: the question is whether the market has bars for its last session at all.
_LATEST_BAR_SQL: Final = {
    "candles_1m.kr": "SELECT MAX(time) AS latest FROM public.kr_candles_1m",
    "candles_1m.us": "SELECT MAX(time) AS latest FROM public.us_candles_1m",
    "candles_1d.kr": "SELECT MAX(time) AS latest FROM public.kr_candles_1d",
    "candles_1d.us": "SELECT MAX(time) AS latest FROM public.us_candles_1d",
    "candles_1d.crypto": (
        "SELECT MAX(time) AS latest FROM public.crypto_candles_1d "
        "WHERE is_closed = TRUE"
    ),
}
_CANDLE_MARKETS_1M: Final[tuple[Market, ...]] = ("kr", "us")
_CANDLE_MARKETS_1D: Final[tuple[Market | Literal["crypto"], ...]] = (
    "kr",
    "us",
    "crypto",
)
_MAX_SESSION_LOOKBACK_DAYS: Final = 10
MARKET_EVENTS_WINDOW_DAYS: Final = 7
RESEARCH_REPORTS_DATASET: Final = "research_reports"
VALUATION_MARKETS: Final[tuple[Market, ...]] = ("kr", "us")


def _parse(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None
    return None


def gap_row(
    *,
    dataset: str,
    symbol: str | None,
    last_updated_at: datetime | None,
    source: str | None,
    venue: str | None,
    missing_reason: str,
    basis: str = "inference",
) -> Row:
    # ``adjusted`` has no meaning for these datasets: null, not "false".
    hints = {"adjusted": "not_applicable"}
    if symbol is None:
        hints["symbol"] = "not_applicable"
    if venue is None:
        hints["venue"] = "not_applicable"
    if source is None:
        hints["source"] = "not_applicable"
    row = {
        "dataset": dataset,
        "symbol": symbol,
        "last_updated_at": iso(last_updated_at),
        "source": source,
        "venue": venue,
        "adjusted": None,
        "missing_reason": missing_reason,
        "basis": basis,
    }
    return row, hints


def krx_after_market_gap(health: dict[str, Any]) -> tuple[Row | None, datetime | None]:
    """From ``assess_krx_after_market_health``: only a non-fresh list is a gap."""
    newest = _parse(health.get("newest_asof"))
    status = str(health.get("status"))
    if status == "fresh":
        return None, newest
    return (
        gap_row(
            dataset="krx_after_market_eligibility",
            symbol=None,
            last_updated_at=newest,
            source="operator_import",
            venue="KRX",
            missing_reason=_HEALTH_REASON.get(status, "incomplete_evidence"),
        ),
        newest,
    )


def reconcile_gaps(
    latest_runs: dict[str, Any], now: datetime
) -> tuple[list[Row], datetime | None]:
    """KIS reconcile freshness (the 90-minute rule of ``kis_lots``).

    Only KIS has a freshness rule in code; another broker's run is neither
    judged stale nor reported, because no existing check defines "stale" for it.
    """
    run = latest_runs.get("kis")
    freshness = compute_freshness(run.finished_at if run else None, now)
    if freshness.state == "fresh":
        return [], freshness.last_reconcile_finished_at
    reason = "missing_data" if freshness.state == "missing" else "stale"
    return (
        [
            gap_row(
                dataset="execution_ledger_reconcile",
                symbol=None,
                last_updated_at=freshness.last_reconcile_finished_at,
                source="kis",
                venue=None,
                missing_reason=reason,
            )
        ],
        freshness.last_reconcile_finished_at,
    )


def negative_class_gap(health: dict[str, Any]) -> tuple[Row | None, datetime | None]:
    status = str(health.get("status"))
    last = _parse(health.get("last_recorded_at"))
    if status == "ok":
        return None, last
    reason = "missing_data" if status == "never_recorded" else "stale"
    return (
        gap_row(
            dataset=f"negative_class_recording.{health.get('market')}",
            symbol=None,
            last_updated_at=last,
            source=str(health.get("last_source"))
            if health.get("last_source")
            else None,
            venue=None,
            missing_reason=reason,
        ),
        last,
    )


# --------------------------------------------------------------------------
# crypto market-regime indicators
# --------------------------------------------------------------------------
def crypto_insight_state(
    snapshot_at: datetime, freshness_seconds: int | None, now: datetime
) -> Literal["fresh", "stale"]:
    """The regime view's rule (``_crypto_regime._state_for``), copied verbatim."""
    age = (now - snapshot_at).total_seconds()
    threshold = freshness_seconds or CRYPTO_REGIME_STALE_AFTER_SECONDS
    return "fresh" if age <= threshold else "stale"


def _snapshot_gap(
    *, dataset: str, provider: str, snapshot: Any, now: datetime
) -> tuple[Row | None, datetime | None]:
    if snapshot is None:
        return (
            gap_row(
                dataset=dataset,
                symbol=None,
                last_updated_at=None,
                source=provider,
                venue=None,
                missing_reason="missing_data",
            ),
            None,
        )
    if dataset == "crypto_insight.breadth" and snapshot.value is None:
        return (
            gap_row(
                dataset=dataset,
                symbol=None,
                last_updated_at=None,
                source=provider,
                venue=None,
                missing_reason="unsupported",
            ),
            snapshot.snapshot_at,
        )
    if crypto_insight_state(snapshot.snapshot_at, snapshot.freshness_seconds, now) == (
        "fresh"
    ):
        return None, snapshot.snapshot_at
    return (
        gap_row(
            dataset=dataset,
            symbol=None,
            last_updated_at=snapshot.snapshot_at,
            source=provider,
            venue=None,
            missing_reason="stale",
        ),
        snapshot.snapshot_at,
    )


def _tvl_gap(rows: list[Any], now: datetime) -> tuple[Row | None, datetime | None]:
    # Same as the regime view's ``_tvl_block``: judge the freshest protocol row.
    freshest = max(rows, key=lambda r: r.snapshot_at) if rows else None
    return _snapshot_gap(
        dataset=CRYPTO_TVL_DATASET,
        provider=CRYPTO_TVL_PROVIDER,
        snapshot=freshest,
        now=now,
    )


def crypto_oi_gap() -> Row:
    """``aggregate_oi`` is disabled in the regime view: a fixed unsupported row."""
    return gap_row(
        dataset="crypto_insight.aggregate_oi",
        symbol=None,
        last_updated_at=None,
        source=CRYPTO_OI_PROVIDER,
        venue=None,
        missing_reason="unsupported",
    )


# --------------------------------------------------------------------------
# candles and valuation: trading-day rule
# --------------------------------------------------------------------------
_EXCHANGE_ZONE: Final[dict[Market, tzinfo]] = {
    "kr": KST,
    "us": ZoneInfo("America/New_York"),
}


def expected_bar_day(
    market: Market | Literal["crypto"], as_of: datetime, *, completed: bool
) -> date:
    """The last session whose bars should already exist at ``as_of``.

    Exchange markets use the exchange's own local date, not KST: a Tuesday
    morning in Seoul is still Monday in New York. ``completed=True`` (daily bars,
    valuation snapshots) requires the session to have closed; ``completed=False``
    (minute bars) requires only that it has opened. A calendar that cannot
    classify a day, or cannot give its bounds, raises so the caller reports
    unknown. Crypto trades every day; its last closed daily bar is the UTC day
    before ``as_of``.
    """
    if market == "crypto":
        return as_of.astimezone(UTC).date() - timedelta(days=1)
    local_today = as_of.astimezone(_EXCHANGE_ZONE[market]).date()
    for back in range(_MAX_SESSION_LOOKBACK_DAYS):
        day = local_today - timedelta(days=back)
        status = trading_session_status(market, day)
        if status == "unknown":
            raise LookupError("exchange calendar cannot classify the day")
        if status != "open":
            continue
        bounds = regular_session_bounds(market, day)
        if bounds is None:
            raise LookupError("exchange calendar gave no session bounds")
        open_utc, close_utc = bounds
        if (close_utc if completed else open_utc) <= as_of:
            return day
    raise LookupError("no confirmed session in the lookback window")


def _bar_gap(
    *,
    dataset: str,
    latest_time: datetime | None,
    trading_day: date,
    day_zone: tzinfo,
) -> tuple[Row | None, datetime | None]:
    if latest_time is None:
        return (
            gap_row(
                dataset=dataset,
                symbol=None,
                last_updated_at=None,
                source=None,
                venue=None,
                missing_reason="missing_data",
            ),
            None,
        )
    if latest_time.tzinfo is None:
        raise ValueError("bar time without a timezone is not an instant")
    if latest_time.astimezone(day_zone).date() >= trading_day:
        return None, latest_time
    return (
        gap_row(
            dataset=dataset,
            symbol=None,
            last_updated_at=latest_time,
            source=None,
            venue=None,
            missing_reason="stale",
        ),
        latest_time,
    )


async def _latest_bar_time(session: AsyncSession, key: str) -> datetime | None:
    # Plain SELECT, not ``kr_candles_freshness``: that helper swallows a failed
    # query and calls ``rollback()``, which would end this snapshot transaction,
    # and it reports a failure as "missing" instead of unknown.
    result = await session.execute(text(_LATEST_BAR_SQL[key]))
    row = result.first()
    return getattr(row, "latest", None) if row is not None else None


def valuation_gap(
    *, market: str, counts: Any, trading_day: date
) -> tuple[Row | None, datetime | None]:
    state = valuation_state(counts.latest_date, trading_day)
    if state == "fresh":
        return None, counts.latest_at
    reason = "missing_data" if state == "missing" else "stale"
    return (
        gap_row(
            dataset=f"valuation_snapshots.{market}",
            symbol=None,
            last_updated_at=counts.latest_at,
            source=None,
            venue=None,
            missing_reason=reason,
        ),
        counts.latest_at,
    )


# --------------------------------------------------------------------------
# market events, research reports
# --------------------------------------------------------------------------
def market_event_gap(row: Any) -> tuple[Row | None, datetime | None]:
    """One freshness-service row. Its ``latest_failed_error`` text is never read."""
    last = row.latest_succeeded_finished_at
    if row.latest_succeeded_partition_date is None:
        reason = "missing_data"
    elif row.stale:
        reason = "stale"
    elif row.partition_count_failed > 0:
        reason = "incomplete_evidence"
    else:
        return None, last
    dataset = ".".join(
        [
            "market_events",
            safe_code(row.source, fallback="unclassified") or "unclassified",
            safe_code(row.category, fallback="unclassified") or "unclassified",
            safe_code(row.market, fallback="unclassified") or "unclassified",
        ]
    )
    return (
        gap_row(
            dataset=dataset,
            symbol=None,
            last_updated_at=last,
            source=None,
            venue=None,
            missing_reason=reason,
        ),
        last,
    )


def market_event_missing(source: str, category: str, market: str) -> Row:
    return gap_row(
        dataset=f"market_events.{source}.{category}.{market}",
        symbol=None,
        last_updated_at=None,
        source=None,
        venue=None,
        missing_reason="missing_data",
    )


def research_reports_gap(readiness: Any) -> tuple[Row | None, datetime | None]:
    if readiness.is_ready:
        return None, readiness.latest_finished_at
    warnings = set(readiness.warnings)
    if "research_reports_unavailable" in warnings:
        reason = "missing_data"
    elif "research_reports_run_unfinished" in warnings:
        reason = "incomplete_evidence"
    else:
        reason = "stale"
    return (
        gap_row(
            dataset=RESEARCH_REPORTS_DATASET,
            symbol=None,
            last_updated_at=readiness.latest_finished_at,
            source=None,
            venue=None,
            missing_reason=reason,
        ),
        readiness.latest_finished_at,
    )


async def read_data_gaps(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any]:
    check_limit(limit)
    digest = params_digest(op="data_gaps")
    offset = decode_cursor(cursor, digest)
    as_of = datetime.now(UTC)
    unknowns = Unknowns()
    rows: list[Row] = []
    seen: list[datetime | None] = []
    signals_failed = False

    async def add(
        field: str,
        read: Callable[[], Awaitable[Any]],
        project: Callable[[Any], tuple[list[Row], datetime | None]],
    ) -> None:
        nonlocal signals_failed
        value, ok = await guarded(session, unknowns, field, read, None)
        if not ok:
            signals_failed = True
            return
        found, newest = project(value)
        rows.extend(found)
        seen.append(newest)

    async def add_source(
        dataset: str,
        read: Callable[[], Awaitable[Any]],
        project: Callable[[Any], tuple[list[Row], datetime | None]],
    ) -> None:
        """One dataset's own sub-read. Any failure, DB or not, is its unknown only."""
        nonlocal signals_failed
        field = f"source.{dataset}"
        try:
            value, ok = await guarded(session, unknowns, field, read, None)
            if not ok:
                signals_failed = True
                return
            found, newest = project(value)
        except Exception:  # noqa: BLE001 — a helper failure must not hide other rows
            unknowns.note(field, "upstream_error")
            signals_failed = True
            return
        rows.extend(found)
        seen.append(newest)

    async with read_snapshot(session):

        async def krx() -> Any:
            return await load_krx_after_market_health(session, now=as_of)

        await add(
            "source.krx_after_market_eligibility",
            krx,
            lambda h: _one(krx_after_market_gap(h)),
        )

        async def runs() -> Any:
            return await ExecutionLedgerRepository(session).latest_run_per_broker()

        await add(
            "source.execution_ledger_reconcile",
            runs,
            lambda r: reconcile_gaps(r, as_of),
        )
        for market in NEGATIVE_CLASS_MARKETS:

            async def negative(market: str = market) -> Any:
                health = await load_negative_class_health(
                    session, market=market, now=as_of
                )
                return health.to_dict()

            await add(
                f"source.negative_class_recording.{market}",
                negative,
                lambda h: _one(negative_class_gap(h)),
            )

        for dataset, metric, provider in _CRYPTO_SINGLE:

            async def single(metric: str = metric, provider: str = provider) -> Any:
                return await get_latest_crypto_insight(
                    session, metric, provider=provider, symbol=None
                )

            await add_source(
                dataset,
                single,
                lambda snap, d=dataset, p=provider: _one(
                    _snapshot_gap(dataset=d, provider=p, snapshot=snap, now=as_of)
                ),
            )

        async def tvl() -> Any:
            return await list_latest_crypto_insights(
                session,
                metrics=[CRYPTO_TVL_METRIC],
                providers=[CRYPTO_TVL_PROVIDER],
            )

        await add_source(
            CRYPTO_TVL_DATASET,
            tvl,
            lambda rows_: _one(_tvl_gap(list(rows_), as_of)),
        )
        # aggregate_oi: no read, the regime view reports it disabled.
        rows.append(crypto_oi_gap())

        for market in _CANDLE_MARKETS_1M:
            dataset = f"candles_1m.{market}"

            async def minute(market: str = market) -> Any:
                trading_day = expected_bar_day(market, as_of, completed=False)
                return trading_day, await _latest_bar_time(
                    session, f"candles_1m.{market}"
                )

            await add_source(
                dataset,
                minute,
                lambda v, d=dataset, zone=_EXCHANGE_ZONE[market]: _one(
                    _bar_gap(
                        dataset=d,
                        latest_time=v[1],
                        trading_day=v[0],
                        day_zone=zone,
                    )
                ),
            )

        for market in _CANDLE_MARKETS_1D:
            dataset = f"candles_1d.{market}"

            async def daily(market: str = market) -> Any:
                trading_day = expected_bar_day(market, as_of, completed=True)
                return trading_day, await _latest_bar_time(
                    session, f"candles_1d.{market}"
                )

            await add_source(
                dataset,
                daily,
                lambda v, d=dataset: _one(
                    _bar_gap(
                        dataset=d,
                        latest_time=v[1],
                        trading_day=v[0],
                        day_zone=UTC,
                    )
                ),
            )

        kst_today = as_of.astimezone(KST).date()

        async def market_events() -> Any:
            return await MarketEventsFreshnessService(session).compute(
                window_from=kst_today - timedelta(days=MARKET_EVENTS_WINDOW_DAYS),
                window_to=kst_today,
                now=as_of,
            )

        def project_market_events(
            response: Any,
        ) -> tuple[list[Row], datetime | None]:
            found: list[Row] = []
            newest: list[datetime | None] = []
            present = set()
            for item in response.rows:
                present.add((item.source, item.category, item.market))
                row, last = market_event_gap(item)
                newest.append(last)
                if row is not None:
                    found.append(row)
            for source, category, market in sorted(EXPECTED_SOURCES):
                if (source, category, market) not in present:
                    found.append(market_event_missing(source, category, market))
            return found, latest(newest)

        await add_source("market_events", market_events, project_market_events)

        async def research() -> Any:
            return await compute_research_reports_readiness(session)

        await add_source(
            RESEARCH_REPORTS_DATASET,
            research,
            lambda r: _one(research_reports_gap(r)),
        )

        for market in VALUATION_MARKETS:
            dataset = f"valuation_snapshots.{market}"

            async def valuation(market: str = market) -> Any:
                trading_day = expected_bar_day(market, as_of, completed=True)
                counts = await MarketValuationSnapshotsRepository(
                    session
                ).coverage_counts(market, fresh_date=trading_day)
                return market, counts, trading_day

            await add_source(
                dataset,
                valuation,
                lambda v: _one(
                    valuation_gap(market=v[0], counts=v[1], trading_day=v[2])
                ),
            )

    # A failed signal means the gap list is incomplete, so no total is claimed.
    total = None if signals_failed else len(rows)
    return build_response(
        as_of=as_of,
        data_as_of=latest(seen),
        rows=rows,
        total=total,
        limit=limit,
        cursor=cursor,
        digest=digest,
        offset=offset,
        unknowns=unknowns,
    )


def _one(pair: tuple[Row | None, datetime | None]) -> tuple[list[Row], datetime | None]:
    row, newest = pair
    return ([row] if row is not None else []), newest
