"""TaskIQ wrappers for invest screener snapshot activation.

Includes both the manual entry point (``build_invest_screener_snapshots``,
dry-run by default) and the ROB-281 scheduled wrappers for KR/US.

ROB-281 recurring activation is double-gated for safety:

* ``invest_screener_schedule_enabled`` registers the cron schedule.
* ``invest_screener_snapshots_commit_enabled`` allows DB writes.

When the schedule flag is False the cron entries are not registered (manual
``taskiq kick`` still works). When the schedule flag is True but the commit
flag is False, scheduled tasks run on cron and produce dry-run output without
touching the database — this is the "dry-run-on-schedule" rollout state.

Locked decisions (see ``docs/plans/2026-05-20-ROB-281-...-plan.md``):

* D1 — KR pre-market repair runs at 07:40 KST (20-minute buffer to NXT
  pre-market 08:00). This slot does NOT prepare same-day data; it repairs
  the prior day's 20:20 KST NXT-final if that run failed or lagged.
* D2 — KR holiday gate via ``exchange_calendars`` XKRX; US via XNYS. US
  cron runs in ``America/New_York`` to avoid DST drift.
* D3 — Failure / suspicious-distribution alerts route to
  ``discord_webhook_alerts`` (Stage 6 wiring in this PR).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Literal
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.jobs.invest_screener_snapshots import (
    SnapshotBuildRequest,
    run_snapshot_build,
    run_snapshot_build_guarded,
)
from app.jobs.support_proximity_snapshots import (
    SupportProximityBuildRequest,
    run_support_proximity_build,
)
from app.services.invest_screener_snapshots.alerts import (
    send_screener_refresh_alert,
)
from app.services.invest_screener_snapshots.guards import (
    InsufficientRowsError,
    SuspiciousDistributionError,
)

logger = logging.getLogger(__name__)

_KST_LABEL = "Asia/Seoul"
_ET_LABEL = "America/New_York"

KRSlot = Literal["pre_market_repair", "krx_preliminary", "nxt_final"]
USSlot = Literal["post_close"]


# ---------------------------------------------------------------------------
# Schedule registration helpers
# ---------------------------------------------------------------------------


def _kr_schedule(cron: str) -> list[dict[str, str]]:
    if not settings.invest_screener_schedule_enabled:
        return []
    return [{"cron": cron, "cron_offset": _KST_LABEL}]


def _us_schedule(cron: str) -> list[dict[str, str]]:
    if not settings.invest_screener_schedule_enabled:
        return []
    return [{"cron": cron, "cron_offset": _ET_LABEL}]


def _scheduled_kr_pre_market_repair_labels() -> list[dict[str, str]]:
    # 07:40 KST Mon–Fri. XKRX holiday filter applied at task entry.
    return _kr_schedule("40 7 * * 1-5")


def _scheduled_kr_krx_preliminary_labels() -> list[dict[str, str]]:
    # 16:20 KST Mon–Fri.
    return _kr_schedule("20 16 * * 1-5")


def _scheduled_kr_nxt_final_labels() -> list[dict[str, str]]:
    # 20:20 KST Mon–Fri.
    return _kr_schedule("20 20 * * 1-5")


def _scheduled_us_post_close_labels() -> list[dict[str, str]]:
    # 17:20 America/New_York Mon–Fri. XNYS holiday filter applied at task entry.
    return _us_schedule("20 17 * * 1-5")


# ---------------------------------------------------------------------------
# Session-day holiday gate (XKRX / XNYS)
# ---------------------------------------------------------------------------


def is_market_session_today(
    market: Literal["kr", "us"], *, now: dt.datetime | None = None
) -> bool:
    """Return whether today (in the market's tz) is a trading session.

    Uses :mod:`exchange_calendars` XKRX for KR and XNYS for US (ROB-281 D2).
    Pulling the calendar is the only expensive op here; imports are lazy to
    keep startup snappy for non-scheduled importers.
    """
    import exchange_calendars as xcals
    import pandas as pd

    if market == "kr":
        cal = xcals.get_calendar("XKRX")
        tz = ZoneInfo("Asia/Seoul")
    else:
        cal = xcals.get_calendar("XNYS")
        tz = ZoneInfo("America/New_York")
    moment = now or dt.datetime.now(dt.UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    today_local = moment.astimezone(tz).date()
    return bool(cal.is_session(pd.Timestamp(today_local)))


# ---------------------------------------------------------------------------
# Scheduled task body — shared across KR/US slots
# ---------------------------------------------------------------------------


async def _run_scheduled_build(
    *,
    market: Literal["kr", "us"],
    slot: str,
) -> dict[str, Any]:
    """Common body for scheduled refresh tasks.

    * Skips the run on XKRX/XNYS holidays (returns a structured skipped result;
      no Discord alert — holidays are expected).
    * Honors ``invest_screener_snapshots_commit_enabled`` for commit gating.
    * US uses ``common_stocks_only=True`` to match the ROB-204 production path.
    * Stage 5 (commit-time guards) and Stage 6 (Discord alerts) hook in here
      when those stages land in this PR.

    Returns a JSON-serializable result dict suitable for TaskIQ result backend.
    """
    if not is_market_session_today(market):
        logger.info(
            "scheduled invest screener slot skipped (non-session day): "
            "market=%s slot=%s",
            market,
            slot,
        )
        return {
            "market": market,
            "slot": slot,
            "skipped": "non_session_day",
            "committed": False,
        }

    commit = settings.invest_screener_snapshots_commit_enabled
    common_stocks_only = market == "us"

    request = SnapshotBuildRequest(
        market=market,
        all_symbols=True,
        commit=commit,
        batch_size=200,
        concurrency=4,
        common_stocks_only=common_stocks_only,
    )

    logger.info(
        "scheduled invest screener build starting: market=%s slot=%s commit=%s",
        market,
        slot,
        commit,
    )
    # Stage 5/6: dry-run → guards → commit, with Discord alerts on failure.
    # Holiday gate above already filtered non-session days, so any exception
    # reaching here is a real data-quality / system signal. Alerts go to
    # discord_webhook_alerts (D3); Hermes is intentionally NOT used for ops.
    try:
        result = await run_snapshot_build_guarded(request)
    except (SuspiciousDistributionError, InsufficientRowsError) as exc:
        await send_screener_refresh_alert(
            slot=slot,
            market=market,
            exception=exc,
            distribution=exc.distribution,
            commit_status="skipped",
        )
        raise
    except Exception as exc:
        await send_screener_refresh_alert(
            slot=slot,
            market=market,
            exception=exc,
            distribution=None,
            commit_status="failed",
        )
        raise

    logger.info(
        "scheduled invest screener build finished: market=%s slot=%s "
        "snapshots_built=%d committed=%s distribution=%s",
        market,
        slot,
        result.snapshots_built,
        result.committed,
        result.snapshot_date_distribution,
    )

    return {
        "market": market,
        "slot": slot,
        "committed": result.committed,
        "symbolsResolved": result.symbols_resolved,
        "snapshotsBuilt": result.snapshots_built,
        "skipped": result.skipped,
        "snapshotDateDistribution": result.snapshot_date_distribution,
        "startedAt": result.started_at.isoformat(),
        "finishedAt": result.finished_at.isoformat(),
        "warnings": list(result.warnings),
    }


# ---------------------------------------------------------------------------
# Existing manual entry point (kept dry-run-by-default, unchanged behavior)
# ---------------------------------------------------------------------------


@broker.task(task_name="build_invest_screener_snapshots")
async def build_invest_screener_snapshots(
    market: Literal["kr", "us"],
    symbols: list[str] | None = None,
    limit: int | None = 20,
    all_symbols: bool = False,
    batch_size: int = 200,
    concurrency: int = 4,
    common_stocks_only: bool = False,
    commit: bool = False,
) -> dict[str, Any]:
    """Build invest_screener_snapshots rows, dry-run by default.

    commit=False returns counts/sample payload metadata without database writes.
    commit=True persists via the snapshot repository and should only be used after
    an operator/reviewer approval flow captured dry-run evidence.
    """
    request = SnapshotBuildRequest(
        market=market,
        symbols=tuple(symbols or ()),
        limit=limit,
        all_symbols=all_symbols,
        batch_size=batch_size,
        concurrency=concurrency,
        commit=commit,
        common_stocks_only=common_stocks_only,
    )
    result = await run_snapshot_build(request)
    return {
        "market": result.market,
        "symbolsResolved": result.symbols_resolved,
        "snapshotsBuilt": result.snapshots_built,
        "skipped": result.skipped,
        "committed": result.committed,
        "batches": result.batches,
        "startedAt": result.started_at.isoformat(),
        "finishedAt": result.finished_at.isoformat(),
        "snapshotDateDistribution": result.snapshot_date_distribution,
        "samples": [
            {
                "market": sample.market,
                "symbol": sample.symbol,
                "snapshotDate": sample.snapshot_date.isoformat(),
                "latestClose": sample.latest_close,
                "consecutiveUpDays": sample.consecutive_up_days,
                "weekChangeRate": sample.week_change_rate,
            }
            for sample in result.samples
        ],
        "warnings": list(result.warnings),
    }


@broker.task(task_name="build_support_proximity_snapshots")
async def build_support_proximity_snapshots(
    candidate_pool_limit: int = 30,
    concurrency: int = 4,
    min_market_cap: float = 300_000_000_000.0,
    min_turnover: float = 1_000_000_000.0,
    commit: bool = False,
) -> dict[str, Any]:
    """Manual, scheduleless support snapshot lever; dry-run by default."""

    from decimal import Decimal

    result = await run_support_proximity_build(
        SupportProximityBuildRequest(
            market="kr",
            candidate_pool_limit=candidate_pool_limit,
            concurrency=concurrency,
            min_market_cap=Decimal(str(min_market_cap)),
            min_turnover=Decimal(str(min_turnover)),
            commit=commit,
        )
    )
    return {
        "market": result.market,
        "sourcePartitionDate": (
            result.source_partition_date.isoformat()
            if result.source_partition_date is not None
            else None
        ),
        "candidatesResolved": result.candidates_resolved,
        "snapshotsBuilt": result.snapshots_built,
        "supportsBuilt": result.supports_built,
        "skipped": result.skipped,
        "committed": result.committed,
        "startedAt": result.started_at.isoformat(),
        "finishedAt": result.finished_at.isoformat(),
        "samples": [
            {
                "symbol": sample.symbol,
                "snapshotDate": sample.snapshot_date.isoformat(),
                "latestClose": sample.latest_close,
                "supportPrice": sample.support_price,
                "supportKind": sample.support_kind,
                "supportStrength": sample.support_strength,
                "distToSupportPct": sample.dist_to_support_pct,
                "marketCap": sample.market_cap,
                "supportComputedAt": sample.support_computed_at.isoformat(),
            }
            for sample in result.samples
        ],
        "warnings": list(result.warnings),
    }


# ---------------------------------------------------------------------------
# ROB-281 — KR scheduled wrappers
# ---------------------------------------------------------------------------


@broker.task(
    task_name="invest_screener_snapshots.kr_pre_market_repair",
    schedule=_scheduled_kr_pre_market_repair_labels(),
)
async def scheduled_kr_pre_market_repair() -> dict[str, Any]:
    """KR pre-market repair at 07:40 KST on KR trading days.

    Per ROB-281 D1, this slot does NOT prepare same-day data — same-day data
    arrives at 16:20 (KRX preliminary) and 20:20 (NXT final). The 07:40 KST
    run exists solely to repair the prior trading day's 20:20 NXT-final if
    that run failed or upstream data was delayed overnight. The 20-minute
    buffer to NXT pre-market open (08:00 KST) allows transient-failure retry.
    """
    return await _run_scheduled_build(market="kr", slot="pre_market_repair")


@broker.task(
    task_name="invest_screener_snapshots.kr_krx_preliminary",
    schedule=_scheduled_kr_krx_preliminary_labels(),
)
async def scheduled_kr_krx_preliminary() -> dict[str, Any]:
    """KR KRX regular-session preliminary refresh at 16:20 KST.

    Surfaced in the UI as ``KRX preliminary`` (ROB-281 Stage 7 wiring) to
    distinguish it from the later 20:20 KST NXT-final result.
    """
    return await _run_scheduled_build(market="kr", slot="krx_preliminary")


@broker.task(
    task_name="invest_screener_snapshots.kr_nxt_final",
    schedule=_scheduled_kr_nxt_final_labels(),
)
async def scheduled_kr_nxt_final() -> dict[str, Any]:
    """KR NXT after-market final refresh at 20:20 KST.

    Surfaced in the UI as ``NXT final`` and is the authoritative end-of-day
    KR snapshot under the post-NXT trading-hours regime.
    """
    return await _run_scheduled_build(market="kr", slot="nxt_final")


# ---------------------------------------------------------------------------
# ROB-281 — US scheduled wrapper
# ---------------------------------------------------------------------------


@broker.task(
    task_name="invest_screener_snapshots.us_post_close",
    schedule=_scheduled_us_post_close_labels(),
)
async def scheduled_us_post_close() -> dict[str, Any]:
    """US post-close refresh at 17:20 America/New_York on US trading days.

    Per ROB-281 D2, cron is registered in ``America/New_York`` (not KST) to
    avoid DST drift. Half-days (e.g., Black Friday 13:00 ET close) require
    no special case — 17:20 ET is post-close on any session date. Holiday
    skip uses XNYS via :func:`is_market_session_today`.
    """
    return await _run_scheduled_build(market="us", slot="post_close")
