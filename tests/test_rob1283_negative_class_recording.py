# tests/test_rob1283_negative_class_recording.py
"""ROB-1283 — negative-class recording: vocabulary, links, and the stall guard.

The failure this guards against is not a crash. It is 66 days of silence that
everything downstream read as "nothing was rejected". So the assertions below
care most about the cases where a wrong answer would still *look* fine:

* a mistyped bucket must raise, not vanish into an empty cohort;
* an unlinkable report link must raise, not persist as a row that never joins;
* a stalled market must report ``stalled``, not ``ok`` with a stale timestamp;
* a gap must be reported as a gap, never smoothed into continuity.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.trade_journal.forecast_service import (
    ForecastValidationError,
    _validate_decision_bucket,
    normalize_report_link,
)
from app.services.trade_journal.negative_class import (
    NEGATIVE_CLASS_BUCKET,
    STALL_THRESHOLD_DAYS,
    assess_negative_class_health,
    negative_class_warnings,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 20, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# vocabulary — a typo must be loud                                            #
# --------------------------------------------------------------------------- #
def test_valid_bucket_passes_through() -> None:
    assert _validate_decision_bucket("deferred_no_action") == "deferred_no_action"
    assert _validate_decision_bucket("  risk_watch  ") == "risk_watch"


def test_none_and_blank_mean_not_classified() -> None:
    assert _validate_decision_bucket(None) is None
    assert _validate_decision_bucket("   ") is None


@pytest.mark.parametrize(
    "bad",
    [
        "defered_no_action",  # single-letter typo
        "deferred",  # truncated
        "DEFERRED_NO_ACTION",  # wrong case
        "no_action",  # plausible synonym
    ],
)
def test_unknown_bucket_raises_rather_than_silently_dropping(bad: str) -> None:
    """A typo'd bucket that stored as-is would be invisible to the cohort query.

    That is the exact shape of this issue's original failure, so it must fail
    at write time and name the accepted vocabulary.
    """
    with pytest.raises(ForecastValidationError) as exc:
        _validate_decision_bucket(bad)
    assert "decision_bucket" in str(exc.value)
    assert NEGATIVE_CLASS_BUCKET in str(exc.value)


# --------------------------------------------------------------------------- #
# links — a link that cannot join is worse than no link                       #
# --------------------------------------------------------------------------- #
def test_report_link_normalised_to_canonical_uuid() -> None:
    upper = "3F2504E0-4F89-11D3-9A0C-0305E82C3301"
    assert normalize_report_link(upper, "report_uuid") == upper.lower()


def test_blank_report_link_is_none_not_empty_string() -> None:
    assert normalize_report_link("  ", "report_uuid") is None
    assert normalize_report_link(None, "report_uuid") is None


@pytest.mark.parametrize("bad", ["not-a-uuid", "12345", "item_uuid=abc"])
def test_non_uuid_report_link_raises(bad: str) -> None:
    with pytest.raises(ForecastValidationError) as exc:
        normalize_report_link(bad, "report_item_uuid")
    assert "report_item_uuid" in str(exc.value)


# --------------------------------------------------------------------------- #
# per-record advisory                                                         #
# --------------------------------------------------------------------------- #
def test_missing_bucket_warns_about_cohort_invisibility() -> None:
    warnings = negative_class_warnings({"decision_bucket": None})
    assert len(warnings) == 1
    assert NEGATIVE_CLASS_BUCKET in warnings[0]


def test_negative_class_without_item_link_warns() -> None:
    warnings = negative_class_warnings(
        {"decision_bucket": NEGATIVE_CLASS_BUCKET, "report_item_uuid": None}
    )
    assert len(warnings) == 1
    assert "report_item_uuid" in warnings[0]


def test_fully_recorded_negative_class_is_silent() -> None:
    assert (
        negative_class_warnings(
            {
                "decision_bucket": NEGATIVE_CLASS_BUCKET,
                "report_item_uuid": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
            }
        )
        == []
    )


def test_positive_bucket_does_not_demand_a_link() -> None:
    assert (
        negative_class_warnings(
            {"decision_bucket": "new_buy_candidate", "report_item_uuid": None}
        )
        == []
    )


def test_warnings_never_raise_on_a_malformed_payload() -> None:
    """Advisory output must never be able to break a forecast that already saved."""
    assert negative_class_warnings({}) != []  # missing bucket -> warns
    assert negative_class_warnings(None) == []  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# the stall guard                                                             #
# --------------------------------------------------------------------------- #
def _assess(**kw):
    base = {
        "now": NOW,
        "market": "kr",
        "forecast_last_at": None,
        "report_item_last_at": None,
        "first_bucketed_forecast_at": None,
    }
    base.update(kw)
    return assess_negative_class_health(**base)


def test_recent_forecast_is_ok() -> None:
    health = _assess(forecast_last_at=datetime(2026, 8, 19, tzinfo=UTC))
    assert health.status == "ok"
    assert health.last_source == "forecast"
    assert health.stale_days == 1


def test_the_actual_rob1283_stall_reports_stalled_not_ok() -> None:
    """The measured production state: items died 06-15, nothing replaced them."""
    health = _assess(report_item_last_at=datetime(2026, 6, 15, tzinfo=UTC))
    assert health.status == "stalled"
    assert health.stale_days == 66
    assert health.last_source == "report_item"


def test_no_records_at_all_is_distinct_from_stalled() -> None:
    """ "Never recorded" and "stopped recording" need different operator responses."""
    health = _assess()
    assert health.status == "never_recorded"
    assert health.stale_days is None
    assert health.last_recorded_at is None


def test_either_surface_counts_and_the_newer_one_wins() -> None:
    health = _assess(
        report_item_last_at=datetime(2026, 6, 15, tzinfo=UTC),
        forecast_last_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    assert health.status == "ok"
    assert health.last_source == "forecast"


def test_threshold_boundary_is_inclusive() -> None:
    exactly = NOW.replace(day=NOW.day - STALL_THRESHOLD_DAYS)
    assert _assess(forecast_last_at=exactly).status == "stalled"
    just_inside = NOW.replace(day=NOW.day - STALL_THRESHOLD_DAYS + 1)
    assert _assess(forecast_last_at=just_inside).status == "ok"


# --------------------------------------------------------------------------- #
# gaps are reported, never smoothed                                           #
# --------------------------------------------------------------------------- #
def test_open_gap_is_marked_open_and_not_backfilled() -> None:
    health = _assess(report_item_last_at=datetime(2026, 6, 15, tzinfo=UTC))
    assert health.gap is not None
    assert health.gap["open"] is True
    assert health.gap["ends_at"] is None
    assert health.gap["backfilled"] is False
    assert health.gap["days"] == 66


def test_closed_gap_keeps_its_real_endpoints() -> None:
    """Once a bucketed forecast takes over, the hole is bounded — not erased."""
    health = _assess(
        report_item_last_at=datetime(2026, 6, 15, tzinfo=UTC),
        first_bucketed_forecast_at=datetime(2026, 8, 20, tzinfo=UTC),
        forecast_last_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    assert health.status == "ok"
    assert health.gap is not None
    assert health.gap["open"] is False
    assert health.gap["starts_at"].startswith("2026-06-15")
    assert health.gap["ends_at"].startswith("2026-08-20")
    assert health.gap["days"] == 66


def test_overlapping_surfaces_report_no_gap() -> None:
    health = _assess(
        report_item_last_at=datetime(2026, 8, 19, tzinfo=UTC),
        first_bucketed_forecast_at=datetime(2026, 7, 1, tzinfo=UTC),
        forecast_last_at=datetime(2026, 8, 19, tzinfo=UTC),
    )
    assert health.gap is None


def test_a_short_quiet_stretch_is_not_called_a_gap() -> None:
    health = _assess(
        report_item_last_at=datetime(2026, 8, 18, tzinfo=UTC),
        first_bucketed_forecast_at=datetime(2026, 8, 20, tzinfo=UTC),
        forecast_last_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    assert health.gap is None


def test_health_payload_is_json_serialisable_for_the_compliance_stamp() -> None:
    """The operator stamp embeds this verbatim, so it must survive json.dumps."""
    import json

    payload = _assess(report_item_last_at=datetime(2026, 6, 15, tzinfo=UTC)).to_dict()
    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped["status"] == "stalled"
    assert round_tripped["gap"]["backfilled"] is False


# --------------------------------------------------------------------------- #
# order path — a bad audit link must not cost the forecast                    #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_order_path_drops_a_bad_link_but_still_publishes_the_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-473 policy preserved: this link is audit metadata on the order path.

    ``save_forecast`` is strict so a session gets a loud error, but the
    automatic place-time publish must degrade to "no link", never to "no
    forecast" — the forecast is the scoring record.
    """
    from app.services import live_place_provenance as lpp

    captured: dict[str, object] = {}

    class _FakeForecast:
        forecast_id = "11111111-1111-1111-1111-111111111111"

    async def fake_save(db, **kwargs):
        captured.update(kwargs)
        return "created", _FakeForecast()

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            return None

    monkeypatch.setattr(lpp, "save_forecast", fake_save)
    monkeypatch.setattr(lpp, "AsyncSessionLocal", lambda: _FakeSession())

    result = await lpp.publish_place_time_forecast(
        correlation_id="corr-1",
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        target_price=130.0,
        min_hold_days=5,
        session_label="kr-open-trade",
        created_by="kr-open-trade",
        report_item_uuid="definitely-not-a-uuid",
    )

    assert result == _FakeForecast.forecast_id  # the forecast still published
    assert captured["report_item_uuid"] is None  # the unusable link was dropped


@pytest.mark.asyncio
async def test_order_path_keeps_a_valid_link(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import live_place_provenance as lpp

    captured: dict[str, object] = {}
    item_uuid = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"

    class _FakeForecast:
        forecast_id = "22222222-2222-2222-2222-222222222222"

    async def fake_save(db, **kwargs):
        captured.update(kwargs)
        return "created", _FakeForecast()

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            return None

    monkeypatch.setattr(lpp, "save_forecast", fake_save)
    monkeypatch.setattr(lpp, "AsyncSessionLocal", lambda: _FakeSession())

    await lpp.publish_place_time_forecast(
        correlation_id="corr-2",
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        target_price=130.0,
        min_hold_days=5,
        session_label="kr-open-trade",
        created_by="kr-open-trade",
        report_item_uuid=item_uuid.upper(),
    )

    assert captured["report_item_uuid"] == item_uuid
