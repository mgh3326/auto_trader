"""ROB-960 -- full-window per-config PBO evaluator wiring tests.

Pure unit tests only: small synthetic in-memory bar slices, no corpus load,
no network, no DB. These fixtures are an INJECTED UNIT FIXTURE SEAM ONLY
(captain Task-1-closure gate C3) -- they do not represent a production-
valid corpus contract; the production caller (rob960_empirical_orchestrator)
is responsible for proving full-window coverage via H1's pinned manifest +
rob941_offline_loader.load_corpus before ever calling this module.

Proves the wiring (H3 generators -> reused rob944_walkforward._run_scenario,
at COST_SCENARIO_PRIMARY_STRESS, fold_id=None throughout) satisfies
rob945_pbo_builder's EvaluateConfigCallback contract -- never a new metric,
never a local funding/engine reimplementation (G1), never a silent gap
default (G2/C1), never an unvalidated request provenance (C2), never a
promoted non-completed H4 outcome (C4).
"""

from __future__ import annotations

import dataclasses

import pytest
import rob941_frozen_scope as frozen
import rob960_pbo_evaluator as evaluator_mod
from rob940_bars_agg import Bar1m
from rob940_cost_model import COST_SCENARIO_PRIMARY_STRESS
from rob940_signal_manifest import FROZEN_S1_CONFIGS, FROZEN_S2_CONFIGS
from rob941_funding_sidecar import FundingSidecar
from rob944_walkforward import SignalEvent, _run_scenario
from rob945_pbo_builder import PboEvaluationRequest, Rob945PboBuilderError
from rob945_pbo_grid import (
    FROZEN_PBO_COST_BPS,
    FROZEN_PBO_SCENARIO_NAME,
    FROZEN_PBO_WINDOW_END_ISO,
    FROZEN_PBO_WINDOW_START_ISO,
)
from rob960_pbo_evaluator import (
    build_evaluate_config_callback,
    compute_pbo_evidence_for_strategy,
)


def _flat_bars_1m(count: int = 5) -> dict[str, tuple[Bar1m, ...]]:
    bars = tuple(
        Bar1m(
            ts=frozen.WINDOW_START_MS + i * 60_000,
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=1.0,
        )
        for i in range(count)
    )
    return dict.fromkeys(frozen.UNIVERSE, bars)


def _flat_funding_sidecars():
    return {symbol: FundingSidecar.from_rows(symbol, ()) for symbol in frozen.UNIVERSE}


def _empty_gap_ranges():
    return dict.fromkeys(frozen.UNIVERSE, ())


def _valid_request(strategy: str, config_id: str) -> PboEvaluationRequest:
    return PboEvaluationRequest(
        strategy=strategy,
        config_id=config_id,
        scenario_name=FROZEN_PBO_SCENARIO_NAME,
        cost_bps=FROZEN_PBO_COST_BPS,
        window_start_iso=FROZEN_PBO_WINDOW_START_ISO,
        window_end_iso=FROZEN_PBO_WINDOW_END_ISO,
        symbols=frozen.UNIVERSE,
    )


def test_zero_signal_config_returns_completed_response_with_empty_trades():
    config = FROZEN_S1_CONFIGS[0]
    evaluate = build_evaluate_config_callback(
        bars_1m=_flat_bars_1m(),
        funding_sidecars=_flat_funding_sidecars(),
        gap_ranges=_empty_gap_ranges(),
        strategy="S1",
    )
    response = evaluate(_valid_request("S1", config.config_id))
    assert response.strategy == "S1"
    assert response.config_id == config.config_id
    assert response.scenario_name == FROZEN_PBO_SCENARIO_NAME
    assert response.cost_bps == FROZEN_PBO_COST_BPS
    assert len(response.symbol_outcomes) == len(frozen.UNIVERSE)
    for outcome in response.symbol_outcomes:
        assert outcome.status == "completed"
        assert outcome.trades == ()
        for trade in outcome.trades:
            assert trade.fold_id is None


def test_s2_config_also_returns_completed_response_with_empty_trades():
    config = FROZEN_S2_CONFIGS[0]
    evaluate = build_evaluate_config_callback(
        bars_1m=_flat_bars_1m(),
        funding_sidecars=_flat_funding_sidecars(),
        gap_ranges=_empty_gap_ranges(),
        strategy="S2",
    )
    response = evaluate(_valid_request("S2", config.config_id))
    assert response.strategy == "S2"
    assert len(response.symbol_outcomes) == len(frozen.UNIVERSE)
    for outcome in response.symbol_outcomes:
        assert outcome.status == "completed"


def test_compute_pbo_evidence_for_strategy_returns_evidence_for_all_zero_grid():
    from rob945_pbo_grid import PboAuxiliaryEvidence

    evidence = compute_pbo_evidence_for_strategy(
        strategy="S1",
        bars_1m=_flat_bars_1m(),
        funding_sidecars=_flat_funding_sidecars(),
        gap_ranges=_empty_gap_ranges(),
    )
    assert isinstance(evidence, PboAuxiliaryEvidence)
    assert evidence.strategy == "S1"
    assert evidence.config_count == 12


# ---------------------------------------------------------------------------
# Captain plan-gate G1 focused RED/GREEN: rob944_walkforward._run_scenario's
# own ``fold_id`` parameter is annotated ``str`` (not ``str | None``) in its
# frozen source -- not runtime-enforced. This module deliberately calls it
# with the literal ``None`` for every PBO evaluation.
# ---------------------------------------------------------------------------


def _permissive_sidecar(symbol: str) -> FundingSidecar:
    from funding_oi_archive import FundingRow

    return FundingSidecar.from_rows(
        symbol,
        [
            FundingRow(
                calc_time=-10_000_000, funding_interval_hours=8, last_funding_rate=0.0
            )
        ],
    )


def _flat_bars(start_ms: int, end_ms: int) -> tuple[Bar1m, ...]:
    out = []
    ts = start_ms
    while ts < end_ms:
        out.append(
            Bar1m(ts=ts, open=100.0, high=100.0, low=100.0, close=100.0, volume=1.0)
        )
        ts += 60_000
    return tuple(out)


def _real_signal(
    symbol: str, signal_ts: int, *, config_id="S1-00", fold_id=None
) -> SignalEvent:
    return SignalEvent(
        strategy="S1",
        config_id=config_id,
        symbol=symbol,
        signal_ts=signal_ts,
        side="long",
        sl_distance_bps=200.0,
        tp_distance_bps=300.0,
        timeout_bars=1,
        cooldown_bars=0,
        fold_id=fold_id,
    )


def test_run_scenario_accepts_fold_id_none_at_runtime_despite_str_annotation():
    bars = _flat_bars(0, 4 * 60_000)
    sidecar = _permissive_sidecar("BTCUSDT")
    signal = _real_signal("BTCUSDT", 60_000)
    outcome, engine_result = _run_scenario(
        bars,
        (signal,),
        COST_SCENARIO_PRIMARY_STRESS,
        sidecar,
        (),  # no gap ranges
        strategy="S1",
        config_id="S1-00",
        symbol="BTCUSDT",
        fold_id=None,
    )
    assert outcome.status == "completed"
    assert engine_result is not None
    assert len(engine_result.trades) == 1
    trade = engine_result.trades[0]
    assert trade.fold_id is None
    assert trade.exit_reason in ("timeout", "take_profit", "stop_loss")


# ---------------------------------------------------------------------------
# Captain Task-1-closure gate C1: no silent gap-authority default.
# ---------------------------------------------------------------------------


def _never_call(*args, **kwargs):
    raise AssertionError("must not be called")


def test_missing_gap_ranges_key_fails_closed_with_zero_execution(monkeypatch):
    from rob944_walkforward import MissingSymbolDataError

    monkeypatch.setattr(evaluator_mod, "_run_scenario", _never_call)
    incomplete_gap_ranges = dict(_empty_gap_ranges())
    del incomplete_gap_ranges["BTCUSDT"]
    with pytest.raises(MissingSymbolDataError):
        build_evaluate_config_callback(
            bars_1m=_flat_bars_1m(),
            funding_sidecars=_flat_funding_sidecars(),
            gap_ranges=incomplete_gap_ranges,
            strategy="S1",
        )


def test_nonempty_gap_ranges_fails_closed_with_zero_execution(monkeypatch):
    monkeypatch.setattr(evaluator_mod, "_run_scenario", _never_call)
    nonempty_gap_ranges = dict(_empty_gap_ranges())
    nonempty_gap_ranges["BTCUSDT"] = ((100, 200),)
    with pytest.raises(Rob945PboBuilderError):
        build_evaluate_config_callback(
            bars_1m=_flat_bars_1m(),
            funding_sidecars=_flat_funding_sidecars(),
            gap_ranges=nonempty_gap_ranges,
            strategy="S1",
        )


# ---------------------------------------------------------------------------
# Captain Task-1-closure gate C2: request provenance pinned before execution.
# ---------------------------------------------------------------------------


def _build_valid_evaluator(strategy="S1"):
    return build_evaluate_config_callback(
        bars_1m=_flat_bars_1m(),
        funding_sidecars=_flat_funding_sidecars(),
        gap_ranges=_empty_gap_ranges(),
        strategy=strategy,
    )


@pytest.mark.parametrize(
    "override",
    [
        {"strategy": "S2"},
        {"config_id": "S1-99"},
        {"scenario_name": "base"},
        {"cost_bps": 13.0},
        {"window_start_iso": "2020-01-01T00:00:00Z"},
        {"window_end_iso": "2020-01-01T00:00:00Z"},
        {"symbols": tuple(reversed(frozen.UNIVERSE))},
    ],
)
def test_provenance_mismatch_fails_closed_with_zero_generator_calls(
    monkeypatch, override
):
    calls = []
    monkeypatch.setattr(
        evaluator_mod,
        "generate_s1_signals",
        lambda *a, **k: calls.append(1) or (),
    )
    evaluate = _build_valid_evaluator("S1")
    bad_request = dataclasses.replace(_valid_request("S1", "S1-00"), **override)
    with pytest.raises(Rob945PboBuilderError):
        evaluate(bad_request)
    assert calls == []


# ---------------------------------------------------------------------------
# Captain Task-1-closure gate C5 item 3: 24 canonical requests / 96
# independent per-symbol _run_scenario calls across both strategies.
# ---------------------------------------------------------------------------


def test_both_strategies_produce_24_requests_and_96_run_scenario_calls(monkeypatch):
    call_count = {"n": 0}
    real_run_scenario = evaluator_mod._run_scenario

    def _counting_run_scenario(*args, **kwargs):
        call_count["n"] += 1
        return real_run_scenario(*args, **kwargs)

    monkeypatch.setattr(evaluator_mod, "_run_scenario", _counting_run_scenario)

    from rob945_pbo_builder import build_pbo_daily_grid

    request_count = {"n": 0}
    for strategy in ("S1", "S2"):
        evaluate = build_evaluate_config_callback(
            bars_1m=_flat_bars_1m(),
            funding_sidecars=_flat_funding_sidecars(),
            gap_ranges=_empty_gap_ranges(),
            strategy=strategy,
        )

        def _counting_evaluate(request, _evaluate=evaluate):
            request_count["n"] += 1
            return _evaluate(request)

        build_pbo_daily_grid(strategy=strategy, evaluate_config=_counting_evaluate)

    assert request_count["n"] == 24  # 12 configs x 2 strategies
    assert call_count["n"] == 96  # 24 requests x 4 symbols


# ---------------------------------------------------------------------------
# Captain Task-1-closure gate C5 item 4: a real trade THROUGH the
# materializer's own build_evaluate_config_callback closure (not only the
# isolated direct _run_scenario test above) -- generator is monkeypatched
# to return one real, hand-built SignalEvent (the S1 aggregation-to-signal
# numerics are H3's own already-tested responsibility, out of scope to
# duplicate here); everything downstream (funding gate, ordering, engine)
# runs FOR REAL, unmocked.
# ---------------------------------------------------------------------------


def test_real_trade_through_evaluate_closure_has_fold_id_none(monkeypatch):
    def _fake_generate_s1_signals(bars_15m, config, *, symbol, fold_id):
        if symbol != "BTCUSDT":
            return ()
        return (_real_signal(symbol, frozen.WINDOW_START_MS + 60_000, fold_id=fold_id),)

    monkeypatch.setattr(evaluator_mod, "generate_s1_signals", _fake_generate_s1_signals)

    bars_1m = {
        symbol: _flat_bars(frozen.WINDOW_START_MS, frozen.WINDOW_START_MS + 4 * 60_000)
        for symbol in frozen.UNIVERSE
    }
    funding_sidecars = {
        symbol: _permissive_sidecar(symbol) for symbol in frozen.UNIVERSE
    }
    evaluate = build_evaluate_config_callback(
        bars_1m=bars_1m,
        funding_sidecars=funding_sidecars,
        gap_ranges=_empty_gap_ranges(),
        strategy="S1",
    )
    response = evaluate(_valid_request("S1", "S1-00"))
    btc_outcome = next(o for o in response.symbol_outcomes if o.symbol == "BTCUSDT")
    assert btc_outcome.status == "completed"
    assert len(btc_outcome.trades) == 1
    trade = btc_outcome.trades[0]
    assert trade.fold_id is None
    assert trade.config_id == "S1-00"


# ---------------------------------------------------------------------------
# Captain Task-1-closure gate C5 item 5: S2 generated rejections flow
# through the existing run_rob944_campaign._s2_rejections_to_no_trade_records
# into _run_scenario's own pre_execution_rejections -- proven by capturing
# the real _run_scenario call's kwargs.
# ---------------------------------------------------------------------------


def test_s2_rejections_flow_through_to_pre_execution_rejections(monkeypatch):
    from rob940_signal_s2 import RejectedCandidate, S2GenerationResult

    captured_calls = []
    real_run_scenario = evaluator_mod._run_scenario

    def _capturing_run_scenario(*args, **kwargs):
        captured_calls.append(kwargs)
        return real_run_scenario(*args, **kwargs)

    monkeypatch.setattr(evaluator_mod, "_run_scenario", _capturing_run_scenario)

    def _fake_generate_s2_signals(bars_5m, bars_1m, config, *, symbol, fold_id):
        if symbol != "BTCUSDT":
            return S2GenerationResult(signals=(), rejections=())
        rejection = RejectedCandidate(
            strategy="S2",
            config_id=config.config_id,
            symbol=symbol,
            side="long",
            signal_ts=frozen.WINDOW_START_MS,
            reason="target_direction_invalid",
            fold_id=fold_id,
        )
        return S2GenerationResult(signals=(), rejections=(rejection,))

    monkeypatch.setattr(evaluator_mod, "generate_s2_signals", _fake_generate_s2_signals)

    bars_1m = {
        symbol: _flat_bars(frozen.WINDOW_START_MS, frozen.WINDOW_START_MS + 5 * 60_000)
        for symbol in frozen.UNIVERSE
    }
    funding_sidecars = {
        symbol: _permissive_sidecar(symbol) for symbol in frozen.UNIVERSE
    }
    evaluate = build_evaluate_config_callback(
        bars_1m=bars_1m,
        funding_sidecars=funding_sidecars,
        gap_ranges=_empty_gap_ranges(),
        strategy="S2",
    )
    evaluate(_valid_request("S2", "S2-00"))
    btc_call = next(c for c in captured_calls if c["symbol"] == "BTCUSDT")
    assert len(btc_call["pre_execution_rejections"]) == 1
    converted = btc_call["pre_execution_rejections"][0]
    assert converted.reason == "target_direction_invalid"
    assert converted.strategy == "S2"
    assert converted.symbol == "BTCUSDT"


# ---------------------------------------------------------------------------
# Captain Task-1-closure gate C4: a non-completed H4 terminal outcome is
# NEVER promoted into an H5 SymbolOutcome.
# ---------------------------------------------------------------------------


def test_noncompleted_h4_outcome_is_never_promoted_to_h5_response(monkeypatch):
    from rob944_scenario_evidence import ScenarioRunOutcome

    def _fake_run_scenario(*args, **kwargs):
        return (
            ScenarioRunOutcome(
                scenario_name="primary_stress",
                status="crashed",
                trade_count=0,
                artifact_hash="0" * 64,
                error_reason="child_execution_crashed",
                no_trade_reason_counts={},
            ),
            None,
        )

    monkeypatch.setattr(evaluator_mod, "_run_scenario", _fake_run_scenario)
    evaluate = _build_valid_evaluator("S1")
    with pytest.raises(Rob945PboBuilderError):
        evaluate(_valid_request("S1", "S1-00"))
