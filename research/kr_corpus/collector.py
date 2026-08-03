"""Resumable ``kr-corpus-v1`` collection and validation orchestration.

The implementation intentionally collects membership snapshots before any
OHLCV histories.  Membership is positive evidence from pykrx on each XKRX
session; an absent ticker is never interpreted as delisting.  All source
errors become explicit gaps or terminal preconditions--never fallback data,
forward-fill, or silent row removal.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import json
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from .config import CorpusConfig
from .pacing import RequestEvent, RequestPacer, RequestProjection
from .state import CoverageRow, StateStore
from .storage import (
    ArtifactSizeLimitExceeded,
    ArtifactWriter,
    ExistingSnapshotError,
)

TerminalVerdict = Literal[
    "READY_FOR_RESEARCH",
    "BUILT_WITH_GAPS",
    "BLOCKED_PRECONDITION",
]


class PreconditionBlocked(RuntimeError):
    """A signed prerequisite is absent; collection must not continue."""


@dataclass(frozen=True)
class RunResult:
    terminal_verdict: TerminalVerdict
    request_budget_projected: int | None
    requests_actual: int
    artifact_root: Path | None
    holdout_root: Path | None
    stop_reason: str | None
    crosscheck_verified: bool


@dataclass
class CrosscheckTracker:
    """Frozen KIS comparison data, retained only inside the local job process."""

    samples: dict[tuple[str, str], dict[str, str]]
    matched: set[tuple[str, str]] = field(default_factory=set)
    mismatches: list[dict[str, object]] = field(default_factory=list)

    @classmethod
    def from_csv(cls, path: Path, expected_sha256: str) -> CrosscheckTracker:
        data = path.read_bytes()
        actual_sha256 = hashlib.sha256(data).hexdigest()
        if actual_sha256 != expected_sha256:
            raise PreconditionBlocked("frozen KIS crosscheck SHA-256 does not match")

        text = data.decode("utf-8-sig")
        reader = csv.DictReader(text.splitlines())
        if reader.fieldnames is None:
            raise PreconditionBlocked("frozen KIS crosscheck has no CSV header")
        required = {"symbol", "session_date", "open", "high", "low", "close", "volume"}
        if not required.issubset(set(reader.fieldnames)):
            raise PreconditionBlocked("frozen KIS crosscheck schema is incomplete")

        samples: dict[tuple[str, str], dict[str, str]] = {}
        for row in reader:
            ticker = str(row["symbol"]).strip().zfill(6)
            session = str(row["session_date"]).strip()
            if not ticker or not session:
                raise PreconditionBlocked("frozen KIS crosscheck contains a blank key")
            key = (session, ticker)
            if key in samples:
                raise PreconditionBlocked(
                    "frozen KIS crosscheck contains a duplicate key"
                )
            samples[key] = {
                key_name: (value or "").strip() for key_name, value in row.items()
            }
        return cls(samples=samples)

    @property
    def verified(self) -> bool:
        return True

    def compare(self, session: str, ticker: str, source_row: dict[str, object]) -> None:
        key = (session, ticker)
        expected = self.samples.get(key)
        if expected is None:
            return
        self.matched.add(key)
        differences: list[dict[str, object]] = []
        for field_name in ("open", "high", "low", "close", "volume", "value"):
            frozen_value = expected.get(field_name, "")
            if not frozen_value:
                continue
            source_value = source_row.get(field_name)
            if not _numeric_values_equal(frozen_value, source_value):
                differences.append(
                    {
                        "field": field_name,
                        "frozen_value": frozen_value,
                        "pykrx_value": source_value,
                    }
                )
        if differences:
            self.mismatches.append(
                {
                    "session": session,
                    "ticker": ticker,
                    "reason": "value_mismatch",
                    "differences": differences,
                }
            )

    def add_unmatched(self) -> None:
        for session, ticker in sorted(set(self.samples) - self.matched):
            self.mismatches.append(
                {
                    "session": session,
                    "ticker": ticker,
                    "reason": "source_row_not_observed",
                    "differences": [],
                }
            )


def _numeric_values_equal(expected: str, actual: object) -> bool:
    if actual is None:
        return False
    try:
        frozen = Decimal(expected.replace(",", ""))
        observed = Decimal(str(actual))
    except (InvalidOperation, ValueError):
        return expected == str(actual)
    return frozen == observed


def _years_inclusive(start: str, end: str) -> tuple[int, ...]:
    return tuple(range(int(start[:4]), int(end[:4]) + 1))


def _safe_int(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} is boolean, not an integer")
    try:
        return int(value)  # numpy integers are intentionally normalized here.
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} is not a usable integer") from exc


def _ohlc_is_valid(row: dict[str, object]) -> bool:
    return int(row["low"]) <= min(int(row["open"]), int(row["close"])) and max(
        int(row["open"]), int(row["close"])
    ) <= int(row["high"])


def _json_lines(rows: Iterable[dict[str, object]]) -> bytes:
    return b"".join(
        (
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        for row in rows
    )


class CorpusCollector:
    """Build exactly one signed corpus snapshot using an injected source adapter."""

    def __init__(
        self,
        config: CorpusConfig,
        source: Any,
        pacer: RequestPacer,
        main_state: StateStore,
        holdout_state: StateStore,
    ) -> None:
        self.config = config
        self.source = source
        self.pacer = pacer
        self.main_state = main_state
        self.holdout_state = holdout_state
        self.writer = ArtifactWriter(config, main_state, holdout_state)
        prior = main_state.get_metadata("requests_actual_total")
        self.request_count_before_process = int(prior or 0)
        self.pacer.max_requests = min(
            self.pacer.max_requests,
            max(0, self.config.max_requests - self.request_count_before_process),
        )
        self.pacer.on_event = self._on_request_event
        self.request_budget_projected: int | None = None
        self.crosscheck: CrosscheckTracker | None = None
        self.stop_reason: str | None = None
        self.started_epoch = self._load_or_create_started_epoch()
        saved_counters = main_state.get_metadata("counters")
        self.counters: Counter[str] = Counter(saved_counters or {})
        self.membership_snapshot_failures = int(
            main_state.get_metadata("membership_snapshot_failures") or 0
        )

    @property
    def requests_actual(self) -> int:
        return self.request_count_before_process + self.pacer.request_count

    def _load_or_create_started_epoch(self) -> float:
        saved = self.main_state.get_metadata("started_epoch")
        if saved is not None:
            return float(saved)
        value = time.time()
        self.main_state.set_metadata("started_epoch", value)
        return value

    def _on_request_event(self, _: RequestEvent) -> None:
        # Persist every internal pykrx request count (including auth refreshes)
        # so a resumed process cannot reset the global MAX_REQUESTS budget.
        self.main_state.set_metadata("requests_actual_total", self.requests_actual)

    def _elapsed_hours(self) -> float:
        return (time.time() - self.started_epoch) / 3600

    def _check_wall_clock(self) -> bool:
        if self._elapsed_hours() >= self.config.max_wall_clock_hours:
            self.stop_reason = "max_wall_clock_hours_reached"
            return False
        return True

    def _state_for_scope(self, scope: Literal["main", "holdout"]) -> StateStore:
        return self.main_state if scope == "main" else self.holdout_state

    def _scope_for_session(self, session: str) -> Literal["main", "holdout"]:
        return "holdout" if self.config.is_holdout_session(session) else "main"

    def _append_progress(self, stage: str, completed: int, total: int) -> None:
        path = self.config.progress_file_path
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(ZoneInfo(self.config.timezone)).isoformat(
            timespec="seconds"
        )
        checkpoint = self.writer.paths.main_partial / "checkpoint.json"
        line = (
            f"{timestamp} | {stage} | {completed}/{total} | "
            f"requests={self.requests_actual} | elapsed_hours={self._elapsed_hours():.4f} | "
            f"checkpoint={checkpoint}\n"
        )
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()

    def _checkpoint(self, stage: str, completed: int, total: int) -> None:
        self.main_state.set_metadata("counters", dict(self.counters))
        self.main_state.set_metadata(
            "membership_snapshot_failures", self.membership_snapshot_failures
        )
        payload = {
            "corpus_id": self.config.corpus_id,
            "run_id": self.config.run_id,
            "stage": stage,
            "completed": completed,
            "total": total,
            "requests_actual": self.requests_actual,
            "elapsed_hours": self._elapsed_hours(),
            "main_membership_completed": self.main_state.completed_membership_count(),
            "holdout_membership_completed": self.holdout_state.completed_membership_count(),
            "main_tickers_completed": self.main_state.completed_ticker_count(),
            "holdout_tickers_completed": self.holdout_state.completed_ticker_count(),
            "last_checkpoint_timezone": self.config.timezone,
        }
        self.writer.write_mutable_json("main", "checkpoint.json", payload)
        self._append_progress(stage, completed, total)

    def _ensure_main_config(self) -> None:
        target = self.writer.paths.main_partial / "config.json"
        if not target.exists():
            self.writer.write_json("main", "config.json", self.config.public_dict())

    def _require_parquet_runtime(self) -> str:
        try:
            version = importlib.metadata.version("pyarrow")
        except importlib.metadata.PackageNotFoundError as exc:
            raise PreconditionBlocked(
                "pyarrow parquet runtime is unavailable in VENV_DIR"
            ) from exc
        # The exact build is already locked by the repository.  We record the
        # actual version but do not modify pyproject.toml or uv.lock.
        if version != "25.0.0":
            raise PreconditionBlocked(
                "pyarrow version does not match the locked runtime"
            )
        return version

    def _load_crosscheck(self) -> CrosscheckTracker:
        return CrosscheckTracker.from_csv(
            Path(self.config.crosscheck_file), self.config.crosscheck_file_sha256
        )

    def run(self) -> RunResult:
        """Run until a truthful terminal verdict, then promote immutable output."""
        try:
            self.writer.initialize()
            self._ensure_main_config()
            parquet_version = self._require_parquet_runtime()
            self.main_state.set_metadata(
                "parquet_runtime", {"pyarrow": parquet_version}
            )
            self.crosscheck = self._load_crosscheck()
            self.main_state.set_metadata("crosscheck_sha256_verified", True)
            self._checkpoint("preflight_local", 0, 0)
        except (
            PreconditionBlocked,
            ExistingSnapshotError,
            ArtifactSizeLimitExceeded,
        ) as exc:
            self.stop_reason = str(exc)
            return self._finish("BLOCKED_PRECONDITION")

        if self.request_count_before_process >= self.config.max_requests:
            self.main_state.record_error(
                "preflight",
                "request_budget",
                "max_requests_already_exhausted",
                f"requests_actual={self.request_count_before_process}",
            )
            self.stop_reason = "max_requests_already_exhausted"
            return self._finish("BLOCKED_PRECONDITION")

        try:
            with self.source.runtime():
                return self._run_source_phases()
        except Exception as exc:
            # Source adapters redact their own failures.  A final defensive
            # pass keeps an unexpected exception out of any persisted report.
            redactor = getattr(self.source, "redactor", None)
            detail = (
                redactor.redact(exc) if redactor is not None else type(exc).__name__
            )
            self.main_state.record_error(
                "source_runtime", "runtime", "runtime_failure", detail
            )
            self.stop_reason = "source_runtime_failure"
            return self._finish("BLOCKED_PRECONDITION")

    def _run_source_phases(self) -> RunResult:
        redactor = getattr(self.source, "redactor", None)
        if redactor is not None:
            self.writer.set_forbidden_text_values(redactor.secrets)
        try:
            sessions, lifecycle_tickers = self._load_or_fetch_preflight()
        except Exception as exc:
            self._record_preflight_failure(exc)
            return self._finish("BLOCKED_PRECONDITION")

        projection = RequestProjection(
            requests_already_observed=self.requests_actual,
            session_count=len(sessions),
            markets_count=len(self.config.markets),
            lifecycle_master_upper_bound=len(lifecycle_tickers),
            max_wall_clock_hours=self.config.max_wall_clock_hours,
        )
        self.request_budget_projected = projection.total
        self.main_state.set_metadata("request_budget_projected", projection.total)
        self.main_state.set_metadata(
            "request_budget_components",
            {
                "requests_already_observed": projection.requests_already_observed,
                "membership_requests": projection.membership_requests,
                "ohlcv_requests": projection.ohlcv_requests,
                "maximum_session_refresh_requests": projection.maximum_session_refresh_requests,
            },
        )
        expected_wait_hours = (
            projection.total * self.config.min_request_interval_sec / 3600
        )
        if projection.total > self.config.max_requests:
            self.main_state.record_error(
                "preflight",
                "request_budget",
                "max_requests_exceeded",
                f"projected={projection.total}; max={self.config.max_requests}",
            )
            self.stop_reason = "projected_request_budget_exceeds_max_requests"
            self._checkpoint("blocked_request_budget", 0, 0)
            return self._finish("BLOCKED_PRECONDITION")
        if expected_wait_hours > self.config.max_wall_clock_hours:
            self.main_state.record_error(
                "preflight",
                "request_budget",
                "minimum_wait_exceeds_wall_clock",
                f"minimum_wait_hours={expected_wait_hours:.6f}",
            )
            self.stop_reason = "projected_minimum_wait_exceeds_max_wall_clock_hours"
            self._checkpoint("blocked_wall_clock_budget", 0, 0)
            return self._finish("BLOCKED_PRECONDITION")

        membership_verdict = self._collect_membership(sessions)
        if membership_verdict is not None:
            return self._finish(membership_verdict)

        actual_tickers = set(self.main_state.all_tickers()) | set(
            self.holdout_state.all_tickers()
        )
        unknown_lifecycle_tickers = sorted(actual_tickers - lifecycle_tickers)
        if unknown_lifecycle_tickers:
            self.main_state.record_error(
                "preflight",
                "lifecycle_master_bound",
                "membership_ticker_outside_budget_master",
                f"count={len(unknown_lifecycle_tickers)}",
            )
            self.stop_reason = "lifecycle_master_did_not_bound_positive_membership"
            self._checkpoint("blocked_unbounded_membership", 0, len(actual_tickers))
            return self._finish("BLOCKED_PRECONDITION")

        post_membership_projection = RequestProjection(
            requests_already_observed=self.requests_actual,
            session_count=0,
            markets_count=0,
            lifecycle_master_upper_bound=len(actual_tickers),
            max_wall_clock_hours=self.config.max_wall_clock_hours,
        )
        if post_membership_projection.total > self.config.max_requests:
            self.main_state.record_error(
                "preflight",
                "post_membership_budget",
                "max_requests_exceeded",
                f"projected={post_membership_projection.total}; max={self.config.max_requests}",
            )
            self.stop_reason = "post_membership_request_budget_exceeds_max_requests"
            self._checkpoint("blocked_post_membership_budget", 0, len(actual_tickers))
            return self._finish("BLOCKED_PRECONDITION")

        ohlcv_verdict = self._collect_ohlcv(tuple(sorted(actual_tickers)))
        if ohlcv_verdict is not None:
            return self._finish(ohlcv_verdict)
        return self._finish(self._integrity_verdict())

    def _load_or_fetch_preflight(self) -> tuple[tuple[str, ...], frozenset[str]]:
        path = self.writer.paths.main_partial / "preflight.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            sessions = tuple(str(value) for value in payload["sessions"])
            tickers = frozenset(
                str(value) for value in payload["lifecycle_master_tickers"]
            )
            self._validate_sessions(sessions)
            if not tickers:
                raise PreconditionBlocked("saved lifecycle-master bound is empty")
            self.main_state.set_metadata("sessions_covered", len(sessions))
            self.main_state.set_metadata("lifecycle_master_upper_bound", len(tickers))
            return sessions, tickers

        lifecycle = self.source.lifecycle_master_bound()
        sessions = self.source.session_calendar()
        self._validate_sessions(sessions)
        if not lifecycle.tickers:
            raise PreconditionBlocked("KRX lifecycle-master budget bound is empty")
        payload = {
            "corpus_id": self.config.corpus_id,
            "session_calendar": self.config.session_calendar,
            "sessions": list(sessions),
            "session_count": len(sessions),
            "lifecycle_master_tickers": sorted(lifecycle.tickers),
            "lifecycle_master_upper_bound": len(lifecycle.tickers),
            "lifecycle_master_listed_count": lifecycle.listed_count,
            "lifecycle_master_delisted_count": lifecycle.delisted_count,
            "purpose": "REQUEST_BUDGET_UPPER_BOUND_ONLY_NOT_MEMBERSHIP_CLASSIFICATION",
        }
        self.writer.write_json("main", "preflight.json", payload)
        self.main_state.set_metadata("sessions_covered", len(sessions))
        self.main_state.set_metadata(
            "lifecycle_master_upper_bound", len(lifecycle.tickers)
        )
        self._checkpoint(
            "preflight_source", 0, len(sessions) * len(self.config.markets)
        )
        return sessions, lifecycle.tickers

    def _validate_sessions(self, sessions: tuple[str, ...]) -> None:
        if not sessions:
            raise PreconditionBlocked("XKRX session calendar returned no sessions")
        if tuple(sorted(sessions)) != sessions or len(set(sessions)) != len(sessions):
            raise PreconditionBlocked(
                "XKRX session calendar is not strictly ascending and unique"
            )
        if (
            sessions[0] < self.config.start_date
            or sessions[-1] > self.config.cutoff_session
        ):
            raise PreconditionBlocked(
                "XKRX session calendar falls outside signed window"
            )

    def _record_preflight_failure(self, exc: Exception) -> None:
        redactor = getattr(self.source, "redactor", None)
        detail = redactor.redact(exc) if redactor is not None else type(exc).__name__
        self.main_state.record_error("preflight", "source", "preflight_failure", detail)
        self.stop_reason = "preflight_source_failure"

    def _collect_membership(self, sessions: tuple[str, ...]) -> TerminalVerdict | None:
        total = len(sessions) * len(self.config.markets)
        processed = (
            self.main_state.completed_membership_count()
            + self.holdout_state.completed_membership_count()
        )
        for session in sessions:
            for market in self.config.markets:
                scope = self._scope_for_session(session)
                state = self._state_for_scope(scope)
                if state.membership_completed(session, market):
                    continue
                if not self._check_wall_clock():
                    self._checkpoint("membership_wall_clock_stop", processed, total)
                    return "BUILT_WITH_GAPS"
                try:
                    tickers = self.source.membership(session, market)
                except Exception as exc:
                    verdict = self._handle_membership_exception(
                        state, session, market, exc, processed, total
                    )
                    if verdict is not None:
                        return verdict
                    continue

                unique_tickers = tuple(sorted(set(tickers)))
                if not tickers:
                    self._record_membership_failure(
                        state,
                        session,
                        market,
                        "empty_membership_on_xkrx_session",
                        "positive membership snapshot was empty; absence was not treated as delisting",
                    )
                    self._checkpoint("membership_empty", processed, total)
                    continue
                if len(unique_tickers) != len(tickers):
                    self._record_membership_failure(
                        state,
                        session,
                        market,
                        "duplicate_ticker_in_membership_snapshot",
                        "duplicate source membership was not silently deduplicated",
                    )
                    self.counters["source_membership_duplicate_rows"] += len(
                        tickers
                    ) - len(unique_tickers)
                    self._checkpoint("membership_duplicate", processed, total)
                    continue

                relative_path = f"membership/market={market}/year={session[:4]}/session={session}.parquet"
                try:
                    if state.file_record(relative_path) is None:
                        self._write_membership_parquet(
                            scope, relative_path, session, market, unique_tickers
                        )
                    state.add_membership(session, market, unique_tickers)
                except ArtifactSizeLimitExceeded:
                    self.stop_reason = "max_artifact_gib_reached_during_membership"
                    self._checkpoint("membership_artifact_size_stop", processed, total)
                    return "BUILT_WITH_GAPS"
                except Exception as exc:
                    self._record_membership_failure(
                        state,
                        session,
                        market,
                        "membership_artifact_write_failure",
                        type(exc).__name__,
                    )
                    self._checkpoint("membership_write_failure", processed, total)
                    continue
                processed += 1
                self._checkpoint("membership", processed, total)
        return None

    def _write_membership_parquet(
        self,
        scope: Literal["main", "holdout"],
        relative_path: str,
        session: str,
        market: str,
        tickers: tuple[str, ...],
    ) -> None:
        self._ensure_holdout_generation_started(scope)
        schema = self._schemas()["membership"]
        self.writer.write_parquet(
            scope,
            relative_path,
            [
                {
                    "session": session,
                    "market": market,
                    "ticker": ticker,
                    "source_product": self.config.source_product,
                }
                for ticker in tickers
            ],
            schema,
        )

    def _record_membership_failure(
        self,
        state: StateStore,
        session: str,
        market: str,
        reason: str,
        detail: str,
    ) -> None:
        state.record_error("membership", f"{session}:{market}", reason, detail)
        self.membership_snapshot_failures += 1

    def _handle_membership_exception(
        self,
        state: StateStore,
        session: str,
        market: str,
        exc: Exception,
        processed: int,
        total: int,
    ) -> TerminalVerdict | None:
        exception_name = type(exc).__name__
        redactor = getattr(self.source, "redactor", None)
        detail = redactor.redact(exc) if redactor is not None else exception_name
        state.record_error("membership", f"{session}:{market}", exception_name, detail)
        self.membership_snapshot_failures += 1
        self._checkpoint("membership_source_failure", processed, total)
        if exception_name == "RequestBudgetExceeded":
            self.stop_reason = "max_requests_reached_during_membership"
            return "BLOCKED_PRECONDITION"
        if exception_name == "SourceBlockedSignal":
            self.stop_reason = "source_blocked_or_rate_limited_during_membership"
            return "BUILT_WITH_GAPS"
        return None

    def _collect_ohlcv(self, tickers: tuple[str, ...]) -> TerminalVerdict | None:
        total = len(tickers)
        processed = sum(
            1
            for ticker in tickers
            if self._ticker_finished_for_all_present_scopes(ticker)
        )
        for ticker in tickers:
            if self._ticker_finished_for_all_present_scopes(ticker):
                continue
            if not self._check_wall_clock():
                self._checkpoint("ohlcv_wall_clock_stop", processed, total)
                return "BUILT_WITH_GAPS"
            try:
                dataframe = self.source.adjusted_ohlcv(ticker)
            except Exception as exc:
                verdict = self._handle_ohlcv_exception(ticker, exc, processed, total)
                if verdict is not None:
                    return verdict
                processed += 1
                self._checkpoint("ohlcv_source_failure", processed, total)
                continue

            if getattr(dataframe, "empty", True):
                self._mark_ticker_gap(
                    ticker, "source_empty", "pykrx returned no adjusted OHLCV rows"
                )
                self._mark_ticker_finished(ticker)
                processed += 1
                self._checkpoint("ohlcv_empty", processed, total)
                continue

            try:
                self._process_ticker_dataframe(ticker, dataframe)
            except ArtifactSizeLimitExceeded:
                self.stop_reason = "max_artifact_gib_reached_during_ohlcv"
                self._checkpoint("ohlcv_artifact_size_stop", processed, total)
                return "BUILT_WITH_GAPS"
            except ExistingSnapshotError as exc:
                self._mark_ticker_gap(
                    ticker,
                    "artifact_collision",
                    type(exc).__name__,
                )
                self.stop_reason = "existing_artifact_file_collision"
                self._checkpoint("ohlcv_artifact_collision", processed, total)
                return "BLOCKED_PRECONDITION"
            except Exception as exc:
                redactor = getattr(self.source, "redactor", None)
                detail = (
                    redactor.redact(exc) if redactor is not None else type(exc).__name__
                )
                self._mark_ticker_gap(ticker, "normalization_failure", detail)
                self._record_ticker_error(ticker, "normalization_failure", detail)
            self._mark_ticker_finished(ticker)
            processed += 1
            self._checkpoint("ohlcv", processed, total)
        return None

    def _ticker_finished_for_all_present_scopes(self, ticker: str) -> bool:
        present_main = bool(self.main_state.ticker_membership(ticker))
        present_holdout = bool(self.holdout_state.ticker_membership(ticker))
        return (not present_main or self.main_state.ticker_completed(ticker)) and (
            not present_holdout or self.holdout_state.ticker_completed(ticker)
        )

    def _mark_ticker_finished(self, ticker: str) -> None:
        if self.main_state.ticker_membership(ticker):
            self.main_state.mark_ticker_completed(ticker)
        if self.holdout_state.ticker_membership(ticker):
            self.holdout_state.mark_ticker_completed(ticker)

    def _record_ticker_error(self, ticker: str, reason: str, detail: str) -> None:
        if self.main_state.ticker_membership(ticker):
            self.main_state.record_error("ohlcv", ticker, reason, detail)
        if self.holdout_state.ticker_membership(ticker):
            self.holdout_state.record_error("ohlcv", ticker, reason, detail)

    def _mark_ticker_gap(self, ticker: str, reason: str, detail: str) -> None:
        if self.main_state.ticker_membership(ticker):
            self.main_state.mark_gap_for_membership(ticker, reason, detail)
        if self.holdout_state.ticker_membership(ticker):
            self.holdout_state.mark_gap_for_membership(ticker, reason, detail)

    def _handle_ohlcv_exception(
        self,
        ticker: str,
        exc: Exception,
        processed: int,
        total: int,
    ) -> TerminalVerdict | None:
        exception_name = type(exc).__name__
        redactor = getattr(self.source, "redactor", None)
        detail = redactor.redact(exc) if redactor is not None else exception_name
        self._mark_ticker_gap(ticker, exception_name, detail)
        self._record_ticker_error(ticker, exception_name, detail)
        if exception_name == "RequestBudgetExceeded":
            self.stop_reason = "max_requests_reached_during_ohlcv"
            self._checkpoint("ohlcv_request_budget_stop", processed, total)
            return "BLOCKED_PRECONDITION"
        if exception_name == "SourceBlockedSignal":
            self.stop_reason = "source_blocked_or_rate_limited_during_ohlcv"
            self._checkpoint("ohlcv_blocked_stop", processed, total)
            return "BUILT_WITH_GAPS"
        return None

    def _process_ticker_dataframe(self, ticker: str, dataframe: Any) -> None:
        destinations = self._membership_destinations(ticker)
        source_rows = self._normalize_source_dataframe(ticker, dataframe)
        grouped_rows: dict[tuple[str, str, int], list[dict[str, object]]] = defaultdict(
            list
        )
        grouped_presence: dict[tuple[str, str, int], list[tuple[str, str, str]]] = (
            defaultdict(list)
        )

        for session, candidates in source_rows.items():
            if len(candidates) != 1:
                self.counters["source_duplicate_rows"] += len(candidates)
                for raw_row in candidates:
                    self._record_source_anomaly(
                        ticker,
                        session,
                        "source_duplicate_row",
                        raw_row,
                    )
                for scope, market in destinations.get(session, []):
                    self._state_for_scope(scope).mark_gap(
                        session,
                        market,
                        ticker,
                        "source_duplicate_row",
                        "more than one pykrx row shared the session",
                    )
                continue

            source_row = candidates[0]
            source_row_for_compare = {
                field_name: source_row.get(field_name)
                for field_name in ("open", "high", "low", "close", "volume", "value")
            }
            if self.crosscheck is not None:
                self.crosscheck.compare(session, ticker, source_row_for_compare)

            member_destinations = destinations.get(session, [])
            if not member_destinations:
                self._record_source_anomaly(
                    ticker,
                    session,
                    "source_bar_without_positive_membership",
                    source_row,
                )
                continue
            if source_row.get("normalization_error"):
                self.counters["normalization_errors"] += 1
                for scope, market in member_destinations:
                    self._state_for_scope(scope).mark_gap(
                        session,
                        market,
                        ticker,
                        "source_row_normalization_failure",
                        str(source_row["normalization_error"]),
                    )
                self._record_source_anomaly(
                    ticker,
                    session,
                    "source_row_normalization_failure",
                    source_row,
                )
                continue
            if not _ohlc_is_valid(source_row):
                self.counters["ohlc_invariant_violations"] += 1
                for scope, market in member_destinations:
                    self._state_for_scope(scope).mark_gap(
                        session,
                        market,
                        ticker,
                        "ohlc_invariant_violation",
                        "source row retained as anomaly and excluded from valid corpus rows",
                    )
                self._record_source_anomaly(
                    ticker,
                    session,
                    "ohlc_invariant_violation",
                    source_row,
                )
                continue
            if int(source_row["volume"]) < 0 or (
                source_row["value"] is not None and int(source_row["value"]) < 0
            ):
                self.counters["negative_volume_or_value"] += 1
                for scope, market in member_destinations:
                    self._state_for_scope(scope).mark_gap(
                        session,
                        market,
                        ticker,
                        "negative_volume_or_value",
                        "source row retained as anomaly and excluded from valid corpus rows",
                    )
                self._record_source_anomaly(
                    ticker,
                    session,
                    "negative_volume_or_value",
                    source_row,
                )
                continue

            for scope, market in member_destinations:
                row = {
                    "session": session,
                    "market": market,
                    "ticker": ticker,
                    "open": source_row["open"],
                    "high": source_row["high"],
                    "low": source_row["low"],
                    "close": source_row["close"],
                    "volume": source_row["volume"],
                    "value": source_row["value"],
                    "price_mode": self.config.price_mode,
                    "source_product": self.config.source_product,
                }
                key = (scope, market, int(session[:4]))
                grouped_rows[key].append(row)
                grouped_presence[key].append((session, market, ticker))

        # Every positive membership observation lacking a valid source bar is
        # materialized later into gaps/ rather than forward-filled or dropped.
        valid_membership_keys = {
            (session, market)
            for (_scope, market, _year), rows in grouped_rows.items()
            for session in (str(row["session"]) for row in rows)
        }
        for session, member_destinations in destinations.items():
            for scope, market in member_destinations:
                if (session, market) not in valid_membership_keys:
                    self._state_for_scope(scope).mark_gap(
                        session,
                        market,
                        ticker,
                        "bar_missing_from_source",
                        "no valid pykrx adjusted OHLCV row matched positive membership",
                    )

        for (scope, market, year), rows in sorted(grouped_rows.items()):
            relative_path = (
                f"dataset/market={market}/year={year}/ticker={ticker}.parquet"
            )
            state = self._state_for_scope(scope)
            if state.file_record(relative_path) is None:
                self._ensure_holdout_generation_started(scope)
                self.writer.write_parquet(
                    scope, relative_path, rows, self._schemas()["bar"]
                )
            for session, row_market, row_ticker in grouped_presence[
                (scope, market, year)
            ]:
                state.add_bar_presence(session, row_market, row_ticker)
            state.commit()
        self.main_state.commit()
        self.holdout_state.commit()

    def _membership_destinations(
        self, ticker: str
    ) -> dict[str, list[tuple[Literal["main", "holdout"], str]]]:
        result: dict[str, list[tuple[Literal["main", "holdout"], str]]] = defaultdict(
            list
        )
        for session, market in self.main_state.ticker_membership(ticker):
            result[session].append(("main", market))
        for session, market in self.holdout_state.ticker_membership(ticker):
            result[session].append(("holdout", market))
        return result

    def _normalize_source_dataframe(
        self, ticker: str, dataframe: Any
    ) -> dict[str, list[dict[str, object]]]:
        rows: dict[str, list[dict[str, object]]] = defaultdict(list)
        required_columns = {"시가", "고가", "저가", "종가", "거래량"}
        columns = {str(column) for column in dataframe.columns}
        if not required_columns.issubset(columns):
            raise ValueError("pykrx adjusted OHLCV schema omitted a required column")
        for index, values in dataframe.iterrows():
            session = index.strftime("%Y-%m-%d")
            if not (self.config.start_date <= session <= self.config.cutoff_session):
                self._record_source_anomaly(
                    ticker,
                    session,
                    "source_row_outside_signed_window",
                    {"session": session},
                )
                continue
            source_row: dict[str, object] = {"session": session}
            try:
                source_row.update(
                    {
                        "open": _safe_int(values["시가"], "open"),
                        "high": _safe_int(values["고가"], "high"),
                        "low": _safe_int(values["저가"], "low"),
                        "close": _safe_int(values["종가"], "close"),
                        "volume": _safe_int(values["거래량"], "volume"),
                        # pykrx's adjusted Naver path does not provide value.
                        # It remains nullable rather than invented from price × volume.
                        "value": None,
                    }
                )
            except ValueError as exc:
                source_row["normalization_error"] = str(exc)
            rows[session].append(source_row)
        return rows

    def _record_source_anomaly(
        self,
        ticker: str,
        session: str,
        kind: str,
        detail: dict[str, object],
    ) -> None:
        scope = self._scope_for_session(session)
        self._state_for_scope(scope).record_source_anomaly(
            kind,
            session,
            "UNMAPPED",
            ticker,
            detail,
        )

    def _schemas(self) -> dict[str, Any]:
        import pyarrow as pa

        return {
            "membership": pa.schema(
                [
                    pa.field("session", pa.string(), nullable=False),
                    pa.field("market", pa.string(), nullable=False),
                    pa.field("ticker", pa.string(), nullable=False),
                    pa.field("source_product", pa.string(), nullable=False),
                ]
            ),
            "bar": pa.schema(
                [
                    pa.field("session", pa.string(), nullable=False),
                    pa.field("market", pa.string(), nullable=False),
                    pa.field("ticker", pa.string(), nullable=False),
                    pa.field("open", pa.int64(), nullable=False),
                    pa.field("high", pa.int64(), nullable=False),
                    pa.field("low", pa.int64(), nullable=False),
                    pa.field("close", pa.int64(), nullable=False),
                    pa.field("volume", pa.int64(), nullable=False),
                    pa.field("value", pa.int64(), nullable=True),
                    pa.field("price_mode", pa.string(), nullable=False),
                    pa.field("source_product", pa.string(), nullable=False),
                ]
            ),
            "gap": pa.schema(
                [
                    pa.field("session", pa.string(), nullable=False),
                    pa.field("market", pa.string(), nullable=False),
                    pa.field("ticker", pa.string(), nullable=False),
                    pa.field("reason", pa.string(), nullable=False),
                    pa.field("detail", pa.string(), nullable=False),
                ]
            ),
        }

    def _ensure_holdout_generation_started(
        self, scope: Literal["main", "holdout"]
    ) -> None:
        if scope != "holdout":
            return
        if self.main_state.get_metadata("holdout_generation_started"):
            return
        path = Path(self.config.holdout_access_log)
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(ZoneInfo(self.config.timezone)).isoformat(
            timespec="seconds"
        )
        with path.open("a", encoding="utf-8") as stream:
            stream.write(
                f"{timestamp} | action=WRITE_GENERATION_BEGIN | run_id={self.config.run_id} | "
                "final_holdout_data_reads=0\n"
            )
            stream.flush()
        self.main_state.set_metadata("holdout_generation_started", True)

    def _write_final_diagnostics(self) -> dict[str, object]:
        """Write validation artifacts before checksum/manifest promotion."""
        self.crosscheck = self.crosscheck or CrosscheckTracker(samples={})
        self.crosscheck.add_unmatched()
        mismatches_by_scope: dict[str, list[dict[str, object]]] = defaultdict(list)
        for mismatch in self.crosscheck.mismatches:
            scope = self._scope_for_session(str(mismatch["session"]))
            mismatches_by_scope[scope].append(mismatch)

        all_years = _years_inclusive(self.config.start_date, self.config.cutoff_session)
        diagnostics: dict[str, object] = {
            "crosscheck_mismatches": len(self.crosscheck.mismatches),
            "coverage": [],
            "gap_reason_counts": {},
        }
        for scope, state in (
            ("main", self.main_state),
            ("holdout", self.holdout_state),
        ):
            has_scope_data = bool(state.files()) or state.membership_count() > 0
            if scope == "holdout" and not has_scope_data:
                continue
            if scope == "holdout":
                self._ensure_holdout_generation_started("holdout")
            errors = list(state.errors())
            anomalies = list(state.source_anomalies())
            self.writer.write_bytes(scope, "errors.jsonl", _json_lines(errors))
            self.writer.write_bytes(
                scope, "source-anomalies.jsonl", _json_lines(anomalies)
            )

            coverage_rows = state.coverage(self.config.markets, all_years)
            scope_coverage = [self._coverage_dict(row) for row in coverage_rows]
            self.writer.write_json(scope, "coverage.json", scope_coverage)
            diagnostics["coverage"].extend(scope_coverage)
            diagnostics["gap_reason_counts"][scope] = dict(state.gap_reason_counts())

            for coverage in coverage_rows:
                if coverage.membership_rows == 0:
                    continue
                gaps = list(state.missing_rows(coverage.market, coverage.year))
                if not gaps:
                    continue
                relative_path = f"gaps/market={coverage.market}/year={coverage.year}/missing.parquet"
                self.writer.write_parquet(
                    scope, relative_path, gaps, self._schemas()["gap"]
                )

            crosscheck_rows = mismatches_by_scope.get(scope, [])
            self.writer.write_json(scope, "crosscheck/mismatches.json", crosscheck_rows)

        # There is no source-product evidence for common-share classification.
        # The derived view is therefore intentionally empty rather than using
        # suffix/name/current-list heuristics or silently treating UNKNOWN as
        # common stock.
        self.writer.write_json(
            "main",
            "derived/common_stock_proven/manifest.json",
            {
                "row_count": 0,
                "classification_status": "NO_PROVEN_COMMON_STOCK_ROWS",
                "reason": "pykrx membership evidence does not prove common-stock status",
                "heuristic_classification_used": False,
                "unknown_auto_excluded": False,
            },
        )
        return diagnostics

    @staticmethod
    def _coverage_dict(row: CoverageRow) -> dict[str, object]:
        return {
            "market": row.market,
            "year": row.year,
            "membership_rows": row.membership_rows,
            "bar_rows": row.bar_rows,
            "coverage": row.ratio,
        }

    def _combined_coverage(self) -> tuple[CoverageRow, ...]:
        all_years = _years_inclusive(self.config.start_date, self.config.cutoff_session)
        combined: list[CoverageRow] = []
        main_rows = {
            (row.market, row.year): row
            for row in self.main_state.coverage(self.config.markets, all_years)
        }
        holdout_rows = {
            (row.market, row.year): row
            for row in self.holdout_state.coverage(self.config.markets, all_years)
        }
        for market in self.config.markets:
            for year in all_years:
                main = main_rows[(market, year)]
                holdout = holdout_rows[(market, year)]
                combined.append(
                    CoverageRow(
                        market=market,
                        year=year,
                        membership_rows=main.membership_rows + holdout.membership_rows,
                        bar_rows=main.bar_rows + holdout.bar_rows,
                    )
                )
        return tuple(combined)

    def _integrity_verdict(self) -> TerminalVerdict:
        coverage = self._combined_coverage()
        coverage_ready = bool(coverage) and all(
            row.ratio is not None
            and row.ratio >= self.config.min_market_year_membership_bar_coverage
            for row in coverage
        )
        no_source_integrity_violations = (
            self.counters["source_duplicate_rows"] == 0
            and self.counters["ohlc_invariant_violations"] == 0
            and self.counters["negative_volume_or_value"] == 0
        )
        if (
            coverage_ready
            and no_source_integrity_violations
            and self.membership_snapshot_failures == 0
            and self.main_state.explicit_gap_count()
            + self.holdout_state.explicit_gap_count()
            == 0
        ):
            return "READY_FOR_RESEARCH"
        return "BUILT_WITH_GAPS"

    def _finish(self, requested_verdict: TerminalVerdict) -> RunResult:
        """Finalize only immutable artifacts; errors degrade, never fabricate READY."""
        verdict = requested_verdict
        try:
            diagnostics = self._write_final_diagnostics()
            combined_coverage = self._combined_coverage()
            combined_gap_count = (
                self.main_state.explicit_gap_count()
                + self.holdout_state.explicit_gap_count()
            )
            minimum_coverage = min(
                (row.ratio for row in combined_coverage if row.ratio is not None),
                default=0.0,
            )
            holdout_data_written = any(
                record.relative_path.startswith(("membership/", "dataset/", "gaps/"))
                for record in self.holdout_state.files()
            )
            coverage_bar_met = bool(combined_coverage) and all(
                row.ratio is not None
                and row.ratio >= self.config.min_market_year_membership_bar_coverage
                for row in combined_coverage
            )
            raw_tickers = set(self.main_state.all_tickers()) | set(
                self.holdout_state.all_tickers()
            )
            metrics = {
                "sessions_covered": int(
                    self.main_state.get_metadata("sessions_covered") or 0
                ),
                "tickers_raw": len(raw_tickers),
                "tickers_in_derived_view": 0,
                "membership_rows": self.main_state.membership_count()
                + self.holdout_state.membership_count(),
                "bar_rows": self.main_state.bar_count()
                + self.holdout_state.bar_count(),
                "duplicate_rows": 0,
                "source_duplicate_rows": self.counters["source_duplicate_rows"],
                "ohlc_invariant_violations": self.counters["ohlc_invariant_violations"],
                "negative_volume_or_value": self.counters["negative_volume_or_value"],
                "coverage_bar_met_0995": coverage_bar_met,
                "delisted_051170_rows": self.main_state.ticker_bar_count("051170")
                + self.holdout_state.ticker_bar_count("051170"),
                "secrets_or_account_id_in_artifacts": False,
            }
            manifest_common = {
                "corpus_id": self.config.corpus_id,
                "terminal_verdict": verdict,
                "source_product": self.config.source_product,
                "pykrx_version": self.config.pykrx_version,
                "auto_trader_base_commit": self.config.auto_trader_base_commit,
                "request_budget_projected": self.request_budget_projected,
                "requests_actual": self.requests_actual,
                "source_fallback_used": False,
                "forward_fill_used": False,
                "crosscheck_file_sha256_verified": self.crosscheck is not None,
                "crosscheck_mismatches": diagnostics["crosscheck_mismatches"],
                "coverage": [self._coverage_dict(row) for row in combined_coverage],
                "minimum_market_year_coverage": minimum_coverage,
                "explicit_gap_count": combined_gap_count,
                "field_scopes": {
                    "metrics": "main_plus_holdout",
                    "coverage": "main_plus_holdout",
                    "minimum_market_year_coverage": "main_plus_holdout",
                    "explicit_gap_count": "main_plus_holdout",
                    "gap_reason_counts": "main_plus_holdout",
                    "crosscheck_mismatches": "main_plus_holdout",
                },
                "gap_reason_counts": {
                    "main": dict(self.main_state.gap_reason_counts()),
                    "holdout": dict(self.holdout_state.gap_reason_counts()),
                },
                "counters": dict(self.counters),
                "membership_snapshot_failures": self.membership_snapshot_failures,
                "derived_common_stock_rows": 0,
                "metrics": metrics,
                "adjusted_ohlcv_value": {
                    "availability": "NULL",
                    "reason": (
                        "pykrx adjusted OHLCV source does not provide turnover; "
                        "the collector never synthesizes value from price_times_volume"
                    ),
                    "consumer_limitation": (
                        "Do not use this corpus for value/turnover/liquidity filters"
                    ),
                },
                "holdout_custody": {
                    "final_data_read_policy": (
                        "collector does not read final holdout data after generation"
                    ),
                    "audit_measurement": (
                        "not emitted: no audited holdout-read wrapper is implemented"
                    ),
                },
                "stop_reason": self.stop_reason,
            }

            holdout_manifest_hash: str | None = None
            if holdout_data_written:
                self._ensure_holdout_generation_started("holdout")
                holdout_checksums = self.writer.checksum_manifest("holdout")
                holdout_manifest = dict(manifest_common)
                holdout_manifest.update(
                    {
                        "scope": "holdout",
                        "checksums_sha256": holdout_checksums.sha256,
                        "files_list_location": "checksums.sha256",
                    }
                )
                holdout_record = self.writer.write_json(
                    "holdout", "manifest.json", holdout_manifest
                )
                holdout_manifest_hash = holdout_record.sha256
                access_path = Path(self.config.holdout_access_log)
                timestamp = datetime.now(ZoneInfo(self.config.timezone)).isoformat(
                    timespec="seconds"
                )
                with access_path.open("a", encoding="utf-8") as stream:
                    stream.write(
                        f"{timestamp} | action=WRITE_GENERATION_COMPLETE | "
                        f"run_id={self.config.run_id} | final_holdout_data_reads=0\n"
                    )
                    stream.flush()

            main_checksums = self.writer.checksum_manifest("main")
            main_manifest = dict(manifest_common)
            main_manifest.update(
                {
                    "scope": "main",
                    "checksums_sha256": main_checksums.sha256,
                    "files_list_location": "checksums.sha256",
                    "holdout_manifest_sha256": holdout_manifest_hash,
                }
            )
            self.writer.write_json("main", "manifest.json", main_manifest)
            if holdout_data_written:
                self.writer.promote("holdout")
            self.writer.promote("main")
        except Exception as exc:
            verdict = "BLOCKED_PRECONDITION"
            self.stop_reason = f"finalization_failure:{type(exc).__name__}"

        return RunResult(
            terminal_verdict=verdict,
            request_budget_projected=self.request_budget_projected,
            requests_actual=self.requests_actual,
            artifact_root=self.writer.paths.main_final
            if self.writer.paths.main_final.exists()
            else None,
            holdout_root=self.writer.paths.holdout_final
            if self.writer.paths.holdout_final.exists()
            else None,
            stop_reason=self.stop_reason,
            crosscheck_verified=self.crosscheck is not None,
        )
