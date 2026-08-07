"""Measured loader for ``D3_CALIBRATION_2025`` — the sealed 2025 partition.

Every byte this module opens passes a :class:`CalibrationAccessGuard` check
first, and the allow-list it binds is derived from the sealed manifest itself:
``manifest.json`` pins ``checksums.sha256`` by digest, and ``checksums.sha256``
enumerates every file. Only enumerated entries whose ``year=`` partition is
2025 (plus the two enumerating documents) become readable; the 2026 rows in the
same list stay closed.

``source-anomalies.jsonl`` is one of the enumerated files, and it interleaves
2025 and 2026 records. Its lines are screened by a byte-level year match
*before* ``json.loads``, so a prospective record's OHLC values never become
Python objects.

The clamp view reproduces the frozen ``clamp-admit-v1`` contract exactly —
``high = max(source_high, open, close)``, ``low = min(source_low, open, close)``,
``no_trade`` (``open=high=low=0 and close>0``) excluded, everything else
admitted. ``research/kr_corpus/clamp_admit.py`` cannot be reused directly: it
refuses a holdout source root by design, and relaxing that refusal is exactly
what A2 forbids.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

from research.kr_corpus.d3_engine.calibration_guard import (
    CALIBRATION_YEAR,
    HOLDOUT_RUN_ID,
    CalibrationAccessGuard,
    partition_year,
)
from research.kr_corpus.d3_engine.primary_corpus import ClampRow, CorpusBar

_SESSION_YEAR = re.compile(rb'"session"\s*:\s*"(\d{4})-')
# The exploration corpus only ever saw ``[0-9]{5}[0-9A-Z]``. The 2025 partition
# also carries KRX short codes whose fifth character is a letter (56 of 2,862:
# 32 ``DDDDAD`` + 24 ``DDDDDA``), so the shape is widened by exactly that much
# and no further.
_TICKER = re.compile(r"[0-9]{4}[0-9A-Z]{2}")

CALIBRATION_CORPUS_ID = "kr-corpus-v1"
CALIBRATION_SCOPE = "holdout"
CLAMP_CLASSIFICATION = "tradeable_adjusted_rounding"
DATASET_COLUMNS = ("session", "market", "ticker", "open", "high", "low", "close")


class CalibrationCorpusInvalid(ValueError):
    code = "RUN_INVALID_CALIBRATION_CORPUS"


@dataclass(frozen=True, slots=True)
class CalibrationManifest:
    run_root: Path
    manifest_sha256: str
    checksums_sha256: str
    allowed_paths: tuple[Path, ...]
    dataset_entries: tuple[tuple[str, PurePosixPath], ...]
    gap_entries: tuple[tuple[str, PurePosixPath], ...]
    anomalies_path: Path
    anomalies_sha256_expected: str
    enumerated_total: int
    excluded_out_of_scope: int
    prospective_example: Path

    def evidence(self) -> dict[str, object]:
        return {
            "run_root": str(self.run_root),
            "manifest_sha256": self.manifest_sha256,
            "checksums_sha256": self.checksums_sha256,
            "enumerated_files_total": self.enumerated_total,
            "authorized_files_2025_scope": len(self.allowed_paths),
            "excluded_out_of_scope_files": self.excluded_out_of_scope,
            "dataset_parquet_2025": len(self.dataset_entries),
            "gap_parquet_2025": len(self.gap_entries),
        }


@dataclass(frozen=True, slots=True)
class CalibrationCorpus:
    bars_original: tuple[CorpusBar, ...]
    bars_clamp: tuple[CorpusBar, ...]
    clamp_rows: dict[tuple[date, str], ClampRow]
    no_trade_excluded: int
    anomaly_lines_prechecked: int
    anomaly_lines_decoded_2025: int
    anomaly_lines_skipped_prospective: int
    manifest: CalibrationManifest

    def evidence(self) -> dict[str, object]:
        return {
            **self.manifest.evidence(),
            "original_valid_bar_rows_2025": len(self.bars_original),
            "clamp_admit_rows_2025": len(self.bars_clamp),
            "clamp_rows_admitted_2025": len(self.clamp_rows),
            "no_trade_rows_excluded_2025": self.no_trade_excluded,
            "anomaly_lines_prechecked": self.anomaly_lines_prechecked,
            "anomaly_lines_decoded_2025": self.anomaly_lines_decoded_2025,
            "anomaly_lines_skipped_prospective_undecoded": (
                self.anomaly_lines_skipped_prospective
            ),
        }


def load_calibration_manifest(
    guard: CalibrationAccessGuard,
    *,
    holdout_root: Path,
    run_id: str = HOLDOUT_RUN_ID,
) -> CalibrationManifest:
    """Read the two enumerating documents and bind the A2 path allow-list."""

    run_root = (holdout_root / "runs" / run_id).expanduser().resolve(strict=True)
    manifest_path = run_root / "manifest.json"
    raw_manifest = guard.read_manifest(
        path=manifest_path, loader=manifest_path.read_bytes
    )
    manifest_sha = hashlib.sha256(raw_manifest).hexdigest()
    manifest = json.loads(raw_manifest)
    if manifest.get("scope") != CALIBRATION_SCOPE:
        raise CalibrationCorpusInvalid("sealed manifest scope drift")
    if manifest.get("corpus_id") != CALIBRATION_CORPUS_ID:
        raise CalibrationCorpusInvalid("sealed manifest corpus_id drift")
    if manifest.get("files_list_location") != "checksums.sha256":
        raise CalibrationCorpusInvalid(
            "sealed manifest does not enumerate by checksums"
        )

    checksums_path = run_root / "checksums.sha256"
    raw_checksums = guard.read_file(
        path=checksums_path, loader=checksums_path.read_bytes
    )
    checksums_sha = hashlib.sha256(raw_checksums).hexdigest()
    if checksums_sha != manifest.get("checksums_sha256"):
        raise CalibrationCorpusInvalid(
            f"enumerating checksum drift:{checksums_sha}!={manifest.get('checksums_sha256')}"
        )

    entries: list[tuple[str, PurePosixPath]] = []
    seen: set[PurePosixPath] = set()
    for line in raw_checksums.decode("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            expected, raw_relative = line.split(maxsplit=1)
        except ValueError as exc:
            raise CalibrationCorpusInvalid("malformed checksum row") from exc
        relative = PurePosixPath(raw_relative.lstrip("*"))
        if (
            len(expected) != 64
            or any(character not in "0123456789abcdef" for character in expected)
            or relative.is_absolute()
            or ".." in relative.parts
        ):
            raise CalibrationCorpusInvalid(f"invalid checksum row:{line}")
        if relative in seen:
            raise CalibrationCorpusInvalid(f"duplicate checksum entry:{relative}")
        seen.add(relative)
        entries.append((expected, relative))
    if not entries:
        raise CalibrationCorpusInvalid("sealed manifest enumerates no files")

    allowed: list[Path] = []
    excluded = 0
    prospective_example: Path | None = None
    dataset: list[tuple[str, PurePosixPath]] = []
    gaps: list[tuple[str, PurePosixPath]] = []
    anomalies_expected: str | None = None
    for expected, relative in entries:
        absolute = (run_root / relative).resolve(strict=False)
        year = partition_year(absolute)
        if year is not None and year != CALIBRATION_YEAR:
            excluded += 1
            if prospective_example is None and relative.parts[0] == "dataset":
                prospective_example = absolute
            continue
        allowed.append(absolute)
        head = relative.parts[0]
        if head == "dataset":
            dataset.append((expected, relative))
        elif head == "gaps":
            gaps.append((expected, relative))
        elif str(relative) == "source-anomalies.jsonl":
            anomalies_expected = expected
    if anomalies_expected is None:
        raise CalibrationCorpusInvalid("sealed manifest does not enumerate anomalies")
    if not dataset:
        raise CalibrationCorpusInvalid("sealed manifest has no 2025 dataset partition")
    if prospective_example is None:
        raise CalibrationCorpusInvalid("sealed manifest has no prospective partition")

    guard.bind_manifest_allowlist(allowed, excluded_out_of_scope=excluded)
    return CalibrationManifest(
        run_root=run_root,
        manifest_sha256=manifest_sha,
        checksums_sha256=checksums_sha,
        allowed_paths=tuple(sorted(allowed)),
        dataset_entries=tuple(sorted(dataset, key=lambda item: str(item[1]))),
        gap_entries=tuple(sorted(gaps, key=lambda item: str(item[1]))),
        anomalies_path=run_root / "source-anomalies.jsonl",
        anomalies_sha256_expected=anomalies_expected,
        enumerated_total=len(entries),
        excluded_out_of_scope=excluded,
        prospective_example=prospective_example,
    )


def _sha256_stream(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_parquet_rows(path: Path, columns: tuple[str, ...]) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    table = pq.ParquetFile(path).read(columns=list(columns))
    if tuple(table.column_names) != columns:
        raise CalibrationCorpusInvalid(f"parquet schema drift:{path.name}")
    return table.to_pylist()


def _partition(relative: PurePosixPath) -> tuple[str, int, str]:
    if len(relative.parts) != 4:
        raise CalibrationCorpusInvalid(f"unexpected dataset partition:{relative}")
    _, market_part, year_part, ticker_part = relative.parts
    if not market_part.startswith("market=") or not year_part.startswith("year="):
        raise CalibrationCorpusInvalid(f"malformed dataset partition:{relative}")
    if not ticker_part.startswith("ticker=") or not ticker_part.endswith(".parquet"):
        raise CalibrationCorpusInvalid(f"malformed ticker partition:{relative}")
    market = market_part.removeprefix("market=")
    year = int(year_part.removeprefix("year="))
    ticker = ticker_part.removeprefix("ticker=").removesuffix(".parquet")
    if market not in {"KOSPI", "KOSDAQ"}:
        raise CalibrationCorpusInvalid(f"unsupported market partition:{market}")
    if year != CALIBRATION_YEAR:
        raise CalibrationCorpusInvalid(f"non-calibration year partition:{year}")
    if _TICKER.fullmatch(ticker) is None:
        raise CalibrationCorpusInvalid(f"invalid ticker partition:{ticker}")
    return market, year, ticker


def _bar(
    raw: dict[str, Any],
    *,
    partition: tuple[str, int, str],
    sessions: frozenset[date],
) -> CorpusBar:
    market, year, ticker = partition
    try:
        session = date.fromisoformat(str(raw["session"]))
        row_market = str(raw["market"])
        row_ticker = str(raw["ticker"])
        open_price = int(raw["open"])
        high = int(raw["high"])
        low = int(raw["low"])
        close = int(raw["close"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CalibrationCorpusInvalid("invalid calibration parquet row") from exc
    if (row_market, session.year, row_ticker) != (market, year, ticker):
        raise CalibrationCorpusInvalid("parquet row/partition identity mismatch")
    if session not in sessions:
        raise CalibrationCorpusInvalid(f"bar outside the sealed 2025 axis:{session}")
    if min(open_price, high, low, close) <= 0:
        raise CalibrationCorpusInvalid("bar prices must be positive")
    if low > min(open_price, close) or high < max(open_price, close):
        raise CalibrationCorpusInvalid("bar OHLC ordering invalid")
    return CorpusBar(
        session=session,
        symbol=ticker,
        market=market,
        open_int=open_price,
        high_int=high,
        low_int=low,
        close_int=close,
    )


def _gap_market_by_key(
    guard: CalibrationAccessGuard, manifest: CalibrationManifest
) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for expected, relative in manifest.gap_entries:
        path = (manifest.run_root / relative).resolve(strict=True)
        actual = guard.read_file(
            path=path, loader=lambda path=path: _sha256_stream(path)
        )
        if actual != expected:
            raise CalibrationCorpusInvalid(f"gap checksum drift:{relative}")
        rows = guard.read_parquet(
            path=path,
            loader=lambda path=path: _read_parquet_rows(
                path, ("session", "market", "ticker", "reason")
            ),
        )
        for row in rows:
            if row["reason"] != "ohlc_invariant_violation":
                continue
            key = (str(row["session"]), str(row["ticker"]))
            market = str(row["market"])
            if result.setdefault(key, market) != market:
                raise CalibrationCorpusInvalid("anomaly key maps to two markets")
    if not result:
        raise CalibrationCorpusInvalid("no 2025 OHLC-invariant gap keys")
    return result


def load_calibration_corpus(
    guard: CalibrationAccessGuard,
    *,
    holdout_root: Path,
    run_id: str = HOLDOUT_RUN_ID,
) -> CalibrationCorpus:
    """Load the 2025 original and clamp bar sets under measured authorization."""

    manifest = load_calibration_manifest(
        guard, holdout_root=holdout_root, run_id=run_id
    )
    sessions = guard.calibration_sessions

    original: list[CorpusBar] = []
    for expected, relative in manifest.dataset_entries:
        partition = _partition(relative)
        path = (manifest.run_root / relative).resolve(strict=True)
        actual = guard.read_file(
            path=path, loader=lambda path=path: _sha256_stream(path)
        )
        if actual != expected:
            raise CalibrationCorpusInvalid(
                f"parquet checksum drift:{relative}:{actual}!={expected}"
            )
        rows = guard.read_parquet(
            path=path,
            loader=lambda path=path: _read_parquet_rows(path, DATASET_COLUMNS),
        )
        decoded = [_bar(raw, partition=partition, sessions=sessions) for raw in rows]
        guard.record_bar_rows([bar.session for bar in decoded])
        original.extend(decoded)

    market_by_key = _gap_market_by_key(guard, manifest)

    anomalies_path = manifest.anomalies_path.resolve(strict=True)
    actual = guard.read_file(
        path=anomalies_path, loader=lambda: _sha256_stream(anomalies_path)
    )
    if actual != manifest.anomalies_sha256_expected:
        raise CalibrationCorpusInvalid("source-anomalies checksum drift")

    clamp_rows: dict[tuple[date, str], ClampRow] = {}
    clamp_bars: list[CorpusBar] = []
    prechecked = 0
    decoded_2025 = 0
    skipped = 0
    no_trade = 0
    seen_keys: set[tuple[str, str]] = set()
    guard.assert_exploration_path(anomalies_path)
    with anomalies_path.open("rb") as stream:
        for raw_line in stream:
            if not raw_line.strip():
                continue
            prechecked += 1
            match = _SESSION_YEAR.search(raw_line)
            if match is None:
                raise CalibrationCorpusInvalid("anomaly line lacks a session field")
            if int(match.group(1)) != CALIBRATION_YEAR:
                skipped += 1
                continue
            decoded_2025 += 1
            record = json.loads(raw_line)
            if record.get("kind") != "ohlc_invariant_violation":
                continue
            session_text = str(record["session"])
            ticker = str(record["ticker"])
            key = (session_text, ticker)
            if key in seen_keys:
                raise CalibrationCorpusInvalid("duplicate source anomaly key")
            seen_keys.add(key)
            session = date.fromisoformat(session_text)
            guard.assert_exploration_date(session)
            market = market_by_key.get(key)
            if market is None:
                raise CalibrationCorpusInvalid("OHLC anomaly has no matching gap row")
            detail = record["detail"]
            if not isinstance(detail, dict):
                raise CalibrationCorpusInvalid("OHLC anomaly detail is not an object")
            open_price = int(detail["open"])
            source_high = int(detail["high"])
            source_low = int(detail["low"])
            close_price = int(detail["close"])
            if (
                open_price == 0
                and source_high == 0
                and source_low == 0
                and close_price > 0
            ):
                no_trade += 1
                continue
            clamped_high = max(source_high, open_price, close_price)
            clamped_low = min(source_low, open_price, close_price)
            delta_high = clamped_high - source_high
            delta_low = source_low - clamped_low
            if not (
                clamped_low <= min(open_price, close_price)
                and max(open_price, close_price) <= clamped_high
                and (delta_high > 0 or delta_low > 0)
            ):
                raise CalibrationCorpusInvalid(
                    "tradeable clamp row did not repair the OHLC invariant"
                )
            if clamped_low <= 0:
                raise CalibrationCorpusInvalid("clamped bar prices must be positive")
            entry_key = (session, ticker)
            if entry_key in clamp_rows:
                raise CalibrationCorpusInvalid(f"duplicate clamped row:{entry_key}")
            clamp_rows[entry_key] = ClampRow(
                market=market,
                session=session,
                symbol=ticker,
                source_high=source_high,
                source_low=source_low,
                high=clamped_high,
                low=clamped_low,
                delta_high=delta_high,
                delta_low=delta_low,
                classification=CLAMP_CLASSIFICATION,
                admitted=True,
            )
            clamp_bars.append(
                CorpusBar(
                    session=session,
                    symbol=ticker,
                    market=market,
                    open_int=open_price,
                    high_int=clamped_high,
                    low_int=clamped_low,
                    close_int=close_price,
                )
            )
    guard.record_bar_rows([bar.session for bar in clamp_bars])

    gap_2025 = {
        key for key in market_by_key if key[0].startswith(f"{CALIBRATION_YEAR}-")
    }
    if seen_keys != gap_2025:
        raise CalibrationCorpusInvalid(
            "2025 anomaly keys and 2025 ohlc_invariant gap keys differ"
        )

    original_keys = {(bar.session, bar.symbol) for bar in original}
    overlap = original_keys & set(clamp_rows)
    if overlap:
        raise CalibrationCorpusInvalid(
            f"clamped row collides with a valid bar:{sorted(overlap)[:3]}"
        )

    combined = sorted(
        [*original, *clamp_bars], key=lambda bar: (bar.session, bar.symbol)
    )
    return CalibrationCorpus(
        bars_original=tuple(
            sorted(original, key=lambda bar: (bar.session, bar.symbol))
        ),
        bars_clamp=tuple(combined),
        clamp_rows=clamp_rows,
        no_trade_excluded=no_trade,
        anomaly_lines_prechecked=prechecked,
        anomaly_lines_decoded_2025=decoded_2025,
        anomaly_lines_skipped_prospective=skipped,
        manifest=manifest,
    )


def market_periods(bars: list[CorpusBar]) -> tuple[tuple[date, date, str], ...]:
    """Contiguous market-membership periods for one symbol's ordered bars."""

    periods: list[tuple[date, date, str]] = []
    start = bars[0].session
    end = start
    market = bars[0].market
    for bar in bars[1:]:
        if bar.market != market:
            periods.append((start, end, market))
            start = bar.session
            market = bar.market
        end = bar.session
    periods.append((start, end, market))
    return tuple(periods)


def group_by_symbol(bars: tuple[CorpusBar, ...]) -> dict[str, list[CorpusBar]]:
    grouped: dict[str, list[CorpusBar]] = defaultdict(list)
    for bar in bars:
        grouped[bar.symbol].append(bar)
    for symbol_bars in grouped.values():
        symbol_bars.sort(key=lambda bar: bar.session)
    return dict(grouped)
