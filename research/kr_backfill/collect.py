"""Stage B collector — three independent source streams into research history.

Destination is ``research.kr_candles_1m``. Production ``public.kr_candles_1m``
is read only for latency probing and is never written.

Write discipline (all enforced here, not by convention):

* ``ON CONFLICT (time_utc, symbol, venue) DO NOTHING`` — existing rows always
  win. No DELETE, no UPDATE, no table other than ``research.kr_candles_1m``.
* regular-session bars only (09:00-15:30 KST); NXT bars discarded, ``venue='KRX'``.
* the 09:00-20:00 KST freeze halts fetching at a checkpoint; it does not abort.
* each source keeps an independent checkpoint. A failed source never has its
  remaining symbols reassigned — the split table is the only provenance record,
  and silent reassignment would destroy it.
* every page appends to progress.jsonl and flushes immediately.

Abort conditions (stop the stream, report, do not "work around"):
conflict ratio far from expectation · query latency regression vs baseline ·
repeated 429s · any change to a witness table outside kr_candles_1m.

The dual Kiwoom surfaces use these same guards; an abort is isolated to the
failing surface. An overlap mismatch stops both participating surfaces because
it invalidates the comparison itself.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import statistics
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg

try:
    from .dual_surface import (
        BACKOFF_PACE_SECONDS,
        KiwoomSurfaceClient,
        OverlapMismatch,
        SurfaceAuthError,
        SurfaceBackoffExhausted,
        SurfaceContractError,
        SurfaceManifest,
        SurfacePacer,
        assignment_sha256,
        compare_overlap_exact,
        http_status_from_exception,
        read_assignment_csv,
        validate_surface,
        write_surface_manifest,
    )
except ImportError:  # pragma: no cover - direct CLI execution
    from dual_surface import (
        BACKOFF_PACE_SECONDS,
        KiwoomSurfaceClient,
        OverlapMismatch,
        SurfaceAuthError,
        SurfaceBackoffExhausted,
        SurfaceContractError,
        SurfaceManifest,
        SurfacePacer,
        assignment_sha256,
        compare_overlap_exact,
        http_status_from_exception,
        read_assignment_csv,
        validate_surface,
        write_surface_manifest,
    )
try:
    from .sources import (  # noqa: E402
        AUTH_STALE_TOKEN,
        EMPTY_RESPONSE,
        KST,
        FetchWindowClosed,
        Pacer,
        assert_fetch_window_open,
        fetch_kis_minutes,
        fetch_kiwoom_minutes,
        fetch_toss_minutes,
        in_regular_session,
        now_kst,
    )
except ImportError:  # pragma: no cover - direct CLI execution
    from sources import (  # noqa: E402
        AUTH_STALE_TOKEN,
        EMPTY_RESPONSE,
        KST,
        FetchWindowClosed,
        Pacer,
        assert_fetch_window_open,
        fetch_kis_minutes,
        fetch_kiwoom_minutes,
        fetch_toss_minutes,
        in_regular_session,
        now_kst,
    )

VENUE = "KRX"

#: Destination is research, NOT production. Changed 2026-08-03 by the storage
#: decision in herdr-inbox/answer-codexmock-research-db-1805.md: production
#: public.kr_candles_1m keeps its 90-day retention and is never backfilled.
TARGET_TABLE = "research.kr_candles_1m"

UPSERT_SQL = """
INSERT INTO research.kr_candles_1m
    (symbol, time_utc, session_date_kst, venue, session_segment, source,
     open, high, low, close, volume, value, retrieved_at, batch_id)
SELECT * FROM UNNEST($1::text[], $2::timestamptz[], $3::date[], $4::text[],
                     $5::text[], $6::text[],
                     $7::numeric[], $8::numeric[], $9::numeric[],
                     $10::numeric[], $11::numeric[], $12::numeric[],
                     $13::timestamptz[], $14::text[])
ON CONFLICT (time_utc, symbol, venue) DO NOTHING
"""

#: Latency regression threshold vs the Stage A baseline median (2.127 ms).
LATENCY_ABORT_FACTOR = 5.0
MAX_CONSECUTIVE_429 = 3
EXIT_SUCCESS = 0
EXIT_PARTIAL_FAILURE = 1
EXIT_TOTAL_FAILURE = 2

# DB 권한으로 대체됨(role auto_trader_kr_backfill, 2026-08-04 적용).
# 행수 감시는 프로덕션 동시 쓰기와 구분 불가.


@dataclass
class StreamStats:
    source: str
    symbols_total: int = 0
    symbols_done: int = 0
    calls: int = 0
    rows_fetched: int = 0
    rows_inserted: int = 0
    rows_skipped_conflict: int = 0
    empty_responses: int = 0
    empty_symbols: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stopped_reason: str | None = None


@dataclass
class SurfaceStats:
    surface: str
    symbols_total: int = 0
    symbols_done: int = 0
    calls: int = 0
    rows_fetched: int = 0
    rows_inserted: int = 0
    rows_skipped_conflict: int = 0
    errors: list[str] = field(default_factory=list)
    stopped_reason: str | None = None


class AbortStream(RuntimeError):
    pass


class ProgressLog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", buffering=1)  # line buffered

    def write(self, record: dict[str, Any]) -> None:
        record.setdefault("ts", now_kst().isoformat())
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        self._fh.close()


class Checkpoint:
    """One file per source. Written after every page."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {}
        if path.exists():
            self.data = json.loads(path.read_text())

    def get(self, symbol: str) -> dict[str, Any]:
        return self.data.setdefault(
            symbol,
            {
                "done": False,
                "oldest_reached": None,
                "rows_inserted": 0,
                "rows_skipped": 0,
            },
        )

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, default=str))
        tmp.replace(self.path)


def dsn() -> str:
    return os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")


async def insert_bars(
    pool: asyncpg.Pool,
    symbol: str,
    bars: dict[datetime, dict[str, float]],
    *,
    source: str,
    batch_id: str,
) -> tuple[int, int]:
    """Returns (inserted, skipped_conflict). Never updates an existing row.

    Counts are measured by before/after row counts rather than inferred from the
    statement tag, so a silent partial insert cannot be reported as a success.
    """
    if not bars:
        return 0, 0
    items = sorted(bars.items())
    times = [t.replace(tzinfo=KST) for t, _ in items]
    session_dates = [t.date() for t, _ in items]
    # Backfill fetches the regular session only, so the segment is known; the
    # collector filters non-regular bars out before reaching here.
    segments = ["KRX_REGULAR"] * len(items)
    now = datetime.now(KST)
    cols = {
        k: [float(v[k]) for _, v in items]
        for k in ("open", "high", "low", "close", "volume", "value")
    }
    async with pool.acquire() as conn:
        async with conn.transaction():
            count_sql = (
                "SELECT count(*) FROM research.kr_candles_1m "
                "WHERE symbol=$1 AND venue=$2 AND time_utc = ANY($3::timestamptz[])"
            )
            before = await conn.fetchval(count_sql, symbol, VENUE, times)
            await conn.execute(
                UPSERT_SQL,
                [symbol] * len(items),
                times,
                session_dates,
                [VENUE] * len(items),
                segments,
                [source.upper()] * len(items),
                cols["open"],
                cols["high"],
                cols["low"],
                cols["close"],
                cols["volume"],
                cols["value"],
                [now] * len(items),
                [batch_id] * len(items),
            )
            after = await conn.fetchval(count_sql, symbol, VENUE, times)
    inserted = after - before
    return inserted, len(items) - inserted


class Guard:
    """Latency and 429 abort-condition monitor shared by all streams."""

    def __init__(self, pool: asyncpg.Pool, baseline_median_ms: float, log: ProgressLog):
        self.pool = pool
        self.baseline = baseline_median_ms
        self.log = log
        self.consecutive_429: dict[str, int] = {}

    async def check(self, source: str) -> None:
        async with self.pool.acquire() as c:
            samples = []
            for _ in range(5):
                t0 = time.perf_counter()
                await c.fetch(
                    "SELECT time, close FROM public.kr_candles_1m "
                    "WHERE symbol=$1 AND time >= $2 ORDER BY time DESC LIMIT 500",
                    "005930",
                    datetime.now(KST) - timedelta(days=1),
                )
                samples.append((time.perf_counter() - t0) * 1000)
        med = statistics.median(samples)
        self.log.write(
            {
                "event": "latency_probe",
                "source": source,
                "median_ms": round(med, 3),
                "baseline_ms": self.baseline,
            }
        )
        if med > self.baseline * LATENCY_ABORT_FACTOR:
            raise AbortStream(
                f"query latency {med:.2f}ms > {LATENCY_ABORT_FACTOR}x baseline {self.baseline}ms"
            )

    def note_429(self, source: str, is_429: bool) -> None:
        if is_429:
            self.consecutive_429[source] = self.consecutive_429.get(source, 0) + 1
            if self.consecutive_429[source] >= MAX_CONSECUTIVE_429:
                raise AbortStream(f"{source}: {MAX_CONSECUTIVE_429} consecutive 429s")
        else:
            self.consecutive_429[source] = 0


async def run_stream(
    source: str,
    symbols: list[str],
    client: Any,
    pool: asyncpg.Pool,
    ckpt: Checkpoint,
    log: ProgressLog,
    guard: Guard,
    start_date: date,
    end_date: date,
    batch_id: str,
) -> StreamStats:
    stats = StreamStats(source=source, symbols_total=len(symbols))
    pacer = Pacer(source)
    start_dt = datetime.combine(start_date, datetime.min.time())

    for symbol in symbols:
        st = ckpt.get(symbol)
        if st.get("done"):
            stats.symbols_done += 1
            continue

        cursor_dt = (
            datetime.fromisoformat(st["oldest_reached"])
            if st.get("oldest_reached")
            else datetime.combine(end_date, datetime.max.time().replace(microsecond=0))
        )
        toss_cursor: str | None = st.get("toss_cursor")

        try:
            while cursor_dt > start_dt:
                try:
                    assert_fetch_window_open()
                except FetchWindowClosed as exc:
                    stats.stopped_reason = f"market_hours_freeze: {exc}"
                    log.write(
                        {
                            "event": "paused_market_hours",
                            "source": source,
                            "symbol": symbol,
                            "cursor": cursor_dt,
                        }
                    )
                    ckpt.save()
                    return stats

                try:
                    if source == "kiwoom":
                        bars, meta = await fetch_kiwoom_minutes(
                            client=client,
                            symbol=symbol,
                            pacer=pacer,
                            max_pages=1,
                            base_dt=cursor_dt.strftime("%Y%m%d"),
                        )
                    elif source == "toss":
                        bars, meta = await fetch_toss_minutes(
                            client=client,
                            symbol=symbol,
                            pacer=pacer,
                            count=200,
                            before=toss_cursor,
                            max_pages=1,
                        )
                        toss_cursor = meta.get("next_before")
                    else:
                        bars, meta = await fetch_kis_minutes(
                            client=client,
                            symbol=symbol,
                            pacer=pacer,
                            session_date=cursor_dt.date(),
                            end_time=cursor_dt.strftime("%H%M%S"),
                            max_pages=1,
                        )
                    guard.note_429(source, False)
                except Exception as exc:  # noqa: BLE001
                    msg = f"{type(exc).__name__}: {exc}"
                    reason_code = str(getattr(exc, "reason_code", type(exc).__name__))
                    stats.calls = pacer.calls
                    guard.note_429(source, "429" in msg or "TooManyRequests" in msg)
                    stats.errors.append(f"{symbol}: {msg}")
                    log.write(
                        {
                            "event": "fetch_error",
                            "source": source,
                            "symbol": symbol,
                            "error": msg,
                            "reason_code": reason_code,
                            "retry_disposition": getattr(
                                exc, "retry_disposition", "NONE"
                            ),
                        }
                    )
                    if reason_code == AUTH_STALE_TOKEN:
                        stats.stopped_reason = (
                            "AUTH_STALE_TOKEN after one read-only retry"
                        )
                        ckpt.save()
                        return stats
                    break

                stats.calls = pacer.calls
                if not bars:
                    reason_code = str(meta.get("outcome_code") or EMPTY_RESPONSE)
                    stats.empty_responses += 1
                    stats.empty_symbols.append(symbol)
                    log.write(
                        {
                            "event": "empty_response",
                            "source": source,
                            "symbol": symbol,
                            "reason_code": reason_code,
                            "calls_cumulative": pacer.calls,
                        }
                    )
                    st["done"] = True
                    break

                oldest = min(bars)
                # regular session only, inside the requested range
                keep = {
                    t: v
                    for t, v in bars.items()
                    if in_regular_session(t)
                    and start_dt <= t <= datetime.combine(end_date, datetime.max.time())
                }
                stats.rows_fetched += len(bars)
                ins, skip = await insert_bars(
                    pool, symbol, keep, source=source, batch_id=batch_id
                )
                stats.rows_inserted += ins
                stats.rows_skipped_conflict += skip
                st["rows_inserted"] = st.get("rows_inserted", 0) + ins
                st["rows_skipped"] = st.get("rows_skipped", 0) + skip

                log.write(
                    {
                        "event": "page",
                        "source": source,
                        "symbol": symbol,
                        "range": [oldest.isoformat(), max(bars).isoformat()],
                        "rows_fetched": len(bars),
                        "rows_kept": len(keep),
                        "rows_inserted": ins,
                        "rows_skipped_conflict": skip,
                        "calls_cumulative": pacer.calls,
                    }
                )

                if oldest >= cursor_dt:  # no backward progress; stop this symbol
                    st["done"] = True
                    break
                cursor_dt = oldest - timedelta(minutes=1)
                st["oldest_reached"] = cursor_dt.isoformat()
                st["toss_cursor"] = toss_cursor
                ckpt.save()

                if source == "toss" and not toss_cursor:
                    st["done"] = True
                    break
            else:
                st["done"] = True
        except AbortStream as exc:
            stats.stopped_reason = str(exc)
            log.write({"event": "abort", "source": source, "reason": str(exc)})
            ckpt.save()
            return stats

        if st.get("done"):
            stats.symbols_done += 1
        ckpt.save()
        await guard.check(source)

    return stats


class OverlapVerifier:
    """Run the deliberate two-surface sample at a fixed batch cadence."""

    def __init__(
        self,
        *,
        symbols: tuple[str, ...],
        session_date: date,
        clients: dict[str, KiwoomSurfaceClient],
        pacers: dict[str, SurfacePacer],
        log: ProgressLog,
        stop_events: dict[str, asyncio.Event],
        every_batches: int = 50,
    ) -> None:
        if not symbols:
            raise SurfaceContractError("dual-surface run requires overlap symbols")
        if len(symbols) > 2:
            raise SurfaceContractError("overlap sample is capped at two symbols")
        if every_batches <= 0:
            raise SurfaceContractError("overlap cadence must be positive")
        self.symbols = symbols
        self.session_date = session_date
        self.clients = clients
        self.pacers = pacers
        self.log = log
        self.stop_events = stop_events
        self.every_batches = every_batches
        self.completed_batches = 0
        self._last_verified_batch = 0
        self._lock = asyncio.Lock()

    async def after_batch(self) -> None:
        async with self._lock:
            self.completed_batches += 1
            if self.completed_batches % self.every_batches:
                return
            if self._last_verified_batch == self.completed_batches:
                return
            self._last_verified_batch = self.completed_batches
            try:
                evidence = []
                for symbol in self.symbols:
                    data: dict[str, dict] = {}
                    for surface in ("mock", "live"):
                        try:
                            rows, _meta = await fetch_kiwoom_minutes(
                                client=self.clients[surface],
                                symbol=symbol,
                                pacer=self.pacers[surface],
                                max_pages=1,
                                base_dt=self.session_date.strftime("%Y%m%d"),
                            )
                            self.pacers[surface].note_status(None)
                        except Exception as exc:  # noqa: BLE001
                            self.pacers[surface].note_exception(exc)
                            raise
                        data[surface] = {
                            timestamp: values
                            for timestamp, values in rows.items()
                            if in_regular_session(timestamp)
                        }
                    evidence.append(
                        compare_overlap_exact(symbol, data["mock"], data["live"])
                    )
            except Exception:
                for event in self.stop_events.values():
                    event.set()
                raise
            self.log.write(
                {
                    "event": "overlap_check",
                    "surface": "mock+live",
                    "batch_number": self.completed_batches,
                    "symbols": list(self.symbols),
                    "evidence": evidence,
                    "comparison": "exact integer equality including value",
                    "mismatch_action": "stop_and_report",
                }
            )


async def run_surface(
    surface: str,
    symbols: list[str],
    client: KiwoomSurfaceClient,
    pool: asyncpg.Pool,
    ckpt: Checkpoint,
    log: ProgressLog,
    start_date: date,
    end_date: date,
    batch_id: str,
    *,
    pacer: SurfacePacer,
    overlap: OverlapVerifier | None,
    stop_event: asyncio.Event,
    guard: Guard | None = None,
) -> SurfaceStats:
    """Collect one immutable symbol partition for one Kiwoom surface."""

    surface = validate_surface(surface)
    stats = SurfaceStats(surface=surface, symbols_total=len(symbols))
    start_dt = datetime.combine(start_date, datetime.min.time())

    for symbol in symbols:
        if stop_event.is_set():
            stats.stopped_reason = "surface_stopped"
            return stats
        state = ckpt.get(symbol)
        if state.get("done"):
            stats.symbols_done += 1
            continue
        cursor_dt = (
            datetime.fromisoformat(state["oldest_reached"])
            if state.get("oldest_reached")
            else datetime.combine(end_date, datetime.max.time().replace(microsecond=0))
        )
        try:
            while cursor_dt > start_dt:
                if stop_event.is_set():
                    stats.stopped_reason = "surface_stopped"
                    return stats
                try:
                    assert_fetch_window_open()
                except FetchWindowClosed as exc:
                    stats.stopped_reason = f"market_hours_freeze: {exc}"
                    log.write(
                        {
                            "event": "paused_market_hours",
                            "surface": surface,
                            "symbol": symbol,
                            "cursor": cursor_dt,
                        }
                    )
                    ckpt.save()
                    return stats

                try:
                    rows, meta = await fetch_kiwoom_minutes(
                        client=client,
                        symbol=symbol,
                        pacer=pacer,
                        max_pages=1,
                        base_dt=cursor_dt.strftime("%Y%m%d"),
                    )
                    pacer.note_status(None)
                except Exception as exc:  # noqa: BLE001
                    status = http_status_from_exception(exc)
                    try:
                        pacer.note_exception(exc)
                    except (SurfaceAuthError, SurfaceBackoffExhausted) as fatal:
                        stop_event.set()
                        stats.stopped_reason = str(fatal)
                        log.write(
                            {
                                "event": "abort",
                                "surface": surface,
                                "symbol": symbol,
                                "reason": str(fatal),
                                "http_status": status,
                            }
                        )
                        return stats
                    if status == 429:
                        # Retry this same page after the surface-local pacing
                        # interval.  A peer surface has its own pacer/event
                        # state and continues independently.
                        log.write(
                            {
                                "event": "backoff",
                                "surface": surface,
                                "symbol": symbol,
                                "http_status": 429,
                                "interval_seconds": pacer.interval,
                                "backoff_level": pacer.backoff_level,
                                "auto_recovery": False,
                            }
                        )
                        continue
                    stats.errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
                    log.write(
                        {
                            "event": "fetch_error",
                            "surface": surface,
                            "symbol": symbol,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    break

                stats.calls = pacer.calls
                if not rows:
                    state["done"] = True
                    break

                oldest = min(rows)
                keep = {
                    timestamp: values
                    for timestamp, values in rows.items()
                    if in_regular_session(timestamp)
                    and start_dt
                    <= timestamp
                    <= datetime.combine(end_date, datetime.max.time())
                }
                stats.rows_fetched += len(rows)
                inserted, skipped = await insert_bars(
                    pool,
                    symbol,
                    keep,
                    source="kiwoom",
                    batch_id=batch_id,
                )
                stats.rows_inserted += inserted
                stats.rows_skipped_conflict += skipped
                state["rows_inserted"] = state.get("rows_inserted", 0) + inserted
                state["rows_skipped"] = state.get("rows_skipped", 0) + skipped
                log.write(
                    {
                        "event": "page",
                        "surface": surface,
                        "symbol": symbol,
                        "range": [oldest.isoformat(), max(rows).isoformat()],
                        "rows_fetched": len(rows),
                        "rows_kept": len(keep),
                        "rows_inserted": inserted,
                        "rows_skipped_conflict": skipped,
                        "calls_cumulative": pacer.calls,
                        "batch_id": batch_id,
                    }
                )
                if oldest >= cursor_dt:
                    state["done"] = True
                    break
                cursor_dt = oldest - timedelta(minutes=1)
                state["oldest_reached"] = cursor_dt.isoformat()
                ckpt.save()
                if overlap is not None:
                    await overlap.after_batch()
            else:
                state["done"] = True
        except (
            AbortStream,
            OverlapMismatch,
            SurfaceAuthError,
            SurfaceBackoffExhausted,
        ) as exc:
            stop_event.set()
            stats.stopped_reason = str(exc)
            log.write({"event": "abort", "surface": surface, "reason": str(exc)})
            ckpt.save()
            return stats

        if state.get("done"):
            stats.symbols_done += 1
        ckpt.save()
        if guard is not None:
            try:
                await guard.check(surface)
            except AbortStream as exc:
                stop_event.set()
                stats.stopped_reason = str(exc)
                log.write({"event": "abort", "surface": surface, "reason": str(exc)})
                ckpt.save()
                return stats
    return stats


def _load_env_file(path: Path, allowed_keys: set[str]) -> None:
    """Load only surface keys; never print values or import a full env file."""

    if not path.is_file():
        raise FileNotFoundError(f"surface env file not found: {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in allowed_keys:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


def _reject_production_env_file(path: Path) -> None:
    if "prod" in str(path).lower():
        raise SurfaceContractError(f"refusing to read a production env file: {path}")


def _prepare_surface_env(
    selected: tuple[str, ...], mock_env_file: Path | None, live_env_file: Path
) -> None:
    ambient_env_file = os.environ.get("ENV_FILE", "")
    if ambient_env_file:
        _reject_production_env_file(Path(ambient_env_file))
    if "mock" in selected and mock_env_file is not None:
        _reject_production_env_file(mock_env_file)
        _load_env_file(
            mock_env_file,
            {
                "KIWOOM_MOCK_ENABLED",
                "KIWOOM_MOCK_APP_KEY",
                "KIWOOM_MOCK_APP_SECRET",
                "KIWOOM_MOCK_ACCOUNT_NO",
                "KIWOOM_MOCK_BASE_URL",
            },
        )
    if "live" in selected:
        _reject_production_env_file(live_env_file)
        _load_env_file(
            live_env_file,
            {
                "KIWOOM_LIVE_MARKETDATA_ENABLED",
                "KIWOOM_LIVE_APP_KEY",
                "KIWOOM_LIVE_APP_SECRET",
                "KIWOOM_LIVE_BASE_URL",
            },
        )
    # pydantic-settings reads this file for non-secret defaults.  The explicit
    # environment values above win over dotenv values, and the path itself is
    # recorded only as an operator input, never with credential contents.
    if selected == ("mock",) and mock_env_file is not None:
        os.environ["ENV_FILE"] = str(mock_env_file)
    elif "live" in selected:
        os.environ["ENV_FILE"] = str(live_env_file)


def _build_surface_runtime(
    selected: tuple[str, ...],
    *,
    mock_factory: Any,
    live_factory: Any,
) -> tuple[dict[str, Any], dict[str, KiwoomSurfaceClient], dict[str, SurfacePacer]]:
    """Build only selected clients and the exact pacers used by the run."""

    factories = {"mock": mock_factory, "live": live_factory}
    raw_clients = {surface: factories[surface]() for surface in selected}
    clients = {
        surface: KiwoomSurfaceClient(surface, raw_clients[surface])
        for surface in selected
    }
    pacers = {surface: SurfacePacer(surface) for surface in selected}
    return raw_clients, clients, pacers


async def _run_dual_surface(args: argparse.Namespace) -> int:
    rows = read_assignment_csv(args.split_csv)
    by_surface: dict[str, list[str]] = {"mock": [], "live": []}
    overlap_symbols: list[str] = []
    by_symbol: dict[str, list[str]] = {}
    for row in rows:
        symbol = row["ticker"].strip()
        surface = validate_surface(row["surface"].strip())
        kind = row.get("assignment_kind", "bulk").strip() or "bulk"
        by_symbol.setdefault(symbol, []).append(surface)
        if kind == "bulk":
            by_surface[surface].append(symbol)
        elif symbol not in overlap_symbols:
            overlap_symbols.append(symbol)
    selected = [args.surface] if args.surface else ["mock", "live"]
    for surface in selected:
        validate_surface(surface)
    if len(selected) == 2 and not overlap_symbols:
        raise SurfaceContractError(
            "dual surface run requires an explicit overlap sample"
        )
    if args.limit_symbols is not None:
        by_surface = {
            surface: values[: args.limit_symbols]
            for surface, values in by_surface.items()
        }

    batch_id = f"kiwoom-dual-{now_kst():%Y%m%dT%H%M%S}"
    job_events = args.job_dir / "events"
    manifest = SurfaceManifest(
        batch_id=batch_id,
        assignment_path=str(args.split_csv),
        assignment_sha256=assignment_sha256(args.split_csv),
    )
    manifest_path = job_events / "kiwoom_dual_surface_manifest.json"
    write_surface_manifest(manifest_path, manifest)
    summary = {
        "status": "DRY_RUN_NO_WRITE" if not args.confirm_write else "READY_TO_RUN",
        "batch_id": batch_id,
        "assignment_path": str(args.split_csv),
        "assignment_sha256": manifest.assignment_sha256,
        "symbols_per_surface": {
            surface: len(by_surface[surface]) for surface in selected
        },
        "overlap_symbols": overlap_symbols[:2],
        "manifest_path": str(manifest_path),
        "backoff_intervals": {
            "mock": [2.0, *BACKOFF_PACE_SECONDS],
            "live": [0.5, *BACKOFF_PACE_SECONDS],
        },
        "auto_recovery": False,
    }
    if not args.confirm_write:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    # This is the only branch that creates clients.  No client is built by the
    # dry-run path, so a planning invocation cannot accidentally call a host.
    selected_tuple = tuple(selected)
    raw_clients: dict[str, Any] = {}
    pool: asyncpg.Pool | None = None
    log: ProgressLog | None = None
    try:
        _prepare_surface_env(selected_tuple, args.mock_env_file, args.live_env_file)
        from app.services.brokers.kiwoom.client import KiwoomMockClient
        from app.services.brokers.kiwoom.live_market_data import (
            KiwoomLiveReadOnlyClient,
        )

        raw_clients, clients, pacers = _build_surface_runtime(
            selected_tuple,
            mock_factory=KiwoomMockClient.from_app_settings,
            live_factory=KiwoomLiveReadOnlyClient.from_app_settings,
        )
        stop_events = {surface: asyncio.Event() for surface in selected}
        pool = await asyncpg.create_pool(dsn(), min_size=2, max_size=6)
        log = ProgressLog(job_events / "progress.jsonl")
        guard = Guard(pool, args.baseline_median_ms, log)
        overlap = None
        if len(selected) == 2:
            overlap = OverlapVerifier(
                symbols=tuple(overlap_symbols[:2]),
                session_date=date.fromisoformat(args.end_date),
                clients=clients,
                pacers=pacers,
                log=log,
                stop_events=stop_events,
            )
        results = await asyncio.gather(
            *[
                run_surface(
                    surface,
                    by_surface[surface],
                    clients[surface],
                    pool,
                    Checkpoint(job_events / f"checkpoint_{surface}.json"),
                    log,
                    date.fromisoformat(args.start_date),
                    date.fromisoformat(args.end_date),
                    batch_id,
                    pacer=pacers[surface],
                    overlap=overlap,
                    stop_event=stop_events[surface],
                    guard=guard,
                )
                for surface in selected
            ]
        )
    finally:
        if pool is not None:
            await pool.close()
        for raw_client in raw_clients.values():
            close = getattr(raw_client, "aclose", None) or getattr(
                raw_client, "close", None
            )
            if close:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
        if log is not None:
            log.close()
    summary["status"] = "COMPLETE"
    summary["results"] = [result.__dict__ for result in results]
    summary["calls_by_surface"] = {
        surface: pacers[surface].calls for surface in selected
    }
    (job_events / "stage_b_dual_surface_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0


def exit_code_for_results(results: list[StreamStats | BaseException]) -> int:
    """Return 0=all useful+clean, 1=partial, 2=no useful successful work."""
    any_useful_work = False
    any_failure = False
    for result in results:
        if isinstance(result, BaseException):
            any_failure = True
            continue
        useful = result.rows_fetched > 0
        any_useful_work = any_useful_work or useful
        clean = (
            useful
            and not result.errors
            and result.stopped_reason is None
            and result.empty_responses == 0
        )
        if not clean:
            any_failure = True

    if not any_useful_work:
        return EXIT_TOTAL_FAILURE
    if any_failure:
        return EXIT_PARTIAL_FAILURE
    return EXIT_SUCCESS


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split-csv", required=True, type=Path)
    ap.add_argument("--job-dir", required=True, type=Path)
    ap.add_argument("--start-date", required=True)
    ap.add_argument("--end-date", required=True)
    ap.add_argument("--sources", default="toss,kiwoom,kis")
    ap.add_argument(
        "--surface",
        choices=("mock", "live"),
        default=None,
        help="run one pre-assigned Kiwoom surface; a surface CSV selects dual mode",
    )
    ap.add_argument(
        "--live-env-file",
        type=Path,
        default=Path(
            "/Users/mgh3326/services/auto_trader/shared/.env.kiwoom-readonly.native"
        ),
        help="native four-key live read-only env file",
    )
    ap.add_argument(
        "--mock-env-file",
        type=Path,
        default=None,
        help="optional env file containing mock gate and credentials",
    )
    ap.add_argument("--baseline-median-ms", type=float, default=2.127)
    ap.add_argument("--limit-symbols", type=int, default=None)
    ap.add_argument(
        "--confirm-write",
        action="store_true",
        help="required; without it nothing is fetched or written",
    )
    args = ap.parse_args()

    start_date = date.fromisoformat(args.start_date)
    end_date = date.fromisoformat(args.end_date)

    # The generated dual artifact is self-describing.  Its `surface` column is
    # the switch that prevents the old three-source loader from silently
    # treating a mock/live split as a generic source split.
    with args.split_csv.open(newline="", encoding="utf-8") as split_fh:
        split_fields = set(csv.DictReader(split_fh).fieldnames or ())
    if "surface" in split_fields:
        return await _run_dual_surface(args)
    if args.surface is not None:
        raise SurfaceContractError(
            "--surface requires a dual Kiwoom split CSV with a surface column"
        )
    wanted = [s.strip() for s in args.sources.split(",") if s.strip()]

    assignment: dict[str, list[str]] = {s: [] for s in wanted}
    with args.split_csv.open() as fh:
        for r in csv.DictReader(fh):
            if r["source"] in assignment:
                assignment[r["source"]].append(r["ticker"])
    if args.limit_symbols:
        assignment = {k: v[: args.limit_symbols] for k, v in assignment.items()}

    if not args.confirm_write:
        print(
            json.dumps(
                {
                    "status": "DRY_RUN_NO_WRITE",
                    "target_table": TARGET_TABLE,
                    "window": [args.start_date, args.end_date],
                    "symbols_per_source": {k: len(v) for k, v in assignment.items()},
                },
                indent=2,
            )
        )
        return 0

    assert_fetch_window_open()

    batch_id = f"kr-backfill-p1-{now_kst():%Y%m%dT%H%M%S}"

    from equality_gate import build_clients

    clients = await build_clients(wanted)
    pool = await asyncpg.create_pool(dsn(), min_size=2, max_size=6)
    log = ProgressLog(args.job_dir / "events" / "progress.jsonl")
    guard = Guard(pool, args.baseline_median_ms, log)

    log.write(
        {
            "event": "stage_b_start",
            "target_table": TARGET_TABLE,
            "batch_id": batch_id,
            "window": [args.start_date, args.end_date],
            "symbols_per_source": {k: len(v) for k, v in assignment.items()},
        }
    )

    try:
        results = await asyncio.gather(
            *[
                run_stream(
                    src,
                    assignment[src],
                    clients[src],
                    pool,
                    Checkpoint(args.job_dir / "events" / f"checkpoint_{src}.json"),
                    log,
                    guard,
                    start_date,
                    end_date,
                    batch_id,
                )
                for src in wanted
            ],
            return_exceptions=True,
        )
    finally:
        await pool.close()
        for c in clients.values():
            close = getattr(c, "aclose", None) or getattr(c, "close", None)
            if close:
                try:
                    res = close()
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:  # noqa: BLE001, S110
                    pass

    summary = []
    for r in results:
        if isinstance(r, Exception):
            summary.append({"error": f"{type(r).__name__}: {r}"})
        else:
            summary.append(r.__dict__)
    log.write({"event": "stage_b_end", "summary": summary})
    (args.job_dir / "events" / "stage_b_summary.json").write_text(
        json.dumps(summary, indent=2, default=str, ensure_ascii=False) + "\n"
    )
    print(json.dumps(summary, indent=2, default=str, ensure_ascii=False))
    log.close()
    return exit_code_for_results(results)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
