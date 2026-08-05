"""Append-only staging collector for the Toss combined KRX/NXT 1-minute corpus.

The Toss response for NXT-eligible names combines KRX and NXT activity.  This
collector keeps that fact explicit: it emits a separate STAGING-only Parquet
corpus, never connects to PostgreSQL, never calls order/account endpoints, and
never promotes a row into the KRX-only research corpus.

The caller must supply the sealed top-200 universe and declare a finite call
budget.  A checkpoint is durable after every page.  Final Parquet pages are
write-once; a restart recovers the next cursor from an already-written page
rather than replacing it.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import os
import random
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from datetime import time as local_time
from pathlib import Path
from typing import Any, Protocol

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
from dotenv import load_dotenv

KST = timezone(timedelta(hours=9))

STAGING_CONTRACT = "STAGING_ONLY_NOT_BACKTEST_INPUT"
SOURCE = "TOSS"
VALUE_SEMANTICS = "CLOSE_X_VOLUME_SYNTHETIC"
DEFAULT_START_DATE = date(2021, 12, 20)
EXPECTED_UNIVERSE_SIZE = 200
PAGE_SIZE = 200
INTRADAY_TARGET_TPS_MAX = 2.0
INTRADAY_CHART_HEADROOM_TPS = 3.0
AFTER_CLOSE_TARGET_TPS_MIN = 3.0
AFTER_CLOSE_TARGET_TPS_MAX = 4.0
LOW_REMAINING_FRACTION = 0.4
CONSECUTIVE_429_STOP_AT = 5
CONSECUTIVE_TRANSIENT_RESUME_STOP_AT = 5
SUMMARY_UPDATE_INTERVAL_SECONDS = 60.0
CALL_BUDGET_ACCOUNTING = "cumulative_staging_scope"
DEFAULT_SHARED_REDIS_URL = "redis://127.0.0.1:6379/0"
_SETTINGS_PLACEHOLDERS = {
    "KIS_APP_KEY": "toss-phase2-unused",
    "KIS_APP_SECRET": "toss-phase2-unused",
    "OPENDART_API_KEY": "toss-phase2-unused",
    # A deliberately invalid port ensures an accidental database path fails.
    "DATABASE_URL": "postgresql+asyncpg://tossphase2:tossphase2@127.0.0.1:1/unused",
    "UPBIT_ACCESS_KEY": "toss-phase2-unused",
    "UPBIT_SECRET_KEY": "toss-phase2-unused",
    "SECRET_KEY": "TossPhase2UnusedConfig_20260804_A1b2C3d4E5f6G7h8I9j0KLMNOP",
}


class CollectionStopped(RuntimeError):
    """A deliberate, fail-closed stop that leaves staging resumable."""


class UnclassifiableSessionSegment(CollectionStopped):
    """A candle outside the approved KST session labels must not be staged."""


def _transient_resume_reason(exc: Exception) -> str | None:
    """Classify only failures safe to retry without issuing an OAuth token."""

    if isinstance(exc, httpx.TransportError):
        return "httpx_transport_error"
    if isinstance(exc, CollectionStopped) and str(exc).startswith(
        "shared_toss_token_cache_miss"
    ):
        return "shared_toss_token_cache_miss"
    return None


class TransientResumeBackoff:
    """Bounded retry path for a cache gap or a one-off transport failure.

    It deliberately excludes every other ``CollectionStopped`` reason, 401,
    and 429.  Those retain their specific fail-closed or header-driven paths.
    """

    def __init__(
        self,
        *,
        sleep: Any = asyncio.sleep,
        jitter_fn: Any = random.uniform,
    ) -> None:
        self._sleep = sleep
        self._jitter = jitter_fn
        self._consecutive = 0

    @property
    def consecutive(self) -> int:
        return self._consecutive

    def reset(self) -> None:
        self._consecutive = 0

    async def backoff(self, *, reason: str) -> tuple[int, float]:
        self._consecutive += 1
        if self._consecutive >= CONSECUTIVE_TRANSIENT_RESUME_STOP_AT:
            raise CollectionStopped(
                "transient_resume_exhausted:"
                f"reason={reason}:stop_at={CONSECUTIVE_TRANSIENT_RESUME_STOP_AT}"
            )
        exponential = min(2.0 ** (self._consecutive - 1), 16.0)
        delay_seconds = exponential + self._jitter(0.0, exponential)
        await self._sleep(delay_seconds)
        return self._consecutive, delay_seconds


class CachedTokenProvider(Protocol):
    async def get_access_token(
        self, *, force_reissue: bool = False, failed_token: str | None = None
    ) -> str: ...


class CachedTokenOnlyProvider:
    """Expose only the shared cache-hit path; this collector cannot issue OAuth."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    async def get_access_token(
        self, *, force_reissue: bool = False, failed_token: str | None = None
    ) -> str:
        if force_reissue or failed_token is not None:
            raise CollectionStopped("force_reissue_prohibited_for_toss_phase2")
        token = await self._manager.get_cached_access_token()
        if token is None:
            raise CollectionStopped(
                "shared_toss_token_cache_miss: collector will not issue OAuth"
            )
        return token


class CountingTransport(httpx.AsyncBaseTransport):
    """Count actual Toss HTTP attempts, rather than successful parsed pages."""

    def __init__(self, *, initial_calls: int = 0) -> None:
        if initial_calls < 0:
            raise ValueError("initial_calls must be non-negative")
        self.calls = initial_calls
        self._inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def now_kst() -> datetime:
    return datetime.now(KST)


def _is_intraday(at: datetime) -> bool:
    current = at.astimezone(KST).time()
    return local_time(9, 0) <= current < local_time(20, 0)


class HeaderAdaptiveChartRateLimiter:
    """A chart-only pace controller whose cap comes from Toss response headers.

    The process-local Toss limiter is retained only for non-chart groups.  The
    chart path deliberately does not invoke its static local default: this
    bulk collector's chart cap is determined solely from Toss response headers.
    After the first chart response exposes ``X-RateLimit-Limit``, intraday
    requests reserve three TPS for the production Toss-first chart consumer.
    If the provider lowers its cap below that reservation, the collector fails
    closed before another page request rather than consuming the production
    budget.
    """

    def __init__(
        self,
        base: Any,
        chart_group: Any,
        *,
        now_kst_fn: Any = now_kst,
        monotonic_fn: Any = time.monotonic,
        sleep: Any = asyncio.sleep,
        jitter_fn: Any = random.uniform,
    ) -> None:
        self._base = base
        self._chart_group = chart_group
        self._now_kst = now_kst_fn
        self._monotonic = monotonic_fn
        self._sleep = sleep
        self._jitter = jitter_fn
        self._cap: int | None = None
        self._remaining: int | None = None
        self._reset_seconds: float | None = None
        self._retry_after_seconds: float | None = None
        self._last_chart_call: float | None = None
        self._low_remaining_not_before = 0.0
        self._retry_not_before = 0.0
        self._consecutive_429 = 0

    @property
    def cap(self) -> int | None:
        return self._cap

    @property
    def cap_auto_discovered(self) -> bool:
        return self._cap is not None

    @property
    def consecutive_429(self) -> int:
        return self._consecutive_429

    @property
    def remaining(self) -> int | None:
        return self._remaining

    @property
    def reset_seconds(self) -> float | None:
        return self._reset_seconds

    @property
    def retry_after_seconds(self) -> float | None:
        return self._retry_after_seconds

    def target_tps(self, at: datetime | None = None) -> float:
        """Return a dynamic target, never treating a documented value as fixed."""

        current = at or self._now_kst()
        if _is_intraday(current):
            if self._cap is None:
                # At most one startup request can happen before the response
                # header discovers the cap; no second request is admitted until
                # ``ensure_cap_discovered`` has run.
                return INTRADAY_TARGET_TPS_MAX
            return max(
                0.0,
                min(
                    INTRADAY_TARGET_TPS_MAX,
                    float(self._cap) - INTRADAY_CHART_HEADROOM_TPS,
                ),
            )
        if self._cap is None:
            return AFTER_CLOSE_TARGET_TPS_MIN
        if (
            self._remaining is not None
            and self._remaining / self._cap < LOW_REMAINING_FRACTION
        ):
            return min(AFTER_CLOSE_TARGET_TPS_MIN, float(self._cap))
        return min(AFTER_CLOSE_TARGET_TPS_MAX, float(self._cap))

    def intraday_headroom_preserved(self) -> bool | None:
        if self._cap is None:
            return None
        target = max(
            0.0,
            min(
                INTRADAY_TARGET_TPS_MAX,
                float(self._cap) - INTRADAY_CHART_HEADROOM_TPS,
            ),
        )
        return self._cap - target >= INTRADAY_CHART_HEADROOM_TPS

    def observe_response(
        self,
        group: Any,
        status_code: int,
        rate_limit_headers: Any,
    ) -> None:
        """Consume one response's redacted rate-limit metadata.

        ``TossReadClient`` calls this for successes and failures before it parses
        the envelope, so a 429 still contributes its Retry-After and reset data.
        """

        if group != self._chart_group:
            return
        limit = getattr(rate_limit_headers, "limit", None)
        remaining = getattr(rate_limit_headers, "remaining", None)
        reset_seconds = getattr(rate_limit_headers, "reset_seconds", None)
        retry_after_seconds = getattr(rate_limit_headers, "retry_after_seconds", None)
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            self._cap = limit
        self._remaining = (
            remaining
            if isinstance(remaining, int)
            and not isinstance(remaining, bool)
            and remaining >= 0
            else None
        )
        self._reset_seconds = (
            float(reset_seconds)
            if isinstance(reset_seconds, (float, int)) and reset_seconds >= 0.0
            else None
        )
        self._retry_after_seconds = (
            float(retry_after_seconds)
            if isinstance(retry_after_seconds, (float, int))
            and retry_after_seconds >= 0.0
            else None
        )
        self._schedule_low_remaining_recovery()
        if status_code < 400:
            self._consecutive_429 = 0

    def _schedule_low_remaining_recovery(self) -> None:
        if (
            self._cap is None
            or self._remaining is None
            or self._reset_seconds is None
            or self._remaining / self._cap >= LOW_REMAINING_FRACTION
        ):
            self._low_remaining_not_before = 0.0
            return
        recovery_floor = math.ceil(self._cap * LOW_REMAINING_FRACTION)
        tokens_to_rebuild = max(recovery_floor - self._remaining + 1, 1)
        self._low_remaining_not_before = max(
            self._low_remaining_not_before,
            self._monotonic() + self._reset_seconds * tokens_to_rebuild,
        )

    def ensure_cap_discovered(self) -> None:
        """Fail closed if the documented cap cannot protect shared chart traffic."""

        if self._cap is None:
            raise CollectionStopped(
                "chart_rate_limit_cap_not_discovered:X-RateLimit-Limit missing"
            )
        if _is_intraday(self._now_kst()) and self.target_tps() <= 0.0:
            raise CollectionStopped(
                "chart_headroom_unavailable: "
                f"cap={self._cap} reserve={INTRADAY_CHART_HEADROOM_TPS:g}"
            )

    async def acquire(self, group: Any) -> None:
        if group != self._chart_group:
            await self._base.acquire(group)
            return
        if self._cap is not None:
            self.ensure_cap_discovered()
        target_tps = self.target_tps()
        delay_until = max(self._low_remaining_not_before, self._retry_not_before)
        if self._last_chart_call is not None and target_tps > 0.0:
            delay_until = max(delay_until, self._last_chart_call + 1.0 / target_tps)
        wait_seconds = delay_until - self._monotonic()
        if wait_seconds > 0.0:
            await self._sleep(wait_seconds)
        # Do not defer chart calls to TossRateLimiter here.  Its established
        # per-process chart default is not shared across clients and would turn
        # a documented value into a hidden fixed cap for this collector.
        self._last_chart_call = self._monotonic()

    async def backoff_after_429(self) -> tuple[int, float]:
        """Honor Retry-After and add the documented exponential backoff+jitter."""

        self._consecutive_429 += 1
        if self._consecutive_429 >= CONSECUTIVE_429_STOP_AT:
            raise CollectionStopped(f"consecutive_chart_429:{CONSECUTIVE_429_STOP_AT}")
        exponential = min(2.0 ** (self._consecutive_429 - 1), 16.0)
        backoff_seconds = exponential + self._jitter(0.0, exponential)
        retry_after_seconds = self._retry_after_seconds or 0.0
        delay_seconds = max(retry_after_seconds, backoff_seconds)
        self._retry_not_before = max(
            self._retry_not_before, self._monotonic() + delay_seconds
        )
        await self._sleep(delay_seconds)
        return self._consecutive_429, delay_seconds


@dataclass(frozen=True)
class SharedErrorBaseline:
    sequence: int


class SharedTossHealthMonitor:
    """Stop this collector for non-rate-limit shared Toss failures only.

    A 429 is an overload signal with a documented recovery path, not an
    immediate stop condition.  Advance the baseline so one external 429 cannot
    repeatedly trip the collector; the collector's own header-aware retry path
    controls subsequent chart requests.
    """

    def __init__(self, read_signal: Any) -> None:
        self._read_signal = read_signal
        self._baseline = SharedErrorBaseline(sequence=0)

    async def start(self) -> None:
        signal = await self._read_signal()
        self._baseline = SharedErrorBaseline(
            sequence=signal.sequence if signal is not None else 0
        )

    async def assert_healthy(self) -> None:
        signal = await self._read_signal()
        if signal is not None and signal.sequence > self._baseline.sequence:
            self._baseline = SharedErrorBaseline(sequence=signal.sequence)
            if signal.status_code == 429:
                return
            raise CollectionStopped(
                "shared_toss_error_observed: "
                f"status={signal.status_code} type={signal.error_type} "
                f"code={signal.error_code} sequence={signal.sequence}"
            )


_TOSS_ERROR_LINE = re.compile(
    r"(?:toss|openapi\.tossinvest).{0,160}(?:\b401\b|\b429\b|error|exception)"
    r"|(?:\b401\b|\b429\b).{0,160}(?:toss|openapi\.tossinvest)",
    re.IGNORECASE,
)
_HTTP_429_LINE = re.compile(r"\b429\b")


class ProductionTossLogMonitor:
    """Tail existing production logs without changing their running processes."""

    def __init__(self, paths: list[Path]) -> None:
        self._paths = paths
        self._positions: dict[Path, tuple[int, int]] = {}

    async def start(self) -> None:
        for path in self._paths:
            if not path.is_file():
                raise CollectionStopped(f"production_toss_log_missing:{path}")
            stat = path.stat()
            self._positions[path] = (stat.st_ino, stat.st_size)

    async def assert_healthy(self) -> None:
        for path in self._paths:
            if not path.is_file():
                raise CollectionStopped(f"production_toss_log_missing:{path}")
            stat = path.stat()
            old_inode, offset = self._positions[path]
            if stat.st_ino != old_inode or stat.st_size < offset:
                offset = 0
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(offset)
                appended = fh.read()
            self._positions[path] = (stat.st_ino, stat.st_size)
            for line in appended.splitlines():
                if not _TOSS_ERROR_LINE.search(line):
                    continue
                if _HTTP_429_LINE.search(line):
                    # Retry-After is not available in a redacted log line.  The
                    # collector must not treat it as a fatal pipe error; its own
                    # response headers govern retries and adaptive pacing.
                    continue
                # Do not copy log content: it can contain an upstream request id
                # and is unnecessary for the collector's fail-closed decision.
                raise CollectionStopped(f"production_toss_error_log_observed:{path}")


class CombinedTossHealthMonitor:
    def __init__(
        self,
        shared: SharedTossHealthMonitor,
        production_logs: ProductionTossLogMonitor,
    ) -> None:
        self._shared = shared
        self._production_logs = production_logs

    async def start(self) -> None:
        await self._shared.start()
        await self._production_logs.start()

    async def assert_healthy(self) -> None:
        await self._shared.assert_healthy()
        await self._production_logs.assert_healthy()


class Checkpoint:
    """Durable per-symbol cursor state; only this file is rewritten on resume."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {"version": 1, "symbols": {}}
        if path.exists():
            self.data = json.loads(path.read_text())

    def state_for(self, symbol: str) -> dict[str, Any]:
        return self.data.setdefault("symbols", {}).setdefault(
            symbol,
            {"before": None, "done": False, "pages": 0, "rows_staged": 0},
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n")
        os.replace(temporary, self.path)


class ProgressLog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8", buffering=1)

    def write(self, event: str, **fields: Any) -> None:
        record = {"event": event, "at_kst": now_kst().isoformat(), **fields}
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        self._fh.close()


def cumulative_calls_from_progress(path: Path) -> int:
    """Return the staging-wide chart-call total across collector restarts.

    Older runs recorded a per-process counter.  Once a run declares the
    cumulative accounting marker, its counter already includes all preceding
    calls and must replace—not be added to—the legacy total.
    """

    if not path.exists():
        return 0

    legacy_total = 0
    cumulative_max = 0
    run_seen = False
    run_is_cumulative = False
    run_max = 0

    def finish_run() -> None:
        nonlocal legacy_total, cumulative_max
        if not run_seen:
            return
        if run_is_cumulative:
            cumulative_max = max(cumulative_max, run_max)
        else:
            legacy_total += run_max

    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"malformed progress log at line {line_number}; refusing to reset call budget"
            ) from exc
        if not isinstance(record, dict):
            raise ValueError(
                f"malformed progress log at line {line_number}; expected object"
            )
        if record.get("event") == "collection_started":
            finish_run()
            run_seen = True
            run_is_cumulative = record.get("call_accounting") == CALL_BUDGET_ACCOUNTING
            prior_calls = record.get("prior_calls_actual", 0)
            if not isinstance(prior_calls, int) or isinstance(prior_calls, bool):
                raise ValueError(
                    f"malformed prior_calls_actual at progress line {line_number}"
                )
            if prior_calls < 0:
                raise ValueError(
                    f"negative prior_calls_actual at progress line {line_number}"
                )
            run_max = prior_calls if run_is_cumulative else 0

        if not run_seen or "calls_actual" not in record:
            continue
        calls = record["calls_actual"]
        if not isinstance(calls, int) or isinstance(calls, bool) or calls < 0:
            raise ValueError(f"malformed calls_actual at progress line {line_number}")
        run_max = max(run_max, calls)

    finish_run()
    return max(legacy_total, cumulative_max)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_universe(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    symbols = [str(row.get("ticker", "")).strip() for row in rows]
    if len(symbols) != EXPECTED_UNIVERSE_SIZE or any(not symbol for symbol in symbols):
        raise ValueError(
            f"universe must contain exactly {EXPECTED_UNIVERSE_SIZE} non-empty tickers"
        )
    if len(set(symbols)) != len(symbols):
        raise ValueError("universe contains duplicate tickers")
    return symbols


def load_readonly_toss_environment(env_file: Path) -> str:
    """Load the dedicated three-key Toss env without reading a general env file."""
    if "prod" in env_file.name.casefold() or not env_file.is_file():
        raise ValueError("dedicated non-production-named Toss env file is required")
    inherited_env_file = os.environ.get("ENV_FILE", "")
    if inherited_env_file and "prod" in Path(inherited_env_file).name.casefold():
        raise ValueError("ENV_FILE must not point at a production env file")
    load_dotenv(env_file, override=True)
    if os.getenv("TOSS_LIVE_ORDER_MUTATIONS_ENABLED", "").casefold() == "true":
        raise ValueError("TOSS_LIVE_ORDER_MUTATIONS_ENABLED must not be enabled")
    if os.getenv("TOSS_API_ENABLED", "").casefold() not in {"1", "true", "yes"}:
        raise ValueError("TOSS_API_ENABLED must be truthy in the readonly env file")
    missing = [
        name
        for name in ("TOSS_API_CLIENT_ID", "TOSS_API_CLIENT_SECRET")
        if not os.getenv(name)
    ]
    if missing:
        raise ValueError("readonly Toss env missing: " + ", ".join(missing))
    # Settings imports require unrelated fields, but no route in this process
    # uses them.  Keep their values inert and point ENV_FILE only at the scoped
    # Toss file so it cannot fall back to a general deployment dotenv.
    os.environ["ENV_FILE"] = str(env_file)
    for name, value in _SETTINGS_PLACEHOLDERS.items():
        os.environ.setdefault(name, value)
    if not os.getenv("REDIS_URL"):
        os.environ["REDIS_URL"] = DEFAULT_SHARED_REDIS_URL
        return f"defaulted:{DEFAULT_SHARED_REDIS_URL}"
    return "process_env_preexisting"


def resolve_toss_base_url(configured: str | None, default: str) -> str:
    """Mirror ``TossReadClient.from_settings`` for a scoped env file."""
    return str(configured or default)


def classify_session_segment(timestamp_kst: datetime) -> str:
    """Classify by clock time only; it asserts no execution venue claim."""
    clock = timestamp_kst.astimezone(KST).time()
    if local_time(8, 0) <= clock < local_time(9, 0):
        return "NXT_PRE"
    if local_time(9, 0) <= clock <= local_time(15, 30):
        return "KRX_REGULAR"
    if local_time(15, 30) < clock <= local_time(20, 0):
        return "NXT_POST"
    raise UnclassifiableSessionSegment(
        "session_segment_unclassifiable:" + timestamp_kst.astimezone(KST).isoformat()
    )


def normalize_timestamp(raw: Any) -> tuple[datetime, datetime]:
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    timestamp_kst = parsed.astimezone(KST).replace(second=0, microsecond=0)
    return timestamp_kst.astimezone(UTC), timestamp_kst


def page_token(before: str | None) -> str:
    raw = before if before is not None else "initial"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def page_path(staging_dir: Path, symbol: str, before: str | None) -> Path:
    return (
        staging_dir / "data" / f"symbol={symbol}" / f"part-{page_token(before)}.parquet"
    )


def _page_metadata(
    *,
    batch_id: str,
    symbol: str,
    request_before: str | None,
    next_before: str | None,
) -> dict[bytes, bytes]:
    metadata = {
        "artifact_state": STAGING_CONTRACT,
        "source": SOURCE,
        "value_semantics": VALUE_SEMANTICS,
        "batch_id": batch_id,
        "symbol": symbol,
        "request_before": request_before or "",
        "next_before": next_before or "",
        "pre_nxt_contract": "NULL=UNKNOWN_UNTIL_OFFICIAL_LAUNCH_DATE",
        "promotion_performed": "false",
    }
    return {key.encode(): value.encode() for key, value in metadata.items()}


def _existing_next_before(path: Path) -> str | None:
    metadata = pq.read_metadata(path).metadata or {}
    raw = metadata.get(b"next_before", b"")
    value = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    return value or None


def write_page(
    *,
    staging_dir: Path,
    symbol: str,
    request_before: str | None,
    next_before: str | None,
    rows: list[dict[str, Any]],
    batch_id: str,
) -> tuple[Path | None, bool]:
    """Write an immutable page, returning ``(path, existed_already)``."""
    if not rows:
        return None, False
    destination = page_path(staging_dir, symbol, request_before)
    if destination.exists():
        return destination, True
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    table = pa.Table.from_pylist(rows)
    table = table.replace_schema_metadata(
        {
            **(table.schema.metadata or {}),
            **_page_metadata(
                batch_id=batch_id,
                symbol=symbol,
                request_before=request_before,
                next_before=next_before,
            ),
        }
    )
    pq.write_table(table, temporary, compression="zstd")
    with temporary.open("rb") as fh:
        os.fsync(fh.fileno())
    try:
        # link(2) is create-only: another process cannot be silently overwritten.
        os.link(temporary, destination)
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        return destination, True
    temporary.unlink(missing_ok=True)
    return destination, False


def make_rows(
    *,
    candles: list[Any],
    symbol: str,
    start_date: date,
    last_eligible_date: date,
    retrieved_at: datetime,
    batch_id: str,
    official_nxt_launch_date: date | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candle in candles:
        time_utc, timestamp_kst = normalize_timestamp(candle.timestamp)
        session_date = timestamp_kst.date()
        # The newest KST session is intentionally excluded, including every
        # still-forming minute in it.  This is not a coverage failure.
        if not start_date <= session_date <= last_eligible_date:
            continue
        volume = float(candle.volume)
        close = float(candle.close_price)
        rows.append(
            {
                "time_utc": time_utc,
                "session_date_kst": session_date,
                "symbol": symbol,
                "session_segment": classify_session_segment(timestamp_kst),
                "source": SOURCE,
                "open": float(candle.open_price),
                "high": float(candle.high_price),
                "low": float(candle.low_price),
                "close": close,
                "volume": volume,
                "value": close * volume,
                "value_semantics": VALUE_SEMANTICS,
                "is_padding": volume == 0.0,
                # Without an exact, sourced NXT launch date this remains NULL
                # (UNKNOWN), never an approximation such as "2025-03".
                "pre_nxt": (
                    session_date < official_nxt_launch_date
                    if official_nxt_launch_date is not None
                    else None
                ),
                "retrieved_at": retrieved_at,
                "batch_id": batch_id,
            }
        )
    return rows


def build_manifest(
    *,
    symbols: list[str],
    universe_csv: Path,
    start_date: date,
    last_eligible_date: date,
    call_budget: int,
    batch_id: str,
    official_nxt_launch_date: date | None,
    official_nxt_launch_source: str | None,
) -> dict[str, Any]:
    return {
        "artifact_state": STAGING_CONTRACT,
        "backtest_input": False,
        "source": SOURCE,
        "scope": "top-200 x 4.6y",
        "symbol_count": len(symbols),
        "universe_csv": str(universe_csv),
        "universe_sha256": sha256_file(universe_csv),
        "window": {
            "start_date": start_date.isoformat(),
            "end_date": last_eligible_date.isoformat(),
        },
        "latest_session_excluded": {
            "enabled": True,
            "definition": "all rows with session_date_kst >= collection_start_kst_date are excluded",
        },
        "call_budget_declared": call_budget,
        "call_budget_accounting": CALL_BUDGET_ACCOUNTING,
        "page_size": PAGE_SIZE,
        "rate_limit_control": {
            "mode": "response_header_adaptive",
            "cap_source": "X-RateLimit-Limit",
            "hardcoded_chart_cap": False,
            "remaining_low_fraction": LOW_REMAINING_FRACTION,
            "intraday": {
                "target_tps_max": INTRADAY_TARGET_TPS_MAX,
                "reserved_production_chart_headroom_tps": INTRADAY_CHART_HEADROOM_TPS,
            },
            "after_close": {
                "target_tps_min": AFTER_CLOSE_TARGET_TPS_MIN,
                "target_tps_max": AFTER_CLOSE_TARGET_TPS_MAX,
            },
            "on_429": {
                "retry_after_required": True,
                "exponential_backoff_with_jitter": True,
                "consecutive_stop_at": CONSECUTIVE_429_STOP_AT,
            },
            "on_isolated_cache_or_transport_failure": {
                "same_checkpoint_cursor_retry": True,
                "shared_cache_only_no_token_issue": True,
                "exponential_backoff_seconds": [1, 2, 4, 8],
                "jitter": True,
                "consecutive_stop_at": CONSECUTIVE_TRANSIENT_RESUME_STOP_AT,
            },
        },
        "value_semantics": VALUE_SEMANTICS,
        "padding_contract": "is_padding=true iff provider volume is zero; not coverage missing",
        "session_segment_contract": "time-of-day only; never a venue assertion",
        "pre_nxt": {
            "launch_date": official_nxt_launch_date.isoformat()
            if official_nxt_launch_date
            else None,
            "launch_date_source": official_nxt_launch_source,
            "unknown_when_launch_date_absent": True,
            "promotion_performed": False,
        },
        "batch_id": batch_id,
        "database_load_performed": False,
        "order_or_account_endpoint_calls": 0,
        "token_mode": "shared_cached_token_reuse_only_no_issue_no_force_reissue",
    }


def initialize_staging(
    *,
    staging_dir: Path,
    manifest: dict[str, Any],
) -> None:
    staging_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = staging_dir / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text())
        immutable_keys = (
            "artifact_state",
            "scope",
            "symbol_count",
            "universe_sha256",
            "window",
            "call_budget_declared",
            "batch_id",
        )
        if any(existing.get(key) != manifest.get(key) for key in immutable_keys):
            raise ValueError("existing staging manifest does not match this collection")
        # Pages are write-once, but the control policy is operational metadata.
        # Upgrade the previously staged corpus from the withdrawn fixed-interval
        # policy before a later, separately authorized resume can append pages.
        if existing.get("rate_limit_control") != manifest["rate_limit_control"]:
            existing.pop("pacing", None)
            existing["rate_limit_control"] = manifest["rate_limit_control"]
            manifest_path.write_text(
                json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        return
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    (staging_dir / "STAGING_ONLY.md").write_text(
        "# STAGING ONLY — NOT A BACKTEST INPUT\n\n"
        "This is an append-only Toss combined KRX/NXT collection. It must not be "
        "loaded into a database or used by a backtest until a separately approved "
        "review and load step. `is_padding=true` is a provider placeholder, and "
        "`value` is synthetic `close * volume`. Chart rate control is derived from "
        "the Toss response headers; the collector reserves intraday headroom for "
        "production chart readers.\n",
        encoding="utf-8",
    )


@dataclass
class CollectionStats:
    symbols_total: int
    symbols_done: int = 0
    rows_staged: int = 0
    pages_staged: int = 0
    pages_reused: int = 0
    http_401: int = 0
    http_429: int = 0
    chart_rate_limit_cap: int | None = None
    rate_limit_backoffs: int = 0
    transient_resume_failures: int = 0
    transient_resume_backoffs: int = 0
    stopped_reason: str | None = None


def collection_stats_from_checkpoint(
    *,
    staging_dir: Path,
    symbols: list[str],
) -> CollectionStats:
    """Seed a resumed summary from durable staging state, not process memory."""

    checkpoint = Checkpoint(staging_dir / "state" / "checkpoint.json")
    states = checkpoint.data.get("symbols", {})
    if not isinstance(states, dict):
        raise ValueError("checkpoint symbols must be an object")
    unexpected_symbols = set(states) - set(symbols)
    if unexpected_symbols:
        raise ValueError(
            "checkpoint contains symbols outside sealed universe: "
            + ", ".join(sorted(unexpected_symbols))
        )

    symbols_done = 0
    pages_staged = 0
    rows_staged = 0
    for symbol, state in states.items():
        if not isinstance(state, dict):
            raise ValueError(f"checkpoint state for {symbol} must be an object")
        pages = state.get("pages", 0)
        rows = state.get("rows_staged", 0)
        if (
            not isinstance(pages, int)
            or isinstance(pages, bool)
            or pages < 0
            or not isinstance(rows, int)
            or isinstance(rows, bool)
            or rows < 0
        ):
            raise ValueError(f"checkpoint counters for {symbol} must be non-negative")
        pages_staged += pages
        rows_staged += rows
        if state.get("done") is True:
            symbols_done += 1

    return CollectionStats(
        symbols_total=len(symbols),
        symbols_done=symbols_done,
        pages_staged=pages_staged,
        rows_staged=rows_staged,
    )


class LatestSummaryWriter:
    """Atomically expose a fresh, redacted staging-only progress snapshot."""

    def __init__(
        self,
        *,
        path: Path,
        stats: CollectionStats,
        transport: CountingTransport,
        call_budget: int,
        monotonic_fn: Any = time.monotonic,
        now_kst_fn: Any = now_kst,
    ) -> None:
        self._path = path
        self._stats = stats
        self._transport = transport
        self._call_budget = call_budget
        self._monotonic = monotonic_fn
        self._now_kst = now_kst_fn
        self._last_write_at: float | None = None

    def _payload(self, *, collector_state: str) -> dict[str, Any]:
        return {
            **asdict(self._stats),
            "calls_actual": self._transport.calls,
            "call_budget_declared": self._call_budget,
            "call_budget_accounting": CALL_BUDGET_ACCOUNTING,
            "call_budget_remaining": max(self._call_budget - self._transport.calls, 0),
            "token_issued_by_collector": False,
            "database_load_performed": False,
            "artifact_state": STAGING_CONTRACT,
            "collector_state": collector_state,
            "updated_at_kst": self._now_kst().isoformat(),
        }

    def write(self, *, collector_state: str) -> dict[str, Any]:
        payload = self._payload(collector_state=collector_state)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._path)
        self._last_write_at = self._monotonic()
        return payload

    def maybe_write(self) -> None:
        if (
            self._last_write_at is None
            or self._monotonic() - self._last_write_at
            >= SUMMARY_UPDATE_INTERVAL_SECONDS
        ):
            self.write(collector_state="RUNNING")


async def _backoff_transient_failure(
    *,
    exc: Exception,
    transient_resumer: TransientResumeBackoff,
    progress: ProgressLog,
    stats: CollectionStats,
    calls_actual: int,
    on_progress: Callable[[], None] | None,
) -> bool:
    reason = _transient_resume_reason(exc)
    if reason is None:
        return False
    stats.transient_resume_failures += 1
    consecutive, delay_seconds = await transient_resumer.backoff(reason=reason)
    stats.transient_resume_backoffs += 1
    progress.write(
        "transient_resume_backoff",
        reason=reason,
        consecutive_transient_failures=consecutive,
        delay_seconds=delay_seconds,
        calls_actual=calls_actual,
    )
    if on_progress is not None:
        on_progress()
    return True


async def _preflight_cached_token(
    *,
    token_provider: CachedTokenProvider,
    transient_resumer: TransientResumeBackoff,
    progress: ProgressLog,
    stats: CollectionStats,
    transport: CountingTransport,
    on_progress: Callable[[], None] | None,
) -> None:
    """Wait through a short shared-cache gap without ever issuing OAuth."""

    while True:
        try:
            await token_provider.get_access_token()
        except Exception as exc:  # noqa: BLE001
            if await _backoff_transient_failure(
                exc=exc,
                transient_resumer=transient_resumer,
                progress=progress,
                stats=stats,
                calls_actual=transport.calls,
                on_progress=on_progress,
            ):
                continue
            raise
        transient_resumer.reset()
        return


async def collect(
    *,
    client: Any,
    transport: CountingTransport,
    chart_pacer: HeaderAdaptiveChartRateLimiter,
    monitor: CombinedTossHealthMonitor,
    staging_dir: Path,
    symbols: list[str],
    start_date: date,
    last_eligible_date: date,
    call_budget: int,
    batch_id: str,
    official_nxt_launch_date: date | None,
    progress: ProgressLog,
    stats: CollectionStats,
    transient_resumer: TransientResumeBackoff | None = None,
    on_progress: Callable[[], None] | None = None,
) -> CollectionStats:
    checkpoint = Checkpoint(staging_dir / "state" / "checkpoint.json")
    transient_resumer = transient_resumer or TransientResumeBackoff()

    for symbol in symbols:
        state = checkpoint.state_for(symbol)
        if state["done"]:
            continue
        while not state["done"]:
            await monitor.assert_healthy()
            if transport.calls >= call_budget:
                raise CollectionStopped(f"call_budget_reached:{call_budget}")
            request_before = state["before"]
            try:
                page = await client.candles(
                    symbol,
                    interval="1m",
                    count=PAGE_SIZE,
                    before=request_before,
                )
            except Exception as exc:  # noqa: BLE001
                status = getattr(exc, "status_code", None)
                if status == 401:
                    stats.http_401 += 1
                if status == 429:
                    stats.http_429 += 1
                    # The startup 429 is still the first response from which
                    # the provider must reveal the live cap.  Never send a
                    # second chart request under an unknown cap.
                    chart_pacer.ensure_cap_discovered()
                    consecutive, delay_seconds = await chart_pacer.backoff_after_429()
                    stats.rate_limit_backoffs += 1
                    progress.write(
                        "chart_429_backoff",
                        consecutive_429=consecutive,
                        delay_seconds=delay_seconds,
                        retry_after_seconds=chart_pacer.retry_after_seconds,
                        rate_limit_cap=chart_pacer.cap,
                        rate_limit_remaining=chart_pacer.remaining,
                        rate_limit_reset_seconds=chart_pacer.reset_seconds,
                        calls_actual=transport.calls,
                    )
                    # Do not advance the checkpoint.  Retrying the same cursor
                    # is safe because no Parquet page has been written for it.
                    continue
                if await _backoff_transient_failure(
                    exc=exc,
                    transient_resumer=transient_resumer,
                    progress=progress,
                    stats=stats,
                    calls_actual=transport.calls,
                    on_progress=on_progress,
                ):
                    # The checkpoint deliberately remains on the same cursor.
                    continue
                raise CollectionStopped(
                    f"toss_request_failed:{type(exc).__name__}:status={status}"
                ) from exc

            chart_pacer.ensure_cap_discovered()
            transient_resumer.reset()
            if stats.chart_rate_limit_cap != chart_pacer.cap:
                stats.chart_rate_limit_cap = chart_pacer.cap
                progress.write(
                    "chart_rate_limit_cap_discovered",
                    cap=chart_pacer.cap,
                    remaining=chart_pacer.remaining,
                    reset_seconds=chart_pacer.reset_seconds,
                    target_tps=chart_pacer.target_tps(),
                    intraday_headroom_preserved=chart_pacer.intraday_headroom_preserved(),
                )
            next_before = page.next_before
            rows = make_rows(
                candles=list(page.candles),
                symbol=symbol,
                start_date=start_date,
                last_eligible_date=last_eligible_date,
                retrieved_at=datetime.now(UTC),
                batch_id=batch_id,
                official_nxt_launch_date=official_nxt_launch_date,
            )
            destination, reused = write_page(
                staging_dir=staging_dir,
                symbol=symbol,
                request_before=request_before,
                next_before=next_before,
                rows=rows,
                batch_id=batch_id,
            )
            if reused and destination is not None:
                next_before = _existing_next_before(destination)
                stats.pages_reused += 1
            elif destination is not None:
                stats.pages_staged += 1
                stats.rows_staged += len(rows)

            state["before"] = next_before
            state["pages"] += 1
            state["rows_staged"] += len(rows) if not reused else 0
            if not page.candles or not next_before:
                state["done"] = True
                stats.symbols_done += 1
            checkpoint.save()
            progress.write(
                "page",
                symbol=symbol,
                request_before=request_before,
                next_before=next_before,
                rows_received=len(page.candles),
                rows_staged=len(rows),
                output=str(destination) if destination is not None else None,
                output_reused=reused,
                calls_actual=transport.calls,
                rate_limit_cap=chart_pacer.cap,
                rate_limit_remaining=chart_pacer.remaining,
                rate_limit_reset_seconds=chart_pacer.reset_seconds,
                target_tps=chart_pacer.target_tps(),
            )
            if on_progress is not None:
                on_progress()
            await monitor.assert_healthy()
    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging-dir", required=True, type=Path)
    parser.add_argument("--universe-csv", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument(
        "--start-date", type=date.fromisoformat, default=DEFAULT_START_DATE
    )
    parser.add_argument("--last-eligible-date", required=True, type=date.fromisoformat)
    parser.add_argument("--call-budget", required=True, type=int)
    parser.add_argument("--official-nxt-launch-date", type=date.fromisoformat)
    parser.add_argument("--official-nxt-launch-source")
    parser.add_argument(
        "--production-log",
        action="append",
        type=Path,
        default=[],
        help="existing production log to tail for new Toss 401/429/error lines",
    )
    parser.add_argument("--confirm-staging", action="store_true")
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> int:
    if not args.confirm_staging:
        raise SystemExit("--confirm-staging is required; no Toss call was made")
    if args.call_budget <= 0:
        raise ValueError("--call-budget must be positive")
    if not args.production_log:
        raise ValueError("at least one --production-log is required")
    if args.official_nxt_launch_date and not args.official_nxt_launch_source:
        raise ValueError("official NXT launch date requires its exact source")
    collection_start_date = now_kst().date()
    if args.last_eligible_date >= collection_start_date:
        raise ValueError(
            "--last-eligible-date must precede collection-start KST date; "
            "latest session is excluded"
        )
    if args.start_date > args.last_eligible_date:
        raise ValueError("start date is after last eligible date")
    redis_url_source = load_readonly_toss_environment(args.env_file)

    # Delay all app imports until the dedicated readonly environment is loaded.
    from app.core.config import settings
    from app.services.brokers.toss.auth import TossOAuthTokenManager
    from app.services.brokers.toss.client import TossReadClient
    from app.services.brokers.toss.health import read_toss_api_error_signal
    from app.services.brokers.toss.rate_limiter import (
        TossApiGroup,
        get_shared_rate_limiter,
    )
    from app.services.brokers.toss.transport import DEFAULT_TOSS_BASE_URL

    symbols = load_universe(args.universe_csv)
    existing_manifest_path = args.staging_dir / "manifest.json"
    if existing_manifest_path.exists():
        batch_id = str(json.loads(existing_manifest_path.read_text())["batch_id"])
    else:
        batch_id = f"toss-phase2-staging-{now_kst():%Y%m%dT%H%M%S}"
    manifest = build_manifest(
        symbols=symbols,
        universe_csv=args.universe_csv,
        start_date=args.start_date,
        last_eligible_date=args.last_eligible_date,
        call_budget=args.call_budget,
        batch_id=batch_id,
        official_nxt_launch_date=args.official_nxt_launch_date,
        official_nxt_launch_source=args.official_nxt_launch_source,
    )
    initialize_staging(staging_dir=args.staging_dir, manifest=manifest)
    existing_manifest = json.loads((args.staging_dir / "manifest.json").read_text())
    batch_id = str(existing_manifest["batch_id"])
    progress_path = args.staging_dir / "events" / "progress.jsonl"
    prior_calls_actual = cumulative_calls_from_progress(progress_path)
    progress = ProgressLog(progress_path)
    transport = CountingTransport(initial_calls=prior_calls_actual)
    shared_limiter = get_shared_rate_limiter()
    manager = TossOAuthTokenManager.from_settings(settings, rate_limiter=shared_limiter)
    token_provider = CachedTokenOnlyProvider(manager)
    chart_pacer = HeaderAdaptiveChartRateLimiter(
        shared_limiter, TossApiGroup.MARKET_DATA_CHART
    )
    client = TossReadClient(
        token_manager=token_provider,
        base_url=resolve_toss_base_url(
            getattr(settings, "toss_api_base_url", None), DEFAULT_TOSS_BASE_URL
        ),
        transport=transport,
        rate_limiter=chart_pacer,
        retry_on_429=False,
        retry_auth_reissue=False,
        response_observer=chart_pacer.observe_response,
        # This optional reader must not emit its own errors as production health
        # signals while it is applying its dedicated 429 recovery policy.
        publish_error_signals=False,
    )
    monitor = CombinedTossHealthMonitor(
        SharedTossHealthMonitor(read_toss_api_error_signal),
        ProductionTossLogMonitor(args.production_log),
    )
    stats = collection_stats_from_checkpoint(
        staging_dir=args.staging_dir,
        symbols=symbols,
    )
    transient_resumer = TransientResumeBackoff()
    summary_writer = LatestSummaryWriter(
        path=args.staging_dir / "events" / "latest_summary.json",
        stats=stats,
        transport=transport,
        call_budget=args.call_budget,
    )
    try:
        await monitor.start()
        progress.write(
            "collection_started",
            scope="top-200 x 4.6y",
            call_budget_declared=args.call_budget,
            call_accounting=CALL_BUDGET_ACCOUNTING,
            prior_calls_actual=prior_calls_actual,
            call_budget_remaining=max(args.call_budget - prior_calls_actual, 0),
            cap_auto_discovery="X-RateLimit-Limit response header",
            intraday_target_tps_max=INTRADAY_TARGET_TPS_MAX,
            intraday_production_chart_headroom_tps=INTRADAY_CHART_HEADROOM_TPS,
            low_remaining_fraction=LOW_REMAINING_FRACTION,
            after_close_target_tps_range=[
                AFTER_CLOSE_TARGET_TPS_MIN,
                AFTER_CLOSE_TARGET_TPS_MAX,
            ],
            consecutive_429_stop_at=CONSECUTIVE_429_STOP_AT,
            latest_session_excluded=True,
            latest_session_definition=(
                "session_date_kst >= collection_start_kst_date is excluded"
            ),
            token_mode="shared_cached_token_reuse_only_no_issue_no_force_reissue",
            redis_url_source=redis_url_source,
            settings_placeholders=sorted(_SETTINGS_PLACEHOLDERS),
            production_logs=[str(path) for path in args.production_log],
        )
        summary_writer.write(collector_state="RUNNING")
        # This preflight can only hit shared Redis.  A short cache gap retries
        # the exact same cache lookup and never starts an OAuth issuance.
        await _preflight_cached_token(
            token_provider=token_provider,
            transient_resumer=transient_resumer,
            progress=progress,
            stats=stats,
            transport=transport,
            on_progress=summary_writer.maybe_write,
        )
        stats = await collect(
            client=client,
            transport=transport,
            chart_pacer=chart_pacer,
            monitor=monitor,
            staging_dir=args.staging_dir,
            symbols=symbols,
            start_date=args.start_date,
            last_eligible_date=args.last_eligible_date,
            call_budget=args.call_budget,
            batch_id=batch_id,
            official_nxt_launch_date=args.official_nxt_launch_date,
            progress=progress,
            stats=stats,
            transient_resumer=transient_resumer,
            on_progress=summary_writer.maybe_write,
        )
    except CollectionStopped as exc:
        stats.stopped_reason = str(exc)
        progress.write(
            "collection_stopped", reason=str(exc), calls_actual=transport.calls
        )
    except Exception as exc:  # noqa: BLE001
        stats.stopped_reason = f"unexpected:{type(exc).__name__}:{exc}"
        progress.write(
            "collection_stopped",
            reason=stats.stopped_reason,
            calls_actual=transport.calls,
        )
        raise
    finally:
        await client.aclose()
        summary = summary_writer.write(
            collector_state=("STOPPED" if stats.stopped_reason else "COMPLETED")
        )
        progress.close()
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def main() -> int:
    return asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
