"""ROB-1061 H3 adversarial-verification remediation (2026-07-26) -- the golden
output digest the prior remediation round promised and never delivered.

Every determinism test in ``test_determinism.py`` (and every reason-histogram
reconciliation test) is shaped ``r1 == r2`` -- two live re-computations of the
SAME code compared against EACH OTHER. That shape is structurally blind to any
mutation that is itself deterministic (produces the same wrong output every
time it runs), because ``r1 == r2`` holds trivially for ANY pure function,
buggy or not. The independent adversarial verifier demonstrated this
concretely: a mutant that injects a constant, additive key into every evidence
dict (changing every ``evidence_hash`` in the output) survives all 131 prior
tests, because nothing anywhere compares a live run's output against a FIXED,
pinned, independently-produced expected value.

This file is that fixed point: one hand-built fixture per engine (chosen to
hit several distinct reason codes each), hashed via the SAME canonical AST
authority (``canonical_hash.canonical_sha256``) H1/H2/ROB-846 use, pinned as a
literal module constant -- mirroring ``alpaca_track_seal/artifact.py``'s
``SEALED_ARTIFACT_SEMANTIC_HASH`` pin.

Changing either ``_AP_A1_GOLDEN_DIGEST``/``_AP_A2_GOLDEN_DIGEST`` constant
below is a DELIBERATE re-pin (the fixture changed, or a real, reviewed engine
behavior change is landing) -- never a routine edit to make a failing test
pass. If you are editing either constant because a test failed, STOP and
report: silently re-pinning to whatever the (possibly buggy) code currently
produces is exactly the kind of post-hoc relaxation this file exists to
prevent.
"""

from __future__ import annotations

from datetime import UTC, datetime

import canonical_hash
import configs as cfg
import dats_engine
import decision_calendar as dc
import pit_universe_alpaca as pu
import wcmb_engine
from daily_bars import DAY_MS, DailyBar

AP_A1_00 = cfg.build_ap_a1_configs()[0]
AP_A2_00 = cfg.build_ap_a2_configs()[0]

# See the module docstring: a DELIBERATE re-pin only, never a routine edit.
_AP_A1_GOLDEN_DIGEST = (
    "9c4f19422aac34365032ad3995dc10b564d09b26cacd5ae82c09aa65e5c287f6"
)
_AP_A2_GOLDEN_DIGEST = (
    "e50bd89be86d66f3d53bcb136eb09240d06b6291a0ae7bb3a2618824e78d1fc9"
)


def _ms(y, m, d, hh=0, mm=0, ss=0) -> int:
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=UTC).timestamp() * 1000)


DECISION_TS = _ms(2026, 7, 20, 0, 5, 0)  # a Monday (valid for both AP-A1/AP-A2)
WINDOW_END = dc.prior_completed_day_window(DECISION_TS)[1]


def _bars_ending_at_window(closes: list[float]) -> tuple[DailyBar, ...]:
    n = len(closes)
    bars = []
    for i, close in enumerate(closes):
        day_start = WINDOW_END - (n - i) * DAY_MS
        bars.append(
            DailyBar(
                day_start_ms=day_start,
                day_end_ms=day_start + DAY_MS,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=0.0,
                minute_count_observed=1440,
                imputed_minutes=0,
                max_gap_minutes=0,
                gap_in_last_60min=False,
                is_valid=True,
                is_segment_start=(i == 0),
            )
        )
    return tuple(bars)


def _uptrend_closes(n: int, start: float = 100.0, step: float = 0.01) -> list[float]:
    return [start * (1 + step) ** i for i in range(n)]


def _downtrend_closes(n: int, start: float = 100.0, step: float = 0.01) -> list[float]:
    return [start * (1 - step) ** i for i in range(n)]


def _flat_closes(n: int, price: float = 100.0) -> list[float]:
    return [price] * n


def _snapshot(eligible: tuple[str, ...]) -> pu.UniverseSnapshot:
    padding = tuple(f"PAD{i}/USD" for i in range(max(0, 18 - len(eligible))))
    all_eligible = tuple(sorted(set(eligible) | set(padding)))
    return pu.UniverseSnapshot(
        decision_ts_ms=DECISION_TS,
        eligible_symbols=all_eligible,
        per_symbol=(),
        n_t=len(all_eligible),
        meets_min_universe_size=len(all_eligible) >= 18,
    )


def _digest(result) -> str:
    """The full-output digest: every record's full field set (via
    ``to_dict()``, so ``evidence_hash`` and every other field are covered) in
    canonical order, plus the reason histogram. Container-permutation-
    invariant the same way ``canonical_sort``/the canonical AST authority
    already are -- this is not a NEW ordering authority, just a hash over the
    existing one's output."""
    payload = {
        "records": [record.to_dict() for record in result.records],
        "reason_histogram": dict(result.reason_histogram),
    }
    return canonical_hash.canonical_sha256(payload)


# --------------------------------------------------------------------------- #
# AP-A1 (dats_engine) fixed fixture -- 6 distinct reason codes in one decision:
# ENTRY_ACCEPTED, NO_ENTRY_SIGNAL, INSUFFICIENT_PRICE_HISTORY, EXIT_TRIGGERED,
# HYSTERESIS_HOLD, UNIVERSE_INELIGIBLE (plus INVALID_DECISION_DAY padding).
# --------------------------------------------------------------------------- #

_AP_A1_ELIGIBLE = (
    "STRONG/USD",
    "FLAT/USD",
    "SHORT/USD",
    "HELD_EXIT/USD",
    "HELD_HOLD/USD",
)


def _ap_a1_fixture_bars() -> dict[str, tuple[DailyBar, ...]]:
    return {
        "STRONG/USD": _bars_ending_at_window(_uptrend_closes(120, step=0.015)),
        "FLAT/USD": _bars_ending_at_window(_flat_closes(120)),
        "SHORT/USD": _bars_ending_at_window(_uptrend_closes(5)),
        "HELD_EXIT/USD": _bars_ending_at_window(
            [100.0 * (0.97**i) for i in range(120)]
        ),
        "HELD_HOLD/USD": _bars_ending_at_window(_uptrend_closes(120, step=0.0001)),
        "NOTELIG/USD": _bars_ending_at_window(_uptrend_closes(90)),
    }


def _ap_a1_fixture_prior_state() -> dict[str, dats_engine.AP_A1_PositionState]:
    return {
        "HELD_EXIT/USD": dats_engine.AP_A1_PositionState(
            state="long", committed_notional=50.0
        ),
        "HELD_HOLD/USD": dats_engine.AP_A1_PositionState(
            state="long", committed_notional=50.0
        ),
        "NOTELIG/USD": dats_engine.AP_A1_PositionState(state="flat"),
    }


def _run_ap_a1_fixture(*, bars_by_symbol=None):
    return dats_engine.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A1_00,
        universe=_snapshot(_AP_A1_ELIGIBLE),
        bars_by_symbol=bars_by_symbol
        if bars_by_symbol is not None
        else _ap_a1_fixture_bars(),
        prior_state=_ap_a1_fixture_prior_state(),
    )


def test_ap_a1_golden_digest_matches_the_pinned_value():
    result = _run_ap_a1_fixture()
    # Sanity: this fixture really does hit every reason code it claims to.
    assert set(result.reason_histogram) == {
        "ENTRY_ACCEPTED",
        "NO_ENTRY_SIGNAL",
        "INSUFFICIENT_PRICE_HISTORY",
        "EXIT_TRIGGERED",
        "HYSTERESIS_HOLD",
        "UNIVERSE_INELIGIBLE",
        "INVALID_DECISION_DAY",
    }
    assert _digest(result) == _AP_A1_GOLDEN_DIGEST


def test_ap_a1_golden_digest_moves_when_a_signal_changes():
    baseline = _run_ap_a1_fixture()
    assert _digest(baseline) == _AP_A1_GOLDEN_DIGEST
    bars = _ap_a1_fixture_bars()
    # A single genuine sustained-trend change on STRONG/USD (not a 1-ULP
    # perturbation -- test_determinism.py already proves ULP sensitivity for
    # evidence_hash; this proves the GOLDEN DIGEST itself is signal-sensitive)
    # flips STRONG/USD from ENTRY_ACCEPTED to NO_ENTRY_SIGNAL.
    bars["STRONG/USD"] = _bars_ending_at_window(_flat_closes(120))
    changed = _run_ap_a1_fixture(bars_by_symbol=bars)
    assert (
        "ENTRY_ACCEPTED" not in changed.reason_histogram
    )  # sanity: signal really moved
    assert _digest(changed) != _AP_A1_GOLDEN_DIGEST


def test_ap_a1_golden_digest_moves_on_additive_evidence_churn_not_only_dropped_fields():
    # The exact adversarial-verification probe: inject a CONSTANT key into
    # EVERY evidence dict (present on every record, same value every time --
    # deterministic, so an `r1 == r2` shaped test would never notice). Only a
    # value pinned against an independent, fixed expectation can catch this.
    baseline = _run_ap_a1_fixture()
    assert _digest(baseline) == _AP_A1_GOLDEN_DIGEST

    original_evidence = dats_engine._evidence

    def _churned_evidence(**kwargs):
        evidence = original_evidence(**kwargs)
        evidence["_mutant_constant_marker"] = True  # additive, same on every record
        return evidence

    import pytest as _pytest  # local import: monkeypatch fixture unavailable outside a test arg

    mp = _pytest.MonkeyPatch()
    try:
        mp.setattr(dats_engine, "_evidence", _churned_evidence)
        churned = _run_ap_a1_fixture()
    finally:
        mp.undo()

    # Every record still exists, same shape, same reason codes -- an r1==r2
    # determinism test comparing two churned runs would see no difference.
    assert {r.symbol for r in churned.records} == {r.symbol for r in baseline.records}
    assert dict(churned.reason_histogram) == dict(baseline.reason_histogram)
    assert _digest(churned) != _AP_A1_GOLDEN_DIGEST


# --------------------------------------------------------------------------- #
# AP-A2 (wcmb_engine) fixed fixture -- 6 distinct reason codes in one
# decision: RANK_BUY_ACCEPTED, INSUFFICIENT_CASH, RANK_EXCEEDS_BUFFER_EXIT,
# RANK_BUFFER_HOLD, SCORE_NOT_POSITIVE (plus INVALID_DECISION_DAY padding).
# --------------------------------------------------------------------------- #

_AP_A2_N = 30


def _ap_a2_fixture_bars() -> dict[str, tuple[DailyBar, ...]]:
    bars = {
        "KEEP/USD": _bars_ending_at_window(_uptrend_closes(_AP_A2_N, step=0.05)),
        "H0/USD": _bars_ending_at_window(_uptrend_closes(_AP_A2_N, step=0.001)),
        "NEG/USD": _bars_ending_at_window(_downtrend_closes(_AP_A2_N)),
    }
    for idx, step in enumerate((0.014, 0.013, 0.012, 0.011, 0.010)):
        bars[f"F{idx}/USD"] = _bars_ending_at_window(
            _uptrend_closes(_AP_A2_N, step=step)
        )
    return bars


def _ap_a2_fixture_prior_held() -> dict[str, wcmb_engine.AP_A2_HeldState]:
    return {
        "KEEP/USD": wcmb_engine.AP_A2_HeldState(committed_notional=1900.0),
        "H0/USD": wcmb_engine.AP_A2_HeldState(committed_notional=90.0),
    }


def _run_ap_a2_fixture(*, bars_by_symbol=None):
    bars = bars_by_symbol if bars_by_symbol is not None else _ap_a2_fixture_bars()
    return wcmb_engine.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A2_00,
        universe=_snapshot(tuple(bars)),
        bars_by_symbol=bars,
        prior_held=_ap_a2_fixture_prior_held(),
    )


def test_ap_a2_golden_digest_matches_the_pinned_value():
    result = _run_ap_a2_fixture()
    assert set(result.reason_histogram) == {
        "RANK_BUY_ACCEPTED",
        "INSUFFICIENT_CASH",
        "RANK_EXCEEDS_BUFFER_EXIT",
        "RANK_BUFFER_HOLD",
        "SCORE_NOT_POSITIVE",
        "INVALID_DECISION_DAY",
    }
    assert _digest(result) == _AP_A2_GOLDEN_DIGEST


def test_ap_a2_golden_digest_moves_when_a_signal_changes():
    baseline = _run_ap_a2_fixture()
    assert _digest(baseline) == _AP_A2_GOLDEN_DIGEST
    bars = _ap_a2_fixture_bars()
    # F0/USD (the accepted buy) loses its edge entirely -> SCORE_NOT_POSITIVE
    # instead of RANK_BUY_ACCEPTED.
    bars["F0/USD"] = _bars_ending_at_window(_downtrend_closes(_AP_A2_N))
    changed = _run_ap_a2_fixture(bars_by_symbol=bars)
    assert "RANK_BUY_ACCEPTED" not in changed.reason_histogram  # sanity
    assert _digest(changed) != _AP_A2_GOLDEN_DIGEST


def test_ap_a2_golden_digest_moves_on_additive_evidence_churn_not_only_dropped_fields():
    baseline = _run_ap_a2_fixture()
    assert _digest(baseline) == _AP_A2_GOLDEN_DIGEST

    original_evidence = wcmb_engine._evidence

    def _churned_evidence(**kwargs):
        evidence = original_evidence(**kwargs)
        evidence["_mutant_constant_marker"] = True
        return evidence

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    try:
        mp.setattr(wcmb_engine, "_evidence", _churned_evidence)
        churned = _run_ap_a2_fixture()
    finally:
        mp.undo()

    assert {r.symbol for r in churned.records} == {r.symbol for r in baseline.records}
    assert dict(churned.reason_histogram) == dict(baseline.reason_histogram)
    assert _digest(churned) != _AP_A2_GOLDEN_DIGEST
