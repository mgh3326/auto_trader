"""Canonical content hash of an approved clause (§3.1, I21). No database."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services.mandate import hashing

pytestmark = pytest.mark.unit
D = Decimal


def _terms(**over):
    base = dict(
        sleeve_id=3,
        terms_version="mandate-terms/v1",
        basket_key="basket-1",
        account_ref="acct",
        universe_rule="rule",
        allowed_symbols=["005930", "000270"],
        entry_max_premium_bps="30",
        per_order_cap="2000000",
        daily_cap="5000000",
        sector_cap_pct="35",
        basket_drawdown_freeze_pct="25",
        account_drawdown_freeze_pct="25",
        tranche_rule={
            "weights": ["0.4", "0.3", "0.3"],
            "triggers_pct_from_first": ["0", "-15", "-30"],
            "min_gap_sessions": 20,
        },
        loss_sell_mode="human_card",
        expires_at=datetime(2026, 12, 1, 0, 0, tzinfo=UTC),
        sleeve_state="live",
        budget_cap="9750000",
        loss_limit="500000",
        prereg_metric="m",
        prereg_min_effect_pct="1",
        prereg_horizon_sessions=20,
        prereg_max_duration_days=90,
    )
    base.update(over)
    return hashing.terms_content(**base)


def test_decimals_hash_the_same_after_a_numeric_round_trip() -> None:
    a = hashing.content_sha256(_terms(entry_max_premium_bps="30"))
    b = hashing.content_sha256(_terms(entry_max_premium_bps=D("30.0000")))
    c = hashing.content_sha256(_terms(entry_max_premium_bps="3E+1"))
    assert a == b == c
    assert hashing.canonical_decimal("0.40") == "0.4"
    assert hashing.canonical_decimal("-15.0000") == "-15"
    assert hashing.canonical_decimal("0.0000") == "0"


def test_symbol_order_does_not_change_the_hash_but_a_value_does() -> None:
    base = hashing.content_sha256(_terms())
    assert base == hashing.content_sha256(_terms(allowed_symbols=["000270", "005930"]))
    assert base != hashing.content_sha256(_terms(allowed_symbols=["000270"]))
    assert base != hashing.content_sha256(_terms(daily_cap="5000001"))
    assert base != hashing.content_sha256(_terms(budget_cap="9750001"))
    assert base != hashing.content_sha256(_terms(account_ref="other"))


def test_timestamps_hash_in_utc_regardless_of_offset() -> None:
    kst = timezone(timedelta(hours=9))
    a = hashing.content_sha256(_terms(expires_at=datetime(2026, 12, 1, 9, 0, tzinfo=kst)))
    assert a == hashing.content_sha256(_terms())
    with pytest.raises(ValueError):
        _terms(expires_at=datetime(2026, 12, 1))


def test_row_identifiers_are_not_part_of_the_content() -> None:
    content = _terms()
    assert "approval_id" not in content and "sleeve_version_id" not in content


def test_profile_content_is_stable() -> None:
    p = hashing.profile_content(
        sleeve_id=3,
        profile_version=1,
        terms_id=9,
        max_tranche_index=1,
        effective_session=date(2026, 11, 23),
        expires_at=datetime(2026, 12, 1, tzinfo=UTC),
    )
    assert p["effective_session"] == "2026-11-23"
    assert hashing.content_sha256(p) == hashing.content_sha256(dict(reversed(list(p.items()))))
