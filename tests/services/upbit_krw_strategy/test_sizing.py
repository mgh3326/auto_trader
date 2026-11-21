from __future__ import annotations

import math
from decimal import Decimal

import pytest

from app.services.upbit_krw_strategy import sizing as z

D = Decimal


def growth_closes(rate: str, count: int = 21) -> tuple[Decimal, ...]:
    """Newest first, constant daily return ``rate``."""
    growth = D(1) + D(rate)
    return tuple(D(100) * growth ** (count - 1 - k) for k in range(count))


def test_constant_daily_return_gives_abs_return_times_sqrt_365() -> None:
    sigma = z.realized_vol_annual(growth_closes("0.01"))
    assert sigma is not None
    assert abs(float(sigma) - 0.01 * math.sqrt(365)) < 1e-12


def test_vol_unavailable_cases() -> None:
    assert z.realized_vol_annual(growth_closes("0.01", 20)) is None  # needs 21 closes
    flat = tuple(D(100) for _ in range(21))
    assert z.realized_vol_annual(flat) is None  # zero estimate cannot size
    bad = list(growth_closes("0.01"))
    bad[3] = D(0)
    assert z.realized_vol_annual(tuple(bad)) is None


def test_vol_uses_only_the_newest_21_closes() -> None:
    base = growth_closes("0.01")
    assert z.realized_vol_annual(base + (D(5), D(900))) == z.realized_vol_annual(base)


def test_ewma_weights_favour_the_newest_return() -> None:
    quiet_then_jump = list(growth_closes("0.001"))
    jumped_new = tuple([quiet_then_jump[0] * D("1.1")] + quiet_then_jump[1:])
    jumped_old = tuple(
        quiet_then_jump[:-2] + [quiet_then_jump[-2] * D("1.1"), quiet_then_jump[-1]]
    )
    new_vol = z.realized_vol_annual(jumped_new)
    old_vol = z.realized_vol_annual(jumped_old)
    assert new_vol is not None and old_vol is not None and new_vol > old_vol


@pytest.mark.parametrize(
    ("sigma", "target"),
    [
        ("0.2", "480000"),  # below sigma*: capped, never scaled up
        ("0.6", "480000"),
        ("1.2", "240000"),
        ("3", "96000"),
        ("5.76", "50000"),  # exact floor boundary survives rounding
    ],
)
def test_target_notional_golden(sigma: str, target: str) -> None:
    assert z.target_notional_krw(D(sigma)) == D(target)


def test_target_notional_rejects_bad_sigma() -> None:
    for bad in (D(0), D(-1), D("NaN")):
        with pytest.raises(ValueError):
            z.target_notional_krw(bad)


def test_size_entry_golden_on_a_one_krw_tick() -> None:
    size = z.size_entry(sigma_hat=D("1.2"), limit_price_krw=D("1234"))
    assert size.sized and size.reason_code == "ok"
    assert size.target_notional_krw == D("240000")
    # 240000 / 1234 = 194.489465151...; floored, never rounded up
    assert size.quantity == D("194.48946515")
    assert size.notional_krw == D("194.48946515") * D("1234")
    assert size.notional_krw <= D("240000")


def test_size_entry_exact_quantity_and_cap() -> None:
    size = z.size_entry(sigma_hat=D("0.2"), limit_price_krw=D("1000"))
    assert (size.quantity, size.notional_krw) == (D("480"), D("480000"))


def test_size_entry_floor_boundary_is_inclusive() -> None:
    at = z.size_entry(sigma_hat=D("5.76"), limit_price_krw=D("1000"))
    assert at.sized and at.notional_krw == D("50000")
    below = z.size_entry(sigma_hat=D("5.77"), limit_price_krw=D("1000"))
    assert (below.sized, below.reason_code) == (False, "skipped_below_floor")
    assert below.quantity is None


def test_size_entry_without_vol_is_skipped_not_zero_sized() -> None:
    size = z.size_entry(sigma_hat=None, limit_price_krw=D("1000"))
    assert (size.sized, size.reason_code) == (False, "skipped_vol_unavailable")


def test_size_entry_below_minimum_order() -> None:
    # 50,000 / 1e13 = 5e-9 -> floors to 0 at 8 decimals
    size = z.size_entry(sigma_hat=D("5.76"), limit_price_krw=D("10000000000000"))
    assert (size.sized, size.reason_code) == (False, "skipped_below_min_order")
    assert size.quantity == D(0)


def test_size_entry_refuses_bad_prices() -> None:
    with pytest.raises(ValueError, match="tick"):
        z.size_entry(sigma_hat=D("1"), limit_price_krw=D("1234.5"))
    with pytest.raises(ValueError, match="100"):
        z.size_entry(sigma_hat=D("1"), limit_price_krw=D("99"))
    with pytest.raises(TypeError):
        z.size_entry(sigma_hat=D("1"), limit_price_krw=1000.0)  # type: ignore[arg-type]
