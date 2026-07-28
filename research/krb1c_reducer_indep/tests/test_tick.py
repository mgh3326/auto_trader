"""§4 tick function / E_m / X_m(P) tests."""

from __future__ import annotations

from fractions import Fraction

import pytest

from research.krb1c_reducer_indep.tick import (
    KOSDAQ_TICK_TABLE,
    KOSPI_TICK_TABLE,
    PRICE_MAX,
    PRICE_MIN,
    TickBand,
    TickTable,
    TickTableError,
)

TABLES = (KOSPI_TICK_TABLE, KOSDAQ_TICK_TABLE)


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.market)
@pytest.mark.parametrize(
    "price,expected",
    [
        (0, 1),
        (1_999, 1),
        (2_000, 5),
        (4_999, 5),
        (5_000, 10),
        (9_990, 10),
        (10_000, 10),  # sealed verify §1: current table, NOT the old 50
        (19_999, 10),
        (20_000, 50),
        (49_999, 50),
        (50_000, 100),
        (199_999, 100),
        (200_000, 500),
        (400_000, 500),
        (499_999, 500),
        (500_000, 1_000),
        (10_000_000, 1_000),
    ],
)
def test_tick_boundaries(table: TickTable, price: int, expected: int) -> None:
    assert table.tick(price) == expected


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.market)
@pytest.mark.parametrize(
    "value,expected",
    [
        (5_000, 5_000),
        (5_001, 5_010),
        (19_991, 20_000),  # crosses into the 50-tick band
        (19_999, 20_000),
        (20_000, 20_000),
        (20_001, 20_050),
        (49_951, 50_000),
        (50_000, 50_000),
        (50_001, 50_100),
        (199_901, 200_000),
        (200_000, 200_000),
        (200_001, 200_500),
        (499_501, 500_000),
        (500_000, 500_000),
        (500_001, 501_000),
    ],
)
def test_tick_ceil_int(table: TickTable, value: int, expected: int) -> None:
    assert table.tick_ceil(value) == expected


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.market)
def test_tick_ceil_accepts_fraction_exactly(table: TickTable) -> None:
    # exactly on a lattice point -> unchanged
    assert table.tick_ceil(Fraction(20_000)) == 20_000
    # a hair above -> next lattice point
    assert table.tick_ceil(Fraction(2_000_001, 100)) == 20_050
    # a hair below a band edge -> the band edge itself
    assert table.tick_ceil(Fraction(1_999_999, 100)) == 20_000


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.market)
def test_tick_ceil_rejects_float(table: TickTable) -> None:
    with pytest.raises(TickTableError):
        table.tick_ceil(20000.5)  # type: ignore[arg-type]


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.market)
def test_tick_ceil_is_least_valid_price_brute_force(table: TickTable) -> None:
    """Cross-check tick_ceil against a brute-force scan on a dense window."""
    for x in range(4_990, 5_120):
        got = table.tick_ceil(x)
        probe = x
        while not table.is_valid_price(probe):
            probe += 1
        assert got == probe, x
    for x in range(19_980, 20_120):
        got = table.tick_ceil(x)
        probe = x
        while not table.is_valid_price(probe):
            probe += 1
        assert got == probe, x
    for x in range(199_900, 200_600):
        got = table.tick_ceil(x)
        probe = x
        while not table.is_valid_price(probe):
            probe += 1
        assert got == probe, x


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.market)
def test_valid_prices_full_enumeration(table: TickTable) -> None:
    prices = table.valid_prices(PRICE_MIN, PRICE_MAX)

    # sealed verify §1 asserts 4,001 valid prices per market on [5k, 400k]
    assert len(prices) == 4_001
    assert prices[0] == PRICE_MIN
    assert prices[-1] == PRICE_MAX
    # strict=False is deliberate: this is an offset self-pairing, so the two
    # operands differ in length by exactly one and strict=True would raise.
    assert all(b > a for a, b in zip(prices, prices[1:], strict=False))
    assert all(table.is_valid_price(p) for p in prices)

    # band-by-band counts
    counts = {
        10: len([p for p in prices if 5_000 <= p < 20_000]),
        50: len([p for p in prices if 20_000 <= p < 50_000]),
        100: len([p for p in prices if 50_000 <= p < 200_000]),
        500: len([p for p in prices if 200_000 <= p <= 400_000]),
    }
    assert counts == {10: 1_500, 50: 600, 100: 1_500, 500: 401}

    # completeness: the enumeration equals a brute-force filter of every
    # integer in range (no sampling, nothing skipped)
    brute = tuple(x for x in range(PRICE_MIN, PRICE_MAX + 1) if table.is_valid_price(x))
    assert prices == brute


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.market)
@pytest.mark.parametrize(
    "price,expected",
    [
        (5_000, (5_000, 20_000, 50_000, 200_000, 500_000)),
        (20_000, (20_000, 50_000, 200_000, 500_000)),
        (50_000, (50_000, 200_000, 500_000)),
        (200_000, (200_000, 500_000)),
        (400_000, (400_000, 500_000)),
    ],
)
def test_exit_candidates(table: TickTable, price: int, expected: tuple) -> None:
    assert table.exit_candidates(price) == expected


def test_table_rejects_gap() -> None:
    with pytest.raises(TickTableError):
        TickTable(
            "BAD",
            [TickBand(0, 100, 1), TickBand(200, None, 5)],
            "test",
        )


def test_table_rejects_overlap() -> None:
    with pytest.raises(TickTableError):
        TickTable(
            "BAD",
            [TickBand(0, 300, 1), TickBand(200, None, 5)],
            "test",
        )


def test_table_rejects_bounded_last_band() -> None:
    with pytest.raises(TickTableError):
        TickTable("BAD", [TickBand(0, 100, 1)], "test")


def test_table_rejects_misaligned_band_lower() -> None:
    with pytest.raises(TickTableError):
        TickTable(
            "BAD",
            [TickBand(0, 101, 1), TickBand(101, None, 5)],
            "test",
        )
