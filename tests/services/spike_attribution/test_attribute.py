from __future__ import annotations

import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.services.spike_attribution.attribute import (
    UNATTRIBUTED,
    UNATTRIBUTED_PHRASE,
    AttributionError,
    build_attribution,
    record_summary,
    rule_eligibility,
    scored_class,
)
from app.services.spike_attribution.contract import (
    ELIGIBILITY_AFTER_MOVE,
    ELIGIBILITY_BEFORE_WINDOW,
    ELIGIBILITY_ELIGIBLE,
    ELIGIBILITY_TIMESTAMP_UNKNOWN,
    EvidenceItem,
    MaterialAvailability,
    SpikeEvent,
    SpikeMaterials,
)

KST = ZoneInfo("Asia/Seoul")
WINDOW_START = dt.datetime(2026, 8, 19, 15, 30, tzinfo=KST)
WINDOW_END = dt.datetime(2026, 8, 20, 15, 30, tzinfo=KST)


def make_event() -> SpikeEvent:
    return SpikeEvent(
        market="kr",
        symbol="035420",
        session_date=dt.date(2026, 8, 20),
        direction="up",
        prev_close=Decimal("208000"),
        close=Decimal("219500"),
        high=Decimal("223500"),
        low=Decimal("209500"),
        close_to_close_pct=Decimal("5.5288"),
        intraday_extreme_pct=Decimal("7.4519"),
        triggered_bases=("close_to_close", "intraday_extreme"),
        window_start_exclusive=WINDOW_START,
        window_end_inclusive=WINDOW_END,
    )


def item(
    *,
    attribution_type: str = "news",
    published_at: dt.datetime | None,
    eligibility: str,
    title: str = "t",
    judgment: str = "unjudged",
) -> EvidenceItem:
    return EvidenceItem(
        attribution_type=attribution_type,
        source="feed",
        title=title,
        url="https://example.test/a",
        published_at=published_at,
        published_at_precision="exact",
        published_at_source="test",
        eligibility=eligibility,
        judgment=judgment,
    )


def materials(*items: EvidenceItem) -> SpikeMaterials:
    return SpikeMaterials(
        evidence=tuple(items),
        availability=(
            MaterialAvailability(material="news", available=True),
            MaterialAvailability(
                material="flow", available=False, reason="unavailable_t_plus_1"
            ),
        ),
    )


# --- window ruling -------------------------------------------------------


@pytest.mark.parametrize(
    ("published", "expected"),
    [
        (dt.datetime(2026, 8, 20, 9, 31, tzinfo=KST), ELIGIBILITY_ELIGIBLE),
        (dt.datetime(2026, 8, 19, 16, 52, tzinfo=KST), ELIGIBILITY_ELIGIBLE),
        (dt.datetime(2026, 8, 20, 15, 30, tzinfo=KST), ELIGIBILITY_ELIGIBLE),
        (dt.datetime(2026, 8, 20, 17, 38, tzinfo=KST), ELIGIBILITY_AFTER_MOVE),
        (dt.datetime(2026, 8, 19, 15, 30, tzinfo=KST), ELIGIBILITY_BEFORE_WINDOW),
        (dt.datetime(2026, 8, 18, 10, 0, tzinfo=KST), ELIGIBILITY_BEFORE_WINDOW),
        (None, ELIGIBILITY_TIMESTAMP_UNKNOWN),
    ],
)
def test_window_ruling(published: dt.datetime | None, expected: str) -> None:
    assert (
        rule_eligibility(
            published_at=published,
            window_start_exclusive=WINDOW_START,
            window_end_inclusive=WINDOW_END,
        )
        == expected
    )


def test_naive_timestamp_is_refused_rather_than_assumed() -> None:
    with pytest.raises(AttributionError):
        rule_eligibility(
            published_at=dt.datetime(2026, 8, 20, 9, 31),
            window_start_exclusive=WINDOW_START,
            window_end_inclusive=WINDOW_END,
        )


# --- attribution ---------------------------------------------------------


def test_no_eligible_evidence_is_unattributed_in_fixed_wording() -> None:
    record = build_attribution(
        event=make_event(),
        materials=materials(
            item(
                published_at=dt.datetime(2026, 8, 20, 17, 38, tzinfo=KST),
                eligibility=ELIGIBILITY_AFTER_MOVE,
            ),
            item(published_at=None, eligibility=ELIGIBILITY_TIMESTAMP_UNKNOWN),
        ),
    )
    assert record.unattributed is True
    assert record.attribution_types == ()
    assert record.candidates == ()
    assert len(record.rejected) == 2
    assert record.unattributed_reason is not None
    assert UNATTRIBUTED_PHRASE in record.unattributed_reason
    assert scored_class(record) == UNATTRIBUTED


def test_unattributed_reason_states_absence_not_a_substitute_cause() -> None:
    record = build_attribution(event=make_event(), materials=materials())
    reason = record.unattributed_reason or ""
    for euphemism in ("기타", "시장 전반", "market-wide", "misc"):
        assert euphemism not in reason
    # It names what was unreadable rather than implying nothing was there.
    assert "flow" in reason


def test_multiple_candidate_types_are_all_kept() -> None:
    record = build_attribution(
        event=make_event(),
        materials=materials(
            item(
                attribution_type="news",
                published_at=dt.datetime(2026, 8, 20, 9, 31, tzinfo=KST),
                eligibility=ELIGIBILITY_ELIGIBLE,
                title="broker note",
            ),
            item(
                attribution_type="disclosure",
                published_at=dt.datetime(2026, 8, 20, 10, 40, tzinfo=KST),
                eligibility=ELIGIBILITY_ELIGIBLE,
                title="조회공시요구",
            ),
        ),
    )
    assert record.unattributed is False
    assert set(record.attribution_types) == {"disclosure", "news"}
    assert len(record.candidates) == 2


def test_type_priority_orders_candidates_and_picks_the_scored_class() -> None:
    record = build_attribution(
        event=make_event(),
        materials=materials(
            item(
                attribution_type="news",
                published_at=dt.datetime(2026, 8, 20, 14, 0, tzinfo=KST),
                eligibility=ELIGIBILITY_ELIGIBLE,
                title="later news",
            ),
            item(
                attribution_type="disclosure",
                published_at=dt.datetime(2026, 8, 20, 10, 40, tzinfo=KST),
                eligibility=ELIGIBILITY_ELIGIBLE,
                title="earlier filing",
            ),
        ),
    )
    # Disclosure outranks news even though the news item is more recent.
    assert record.attribution_types[0] == "disclosure"
    assert scored_class(record) == "disclosure"
    # ...and the news candidate is still on the record, not discarded.
    assert [c.title for c in record.candidates] == ["earlier filing", "later news"]


def test_recency_breaks_ties_inside_one_type() -> None:
    record = build_attribution(
        event=make_event(),
        materials=materials(
            item(
                published_at=dt.datetime(2026, 8, 20, 9, 0, tzinfo=KST),
                eligibility=ELIGIBILITY_ELIGIBLE,
                title="older",
            ),
            item(
                published_at=dt.datetime(2026, 8, 20, 14, 0, tzinfo=KST),
                eligibility=ELIGIBILITY_ELIGIBLE,
                title="newer",
            ),
        ),
    )
    assert [c.title for c in record.candidates] == ["newer", "older"]


def test_unjudged_news_is_a_visible_candidate_never_auto_excluded() -> None:
    record = build_attribution(
        event=make_event(),
        materials=materials(
            item(
                published_at=dt.datetime(2026, 8, 20, 9, 31, tzinfo=KST),
                eligibility=ELIGIBILITY_ELIGIBLE,
                judgment="unjudged",
            )
        ),
    )
    assert record.candidates[0].judgment == "unjudged"
    assert record.unattributed is False


def test_unattributed_is_never_accepted_as_an_evidence_type() -> None:
    with pytest.raises(AttributionError):
        build_attribution(
            event=make_event(),
            materials=materials(
                item(
                    attribution_type="unattributed",
                    published_at=dt.datetime(2026, 8, 20, 9, 31, tzinfo=KST),
                    eligibility=ELIGIBILITY_ELIGIBLE,
                )
            ),
        )


def test_record_is_json_safe_and_declares_no_promotion() -> None:
    record = build_attribution(event=make_event(), materials=materials())
    payload = record.as_dict()
    assert payload["promote"] is False
    assert payload["live_gate_impact"] is False
    assert payload["correlation_id"].startswith("rob-1303-spike-attribution:kr:035420:")
    summary = record_summary(record)
    assert summary["scored_class"] == UNATTRIBUTED
    assert summary["unattributed"] is True
