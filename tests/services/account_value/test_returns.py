from datetime import date, timedelta
from decimal import Decimal

from app.services.account_value.returns import Flow, modified_dietz


def test_mid_period_deposit_does_not_count_as_return():
    start = date(2026, 1, 1)
    values = {
        start + timedelta(days=i): Decimal(100 if i < 3 else 150) for i in range(8)
    }
    result = modified_dietz(
        start,
        start + timedelta(days=7),
        values,
        [Flow(start + timedelta(days=3), Decimal(50))],
    )
    assert result.value == 0
    assert result.reason is None


def test_missing_intermediate_day_is_unmeasured():
    start = date(2026, 1, 1)
    result = modified_dietz(
        start,
        start + timedelta(days=2),
        {start: Decimal(100), start + timedelta(days=2): Decimal(100)},
        [],
    )
    assert result.value is None
    assert result.reason == "missing_snapshot_day"


def test_missing_benchmark_is_unknown():
    from app.services.account_value.returns import benchmark_return

    result = benchmark_return("kr", Decimal(100), None)
    assert result.value is None
    assert result.reason == "benchmark_missing"


def test_us_mixed_adjustment_is_not_total_return():
    from app.services.account_value.returns import benchmark_return

    result = benchmark_return("us", Decimal(100), Decimal(105))
    assert result.value is None
    assert result.reason == "benchmark_total_return_unverified"
