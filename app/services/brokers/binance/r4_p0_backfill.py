"""R4 P0 historical seed backfill from production-public Binance USD-M REST.

The backfill is deliberately separate from the live PIT collector. It reuses
the live ``pit_records`` schema, but writes only ``r4_p0_backfill.sqlite3`` and
labels every row and the artifact itself as historical backfill provenance.
``local_receive_time`` is therefore the backfill HTTP response time, never the
historical live receive time.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import sqlite3
import urllib.parse
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

import httpx

from app.services.brokers.binance.r4_p0_collector import (
    REST_BASE_URL,
    SIGNAL_SYMBOLS,
    SYMBOLS,
    AppendOnlyPITStore,
    epoch_ms,
    iso_utc,
    utc_now,
)

BACKFILL_VERSION: Final = "r4-p0-seed-backfill.v1"
BACKFILL_DB_FILENAME: Final = "r4_p0_backfill.sqlite3"
BACKFILL_SOURCE_SUFFIX: Final = ".backfill"
KLINE_SOURCE: Final = f"binance_usdm.klines4h{BACKFILL_SOURCE_SUFFIX}"
PREMIUM_SOURCE: Final = f"binance_usdm.premiumIndexKlines4h{BACKFILL_SOURCE_SUFFIX}"
OI_SOURCE: Final = f"binance_usdm.openInterestHist{BACKFILL_SOURCE_SUFFIX}"
LIVE_OI_SOURCE: Final = "binance_usdm.openInterest"
BACKFILL_REST_PATH_ALLOWLIST: Final = frozenset(
    {
        "/fapi/v1/klines",
        "/fapi/v1/premiumIndexKlines",
        "/futures/data/openInterestHist",
    }
)
FOUR_H_MS: Final = 4 * 60 * 60 * 1000
FIVE_M_MS: Final = 5 * 60 * 1000
TARGET_EPOCHS: Final = 252
OI_PAGE_LIMIT: Final = 500
OI_MAX_PAGES: Final = 24
OI_MATCH_TOLERANCE_MS: Final = FIVE_M_MS
OI_INTEGRITY_THRESHOLD: Final = Decimal("0.01")
USD_M_REQUEST_WEIGHT_LIMIT_1M: Final = 2400
OI_REQUEST_LIMIT_5M: Final = 1000

log = logging.getLogger("r4_p0_backfill")


def assert_backfill_rest_target(path: str) -> None:
    """Allow only unsigned public GET paths on production USD-M."""

    parsed = urllib.parse.urlparse(f"{REST_BASE_URL}{path}")
    if (
        parsed.scheme != "https"
        or parsed.hostname != "fapi.binance.com"
        or parsed.path not in BACKFILL_REST_PATH_ALLOWLIST
    ):
        raise ValueError(
            "blocked Binance backfill REST target: "
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        )


def floor_utc_4h_ms(value: dt.datetime) -> int:
    epoch = int(value.astimezone(dt.UTC).timestamp() * 1000)
    return epoch - (epoch % FOUR_H_MS)


def floor_utc_5m_ms(value: dt.datetime) -> int:
    epoch = int(value.astimezone(dt.UTC).timestamp() * 1000)
    return epoch - (epoch % FIVE_M_MS)


def kline_ip_weight(limit: int) -> int:
    """Official USD-M kline/premium-kline weight table."""

    if limit < 1:
        raise ValueError("limit must be positive")
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


@dataclass(frozen=True, slots=True)
class BackfillConfig:
    artifact_root: Path
    symbols: tuple[str, ...] = SYMBOLS
    target_epochs: int = TARGET_EPOCHS
    request_delay_seconds: float = 0.35
    request_timeout_seconds: float = 20.0
    oi_max_pages: int = OI_MAX_PAGES


@dataclass(slots=True)
class RateLimitUsage:
    request_count: int = 0
    endpoint_requests: Counter[str] = field(default_factory=Counter)
    status_counts: Counter[int] = field(default_factory=Counter)
    estimated_ip_weight: int = 0
    oi_requests_5m: int = 0
    used_weight_headers: list[int] = field(default_factory=list)
    responses_429: int = 0
    responses_418: int = 0

    def observe(
        self,
        *,
        path: str,
        params: Mapping[str, Any],
        response: httpx.Response,
    ) -> None:
        self.request_count += 1
        self.endpoint_requests[path] += 1
        self.status_counts[response.status_code] += 1
        if path in {"/fapi/v1/klines", "/fapi/v1/premiumIndexKlines"}:
            self.estimated_ip_weight += kline_ip_weight(int(params["limit"]))
        elif path == "/futures/data/openInterestHist":
            # Official contract: IP weight 0, separate 1000 requests / 5 min.
            self.oi_requests_5m += 1
        header = response.headers.get("x-mbx-used-weight-1m")
        if header is not None:
            try:
                self.used_weight_headers.append(int(header))
            except ValueError:
                pass
        if response.status_code == 429:
            self.responses_429 += 1
        elif response.status_code == 418:
            self.responses_418 += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_count": self.request_count,
            "endpoint_requests": dict(sorted(self.endpoint_requests.items())),
            "status_counts": {
                str(code): count for code, count in sorted(self.status_counts.items())
            },
            "estimated_ip_weight": self.estimated_ip_weight,
            "ip_weight_limit_1m": USD_M_REQUEST_WEIGHT_LIMIT_1M,
            "oi_requests_5m": self.oi_requests_5m,
            "oi_request_limit_5m": OI_REQUEST_LIMIT_5M,
            "observed_used_weight_1m_min": (
                min(self.used_weight_headers) if self.used_weight_headers else None
            ),
            "observed_used_weight_1m_max": (
                max(self.used_weight_headers) if self.used_weight_headers else None
            ),
            "responses_429": self.responses_429,
            "responses_418": self.responses_418,
        }


class BackfillPITStore(AppendOnlyPITStore):
    """Same immutable PIT row schema, in a provenance-sealed backfill file."""

    _PROVENANCE: Final = {
        "artifact_kind": "historical_rest_backfill",
        "live_artifact": "false",
        "production_database": "false",
        "source_host": "fapi.binance.com",
        "http_method": "GET",
        "authentication": "none",
        "local_receive_time_semantics": (
            "backfill_http_response_completion_time_not_historical_live_receive_time"
        ),
        "oi_grid": "5m",
        "oi_boundary_tolerance": "plus_or_minus_5m_inclusive",
        "oi_precedence": "live_poll_nearest_then_openInterestHist_backfill",
        "oi_overlap_integrity_threshold": "relative_difference_gt_1pct",
        "ofi_definition": (
            "complete_4h_kline_taker_buy_base_over_total_minus_taker_buy_base"
        ),
    }

    def __init__(self, root: Path) -> None:
        super().__init__(
            root,
            collector_version=BACKFILL_VERSION,
            artifact_filename=BACKFILL_DB_FILENAME,
            lock_filename=".backfill.lock",
        )
        self._configure_provenance()

    def _configure_provenance(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS artifact_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS artifact_metadata_no_update
            BEFORE UPDATE ON artifact_metadata
            BEGIN
                SELECT RAISE(ABORT, 'artifact_metadata is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS artifact_metadata_no_delete
            BEFORE DELETE ON artifact_metadata
            BEGIN
                SELECT RAISE(ABORT, 'artifact_metadata is immutable');
            END;
            """
        )
        for key, value in self._PROVENANCE.items():
            existing = self._db.execute(
                "SELECT value FROM artifact_metadata WHERE key = ?", (key,)
            ).fetchone()
            if existing is not None and existing["value"] != value:
                raise RuntimeError(f"backfill provenance mismatch for {key}")
            if existing is None:
                self._db.execute(
                    "INSERT INTO artifact_metadata(key, value) VALUES (?, ?)",
                    (key, value),
                )
        self._db.commit()

    def provenance(self) -> dict[str, str]:
        return {
            row["key"]: row["value"]
            for row in self._db.execute(
                "SELECT key, value FROM artifact_metadata ORDER BY key"
            )
        }

    def seal_run_evidence(self, run_id: str, evidence: Mapping[str, Any]) -> None:
        key = f"run_evidence/{run_id}"
        self._db.execute(
            "INSERT INTO artifact_metadata(key, value) VALUES (?, ?)",
            (key, json.dumps(evidence, separators=(",", ":"), sort_keys=True)),
        )
        self._db.commit()


@dataclass(frozen=True, slots=True)
class OIObservation:
    timestamp_ms: int
    value: Decimal
    provenance: str


@dataclass(frozen=True, slots=True)
class OIBoundarySelection:
    observation: OIObservation | None
    integrity_flag: bool
    live_backfill_relative_difference: Decimal | None


def _nearest_oi(
    observations: Iterable[OIObservation], boundary_ms: int
) -> OIObservation | None:
    eligible = [
        item
        for item in observations
        if abs(item.timestamp_ms - boundary_ms) <= OI_MATCH_TOLERANCE_MS
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda item: (abs(item.timestamp_ms - boundary_ms), -item.timestamp_ms),
    )


def select_oi_boundary_observation(
    *,
    boundary_ms: int,
    live: Sequence[OIObservation],
    backfill: Sequence[OIObservation],
) -> OIBoundarySelection:
    """Apply frozen ±5m tolerance, live priority, and 1% overlap flag."""

    nearest_live = _nearest_oi(live, boundary_ms)
    nearest_backfill = _nearest_oi(backfill, boundary_ms)
    relative_difference: Decimal | None = None
    integrity_flag = False
    if nearest_live is not None and nearest_backfill is not None:
        denominator = abs(nearest_live.value)
        if denominator == 0:
            relative_difference = (
                Decimal(0) if nearest_backfill.value == 0 else Decimal("Infinity")
            )
        else:
            relative_difference = (
                abs(nearest_live.value - nearest_backfill.value) / denominator
            )
        integrity_flag = relative_difference > OI_INTEGRITY_THRESHOLD
    return OIBoundarySelection(
        observation=nearest_live or nearest_backfill,
        integrity_flag=integrity_flag,
        live_backfill_relative_difference=relative_difference,
    )


def _parse_json_row(row: sqlite3.Row) -> Any:
    return json.loads(row["raw_payload"])


def _source_rows(
    store: BackfillPITStore, *, source: str, symbol: str
) -> list[sqlite3.Row]:
    return list(
        store._db.execute(
            """
            SELECT * FROM pit_records
            WHERE source = ? AND symbol = ?
            ORDER BY event_time, append_id
            """,
            (source, symbol),
        )
    )


def _iso_ms(value: int | None) -> str | None:
    return epoch_ms(value)


def build_coverage_report(
    store: BackfillPITStore,
    *,
    symbols: Sequence[str] = SYMBOLS,
    target_epochs: int = TARGET_EPOCHS,
    observed_at: dt.datetime | None = None,
    rate_limit: RateLimitUsage | None = None,
    oi_boundary_proofs: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Measure W2 source coverage without returns, PnL, or direction metrics."""

    now = observed_at or utc_now()
    cutoff_ms = floor_utc_4h_ms(now)
    target_open_times = [
        cutoff_ms - (target_epochs - index) * FOUR_H_MS
        for index in range(target_epochs)
    ]
    target_set = set(target_open_times)
    provenance = store.provenance()
    if oi_boundary_proofs is None:
        recovered_proofs: dict[str, Mapping[str, Any]] = {}
        for key, value in provenance.items():
            if not key.startswith("run_evidence/"):
                continue
            evidence = json.loads(value)
            recovered_proofs.update(evidence.get("oi_boundary_proofs", {}))
        oi_boundary_proofs = recovered_proofs
    by_symbol: dict[str, Any] = {}
    retention_earliest: list[int] = []
    retention_latest: list[int] = []

    for symbol in symbols:
        kline_rows = _source_rows(store, source=KLINE_SOURCE, symbol=symbol)
        premium_rows = _source_rows(store, source=PREMIUM_SOURCE, symbol=symbol)
        oi_rows = _source_rows(store, source=OI_SOURCE, symbol=symbol)

        kline_by_open: dict[int, list[Any]] = {}
        ofi_valid: set[int] = set()
        for row in kline_rows:
            payload = _parse_json_row(row)
            if not isinstance(payload, list) or len(payload) < 10:
                continue
            open_ms = int(payload[0])
            kline_by_open[open_ms] = payload
            try:
                total = Decimal(str(payload[5]))
                taker_buy = Decimal(str(payload[9]))
            except (InvalidOperation, ValueError):
                continue
            if taker_buy > 0 and total - taker_buy > 0:
                ofi_valid.add(open_ms)

        premium_by_open: dict[int, list[Any]] = {}
        for row in premium_rows:
            payload = _parse_json_row(row)
            if isinstance(payload, list) and len(payload) >= 7:
                premium_by_open[int(payload[0])] = payload

        oi_observations: list[OIObservation] = []
        for row in oi_rows:
            payload = _parse_json_row(row)
            if not isinstance(payload, dict):
                continue
            try:
                oi_observations.append(
                    OIObservation(
                        timestamp_ms=int(payload["timestamp"]),
                        value=Decimal(str(payload["sumOpenInterest"])),
                        provenance="openInterestHist_backfill",
                    )
                )
            except (InvalidOperation, KeyError, TypeError, ValueError):
                continue
        oi_observations.sort(key=lambda item: item.timestamp_ms)
        oi_grid_gap_count = sum(
            right.timestamp_ms - left.timestamp_ms != FIVE_M_MS
            for left, right in zip(oi_observations, oi_observations[1:], strict=False)
        )
        oi_off_grid_count = sum(
            item.timestamp_ms % FIVE_M_MS != 0 for item in oi_observations
        )
        if oi_observations:
            retention_earliest.append(oi_observations[0].timestamp_ms)
            retention_latest.append(oi_observations[-1].timestamp_ms)

        oi_eligible_epochs: list[int] = []
        for open_ms in target_open_times:
            at_open = select_oi_boundary_observation(
                boundary_ms=open_ms, live=(), backfill=oi_observations
            ).observation
            at_close = select_oi_boundary_observation(
                boundary_ms=open_ms + FOUR_H_MS,
                live=(),
                backfill=oi_observations,
            ).observation
            if at_open is not None and at_close is not None:
                oi_eligible_epochs.append(open_ms)

        ofi_covered = target_set & ofi_valid
        premium_covered = target_set & set(premium_by_open)
        by_symbol[symbol] = {
            "role": "signal" if symbol in SIGNAL_SYMBOLS else "predictor_only",
            "ofi": {
                "source": KLINE_SOURCE,
                "stored_rows": len(kline_rows),
                "target_epochs": target_epochs,
                "covered_target_epochs": len(ofi_covered),
                "period_start": _iso_ms(min(ofi_covered) if ofi_covered else None),
                "period_end": _iso_ms(
                    max(ofi_covered) + FOUR_H_MS if ofi_covered else None
                ),
                "coverage_100pct": len(ofi_covered) == target_epochs,
                "definition": (
                    "log(taker_buy_base/(total_base-taker_buy_base)); "
                    "coverage requires both sides > 0"
                ),
            },
            "premium": {
                "source": PREMIUM_SOURCE,
                "stored_rows": len(premium_rows),
                "target_epochs": target_epochs,
                "covered_target_epochs": len(premium_covered),
                "period_start": _iso_ms(
                    min(premium_covered) if premium_covered else None
                ),
                "period_end": _iso_ms(
                    max(premium_covered) + FOUR_H_MS if premium_covered else None
                ),
                "coverage_100pct": len(premium_covered) == target_epochs,
            },
            "open_interest": {
                "source": OI_SOURCE,
                "stored_5m_points": len(oi_observations),
                "point_period_start": _iso_ms(
                    oi_observations[0].timestamp_ms if oi_observations else None
                ),
                "point_period_end": _iso_ms(
                    oi_observations[-1].timestamp_ms if oi_observations else None
                ),
                "target_epochs": target_epochs,
                "boundary_pair_eligible_epochs": len(oi_eligible_epochs),
                "eligible_period_start": _iso_ms(
                    min(oi_eligible_epochs) if oi_eligible_epochs else None
                ),
                "eligible_period_end": _iso_ms(
                    max(oi_eligible_epochs) + FOUR_H_MS if oi_eligible_epochs else None
                ),
                "coverage_100pct": len(oi_eligible_epochs) == target_epochs,
                "grid_gap_count": oi_grid_gap_count,
                "off_grid_count": oi_off_grid_count,
                "retention_boundary_proof": oi_boundary_proofs.get(symbol),
                "boundary_tolerance": "±5m inclusive",
                "selection_priority": "live poll nearest, then backfill nearest",
            },
        }

    latest_shared = min(retention_latest) if retention_latest else None
    earliest_shared = max(retention_earliest) if retention_earliest else None
    shared_days = (
        (latest_shared - earliest_shared) / (24 * 60 * 60 * 1000)
        if latest_shared is not None
        and earliest_shared is not None
        and latest_shared >= earliest_shared
        else None
    )
    earliest_age_days = (
        (int(now.timestamp() * 1000) - earliest_shared) / (24 * 60 * 60 * 1000)
        if earliest_shared is not None
        else None
    )
    audit = store.audit()
    signal_coverage_ok = all(
        by_symbol[symbol]["ofi"]["coverage_100pct"]
        and by_symbol[symbol]["premium"]["coverage_100pct"]
        for symbol in sorted(SIGNAL_SYMBOLS)
    )
    return {
        "report_version": BACKFILL_VERSION,
        "observed_at": iso_utc(now),
        "artifact": {
            "path": str(store.path),
            "rows": audit["rows"],
            "integrity_audit": audit,
            "provenance": {
                key: value
                for key, value in provenance.items()
                if not key.startswith("run_evidence/")
            },
            "sealed_run_evidence_keys": sorted(
                key for key in provenance if key.startswith("run_evidence/")
            ),
        },
        "target": {
            "symbols": list(symbols),
            "signal_symbols": sorted(SIGNAL_SYMBOLS),
            "target_epochs": target_epochs,
            "target_period_start": _iso_ms(target_open_times[0]),
            "target_period_end": _iso_ms(cutoff_ms),
        },
        "symbols": by_symbol,
        "oi_retention": {
            "shared_earliest_point": _iso_ms(earliest_shared),
            "shared_latest_point": _iso_ms(latest_shared),
            "shared_observed_span_days": shared_days,
            "earliest_shared_point_age_days_at_observation": earliest_age_days,
            "boundary_proofs": dict(oi_boundary_proofs),
            "definition": (
                "latest shared point minus earliest shared point across all symbols"
            ),
        },
        "rate_limit": rate_limit.as_dict() if rate_limit else None,
        "acceptance": {
            "three_signal_symbols_ofi_premium_252_100pct": signal_coverage_ok,
            "oi_matches_measured_retention": (
                bool(retention_earliest)
                and all(
                    oi_boundary_proofs.get(symbol, {}).get("confirmed")
                    for symbol in symbols
                )
                and all(
                    by_symbol[symbol]["open_interest"]["grid_gap_count"] == 0
                    and by_symbol[symbol]["open_interest"]["off_grid_count"] == 0
                    for symbol in symbols
                )
            ),
            "no_forward_return_pnl_or_direction_metrics": True,
        },
    }


class BinanceR4P0Backfill:
    def __init__(self, config: BackfillConfig, store: BackfillPITStore) -> None:
        if config.target_epochs < 1 or config.target_epochs > 1500:
            raise ValueError("target_epochs must be between 1 and 1500")
        if config.oi_max_pages < 1:
            raise ValueError("oi_max_pages must be positive")
        self.config = config
        self.store = store
        self.run_id = f"backfill:{uuid.uuid4().hex}"
        self.anchor_time = utc_now()
        self.rate_limit = RateLimitUsage()
        self.insert_counts: Counter[str] = Counter()
        self.duplicate_counts: Counter[str] = Counter()
        self.oi_boundary_proofs: dict[str, dict[str, Any]] = {}

    async def run(self) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=REST_BASE_URL,
            timeout=httpx.Timeout(self.config.request_timeout_seconds),
            follow_redirects=False,
            headers={"User-Agent": f"auto-trader/{BACKFILL_VERSION}"},
        ) as client:
            for symbol in self.config.symbols:
                await self._fetch_kline_family(
                    client,
                    symbol=symbol,
                    path="/fapi/v1/klines",
                    source=KLINE_SOURCE,
                )
                await self._fetch_kline_family(
                    client,
                    symbol=symbol,
                    path="/fapi/v1/premiumIndexKlines",
                    source=PREMIUM_SOURCE,
                )
                await self._fetch_oi_history(client, symbol=symbol)
        completed_at = utc_now()
        self.store.seal_run_evidence(
            self.run_id,
            {
                "anchor_time": iso_utc(self.anchor_time),
                "completed_at": iso_utc(completed_at),
                "oi_boundary_proofs": self.oi_boundary_proofs,
                "rate_limit": self.rate_limit.as_dict(),
            },
        )
        return build_coverage_report(
            self.store,
            symbols=self.config.symbols,
            target_epochs=self.config.target_epochs,
            observed_at=self.anchor_time,
            rate_limit=self.rate_limit,
            oi_boundary_proofs=self.oi_boundary_proofs,
        )

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        *,
        path: str,
        params: Mapping[str, Any],
    ) -> tuple[Any, dt.datetime, dt.datetime]:
        assert_backfill_rest_target(path)
        started = utc_now()
        response = await client.get(path, params=params)
        completed = utc_now()
        self.rate_limit.observe(path=path, params=params, response=response)
        if response.status_code in {418, 429}:
            retry_after = response.headers.get("retry-after")
            raise RuntimeError(
                f"rate limit response {response.status_code}; retry-after={retry_after}"
            )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, str) or not isinstance(payload, (dict, list)):
            raise ValueError(f"invalid JSON shape from allowlisted path {path}")
        if self.config.request_delay_seconds:
            await asyncio.sleep(self.config.request_delay_seconds)
        return payload, started, completed

    def _append(
        self,
        *,
        source: str,
        symbol: str,
        payload: Any,
        started: dt.datetime,
        completed: dt.datetime,
        event_time_ms: int,
        transaction_time_ms: int | None,
        sequence: int,
        path: str,
    ) -> None:
        inserted = self.store.append(
            source=source,
            symbol=symbol,
            raw_payload=payload,
            local_receive_time=completed,
            run_id=self.run_id,
            event_time=epoch_ms(event_time_ms),
            transaction_time=epoch_ms(transaction_time_ms),
            request_started_at=iso_utc(started),
            request_completed_at=iso_utc(completed),
            sequence_or_trade_id=str(sequence),
            gap_detected=False,
            reconnect_id=f"{self.run_id}:rest:{path}",
        )
        if inserted:
            self.insert_counts[source] += 1
        else:
            self.duplicate_counts[source] += 1

    async def _fetch_kline_family(
        self,
        client: httpx.AsyncClient,
        *,
        symbol: str,
        path: str,
        source: str,
    ) -> None:
        cutoff_ms = floor_utc_4h_ms(self.anchor_time)
        start_ms = cutoff_ms - self.config.target_epochs * FOUR_H_MS
        params = {
            "symbol": symbol,
            "interval": "4h",
            "startTime": start_ms,
            "endTime": cutoff_ms - 1,
            "limit": self.config.target_epochs,
        }
        payload, started, completed = await self._get_json(
            client, path=path, params=params
        )
        if not isinstance(payload, list):
            raise ValueError(f"invalid kline response from {path}")
        completed_ms = int(completed.timestamp() * 1000)
        complete_rows = [
            row
            for row in payload
            if isinstance(row, list)
            and len(row) >= 10
            and int(row[0]) >= start_ms
            and int(row[6]) <= completed_ms
        ]
        if len(complete_rows) != self.config.target_epochs:
            raise RuntimeError(
                f"{symbol} {path}: expected {self.config.target_epochs} complete "
                f"4h rows, received {len(complete_rows)}"
            )
        for row in complete_rows:
            self._append(
                source=source,
                symbol=symbol,
                payload=row,
                started=started,
                completed=completed,
                event_time_ms=int(row[0]),
                transaction_time_ms=int(row[6]),
                sequence=int(row[0]),
                path=path,
            )

    async def _fetch_oi_history(
        self, client: httpx.AsyncClient, *, symbol: str
    ) -> None:
        path = "/futures/data/openInterestHist"
        page_end_ms = floor_utc_5m_ms(self.anchor_time)
        saw_rows = False
        previous_earliest: int | None = None
        for _page in range(self.config.oi_max_pages):
            params = {
                "symbol": symbol,
                "period": "5m",
                "endTime": page_end_ms,
                "limit": OI_PAGE_LIMIT,
            }
            payload, started, completed = await self._get_json(
                client, path=path, params=params
            )
            if not isinstance(payload, list):
                raise ValueError(f"invalid OI response for {symbol}")
            rows = [
                item
                for item in payload
                if isinstance(item, dict)
                and item.get("timestamp") is not None
                and item.get("sumOpenInterest") is not None
            ]
            if not rows:
                if saw_rows:
                    self.oi_boundary_proofs[symbol] = {
                        "confirmed": True,
                        "oldest_returned_point": epoch_ms(previous_earliest),
                        "older_empty_page_end": epoch_ms(page_end_ms),
                    }
                    break
                raise RuntimeError(
                    f"{symbol} OI latest retention page returned zero rows"
                )
            saw_rows = True
            rows.sort(key=lambda item: int(item["timestamp"]))
            earliest = int(rows[0]["timestamp"])
            if previous_earliest is not None and earliest >= previous_earliest:
                raise RuntimeError(f"{symbol} OI pagination did not move backward")
            previous_earliest = earliest
            for item in rows:
                timestamp = int(item["timestamp"])
                self._append(
                    source=OI_SOURCE,
                    symbol=symbol,
                    payload=item,
                    started=started,
                    completed=completed,
                    event_time_ms=timestamp,
                    transaction_time_ms=None,
                    sequence=timestamp,
                    path=path,
                )
            page_end_ms = earliest - 1
            if len(rows) < OI_PAGE_LIMIT:
                # One extra page proves the observed retention boundary.
                continue
        else:
            raise RuntimeError(
                f"{symbol} OI pagination hit safety cap {self.config.oi_max_pages}"
            )
        if not saw_rows:
            raise RuntimeError(f"{symbol} OI retention probe returned zero rows")


def dump_report(path: Path, report: Mapping[str, Any]) -> None:
    """Write the generated research report atomically beside the backfill DB."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
