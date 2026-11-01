"""Idempotent per-day collectors: universe, daily bars, corporate actions.

``collect(root, market, day, datasets, fetch)`` builds each requested dataset's
partition for ``day`` unless it is already published and recorded, in which
case it fetches nothing and writes nothing. Datasets run in a fixed order
(universe, bars, actions) because bars and actions read the universe.

Refusals (``RefusedRowError``) and coverage failures write nothing for that
dataset; a rerun retries. Nothing here touches a database, a broker, an
account, a credential or a scheduler.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from research.us_universe.store import code_commit

from . import bar_sources, sources
from .bar_sources import (
    DEFAULT_ADJUSTED_WINDOW,
    DEFAULT_SOURCE_ORDER,
    VINTAGE_ADJUSTED,
    VINTAGE_RAW,
    BarSource,
    BrokerBarSource,
    BrokerDailyClient,
    SourceUnavailableError,
    YahooBarSource,
)
from .budget import DEFAULT_BUDGETS, BudgetSpec, ResearchBudget
from .calendars import bar_available_at, is_partition_day, local_today, session_close
from .contract import (
    BAR_DATASETS,
    DATASET_ACTIONS,
    DATASET_BARS,
    DATASET_BARS_ADJUSTED,
    DATASET_UNIVERSE,
    DATASETS,
    RefusedRowError,
    canonical_json,
    check_coverage_floor,
    iso,
    make_row,
    market_datasets,
)
from .public_http import Fetch
from .store import (
    ManifestMismatchError,
    PublishResult,
    dataset_lock,
    manifest_records,
    partition_path,
    publish_partition,
    read_partition,
    recorded_dates,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MIN_COVERAGE = 0.95
# A universe snapshot that loses more than this share of a venue's instruments
# (and more than UNIVERSE_DROP_SLACK of them) since the previous snapshot is a
# truncated source body, not a market event: it would turn every omitted
# instrument into a listing_removed and back. Refused; an operator who has
# checked a genuine mass delisting reruns with a larger --max-universe-drop.
DEFAULT_MAX_UNIVERSE_DROP = 0.10
UNIVERSE_DROP_SLACK = 5
MISSING_LIST_CAP = 500


class CollectError(RuntimeError):
    """A dataset could not be collected; nothing was written for it."""


@dataclass
class _Run:
    root: Path
    market: str
    day: date
    fetch: Fetch
    now: Callable[[], datetime]
    symbols: frozenset[str] | None
    min_coverage: float
    max_universe_drop: float
    commit: str
    lookback_days: int = sources.YAHOO_LOOKBACK_DAYS
    yahoo_cache: dict[tuple[str, str], sources.YahooChart | str] = field(
        default_factory=dict
    )
    source_order: tuple[str, ...] = ()
    bar_sources: dict[str, BarSource] = field(default_factory=dict)
    adjusted_window: int = DEFAULT_ADJUSTED_WINDOW
    source_unavailable: dict[str, str] = field(default_factory=dict)

    def observed(self) -> datetime:
        return self.now().astimezone(UTC)


def collect(
    root: Path,
    *,
    market: str,
    day: date,
    datasets: Sequence[str] | None = None,
    fetch: Fetch,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    symbols: Iterable[str] | None = None,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    max_universe_drop: float = DEFAULT_MAX_UNIVERSE_DROP,
    commit: str | None = None,
    source_order: Sequence[str] | None = None,
    broker_clients: Mapping[str, BrokerDailyClient] | None = None,
    budgets: Mapping[str, ResearchBudget] | None = None,
    source_unavailable: Mapping[str, str] | None = None,
    adjusted_window: int = DEFAULT_ADJUSTED_WINDOW,
) -> list[PublishResult]:
    """Collect ``datasets`` (default: every dataset of the market) for ``day``.

    KR/US bars are served by the first source in ``source_order`` (default
    ``kis, toss, yahoo``) that covers the floor; a broker source needs its
    client in ``broker_clients`` and is otherwise recorded as unavailable.
    Every broker call takes a slot of that broker's ``budgets`` entry (an
    in-memory default budget when none is given).
    """
    allowed = market_datasets(market)
    if datasets is None:
        datasets = allowed
    if not datasets:
        raise ValueError("no datasets requested; an empty run would collect nothing")
    unknown = [d for d in datasets if d not in DATASETS]
    if unknown:
        raise ValueError(f"unknown datasets {unknown}")
    not_here = [d for d in datasets if d not in allowed]
    if not_here:
        raise ValueError(f"datasets {not_here} do not apply to {market}")
    if (
        isinstance(adjusted_window, bool)
        or not isinstance(adjusted_window, int)
        or not 1 <= adjusted_window <= 100
    ):
        raise ValueError(
            f"adjusted_window must be within [1, 100], got {adjusted_window!r}"
        )
    check_coverage_floor(min_coverage)
    if not 0.0 <= max_universe_drop <= 1.0:
        raise ValueError(
            f"max_universe_drop must be within [0, 1], got {max_universe_drop!r}"
        )
    run = _Run(
        root=root,
        market=market,
        day=day,
        fetch=fetch,
        now=now,
        symbols=frozenset(symbols) if symbols is not None else None,
        min_coverage=min_coverage,
        max_universe_drop=max_universe_drop,
        commit=commit or code_commit(REPO_ROOT),
        adjusted_window=adjusted_window,
    )
    run.lookback_days = _actions_lookback_days(root, market, day)
    _configure_bar_sources(run, source_order, broker_clients, budgets)
    run.source_unavailable = dict(source_unavailable or {})
    results: list[PublishResult] = []
    errors: list[CollectError | RefusedRowError] = []
    for dataset in DATASETS:  # fixed order: universe, bars, adjusted, actions
        if dataset not in datasets:
            continue
        # One dataset failing must not stop the others: corporate actions in
        # particular can only be observed on their own day.
        try:
            results.append(_collect_one(run, dataset))
        except (CollectError, RefusedRowError) as exc:
            errors.append(exc)
        except ManifestMismatchError as exc:
            errors.append(CollectError(str(exc)))
    if errors:
        # Refusals outrank collection failures; every error is attached.
        errors.sort(key=lambda e: 0 if isinstance(e, RefusedRowError) else 1)
        first = errors[0]
        for other in errors[1:]:
            first.add_note(f"also: {type(other).__name__}: {other}")
        first.results = results  # type: ignore[attr-defined]
        raise first
    return results


# Extra days requested before the last recorded actions partition, so an
# outage of any length is recovered (events are recorded as first observed on
# the day they are finally read). Capped at ~20 years.
ACTIONS_RECOVERY_MARGIN_DAYS = 7
ACTIONS_MAX_LOOKBACK_DAYS = 7300


def _actions_lookback_days(root: Path, market: str, day: date) -> int:
    earlier = [d for d in recorded_dates(root, DATASET_ACTIONS, market) if d < day]
    if not earlier:
        return sources.YAHOO_LOOKBACK_DAYS
    gap = (day - earlier[-1]).days + ACTIONS_RECOVERY_MARGIN_DAYS
    return min(max(sources.YAHOO_LOOKBACK_DAYS, gap), ACTIONS_MAX_LOOKBACK_DAYS)


def _configure_bar_sources(
    run: _Run,
    source_order: Sequence[str] | None,
    broker_clients: Mapping[str, BrokerDailyClient] | None,
    budgets: Mapping[str, ResearchBudget] | None,
) -> None:
    if run.market not in DEFAULT_SOURCE_ORDER:
        if source_order is not None or broker_clients:
            raise ValueError(
                f"bar sources and broker clients do not apply to {run.market}"
            )
        return
    run.source_order = bar_sources.check_source_order(
        run.market,
        DEFAULT_SOURCE_ORDER[run.market] if source_order is None else source_order,
    )
    run.bar_sources[bar_sources.SOURCE_YAHOO] = YahooBarSource(
        lambda instrument, venue: _yahoo(run, instrument, venue)
    )
    for name, client in (broker_clients or {}).items():
        if name not in bar_sources.BROKER_SOURCES:
            raise ValueError(f"not a broker source: {name!r}")
        budget = (budgets or {}).get(name) or ResearchBudget(
            BudgetSpec(name, *DEFAULT_BUDGETS[name])
        )
        run.bar_sources[name] = BrokerBarSource(name, client, budget)


def _collect_one(run: _Run, dataset: str) -> PublishResult:
    root, market, day = run.root, run.market, run.day
    with dataset_lock(root, dataset, market):
        if partition_path(root, dataset, market, day).exists():
            # recorded: already_present; unrecorded (crash): manifest repair
            # from the on-disk bytes. Either way nothing is fetched.
            return publish_partition(
                root, dataset=dataset, market=market, day=day, rows=(), meta={}
            )
        if dataset in BAR_DATASETS and not is_partition_day(market, day):
            # No session, no bar: a holiday run is a no-op, not a failure.
            return PublishResult(
                dataset,
                market,
                day,
                "skipped_no_session",
                partition_path(root, dataset, market, day),
                0,
                "",
            )
        builder = {
            DATASET_UNIVERSE: _build_universe,
            DATASET_BARS: _build_bars,
            DATASET_BARS_ADJUSTED: _build_bars_adjusted,
            DATASET_ACTIONS: _build_actions,
        }[dataset]
        rows, meta = builder(run)
        return publish_partition(
            root,
            dataset=dataset,
            market=market,
            day=day,
            rows=rows,
            meta={"code_commit": run.commit, **meta},
        )


# --------------------------------------------------------------------------
# helpers


def _require_observable_today(run: _Run, dataset: str) -> None:
    """Snapshots (universe, actions) can only be observed on their own day."""
    today = local_today(run.market, run.now())
    if today != run.day:
        raise RefusedRowError(
            f"{dataset} {run.market} partition {run.day.isoformat()} can only be observed on "
            f"that day; now is {today.isoformat()} — its available_at would be later than "
            f"the partition date"
        )


def _get(run: _Run, url: str) -> bytes:
    resp = run.fetch(url)
    if resp.status != 200:
        raise sources.SourceError(f"HTTP {resp.status}")
    return resp.body


def _coverage_gate(
    run: _Run, dataset: str, requested: int, missing: list[dict[str, str]]
) -> float:
    if requested == 0:
        raise CollectError(
            f"{dataset} {run.market} {run.day}: no instruments to collect"
        )
    coverage = (requested - len(missing)) / requested
    if coverage < run.min_coverage:
        reasons: dict[str, int] = {}
        for item in missing:
            reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
        raise CollectError(
            f"{dataset} {run.market} {run.day}: coverage {coverage:.3f} < {run.min_coverage} "
            f"({len(missing)}/{requested} missing; reasons {reasons}); nothing written"
        )
    return coverage


def _instruments(run: _Run) -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
    """Tradeable (instrument, venue) pairs from the latest two universe
    partitions dated on or before the run day (PIT: never a later universe).

    Two partitions so that an instrument dropped from today's listing still
    gets its final bar.
    """
    days = [
        d
        for d in recorded_dates(run.root, DATASET_UNIVERSE, run.market)
        if d <= run.day
    ]
    if not days:
        raise CollectError(
            f"no universe partition for {run.market} on or before {run.day}; "
            f"collect the universe dataset first"
        )
    records = manifest_records(run.root, DATASET_UNIVERSE, run.market)
    picked = days[-2:]
    pairs: set[tuple[str, str]] = set()
    refs = []
    for d in picked:
        for row in read_partition(run.root, DATASET_UNIVERSE, run.market, d):
            if _selected(run.market, row):
                pairs.add((row["instrument"], row["venue"]))
        refs.append({"date": d.isoformat(), "sha256": records[d.isoformat()]["sha256"]})
    if run.symbols is not None:
        unknown = run.symbols - {p[0] for p in pairs}
        if unknown:
            raise CollectError(f"--symbols not in the universe: {sorted(unknown)}")
        pairs = {p for p in pairs if p[0] in run.symbols}
    return sorted(pairs), refs


def _yahoo(run: _Run, instrument: str, venue: str) -> sources.YahooChart | str:
    key = (instrument, venue)
    if key not in run.yahoo_cache:
        # wide enough for the actions lookback and the adjusted window
        days = max(run.lookback_days, math.ceil(run.adjusted_window * 7 / 5) + 14)
        url = sources.yahoo_chart_url(run.market, instrument, venue, run.day, days)
        try:
            run.yahoo_cache[key] = sources.parse_yahoo_chart(run.market, _get(run, url))
        except (
            sources.SourceError,
            TypeError,
            AttributeError,
            KeyError,
            IndexError,
        ) as exc:
            run.yahoo_cache[key] = f"source_error:{exc}"[:200]
        except OSError as exc:
            run.yahoo_cache[key] = f"fetch_error:{type(exc).__name__}"
    return run.yahoo_cache[key]


# Which universe rows get bars/actions. US: common stocks and ETFs only —
# preferreds ($-series), rights, warrants and units have no Yahoo chart
# (measured 2026-11-01: 7/120 random US universe rows missing, all of those
# kinds; 150/150 of common-stock-or-ETF rows present).
SELECTION_POLICY = {
    "kr": "tradeable",
    "us": "tradeable_and_(likely_common_stock_or_etf)",
    "crypto": "tradeable",
}


def _selected(market: str, row: Mapping[str, Any]) -> bool:
    if row.get("tradeable") is not True:
        return False
    if market == "us":
        return row.get("likely_common_stock") is True or row.get("etf") is True
    return True


def _selection_meta(run: _Run, refs: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "universe_partitions": refs,
        "selection_policy": SELECTION_POLICY[run.market],
        "instrument_selection": "explicit_subset"
        if run.symbols is not None
        else "universe",
    }


# --------------------------------------------------------------------------
# universe


def _build_universe(run: _Run) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    _require_observable_today(run, DATASET_UNIVERSE)
    urls = sources.universe_urls(run.market)
    bodies: dict[str, bytes] = {}
    fetched_files = []
    for name, url in urls.items():
        try:
            bodies[name] = _get(run, url)
        except (sources.SourceError, OSError) as exc:
            raise CollectError(f"universe {run.market}: {name}: {exc}") from exc
        fetched_files.append(
            {
                "name": name,
                "url": url,
                "sha256": hashlib.sha256(bodies[name]).hexdigest(),
            }
        )
    try:
        # parse_universe refuses an empty snapshot and an empty source file:
        # the first partition is the write-once baseline, so an empty one
        # would leave bars with no instruments and turn the next listing diff
        # into "everything was added".
        entries = sources.parse_universe(run.market, bodies)
    except sources.SourceError as exc:
        raise CollectError(
            f"universe {run.market} {run.day}: {exc}; nothing written"
        ) from exc
    observed = iso(run.observed())
    source = {
        "kr": "kis_master",
        "us": "nasdaqtrader_symdir",
        "crypto": "exchange_listing",
    }[run.market]
    rows = [
        make_row(
            instrument=e.instrument,
            venue=e.venue,
            event_time=observed,
            available_at=observed,
            observed_at=observed,
            source=source,
            source_version="v1",
            listing_status=e.listing_status,
            tradeable=e.tradeable,
            **dict(e.attrs),
        )
        for e in entries
    ]
    floor_meta = _universe_floor(run, entries)
    return rows, {
        "source_files": fetched_files,
        "instruments": len(rows),
        **floor_meta,
    }


def _universe_floor(run: _Run, entries: list[sources.UniverseEntry]) -> dict[str, Any]:
    earlier = [
        d for d in recorded_dates(run.root, DATASET_UNIVERSE, run.market) if d < run.day
    ]
    if not earlier:
        return {"universe_floor": "baseline"}
    previous: dict[str, int] = {}
    for row in read_partition(run.root, DATASET_UNIVERSE, run.market, earlier[-1]):
        previous[row["venue"]] = previous.get(row["venue"], 0) + 1
    current: dict[str, int] = {}
    for e in entries:
        current[e.venue] = current.get(e.venue, 0) + 1
    for venue, before in sorted(previous.items()):
        now = current.get(venue, 0)
        lost = before - now
        if lost > UNIVERSE_DROP_SLACK and lost > before * run.max_universe_drop:
            raise CollectError(
                f"universe {run.market} {run.day}: {venue} has {now} instruments, "
                f"{lost} fewer than {before} on {earlier[-1]} (allowed drop "
                f"{run.max_universe_drop:.0%}); a truncated source is refused, "
                f"nothing written"
            )
    return {
        "universe_floor": {
            "compared_to": earlier[-1].isoformat(),
            "max_drop": run.max_universe_drop,
            "previous": previous,
        }
    }


# --------------------------------------------------------------------------
# daily bars

# A broker that fails this many instruments in a row before serving any is
# abandoned for the run (auth or host outage): its budget is not spent on the
# rest of the universe and the next source is tried at once.
SOURCE_ABORT_STREAK = 25


def _bars_ready(run: _Run, dataset: str) -> datetime:
    if not is_partition_day(run.market, run.day):
        raise CollectError(
            f"{run.day} is not a {run.market} partition day (no session)"
        )
    available = bar_available_at(run.market, run.day)
    if run.now() < available:
        raise RefusedRowError(
            f"{dataset} {run.market} {run.day}: not available until {iso(available)}; "
            f"a bar read now would be a forming bar"
        )
    return available


def _build_bars(run: _Run) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    available = _bars_ready(run, DATASET_BARS)
    if run.market in DEFAULT_SOURCE_ORDER:
        return _equity_bars(run, DATASET_BARS, VINTAGE_RAW, available)
    pairs, refs = _instruments(run)
    rows: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for instrument, venue in pairs:
        try:
            row = _crypto_bar_row(run, instrument, venue, available)
        except (
            sources.SourceError,
            TypeError,
            AttributeError,
            KeyError,
            IndexError,
        ) as exc:
            # a malformed body for one instrument is that instrument's gap
            missing.append(
                {
                    "instrument": instrument,
                    "venue": venue,
                    "reason": f"source_error:{exc}"[:200],
                }
            )
            continue
        except OSError as exc:
            missing.append(
                {
                    "instrument": instrument,
                    "venue": venue,
                    "reason": f"fetch_error:{type(exc).__name__}",
                }
            )
            continue
        if isinstance(row, str):
            missing.append({"instrument": instrument, "venue": venue, "reason": row})
        else:
            rows.append(row)
    coverage = _coverage_gate(run, DATASET_BARS, len(pairs), missing)
    return rows, {
        **_selection_meta(run, refs),
        "served_by": "exchange_public",
        "instruments_requested": len(pairs),
        "instruments_missing": len(missing),
        "coverage": round(coverage, 6),
        "missing": missing[:MISSING_LIST_CAP],
        "bar_available_at": iso(available),
    }


def _build_bars_adjusted(run: _Run) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # A vintage is the source's adjustment *as of P*: read later, it would carry
    # adjustments announced after P under P's available_at. Raw bars of P do not
    # change, so only they can be backfilled.
    _require_observable_today(run, DATASET_BARS_ADJUSTED)
    available = _bars_ready(run, DATASET_BARS_ADJUSTED)
    return _equity_bars(run, DATASET_BARS_ADJUSTED, VINTAGE_ADJUSTED, available)


def _reason_counts(missing: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in missing:
        key = item["reason"].split(":", 1)[0]
        counts[key] = counts.get(key, 0) + 1
    return counts


def _equity_bars(
    run: _Run, dataset: str, vintage: str, available: datetime
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Serve the partition from the first source that covers the floor."""
    pairs, refs = _instruments(run)
    if not pairs:
        raise CollectError(
            f"{dataset} {run.market} {run.day}: no instruments to collect"
        )
    attempts: list[dict[str, Any]] = []
    for name in run.source_order:
        source = run.bar_sources.get(name)
        if source is None:
            detail = run.source_unavailable.get(name, "not configured")
            attempts.append(
                {"source": name, "outcome": "unavailable", "detail": detail}
            )
            continue
        try:
            rows, missing, tried = _source_rows(run, source, vintage, pairs, available)
        except SourceUnavailableError as exc:
            attempts.append(
                {"source": name, "outcome": "unavailable", "detail": str(exc)[:200]}
            )
            continue
        coverage = (len(pairs) - len(missing)) / len(pairs)
        if coverage < run.min_coverage:
            # stopped as soon as the floor was out of reach: coverage is then
            # the best the source could still have reached
            attempts.append(
                {
                    "source": name,
                    "outcome": "coverage_below_floor",
                    "coverage": round(coverage, 6),
                    "instruments_tried": tried,
                    "missing_reasons": _reason_counts(missing),
                }
            )
            continue
        attempts.append(
            {"source": name, "outcome": "served", "coverage": round(coverage, 6)}
        )
        meta: dict[str, Any] = {
            **_selection_meta(run, refs),
            "vintage": vintage,
            "served_by": name,
            "source_order": list(run.source_order),
            "source_attempts": attempts,
            "adjustment_basis": source.adjustment_basis(run.market, vintage),
            "instruments_requested": len(pairs),
            "instruments_missing": len(missing),
            "coverage": round(coverage, 6),
            "missing": missing[:MISSING_LIST_CAP],
            "bar_available_at": iso(available),
        }
        if vintage == VINTAGE_ADJUSTED:
            # Re-check after the last read: a run that crossed the market-local
            # midnight read some vintages on the next day, and they would be
            # published under P's available_at (outside _source_rows, whose
            # broad except would turn the refusal into one instrument's gap).
            _require_observable_today(run, dataset)
            meta["adjusted_window_sessions"] = run.adjusted_window
            per_instrument: dict[tuple[str, str], int] = {}
            for r in rows:
                key = (r["instrument"], r["venue"])
                per_instrument[key] = per_instrument.get(key, 0) + 1
            # fewer sessions than the window: a recent listing, or a source
            # that answered short; counted so neither passes unnoticed
            meta["instruments_short_window"] = sum(
                1 for n in per_instrument.values() if n < run.adjusted_window
            )
        budget = getattr(source, "_budget", None)
        if isinstance(budget, ResearchBudget):
            meta["research_budget"] = {name: budget.spec.text()}
        return rows, meta
    raise CollectError(
        f"{dataset} {run.market} {run.day}: no source covered the floor "
        f"{run.min_coverage}: {_attempts_text(attempts, run.min_coverage)}; "
        f"nothing written"
    )


def _attempts_text(attempts: list[dict[str, Any]], floor: float) -> str:
    parts = []
    for a in attempts:
        if a["outcome"] == "unavailable":
            parts.append(f"{a['source']} unavailable ({a['detail']})")
        else:
            parts.append(
                f"{a['source']} coverage {a['coverage']:.3f} < {floor} "
                f"after {a['instruments_tried']} instruments "
                f"(reasons {a['missing_reasons']})"
            )
    return "; ".join(parts)


def _source_rows(
    run: _Run,
    source: BarSource,
    vintage: str,
    pairs: list[tuple[str, str]],
    available: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], int]:
    """Rows and gaps of one source, and how many instruments it was asked for.

    Stops as soon as the coverage floor can no longer be reached, so a failing
    source does not spend its budget (and the run's time) on the rest of the
    universe before the next source is tried.
    """
    rows: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    streak = 0
    tried = 0
    for instrument, venue in pairs:
        if (len(pairs) - len(missing)) / len(pairs) < run.min_coverage:
            break
        tried += 1
        try:
            got = _equity_rows(run, source, vintage, instrument, venue, available)
        except SourceUnavailableError:
            raise
        except sources.SourceError as exc:
            got = f"source_error:{exc}"[:200]
        except OSError as exc:
            got = f"fetch_error:{type(exc).__name__}"
        except Exception as exc:  # noqa: BLE001 - a broker client's own error
            # class name only: broker messages may echo request details
            got = f"client_error:{type(exc).__name__}"
        if isinstance(got, str):
            missing.append({"instrument": instrument, "venue": venue, "reason": got})
            if got.startswith(("source_error", "fetch_error", "client_error")):
                streak += 1
                if not rows and streak >= SOURCE_ABORT_STREAK:
                    raise SourceUnavailableError(
                        f"{source.name}: {streak} consecutive errors before any bar "
                        f"(last {got.split(':', 1)[0]})"
                    )
            continue
        streak = 0
        rows.extend(got)
    return rows, missing, tried


def _equity_rows(
    run: _Run,
    source: BarSource,
    vintage: str,
    instrument: str,
    venue: str,
    available: datetime,
) -> list[dict[str, Any]] | str:
    answer = source.bars(
        run.market, instrument, venue, run.day, vintage, run.adjusted_window
    )
    observed = iso(run.observed())  # after the read
    if isinstance(answer, str):
        return answer
    today = next((b for b in answer.bars if b.day == run.day), None)
    if today is None:
        return "no_bar_for_session"
    if today.close is None:
        return "null_bar"
    common: dict[str, Any] = {
        "instrument": instrument,
        "venue": venue,
        "available_at": iso(available),
        "observed_at": observed,
        "source": source.source_id,
        "source_version": source.version,
        "currency": answer.currency,
        "price_basis": vintage,
        "adjustment_basis": source.adjustment_basis(run.market, vintage),
    }
    if vintage == VINTAGE_RAW:
        extra = {}
        if answer.extra and run.day in answer.extra:
            extra["adj_close"] = answer.extra[run.day]  # yahoo only
        return [
            make_row(
                **common,
                **extra,
                event_time=iso(session_close(run.market, run.day)),
                bar_date=run.day.isoformat(),
                open=today.open,
                high=today.high,
                low=today.low,
                close=today.close,
                volume=today.volume,
            )
        ]
    out = []
    for bar in answer.bars[-run.adjusted_window :]:
        if bar.close is None or not is_partition_day(run.market, bar.day):
            continue  # a null or off-calendar bar carries no adjusted price
        out.append(
            make_row(
                **common,
                event_time=iso(session_close(run.market, bar.day)),
                bar_date=bar.day.isoformat(),
                vintage_date=run.day.isoformat(),
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
        )
    return out


def _crypto_bar_row(
    run: _Run, instrument: str, venue: str, available: datetime
) -> dict[str, Any] | str:
    common: dict[str, Any] = {
        "instrument": instrument,
        "venue": venue,
        "available_at": iso(available),
    }
    # crypto: partition P holds the candle of UTC day P-1 (closed at P 00:00)
    if venue == "binance_usdt_spot":
        url = sources.binance_klines_url(instrument, run.day)
        candle = sources.parse_binance_kline(_get(run, url), run.day)
        source, version = sources.BINANCE_SOURCE, sources.BINANCE_VERSION
    elif venue == "upbit_krw":
        url = sources.upbit_candles_url(instrument, run.day)
        candle = sources.parse_upbit_candle(_get(run, url), run.day)
        source, version = sources.UPBIT_SOURCE, sources.UPBIT_VERSION
    else:
        return f"unknown_venue:{venue}"
    common["observed_at"] = iso(run.observed())  # after the read
    if candle is None:
        return "no_candle"
    return make_row(
        **common,
        event_time=iso(session_close(run.market, run.day)),
        source=source,
        source_version=version,
        bar_date=sources.candle_open_day(run.day).isoformat(),
        price_basis="as_observed",
        **candle,
    )


# --------------------------------------------------------------------------
# corporate actions


def _fact_hash(fields: Mapping[str, Any]) -> str:
    """Identity of an action independent of when we observed it."""
    return hashlib.sha256(canonical_json(fields).encode("utf-8")).hexdigest()


def _known_fact_hashes(run: _Run) -> set[str]:
    known: set[str] = set()
    for d in recorded_dates(run.root, DATASET_ACTIONS, run.market):
        if d < run.day:
            known |= {
                r["fact_hash"]
                for r in read_partition(run.root, DATASET_ACTIONS, run.market, d)
            }
    return known


def _listing_changes(run: _Run) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Diff today's universe partition against the last one already diffed."""
    days = [
        d
        for d in recorded_dates(run.root, DATASET_UNIVERSE, run.market)
        if d <= run.day
    ]
    if not days or days[-1] != run.day:
        raise CollectError(
            f"corporate_actions {run.market} {run.day}: needs today's universe partition"
        )
    today_rows = read_partition(run.root, DATASET_UNIVERSE, run.market, run.day)
    today = {(r["instrument"], r["venue"]): r for r in today_rows}
    # when the listing state was observed (universe rows share one stamp)
    seen_at = max(r["observed_at"] for r in today_rows)
    # Reference = the universe the latest earlier actions partition compared
    # against (an actions partition on day X always reads universe X). A day
    # whose actions failed is thereby folded into the next successful diff
    # instead of being lost; with no earlier actions partition the reference is
    # the first universe partition. Only the very first universe is a baseline.
    earlier_actions = [
        d for d in recorded_dates(run.root, DATASET_ACTIONS, run.market) if d < run.day
    ]
    earlier_universes = [d for d in days if d < run.day]
    ref = _listing_reference(run, earlier_actions, earlier_universes)
    if ref is None:
        return [], {"listing_baseline": True, "listing_observed_at": seen_at}
    prev_day = ref.day
    prev = {
        (r["instrument"], r["venue"]): r
        for r in read_partition(run.root, DATASET_UNIVERSE, run.market, prev_day)
    }
    changes = []
    for key in sorted(set(today) | set(prev)):
        old, new = prev.get(key), today.get(key)
        if old is None:
            changes.append((key, "listing_added", None, new["listing_status"]))
        elif new is None:
            changes.append((key, "listing_removed", old["listing_status"], None))
        elif old["listing_status"] != new["listing_status"]:
            changes.append(
                (
                    key,
                    "listing_status_changed",
                    old["listing_status"],
                    new["listing_status"],
                )
            )
    already = _listing_facts_after(run, prev_day) if ref.base_lost else set()
    out = []
    skipped = 0
    for (instrument, venue), action_type, before, after in changes:
        if (instrument, venue, action_type, before, after) in already:
            skipped += 1  # recorded by an actions partition after prev_day
            continue
        out.append(
            {
                "instrument": instrument,
                "venue": venue,
                "action_type": action_type,
                "status_before": before,
                "status_after": after,
                "compared_universe": [prev_day.isoformat(), run.day.isoformat()],
            }
        )
    meta: dict[str, Any] = {
        "listing_observed_at": seen_at,
        "listing_baseline": False,
        "listing_compared": [prev_day.isoformat(), run.day.isoformat()],
    }
    if ref.base is not None and ref.day != ref.base:
        meta["listing_reference_fallback"] = {
            "base": ref.base.isoformat(),
            "used": prev_day.isoformat(),
            # base universe lost: changes the base actions already recorded are
            # skipped; a reference after the base cannot see (base, used]
            "already_recorded_skipped": skipped,
            "unobserved_interval": [ref.base.isoformat(), prev_day.isoformat()]
            if prev_day > ref.base
            else None,
        }
    return out, meta


@dataclass(frozen=True)
class _ListingRef:
    day: date  # the universe the diff compares against
    base: date | None  # the universe the last actions partition compared against
    base_lost: bool  # base has no universe record any more


def _listing_reference(
    run: _Run, earlier_actions: list[date], earlier_universes: list[date]
) -> _ListingRef | None:
    """The universe the next listing diff compares against.

    * No earlier universe: ``None`` (today is the baseline).
    * No earlier actions partition: the first universe (nothing is recorded
      yet, so every change since the first universe is new).
    * Otherwise the *base* is the universe the latest earlier actions partition
      compared against (its own day). Every recorded listing fact compares
      against a universe on or before the base.
      - The base universe is recorded: use it (the normal case).
      - It is lost (a crash kept the actions record but not the universe's):
        use the latest universe before the base; the changes between it and
        the base were recorded by actions after it, so those are skipped
        (``_listing_facts_after``) instead of re-emitted under a new
        ``compared_universe``.
      - No universe on or before the base survives: use the nearest universe
        after it. Nothing after the base has been recorded, so nothing is
        re-emitted; the changes inside (base, reference] can no longer be
        observed and the manifest says so.

    Never the first universe once actions exist, unless it is one of those.
    """
    if not earlier_universes:
        return None
    if not earlier_actions:
        return _ListingRef(earlier_universes[0], None, False)
    last = earlier_actions[-1]
    compared = (
        manifest_records(run.root, DATASET_ACTIONS, run.market)
        .get(last.isoformat(), {})
        .get("listing_compared")
    )
    base = last
    if (
        isinstance(compared, list)
        and len(compared) == 2
        and isinstance(compared[1], str)
    ):
        try:
            base = date.fromisoformat(compared[1])
        except ValueError:
            base = last
    at_or_before = [d for d in earlier_universes if d <= base]
    if at_or_before:
        return _ListingRef(at_or_before[-1], base, at_or_before[-1] != base)
    after = [d for d in earlier_universes if d > base]
    return _ListingRef(after[0], base, True)


def _listing_facts_after(run: _Run, day: date) -> set[tuple[Any, ...]]:
    """Listing changes recorded by actions partitions dated after ``day``."""
    out: set[tuple[Any, ...]] = set()
    for d in recorded_dates(run.root, DATASET_ACTIONS, run.market):
        if day < d < run.day:
            for r in read_partition(run.root, DATASET_ACTIONS, run.market, d):
                if str(r.get("action_type", "")).startswith("listing_"):
                    out.add(
                        (
                            r["instrument"],
                            r["venue"],
                            r["action_type"],
                            r.get("status_before"),
                            r.get("status_after"),
                        )
                    )
    return out


def _build_actions(run: _Run) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    _require_observable_today(run, DATASET_ACTIONS)
    known = _known_fact_hashes(run)
    pending: list[tuple[dict[str, Any], str, str, str, str]] = []
    meta: dict[str, Any] = {}

    def emit(
        fields: dict[str, Any], event_time: str, source: str, version: str
    ) -> None:
        identity = {"source": source, "event_time": event_time, **fields}
        fact = _fact_hash(identity)
        if fact in known:
            return
        known.add(fact)
        pending.append((fields, event_time, source, version, fact))

    listing, listing_meta = _listing_changes(run)
    listing_seen = listing_meta.pop("listing_observed_at")
    meta.update(listing_meta)
    for fields in listing:
        emit(fields, listing_seen, "universe_diff", "v1")

    if run.market in ("kr", "us"):
        pairs, refs = _instruments(run)
        missing: list[dict[str, str]] = []
        for instrument, venue in pairs:
            chart = _yahoo(run, instrument, venue)
            if isinstance(chart, str):
                missing.append(
                    {"instrument": instrument, "venue": venue, "reason": chart}
                )
                continue
            for ev in chart.events:
                if ev.ex_date > run.day:
                    continue  # never record a future-dated event as known today
                emit(
                    {
                        "instrument": instrument,
                        "venue": venue,
                        "action_type": ev.action_type,
                        "ex_date": ev.ex_date.isoformat(),
                        **dict(ev.fields),
                    },
                    iso(ev.event_time),
                    sources.YAHOO_SOURCE,
                    sources.YAHOO_VERSION,
                )
        coverage = _coverage_gate(run, DATASET_ACTIONS, len(pairs), missing)
        meta.update(
            {
                **_selection_meta(run, refs),
                "instruments_requested": len(pairs),
                "instruments_missing": len(missing),
                "coverage": round(coverage, 6),
                "missing": missing[:MISSING_LIST_CAP],
                "lookback_days": run.lookback_days,
            }
        )
    # Stamp after every source read of this run has finished: a fact cannot be
    # claimed known before the read that revealed it (bars may have read the
    # same charts earlier; the later stamp is the conservative one).
    observed = iso(run.observed())
    rows = [
        make_row(
            **fields,
            event_time=event_time,
            available_at=observed,  # first observation: no announcement time exists
            observed_at=observed,
            source=source,
            source_version=version,
            fact_hash=fact,
        )
        for fields, event_time, source, version, fact in pending
    ]
    meta["semantics"] = (
        "first_observed: available_at is when this collector first saw the fact"
    )
    return rows, meta
