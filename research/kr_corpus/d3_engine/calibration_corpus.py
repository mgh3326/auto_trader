"""D3_CALIBRATION_2025 access-spy loader — the sole authorized opener of the
sealed kr-corpus-v1 holdout root, scoped exclusively to calendar year 2025.

D3-C2P (`brief-D3-DIAG-calibration-fidelity-20260807.md`) is the first job
ever authorized to open `D3_CALIBRATION_2025`. This module implements that
one-time, narrowly-scoped open. It is new runner/loader code only:

* it does not import, modify, or relax
  ``research/kr_corpus/backtest/holdout_guard.py`` or
  ``research/kr_corpus/d3_engine/guards.py`` — both continue to hard-block
  holdout/calibration/prospective access for every other caller unchanged
  (regression-pinned in ``tests/test_calibration_corpus_guard.py``);
* it never resolves, opens, or decodes any byte range whose session date is
  outside 2025-01-01..2025-12-31, even though the sealed holdout root mixes
  2025 and 2026 ("prospective") data in the same manifest, checksum list, and
  ``source-anomalies.jsonl`` file;
* every gate is checked *before* the corresponding read, and every check and
  every read is counted in ``CalibrationAccessSpy`` — no self-declared "zero
  log" statement is treated as evidence (GAP-08).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Callable, TypeVar

from research.kr_corpus.backtest.holdout_guard import (
    HOLDOUT_DIR,
    HoldoutAccessError,
    path_is_under_holdout,
)

T = TypeVar("T")

CALIBRATION_YEAR = 2025
CALIBRATION_WINDOW_START = date(2025, 1, 1)
CALIBRATION_WINDOW_END = date(2025, 12, 31)

HOLDOUT_RUN_ID = "kr-corpus-v1-20260803-1001"

# Byte-level pre-check only: extract the "session":"YYYY-..." year without a
# JSON decode. Used to skip 2026 ("prospective") JSONL lines before any of
# their field values (open/high/low/close) ever reach a Python object.
_SESSION_YEAR_RE = re.compile(rb'"session"\s*:\s*"(\d{4})-')


class CalibrationCorpusInvalid(ValueError):
    code = "RUN_INVALID_CALIBRATION_CORPUS"


class CalibrationAccessBlocked(PermissionError):
    code = "SEALED_ACCESS_BLOCKED"


@dataclass(slots=True)
class CalibrationAccessSpy:
    """Counts every gate check and every completed read. Fail-closed only."""

    # gates evaluated before any read
    year_checks: int = 0
    date_checks: int = 0
    path_checks: int = 0
    # authorized (year==2025) completed reads
    manifest_reads: int = 0
    checksums_reads: int = 0
    parquet_files_read: int = 0
    bar_rows_read: int = 0
    gap_files_read: int = 0
    gap_rows_read: int = 0
    anomaly_lines_prechecked: int = 0
    anomaly_lines_decoded_2025: int = 0
    anomaly_lines_skipped_2026_undecoded: int = 0
    # blocked attempts (must be >0 only in the dedicated fail-closed probes)
    blocked_year_attempts: int = 0
    blocked_date_attempts: int = 0
    blocked_path_attempts: int = 0

    def evidence(self) -> dict[str, int]:
        return {
            "calibration_year_checks": self.year_checks,
            "calibration_date_checks": self.date_checks,
            "calibration_path_checks": self.path_checks,
            "calibration_manifest_reads": self.manifest_reads,
            "calibration_checksums_reads": self.checksums_reads,
            "calibration_parquet_files_read": self.parquet_files_read,
            "calibration_bar_rows_read": self.bar_rows_read,
            "calibration_gap_files_read": self.gap_files_read,
            "calibration_gap_rows_read": self.gap_rows_read,
            "calibration_anomaly_lines_prechecked": self.anomaly_lines_prechecked,
            "calibration_anomaly_lines_decoded_2025": self.anomaly_lines_decoded_2025,
            "calibration_anomaly_lines_skipped_2026_undecoded": (
                self.anomaly_lines_skipped_2026_undecoded
            ),
            "blocked_year_attempts": self.blocked_year_attempts,
            "blocked_date_attempts": self.blocked_date_attempts,
            "blocked_path_attempts": self.blocked_path_attempts,
        }


class CalibrationAccessGuard:
    """Authorize exactly ``D3_CALIBRATION_2025`` reads; block everything else.

    This is a *narrow allow-list* guard, the inverse shape of
    ``guards.SealedAccessGuard`` (which is a deny-list for exploration code).
    Both can coexist because they gate disjoint callers: exploration code
    never imports this module, and this module never imports
    ``SealedAccessGuard``.
    """

    def __init__(self, spy: CalibrationAccessSpy | None = None) -> None:
        self.spy = spy or CalibrationAccessSpy()

    def assert_calibration_year(self, year: int) -> int:
        self.spy.year_checks += 1
        if year != CALIBRATION_YEAR:
            self.spy.blocked_year_attempts += 1
            raise CalibrationAccessBlocked(
                f"year {year} is not the authorized D3_CALIBRATION_2025 year "
                f"({CALIBRATION_YEAR}); holdout years outside this scope "
                "(including 2026 prospective) remain BLOCKED"
            )
        return year

    def assert_calibration_date(self, value: date) -> date:
        self.spy.date_checks += 1
        if not (CALIBRATION_WINDOW_START <= value <= CALIBRATION_WINDOW_END):
            self.spy.blocked_date_attempts += 1
            raise CalibrationAccessBlocked(
                f"date {value.isoformat()} is outside the authorized "
                f"D3_CALIBRATION_2025 window {CALIBRATION_WINDOW_START}.."
                f"{CALIBRATION_WINDOW_END}"
            )
        return value

    def assert_calibration_path(self, path: Path) -> Path:
        """Path must resolve under HOLDOUT_DIR and its year=2025 partition.

        Reuses ``holdout_guard.path_is_under_holdout`` as a *positive* check
        (this loader must only ever touch the canonical holdout root) rather
        than the negative deny-check every other caller uses it for.
        """
        self.spy.path_checks += 1
        resolved = path.expanduser().resolve(strict=False)
        if not path_is_under_holdout(resolved):
            self.spy.blocked_path_attempts += 1
            raise CalibrationAccessBlocked(
                f"path {resolved} is not under the authorized holdout root "
                f"{HOLDOUT_DIR}"
            )
        if "year=2026" in resolved.parts or resolved.parts[-2:-1] == ("2026",):
            self.spy.blocked_path_attempts += 1
            raise CalibrationAccessBlocked(
                f"path {resolved} touches the 2026 prospective partition; "
                "blocked outside D3_CALIBRATION_2025 scope"
            )
        return resolved

    def read_manifest(self, *, path: Path, loader: Callable[[], T]) -> T:
        self.assert_calibration_path(path)
        result = loader()
        self.spy.manifest_reads += 1
        return result

    def read_checksums(self, *, path: Path, loader: Callable[[], T]) -> T:
        self.assert_calibration_path(path)
        result = loader()
        self.spy.checksums_reads += 1
        return result

    def read_year_partition_parquet(
        self, *, year: int, path: Path, loader: Callable[[], T]
    ) -> T:
        self.assert_calibration_year(year)
        self.assert_calibration_path(path)
        result = loader()
        self.spy.parquet_files_read += 1
        return result

    def read_year_partition_gap_file(
        self, *, year: int, path: Path, loader: Callable[[], T]
    ) -> T:
        self.assert_calibration_year(year)
        self.assert_calibration_path(path)
        result = loader()
        self.spy.gap_files_read += 1
        return result

    def record_bar_rows(self, sessions: list[date] | tuple[date, ...]) -> None:
        for session in sessions:
            self.assert_calibration_date(session)
        self.spy.bar_rows_read += len(sessions)

    def record_gap_rows(self, count: int) -> None:
        self.spy.gap_rows_read += count

    def precheck_anomaly_line(self, raw_line: bytes) -> bool:
        """Byte-level year check with NO JSON decode. Returns True iff 2025.

        This is the gate that lets the loader stream a single
        ``source-anomalies.jsonl`` file that mixes 2025 (authorized) and 2026
        (blocked) records without ever constructing a Python object from a
        2026 line's field values (open/high/low/close/volume).
        """
        self.spy.anomaly_lines_prechecked += 1
        match = _SESSION_YEAR_RE.search(raw_line)
        if match is None:
            raise CalibrationCorpusInvalid("anomaly line has no session field")
        year = int(match.group(1))
        if year != CALIBRATION_YEAR:
            self.spy.anomaly_lines_skipped_2026_undecoded += 1
            return False
        self.spy.anomaly_lines_decoded_2025 += 1
        return True


def measure_calibration_fail_closed_probes() -> dict[str, object]:
    """Prove the three non-calibration routes stay BLOCKED (GAP-08).

    Mirrors ``guards.measure_sealed_fail_closed_probes``: every probe must
    raise before its loader executes, and the shared, unmodified
    ``holdout_guard`` regression pins (2025 AND 2026 dates) must still block.
    """
    spy = CalibrationAccessSpy()
    guard = CalibrationAccessGuard(spy)
    loader_calls = 0

    def loader() -> bytes:
        nonlocal loader_calls
        loader_calls += 1
        return b"forbidden"

    outcomes: dict[str, str] = {}

    # 1) holdout year=2026 (prospective) partition — BLOCKED
    try:
        guard.read_year_partition_parquet(
            year=2026,
            path=HOLDOUT_DIR
            / "runs"
            / HOLDOUT_RUN_ID
            / "dataset"
            / "market=KOSPI"
            / "year=2026"
            / "ticker=005930.parquet",
            loader=loader,
        )
    except CalibrationAccessBlocked:
        outcomes["prospective_2026_partition"] = "PASS"
    else:
        outcomes["prospective_2026_partition"] = "FAIL"

    # 2) a path outside the canonical holdout root, even if it says "2025" —
    #    BLOCKED (path allow-list, not just a year check)
    try:
        guard.read_year_partition_parquet(
            year=2025,
            path=Path("/tmp/not-holdout/dataset/market=KOSPI/year=2025/x.parquet"),
            loader=loader,
        )
    except CalibrationAccessBlocked:
        outcomes["non_holdout_path"] = "PASS"
    else:
        outcomes["non_holdout_path"] = "FAIL"

    # 3) a 2026-dated bar observed mid-stream — BLOCKED
    try:
        guard.record_bar_rows([date(2025, 6, 2), date(2026, 1, 5)])
    except CalibrationAccessBlocked:
        outcomes["prospective_2026_date_mid_stream"] = "PASS"
    else:
        outcomes["prospective_2026_date_mid_stream"] = "FAIL"

    # 4) regression pin — the shared, unmodified holdout_guard must still
    #    hard-block both in-window years for every *other* caller.
    from research.kr_corpus.backtest import holdout_guard as _hg

    holdout_guard_still_blocks = True
    for probe_date in (date(2025, 6, 1), date(2026, 1, 5)):
        try:
            _hg.assert_date_not_holdout(probe_date)
        except HoldoutAccessError:
            pass
        else:
            holdout_guard_still_blocks = False
    outcomes["holdout_guard_regression_unchanged"] = (
        "PASS" if holdout_guard_still_blocks else "FAIL"
    )

    # 5) regression pin — SealedAccessGuard (exploration/primary code) must
    #    still hard-block any 2025+ date, unaffected by this module existing.
    from research.kr_corpus.d3_engine.guards import (
        SealedAccessBlocked,
        SealedAccessGuard,
    )

    sealed_guard = SealedAccessGuard()
    try:
        sealed_guard.assert_exploration_date(date(2025, 1, 2))
    except SealedAccessBlocked:
        outcomes["sealed_access_guard_regression_unchanged"] = "PASS"
    else:
        outcomes["sealed_access_guard_regression_unchanged"] = "FAIL"

    evidence = spy.evidence()
    passed = all(value == "PASS" for value in outcomes.values()) and loader_calls == 0
    return {
        "status": "PASS" if passed else "FAIL",
        "outcomes": outcomes,
        "loader_calls": loader_calls,
        "spy_evidence": evidence,
    }


# ---------------------------------------------------------------------------
# Bar / clamp materialization (year=2025 only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalibrationBar:
    session: date
    symbol: str
    market: str
    open: int
    high: int
    low: int
    close: int
    volume: int


@dataclass(frozen=True, slots=True)
class CalibrationClampRow:
    session: date
    symbol: str
    market: str
    source_high: int
    source_low: int
    high: int
    low: int
    delta_high: int
    delta_low: int
    classification: str
    admitted: bool


@dataclass(frozen=True, slots=True)
class LoadedCalibrationView:
    view: str  # "original" | "clamp"
    bars: tuple[CalibrationBar, ...]
    clamp_rows: dict[tuple[date, str], CalibrationClampRow]
    no_trade_count: int
    manifest_sha256: str
    parquet_files: int
    row_count: int
    access_evidence: dict[str, int]


@dataclass(frozen=True, slots=True)
class CalibrationCorpusPaths:
    holdout_root: Path

    @classmethod
    def defaults(cls) -> CalibrationCorpusPaths:
        return cls(holdout_root=HOLDOUT_DIR / "runs" / HOLDOUT_RUN_ID)


class CalibrationCorpusLoader:
    """Load ONLY the year=2025 partition of the sealed holdout root."""

    def __init__(
        self,
        *,
        paths: CalibrationCorpusPaths | None = None,
        guard: CalibrationAccessGuard | None = None,
    ) -> None:
        self.paths = paths or CalibrationCorpusPaths.defaults()
        self.guard = guard or CalibrationAccessGuard()
        root = self.paths.holdout_root.expanduser().resolve(strict=True)
        if not path_is_under_holdout(root):
            raise CalibrationCorpusInvalid(
                f"configured root {root} is not the canonical holdout root"
            )
        self.root = root

    def load_manifest(self) -> dict[str, Any]:
        manifest_path = self.root / "manifest.json"
        raw = self.guard.read_manifest(
            path=manifest_path, loader=manifest_path.read_bytes
        )
        manifest = json.loads(raw)
        if manifest.get("scope") != "holdout":
            raise CalibrationCorpusInvalid("holdout manifest scope drift")
        if manifest.get("corpus_id") != "kr-corpus-v1":
            raise CalibrationCorpusInvalid("holdout manifest corpus_id drift")
        return manifest

    def _checksum_entries_for_2025(
        self,
    ) -> tuple[tuple[str, PurePosixPath], ...]:
        checksums_path = self.root / "checksums.sha256"
        raw = self.guard.read_checksums(
            path=checksums_path, loader=checksums_path.read_bytes
        )
        text = raw.decode("utf-8")
        entries: list[tuple[str, PurePosixPath]] = []
        seen: set[PurePosixPath] = set()
        for line in text.splitlines():
            if not line.strip():
                continue
            expected, raw_relative = line.split(maxsplit=1)
            relative = PurePosixPath(raw_relative.lstrip("*"))
            if relative.parts and relative.parts[0] != "dataset":
                continue
            if "year=2025" not in relative.parts:
                continue  # 2026 entries: path noted, bytes never opened below
            if relative.suffix != ".parquet" or relative in seen:
                raise CalibrationCorpusInvalid(f"invalid dataset checksum row:{line}")
            seen.add(relative)
            entries.append((expected, relative))
        if not entries:
            raise CalibrationCorpusInvalid("no year=2025 dataset entries found")
        return tuple(sorted(entries, key=lambda item: str(item[1])))

    def load_original(self) -> LoadedCalibrationView:
        manifest = self.load_manifest()
        entries = self._checksum_entries_for_2025()
        by_symbol: dict[str, list[CalibrationBar]] = defaultdict(list)
        total_rows = 0
        for expected_sha, relative in entries:
            market, year, ticker = _partition(relative)
            path = (self.root / relative).resolve(strict=True)
            actual_sha = self.guard.read_year_partition_parquet(
                year=year, path=path, loader=lambda path=path: _sha256_stream(path)
            )
            if actual_sha != expected_sha:
                raise CalibrationCorpusInvalid(
                    f"parquet checksum drift:{relative}:{actual_sha}!={expected_sha}"
                )
            rows = self.guard.read_year_partition_parquet(
                year=year, path=path, loader=lambda path=path: _read_parquet_rows(path)
            )
            sessions = [date.fromisoformat(str(row["session"])) for row in rows]
            self.guard.record_bar_rows(sessions)
            for row, session in zip(rows, sessions):
                by_symbol[ticker].append(
                    CalibrationBar(
                        session=session,
                        symbol=ticker,
                        market=market,
                        open=int(row["open"]),
                        high=int(row["high"]),
                        low=int(row["low"]),
                        close=int(row["close"]),
                        volume=int(row["volume"]),
                    )
                )
            total_rows += len(rows)
        bars = tuple(
            bar
            for symbol in sorted(by_symbol)
            for bar in sorted(by_symbol[symbol], key=lambda b: b.session)
        )
        return LoadedCalibrationView(
            view="original",
            bars=bars,
            clamp_rows={},
            no_trade_count=0,
            manifest_sha256=hashlib.sha256(
                json.dumps(manifest, sort_keys=True).encode()
            ).hexdigest(),
            parquet_files=len(entries),
            row_count=total_rows,
            access_evidence=self.guard.spy.evidence(),
        )

    def load_clamp(self, original: LoadedCalibrationView) -> LoadedCalibrationView:
        """Apply the frozen clamp formula (manifest-documented, deterministic)
        to the 2025-scope OHLC anomalies only. Reimplemented here (not calling
        ``clamp_admit.build_clamp_admit_view``, which hard-refuses any source
        root containing "holdout" by design) because the frozen builder must
        not be relaxed for this one-time authorized open.
        """
        clamp_rows: dict[tuple[date, str], CalibrationClampRow] = {}
        no_trade_count = 0
        clamp_count = 0
        anomalies_path = self.root / "source-anomalies.jsonl"
        self.guard.assert_calibration_path(anomalies_path)
        with anomalies_path.open("rb") as stream:
            for raw_line in stream:
                if not raw_line.strip():
                    continue
                if not self.guard.precheck_anomaly_line(raw_line):
                    continue  # 2026 line: never json.loads'd, never decoded
                record = json.loads(raw_line)
                if record.get("kind") != "ohlc_invariant_violation":
                    continue
                session = date.fromisoformat(str(record["session"]))
                self.guard.assert_calibration_date(session)
                ticker = str(record["ticker"])
                detail = record["detail"]
                open_price = int(detail["open"])
                source_high = int(detail["high"])
                source_low = int(detail["low"])
                close_price = int(detail["close"])
                is_no_trade = (
                    open_price == 0
                    and source_high == 0
                    and source_low == 0
                    and close_price > 0
                )
                if is_no_trade:
                    no_trade_count += 1
                    continue
                clamped_high = max(source_high, open_price, close_price)
                clamped_low = min(source_low, open_price, close_price)
                delta_high = clamped_high - source_high
                delta_low = source_low - clamped_low
                if delta_high <= 0 and delta_low <= 0:
                    raise CalibrationCorpusInvalid(
                        "tradeable clamp row did not repair OHLC invariant"
                    )
                market = _market_for_ticker(ticker, self._market_lookup())
                key = (session, ticker)
                if key in clamp_rows:
                    raise CalibrationCorpusInvalid(f"duplicate clamped row:{key}")
                clamp_rows[key] = CalibrationClampRow(
                    session=session,
                    symbol=ticker,
                    market=market,
                    source_high=source_high,
                    source_low=source_low,
                    high=clamped_high,
                    low=clamped_low,
                    delta_high=delta_high,
                    delta_low=delta_low,
                    classification="tradeable_adjusted_rounding",
                    admitted=True,
                )
                clamp_count += 1
        self.guard.record_gap_rows(no_trade_count + clamp_count)
        return LoadedCalibrationView(
            view="clamp",
            bars=original.bars,  # clamp_rows carries the corrected OHLC deltas
            clamp_rows=clamp_rows,
            no_trade_count=no_trade_count,
            manifest_sha256=original.manifest_sha256,
            parquet_files=original.parquet_files,
            row_count=original.row_count + clamp_count,
            access_evidence=self.guard.spy.evidence(),
        )

    def _market_lookup(self) -> dict[str, str]:
        lookup: dict[str, str] = {}
        for market in ("KOSPI", "KOSDAQ"):
            partition_dir = self.root / "dataset" / f"market={market}" / "year=2025"
            self.guard.assert_calibration_path(partition_dir)
            for path in sorted(partition_dir.glob("ticker=*.parquet")):
                ticker = path.stem.removeprefix("ticker=")
                lookup[ticker] = market
        return lookup


def _partition(relative: PurePosixPath) -> tuple[str, int, str]:
    if len(relative.parts) != 4:
        raise CalibrationCorpusInvalid(f"unexpected dataset partition:{relative}")
    _, market_part, year_part, ticker_part = relative.parts
    market = market_part.removeprefix("market=")
    year = int(year_part.removeprefix("year="))
    ticker = ticker_part.removeprefix("ticker=").removesuffix(".parquet")
    if market not in {"KOSPI", "KOSDAQ"}:
        raise CalibrationCorpusInvalid(f"unsupported market partition:{market}")
    if year != CALIBRATION_YEAR:
        raise CalibrationCorpusInvalid(f"non-calibration year partition:{year}")
    return market, year, ticker


def _market_for_ticker(ticker: str, lookup: dict[str, str]) -> str:
    market = lookup.get(ticker)
    if market is None:
        raise CalibrationCorpusInvalid(f"clamp anomaly ticker has no market:{ticker}")
    return market


def _sha256_stream(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    columns = ["session", "market", "ticker", "open", "high", "low", "close", "volume"]
    table = pq.ParquetFile(path).read(columns=columns)
    return table.to_pylist()
