"""ROB-1283 — negative-class ("we looked and did NOT buy") recording health.

Why this module exists
----------------------
The rejected-buy cohort used to live in ``review.investment_report_items``
under ``decision_bucket='deferred_no_action'``. That path stopped being
called on 2026-06-15 and nothing noticed for 66 days, because nothing ever
*looked*. The gate-calibration diagnostic that eventually noticed had to
reconstruct the cohort from free text with a regex, and a single Korean verb
ending moved its headline figure by 73% -- a proxy biased by prose style.

So this module does two things and deliberately not a third:

* ``negative_class_warnings`` -- per-record advisory attached to a
  ``forecast_save`` response, so a session that records a rejection without a
  bucket (or without a report link) is told so at the moment it happens.
* ``assess_negative_class_health`` / ``load_negative_class_health`` -- the
  run-start regression guard. ``get_operating_briefing`` embeds the result,
  and because the operator compliance stamp (``operator-compliance/v1``)
  captures the whole briefing response verbatim, the guard reaches the
  session compliance stamp with no operator-repo change.
* It does NOT infer, impute, or backfill. A stretch of time with no records
  is reported as a gap with its real endpoints. Fake continuity is the one
  outcome worse than a visible hole, because it would be believed.

Read-only and side-effect free: no broker, order, watch, approval, or
order-intent path is reachable from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReport, InvestmentReportItem
from app.models.review import TradeForecast

# The one bucket that means "considered and rejected". Other buckets are
# recorded too, but this is the one the gate-calibration cohort is built from.
NEGATIVE_CLASS_BUCKET = "deferred_no_action"

# A week with zero recorded rejections, across every session in a market, is
# not a quiet week -- the KR/US/crypto prompts each run multiple times a day
# and every one of them evaluates candidates it does not buy. Chosen to be
# loose enough that a holiday stretch does not cry wolf.
STALL_THRESHOLD_DAYS = 7

# briefing market -> instrument_type stored on trade_forecasts.
_MARKET_TO_INSTRUMENT: dict[str, str] = {
    "kr": "equity_kr",
    "us": "equity_us",
    "crypto": "crypto",
}


@dataclass(frozen=True)
class NegativeClassHealth:
    """Assessment result. ``status`` is the single field worth alerting on."""

    status: str  # ok | stalled | never_recorded
    market: str
    last_recorded_at: str | None
    last_source: str | None  # forecast | report_item
    stale_days: int | None
    stall_threshold_days: int
    forecast_last_at: str | None
    report_item_last_at: str | None
    gap: dict[str, Any] | None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "market": self.market,
            "last_recorded_at": self.last_recorded_at,
            "last_source": self.last_source,
            "stale_days": self.stale_days,
            "stall_threshold_days": self.stall_threshold_days,
            "forecast_last_at": self.forecast_last_at,
            "report_item_last_at": self.report_item_last_at,
            "gap": self.gap,
            "notes": self.notes,
        }


def negative_class_warnings(forecast: dict[str, Any]) -> list[str]:
    """Advisory strings for one saved forecast. Never raises, never blocks.

    A warning here is not an error: the forecast is already stored. It exists
    so a session learns at call time that the row it just wrote will not be
    findable as a rejection later.
    """
    try:
        out: list[str] = []
        bucket = forecast.get("decision_bucket")
        if bucket is None:
            out.append(
                "decision_bucket is unset — this forecast will not appear in the "
                "rejected-candidate (negative class) cohort. If this call records "
                "a candidate you evaluated and did NOT buy, pass "
                f"decision_bucket='{NEGATIVE_CLASS_BUCKET}' (ROB-1283)."
            )
            return out
        if bucket == NEGATIVE_CLASS_BUCKET and not forecast.get("report_item_uuid"):
            out.append(
                "negative-class forecast recorded without report_item_uuid — the "
                "forecast alone is scorable, but the rationale/evidence half "
                "lives on the report item. If you created one via "
                "investment_report_create / investment_report_add_items, pass its "
                "item_uuid as report_item_uuid so the two halves join (ROB-1283)."
            )
        return out
    except Exception:  # noqa: BLE001 — advisory must never break forecast_save
        return []


def assess_negative_class_health(
    *,
    now: datetime,
    market: str,
    forecast_last_at: datetime | None,
    report_item_last_at: datetime | None,
    first_bucketed_forecast_at: datetime | None,
    stall_threshold_days: int = STALL_THRESHOLD_DAYS,
) -> NegativeClassHealth:
    """Pure assessment — no DB, no clock, no network. All inputs are given.

    ``forecast_last_at`` is the newest bucketed forecast, ``report_item_last_at``
    the newest ``deferred_no_action`` report item. Either surface counts as a
    record; the cohort is the union, because the two are the same claim written
    in two places.
    """
    notes: list[str] = []
    candidates: list[tuple[datetime, str]] = []
    if forecast_last_at is not None:
        candidates.append((forecast_last_at, "forecast"))
    if report_item_last_at is not None:
        candidates.append((report_item_last_at, "report_item"))

    gap = _describe_gap(
        report_item_last_at=report_item_last_at,
        first_bucketed_forecast_at=first_bucketed_forecast_at,
        now=now,
    )
    if gap is not None:
        notes.append(
            "A stretch of time carries no structured negative-class record. It is "
            "reported as a gap and is NOT backfilled — any cohort built across it "
            "is incomplete by exactly this window (ROB-1283)."
        )

    if not candidates:
        return NegativeClassHealth(
            status="never_recorded",
            market=market,
            last_recorded_at=None,
            last_source=None,
            stale_days=None,
            stall_threshold_days=stall_threshold_days,
            forecast_last_at=None,
            report_item_last_at=None,
            gap=gap,
            notes=[
                *notes,
                "No negative-class record has ever been observed for this market. "
                "Record rejected candidates with "
                f"forecast_save(decision_bucket='{NEGATIVE_CLASS_BUCKET}', ...).",
            ],
        )

    last_at, last_source = max(candidates, key=lambda c: c[0])
    stale_days = max(0, (now - last_at).days)
    stalled = stale_days >= stall_threshold_days
    if stalled:
        notes.append(
            f"No negative-class record in {stale_days} days (threshold "
            f"{stall_threshold_days}). Sessions evaluate candidates they do not "
            "buy every run, so silence here means the recording contract is not "
            "being honoured — not that nothing was rejected. Record with "
            f"forecast_save(decision_bucket='{NEGATIVE_CLASS_BUCKET}', ...)."
        )
    return NegativeClassHealth(
        status="stalled" if stalled else "ok",
        market=market,
        last_recorded_at=last_at.isoformat(),
        last_source=last_source,
        stale_days=stale_days,
        stall_threshold_days=stall_threshold_days,
        forecast_last_at=(
            forecast_last_at.isoformat() if forecast_last_at is not None else None
        ),
        report_item_last_at=(
            report_item_last_at.isoformat() if report_item_last_at is not None else None
        ),
        gap=gap,
        notes=notes,
    )


def _describe_gap(
    *,
    report_item_last_at: datetime | None,
    first_bucketed_forecast_at: datetime | None,
    now: datetime,
) -> dict[str, Any] | None:
    """Describe the hole between the old surface dying and the new one starting.

    Endpoints are derived from data, never hardcoded, so the description stays
    true as the gap closes. ``ends_at=None`` means the gap is still open.
    """
    if report_item_last_at is None:
        return None
    end = first_bucketed_forecast_at
    if end is not None and end <= report_item_last_at:
        return None  # the surfaces overlap; there is no hole
    boundary = end if end is not None else now
    days = max(0, (boundary - report_item_last_at).days)
    if days < STALL_THRESHOLD_DAYS:
        return None
    return {
        "starts_at": report_item_last_at.isoformat(),
        "ends_at": end.isoformat() if end is not None else None,
        "open": end is None,
        "days": days,
        "reason": (
            "report-item recording stopped and no bucketed forecast had taken over yet"
        ),
        "backfilled": False,
    }


async def load_negative_class_health(
    db: AsyncSession,
    *,
    market: str,
    now: datetime,
    stall_threshold_days: int = STALL_THRESHOLD_DAYS,
) -> NegativeClassHealth:
    """Read the three timestamps the assessment needs. Read-only."""
    instrument = _MARKET_TO_INSTRUMENT.get(market)

    forecast_filters = [TradeForecast.decision_bucket == NEGATIVE_CLASS_BUCKET]
    if instrument is not None:
        forecast_filters.append(TradeForecast.instrument_type == instrument)

    forecast_last_at = (
        await db.execute(
            select(func.max(TradeForecast.created_at)).where(*forecast_filters)
        )
    ).scalar_one_or_none()

    # ``first_bucketed`` anchors the far edge of the historical gap. It uses the
    # same filters so the gap is described in the same scope it is measured.
    first_bucketed = (
        await db.execute(
            select(func.min(TradeForecast.created_at)).where(*forecast_filters)
        )
    ).scalar_one_or_none()

    item_stmt = (
        select(func.max(InvestmentReportItem.created_at))
        .join(InvestmentReport, InvestmentReportItem.report_id == InvestmentReport.id)
        .where(InvestmentReportItem.decision_bucket == NEGATIVE_CLASS_BUCKET)
    )
    if market:
        item_stmt = item_stmt.where(InvestmentReport.market == market)
    report_item_last_at = (await db.execute(item_stmt)).scalar_one_or_none()

    return assess_negative_class_health(
        now=now,
        market=market,
        forecast_last_at=_aware(forecast_last_at, now),
        report_item_last_at=_aware(report_item_last_at, now),
        first_bucketed_forecast_at=_aware(first_bucketed, now),
        stall_threshold_days=stall_threshold_days,
    )


def _aware(value: datetime | None, reference: datetime) -> datetime | None:
    """Match tz-awareness to ``reference`` so subtraction never raises.

    The columns are ``TIMESTAMP WITH TIME ZONE``, but a SQLite-backed test
    harness hands back naive datetimes; normalising here keeps the pure
    assessment free of driver trivia.
    """
    if value is None:
        return None
    if (value.tzinfo is None) == (reference.tzinfo is None):
        return value
    if reference.tzinfo is None:
        return value.replace(tzinfo=None)
    return value.replace(tzinfo=reference.tzinfo)


def market_stall_window(now: datetime, days: int = STALL_THRESHOLD_DAYS) -> datetime:
    """Convenience for callers that want the cutoff instant itself."""
    return now - timedelta(days=days)


__all__ = [
    "NEGATIVE_CLASS_BUCKET",
    "STALL_THRESHOLD_DAYS",
    "NegativeClassHealth",
    "assess_negative_class_health",
    "load_negative_class_health",
    "market_stall_window",
    "negative_class_warnings",
]
