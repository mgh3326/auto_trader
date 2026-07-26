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
import urllib.parse
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
import websockets

REST_BASE_URL: Final = "https://fapi.binance.com"
WS_PUBLIC_BASE_URL: Final = "wss://fstream.binance.com/public"
WS_MARKET_BASE_URL: Final = "wss://fstream.binance.com/market"
REST_PATH_ALLOWLIST: Final = frozenset(
    {
        "/fapi/v1/openInterest",
        "/fapi/v1/premiumIndex",
        "/fapi/v1/premiumIndexKlines",
        "/futures/data/basis",
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
COLLECTOR_VERSION: Final = "r4-p0-collector.v1"
EXPECTED_SOURCES: Final = frozenset(
    {
        "binance_usdm.aggTrade",
        "binance_usdm.forceOrder",
        "binance_usdm.bookTicker",
        "binance_usdm.depth5",
        "binance_usdm.openInterest",
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
    duration_seconds: float | None = None
    oi_poll_seconds: float = 60.0
    premium_poll_seconds: float = 60.0
    basis_poll_seconds: float = 300.0
    taker_poll_seconds: float = 300.0
    status_seconds: float = 30.0
    symbols: tuple[str, ...] = SYMBOLS


class AppendOnlyPITStore:
    """Crash-safe local research artifact with immutable rows and deduplication."""

    def __init__(
        self, root: Path, *, collector_version: str = COLLECTOR_VERSION
    ) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "r4_p0_collector.sqlite3"
        self.collector_version = collector_version
        self._lock_file = (self.root / ".collector.lock").open("a+")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            raise RuntimeError(
                f"collector artifact is already locked: {self.root}"
            ) from exc
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._configure()

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
        self._previous_sequence: dict[tuple[str, str], int] = {}
        self._seen_on_connection: set[tuple[str, str, str]] = set()
        self._last_book_snapshot: dict[str, dt.datetime] = {}

    async def run(self) -> None:
        log.info(
            "collector.start run_id=%s symbols=%s artifact=%s",
            self.run_id,
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
        }

    async def _duration_timer(self) -> None:
        assert self.config.duration_seconds is not None
        await asyncio.sleep(self.config.duration_seconds)
        self.stop.set()

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.status_seconds)
            log.info(
                "collector.status run_id=%s session_counts=%s duplicates=%s failures=%s total=%s",
                self.run_id,
                dict(self.session_counts),
                dict(self.duplicate_counts),
                dict(self.failure_counts),
                self.store.counts(),
            )

    async def _ws_supervisor(self, lane: str) -> None:
        failure_attempt = 0
        reconnect_number = 0
        url = build_ws_urls(self.config.symbols)[lane]
        while not self.stop.is_set():
            reconnect_number += 1
            reconnect_id = f"{self.run_id}:ws:{lane}:{reconnect_number}"
            request_started = utc_now()
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
                    log.info(
                        "collector.ws.connected lane=%s reconnect_id=%s",
                        lane,
                        reconnect_id,
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
                self.failure_counts["websocket"] += 1
                delay = min(60.0, 1.0 * (2 ** min(failure_attempt, 6)))
                delay *= random.uniform(0.8, 1.2)
                failure_attempt += 1
                log.error(
                    "collector.ws.disconnected reconnect_id=%s error=%s backoff_s=%.2f",
                    reconnect_id,
                    type(exc).__name__,
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
                    log.error(
                        "collector.poll.failed family=%s error=%s backoff_s=%.2f",
                        family,
                        type(exc).__name__,
                        delay,
                    )
                    await asyncio.sleep(delay)

    async def _poll_family(self, client: httpx.AsyncClient, family: str) -> None:
        for symbol in self.config.symbols:
            if family == "open_interest":
                await self._rest_get(
                    client,
                    path="/fapi/v1/openInterest",
                    params={"symbol": symbol},
                    symbol=symbol,
                    outputs=(("binance_usdm.openInterest", None),),
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
            elif family == "basis":
                await self._rest_get(
                    client,
                    path="/futures/data/basis",
                    params={
                        "pair": symbol,
                        "contractType": "PERPETUAL",
                        "period": "5m",
                        "limit": 1,
                    },
                    symbol=symbol,
                    outputs=(("binance_usdm.basis", None),),
                )
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

    async def _rest_premium_kline(
        self, client: httpx.AsyncClient, *, symbol: str
    ) -> None:
        path = "/fapi/v1/premiumIndexKlines"
        assert_rest_target(path)
        started = utc_now()
        response = await client.get(
            path,
            params={"symbol": symbol, "interval": "1m", "limit": 2},
        )
        completed = utc_now()
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError(f"invalid JSON shape from allowlisted path {path}")
        completed_ms = int(completed.timestamp() * 1000)
        complete_rows = [
            row
            for row in payload
            if isinstance(row, list) and len(row) >= 7 and int(row[6]) <= completed_ms
        ]
        if not complete_rows:
            raise ValueError(f"no completed 1m row from allowlisted path {path}")
        item = complete_rows[-1]
        source = "binance_usdm.premiumIndexKline1m"
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

    async def _rest_get(
        self,
        client: httpx.AsyncClient,
        *,
        path: str,
        params: Mapping[str, Any],
        symbol: str,
        outputs: Sequence[tuple[str, str | None]],
    ) -> None:
        assert_rest_target(path)
        started = utc_now()
        response = await client.get(path, params=params)
        completed = utc_now()
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
            raise ValueError(f"missing exchange timestamp from allowlisted path {path}")
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
                sequence_or_trade_id=str(sequence) if sequence is not None else None,
                gap_detected=False,
                reconnect_id=reconnect_id,
            )
            self._count_result(source, inserted)

    def _count_result(self, source: str, inserted: bool) -> None:
        if inserted:
            self.session_counts[source] += 1
        else:
            self.duplicate_counts[source] += 1


def redact_sample(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return a compact, secret-free proof row with every PIT contract column."""
    return {column: row.get(column) for column in PIT_COLUMNS}
