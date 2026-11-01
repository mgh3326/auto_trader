"""Rebuild daily candles and the point-in-time universe including delisted pairs.

Inputs (all public, unauthenticated):

* Binance spot archive (data.binance.vision): every USDT pair that ever had a
  monthly 1d archive, delisted or not, checksum-verified.
* Binance exchangeInfo snapshot (from the ``snapshot`` command): today's state
  of each pair (``TRADING`` / ``BREAK`` / absent) to label live vs delisted.
* Upbit quotation API: current KRW markets' daily candles, plus probes of
  known delisted markets (Upbit has no archive; delisted markets answer 404).

Outputs under ``<out_root>`` (raw data stays outside git)::

    candles/<venue>/<split>/<SYMBOL>.parquet   split = exploration | holdout
    universe/pit_universe_<venue>.csv          one row per pair (first/last trade)
    universe/manifest.json                     inputs, table hashes, coverage window
    universe/candle_files.json                 sha256 + row count of every candle file
    universe/coverage.json, coverage.md        delisted share and probe evidence

The holdout boundary is the one crypto-corpus-v1 used, so the new candles
keep the same exploration/holdout separation; nothing here reads the holdout
files back.
"""

from __future__ import annotations

import calendar
import hashlib
import json
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from research.crypto_corpus.constants import HOLDOUT_START

from . import binance_archive as ba
from . import upbit_public as up
from .public_http import Fetch
from .snapshot import latest_snapshot
from .universe import PairListing, PitUniverse, derive_listing

CORPUS_ID = "crypto-universe-v1"
SURVIVORSHIP_LABEL = "INCLUDES_DELISTED_WHERE_ARCHIVED"
HOLDOUT_START_DAY: date = HOLDOUT_START.date()
BINANCE_SOURCE = "binance_spot_archive_1d_monthly"
UPBIT_SOURCE = "upbit_quotation_candles_days"
# The previous corpus enumerated only pairs listed today. Stated in ROB-1432.
CURRENT_CORPUS_DELISTED_SHARE = 0.0

CANDLE_SCHEMA = pa.schema(
    [
        ("day", pa.date32()),
        ("open", pa.string()),
        ("high", pa.string()),
        ("low", pa.string()),
        ("close", pa.string()),
        ("volume", pa.string()),
        ("quote_volume", pa.string()),
        ("trades", pa.int64()),
    ]
)


@dataclass(frozen=True)
class VenueResult:
    listings: tuple[PairListing, ...]
    coverage_start: date
    coverage_end: date
    candle_files: tuple[dict[str, Any], ...]
    errors: tuple[dict[str, str], ...]
    extra: dict[str, Any]


EMPTY_RESULT = VenueResult((), date.min, date.min, (), (), {})


def _month_end(month: str) -> date:
    year, mon = int(month[:4]), int(month[5:7])
    return date(year, mon, calendar.monthrange(year, mon)[1])


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_candles(
    out_root: Path, venue: str, symbol: str, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Write one symbol's bars split at the holdout boundary; return file records."""
    records = []
    for split, chosen in (
        ("exploration", [r for r in rows if r["day"] < HOLDOUT_START_DAY]),
        ("holdout", [r for r in rows if r["day"] >= HOLDOUT_START_DAY]),
    ):
        if not chosen:
            continue
        path = out_root / "candles" / venue / split / f"{symbol}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(
            chosen, schema=CANDLE_SCHEMA
        ).replace_schema_metadata(
            {
                "crypto_universe.corpus_id": CORPUS_ID,
                "crypto_universe.venue": venue,
                "crypto_universe.symbol": symbol,
                "crypto_universe.split": split,
                "crypto_universe.survivorship_label": SURVIVORSHIP_LABEL,
            }
        )
        tmp = path.with_suffix(".parquet.part")
        pq.write_table(table, tmp)
        tmp.replace(path)
        records.append(
            {
                "path": path.relative_to(out_root).as_posix(),
                "split": split,
                "rows": len(chosen),
                "sha256": _sha256_file(path),
            }
        )
    return records


# -- Binance -----------------------------------------------------------------


def binance_states(exchange_info: Any) -> dict[str, tuple[str, str, str]]:
    """symbol -> (status, baseAsset, quoteAsset) for USDT spot pairs."""
    if not isinstance(exchange_info, dict) or not isinstance(
        exchange_info.get("symbols"), list
    ):
        raise ValueError("exchangeInfo snapshot has no symbols list")
    out: dict[str, tuple[str, str, str]] = {}
    for item in exchange_info["symbols"]:
        if isinstance(item, dict) and item.get("quoteAsset") == "USDT":
            out[str(item["symbol"])] = (
                str(item.get("status")),
                str(item.get("baseAsset")),
                "USDT",
            )
    return out


def build_binance(
    fetch: Fetch,
    out_root: Path,
    exchange_info: Any,
    *,
    snapshot_date: date,
    cache_dir: Path | None,
    workers: int = 8,
    symbols: Iterable[str] | None = None,
    progress: Callable[[str], None] = lambda _msg: None,
) -> VenueResult:
    venue = "binance_usdt_spot"
    states = binance_states(exchange_info)
    archive_symbols = (
        sorted(set(symbols)) if symbols is not None else ba.list_archive_symbols(fetch)
    )
    progress(f"binance: {len(archive_symbols)} archive symbols")
    months_by_symbol: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for sym, months in zip(
            archive_symbols,
            pool.map(lambda s: ba.list_months(fetch, s), archive_symbols),
            strict=True,
        ):
            months_by_symbol[sym] = months
    all_months = [m for ms in months_by_symbol.values() for m in ms]
    if not all_months:
        raise ba.ArchiveError("no monthly archives found")
    coverage_end = _month_end(max(all_months))
    if snapshot_date <= coverage_end:
        # An older snapshot would label pairs listed after it as delisted.
        raise ValueError(
            f"exchangeInfo snapshot {snapshot_date} is not after coverage_end "
            f"{coverage_end}; take a fresh snapshot first"
        )
    tasks = [(s, m) for s, ms in months_by_symbol.items() for m in ms]
    progress(f"binance: {len(tasks)} symbol-months, coverage_end {coverage_end}")

    bars_by_symbol: dict[str, list[ba.DailyBar]] = {s: [] for s in archive_symbols}
    errors: list[dict[str, str]] = []

    def load(task: tuple[str, str]) -> tuple[str, str, list[ba.DailyBar] | str]:
        sym, month = task
        try:
            return sym, month, ba.fetch_month(fetch, sym, month, cache_dir)
        except Exception as exc:  # recorded per pair; the pair is excluded
            return sym, month, f"{type(exc).__name__}: {exc}"

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for sym, month, result in pool.map(load, tasks):
            done += 1
            if isinstance(result, str):
                errors.append({"symbol": sym, "month": month, "error": result})
            else:
                bars_by_symbol[sym].extend(result)
            if done % 2000 == 0:
                progress(f"binance: {done}/{len(tasks)} months")

    listings: list[PairListing] = []
    files: list[dict[str, Any]] = []
    failed = {e["symbol"] for e in errors}
    for sym in archive_symbols:
        status, base, quote = states.get(sym, ("ABSENT", sym[: -len("USDT")], "USDT"))
        bars = sorted(bars_by_symbol[sym], key=lambda b: b.day)
        days = [b.day for b in bars]
        if len(days) != len(set(days)):
            errors.append({"symbol": sym, "month": "*", "error": "duplicate day"})
            continue
        if sym in failed:
            continue  # incomplete history: excluded rather than truncated
        listings.append(
            derive_listing(
                venue=venue,
                symbol=sym,
                base_asset=base,
                quote_asset=quote,
                traded_days=[b.day for b in bars if b.trades > 0],
                exchange_status=status,
                coverage_end=coverage_end,
                source=BINANCE_SOURCE,
            )
        )
        rows = [
            {
                "day": b.day,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
                "quote_volume": b.quote_volume,
                "trades": b.trades,
            }
            for b in bars
        ]
        for record in write_candles(out_root, venue, sym, rows):
            files.append({"symbol": sym, **record})
    full_run = symbols is None
    not_in_archive = sorted(set(states) - set(archive_symbols)) if full_run else []
    return VenueResult(
        listings=tuple(listings),
        coverage_start=min(
            (x.first_trade_date for x in listings if x.first_trade_date),
            default=coverage_end,
        ),
        coverage_end=coverage_end,
        candle_files=tuple(files),
        errors=tuple(errors),
        extra={
            "full_archive_run": full_run,
            "archive_symbol_count": len(archive_symbols),
            "symbol_month_count": len(tasks),
            "exchange_info_usdt_pairs_without_archive": not_in_archive,
            "exchange_info_states_of_unarchived": dict(
                Counter(states[s][0] for s in not_in_archive)
            ),
        },
    )


# -- Upbit -------------------------------------------------------------------


def build_upbit(
    fetch: Fetch,
    out_root: Path,
    market_all: Any,
    *,
    coverage_end: date,
    delisted_candidates: Iterable[str],
    pacer: up.Pacer | None = None,
    markets: Iterable[str] | None = None,
    progress: Callable[[str], None] = lambda _msg: None,
) -> VenueResult:
    venue = "upbit_krw"
    pacer = pacer or up.Pacer()
    live = up.krw_markets(market_all)
    chosen = sorted(set(markets)) if markets is not None else live
    listings: list[PairListing] = []
    files: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    probes = up.probe_delisted(
        fetch, sorted({m for m in delisted_candidates if m not in live}), pacer
    )
    obtained = [p.market for p in probes if p.obtainable]
    targets = chosen + obtained
    for index, market in enumerate(targets, start=1):
        try:
            candles = up.fetch_daily_candles(fetch, market, coverage_end, pacer)
        except Exception as exc:  # recorded per pair; the pair is excluded
            errors.append(
                {
                    "symbol": market,
                    "month": "*",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        listings.append(
            derive_listing(
                venue=venue,
                symbol=market,
                base_asset=market.split("-", 1)[1],
                quote_asset="KRW",
                traded_days=[c.day for c in candles],
                exchange_status="ACTIVE" if market in live else "ABSENT",
                coverage_end=coverage_end,
                source=UPBIT_SOURCE,
            )
        )
        rows = [
            {
                "day": c.day,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
                "quote_volume": c.quote_volume,
                "trades": None,
            }
            for c in candles
        ]
        for record in write_candles(out_root, venue, market, rows):
            files.append({"symbol": market, **record})
        if index % 50 == 0:
            progress(f"upbit: {index}/{len(targets)} markets")
    return VenueResult(
        listings=tuple(listings),
        coverage_start=min(
            (x.first_trade_date for x in listings if x.first_trade_date),
            default=coverage_end,
        ),
        coverage_end=coverage_end,
        candle_files=tuple(files),
        errors=tuple(errors),
        extra={
            "live_krw_markets": len(live),
            "delisted_probes": [p.as_dict() for p in probes],
            "delisted_obtained": obtained,
            "note": (
                "Upbit publishes no historical archive and its quotation API "
                "answers 404 for delisted markets, so delisted Upbit pairs are "
                "only obtainable when a probe returns candles."
            ),
        },
    )


# -- coverage ----------------------------------------------------------------


def _share(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def venue_coverage(result: VenueResult) -> dict[str, Any]:
    traded = [x for x in result.listings if x.status != "no_trades"]
    statuses = Counter(x.status for x in result.listings)
    delisted = [x for x in traded if x.status == "delisted"]
    core = [x for x in traded if not x.is_leveraged_token and not x.is_stable_base]
    core_delisted = [x for x in core if x.status == "delisted"]
    by_year = Counter(
        x.last_trade_date.year for x in delisted if x.last_trade_date is not None
    )
    return {
        "coverage_start": result.coverage_start.isoformat(),
        "coverage_end": result.coverage_end.isoformat(),
        "pairs": len(result.listings),
        "pairs_with_trades": len(traded),
        "status_counts": dict(sorted(statuses.items())),
        "delisted_pairs": len(delisted),
        "delisted_share": _share(len(delisted), len(traded)),
        "delisted_share_excluding_leveraged_and_stable": _share(
            len(core_delisted), len(core)
        ),
        "leveraged_token_pairs": sum(x.is_leveraged_token for x in traded),
        "stable_or_fiat_base_pairs": sum(x.is_stable_base for x in traded),
        "delisted_missing_last_trade_date": sum(
            x.last_trade_date is None for x in delisted
        ),
        "delisted_last_trade_by_year": dict(sorted(by_year.items())),
        "censored_last_trade": sum(x.last_trade_censored for x in traded),
        "pairs_with_internal_gaps": sum(x.gap_days > 0 for x in traded),
        "pairs_with_multiple_episodes": sorted(
            x.symbol for x in traded if len(x.episodes) > 1
        ),
        "fetch_errors": list(result.errors),
        **result.extra,
    }


def render_coverage_md(coverage: dict[str, Any]) -> str:
    lines = [
        f"# {CORPUS_ID} coverage",
        "",
        f"Built {coverage['built_at']}. Previous corpus (crypto-corpus-v1) "
        f"delisted share: {CURRENT_CORPUS_DELISTED_SHARE:.0%} (survivor-only).",
        "",
        "| venue | window | pairs traded | live | delisted | delisted share | "
        "share excl. leveraged/stable | censored last trade |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for venue, cov in coverage["venues"].items():
        counts = cov["status_counts"]
        lines.append(
            f"| {venue} | {cov['coverage_start']} .. {cov['coverage_end']} | "
            f"{cov['pairs_with_trades']} | {counts.get('live', 0)} | "
            f"{cov['delisted_pairs']} | {cov['delisted_share']:.1%} | "
            f"{cov['delisted_share_excluding_leveraged_and_stable']:.1%} | "
            f"{cov['censored_last_trade']} |"
        )
    for venue, cov in coverage["venues"].items():
        lines += ["", f"## {venue}", ""]
        lines.append(f"- status counts: {cov['status_counts']}")
        lines.append(
            f"- delisted pairs by year of last trade: {cov['delisted_last_trade_by_year']}"
        )
        lines.append(
            f"- delisted pairs missing a last trade date: "
            f"{cov['delisted_missing_last_trade_date']}"
        )
        lines.append(
            f"- pairs with internal no-bar days: {cov['pairs_with_internal_gaps']}"
        )
        multi = cov["pairs_with_multiple_episodes"]
        lines.append(
            f"- pairs with more than one listing episode (relisted or symbol "
            f"reused): {len(multi)} {multi}"
        )
        lines.append(f"- fetch errors (pairs excluded): {len(cov['fetch_errors'])}")
        if cov.get("full_archive_run"):
            missing = cov["exchange_info_usdt_pairs_without_archive"]
            lines.append(
                f"- exchangeInfo USDT pairs with no monthly archive yet: {len(missing)} "
                f"{cov['exchange_info_states_of_unarchived']}"
            )
        if "delisted_probes" in cov:
            probes = cov["delisted_probes"]
            statuses = Counter(p["http_status"] for p in probes)
            lines.append(f"- {cov['note']}")
            lines.append(
                f"- delisted probes: {len(probes)} markets, HTTP status counts "
                f"{dict(sorted(statuses.items()))}, obtained {cov['delisted_obtained']} "
                f"(per-probe status and body sha256 in coverage.json)"
            )
            known = [p for p in probes if p["market"] in up.DEFAULT_DELISTED_PROBES]
            for probe in known:
                lines.append(
                    f"  - known delisted {probe['market']}: HTTP {probe['http_status']}, "
                    f"candles {probe['candles_returned']}"
                )
    return "\n".join(lines) + "\n"


def write_outputs(
    out_root: Path,
    results: dict[str, VenueResult],
    inputs: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write one PIT table per venue (each has its own coverage window)."""
    built_at = (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    folder = out_root / "universe"
    folder.mkdir(parents=True, exist_ok=True)
    tables: dict[str, dict[str, str]] = {}
    for venue, result in results.items():
        universe = PitUniverse(
            result.coverage_start, result.coverage_end, result.listings
        )
        path = folder / f"pit_universe_{venue}.csv"
        path.write_text(universe.to_csv(), encoding="utf-8")
        tables[venue] = {
            "path": path.relative_to(out_root).as_posix(),
            "sha256": universe.sha256(),
            "coverage_start": result.coverage_start.isoformat(),
            "coverage_end": result.coverage_end.isoformat(),
        }
    coverage = {
        "corpus_id": CORPUS_ID,
        "built_at": built_at,
        "current_corpus_delisted_share": CURRENT_CORPUS_DELISTED_SHARE,
        "venues": {venue: venue_coverage(r) for venue, r in results.items()},
    }
    (folder / "coverage.json").write_text(
        json.dumps(coverage, indent=2, sort_keys=True) + "\n"
    )
    (folder / "coverage.md").write_text(render_coverage_md(coverage))
    candle_bytes = (
        json.dumps(
            {venue: list(r.candle_files) for venue, r in results.items()},
            indent=1,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    candle_index = folder / "candle_files.json"
    candle_index.write_bytes(candle_bytes)
    manifest = {
        "corpus_id": CORPUS_ID,
        "built_at": built_at,
        "survivorship_label": SURVIVORSHIP_LABEL,
        "holdout_start": HOLDOUT_START_DAY.isoformat(),
        "inputs": inputs,
        "pit_rule": (
            "live pair: first_trade_date <= t; other pairs: first_trade_date <= t "
            "<= last_trade_date; t after coverage_end raises"
        ),
        "tables": tables,
        "candle_files": {
            "path": candle_index.relative_to(out_root).as_posix(),
            "sha256": hashlib.sha256(candle_bytes).hexdigest(),
            "count": sum(len(r.candle_files) for r in results.values()),
        },
    }
    (folder / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    return coverage


def resolve_snapshot(root: Path, source: str) -> tuple[Any, dict[str, str]]:
    snap_day, raw, digest = latest_snapshot(root, source)
    return json.loads(raw), {
        "source": source,
        "snapshot_date": snap_day.isoformat(),
        "sha256": digest,
    }
