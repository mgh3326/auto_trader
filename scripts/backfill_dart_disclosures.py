#!/usr/bin/env python3
"""Backfill KR DART disclosures for an inclusive date range (#1085).

Read-only against DART's public filing list (``list_date_ex`` scrape via
OpenDartReader — the same fetch the scheduled daily path uses). Writes go only
through ``app.services.market_events.ingestion.ingest_kr_disclosures_for_date``,
which upserts idempotently on ``source_event_id`` (= DART ``rcept_no``) and
records one ``market_event_ingestion_partitions`` row per day.

Default mode is **dry-run**: every date in the range is fetched and reported —
date, XKRX session status, raw row count, parseable event count, current
partition state, and the predicted outcome — with no DB writes. ``--commit``
performs the actual per-day ingestion and marks partitions succeeded/failed
exactly like the scheduled CLI: a confirmed XKRX trading session that still
returns zero filings is recorded ``failed`` (never ``succeeded``), so it stays
visible and retryable.

Desk usage (after merge, on an operator host with OPENDART_API_KEY set):

    # dry-run first — per-day counts, no DB writes
    uv run python -m scripts.backfill_dart_disclosures \
        --from-date 2026-01-01 --to-date 2026-07-21

    # then write
    uv run python -m scripts.backfill_dart_disclosures \
        --from-date 2026-01-01 --to-date 2026-07-21 --commit

    # resume where a previous run stopped (see ``next_from_date`` in the
    # JSON summary) and bound the session size
    uv run python -m scripts.backfill_dart_disclosures \
        --from-date 2026-01-01 --to-date 2026-07-21 \
        --resume-from 2026-01-19 --max-days 30

Every DART fetch is paced (``--pace-seconds``, default 1 s between calls)
and bounded (``--max-calls`` global cap, ``--max-days`` day cap per run).
Transient faults — connection reset/refused, timeouts, HTTP 429/5xx, and
DART over-limit status codes — are retried with bounded exponential
backoff plus jitter (``--retry-*``, 5 attempts / 5 s start by default).
A day that still fails is recorded as failed and the run continues, until
``--max-consecutive-failures`` (default 3) days fail in a row — then it
stops cleanly with a JSON summary, never a traceback.

No documented DART rate limit was found in this repo or in the vendored
OpenDartReader docs, so the pacing floor is a desk-chosen conservative
default, not a spec-derived value.

Exit codes: ``0`` = every processed day succeeded (commit) or is
predicted to succeed (dry-run), including runs truncated by ``--max-days``;
``2`` = at least one day failed or would fail, or the run stopped early
on the call budget / consecutive-failure rule; ``1`` = the CLI itself
crashed.
"""

from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import logging
import random
import re
import socket
import ssl
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import requests
import urllib3.exceptions
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cli import setup_logging_and_sentry
from app.core.db import AsyncSessionLocal
from app.models.market_events import MarketEventIngestionPartition
from app.monitoring.sentry import capture_exception
from app.services.market_events.ingestion import ingest_kr_disclosures_for_date
from app.services.market_events.normalizers import normalize_dart_disclosure_row
from app.services.market_events.session_calendar import trading_session_status
from scripts.ingest_market_events import iter_partition_dates

logger = logging.getLogger(__name__)

SOURCE = "dart"
CATEGORY = "disclosure"
MARKET = "kr"

DEFAULT_PACE_SECONDS = 1.0
DEFAULT_MAX_CALLS = 1000
DEFAULT_RETRY_MAX_ATTEMPTS = 5
DEFAULT_RETRY_BASE_SECONDS = 5.0
DEFAULT_RETRY_MAX_SECONDS = 60.0
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3

FetchRows = Callable[[date], Awaitable[list[dict[str, Any]]]]

# OpenDartReader raises ``ValueError({'status': status, 'message': message})``
# on its official-API paths (e.g. OpenDartReader ``dart_list.py`` /
# ``dart_finstate.py`` — every ``status != '000'`` response). ``'020'`` is
# DART's request-limit-exceeded status (요청 제한 초과); ``'800'`` (system
# maintenance) and ``'900'`` (undefined server error) are server-side and
# equally retryable. Everything else — ``'010'``/``'011'``/``'012'`` key and
# IP problems, ``'013'`` no data, ``'100'``/``'101'`` bad request — is
# treated as non-transient because retrying cannot fix it.
DART_TRANSIENT_STATUS_CODES = frozenset({"020", "800", "900"})

_TRANSIENT_EXCEPTION_TYPES: tuple[type[BaseException], ...] = (
    ConnectionError,  # builtin: reset / refused / aborted / broken pipe
    TimeoutError,  # builtin (also covers asyncio.TimeoutError, socket.timeout)
    socket.gaierror,  # DNS resolution hiccups
    ssl.SSLError,  # TLS-level resets seen under throttling (UNEXPECTED_EOF)
    http.client.HTTPException,  # RemoteDisconnected, IncompleteRead, BadStatusLine
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.RetryError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
    urllib3.exceptions.HTTPError,  # MaxRetryError / ReadTimeoutError / ProtocolError
)


class _CallBudgetExhausted(RuntimeError):
    """The run-level DART call cap was hit before another fetch could start."""


_SECRET_PARAM = re.compile(
    r"(?i)\b(crtfc_key|api_key|apikey|access_key|token|secret|password|key)\s*=\s*[^\s&\"']+"
)


def _scrub_secret_params(text: str) -> str:
    return _SECRET_PARAM.sub(lambda m: f"{m.group(1)}=***", text)


def _sanitize_error_text(exc: BaseException) -> str:
    """Scrub query-string secrets from exception text before emitting/logging.

    ``requests``/``urllib3`` transport errors embed the request URL in their
    message, and the DART corp-code download URL carries ``crtfc_key`` — the
    API key must never land in stdout lines, logs, or the JSON summary.
    """
    return _scrub_secret_params(str(exc))


def _transient_dart_error(exc: BaseException) -> str | None:
    """Return a short reason tag when ``exc`` is a retryable DART fault.

    Order matters: the OpenDartReader ``ValueError`` status envelope is
    checked first, then any HTTP ``response.status_code`` (429/5xx are
    retryable, other 4xx is not), then the network/transport exception
    families. HTML error pages that surface as parse errors
    (``DartResponseSchemaError``, ``AttributeError``) are deliberately NOT
    transient — they mean the scrape contract drifted, which a short
    backoff cannot repair.
    """
    if isinstance(exc, ValueError) and exc.args and isinstance(exc.args[0], dict):
        status = str(exc.args[0].get("status", ""))
        if status in DART_TRANSIENT_STATUS_CODES:
            return f"dart_status_{status}"
        return None

    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        if status_code == 429 or 500 <= status_code < 600:
            return f"http_{status_code}"
        return None

    if isinstance(exc, _TRANSIENT_EXCEPTION_TYPES):
        return "network_error"
    return None


class _DartCallBudget:
    """Mutable per-run counters shared by every per-day fetch."""

    def __init__(self) -> None:
        self.calls = 0
        self.retries = 0


def _make_paced_fetch(
    fetch_rows: FetchRows,
    *,
    budget: _DartCallBudget,
    max_calls: int,
    pace_seconds: float,
    retry_max_attempts: int,
    retry_base_seconds: float,
    retry_max_seconds: float,
    sleep: Callable[[float], Awaitable[None]],
    rng: Callable[[], float],
    emit: Callable[[str], None],
) -> FetchRows:
    """Wrap ``fetch_rows`` with pacing, a call budget, and bounded retry.

    Every DART call is preceded by a ``pace_seconds`` wait (except the very
    first call of the run). Transient failures retry up to
    ``retry_max_attempts`` total attempts with exponential backoff
    (``retry_base_seconds * 2**(attempt-1)``, capped at
    ``retry_max_seconds``) scaled by ``[0.5, 1.5)`` jitter.
    """

    async def _paced_fetch(target_date: date) -> list[dict[str, Any]]:
        attempt = 0
        while True:
            if budget.calls >= max_calls:
                raise _CallBudgetExhausted(
                    f"dart call budget exhausted "
                    f"({budget.calls}/{max_calls} calls this run)"
                )
            if budget.calls > 0:
                await sleep(pace_seconds)
            attempt += 1
            budget.calls += 1
            try:
                return await fetch_rows(target_date)
            except Exception as exc:
                tag = _transient_dart_error(exc)
                if tag is None or attempt >= retry_max_attempts:
                    raise
                budget.retries += 1
                delay = min(
                    retry_base_seconds * (2 ** (attempt - 1)),
                    retry_max_seconds,
                ) * (0.5 + rng())
                emit(
                    f"{target_date.isoformat()} fetch_retry attempt={attempt} "
                    f"reason={tag} error={type(exc).__name__}: "
                    f"{_sanitize_error_text(exc)} sleep={delay:.1f}s"
                )
                await sleep(delay)

    return _paced_fetch


async def _default_fetch(target_date: date) -> list[dict[str, Any]]:
    from app.services.market_events.dart_helpers import fetch_dart_filings_for_date

    # Bypass the OpenDartReader docs_cache: backfill retries must hit the
    # network each attempt, and a cached error page would poison reruns.
    return await fetch_dart_filings_for_date(target_date, use_cache=False)


def _parseable_count(rows: list[dict[str, Any]]) -> int:
    """Count rows the DART normalizer would accept (no DB writes)."""
    parseable = 0
    for row in rows:
        try:
            normalize_dart_disclosure_row(row)
        except ValueError:
            continue
        parseable += 1
    return parseable


def _predicted_action(session_status: str, parseable: int) -> str:
    if parseable > 0:
        return "ingest"
    if session_status == "open":
        return "fail_zero_filings_on_trading_day"
    if session_status == "closed":
        return "ok_zero_on_closed_day"
    return "fail_calendar_unknown"


async def _load_partition_states(
    db: AsyncSession,
    from_date: date,
    to_date: date,
) -> dict[date, str]:
    stmt = select(
        MarketEventIngestionPartition.partition_date,
        MarketEventIngestionPartition.status,
    ).where(
        MarketEventIngestionPartition.source == SOURCE,
        MarketEventIngestionPartition.category == CATEGORY,
        MarketEventIngestionPartition.market == MARKET,
        MarketEventIngestionPartition.partition_date >= from_date,
        MarketEventIngestionPartition.partition_date <= to_date,
    )
    rows = (await db.execute(stmt)).all()
    return dict(rows)


async def run_backfill(
    *,
    db: AsyncSession,
    from_date: date,
    to_date: date,
    commit: bool = False,
    fetch_rows: FetchRows | None = None,
    emit: Callable[[str], None] = print,
    resume_from: date | None = None,
    max_days: int | None = None,
    pace_seconds: float = DEFAULT_PACE_SECONDS,
    max_calls: int = DEFAULT_MAX_CALLS,
    retry_max_attempts: int = DEFAULT_RETRY_MAX_ATTEMPTS,
    retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
    retry_max_seconds: float = DEFAULT_RETRY_MAX_SECONDS,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    rng: Callable[[], float] | None = None,
) -> int:
    """Backfill DART disclosures day by day over [from_date, to_date].

    ``commit=False`` (default) is a pure dry-run: it fetches and classifies
    every day but never writes. ``commit=True`` routes each day through
    ``ingest_kr_disclosures_for_date`` and commits per day, so every partition
    row reflects a complete attempt and a crash mid-range leaves the tail
    untouched rather than half-marked.

    ``fetch_rows`` is an injection point for tests; the production default is
    ``dart_helpers.fetch_dart_filings_for_date`` with ``use_cache=False``.

    Pacing/resilience (#1097): the wrapped fetch paces every DART call by
    ``pace_seconds``, retries transient faults with bounded jittered
    exponential backoff, and enforces ``max_calls`` for the whole run.
    ``resume_from`` skips days before it; ``max_days`` bounds one run. A
    failed day is recorded and the run continues until
    ``max_consecutive_failures`` days fail in a row — the final JSON
    summary's ``stop_reason`` and ``next_from_date`` let the desk resume
    exactly where it stopped. ``sleep``/``rng`` are injection points for
    tests (mock clock / deterministic jitter).
    """
    if from_date > to_date:
        raise ValueError("from_date must be <= to_date")
    if pace_seconds < 0:
        raise ValueError("pace_seconds must be >= 0")
    if max_calls < 1:
        raise ValueError("max_calls must be >= 1")
    if retry_max_attempts < 1:
        raise ValueError("retry_max_attempts must be >= 1")
    if retry_base_seconds < 0 or retry_max_seconds <= 0:
        raise ValueError("retry backoff seconds must be positive")
    if max_consecutive_failures < 1:
        raise ValueError("max_consecutive_failures must be >= 1")
    if max_days is not None and max_days < 1:
        raise ValueError("max_days must be >= 1")

    effective_from = from_date
    if resume_from is not None:
        effective_from = max(from_date, resume_from)
        if effective_from > to_date:
            raise ValueError("resume_from is after to_date")

    if fetch_rows is None:
        fetch_rows = _default_fetch
    if sleep is None:
        sleep = asyncio.sleep
    if rng is None:
        rng = random.random

    budget = _DartCallBudget()
    paced_fetch = _make_paced_fetch(
        fetch_rows,
        budget=budget,
        max_calls=max_calls,
        pace_seconds=pace_seconds,
        retry_max_attempts=retry_max_attempts,
        retry_base_seconds=retry_base_seconds,
        retry_max_seconds=retry_max_seconds,
        sleep=sleep,
        rng=rng,
        emit=emit,
    )

    planned_dates = list(iter_partition_dates(effective_from, to_date))
    truncated_next: date | None = None
    if max_days is not None and len(planned_dates) > max_days:
        truncated_next = planned_dates[max_days]
        planned_dates = planned_dates[:max_days]

    partition_states = await _load_partition_states(db, from_date, to_date)

    succeeded = 0
    failed = 0
    consecutive_failures = 0
    failed_dates: list[str] = []
    processed_through: date | None = None
    stop_reason = "completed"
    next_from_date: date | None = None

    for index, d in enumerate(planned_dates):
        if budget.calls >= max_calls:
            stop_reason = "call_budget_exhausted"
            next_from_date = d
            emit(f"{d.isoformat()} skipped reason=call_budget_exhausted")
            break

        day_calls_start = budget.calls

        if not commit:
            session_status = trading_session_status(MARKET, d)
            partition_state = partition_states.get(d, "none")
            try:
                rows = await paced_fetch(d)
            except Exception as exc:
                emit(
                    f"{d.isoformat()} session={session_status} "
                    f"fetch_error={type(exc).__name__}: "
                    f"{_sanitize_error_text(exc)} "
                    f"calls={budget.calls - day_calls_start} outcome=failed"
                )
                failed += 1
                failed_dates.append(d.isoformat())
                consecutive_failures += 1
            else:
                parseable = _parseable_count(rows)
                action = _predicted_action(session_status, parseable)
                emit(
                    f"{d.isoformat()} session={session_status} "
                    f"rows={len(rows)} parseable={parseable} "
                    f"partition={partition_state} action={action} "
                    f"calls={budget.calls - day_calls_start}"
                )
                if action.startswith("fail"):
                    failed += 1
                    failed_dates.append(d.isoformat())
                    consecutive_failures += 1
                else:
                    succeeded += 1
                    consecutive_failures = 0
        else:
            try:
                result = await ingest_kr_disclosures_for_date(
                    db, d, fetch_rows=paced_fetch
                )
                await db.commit()
            except Exception as exc:
                await db.rollback()
                emit(
                    f"{d.isoformat()} status=failed "
                    f"calls={budget.calls - day_calls_start} "
                    f"error={type(exc).__name__}: {_sanitize_error_text(exc)}"
                )
                result = None
                day_failed_error: str | None = _sanitize_error_text(exc)
            else:
                emit(
                    f"{d.isoformat()} status={result.status} "
                    f"events={result.event_count} "
                    f"calls={budget.calls - day_calls_start}"
                    + (
                        f" error={_scrub_secret_params(result.error)}"
                        if result.error
                        else ""
                    )
                )
                day_failed_error = (
                    _scrub_secret_params(result.error) if result.error else None
                )

            if result is not None and result.status == "succeeded":
                succeeded += 1
                consecutive_failures = 0
            else:
                failed += 1
                failed_dates.append(d.isoformat())
                consecutive_failures += 1
                logger.error(
                    "dart backfill failed for %s on %s: %s",
                    f"{SOURCE}/{CATEGORY}/{MARKET}",
                    d,
                    day_failed_error,
                )

        processed_through = d

        if consecutive_failures >= max_consecutive_failures:
            stop_reason = "consecutive_failures"
            if index + 1 < len(planned_dates):
                next_from_date = planned_dates[index + 1]
            else:
                next_from_date = truncated_next
            emit(
                f"stop: {consecutive_failures} consecutive failed days "
                f"(limit {max_consecutive_failures}); "
                f"next_from_date="
                f"{next_from_date.isoformat() if next_from_date else 'none'}"
            )
            break

    else:
        if truncated_next is not None:
            stop_reason = "max_days"
            next_from_date = truncated_next

    days_remaining = (
        (to_date - next_from_date).days + 1 if next_from_date is not None else 0
    )
    summary = {
        "source": SOURCE,
        "category": CATEGORY,
        "market": MARKET,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "resume_from": resume_from.isoformat() if resume_from else None,
        "max_days": max_days,
        "dry_run": not commit,
        "succeeded": succeeded,
        "failed": failed,
        "days_processed": succeeded + failed,
        "days_remaining": days_remaining,
        "consecutive_failures": consecutive_failures,
        "stop_reason": stop_reason,
        "processed_through": (
            processed_through.isoformat() if processed_through else None
        ),
        "next_from_date": next_from_date.isoformat() if next_from_date else None,
        "failed_dates": failed_dates,
        "dart_calls": budget.calls,
        "dart_retries": budget.retries,
        "pace_seconds": pace_seconds,
        "max_calls": max_calls,
        "retry_max_attempts": retry_max_attempts,
    }
    emit(json.dumps(summary))
    logger.info("dart backfill complete: %s", summary)
    if failed == 0 and stop_reason in ("completed", "max_days"):
        return 0
    return 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill KR DART disclosures over an inclusive date range. "
            "Default is --dry-run (per-day counts, no DB writes); --commit "
            "writes through the standard ingestion path."
        )
    )
    parser.add_argument(
        "--from-date",
        type=date.fromisoformat,
        dest="from_date",
        required=True,
        help="ISO start date (inclusive).",
    )
    parser.add_argument(
        "--to-date",
        type=date.fromisoformat,
        dest="to_date",
        required=True,
        help="ISO end date (inclusive).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_false",
        dest="commit",
        help="Fetch and report per-day counts without writing (default).",
    )
    mode.add_argument(
        "--commit",
        action="store_true",
        dest="commit",
        help="Write events and partition rows through the ingestion path.",
    )
    parser.set_defaults(commit=False)
    parser.add_argument(
        "--resume-from",
        type=date.fromisoformat,
        default=None,
        help=(
            "Skip days before this date (inclusive) within the range — pass "
            "the previous run's next_from_date. Dates before --from-date clamp "
            "to it; a date after --to-date is an error."
        ),
    )
    parser.add_argument(
        "--max-days",
        type=int,
        default=None,
        help=(
            "Process at most N days this run; remaining days are reported "
            "via next_from_date in the JSON summary."
        ),
    )
    parser.add_argument(
        "--pace-seconds",
        type=float,
        default=DEFAULT_PACE_SECONDS,
        help=(
            "Minimum delay between DART fetch calls, including retries "
            f"(default {DEFAULT_PACE_SECONDS}; keep the default so the backfill "
            "does not starve or throttle the regular collector)."
        ),
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=DEFAULT_MAX_CALLS,
        help=(
            "Global cap on DART fetch calls for the whole run "
            f"(default {DEFAULT_MAX_CALLS})."
        ),
    )
    parser.add_argument(
        "--retry-max-attempts",
        type=int,
        default=DEFAULT_RETRY_MAX_ATTEMPTS,
        help=(
            "Max fetch attempts per day including the first "
            f"(default {DEFAULT_RETRY_MAX_ATTEMPTS})."
        ),
    )
    parser.add_argument(
        "--retry-base-seconds",
        type=float,
        default=DEFAULT_RETRY_BASE_SECONDS,
        help=(
            "Initial backoff delay, doubled per retry "
            f"(default {DEFAULT_RETRY_BASE_SECONDS}s)."
        ),
    )
    parser.add_argument(
        "--retry-max-seconds",
        type=float,
        default=DEFAULT_RETRY_MAX_SECONDS,
        help=f"Cap on a single backoff delay (default {DEFAULT_RETRY_MAX_SECONDS}s).",
    )
    parser.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=DEFAULT_MAX_CONSECUTIVE_FAILURES,
        help=(
            "Stop cleanly after this many consecutive failed days "
            f"(default {DEFAULT_MAX_CONSECUTIVE_FAILURES})."
        ),
    )
    ns = parser.parse_args(argv)
    if ns.from_date > ns.to_date:
        parser.error("--from-date must be less than or equal to --to-date")
    if ns.resume_from is not None and ns.resume_from > ns.to_date:
        parser.error("--resume-from is after --to-date")
    if ns.max_days is not None and ns.max_days < 1:
        parser.error("--max-days must be >= 1")
    if ns.pace_seconds < 0:
        parser.error("--pace-seconds must be >= 0")
    if ns.max_calls < 1:
        parser.error("--max-calls must be >= 1")
    if ns.retry_max_attempts < 1:
        parser.error("--retry-max-attempts must be >= 1")
    if ns.retry_base_seconds < 0 or ns.retry_max_seconds <= 0:
        parser.error("--retry-*-seconds must be positive")
    if ns.max_consecutive_failures < 1:
        parser.error("--max-consecutive-failures must be >= 1")
    return ns


async def main(argv: list[str] | None = None) -> int:
    setup_logging_and_sentry(service_name="dart-backfill")
    ns = parse_args(argv)

    try:
        async with AsyncSessionLocal() as db:
            return await run_backfill(
                db=db,
                from_date=ns.from_date,
                to_date=ns.to_date,
                commit=ns.commit,
                resume_from=ns.resume_from,
                max_days=ns.max_days,
                pace_seconds=ns.pace_seconds,
                max_calls=ns.max_calls,
                retry_max_attempts=ns.retry_max_attempts,
                retry_base_seconds=ns.retry_base_seconds,
                retry_max_seconds=ns.retry_max_seconds,
                max_consecutive_failures=ns.max_consecutive_failures,
            )
    except Exception as exc:
        capture_exception(exc, process="backfill_dart_disclosures")
        logger.error("backfill_dart_disclosures crashed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
