"""Binance USD-M R4 P0 point-in-time public market-data collector.

This module is deliberately isolated from every Binance execution adapter.  It
can issue only unsigned GET requests to a small production-public allowlist and
can connect only to the public USD-M websocket host.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import fcntl
import hashlib
import json
import logging
import random
import sqlite3
import subprocess
import traceback
import urllib.parse
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import httpx
import websockets

from app.services.brokers.binance.r4_p0_hardening import (
    FINAL_COMPLETE,
    AlertDispatcher,
    DeterministicEpochFinalizer,
    EpochLedger,
    EpochPolicy,
    FinalizationInvariantError,
    RawPITReader,
    StudyManifest,
    assert_artifact_manifest_compatible,
    availability_report,
    bind_study_manifest,
    decision_epoch_for_event,
    finalize_at,
    first_fully_observed_epoch,
    scheduled_epochs,
    sha256_bytes,
)

REST_BASE_URL: Final = "https://fapi.binance.com"
WS_PUBLIC_BASE_URL: Final = "wss://fstream.binance.com/public"
WS_MARKET_BASE_URL: Final = "wss://fstream.binance.com/market"
REST_PATH_ALLOWLIST: Final = frozenset(
    {
        "/fapi/v1/openInterest",
        "/fapi/v1/premiumIndex",
        "/fapi/v1/premiumIndexKlines",
        "/futures/data/basis",
        "/futures/data/openInterestHist",
        "/futures/data/takerlongshortRatio",
    }
)
SYMBOLS: Final = ("XRPUSDT", "DOGEUSDT", "SOLUSDT", "BTCUSDT")
SIGNAL_SYMBOLS: Final = frozenset({"XRPUSDT", "DOGEUSDT", "SOLUSDT"})
PREDICTOR_ONLY_SYMBOLS: Final = frozenset({"BTCUSDT"})
PIT_COLUMNS: Final = (
    "source",
    "symbol",
    "event_time",
    "transaction_time",
    "local_receive_time",
    "request_started_at",
    "request_completed_at",
    "sequence_or_trade_id",
    "raw_payload_sha256",
    "collector_version",
    "partition_sha256",
    "gap_detected",
    "reconnect_id",
)
COLLECTOR_VERSION: Final = "r4-p0-collector.v5"
BASIS_RECOVERY_LIMIT: Final = 100
# Binance's taker ratio endpoint maps requested boundaries to the preceding
# 5-minute buckets ([S-5m, E-5m]); the other epoch-history endpoints use their
# requested boundaries directly. Keep these source-specific vendor semantics
# explicit so their recovery windows are not incorrectly normalized.
EPOCH_HISTORY_REQUEST_OFFSET_MS: Final[dict[str, int]] = {
    "binance_usdm.openInterestHist": 0,
    "binance_usdm.basis": 0,
    "binance_usdm.takerLongShortRatio": 5 * 60 * 1000,
    "binance_usdm.premiumIndexKline1m": 0,
}
EXPECTED_SOURCES: Final = frozenset(
    {
        "binance_usdm.aggTrade",
        "binance_usdm.forceOrder",
        "binance_usdm.bookTicker",
        "binance_usdm.depth5",
        "binance_usdm.openInterest",
        "binance_usdm.openInterestHist",
        "binance_usdm.basis",
        "binance_usdm.takerLongShortRatio",
        "binance_usdm.premiumIndex",
        "binance_usdm.premiumIndexKline1m",
        "binance_usdm.predictedFunding",
    }
)
SPARSE_SOURCES: Final = frozenset({"binance_usdm.forceOrder"})
REQUIRED_ACTIVE_SOURCES: Final = EXPECTED_SOURCES - SPARSE_SOURCES

log = logging.getLogger("r4_p0_collector")


class MissingBasisDataError(ValueError):
    """The venue returned no usable basis observation for a requested symbol."""


class BasisPollError(RuntimeError):
    """One or more symbols failed without starving the remaining basis polls."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def iso_utc(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return (
        value.astimezone(dt.UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def epoch_ms(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return iso_utc(dt.datetime.fromtimestamp(int(value) / 1000, tz=dt.UTC))
    except (TypeError, ValueError, OSError):
        return None


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@lru_cache(maxsize=1)
def runtime_code_hash() -> str:
    """Return the exact deployed Git commit loaded by this process."""

    repository_root = Path(__file__).resolve().parents[4]
    try:
        result = subprocess.run(
            ("git", "-C", str(repository_root), "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            "cannot resolve deployed HEAD from the loaded source tree"
        ) from exc
    resolved = result.stdout.strip().lower()
    if len(resolved) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in resolved
    ):
        raise RuntimeError(f"invalid deployed HEAD returned by git: {resolved!r}")
    return resolved


def assert_rest_target(path: str) -> None:
    parsed = urllib.parse.urlparse(f"{REST_BASE_URL}{path}")
    if (
        parsed.scheme != "https"
        or parsed.hostname != "fapi.binance.com"
        or parsed.path not in REST_PATH_ALLOWLIST
    ):
        raise ValueError(
            f"blocked Binance REST target: {parsed.scheme}://{parsed.netloc}{parsed.path}"
        )


def assert_ws_target(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.rstrip("/")
    if (
        parsed.scheme != "wss"
        or parsed.hostname != "fstream.binance.com"
        or not (path.startswith("/public/") or path.startswith("/market/"))
    ):
        raise ValueError(
            f"blocked Binance websocket target: {parsed.scheme}://{parsed.netloc}"
        )


def build_ws_urls(symbols: Sequence[str] = SYMBOLS) -> dict[str, str]:
    public_streams = [
        f"{symbol.lower()}@{stream}"
        for symbol in symbols
        for stream in ("bookTicker", "depth5@100ms")
    ]
    market_streams = [
        f"{symbol.lower()}@{stream}"
        for symbol in symbols
        for stream in ("aggTrade", "forceOrder")
    ]
    # The all-market snapshot is retained alongside symbol streams as required
    # by the R4 source contract. Semantic record ids deduplicate overlap.
    market_streams.append("!forceOrder@arr")
    urls = {
        "public": (f"{WS_PUBLIC_BASE_URL}/stream?streams={'/'.join(public_streams)}"),
        "market": (f"{WS_MARKET_BASE_URL}/stream?streams={'/'.join(market_streams)}"),
    }
    for url in urls.values():
        assert_ws_target(url)
    return urls


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    artifact_root: Path
    epoch_policy: EpochPolicy
    study_manifest: StudyManifest
    duration_seconds: float | None = None
    oi_poll_seconds: float = 60.0
    premium_poll_seconds: float = 60.0
    basis_poll_seconds: float = 300.0
    taker_poll_seconds: float = 300.0
    status_seconds: float = 30.0
    symbols: tuple[str, ...] = SYMBOLS
    collector_instance_id: str = "r4-p0-local"
    replica_artifacts: tuple[Path, ...] = ()
    alert_webhook_urls: tuple[str, ...] = ()
    epoch_tick_seconds: float = 5.0
    epoch_retry_seconds: float = 30.0
    deadline_risk_seconds: float = 60.0 * 60.0
    heartbeat_stale_seconds: float = 120.0
    minimum_healthy_replicas: int = 1
    epoch_observation_start: dt.datetime | None = None

    def __post_init__(self) -> None:
        if self.epoch_policy != self.study_manifest.epoch_policy:
            raise ValueError(
                "collector policy must come from the pinned study manifest"
            )
        policy_symbols = tuple(sorted(set(self.symbols).intersection(SIGNAL_SYMBOLS)))
        if self.epoch_policy.symbols != policy_symbols:
            raise ValueError(
                "collector signal symbols must match the pinned study manifest"
            )
        if self.epoch_policy.required_sources != tuple(sorted(REQUIRED_ACTIVE_SOURCES)):
            raise ValueError(
                "collector sources must match the sealed active source set"
            )
        if (
            self.epoch_observation_start is not None
            and self.epoch_observation_start.tzinfo is None
        ):
            raise ValueError("epoch_observation_start must be timezone-aware")


class AppendOnlyPITStore:
    """Crash-safe local research artifact with immutable rows and deduplication."""

    def __init__(
        self,
        root: Path,
        *,
        collector_version: str = COLLECTOR_VERSION,
        artifact_filename: str = "r4_p0_collector.sqlite3",
        lock_filename: str = ".collector.lock",
        study_manifest: StudyManifest | None = None,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / artifact_filename
        self.collector_version = collector_version
        self.study_manifest = study_manifest
        self._lock_file = (self.root / lock_filename).open("a+")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            raise RuntimeError(
                f"collector artifact is already locked: {self.root}"
            ) from exc
        try:
            if study_manifest is not None:
                assert_artifact_manifest_compatible(self.path, study_manifest)
            self._db = sqlite3.connect(self.path)
            self._db.row_factory = sqlite3.Row
            self._configure()
            if study_manifest is not None:
                bind_study_manifest(
                    self._db,
                    study_manifest,
                    recorded_at=dt.datetime.now(tz=dt.UTC),
                )
        except BaseException:
            database = getattr(self, "_db", None)
            if database is not None:
                database.close()
            self._lock_file.close()
            raise

    def _configure(self) -> None:
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS pit_records (
                append_id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id TEXT NOT NULL UNIQUE,
                partition_key TEXT NOT NULL,
                source TEXT NOT NULL,
                symbol TEXT NOT NULL,
                event_time TEXT,
                transaction_time TEXT,
                local_receive_time TEXT NOT NULL,
                request_started_at TEXT,
                request_completed_at TEXT,
                sequence_or_trade_id TEXT,
                raw_payload_sha256 TEXT NOT NULL,
                collector_version TEXT NOT NULL,
                partition_sha256 TEXT NOT NULL,
                gap_detected INTEGER NOT NULL CHECK (gap_detected IN (0, 1)),
                reconnect_id TEXT,
                previous_partition_sha256 TEXT,
                run_id TEXT NOT NULL,
                raw_payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_pit_partition
                ON pit_records(partition_key, append_id);
            CREATE INDEX IF NOT EXISTS ix_pit_source_symbol
                ON pit_records(source, symbol, append_id);
            CREATE TRIGGER IF NOT EXISTS pit_records_no_update
            BEFORE UPDATE ON pit_records
            BEGIN
                SELECT RAISE(ABORT, 'pit_records is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS pit_records_no_delete
            BEFORE DELETE ON pit_records
            BEGIN
                SELECT RAISE(ABORT, 'pit_records is append-only');
            END;
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()
        fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
        self._lock_file.close()

    def __enter__(self) -> AppendOnlyPITStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def has_source_symbol(self, source: str, symbol: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM pit_records WHERE source = ? AND symbol = ? LIMIT 1",
            (source, symbol),
        ).fetchone()
        return row is not None

    def first_event_time(self, source: str, symbol: str) -> str | None:
        row = self._db.execute(
            """
            SELECT MIN(event_time) AS event_time
            FROM pit_records
            WHERE source = ? AND symbol = ?
            """,
            (source, symbol),
        ).fetchone()
        return row["event_time"] if row is not None else None

    def latest_event_time(self, source: str, symbol: str) -> str | None:
        row = self._db.execute(
            """
            SELECT MAX(event_time) AS event_time
            FROM pit_records
            WHERE source = ? AND symbol = ?
            """,
            (source, symbol),
        ).fetchone()
        return row["event_time"] if row is not None else None

    def append(
        self,
        *,
        source: str,
        symbol: str,
        raw_payload: Any,
        local_receive_time: dt.datetime,
        run_id: str,
        event_time: str | None,
        transaction_time: str | None,
        request_started_at: str | None,
        request_completed_at: str | None,
        sequence_or_trade_id: str | None,
        gap_detected: bool,
        reconnect_id: str | None,
    ) -> bool:
        raw_text = canonical_json(raw_payload)
        raw_hash = sha256_text(raw_text)
        local_iso = iso_utc(local_receive_time)
        assert local_iso is not None
        record_identity = {
            "source": source,
            "symbol": symbol,
            "event_time": event_time,
            "transaction_time": transaction_time,
            "sequence_or_trade_id": sequence_or_trade_id,
            "raw_payload_sha256": raw_hash,
        }
        record_id = sha256_text(canonical_json(record_identity))
        day = local_iso[:10]
        partition_key = f"{source}/{symbol}/{day}"

        self._db.execute("BEGIN IMMEDIATE")
        try:
            if self._db.execute(
                "SELECT 1 FROM pit_records WHERE record_id = ?", (record_id,)
            ).fetchone():
                self._db.rollback()
                return False
            previous_row = self._db.execute(
                """
                SELECT partition_sha256 FROM pit_records
                WHERE partition_key = ? ORDER BY append_id DESC LIMIT 1
                """,
                (partition_key,),
            ).fetchone()
            previous_hash = previous_row["partition_sha256"] if previous_row else None
            chain_payload = {
                **record_identity,
                "local_receive_time": local_iso,
                "request_started_at": request_started_at,
                "request_completed_at": request_completed_at,
                "collector_version": self.collector_version,
                "gap_detected": gap_detected,
                "reconnect_id": reconnect_id,
                "previous_partition_sha256": previous_hash,
                "run_id": run_id,
            }
            partition_hash = sha256_text(
                f"{previous_hash or ''}\n{canonical_json(chain_payload)}"
            )
            self._db.execute(
                """
                INSERT INTO pit_records (
                    record_id, partition_key, source, symbol, event_time,
                    transaction_time, local_receive_time, request_started_at,
                    request_completed_at, sequence_or_trade_id,
                    raw_payload_sha256, collector_version, partition_sha256,
                    gap_detected, reconnect_id, previous_partition_sha256,
                    run_id, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    partition_key,
                    source,
                    symbol,
                    event_time,
                    transaction_time,
                    local_iso,
                    request_started_at,
                    request_completed_at,
                    sequence_or_trade_id,
                    raw_hash,
                    self.collector_version,
                    partition_hash,
                    int(gap_detected),
                    reconnect_id,
                    previous_hash,
                    run_id,
                    raw_text,
                ),
            )
            self._db.commit()
            return True
        except BaseException:
            self._db.rollback()
            raise

    def audit(self) -> dict[str, Any]:
        missing = []
        bad_raw_hash = 0
        bad_chain = 0
        previous_by_partition: dict[str, str | None] = {}
        count = 0
        rows = self._db.execute("SELECT * FROM pit_records ORDER BY append_id")
        for row in rows:
            count += 1
            record = dict(row)
            missing.extend(column for column in PIT_COLUMNS if column not in record)
            if sha256_text(record["raw_payload"]) != record["raw_payload_sha256"]:
                bad_raw_hash += 1
            previous = previous_by_partition.get(record["partition_key"])
            if record["previous_partition_sha256"] != previous:
                bad_chain += 1
            chain_payload = {
                "source": record["source"],
                "symbol": record["symbol"],
                "event_time": record["event_time"],
                "transaction_time": record["transaction_time"],
                "sequence_or_trade_id": record["sequence_or_trade_id"],
                "raw_payload_sha256": record["raw_payload_sha256"],
                "local_receive_time": record["local_receive_time"],
                "request_started_at": record["request_started_at"],
                "request_completed_at": record["request_completed_at"],
                "collector_version": record["collector_version"],
                "gap_detected": bool(record["gap_detected"]),
                "reconnect_id": record["reconnect_id"],
                "previous_partition_sha256": record["previous_partition_sha256"],
                "run_id": record["run_id"],
            }
            expected = sha256_text(f"{previous or ''}\n{canonical_json(chain_payload)}")
            if expected != record["partition_sha256"]:
                bad_chain += 1
            previous_by_partition[record["partition_key"]] = record["partition_sha256"]
        return {
            "rows": count,
            "pit_columns": list(PIT_COLUMNS),
            "missing_pit_columns": sorted(set(missing)),
            "bad_raw_payload_hashes": bad_raw_hash,
            "bad_partition_chain_links": bad_chain,
            "ok": not missing and bad_raw_hash == 0 and bad_chain == 0,
        }

    def sample_by_source(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            """
            SELECT p.* FROM pit_records p
            INNER JOIN (
                SELECT source, MIN(append_id) AS append_id
                FROM pit_records GROUP BY source
            ) firsts ON firsts.append_id = p.append_id
            ORDER BY p.source
            """
        )
        return [dict(row) for row in rows]

    def counts(self) -> dict[str, int]:
        return {
            row["source"]: row["count"]
            for row in self._db.execute(
                "SELECT source, COUNT(*) AS count FROM pit_records GROUP BY source"
            )
        }


class BinanceR4P0Collector:
    def __init__(self, config: CollectorConfig, store: AppendOnlyPITStore) -> None:
        self.config = config
        self.store = store
        self.run_id = uuid.uuid4().hex
        self.stop = asyncio.Event()
        self.session_counts: Counter[str] = Counter()
        self.duplicate_counts: Counter[str] = Counter()
        self.failure_counts: Counter[str] = Counter()
        self.code_hash = runtime_code_hash()
        self._previous_sequence: dict[tuple[str, str], int] = {}
        self._seen_on_connection: set[tuple[str, str, str]] = set()
        self._last_book_snapshot: dict[str, dt.datetime] = {}
        if (
            store.study_manifest is None
            or store.study_manifest.content_sha256
            != config.study_manifest.content_sha256
        ):
            raise ValueError(
                "collector store must be bound to the configured study manifest"
            )
        for replica_path in config.replica_artifacts:
            assert_artifact_manifest_compatible(
                replica_path,
                config.study_manifest,
            )
        self.study_manifest = config.study_manifest
        self.epoch_policy = config.epoch_policy
        self.epoch_start = self.epoch_policy.t0
        if config.epoch_observation_start is not None:
            self.epoch_start = max(
                self.epoch_start,
                first_fully_observed_epoch(config.epoch_observation_start),
            )
        self.epoch_ledger = EpochLedger(store._db, self.epoch_policy)
        self.raw_reader = RawPITReader(
            store._db,
            store.path,
            config.replica_artifacts,
            study_manifest=self.study_manifest,
        )
        self.epoch_finalizer = DeterministicEpochFinalizer(
            self.epoch_ledger,
            self.raw_reader,
            collector_instance_id=self.config.collector_instance_id,
            run_id=self.run_id,
        )
        self.local_raw_reader = RawPITReader(store._db, store.path, ())
        self.local_epoch_finalizer = DeterministicEpochFinalizer(
            self.epoch_ledger,
            self.local_raw_reader,
            collector_instance_id=self.config.collector_instance_id,
            run_id=self.run_id,
        )
        self.alert_dispatcher = AlertDispatcher(
            self.epoch_ledger, config.alert_webhook_urls
        )
        self._opened_epochs: set[str] = set()
        self._finalized_epochs: set[tuple[str, str]] = set()
        self._last_epoch_retry: dt.datetime | None = None

    async def run(self) -> None:
        started_at = utc_now()
        self.epoch_ledger.validate_t0_startup(started_at=started_at)
        self.epoch_ledger.append_process_version(
            collector_instance_id=self.config.collector_instance_id,
            run_id=self.run_id,
            started_at=started_at,
            code_hash=self.code_hash,
            collector_version=COLLECTOR_VERSION,
            study_manifest_sha256=self.study_manifest.content_sha256,
        )
        log.info(
            "collector.start run_id=%s code_hash=%s manifest_sha256=%s "
            "study_id=%s t0=%s epoch_start=%s symbols=%s artifact=%s",
            self.run_id,
            self.code_hash,
            self.study_manifest.content_sha256,
            self.epoch_policy.study_id,
            iso_utc(self.epoch_policy.t0),
            iso_utc(self.epoch_start),
            ",".join(self.config.symbols),
            self.store.path,
        )
        tasks = [
            asyncio.create_task(self._ws_supervisor("public"), name="r4-p0-ws-public"),
            asyncio.create_task(self._ws_supervisor("market"), name="r4-p0-ws-market"),
            asyncio.create_task(
                self._poll_loop("open_interest", self.config.oi_poll_seconds),
                name="r4-p0-open-interest",
            ),
            asyncio.create_task(
                self._poll_loop("premium", self.config.premium_poll_seconds),
                name="r4-p0-premium",
            ),
            asyncio.create_task(
                self._poll_loop("basis", self.config.basis_poll_seconds),
                name="r4-p0-basis",
            ),
            asyncio.create_task(
                self._poll_loop("taker", self.config.taker_poll_seconds),
                name="r4-p0-taker",
            ),
            asyncio.create_task(
                self._epoch_supervisor(), name="r4-p0-epoch-supervisor"
            ),
            asyncio.create_task(self._status_loop(), name="r4-p0-status"),
        ]
        timer = None
        if self.config.duration_seconds is not None:
            timer = asyncio.create_task(self._duration_timer(), name="r4-p0-duration")
            tasks.append(timer)
        try:
            await self.stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            log.info(
                "collector.stop run_id=%s session_counts=%s duplicates=%s failures=%s",
                self.run_id,
                dict(self.session_counts),
                dict(self.duplicate_counts),
                dict(self.failure_counts),
            )

    def health(self) -> dict[str, Any]:
        active = set(self.session_counts) | set(self.duplicate_counts)
        missing_required = sorted(REQUIRED_ACTIVE_SOURCES - active)
        missing_sparse = sorted(SPARSE_SOURCES - active)
        return {
            "ok": not missing_required and not self.failure_counts,
            "missing_required_sources": missing_required,
            "missing_sparse_sources": missing_sparse,
            "failures": dict(self.failure_counts),
            "session_counts": dict(self.session_counts),
            "duplicate_counts": dict(self.duplicate_counts),
            "code_hash": self.code_hash,
            "collector_version": COLLECTOR_VERSION,
            "study_id": self.epoch_policy.study_id,
            "policy_hash": self.epoch_policy.policy_hash,
            "study_manifest_sha256": self.study_manifest.content_sha256,
            "t0": iso_utc(self.epoch_policy.t0),
            "epoch_start": iso_utc(self.epoch_start),
        }

    async def _duration_timer(self) -> None:
        assert self.config.duration_seconds is not None
        await asyncio.sleep(self.config.duration_seconds)
        self.stop.set()

    async def _status_loop(self) -> None:
        try:
            await self._status_loop_body()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure_counts["status_supervisor"] += 1
            log.exception(
                "collector.supervisor.failed lane=status error=%s detail=%r",
                type(exc).__name__,
                str(exc),
            )
            self.stop.set()
            raise

    async def _status_loop_body(self) -> None:
        while True:
            await asyncio.sleep(self.config.status_seconds)
            observed_at = utc_now()
            health = self.health()
            self.epoch_ledger.append_heartbeat(
                collector_instance_id=self.config.collector_instance_id,
                run_id=self.run_id,
                observed_at=observed_at,
                health=health,
            )
            log.info(
                "collector.status run_id=%s session_counts=%s duplicates=%s failures=%s total=%s",
                self.run_id,
                dict(self.session_counts),
                dict(self.duplicate_counts),
                dict(self.failure_counts),
                self.store.counts(),
            )
            artifact_paths = (self.store.path, *self.config.replica_artifacts)
            availability = availability_report(
                artifact_paths,
                self.epoch_policy,
                observed_at=observed_at,
                stale_after_seconds=self.config.heartbeat_stale_seconds,
                expected_code_hash=self.code_hash,
                expected_study_manifest_sha256=(self.study_manifest.content_sha256),
            )
            if (
                availability["healthy_replica_count"]
                < self.config.minimum_healthy_replicas
            ):
                await self.alert_dispatcher.emit(
                    alert_key=(
                        "COLLECTOR_REDUNDANCY_LOST:"
                        f"{self.epoch_policy.study_id}:"
                        f"{self.epoch_policy.policy_hash}:"
                        f"{int(observed_at.timestamp()) // 900}"
                    ),
                    severity="CRITICAL",
                    payload={
                        "alert_type": "COLLECTOR_REDUNDANCY_LOST",
                        "minimum_healthy_replicas": (
                            self.config.minimum_healthy_replicas
                        ),
                        **availability,
                    },
                    now=observed_at,
                )

    async def _epoch_supervisor(self) -> None:
        try:
            await self._epoch_supervisor_body()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure_counts["epoch_supervisor"] += 1
            log.exception(
                "collector.supervisor.failed lane=epoch error=%s detail=%r",
                type(exc).__name__,
                str(exc),
            )
            self.stop.set()
            raise

    async def _epoch_supervisor_body(self) -> None:
        async with httpx.AsyncClient(
            base_url=REST_BASE_URL,
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
            headers={"User-Agent": f"auto-trader/{COLLECTOR_VERSION}"},
        ) as client:
            while not self.stop.is_set():
                now = utc_now()
                for epoch in scheduled_epochs(self.epoch_start, now):
                    epoch_text = iso_utc(epoch)
                    assert epoch_text is not None
                    if epoch_text not in self._opened_epochs:
                        self.epoch_ledger.ensure_open_rows(
                            epoch,
                            self.config.collector_instance_id,
                            now,
                        )
                        self._opened_epochs.add(epoch_text)
                    if now >= finalize_at(epoch):
                        await self._finalize_epoch(epoch, now)

                retry_epoch = decision_epoch_for_event(now) - dt.timedelta(hours=4)
                retry_deadline = finalize_at(retry_epoch)
                if (
                    retry_epoch >= self.epoch_start
                    and retry_epoch <= now < retry_deadline
                ):
                    due = (
                        self._last_epoch_retry is None
                        or (now - self._last_epoch_retry).total_seconds()
                        >= self.config.epoch_retry_seconds
                    )
                    if due:
                        await self._retry_incomplete_epoch(client, retry_epoch, now)
                        self._last_epoch_retry = now
                    if (
                        retry_deadline - now
                    ).total_seconds() <= self.config.deadline_risk_seconds:
                        await self._alert_deadline_risk(retry_epoch, now)
                await asyncio.sleep(self.config.epoch_tick_seconds)

    async def _finalize_epoch(
        self, epoch: dt.datetime, observed_at: dt.datetime
    ) -> None:
        epoch_text = iso_utc(epoch) or ""
        if all(
            (symbol, epoch_text) in self._finalized_epochs
            for symbol in self.epoch_policy.symbols
        ):
            return
        for symbol in self.epoch_policy.symbols:
            cache_key = (symbol, epoch_text)
            if cache_key in self._finalized_epochs:
                continue
            try:
                result = self.epoch_finalizer.finalize(
                    symbol, epoch, observed_at=observed_at
                )
            except FinalizationInvariantError as exc:
                self.failure_counts["finalizer_invariant"] += 1
                await self.alert_dispatcher.emit(
                    alert_key=(
                        "LEDGER_REPRODUCTION_FAIL:"
                        f"{self.epoch_policy.study_id}:"
                        f"{self.epoch_policy.policy_hash}:"
                        f"{symbol}:{epoch_text}"
                    ),
                    severity="CRITICAL",
                    payload={
                        "alert_type": "LEDGER_REPRODUCTION_FAIL",
                        "decision_epoch_utc": epoch_text,
                        "error_type": type(exc).__name__,
                        "policy_hash": self.epoch_policy.policy_hash,
                        "study_id": self.epoch_policy.study_id,
                        "symbol": symbol,
                    },
                    now=observed_at,
                )
                self.stop.set()
                return
            self._finalized_epochs.add(cache_key)
            self.epoch_finalizer.append_late_only(
                symbol, epoch, recorded_at=observed_at
            )
            if result.final_status != FINAL_COMPLETE:
                await self.alert_dispatcher.emit(
                    alert_key=(
                        "DATA_INTEGRITY_FAIL:"
                        f"{result.study_id}:{result.policy_hash}:"
                        f"{result.symbol}:{result.decision_epoch_utc}:"
                        f"{result.final_status}"
                    ),
                    severity="CRITICAL",
                    payload={
                        "alert_type": "DATA_INTEGRITY_FAIL",
                        "conflict_sources": result.conflict_sources,
                        "decision_epoch_utc": result.decision_epoch_utc,
                        "evaluation_hash": result.evaluation_hash,
                        "final_status": result.final_status,
                        "finalize_at": result.finalize_at,
                        "invalid_sources": result.invalid_sources,
                        "missing_sources": result.missing_sources,
                        "policy_hash": result.policy_hash,
                        "study_id": result.study_id,
                        "symbol": result.symbol,
                    },
                    now=observed_at,
                )

    async def _alert_deadline_risk(
        self, epoch: dt.datetime, observed_at: dt.datetime
    ) -> None:
        for symbol in self.epoch_policy.symbols:
            preview = self.epoch_finalizer.preview(symbol, epoch)
            if preview.final_status == FINAL_COMPLETE:
                continue
            await self.alert_dispatcher.emit(
                alert_key=(
                    "EPOCH_DEADLINE_RISK:"
                    f"{preview.study_id}:{preview.policy_hash}:"
                    f"{symbol}:{preview.decision_epoch_utc}"
                ),
                severity="WARNING",
                payload={
                    "alert_type": "EPOCH_DEADLINE_RISK",
                    "conflict_sources": preview.conflict_sources,
                    "decision_epoch_utc": preview.decision_epoch_utc,
                    "finalize_at": preview.finalize_at,
                    "invalid_sources": preview.invalid_sources,
                    "missing_sources": preview.missing_sources,
                    "policy_hash": preview.policy_hash,
                    "study_id": preview.study_id,
                    "symbol": symbol,
                },
                now=observed_at,
            )

    async def _retry_incomplete_epoch(
        self,
        client: httpx.AsyncClient,
        epoch: dt.datetime,
        observed_at: dt.datetime,
    ) -> None:
        recoverable = {
            "binance_usdm.basis",
            "binance_usdm.openInterestHist",
            "binance_usdm.premiumIndexKline1m",
            "binance_usdm.takerLongShortRatio",
        }
        for symbol in self.epoch_policy.symbols:
            preview = self.local_epoch_finalizer.preview(symbol, epoch)
            retry_sources = sorted(
                (set(preview.missing_sources) | set(preview.invalid_sources))
                & recoverable
            )
            for source in retry_sources:
                if observed_at >= finalize_at(epoch):
                    self.epoch_ledger.append_attempt(
                        collector_instance_id=self.config.collector_instance_id,
                        decision_epoch=epoch,
                        attempted_at=observed_at,
                        completed_at=observed_at,
                        request_identity={
                            "method": "GET",
                            "source_name": source,
                            "symbol": symbol,
                            "target": "deadline-recovery",
                        },
                        response_sha256=None,
                        terminal_status="DEADLINE_EXPIRED",
                    )
                    continue
                try:
                    await self._rest_epoch_history(
                        client,
                        source=source,
                        symbol=symbol,
                        decision_epoch=epoch,
                    )
                except Exception as exc:
                    # The request-level immutable attempt row already contains
                    # the terminal failure. Other missing sources still retry.
                    self.failure_counts["epoch_retry"] += 1
                    log.exception(
                        "collector.epoch_retry.failed source=%s symbol=%s "
                        "decision_epoch=%s error=%s detail=%r",
                        source,
                        symbol,
                        iso_utc(epoch),
                        type(exc).__name__,
                        str(exc),
                    )
                    continue

    async def _ws_supervisor(self, lane: str) -> None:
        failure_attempt = 0
        reconnect_number = 0
        url = build_ws_urls(self.config.symbols)[lane]
        while not self.stop.is_set():
            reconnect_number += 1
            reconnect_id = f"{self.run_id}:ws:{lane}:{reconnect_number}"
            request_started = utc_now()
            request_identity = {
                "lane": lane,
                "method": "WEBSOCKET_CONNECT",
                "target": url,
            }
            connect_attempt_id = self.epoch_ledger.begin_attempt(
                collector_instance_id=self.config.collector_instance_id,
                decision_epoch=decision_epoch_for_event(request_started),
                attempted_at=request_started,
                request_identity=request_identity,
            )
            connected_at: dt.datetime | None = None
            try:
                assert_ws_target(url)
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                    max_queue=4096,
                ) as ws:
                    request_completed = utc_now()
                    connected_at = request_completed
                    log.info(
                        "collector.ws.connected lane=%s reconnect_id=%s",
                        lane,
                        reconnect_id,
                    )
                    self.epoch_ledger.append_attempt(
                        collector_instance_id=self.config.collector_instance_id,
                        decision_epoch=decision_epoch_for_event(request_started),
                        attempted_at=request_started,
                        completed_at=request_completed,
                        request_identity=request_identity,
                        response_sha256=None,
                        terminal_status="SUCCESS",
                        attempt_id=connect_attempt_id,
                    )
                    failure_attempt = 0
                    async for raw in ws:
                        received = utc_now()
                        self._handle_ws_raw(
                            raw,
                            received=received,
                            request_started=request_started,
                            request_completed=request_completed,
                            reconnect_id=reconnect_id,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed_at = utc_now()
                failure_identity = (
                    {
                        "lane": lane,
                        "method": "WEBSOCKET_SESSION",
                        "reconnect_id": reconnect_id,
                        "target": url,
                    }
                    if connected_at is not None
                    else request_identity
                )
                self.epoch_ledger.append_attempt(
                    collector_instance_id=self.config.collector_instance_id,
                    decision_epoch=decision_epoch_for_event(
                        connected_at or request_started
                    ),
                    attempted_at=connected_at or request_started,
                    completed_at=failed_at,
                    request_identity=failure_identity,
                    response_sha256=None,
                    terminal_status="TRANSPORT_ERROR",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    error_traceback=traceback.format_exc(),
                    attempt_id=(
                        None if connected_at is not None else connect_attempt_id
                    ),
                )
                self.failure_counts["websocket"] += 1
                delay = min(60.0, 1.0 * (2 ** min(failure_attempt, 6)))
                delay *= random.uniform(0.8, 1.2)
                failure_attempt += 1
                log.exception(
                    "collector.ws.disconnected reconnect_id=%s error=%s "
                    "detail=%r backoff_s=%.2f",
                    reconnect_id,
                    type(exc).__name__,
                    str(exc),
                    delay,
                )
                await asyncio.sleep(delay)

    def _handle_ws_raw(
        self,
        raw: str | bytes,
        *,
        received: dt.datetime,
        request_started: dt.datetime,
        request_completed: dt.datetime,
        reconnect_id: str,
    ) -> None:
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            self.failure_counts["websocket_malformed"] += 1
            log.error("collector.ws.malformed reconnect_id=%s", reconnect_id)
            return
        stream = message.get("stream", "") if isinstance(message, dict) else ""
        data = message.get("data") if isinstance(message, dict) else None
        events = data if isinstance(data, list) else [data]
        for event in events:
            if not isinstance(event, dict):
                continue
            normalized = self._normalize_ws_event(stream, event)
            if normalized is None:
                continue
            source, symbol, event_time, transaction_time, sequence = normalized
            if symbol not in self.config.symbols:
                continue
            if source == "binance_usdm.bookTicker":
                previous_snapshot = self._last_book_snapshot.get(symbol)
                if (
                    previous_snapshot is not None
                    and (received - previous_snapshot).total_seconds() < 1.0
                ):
                    continue
                self._last_book_snapshot[symbol] = received
            gap = self._ws_gap(
                source=source,
                symbol=symbol,
                sequence=sequence,
                payload=event,
                reconnect_id=reconnect_id,
            )
            inserted = self.store.append(
                source=source,
                symbol=symbol,
                raw_payload=event,
                local_receive_time=received,
                run_id=self.run_id,
                event_time=event_time,
                transaction_time=transaction_time,
                request_started_at=iso_utc(request_started),
                request_completed_at=iso_utc(request_completed),
                sequence_or_trade_id=str(sequence) if sequence is not None else None,
                gap_detected=gap,
                reconnect_id=reconnect_id,
            )
            self._count_result(source, inserted)

    @staticmethod
    def _normalize_ws_event(
        stream: str, event: Mapping[str, Any]
    ) -> tuple[str, str, str | None, str | None, int | None] | None:
        event_type = event.get("e")
        symbol = str(event.get("s") or event.get("o", {}).get("s") or "").upper()
        if event_type == "aggTrade" or "@aggTrade" in stream:
            return (
                "binance_usdm.aggTrade",
                symbol,
                epoch_ms(event.get("E")),
                epoch_ms(event.get("T")),
                int(event["a"]),
            )
        if event_type == "forceOrder" or "forceOrder" in stream:
            order = event.get("o", {})
            sequence = order.get("T") or event.get("E")
            return (
                "binance_usdm.forceOrder",
                symbol,
                epoch_ms(event.get("E")),
                epoch_ms(order.get("T")),
                int(sequence) if sequence is not None else None,
            )
        if event_type == "bookTicker" or "@bookTicker" in stream:
            return (
                "binance_usdm.bookTicker",
                symbol,
                epoch_ms(event.get("E")),
                epoch_ms(event.get("T")),
                int(event["u"]),
            )
        if event_type == "depthUpdate" or "@depth" in stream:
            return (
                "binance_usdm.depth5",
                symbol,
                epoch_ms(event.get("E")),
                epoch_ms(event.get("T")),
                int(event["u"]),
            )
        return None

    def _ws_gap(
        self,
        *,
        source: str,
        symbol: str,
        sequence: int | None,
        payload: Mapping[str, Any],
        reconnect_id: str,
    ) -> bool:
        key = (source, symbol)
        connection_key = (source, symbol, reconnect_id)
        first_on_connection = connection_key not in self._seen_on_connection
        if first_on_connection:
            self._seen_on_connection.add(connection_key)
        gap = first_on_connection and self.store.has_source_symbol(source, symbol)
        previous = self._previous_sequence.get(key)
        if previous is not None and sequence is not None:
            if source == "binance_usdm.depth5" and payload.get("pu") is not None:
                gap = gap or int(payload["pu"]) != previous
            elif source == "binance_usdm.aggTrade":
                gap = gap or sequence != previous + 1
            elif source == "binance_usdm.bookTicker":
                gap = gap or sequence <= previous
        if sequence is not None:
            self._previous_sequence[key] = sequence
        if gap:
            log.warning(
                "collector.gap source=%s symbol=%s previous=%s current=%s reconnect_id=%s",
                source,
                symbol,
                previous,
                sequence,
                reconnect_id,
            )
        return gap

    async def _poll_loop(self, family: str, interval: float) -> None:
        failure_attempt = 0
        async with httpx.AsyncClient(
            base_url=REST_BASE_URL,
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
            headers={"User-Agent": f"auto-trader/{COLLECTOR_VERSION}"},
        ) as client:
            while not self.stop.is_set():
                try:
                    await self._poll_family(client, family)
                    failure_attempt = 0
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.failure_counts[family] += 1
                    delay = min(interval, 2.0 * (2 ** min(failure_attempt, 5)))
                    delay *= random.uniform(0.8, 1.2)
                    failure_attempt += 1
                    log.exception(
                        "collector.poll.failed family=%s error=%s detail=%r backoff_s=%.2f",
                        family,
                        type(exc).__name__,
                        str(exc),
                        delay,
                    )
                    await asyncio.sleep(delay)

    async def _poll_family(self, client: httpx.AsyncClient, family: str) -> None:
        if family == "basis":
            await self._poll_basis_family(client)
            return
        for symbol in self.config.symbols:
            if family == "open_interest":
                await self._rest_get(
                    client,
                    path="/fapi/v1/openInterest",
                    params={"symbol": symbol},
                    symbol=symbol,
                    outputs=(("binance_usdm.openInterest", None),),
                )
                await self._rest_get(
                    client,
                    path="/futures/data/openInterestHist",
                    params={"symbol": symbol, "period": "5m", "limit": 1},
                    symbol=symbol,
                    outputs=(("binance_usdm.openInterestHist", None),),
                )
            elif family == "premium":
                await self._rest_get(
                    client,
                    path="/fapi/v1/premiumIndex",
                    params={"symbol": symbol},
                    symbol=symbol,
                    outputs=(
                        ("binance_usdm.premiumIndex", None),
                        ("binance_usdm.predictedFunding", "lastFundingRate"),
                    ),
                )
                await self._rest_premium_kline(client, symbol=symbol)
            elif family == "taker":
                await self._rest_get(
                    client,
                    path="/futures/data/takerlongshortRatio",
                    params={"symbol": symbol, "period": "5m", "limit": 1},
                    symbol=symbol,
                    outputs=(("binance_usdm.takerLongShortRatio", None),),
                )
            else:
                raise ValueError(f"unknown poll family: {family}")

    async def _poll_basis_family(self, client: httpx.AsyncClient) -> None:
        failures: list[tuple[str, Exception]] = []
        for symbol in self.config.symbols:
            try:
                await self._rest_basis(client, symbol=symbol)
            except asyncio.CancelledError:
                raise
            except MissingBasisDataError as exc:
                failures.append((symbol, exc))
        if failures:
            detail = "; ".join(
                f"{symbol}={type(exc).__name__}({exc})" for symbol, exc in failures
            )
            raise BasisPollError(f"basis poll incomplete: {detail}")

    async def _rest_basis(self, client: httpx.AsyncClient, *, symbol: str) -> None:
        path = "/futures/data/basis"
        assert_rest_target(path)
        started = utc_now()
        params = {
            "pair": symbol,
            "contractType": "PERPETUAL",
            "period": "5m",
            # Re-read a bounded 8h20m window so a later successful request
            # can repair transient holes before epoch finalization.
            "limit": BASIS_RECOVERY_LIMIT,
        }
        target_epoch = decision_epoch_for_event(started)
        attempt_id = self._begin_rest_attempt(
            decision_epoch=target_epoch,
            attempted_at=started,
            path=path,
            params=params,
            symbol=symbol,
            sources=("binance_usdm.basis",),
        )
        response_hash: str | None = None
        response_body_summary: str | None = None
        completed = started
        inserted_count = 0
        try:
            response = await client.get(path, params=params)
            completed = utc_now()
            response_hash = sha256_bytes(response.content)
            response_body_summary = self._response_body_summary(response.content)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError(
                    f"invalid JSON shape from allowlisted path {path} symbol={symbol}"
                )
            if not payload:
                raise MissingBasisDataError(
                    f"no basis observations from allowlisted path {path} symbol={symbol}"
                )

            rows: list[tuple[int, Mapping[str, Any], str]] = []
            for item in payload:
                if not isinstance(item, dict):
                    raise ValueError(
                        f"invalid basis row from allowlisted path {path} symbol={symbol}"
                    )
                pair = str(item.get("pair") or "").upper()
                contract_type = str(item.get("contractType") or "").upper()
                timestamp = item.get("timestamp")
                event_time = epoch_ms(timestamp)
                if pair != symbol or contract_type != "PERPETUAL" or event_time is None:
                    raise ValueError(
                        f"invalid basis identity from allowlisted path {path} "
                        f"symbol={symbol} pair={pair!r} "
                        f"contractType={contract_type!r} timestamp={timestamp!r}"
                    )
                rows.append((int(timestamp), item, event_time))

            collection_floor = self.store.first_event_time("binance_usdm.basis", symbol)
            if collection_floor is None:
                # This collector is not the seed-backfill lane. On first sight
                # of a symbol, retain only the latest venue row and establish
                # the live collection floor.
                rows = [max(rows, key=lambda row: row[0])]
            else:
                rows = [row for row in rows if row[2] >= collection_floor]

            reconnect_id = f"{self.run_id}:rest:{path}"
            for timestamp, item, event_time in sorted(rows, key=lambda row: row[0]):
                inserted = self.store.append(
                    source="binance_usdm.basis",
                    symbol=symbol,
                    raw_payload=item,
                    local_receive_time=completed,
                    run_id=self.run_id,
                    event_time=event_time,
                    transaction_time=None,
                    request_started_at=iso_utc(started),
                    request_completed_at=iso_utc(completed),
                    sequence_or_trade_id=str(timestamp),
                    gap_detected=False,
                    reconnect_id=reconnect_id,
                )
                self._count_result("binance_usdm.basis", inserted)
                inserted_count += int(inserted)
        except Exception as exc:
            self._append_rest_attempt(
                decision_epoch=target_epoch,
                attempted_at=started,
                completed_at=completed,
                path=path,
                params=params,
                symbol=symbol,
                sources=("binance_usdm.basis",),
                response_hash=response_hash,
                terminal_status=self._attempt_failure_status(exc),
                error_type=type(exc).__name__,
                error_message=str(exc),
                error_traceback=traceback.format_exc(),
                response_body_summary=response_body_summary,
                attempt_id=attempt_id,
            )
            raise
        self._append_rest_attempt(
            decision_epoch=target_epoch,
            attempted_at=started,
            completed_at=completed,
            path=path,
            params=params,
            symbol=symbol,
            sources=("binance_usdm.basis",),
            response_hash=response_hash,
            terminal_status=("SUCCESS" if inserted_count else "EXACT_DUPLICATE"),
            attempt_id=attempt_id,
        )

    async def _rest_premium_kline(
        self,
        client: httpx.AsyncClient,
        *,
        symbol: str,
        decision_epoch: dt.datetime | None = None,
    ) -> None:
        path = "/fapi/v1/premiumIndexKlines"
        assert_rest_target(path)
        started = utc_now()
        target_epoch = decision_epoch or decision_epoch_for_event(started)
        source = "binance_usdm.premiumIndexKline1m"
        latest_event_time = self.store.latest_event_time(source, symbol)
        latest_event_ms: int | None = None
        params: dict[str, Any] = {"symbol": symbol, "interval": "1m", "limit": 2}
        if latest_event_time is not None:
            latest_event = dt.datetime.fromisoformat(
                latest_event_time.replace("Z", "+00:00")
            )
            latest_event_ms = int(latest_event.timestamp() * 1000)
            interval_start = target_epoch - dt.timedelta(hours=4)
            request_end_ms = min(
                int(started.timestamp() * 1000),
                int(target_epoch.timestamp() * 1000) - 1,
            )
            request_start_ms = max(
                int(interval_start.timestamp() * 1000),
                min(latest_event_ms + 1, request_end_ms),
            )
            params = {
                "symbol": symbol,
                "interval": "1m",
                "startTime": request_start_ms,
                "endTime": request_end_ms,
                "limit": 500,
            }
        attempt_id = self._begin_rest_attempt(
            decision_epoch=target_epoch,
            attempted_at=started,
            path=path,
            params=params,
            symbol=symbol,
            sources=("binance_usdm.premiumIndexKline1m",),
        )
        response_hash: str | None = None
        response_body_summary: str | None = None
        completed = started
        try:
            response = await client.get(path, params=params)
            completed = utc_now()
            response_hash = sha256_bytes(response.content)
            response_body_summary = self._response_body_summary(response.content)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError(f"invalid JSON shape from allowlisted path {path}")
            completed_ms = int(completed.timestamp() * 1000)
            complete_rows = [
                row
                for row in payload
                if isinstance(row, list)
                and len(row) >= 7
                and int(row[6]) <= completed_ms
            ]
            if not complete_rows:
                if latest_event_ms is None:
                    raise ValueError(
                        f"no completed 1m row from allowlisted path {path}"
                    )
            elif latest_event_ms is None:
                # Establish the live collection floor without seed backfill.
                complete_rows = [max(complete_rows, key=lambda row: int(row[6]))]
            else:
                interval_start_ms = int(
                    (target_epoch - dt.timedelta(hours=4)).timestamp() * 1000
                )
                interval_end_ms = int(target_epoch.timestamp() * 1000)
                complete_rows = [
                    row
                    for row in complete_rows
                    if latest_event_ms < int(row[6])
                    and interval_start_ms <= int(row[6]) < interval_end_ms
                ]

            inserted_count = 0
            for item in sorted(complete_rows, key=lambda row: int(row[6])):
                inserted = self.store.append(
                    source=source,
                    symbol=symbol,
                    raw_payload=item,
                    local_receive_time=completed,
                    run_id=self.run_id,
                    event_time=epoch_ms(item[6]),
                    transaction_time=epoch_ms(item[6]),
                    request_started_at=iso_utc(started),
                    request_completed_at=iso_utc(completed),
                    sequence_or_trade_id=str(item[0]),
                    gap_detected=False,
                    reconnect_id=f"{self.run_id}:rest:{path}",
                )
                self._count_result(source, inserted)
                inserted_count += int(inserted)
        except Exception as exc:
            self._append_rest_attempt(
                decision_epoch=target_epoch,
                attempted_at=started,
                completed_at=completed,
                path=path,
                params=params,
                symbol=symbol,
                sources=("binance_usdm.premiumIndexKline1m",),
                response_hash=response_hash,
                terminal_status=self._attempt_failure_status(exc),
                error_type=type(exc).__name__,
                error_message=str(exc),
                error_traceback=traceback.format_exc(),
                response_body_summary=response_body_summary,
                attempt_id=attempt_id,
            )
            raise
        self._append_rest_attempt(
            decision_epoch=target_epoch,
            attempted_at=started,
            completed_at=completed,
            path=path,
            params=params,
            symbol=symbol,
            sources=("binance_usdm.premiumIndexKline1m",),
            response_hash=response_hash,
            terminal_status="SUCCESS" if inserted_count else "EXACT_DUPLICATE",
            attempt_id=attempt_id,
        )

    async def _rest_get(
        self,
        client: httpx.AsyncClient,
        *,
        path: str,
        params: Mapping[str, Any],
        symbol: str,
        outputs: Sequence[tuple[str, str | None]],
        decision_epoch: dt.datetime | None = None,
    ) -> None:
        assert_rest_target(path)
        started = utc_now()
        target_epoch = decision_epoch or decision_epoch_for_event(started)
        output_sources = tuple(source for source, _ in outputs)
        attempt_id = self._begin_rest_attempt(
            decision_epoch=target_epoch,
            attempted_at=started,
            path=path,
            params=params,
            symbol=symbol,
            sources=output_sources,
        )
        response_hash: str | None = None
        response_body_summary: str | None = None
        completed = started
        inserted_any = False
        all_duplicates = True
        try:
            response = await client.get(path, params=params)
            completed = utc_now()
            response_hash = sha256_bytes(response.content)
            response_body_summary = self._response_body_summary(response.content)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, str) or not isinstance(payload, (dict, list)):
                raise ValueError(f"invalid JSON shape from allowlisted path {path}")
            item = payload[-1] if isinstance(payload, list) and payload else payload
            if not isinstance(item, dict):
                raise ValueError(f"empty/invalid payload from allowlisted path {path}")
            event_time = epoch_ms(item.get("time") or item.get("timestamp"))
            if event_time is None:
                # A response with no exchange timestamp cannot satisfy the PIT
                # contract and must never be silently persisted.
                raise ValueError(
                    f"missing exchange timestamp from allowlisted path {path}"
                )
            sequence = item.get("timestamp") or item.get("time")
            reconnect_id = f"{self.run_id}:rest:{path}"
            for source, semantic_field in outputs:
                semantic_payload = (
                    {**item, "_semantic_field": semantic_field}
                    if semantic_field is not None
                    else item
                )
                inserted = self.store.append(
                    source=source,
                    symbol=symbol,
                    raw_payload=semantic_payload,
                    local_receive_time=completed,
                    run_id=self.run_id,
                    event_time=event_time,
                    transaction_time=None,
                    request_started_at=iso_utc(started),
                    request_completed_at=iso_utc(completed),
                    sequence_or_trade_id=(
                        str(sequence) if sequence is not None else None
                    ),
                    gap_detected=False,
                    reconnect_id=reconnect_id,
                )
                self._count_result(source, inserted)
                inserted_any = inserted_any or inserted
                all_duplicates = all_duplicates and not inserted
        except Exception as exc:
            self._append_rest_attempt(
                decision_epoch=target_epoch,
                attempted_at=started,
                completed_at=completed,
                path=path,
                params=params,
                symbol=symbol,
                sources=output_sources,
                response_hash=response_hash,
                terminal_status=self._attempt_failure_status(exc),
                error_type=type(exc).__name__,
                error_message=str(exc),
                error_traceback=traceback.format_exc(),
                response_body_summary=response_body_summary,
                attempt_id=attempt_id,
            )
            raise
        self._append_rest_attempt(
            decision_epoch=target_epoch,
            attempted_at=started,
            completed_at=completed,
            path=path,
            params=params,
            symbol=symbol,
            sources=output_sources,
            response_hash=response_hash,
            terminal_status=(
                "EXACT_DUPLICATE" if all_duplicates and not inserted_any else "SUCCESS"
            ),
            attempt_id=attempt_id,
        )

    async def _rest_epoch_history(
        self,
        client: httpx.AsyncClient,
        *,
        source: str,
        symbol: str,
        decision_epoch: dt.datetime,
    ) -> None:
        interval_start = decision_epoch - dt.timedelta(hours=4)
        start_ms = int(interval_start.timestamp() * 1000)
        end_ms = int(decision_epoch.timestamp() * 1000) - 1
        request_offset_ms = EPOCH_HISTORY_REQUEST_OFFSET_MS[source]
        request_start_ms = start_ms + request_offset_ms
        request_end_ms = end_ms + request_offset_ms
        request_by_source: dict[str, tuple[str, dict[str, Any]]] = {
            "binance_usdm.openInterestHist": (
                "/futures/data/openInterestHist",
                {
                    "symbol": symbol,
                    "period": "5m",
                    "startTime": request_start_ms,
                    "endTime": request_end_ms,
                    "limit": 500,
                },
            ),
            "binance_usdm.basis": (
                "/futures/data/basis",
                {
                    "pair": symbol,
                    "contractType": "PERPETUAL",
                    "period": "5m",
                    "startTime": request_start_ms,
                    "endTime": request_end_ms,
                    "limit": 500,
                },
            ),
            "binance_usdm.takerLongShortRatio": (
                "/futures/data/takerlongshortRatio",
                {
                    "symbol": symbol,
                    "period": "5m",
                    "startTime": request_start_ms,
                    "endTime": request_end_ms,
                    "limit": 500,
                },
            ),
            "binance_usdm.premiumIndexKline1m": (
                "/fapi/v1/premiumIndexKlines",
                {
                    "symbol": symbol,
                    "interval": "1m",
                    "startTime": request_start_ms,
                    "endTime": request_end_ms,
                    "limit": 500,
                },
            ),
        }
        path, params = request_by_source[source]
        assert_rest_target(path)
        started = utc_now()
        attempt_id = self._begin_rest_attempt(
            decision_epoch=decision_epoch,
            attempted_at=started,
            path=path,
            params=params,
            symbol=symbol,
            sources=(source,),
        )
        response_hash: str | None = None
        response_body_summary: str | None = None
        completed = started
        inserted_count = 0
        try:
            response = await client.get(path, params=params)
            completed = utc_now()
            response_hash = sha256_bytes(response.content)
            response_body_summary = self._response_body_summary(response.content)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError(f"invalid historical JSON shape from {path}")
            reconnect_id = f"{self.run_id}:retry:{path}"
            for item in payload:
                if source == "binance_usdm.premiumIndexKline1m":
                    if not isinstance(item, list) or len(item) < 7:
                        raise ValueError(f"invalid historical kline row from {path}")
                    event_time = epoch_ms(item[6])
                    sequence = str(item[0])
                else:
                    if not isinstance(item, dict):
                        raise ValueError(f"invalid historical data row from {path}")
                    timestamp = item.get("timestamp") or item.get("time")
                    event_time = epoch_ms(timestamp)
                    sequence = str(timestamp) if timestamp is not None else None
                if event_time is None:
                    raise ValueError(
                        f"missing historical exchange timestamp from {path}"
                    )
                event_dt = dt.datetime.fromisoformat(event_time.replace("Z", "+00:00"))
                if not interval_start <= event_dt < decision_epoch:
                    continue
                inserted = self.store.append(
                    source=source,
                    symbol=symbol,
                    raw_payload=item,
                    local_receive_time=completed,
                    run_id=self.run_id,
                    event_time=event_time,
                    transaction_time=(
                        event_time
                        if source == "binance_usdm.premiumIndexKline1m"
                        else None
                    ),
                    request_started_at=iso_utc(started),
                    request_completed_at=iso_utc(completed),
                    sequence_or_trade_id=sequence,
                    gap_detected=False,
                    reconnect_id=reconnect_id,
                )
                self._count_result(source, inserted)
                inserted_count += int(inserted)
            if not payload:
                raise ValueError(f"empty historical response from {path}")
        except Exception as exc:
            self._append_rest_attempt(
                decision_epoch=decision_epoch,
                attempted_at=started,
                completed_at=completed,
                path=path,
                params=params,
                symbol=symbol,
                sources=(source,),
                response_hash=response_hash,
                terminal_status=self._attempt_failure_status(exc),
                error_type=type(exc).__name__,
                error_message=str(exc),
                error_traceback=traceback.format_exc(),
                response_body_summary=response_body_summary,
                attempt_id=attempt_id,
            )
            raise
        self._append_rest_attempt(
            decision_epoch=decision_epoch,
            attempted_at=started,
            completed_at=completed,
            path=path,
            params=params,
            symbol=symbol,
            sources=(source,),
            response_hash=response_hash,
            terminal_status=("SUCCESS" if inserted_count else "EXACT_DUPLICATE"),
            attempt_id=attempt_id,
        )

    @staticmethod
    def _attempt_failure_status(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            return "HTTP_ERROR"
        if isinstance(exc, httpx.HTTPError):
            return "TRANSPORT_ERROR"
        return "INVALID_RESPONSE"

    @staticmethod
    def _response_body_summary(content: bytes, *, limit: int = 4096) -> str:
        decoded = content[:limit].decode("utf-8", errors="replace")
        if len(content) > limit:
            return f"{decoded}…[truncated {len(content) - limit} bytes]"
        return decoded

    def _append_rest_attempt(
        self,
        *,
        decision_epoch: dt.datetime,
        attempted_at: dt.datetime,
        completed_at: dt.datetime,
        path: str,
        params: Mapping[str, Any],
        symbol: str,
        sources: Sequence[str],
        response_hash: str | None,
        terminal_status: str,
        error_type: str | None = None,
        error_message: str | None = None,
        error_traceback: str | None = None,
        response_body_summary: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        request_identity = self._rest_request_identity(
            path=path,
            params=params,
            symbol=symbol,
            sources=sources,
        )
        self.epoch_ledger.append_attempt(
            collector_instance_id=self.config.collector_instance_id,
            decision_epoch=decision_epoch,
            attempted_at=attempted_at,
            completed_at=completed_at,
            request_identity=request_identity,
            response_sha256=response_hash,
            terminal_status=terminal_status,
            error_type=error_type,
            error_message=error_message,
            error_traceback=error_traceback,
            response_body_summary=response_body_summary,
            attempt_id=attempt_id,
        )

    def _begin_rest_attempt(
        self,
        *,
        decision_epoch: dt.datetime,
        attempted_at: dt.datetime,
        path: str,
        params: Mapping[str, Any],
        symbol: str,
        sources: Sequence[str],
    ) -> str:
        return self.epoch_ledger.begin_attempt(
            collector_instance_id=self.config.collector_instance_id,
            decision_epoch=decision_epoch,
            attempted_at=attempted_at,
            request_identity=self._rest_request_identity(
                path=path,
                params=params,
                symbol=symbol,
                sources=sources,
            ),
        )

    @staticmethod
    def _rest_request_identity(
        *,
        path: str,
        params: Mapping[str, Any],
        symbol: str,
        sources: Sequence[str],
    ) -> dict[str, Any]:
        return {
            "base_url": REST_BASE_URL,
            "method": "GET",
            "params": dict(sorted(params.items())),
            "path": path,
            "sources": sorted(sources),
            "symbol": symbol,
        }

    def _count_result(self, source: str, inserted: bool) -> None:
        if inserted:
            self.session_counts[source] += 1
        else:
            self.duplicate_counts[source] += 1


def redact_sample(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return a compact, secret-free proof row with every PIT contract column."""
    return {column: row.get(column) for column in PIT_COLUMNS}
