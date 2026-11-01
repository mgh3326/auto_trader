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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from research.us_universe.store import code_commit

from . import sources
from .calendars import bar_available_at, is_partition_day, local_today, session_close
from .contract import (
    DATASET_ACTIONS,
    DATASET_BARS,
    DATASET_UNIVERSE,
    DATASETS,
    RefusedRowError,
    canonical_json,
    check_coverage_floor,
    iso,
    make_row,
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

    def observed(self) -> datetime:
        return self.now().astimezone(UTC)


def collect(
    root: Path,
    *,
    market: str,
    day: date,
    datasets: Sequence[str] = DATASETS,
    fetch: Fetch,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    symbols: Iterable[str] | None = None,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    max_universe_drop: float = DEFAULT_MAX_UNIVERSE_DROP,
    commit: str | None = None,
) -> list[PublishResult]:
    unknown = [d for d in datasets if d not in DATASETS]
    if unknown:
        raise ValueError(f"unknown datasets {unknown}")
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
        symbols=frozenset(symbols) if symbols else None,
        min_coverage=min_coverage,
        max_universe_drop=max_universe_drop,
        commit=commit or code_commit(REPO_ROOT),
    )
    run.lookback_days = _actions_lookback_days(root, market, day)
    results: list[PublishResult] = []
    errors: list[CollectError | RefusedRowError] = []
    for dataset in DATASETS:  # fixed order: universe, bars, actions
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


def _collect_one(run: _Run, dataset: str) -> PublishResult:
    root, market, day = run.root, run.market, run.day
    with dataset_lock(root, dataset, market):
        if partition_path(root, dataset, market, day).exists():
            # recorded: already_present; unrecorded (crash): manifest repair
            # from the on-disk bytes. Either way nothing is fetched.
            return publish_partition(
                root, dataset=dataset, market=market, day=day, rows=(), meta={}
            )
        if dataset == DATASET_BARS and not is_partition_day(market, day):
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
        url = sources.yahoo_chart_url(
            run.market, instrument, venue, run.day, run.lookback_days
        )
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
    entries = sources.parse_universe(run.market, bodies)
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


def _build_bars(run: _Run) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not is_partition_day(run.market, run.day):
        raise CollectError(
            f"{run.day} is not a {run.market} partition day (no session)"
        )
    available = bar_available_at(run.market, run.day)
    if run.now() < available:
        raise RefusedRowError(
            f"bars {run.market} {run.day}: not available until {iso(available)}; "
            f"a bar read now would be a forming bar"
        )
    pairs, refs = _instruments(run)
    rows: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for instrument, venue in pairs:
        try:
            row = _bar_row(run, instrument, venue, available)
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
        "instruments_requested": len(pairs),
        "instruments_missing": len(missing),
        "coverage": round(coverage, 6),
        "missing": missing[:MISSING_LIST_CAP],
        "bar_available_at": iso(available),
    }


def _bar_row(
    run: _Run, instrument: str, venue: str, available: datetime
) -> dict[str, Any] | str:
    common = {
        "instrument": instrument,
        "venue": venue,
        "available_at": iso(available),
        "observed_at": iso(run.observed()),
    }
    if run.market in ("kr", "us"):
        chart = _yahoo(run, instrument, venue)
        if isinstance(chart, str):
            return chart
        bar = next((b for b in chart.bars if b.day == run.day), None)
        if bar is None:
            return "no_bar_for_session"
        if bar.close is None:
            return "null_bar"
        return make_row(
            **common,
            event_time=iso(session_close(run.market, run.day)),
            source=sources.YAHOO_SOURCE,
            source_version=sources.YAHOO_VERSION,
            bar_date=run.day.isoformat(),
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            adj_close=bar.adj_close,
            currency=chart.currency,
            price_basis="as_observed",
        )
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
    today = {
        (r["instrument"], r["venue"]): r
        for r in read_partition(run.root, DATASET_UNIVERSE, run.market, run.day)
    }
    # Reference = the universe the latest earlier actions partition compared
    # against (an actions partition on day X always reads universe X). A day
    # whose actions failed is thereby folded into the next successful diff
    # instead of being lost; with no earlier actions partition the reference is
    # the first universe partition. Only the very first universe is a baseline.
    earlier_actions = [
        d for d in recorded_dates(run.root, DATASET_ACTIONS, run.market) if d < run.day
    ]
    earlier_universes = [d for d in days if d < run.day]
    if earlier_actions and earlier_actions[-1] in earlier_universes:
        prev_day = earlier_actions[-1]
    elif earlier_universes:
        prev_day = earlier_universes[0]
    else:
        return [], {"listing_baseline": True}
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
    out = []
    for (instrument, venue), action_type, before, after in changes:
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
    return out, {
        "listing_baseline": False,
        "listing_compared": [prev_day.isoformat(), run.day.isoformat()],
    }


def _build_actions(run: _Run) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    _require_observable_today(run, DATASET_ACTIONS)
    observed = iso(run.observed())
    known = _known_fact_hashes(run)
    rows: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}

    def emit(
        fields: dict[str, Any], event_time: str, source: str, version: str
    ) -> None:
        identity = {"source": source, "event_time": event_time, **fields}
        fact = _fact_hash(identity)
        if fact in known:
            return
        known.add(fact)
        rows.append(
            make_row(
                **fields,
                event_time=event_time,
                available_at=observed,  # first observation: the source has no announcement time
                observed_at=observed,
                source=source,
                source_version=version,
                fact_hash=fact,
            )
        )

    listing, listing_meta = _listing_changes(run)
    meta.update(listing_meta)
    for fields in listing:
        emit(fields, observed, "universe_diff", "v1")

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
    meta["semantics"] = (
        "first_observed: available_at is when this collector first saw the fact"
    )
    return rows, meta
