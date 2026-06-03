from __future__ import annotations

import pytest

from app.services.snapshot_commit_guard import (
    PartialCommitBlocked,
    assert_min_coverage,
)


def test_passes_when_count_meets_floor():
    # floor = ceil(100 * 0.60) = 60
    assert assert_min_coverage(60, 100, market="kr") is None
    assert assert_min_coverage(99, 100, market="kr") is None


def test_blocks_when_count_below_floor():
    with pytest.raises(PartialCommitBlocked) as exc:
        assert_min_coverage(20, 100, market="kr")
    assert exc.value.count == 20
    assert exc.value.universe_count == 100
    assert exc.value.market == "kr"


def test_universe_zero_disables_gate():
    # never block on a missing universe denominator
    assert assert_min_coverage(0, 0, market="kr") is None
    assert assert_min_coverage(5, 0, market="us") is None


def test_custom_ratio():
    # floor = ceil(100 * 0.5) = 50
    assert assert_min_coverage(50, 100, market="kr", min_ratio=0.5) is None
    with pytest.raises(PartialCommitBlocked):
        assert_min_coverage(49, 100, market="kr", min_ratio=0.5)
