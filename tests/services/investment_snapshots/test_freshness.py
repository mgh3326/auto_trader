# tests/services/investment_snapshots/test_freshness.py
import datetime as dt

import pytest

from app.services.investment_snapshots.freshness import (
    FreshnessPolicy,
    classify_freshness,
)


def _policy(soft: int, hard: int) -> FreshnessPolicy:
    return FreshnessPolicy(
        soft_ttl=dt.timedelta(seconds=soft),
        hard_ttl=dt.timedelta(seconds=hard),
    )


def test_classify_fresh_within_soft():
    now = dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)
    as_of = now - dt.timedelta(seconds=30)
    assert classify_freshness(as_of=as_of, now=now, policy=_policy(60, 300)) == "fresh"


def test_classify_soft_stale_past_soft_within_hard():
    now = dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)
    as_of = now - dt.timedelta(seconds=120)
    assert (
        classify_freshness(as_of=as_of, now=now, policy=_policy(60, 300))
        == "soft_stale"
    )


def test_classify_hard_stale_past_hard():
    now = dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)
    as_of = now - dt.timedelta(seconds=900)
    assert (
        classify_freshness(as_of=as_of, now=now, policy=_policy(60, 300))
        == "hard_stale"
    )


def test_classify_unavailable_when_as_of_is_none():
    now = dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)
    assert (
        classify_freshness(as_of=None, now=now, policy=_policy(60, 300))
        == "unavailable"
    )


def test_classify_rejects_naive_datetime():
    now = dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)
    naive = dt.datetime(2026, 5, 19, 11, 10, 30)  # no tzinfo
    with pytest.raises(ValueError, match="tz-aware"):
        classify_freshness(as_of=naive, now=now, policy=_policy(60, 300))


def test_classify_rejects_future_as_of_more_than_skew():
    now = dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)
    future = now + dt.timedelta(seconds=120)
    with pytest.raises(ValueError, match="future"):
        classify_freshness(as_of=future, now=now, policy=_policy(60, 300))


def test_classify_tolerates_small_clock_skew_into_future():
    now = dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)
    slight_future = now + dt.timedelta(seconds=2)
    assert (
        classify_freshness(as_of=slight_future, now=now, policy=_policy(60, 300))
        == "fresh"
    )
