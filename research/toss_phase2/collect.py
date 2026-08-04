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
import os
import re
import time
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
PACING_SECONDS_DURING_MARKET = 0.5
PACING_SECONDS_AFTER_CLOSE = 0.3
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

    def __init__(self) -> None:
        self.calls = 0
        self._inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def now_kst() -> datetime:
    return datetime.now(KST)


def pacing_seconds(at: datetime | None = None) -> float:
    """Use the operator-approved 0.5 s during KR/NXT hours, else 0.3 s."""
    current = (at or now_kst()).astimezone(KST).time()
    if local_time(9, 0) <= current < local_time(20, 0):
        return PACING_SECONDS_DURING_MARKET
    return PACING_SECONDS_AFTER_CLOSE


class PacingRateLimiter:
    """Keep the process-wide Toss limiter and add the Phase 2 serial pacing."""

    def __init__(self, base: Any, chart_group: Any) -> None:
        self._base = base
        self._chart_group = chart_group
        self._last_chart_call = 0.0

    async def acquire(self, group: Any) -> None:
        await self._base.acquire(group)
        if group != self._chart_group:
            return
        interval = pacing_seconds()
        elapsed = time.monotonic() - self._last_chart_call
        if elapsed < interval:
            await asyncio.sleep(interval - elapsed)
        self._last_chart_call = time.monotonic()


@dataclass(frozen=True)
class SharedErrorBaseline:
    sequence: int


class SharedTossHealthMonitor:
    """Stop this collector if a new shared Toss error is published."""

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
            if _TOSS_ERROR_LINE.search(appended):
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
    return "UNKNOWN"


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
        "page_size": PAGE_SIZE,
        "pacing": {
            "during_09_00_to_20_00_kst_seconds": PACING_SECONDS_DURING_MARKET,
            "after_close_seconds": PACING_SECONDS_AFTER_CLOSE,
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
        return
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    (staging_dir / "STAGING_ONLY.md").write_text(
        "# STAGING ONLY — NOT A BACKTEST INPUT\n\n"
        "This is an append-only Toss combined KRX/NXT collection. It must not be "
        "loaded into a database or used by a backtest until a separately approved "
        "review and load step. `is_padding=true` is a provider placeholder, and "
        "`value` is synthetic `close * volume`.\n",
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
    stopped_reason: str | None = None


async def collect(
    *,
    client: Any,
    transport: CountingTransport,
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
) -> CollectionStats:
    checkpoint = Checkpoint(staging_dir / "state" / "checkpoint.json")

    for symbol in symbols:
        state = checkpoint.state_for(symbol)
        if state["done"]:
            stats.symbols_done += 1
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
                raise CollectionStopped(
                    f"toss_request_failed:{type(exc).__name__}:status={status}"
                ) from exc

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
                pacing_seconds=pacing_seconds(),
            )
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
    progress = ProgressLog(args.staging_dir / "events" / "progress.jsonl")
    transport = CountingTransport()
    shared_limiter = get_shared_rate_limiter()
    manager = TossOAuthTokenManager.from_settings(settings, rate_limiter=shared_limiter)
    client = TossReadClient(
        token_manager=CachedTokenOnlyProvider(manager),
        base_url=resolve_toss_base_url(
            getattr(settings, "toss_api_base_url", None), DEFAULT_TOSS_BASE_URL
        ),
        transport=transport,
        rate_limiter=PacingRateLimiter(shared_limiter, TossApiGroup.MARKET_DATA_CHART),
        retry_on_429=False,
        retry_auth_reissue=False,
    )
    monitor = CombinedTossHealthMonitor(
        SharedTossHealthMonitor(read_toss_api_error_signal),
        ProductionTossLogMonitor(args.production_log),
    )
    stats = CollectionStats(symbols_total=len(symbols))
    try:
        await monitor.start()
        # This preflight can only hit shared Redis.  It proves the first request
        # cannot start an OAuth issuance; no Toss HTTP is sent here.
        await CachedTokenOnlyProvider(manager).get_access_token()
        progress.write(
            "collection_started",
            scope="top-200 x 4.6y",
            call_budget_declared=args.call_budget,
            pacing_seconds=pacing_seconds(),
            latest_session_excluded=True,
            latest_session_definition=(
                "session_date_kst >= collection_start_kst_date is excluded"
            ),
            token_mode="shared_cached_token_reuse_only_no_issue_no_force_reissue",
            redis_url_source=redis_url_source,
            settings_placeholders=sorted(_SETTINGS_PLACEHOLDERS),
            production_logs=[str(path) for path in args.production_log],
        )
        stats = await collect(
            client=client,
            transport=transport,
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
        )
    except CollectionStopped as exc:
        stats.stopped_reason = str(exc)
        progress.write(
            "collection_stopped", reason=str(exc), calls_actual=transport.calls
        )
    finally:
        await client.aclose()
        summary = {
            **asdict(stats),
            "calls_actual": transport.calls,
            "call_budget_declared": args.call_budget,
            "token_issued_by_collector": False,
            "database_load_performed": False,
            "artifact_state": STAGING_CONTRACT,
        }
        (args.staging_dir / "events" / "latest_summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        progress.close()
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def main() -> int:
    return asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
