"""ROB-491 status handling: pending is not a verdict, excluded is honoured.

``symbol_news_relevance.status`` is written only by the external judgment job.
Three cases have to stay distinguishable, and conflating any two of them either
invents a judgment we do not have or overrides one we do.
"""

from __future__ import annotations

import pytest

from app.services.spike_attribution.contract import (
    ELIGIBILITY_ELIGIBLE,
    ELIGIBILITY_JUDGED_NOT_RELEVANT,
)
from app.services.spike_attribution.materials import (
    EXTERNALLY_JUDGED_STATUSES,
    JUDGMENT_BY_STATUS,
)
from app.services.spike_attribution.spec import PRE_REGISTRATION


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("confirmed", "judged_relevant"),
        ("excluded", "judged_not_relevant"),
        ("pending", "unjudged"),
        (None, "unjudged"),
        ("some_future_status", "unjudged"),
    ],
)
def test_status_maps_to_the_right_judgment(status: str | None, expected: str) -> None:
    assert JUDGMENT_BY_STATUS.get(status, "unjudged") == expected


def test_a_pending_row_is_not_reported_as_judged() -> None:
    # A queued row means nobody has looked yet. Reporting it as
    # "judged_relevant" would manufacture a verdict out of a work item.
    assert JUDGMENT_BY_STATUS["pending"] == "unjudged"
    assert "pending" not in EXTERNALLY_JUDGED_STATUSES


def test_only_real_verdicts_count_as_externally_judged() -> None:
    assert EXTERNALLY_JUDGED_STATUSES == {"confirmed", "excluded"}


def test_excluded_is_honoured_as_not_a_cause() -> None:
    # ROB-491: this code never *sets* excluded, but when the judge has set it,
    # the article must not become a cause candidate just because its timestamp
    # lands inside the window.
    assert ELIGIBILITY_JUDGED_NOT_RELEVANT != ELIGIBILITY_ELIGIBLE
    assert JUDGMENT_BY_STATUS["excluded"] == "judged_not_relevant"


def test_the_status_map_matches_the_pinned_pre_registration() -> None:
    pinned = PRE_REGISTRATION["attribution"]["judgment_status_map"]
    for status, judgment in JUDGMENT_BY_STATUS.items():
        assert pinned[status] == judgment
    assert pinned["no_row"] == "unjudged"
    assert PRE_REGISTRATION["attribution"]["pending_is_not_a_verdict"] is True


def test_every_judgment_value_is_in_the_pinned_vocabulary() -> None:
    vocabulary = set(PRE_REGISTRATION["attribution"]["confidence_vocabulary"])
    assert set(JUDGMENT_BY_STATUS.values()) <= vocabulary
    # ``not_applicable`` covers disclosures/earnings, which carry no judgment.
    assert "not_applicable" in vocabulary


def test_pinned_status_vocabulary_matches_the_db_check_constraint() -> None:
    # symbol_news_relevance CHECK: status IN ('pending','confirmed','excluded')
    assert set(PRE_REGISTRATION["materials"]["news"]["status_vocabulary"]) == {
        "confirmed",
        "pending",
        "excluded",
    }
