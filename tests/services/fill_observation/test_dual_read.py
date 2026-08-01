from decimal import Decimal

import pytest

from app.services.fill_observation.contracts import FillDualReadStatus
from app.services.fill_observation.dual_read import _classify_dual_read

pytestmark = pytest.mark.unit


def test_dual_read_exposes_review_trades_partial_fill_loss() -> None:
    status, mismatches = _classify_dual_read(
        observation_count=2,
        observation_quantity=Decimal("5"),
        review_trade_quantity=Decimal("2"),
        execution_ledger_quantity=Decimal("5"),
    )

    assert status is FillDualReadStatus.MISMATCH
    assert mismatches == ("review.trades",)


def test_dual_read_matches_all_present_legacy_projections() -> None:
    status, mismatches = _classify_dual_read(
        observation_count=2,
        observation_quantity=Decimal("5"),
        review_trade_quantity=Decimal("5"),
        execution_ledger_quantity=Decimal("5"),
    )

    assert status is FillDualReadStatus.MATCH
    assert mismatches == ()


@pytest.mark.parametrize(
    (
        "observation_count",
        "trade_quantity",
        "execution_quantity",
        "expected",
    ),
    [
        (1, None, None, FillDualReadStatus.NEW_ONLY),
        (0, Decimal("1"), None, FillDualReadStatus.LEGACY_ONLY),
        (0, None, None, FillDualReadStatus.EMPTY),
    ],
)
def test_dual_read_reports_migration_presence_states(
    observation_count: int,
    trade_quantity: Decimal | None,
    execution_quantity: Decimal | None,
    expected: FillDualReadStatus,
) -> None:
    status, mismatches = _classify_dual_read(
        observation_count=observation_count,
        observation_quantity=(Decimal("1") if observation_count else Decimal(0)),
        review_trade_quantity=trade_quantity,
        execution_ledger_quantity=execution_quantity,
    )
    assert status is expected
    assert mismatches == ()
