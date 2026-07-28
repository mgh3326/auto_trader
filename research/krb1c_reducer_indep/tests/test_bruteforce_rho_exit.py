"""Brute-force validation of the §4.6/§4.7 exit-candidate shortcut.

Both the reference implementation and this one compute

    rho_exit(P) = max over Q in X_m(P) of tick(Q)/Q

on the *finite* candidate set X_m(P) = {P} ∪ {tick_ceil(d_h) : d_h > P},
justified by §4.7. Agreement between two implementations that share that
shortcut would not detect an error in the shortcut itself. This test therefore
recomputes rho_exit by scanning **every** valid quote price Q >= P, using no
part of the shortcut.

Coverage of the open-ended final band: the scan runs to 1,000,000, i.e. a full
500,000 KRW into the [500,000, inf) band. Inside a band tick is constant, so
tick(Q)/Q is strictly decreasing there; the test asserts that decrease
empirically over the scanned portion, which pins the band's maximum at its
first valid price and makes the unscanned tail irrelevant.

Comparisons use integer cross-multiplication (tick(Q)*P vs tick(P)*Q) — exact,
and fast enough to run the whole 4,001-price sweep.
"""

from __future__ import annotations

import pytest

from research.krb1c_reducer_indep.reducer import rho_exit_of
from research.krb1c_reducer_indep.tick import (
    KOSDAQ_TICK_TABLE,
    KOSPI_TICK_TABLE,
    PRICE_MAX,
    PRICE_MIN,
    TickTable,
)

SCAN_LIMIT = 1_000_000

TABLES = (KOSPI_TICK_TABLE, KOSDAQ_TICK_TABLE)


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.market)
def test_tick_over_q_strictly_decreases_inside_each_band(table: TickTable) -> None:
    """The monotonicity §4.7 relies on, checked directly on the lattice."""
    for band in table.bands:
        start = table.tick_ceil(max(band.lower, 1))
        stop = band.upper if band.upper is not None else SCAN_LIMIT
        q = start
        while True:
            nxt = q + band.tick
            if nxt >= stop:
                break
            # tick constant in band => tick/q > tick/nxt  <=>  nxt > q
            assert table.tick(q) == band.tick
            assert table.tick(nxt) == band.tick
            assert band.tick * nxt > band.tick * q
            q = nxt


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.market)
def test_rho_exit_matches_full_scan_over_all_valid_q(table: TickTable) -> None:
    """Full sweep: every P in E_m, rho_exit recomputed by scanning all valid Q.

    Runs the complete 4,001-price enumeration per market — not a sample.
    """
    all_q = tuple(table.valid_prices(PRICE_MIN, SCAN_LIMIT))
    prices = table.valid_prices(PRICE_MIN, PRICE_MAX)
    assert len(prices) == 4_001

    ticks = {q: table.tick(q) for q in all_q}

    # Running maximum from the top down: best_from[i] = argmax over all_q[i:]
    # of tick(Q)/Q, with ties resolved to the *least* Q (§4.8).
    n = len(all_q)
    best_idx = [0] * n
    best_idx[n - 1] = n - 1
    for i in range(n - 2, -1, -1):
        j = best_idx[i + 1]
        qi, qj = all_q[i], all_q[j]
        # tick(qi)/qi  >=  tick(qj)/qj   <=>   tick(qi)*qj >= tick(qj)*qi
        if ticks[qi] * qj >= ticks[qj] * qi:
            best_idx[i] = i  # ">=" keeps the least Q on a tie
        else:
            best_idx[i] = j

    index_of = {q: i for i, q in enumerate(all_q)}
    checked = 0
    for price in prices:
        expected_q = all_q[best_idx[index_of[price]]]
        rho, witness = rho_exit_of(table, price)
        assert witness == expected_q, f"{table.market} P={price}"
        assert rho.numerator * expected_q == ticks[expected_q] * rho.denominator
        checked += 1
    assert checked == 4_001
