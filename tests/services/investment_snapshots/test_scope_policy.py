import pytest

from app.services.investment_snapshots.scope_policy import (
    ACCOUNT_INDEPENDENT_SNAPSHOT_KINDS,
    is_account_independent,
    normalize_account_scope,
)


@pytest.mark.unit
def test_account_independent_kinds_are_exactly_the_shared_four() -> None:
    assert ACCOUNT_INDEPENDENT_SNAPSHOT_KINDS == frozenset(
        {"market", "news", "candidate_universe", "symbol"}
    )


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["market", "news", "candidate_universe", "symbol"])
def test_independent_kinds_normalize_scope_to_none(kind: str) -> None:
    assert is_account_independent(kind) is True
    assert normalize_account_scope(kind, "kis_live") is None
    assert normalize_account_scope(kind, "kis_mock") is None
    assert normalize_account_scope(kind, None) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "kind", ["portfolio", "journal", "watch_context", "pending_orders"]
)
def test_account_bound_kinds_preserve_scope(kind: str) -> None:
    assert is_account_independent(kind) is False
    assert normalize_account_scope(kind, "kis_live") == "kis_live"
    assert normalize_account_scope(kind, "kis_mock") == "kis_mock"
    assert normalize_account_scope(kind, None) is None
