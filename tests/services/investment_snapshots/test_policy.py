"""ROB-269 Phase 2 — policy constants."""

from __future__ import annotations

import datetime as dt

import pytest

from app.services.investment_snapshots.policy import (
    INTRADAY_ACTION_REPORT_V1,
    POLICIES,
    get_policy,
)


def test_intraday_action_report_v1_has_expected_required_kinds():
    assert set(INTRADAY_ACTION_REPORT_V1.required_kinds()) == {
        "portfolio",
        "journal",
        "watch_context",
        "market",
    }


def test_intraday_action_report_v1_optional_kinds_match_phase1_whitelist():
    optional = set(INTRADAY_ACTION_REPORT_V1.optional_kinds())
    # symbol/candidate_universe/news are domain-ref candidates;
    # naver/toss/browser/invest_page are whitelisted-generic (pre-plan Decision 1).
    # pending_orders is ROB-274 — broker pending orders feed the proposal
    # classifier; optional because broker-side fetch failures degrade to
    # action/review rather than blocking the whole bundle.
    assert optional == {
        "symbol",
        "candidate_universe",
        "kr_market_ranking",
        "news",
        "naver_remote_debug",
        "toss_remote_debug",
        "browser_probe",
        "invest_page",
        "pending_orders",
    }


def test_intraday_action_report_v1_all_kinds_have_positive_ttls():
    for kind in INTRADAY_ACTION_REPORT_V1.kinds:
        assert kind.freshness.soft_ttl > dt.timedelta(0)
        assert kind.freshness.hard_ttl > kind.freshness.soft_ttl
        assert kind.collector_timeout > dt.timedelta(0)


def test_kind_policy_lookup():
    portfolio = INTRADAY_ACTION_REPORT_V1.kind_policy("portfolio")
    assert portfolio is not None
    assert portfolio.required is True
    assert INTRADAY_ACTION_REPORT_V1.kind_policy("never_a_kind") is None


def test_to_snapshot_json_round_trip_shape():
    snapshot = INTRADAY_ACTION_REPORT_V1.to_snapshot_json()
    assert snapshot["policy_version"] == "intraday_action_report_v1"
    assert snapshot["bundle_ttl_seconds"] == {"soft": 180, "hard": 300}
    assert isinstance(snapshot["kinds"], list)
    portfolio_entry = next(
        k for k in snapshot["kinds"] if k["snapshot_kind"] == "portfolio"
    )
    assert portfolio_entry["required"] is True
    assert portfolio_entry["soft_ttl_seconds"] == 180
    assert portfolio_entry["hard_ttl_seconds"] == 300


def test_get_policy_returns_intraday_action_report_v1():
    assert get_policy("intraday_action_report_v1") is INTRADAY_ACTION_REPORT_V1


def test_get_policy_unknown_raises_keyerror_with_listed_options():
    with pytest.raises(KeyError, match="intraday_action_report_v1"):
        get_policy("nonexistent_policy")


def test_policy_registry_only_contains_v1_in_phase2():
    # Phase 2 ships exactly one policy. Adding new policies should require an
    # explicit reviewer pass; this test fails if a new policy is added without
    # also updating the test (forces a reviewer touch).
    assert set(POLICIES) == {"intraday_action_report_v1"}
