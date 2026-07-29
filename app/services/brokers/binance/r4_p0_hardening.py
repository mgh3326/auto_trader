"""Immutable epoch ledger and deterministic finalizer for the R4 P0 collector.

This module contains infrastructure only.  It does not calculate research
features, scores, candidates, or stage decisions.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
import sqlite3
import urllib.parse
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

import httpx

log = logging.getLogger("r4_p0_collector")

EPOCH_HOURS: Final = 4
EPOCH_SECONDS: Final = EPOCH_HOURS * 60 * 60
DEFAULT_STUDY_ID: Final = "R4.1-DFC-4H"
DEFAULT_POLICY_HASH: Final = (
    "b3ee7db2f4cd8f76522a9c66ca8201177a01c24bbbd3876f53da4fb2f7c14a94"
)
DEFAULT_T0: Final = dt.datetime(2026, 7, 27, tzinfo=dt.UTC)
FINALIZER_VERSION: Final = "r4-p0-epoch-finalizer.v2"

FINAL_COMPLETE: Final = "FINAL_COMPLETE"
FINAL_MISSING: Final = "FINAL_MISSING"
FINAL_CONFLICT: Final = "FINAL_CONFLICT"
FinalStatus = Literal["FINAL_COMPLETE", "FINAL_MISSING", "FINAL_CONFLICT"]

TERMINAL_STATUSES: Final = frozenset(
    {
        "SUCCESS",
        "EXACT_DUPLICATE",
        "HTTP_ERROR",
        "TRANSPORT_ERROR",
        "INVALID_RESPONSE",
        "DEADLINE_EXPIRED",
    }
)

SOURCE_SCHEMA_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "binance_usdm.aggTrade": ("p", "q"),
    "binance_usdm.bookTicker": ("b", "B", "a", "A"),
    "binance_usdm.depth5": ("b", "a"),
    "binance_usdm.openInterest": ("openInterest",),
    "binance_usdm.openInterestHist": ("sumOpenInterest",),
    "binance_usdm.basis": ("basis",),
    "binance_usdm.takerLongShortRatio": (
        "buySellRatio",
        "buyVol",
        "sellVol",
    ),
    "binance_usdm.premiumIndex": ("markPrice", "indexPrice"),
    "binance_usdm.premiumIndexKline1m": (),
    "binance_usdm.predictedFunding": ("lastFundingRate",),
}
SOURCE_CADENCE_SECONDS: Final[dict[str, int]] = {
    "binance_usdm.basis": 5 * 60,
    "binance_usdm.openInterestHist": 5 * 60,
    "binance_usdm.premiumIndexKline1m": 60,
    "binance_usdm.takerLongShortRatio": 5 * 60,
}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def iso_utc(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("naive datetime is forbidden")
    return (
        value.astimezone(dt.UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def parse_utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("naive timestamp is forbidden")
    return parsed.astimezone(dt.UTC)


def floor_epoch(value: dt.datetime) -> dt.datetime:
    value = value.astimezone(dt.UTC)
    hour = value.hour - (value.hour % EPOCH_HOURS)
    return value.replace(hour=hour, minute=0, second=0, microsecond=0)


def decision_epoch_for_event(value: dt.datetime) -> dt.datetime:
    """Return e for the half-open source interval [e-4h, e).

    An event exactly on a four-hour boundary belongs to the next epoch.
    """

    return floor_epoch(value) + dt.timedelta(hours=EPOCH_HOURS)


def finalize_at(decision_epoch: dt.datetime) -> dt.datetime:
    return decision_epoch.astimezone(dt.UTC) + dt.timedelta(hours=EPOCH_HOURS)


def scheduled_epochs(
    start: dt.datetime, end_inclusive: dt.datetime
) -> Iterable[dt.datetime]:
    current = floor_epoch(start)
    end = floor_epoch(end_inclusive)
    while current <= end:
        yield current
        current += dt.timedelta(hours=EPOCH_HOURS)


def _trigger_sql(table: str) -> str:
    return f"""
    CREATE TRIGGER IF NOT EXISTS {table}_no_update
    BEFORE UPDATE ON {table}
    BEGIN
        SELECT RAISE(ABORT, '{table} is append-only');
    END;
    CREATE TRIGGER IF NOT EXISTS {table}_no_delete
    BEFORE DELETE ON {table}
    BEGIN
        SELECT RAISE(ABORT, '{table} is append-only');
    END;
    """


@dataclass(frozen=True, slots=True)
class EpochPolicy:
    required_sources: tuple[str, ...]
    symbols: tuple[str, ...]
    study_id: str = DEFAULT_STUDY_ID
    policy_hash: str = DEFAULT_POLICY_HASH
    t0: dt.datetime = DEFAULT_T0

    def __post_init__(self) -> None:
        if not self.required_sources or not self.symbols:
            raise ValueError("epoch policy requires sources and symbols")
        if tuple(sorted(set(self.required_sources))) != self.required_sources:
            raise ValueError("required_sources must be unique and sorted")
        if tuple(sorted(set(self.symbols))) != self.symbols:
            raise ValueError("symbols must be unique and sorted")
        if self.t0.tzinfo is None or floor_epoch(self.t0) != self.t0:
            raise ValueError("t0 must be an aware UTC four-hour boundary")

    @property
    def source_manifest_hash(self) -> str:
        return sha256_text(
            canonical_json(
                {
                    "required_sources": self.required_sources,
                    "source_schema_fields": {
                        source: SOURCE_SCHEMA_FIELDS.get(source, ())
                        for source in self.required_sources
                    },
                    "source_cadence_seconds": {
                        source: SOURCE_CADENCE_SECONDS[source]
                        for source in self.required_sources
                        if source in SOURCE_CADENCE_SECONDS
                    },
                    "symbols": self.symbols,
                }
            )
        )


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    study_id: str
    policy_hash: str
    symbol: str
    decision_epoch_utc: str
    finalize_at: str
    final_status: FinalStatus
    evaluation_hash: str
    missing_sources: tuple[str, ...]
    conflict_sources: tuple[str, ...]
    invalid_sources: tuple[str, ...]
    inserted: bool


class FinalizationInvariantError(RuntimeError):
    """Raised when immutable finalization disagrees with reconstructed input."""


class T0StartupGateError(RuntimeError):
    """Raised when a collector would violate the precommitted T0 contract."""


class EpochLedger:
    """Append-only operational ledger sharing the raw collector connection."""

    def __init__(self, connection: sqlite3.Connection, policy: EpochPolicy) -> None:
        self.db = connection
        self.db.row_factory = sqlite3.Row
        self.policy = policy
        self._configure()

    def _configure(self) -> None:
        tables = """
        CREATE TABLE IF NOT EXISTS epoch_source_events (
            append_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            study_id TEXT NOT NULL,
            policy_hash TEXT NOT NULL,
            source_name TEXT NOT NULL,
            symbol TEXT NOT NULL,
            decision_epoch_utc TEXT NOT NULL,
            finalize_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            collector_instance_id TEXT NOT NULL,
            details_sha256 TEXT NOT NULL,
            details_json TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_epoch_source_open
            ON epoch_source_events (
                study_id, policy_hash, source_name, symbol,
                decision_epoch_utc, event_type
            ) WHERE event_type = 'OPEN';

        CREATE TABLE IF NOT EXISTS collector_attempt_starts (
            append_id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id TEXT NOT NULL UNIQUE,
            study_id TEXT NOT NULL,
            policy_hash TEXT NOT NULL,
            collector_instance_id TEXT NOT NULL,
            decision_epoch_utc TEXT NOT NULL,
            finalize_at TEXT NOT NULL,
            attempted_at TEXT NOT NULL,
            request_identity_sha256 TEXT NOT NULL,
            request_identity_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_collector_attempt_start_epoch
            ON collector_attempt_starts (
                study_id, policy_hash, decision_epoch_utc, append_id
            );

        CREATE TABLE IF NOT EXISTS collector_attempts (
            append_id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id TEXT NOT NULL UNIQUE,
            study_id TEXT NOT NULL,
            policy_hash TEXT NOT NULL,
            collector_instance_id TEXT NOT NULL,
            decision_epoch_utc TEXT NOT NULL,
            finalize_at TEXT NOT NULL,
            attempted_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            request_identity_sha256 TEXT NOT NULL,
            request_identity_json TEXT NOT NULL,
            response_sha256 TEXT,
            terminal_status TEXT NOT NULL CHECK (
                terminal_status IN (
                    'SUCCESS', 'EXACT_DUPLICATE', 'HTTP_ERROR',
                    'TRANSPORT_ERROR', 'INVALID_RESPONSE',
                    'DEADLINE_EXPIRED'
                )
            ),
            error_type TEXT,
            error_message TEXT,
            error_traceback TEXT,
            response_body_summary TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_collector_attempt_epoch
            ON collector_attempts (
                study_id, policy_hash, decision_epoch_utc, append_id
            );

        CREATE TABLE IF NOT EXISTS symbol_epoch_finalizations (
            append_id INTEGER PRIMARY KEY AUTOINCREMENT,
            finalization_id TEXT NOT NULL UNIQUE,
            study_id TEXT NOT NULL,
            policy_hash TEXT NOT NULL,
            source_manifest_hash TEXT NOT NULL,
            symbol TEXT NOT NULL,
            decision_epoch_utc TEXT NOT NULL,
            finalize_at TEXT NOT NULL,
            finalized_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            final_status TEXT NOT NULL CHECK (
                final_status IN (
                    'FINAL_COMPLETE', 'FINAL_MISSING', 'FINAL_CONFLICT'
                )
            ),
            missing_sources_json TEXT NOT NULL,
            conflict_sources_json TEXT NOT NULL,
            invalid_sources_json TEXT NOT NULL,
            evaluation_hash TEXT NOT NULL,
            evaluation_json TEXT NOT NULL,
            finalizer_version TEXT NOT NULL,
            UNIQUE (study_id, policy_hash, symbol, decision_epoch_utc)
        );
        CREATE INDEX IF NOT EXISTS ix_symbol_epoch_status
            ON symbol_epoch_finalizations (
                study_id, policy_hash, final_status, decision_epoch_utc
            );

        CREATE TABLE IF NOT EXISTS late_only_corrections (
            append_id INTEGER PRIMARY KEY AUTOINCREMENT,
            correction_id TEXT NOT NULL UNIQUE,
            study_id TEXT NOT NULL,
            policy_hash TEXT NOT NULL,
            symbol TEXT NOT NULL,
            decision_epoch_utc TEXT NOT NULL,
            source_name TEXT NOT NULL,
            raw_record_id TEXT NOT NULL,
            raw_payload_sha256 TEXT NOT NULL,
            received_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            correction_status TEXT NOT NULL CHECK (
                correction_status = 'LATE_ONLY'
            ),
            UNIQUE (
                study_id, policy_hash, symbol, decision_epoch_utc,
                raw_record_id
            )
        );

        CREATE TABLE IF NOT EXISTS collector_heartbeats (
            append_id INTEGER PRIMARY KEY AUTOINCREMENT,
            heartbeat_id TEXT NOT NULL UNIQUE,
            study_id TEXT NOT NULL,
            policy_hash TEXT NOT NULL,
            collector_instance_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            health_sha256 TEXT NOT NULL,
            health_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_collector_heartbeat_latest
            ON collector_heartbeats (
                study_id, policy_hash, collector_instance_id, append_id
            );

        CREATE TABLE IF NOT EXISTS collector_process_versions (
            append_id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_stamp_id TEXT NOT NULL UNIQUE,
            study_id TEXT NOT NULL,
            policy_hash TEXT NOT NULL,
            collector_instance_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            collector_version TEXT NOT NULL,
            t0_utc TEXT,
            UNIQUE (study_id, policy_hash, collector_instance_id, run_id)
        );
        CREATE INDEX IF NOT EXISTS ix_collector_process_version_latest
            ON collector_process_versions (
                study_id, policy_hash, collector_instance_id, append_id
            );

        CREATE TABLE IF NOT EXISTS collector_alert_events (
            append_id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_event_id TEXT NOT NULL UNIQUE,
            alert_key TEXT NOT NULL,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            delivery_status TEXT,
            error_type TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_collector_alert_key
            ON collector_alert_events (alert_key, append_id);
        """
        self.db.executescript(
            tables
            + _trigger_sql("epoch_source_events")
            + _trigger_sql("collector_attempt_starts")
            + _trigger_sql("collector_attempts")
            + _trigger_sql("symbol_epoch_finalizations")
            + _trigger_sql("late_only_corrections")
            + _trigger_sql("collector_heartbeats")
            + _trigger_sql("collector_process_versions")
            + _trigger_sql("collector_alert_events")
        )
        attempt_columns = {
            row["name"]
            for row in self.db.execute("PRAGMA table_info(collector_attempts)")
        }
        for column in (
            "error_message",
            "error_traceback",
            "response_body_summary",
        ):
            if column not in attempt_columns:
                self.db.execute(
                    f"ALTER TABLE collector_attempts ADD COLUMN {column} TEXT"
                )
        process_version_columns = {
            row["name"]
            for row in self.db.execute("PRAGMA table_info(collector_process_versions)")
        }
        if "t0_utc" not in process_version_columns:
            self.db.execute(
                "ALTER TABLE collector_process_versions ADD COLUMN t0_utc TEXT"
            )
        self.db.commit()

    def validate_t0_startup(self, *, started_at: dt.datetime) -> None:
        """Fail closed before collection starts if the committed T0 is unsafe."""

        configured_t0 = iso_utc(self.policy.t0)
        stored_rows = self.db.execute(
            """
            SELECT DISTINCT t0_utc
            FROM collector_process_versions
            WHERE study_id = ? AND policy_hash = ? AND t0_utc IS NOT NULL
            ORDER BY t0_utc
            """,
            (self.policy.study_id, self.policy.policy_hash),
        ).fetchall()
        stored_t0_values = [row["t0_utc"] for row in stored_rows]
        mismatched_t0_values = [
            stored_t0 for stored_t0 in stored_t0_values if stored_t0 != configured_t0
        ]
        if mismatched_t0_values:
            raise T0StartupGateError(
                "G-T0-A refused collector startup: "
                f"stored_t0={','.join(mismatched_t0_values)} "
                f"configured_t0={configured_t0}"
            )

        has_pit_records = (
            self.db.execute("SELECT 1 FROM pit_records LIMIT 1").fetchone() is not None
        )
        t0_minus_4h = self.policy.t0 - dt.timedelta(hours=EPOCH_HOURS)
        if not has_pit_records and started_at > t0_minus_4h:
            raise T0StartupGateError(
                "G-T0-B refused fresh-artifact startup: "
                f"started_at={iso_utc(started_at)} "
                f"t0_minus_4h={iso_utc(t0_minus_4h)}; "
                "기존 T0 를 바꾸지 말고 새 커밋·새 T0 를 사전 고정하라"
            )

    def ensure_open_rows(
        self, decision_epoch: dt.datetime, collector_instance_id: str, now: dt.datetime
    ) -> None:
        epoch_text = iso_utc(decision_epoch)
        deadline_text = iso_utc(finalize_at(decision_epoch))
        recorded_at = iso_utc(now)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for symbol in self.policy.symbols:
                for source in self.policy.required_sources:
                    details = canonical_json(
                        {
                            "source_interval_end": epoch_text,
                            "source_interval_start": iso_utc(
                                decision_epoch - dt.timedelta(hours=EPOCH_HOURS)
                            ),
                        }
                    )
                    identity = canonical_json(
                        {
                            "decision_epoch_utc": epoch_text,
                            "event_type": "OPEN",
                            "policy_hash": self.policy.policy_hash,
                            "source_name": source,
                            "study_id": self.policy.study_id,
                            "symbol": symbol,
                        }
                    )
                    self.db.execute(
                        """
                        INSERT OR IGNORE INTO epoch_source_events (
                            event_id, study_id, policy_hash, source_name,
                            symbol, decision_epoch_utc, finalize_at, event_type,
                            recorded_at, collector_instance_id,
                            details_sha256, details_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?)
                        """,
                        (
                            sha256_text(identity),
                            self.policy.study_id,
                            self.policy.policy_hash,
                            source,
                            symbol,
                            epoch_text,
                            deadline_text,
                            recorded_at,
                            collector_instance_id,
                            sha256_text(details),
                            details,
                        ),
                    )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def begin_attempt(
        self,
        *,
        collector_instance_id: str,
        decision_epoch: dt.datetime,
        attempted_at: dt.datetime,
        request_identity: Mapping[str, Any],
    ) -> str:
        request_json = canonical_json(request_identity)
        attempt_id = uuid.uuid4().hex
        self.db.execute(
            """
            INSERT INTO collector_attempt_starts (
                attempt_id, study_id, policy_hash, collector_instance_id,
                decision_epoch_utc, finalize_at, attempted_at,
                request_identity_sha256, request_identity_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                self.policy.study_id,
                self.policy.policy_hash,
                collector_instance_id,
                iso_utc(decision_epoch),
                iso_utc(finalize_at(decision_epoch)),
                iso_utc(attempted_at),
                sha256_text(request_json),
                request_json,
            ),
        )
        self.db.commit()
        return attempt_id

    def append_attempt(
        self,
        *,
        collector_instance_id: str,
        decision_epoch: dt.datetime,
        attempted_at: dt.datetime,
        completed_at: dt.datetime,
        request_identity: Mapping[str, Any],
        response_sha256: str | None,
        terminal_status: str,
        error_type: str | None = None,
        error_message: str | None = None,
        error_traceback: str | None = None,
        response_body_summary: str | None = None,
        attempt_id: str | None = None,
    ) -> str:
        if terminal_status not in TERMINAL_STATUSES:
            raise ValueError(f"invalid terminal status: {terminal_status}")
        request_json = canonical_json(request_identity)
        if attempt_id is None:
            attempt_id = self.begin_attempt(
                collector_instance_id=collector_instance_id,
                decision_epoch=decision_epoch,
                attempted_at=attempted_at,
                request_identity=request_identity,
            )
        start = self.db.execute(
            """
            SELECT request_identity_sha256 FROM collector_attempt_starts
            WHERE attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if start is None or start["request_identity_sha256"] != sha256_text(
            request_json
        ):
            raise ValueError("attempt terminal identity does not match start")
        self.db.execute(
            """
            INSERT INTO collector_attempts (
                attempt_id, study_id, policy_hash, collector_instance_id,
                decision_epoch_utc, finalize_at, attempted_at, completed_at,
                request_identity_sha256, request_identity_json,
                response_sha256, terminal_status, error_type, error_message,
                error_traceback, response_body_summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                self.policy.study_id,
                self.policy.policy_hash,
                collector_instance_id,
                iso_utc(decision_epoch),
                iso_utc(finalize_at(decision_epoch)),
                iso_utc(attempted_at),
                iso_utc(completed_at),
                sha256_text(request_json),
                request_json,
                response_sha256,
                terminal_status,
                error_type,
                error_message,
                error_traceback,
                response_body_summary,
            ),
        )
        self.db.commit()
        return attempt_id

    def append_heartbeat(
        self,
        *,
        collector_instance_id: str,
        run_id: str,
        observed_at: dt.datetime,
        health: Mapping[str, Any],
    ) -> None:
        health_json = canonical_json(health)
        self.db.execute(
            """
            INSERT INTO collector_heartbeats (
                heartbeat_id, study_id, policy_hash, collector_instance_id,
                run_id, observed_at, health_sha256, health_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                self.policy.study_id,
                self.policy.policy_hash,
                collector_instance_id,
                run_id,
                iso_utc(observed_at),
                sha256_text(health_json),
                health_json,
            ),
        )
        self.db.commit()

    def append_process_version(
        self,
        *,
        collector_instance_id: str,
        run_id: str,
        started_at: dt.datetime,
        code_hash: str,
        collector_version: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO collector_process_versions (
                version_stamp_id, study_id, policy_hash, collector_instance_id,
                run_id, started_at, code_hash, collector_version, t0_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                self.policy.study_id,
                self.policy.policy_hash,
                collector_instance_id,
                run_id,
                iso_utc(started_at),
                code_hash,
                collector_version,
                iso_utc(self.policy.t0),
            ),
        )
        self.db.commit()

    def finalization_row(
        self, symbol: str, decision_epoch: dt.datetime
    ) -> sqlite3.Row | None:
        return self.db.execute(
            """
            SELECT * FROM symbol_epoch_finalizations
            WHERE study_id = ? AND policy_hash = ? AND symbol = ?
              AND decision_epoch_utc = ?
            """,
            (
                self.policy.study_id,
                self.policy.policy_hash,
                symbol,
                iso_utc(decision_epoch),
            ),
        ).fetchone()

    def append_finalization(
        self,
        *,
        symbol: str,
        decision_epoch: dt.datetime,
        recorded_at: dt.datetime,
        final_status: FinalStatus,
        missing_sources: Sequence[str],
        conflict_sources: Sequence[str],
        invalid_sources: Sequence[str],
        evaluation_hash: str,
        evaluation_json: str,
    ) -> None:
        identity = canonical_json(
            {
                "decision_epoch_utc": iso_utc(decision_epoch),
                "policy_hash": self.policy.policy_hash,
                "study_id": self.policy.study_id,
                "symbol": symbol,
            }
        )
        deadline = finalize_at(decision_epoch)
        self.db.execute(
            """
            INSERT INTO symbol_epoch_finalizations (
                finalization_id, study_id, policy_hash,
                source_manifest_hash, symbol, decision_epoch_utc,
                finalize_at, finalized_at, recorded_at, final_status,
                missing_sources_json, conflict_sources_json,
                invalid_sources_json, evaluation_hash, evaluation_json,
                finalizer_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sha256_text(identity),
                self.policy.study_id,
                self.policy.policy_hash,
                self.policy.source_manifest_hash,
                symbol,
                iso_utc(decision_epoch),
                iso_utc(deadline),
                iso_utc(deadline),
                iso_utc(recorded_at),
                final_status,
                canonical_json(list(missing_sources)),
                canonical_json(list(conflict_sources)),
                canonical_json(list(invalid_sources)),
                evaluation_hash,
                evaluation_json,
                FINALIZER_VERSION,
            ),
        )
        self.db.commit()

    def alert_event_exists(
        self, alert_key: str, event_type: str, delivery_status: str | None = None
    ) -> bool:
        sql = """
            SELECT 1 FROM collector_alert_events
            WHERE alert_key = ? AND event_type = ?
        """
        params: list[str] = [alert_key, event_type]
        if delivery_status is not None:
            sql += " AND delivery_status = ?"
            params.append(delivery_status)
        sql += " LIMIT 1"
        return self.db.execute(sql, params).fetchone() is not None

    def append_alert_event(
        self,
        *,
        alert_key: str,
        event_type: str,
        severity: str,
        recorded_at: dt.datetime,
        payload: Mapping[str, Any],
        delivery_status: str | None = None,
        error_type: str | None = None,
    ) -> None:
        payload_json = canonical_json(payload)
        self.db.execute(
            """
            INSERT INTO collector_alert_events (
                alert_event_id, alert_key, event_type, severity, recorded_at,
                payload_sha256, payload_json, delivery_status, error_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                alert_key,
                event_type,
                severity,
                iso_utc(recorded_at),
                sha256_text(payload_json),
                payload_json,
                delivery_status,
                error_type,
            ),
        )
        self.db.commit()


class RawPITReadError(RuntimeError):
    """A required local or replica PIT artifact could not be read."""


class RawPITReader:
    """Deterministically merge the local raw ledger and read-only replicas."""

    def __init__(
        self,
        local_connection: sqlite3.Connection,
        local_path: Path,
        replica_paths: Sequence[Path] = (),
    ) -> None:
        self.local_connection = local_connection
        self.local_path = local_path.resolve()
        self.replica_paths = tuple(
            sorted(
                {
                    path.expanduser().resolve()
                    for path in replica_paths
                    if path.expanduser().resolve() != self.local_path
                },
                key=str,
            )
        )

    @staticmethod
    def _query(
        connection: sqlite3.Connection,
        *,
        symbol: str,
        interval_start: str,
        interval_end: str,
        deadline: str | None,
    ) -> list[dict[str, Any]]:
        connection.row_factory = sqlite3.Row
        deadline_clause = "AND local_receive_time <= ?" if deadline is not None else ""
        params: list[str] = [symbol, interval_start, interval_end]
        if deadline is not None:
            params.append(deadline)
        rows = connection.execute(
            f"""
            SELECT record_id, source, symbol, event_time, transaction_time,
                   local_receive_time, sequence_or_trade_id,
                   raw_payload_sha256, gap_detected, run_id, raw_payload
            FROM pit_records
            WHERE symbol = ?
              AND event_time >= ?
              AND event_time < ?
              {deadline_clause}
            """,
            params,
        )
        return [dict(row) for row in rows]

    def rows_for_epoch(
        self,
        *,
        symbol: str,
        decision_epoch: dt.datetime,
        received_by: dt.datetime | None,
    ) -> list[dict[str, Any]]:
        interval_start = iso_utc(decision_epoch - dt.timedelta(hours=EPOCH_HOURS))
        interval_end = iso_utc(decision_epoch)
        deadline = iso_utc(received_by) if received_by is not None else None
        try:
            rows = self._query(
                self.local_connection,
                symbol=symbol,
                interval_start=interval_start,
                interval_end=interval_end,
                deadline=deadline,
            )
        except sqlite3.Error as exc:
            raise RawPITReadError(
                f"failed to read local PIT artifact {self.local_path}: {exc}"
            ) from exc
        for path in self.replica_paths:
            if not path.is_file():
                continue
            uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
            try:
                with sqlite3.connect(uri, uri=True) as replica:
                    rows.extend(
                        self._query(
                            replica,
                            symbol=symbol,
                            interval_start=interval_start,
                            interval_end=interval_end,
                            deadline=deadline,
                        )
                    )
            except sqlite3.Error as exc:
                raise RawPITReadError(
                    f"failed to read replica PIT artifact {path}: {exc}"
                ) from exc
        return rows


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _finite_depth(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for level in value:
        if (
            not isinstance(level, list)
            or len(level) < 2
            or not _finite_number(level[0])
            or not _finite_number(level[1])
        ):
            return False
    return True


def _payload_valid(source: str, payload: Any) -> bool:
    if source == "binance_usdm.premiumIndexKline1m":
        return (
            isinstance(payload, list)
            and len(payload) >= 7
            and all(_finite_number(payload[index]) for index in range(1, 6))
        )
    if not isinstance(payload, dict):
        return False
    fields = SOURCE_SCHEMA_FIELDS.get(source)
    if fields is None:
        return False
    for field in fields:
        value = payload.get(field)
        if source == "binance_usdm.depth5" and field in {"a", "b"}:
            if not _finite_depth(value):
                return False
        elif not _finite_number(value):
            return False
    return True


def _source_identity(row: Mapping[str, Any]) -> str:
    source_event_id = row.get("sequence_or_trade_id")
    if source_event_id is not None:
        identity = {
            "source_event_id": str(source_event_id),
            "source_name": row["source"],
            "symbol": row["symbol"],
        }
    else:
        identity = {
            "record_type": "PIT_OBSERVATION",
            "source_event_time": row.get("event_time"),
            "source_interval_end": row.get("event_time"),
            "source_interval_start": row.get("event_time"),
            "source_name": row["source"],
            "symbol": row["symbol"],
        }
    return sha256_text(canonical_json(identity))


def _observation_slot(
    event_time: Any,
    *,
    interval_start: dt.datetime,
    cadence_seconds: int,
) -> int | None:
    if not isinstance(event_time, str):
        return None
    try:
        observed_at = parse_utc(event_time)
    except ValueError:
        return None
    offset_seconds = (observed_at - interval_start).total_seconds()
    if not 0 <= offset_seconds < EPOCH_SECONDS:
        return None
    return int(offset_seconds // cadence_seconds)


class DeterministicEpochFinalizer:
    def __init__(self, ledger: EpochLedger, raw_reader: RawPITReader) -> None:
        self.ledger = ledger
        self.raw_reader = raw_reader

    def _evaluate(
        self, symbol: str, decision_epoch: dt.datetime
    ) -> tuple[
        FinalStatus,
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
        str,
        str,
    ]:
        deadline = finalize_at(decision_epoch)
        interval_start = decision_epoch - dt.timedelta(hours=EPOCH_HOURS)
        rows = self.raw_reader.rows_for_epoch(
            symbol=symbol,
            decision_epoch=decision_epoch,
            received_by=deadline,
        )
        by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if row["source"] in self.ledger.policy.required_sources:
                by_source[row["source"]].append(row)

        missing: list[str] = []
        conflicts: list[str] = []
        invalid: list[str] = []
        source_evidence: dict[str, Any] = {}
        for source in self.ledger.policy.required_sources:
            source_rows = by_source.get(source, [])
            identities: dict[str, set[str]] = defaultdict(set)
            evidence_rows: set[tuple[str, str]] = set()
            valid_evidence: set[tuple[str, str]] = set()
            invalid_evidence: set[tuple[str, str]] = set()
            gap_flags: dict[tuple[str, str], set[bool]] = defaultdict(set)
            evidence_slots: dict[tuple[str, str], int] = {}
            for row in source_rows:
                identity = _source_identity(row)
                payload_hash = str(row["raw_payload_sha256"])
                evidence_key = (identity, payload_hash)
                identities[identity].add(payload_hash)
                evidence_rows.add(evidence_key)
                gap_flags[evidence_key].add(bool(row["gap_detected"]))
                try:
                    payload = json.loads(row["raw_payload"])
                except (json.JSONDecodeError, TypeError):
                    invalid_evidence.add(evidence_key)
                    continue
                if sha256_text(
                    canonical_json(payload)
                ) != payload_hash or not _payload_valid(source, payload):
                    invalid_evidence.add(evidence_key)
                else:
                    valid_evidence.add(evidence_key)
                    cadence_seconds = SOURCE_CADENCE_SECONDS.get(source)
                    if cadence_seconds is not None:
                        slot = _observation_slot(
                            row.get("event_time"),
                            interval_start=interval_start,
                            cadence_seconds=cadence_seconds,
                        )
                        if slot is not None:
                            evidence_slots[evidence_key] = slot
            uncovered_gaps = sorted(
                [identity, payload_hash]
                for (identity, payload_hash), flags in gap_flags.items()
                if flags == {True}
            )
            conflicting_ids = sorted(
                identity for identity, hashes in identities.items() if len(hashes) > 1
            )
            cadence_seconds = SOURCE_CADENCE_SECONDS.get(source)
            expected_observations = (
                EPOCH_SECONDS // cadence_seconds
                if cadence_seconds is not None
                else None
            )
            covered_slots = sorted(
                {
                    slot
                    for evidence_key, slot in evidence_slots.items()
                    if evidence_key in valid_evidence
                    and gap_flags[evidence_key] != {True}
                }
            )
            missing_slots = (
                sorted(set(range(expected_observations)) - set(covered_slots))
                if expected_observations is not None
                else []
            )
            if conflicting_ids:
                conflicts.append(source)
                state = "CONFLICT"
            elif not source_rows:
                missing.append(source)
                state = "MISSING"
            elif (
                invalid_evidence
                or uncovered_gaps
                or not valid_evidence
                or missing_slots
            ):
                invalid.append(source)
                state = "INVALID"
            else:
                state = "COMPLETE"
            source_evidence[source] = {
                "conflicting_source_identities": conflicting_ids,
                "covered_observation_count": (
                    len(covered_slots) if expected_observations is not None else None
                ),
                "expected_observation_count": expected_observations,
                "invalid_source_identity_payload_hashes": [
                    [identity, payload_hash]
                    for identity, payload_hash in sorted(invalid_evidence)
                ],
                "missing_observation_slots": [
                    iso_utc(
                        interval_start + dt.timedelta(seconds=slot * cadence_seconds)
                    )
                    for slot in missing_slots
                ]
                if cadence_seconds is not None
                else [],
                "source_identity_payload_hashes": [
                    [identity, payload_hash]
                    for identity, payload_hash in sorted(evidence_rows)
                ],
                "state": state,
                "uncovered_gap_identity_payload_hashes": uncovered_gaps,
                "valid_source_identity_payload_hashes": [
                    [identity, payload_hash]
                    for identity, payload_hash in sorted(valid_evidence)
                ],
            }

        if conflicts:
            status: FinalStatus = FINAL_CONFLICT
        elif missing or invalid:
            status = FINAL_MISSING
        else:
            status = FINAL_COMPLETE
        evaluation = {
            "decision_epoch_utc": iso_utc(decision_epoch),
            "final_status": status,
            "finalize_at": iso_utc(deadline),
            "finalizer_version": FINALIZER_VERSION,
            "policy_hash": self.ledger.policy.policy_hash,
            "source_evidence": source_evidence,
            "source_manifest_hash": self.ledger.policy.source_manifest_hash,
            "study_id": self.ledger.policy.study_id,
            "symbol": symbol,
        }
        evaluation_json = canonical_json(evaluation)
        return (
            status,
            tuple(missing),
            tuple(conflicts),
            tuple(invalid),
            sha256_text(evaluation_json),
            evaluation_json,
        )

    def preview(self, symbol: str, decision_epoch: dt.datetime) -> FinalizationResult:
        status, missing, conflicts, invalid, digest, _ = self._evaluate(
            symbol, decision_epoch
        )
        return FinalizationResult(
            study_id=self.ledger.policy.study_id,
            policy_hash=self.ledger.policy.policy_hash,
            symbol=symbol,
            decision_epoch_utc=iso_utc(decision_epoch),
            finalize_at=iso_utc(finalize_at(decision_epoch)),
            final_status=status,
            evaluation_hash=digest,
            missing_sources=missing,
            conflict_sources=conflicts,
            invalid_sources=invalid,
            inserted=False,
        )

    def finalize(
        self,
        symbol: str,
        decision_epoch: dt.datetime,
        *,
        observed_at: dt.datetime,
    ) -> FinalizationResult:
        deadline = finalize_at(decision_epoch)
        if observed_at < deadline:
            raise ValueError(f"cannot finalize before {iso_utc(deadline)}")
        status, missing, conflicts, invalid, digest, evaluation_json = self._evaluate(
            symbol, decision_epoch
        )
        existing = self.ledger.finalization_row(symbol, decision_epoch)
        if existing is not None:
            if (
                existing["evaluation_hash"] != digest
                or existing["final_status"] != status
            ):
                raise FinalizationInvariantError(
                    "reconstructed input disagrees with immutable finalization "
                    f"for {symbol} {iso_utc(decision_epoch)}"
                )
            return FinalizationResult(
                study_id=self.ledger.policy.study_id,
                policy_hash=self.ledger.policy.policy_hash,
                symbol=symbol,
                decision_epoch_utc=iso_utc(decision_epoch),
                finalize_at=iso_utc(deadline),
                final_status=status,
                evaluation_hash=digest,
                missing_sources=missing,
                conflict_sources=conflicts,
                invalid_sources=invalid,
                inserted=False,
            )
        self.ledger.append_finalization(
            symbol=symbol,
            decision_epoch=decision_epoch,
            recorded_at=observed_at,
            final_status=status,
            missing_sources=missing,
            conflict_sources=conflicts,
            invalid_sources=invalid,
            evaluation_hash=digest,
            evaluation_json=evaluation_json,
        )
        return FinalizationResult(
            study_id=self.ledger.policy.study_id,
            policy_hash=self.ledger.policy.policy_hash,
            symbol=symbol,
            decision_epoch_utc=iso_utc(decision_epoch),
            finalize_at=iso_utc(deadline),
            final_status=status,
            evaluation_hash=digest,
            missing_sources=missing,
            conflict_sources=conflicts,
            invalid_sources=invalid,
            inserted=True,
        )

    def append_late_only(
        self, symbol: str, decision_epoch: dt.datetime, *, recorded_at: dt.datetime
    ) -> int:
        if self.ledger.finalization_row(symbol, decision_epoch) is None:
            return 0
        deadline = finalize_at(decision_epoch)
        rows = self.raw_reader.rows_for_epoch(
            symbol=symbol,
            decision_epoch=decision_epoch,
            received_by=None,
        )
        inserted = 0
        for row in sorted(rows, key=lambda item: str(item["record_id"])):
            if parse_utc(row["local_receive_time"]) <= deadline:
                continue
            correction_identity = canonical_json(
                {
                    "decision_epoch_utc": iso_utc(decision_epoch),
                    "policy_hash": self.ledger.policy.policy_hash,
                    "raw_record_id": row["record_id"],
                    "study_id": self.ledger.policy.study_id,
                    "symbol": symbol,
                }
            )
            cursor = self.ledger.db.execute(
                """
                INSERT OR IGNORE INTO late_only_corrections (
                    correction_id, study_id, policy_hash, symbol,
                    decision_epoch_utc, source_name, raw_record_id,
                    raw_payload_sha256, received_at, recorded_at,
                    correction_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'LATE_ONLY')
                """,
                (
                    sha256_text(correction_identity),
                    self.ledger.policy.study_id,
                    self.ledger.policy.policy_hash,
                    symbol,
                    iso_utc(decision_epoch),
                    row["source"],
                    row["record_id"],
                    row["raw_payload_sha256"],
                    row["local_receive_time"],
                    iso_utc(recorded_at),
                ),
            )
            inserted += cursor.rowcount
        self.ledger.db.commit()
        return inserted


class AlertDispatcher:
    """Durably log alerts and optionally deliver them to an HTTPS webhook."""

    def __init__(self, ledger: EpochLedger, webhook_urls: Sequence[str] = ()) -> None:
        self.ledger = ledger
        self.webhook_urls = tuple(webhook_urls)
        for url in self.webhook_urls:
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError("alert webhooks must use HTTPS")

    async def emit(
        self,
        *,
        alert_key: str,
        severity: str,
        payload: Mapping[str, Any],
        now: dt.datetime,
    ) -> None:
        if not self.ledger.alert_event_exists(alert_key, "ALERT_RAISED"):
            self.ledger.append_alert_event(
                alert_key=alert_key,
                event_type="ALERT_RAISED",
                severity=severity,
                recorded_at=now,
                payload=payload,
                delivery_status="PERSISTED",
            )
        if not self.ledger.alert_event_exists(alert_key, "LOG_DELIVERY", "SUCCEEDED"):
            log.critical(
                "collector.alert key=%s severity=%s payload=%s",
                alert_key,
                severity,
                canonical_json(payload),
            )
            self.ledger.append_alert_event(
                alert_key=alert_key,
                event_type="LOG_DELIVERY",
                severity=severity,
                recorded_at=now,
                payload=payload,
                delivery_status="SUCCEEDED",
            )
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0), follow_redirects=False
        ) as client:
            for index, url in enumerate(self.webhook_urls):
                delivery_type = f"WEBHOOK_{index}"
                if self.ledger.alert_event_exists(
                    alert_key, delivery_type, "SUCCEEDED"
                ):
                    continue
                try:
                    response = await client.post(url, json=dict(payload))
                    response.raise_for_status()
                except Exception as exc:
                    self.ledger.append_alert_event(
                        alert_key=alert_key,
                        event_type=delivery_type,
                        severity=severity,
                        recorded_at=now,
                        payload=payload,
                        delivery_status="FAILED",
                        error_type=type(exc).__name__,
                    )
                else:
                    self.ledger.append_alert_event(
                        alert_key=alert_key,
                        event_type=delivery_type,
                        severity=severity,
                        recorded_at=now,
                        payload=payload,
                        delivery_status="SUCCEEDED",
                    )


def latest_heartbeat(path: Path, policy: EpochPolicy) -> dict[str, Any] | None:
    path = path.expanduser().resolve()
    if not path.is_file():
        return None
    uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT * FROM collector_heartbeats
                WHERE study_id = ? AND policy_hash = ?
                ORDER BY append_id DESC LIMIT 1
                """,
                (policy.study_id, policy.policy_hash),
            ).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row is not None else None


def latest_process_version(
    path: Path,
    policy: EpochPolicy,
    *,
    run_id: str | None = None,
) -> dict[str, Any] | None:
    path = path.expanduser().resolve()
    if not path.is_file():
        return None
    uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            sql = """
                SELECT * FROM collector_process_versions
                WHERE study_id = ? AND policy_hash = ?
            """
            params = [policy.study_id, policy.policy_hash]
            if run_id is not None:
                sql += " AND run_id = ?"
                params.append(run_id)
            sql += " ORDER BY append_id DESC LIMIT 1"
            row = connection.execute(sql, params).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row is not None else None


def availability_report(
    artifact_paths: Sequence[Path],
    policy: EpochPolicy,
    *,
    observed_at: dt.datetime,
    stale_after_seconds: float,
    expected_code_hash: str | None = None,
) -> dict[str, Any]:
    replicas: list[dict[str, Any]] = []
    fresh_instance_ids: set[str] = set()
    instance_path_counts: dict[str, int] = defaultdict(int)
    unstamped_artifacts: list[str] = []
    version_mismatches: list[dict[str, str]] = []
    for path in sorted(
        {item.expanduser().resolve() for item in artifact_paths}, key=str
    ):
        heartbeat = latest_heartbeat(path, policy)
        if heartbeat is None:
            replicas.append({"artifact": str(path), "status": "UNAVAILABLE"})
            if expected_code_hash is not None:
                unstamped_artifacts.append(str(path))
            continue
        age = (observed_at - parse_utc(heartbeat["observed_at"])).total_seconds()
        status = "HEALTHY" if 0 <= age <= stale_after_seconds else "STALE"
        version = latest_process_version(path, policy, run_id=heartbeat["run_id"])
        if expected_code_hash is not None:
            if version is None:
                status = "VERSION_UNSTAMPED"
                unstamped_artifacts.append(str(path))
            elif version["code_hash"] != expected_code_hash:
                status = "VERSION_MISMATCH"
                version_mismatches.append(
                    {
                        "actual_code_hash": version["code_hash"],
                        "artifact": str(path),
                        "expected_code_hash": expected_code_hash,
                    }
                )
        if status == "HEALTHY":
            fresh_instance_ids.add(heartbeat["collector_instance_id"])
            instance_path_counts[heartbeat["collector_instance_id"]] += 1
        replica = {
            "age_seconds": age,
            "artifact": str(path),
            "collector_instance_id": heartbeat["collector_instance_id"],
            "observed_at": heartbeat["observed_at"],
            "status": status,
        }
        if version is not None:
            replica.update(
                {
                    "code_hash": version["code_hash"],
                    "collector_version": version["collector_version"],
                    "version_run_id": version["run_id"],
                    "version_started_at": version["started_at"],
                }
            )
        replicas.append(replica)
    return {
        "duplicate_collector_instance_ids": sorted(
            instance_id
            for instance_id, count in instance_path_counts.items()
            if count > 1
        ),
        "expected_code_hash": expected_code_hash,
        "healthy_replica_count": len(fresh_instance_ids),
        "observed_at": iso_utc(observed_at),
        "replicas": replicas,
        "unstamped_artifacts": sorted(set(unstamped_artifacts)),
        "version_mismatches": version_mismatches,
        "version_stamp_match": (
            None
            if expected_code_hash is None
            else not unstamped_artifacts and not version_mismatches
        ),
    }


def finalization_report(
    artifact_paths: Sequence[Path],
    policy: EpochPolicy,
    *,
    decision_epoch: dt.datetime,
) -> dict[str, Any]:
    """Read finalization rows from replicas without mutating any collector DB."""

    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    unavailable_artifacts: list[str] = []
    for path in sorted(
        {item.expanduser().resolve() for item in artifact_paths}, key=str
    ):
        if not path.is_file():
            unavailable_artifacts.append(str(path))
            continue
        uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT symbol, final_status, evaluation_hash, recorded_at
                    FROM symbol_epoch_finalizations
                    WHERE study_id = ? AND policy_hash = ?
                      AND decision_epoch_utc = ?
                    """,
                    (
                        policy.study_id,
                        policy.policy_hash,
                        iso_utc(decision_epoch),
                    ),
                )
                for row in rows:
                    key = (row["symbol"], row["evaluation_hash"])
                    rows_by_key[key] = {
                        "evaluation_hash": row["evaluation_hash"],
                        "final_status": row["final_status"],
                        "recorded_at": row["recorded_at"],
                        "symbol": row["symbol"],
                    }
        except sqlite3.Error:
            unavailable_artifacts.append(str(path))
    rows = sorted(
        rows_by_key.values(),
        key=lambda item: (
            item["symbol"],
            item["evaluation_hash"],
        ),
    )
    symbols_present = {row["symbol"] for row in rows}
    divergent_symbols = sorted(
        symbol
        for symbol in policy.symbols
        if len({row["evaluation_hash"] for row in rows if row["symbol"] == symbol}) > 1
    )
    return {
        "decision_epoch_utc": iso_utc(decision_epoch),
        "divergent_symbols": divergent_symbols,
        "finalizations": rows,
        "missing_symbols": sorted(set(policy.symbols) - symbols_present),
        "unavailable_artifacts": unavailable_artifacts,
    }
