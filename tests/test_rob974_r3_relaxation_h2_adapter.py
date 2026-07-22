from __future__ import annotations

import math
from dataclasses import replace

import pytest
from rob974_h2_dtos import (
    S3EngineResult,
    S3IncompleteRecord,
    S3NoTradeRecord,
    S3SignalIntent,
    S3Trade,
    S4PairTrade,
)
from rob974_h3_h2_adapter import adapt_s3_candidate
from rob974_h3_s3 import S3Candidate
from rob974_h3_s4 import HISTORICAL_NOTIONAL_ASSUMPTION, S4Candidate
from rob974_h4_adapter import (
    H4ContractDrift,
    SealedS3Terminal,
    seal_s3_engine_input,
    seal_s3_engine_output,
)
from rob974_h4_contracts import exact_h4_folds
from rob974_r3_evidence_context import issue_r3_production_evidence_context
from rob974_r3_h3_adapter import adapt_r3_s4_candidate_for_execution
from rob974_r3_h4_s4_adapter import (
    SealedR3S4Terminal,
    seal_r3_s4_engine_input,
    seal_r3_s4_engine_output,
)
from rob974_r3_manifest import FROZEN_R3_ROSTER, R3S3Config, R3S4Config
from rob974_r3_plan import build_production_r3_plan
from rob974_r3_relaxation import (
    PhaseLedgerEvidence,
    RelaxationInputError,
    analyze_relaxation_campaign,
)
from rob974_r3_relaxation_h2_adapter import (
    R3H2CellFoldInput,
    normalize_r3_phase_ledgers,
    normalize_r3_s3_trade,
    normalize_r3_s4_trade,
)
from rob974_r3_s4_dtos import (
    R3S4EngineResult,
    R3S4PairSignalIntent,
    R3S4PairTrade,
)


class _S3TradeSubclass(S3Trade):
    pass


def _s3_trade(config: R3S3Config, fold_id: str, signal_ts: int) -> S3Trade:
    return S3Trade(
        symbol="XRPUSDT",
        side="long",
        config_id=config.config_id,
        fold_id=fold_id,
        signal_ts=signal_ts,
        entry_ts=signal_ts + 60_000,
        entry_price=1.0,
        exit_ts=signal_ts + 120_000,
        exit_price=1.01,
        exit_reason="TP",
        mfe_bps=20.0,
        mae_bps=-5.0,
        gross_bps=10.0,
        volatility_percentile=47.5,
    )


def _s4_trade(
    config: R3S4Config,
    fold_id: str,
    signal_ts: int,
    **overrides: object,
) -> R3S4PairTrade:
    values: dict[str, object] = {
        "pair": ("XRPUSDT", "DOGEUSDT"),
        "side_a": "short",
        "side_b": "long",
        "config_id": config.config_id,
        "fold_id": fold_id,
        "signal_ts": signal_ts,
        "entry_ts": signal_ts + 60_000,
        "weight_a": 0.5,
        "weight_b": 0.5,
        "beta_a": 1.2,
        "beta_b": 0.8,
        "mu": 0.01,
        "sigma": 0.05,
        "observed_z": config.z_entry,
        "z_threshold": config.z_entry,
        "gross_notional": 12.0,
        "entry_price_a": 1.0,
        "entry_price_b": 2.0,
        "exit_ts": signal_ts + 120_000,
        "exit_price_a": 0.99,
        "exit_price_b": 2.02,
        "exit_reason": "MEAN_EXIT",
        "mfe_bps": 30.0,
        "mae_bps": -7.0,
        "gross_bps": 15.0,
        "order_id_a": None,
        "order_id_b": None,
        "pair_exec_status": "historical_atomic_assumption",
        "pair_executor_validated": False,
        "demo_eligible": False,
        "volatility_percentile": None,
        "volatility_percentile_provenance": "not_defined_for_s4",
    }
    values.update(overrides)
    return R3S4PairTrade(**values)  # type: ignore[arg-type]


def _s3_candidate(
    config: R3S3Config, signal_ts: int, *, symbol: str = "XRPUSDT"
) -> S3Candidate:
    return S3Candidate(
        strategy="S3",
        config_id=config.config_id,
        decision_ts=signal_ts,
        symbol=symbol,
        side="long",
        R=0.03,
        S=1.8,
        ER=0.5,
        Q=0.6,
        A=0.01,
        atr20=0.01,
        close=1.0,
        vwap12=0.99,
        vwap24=0.98,
        market_return_24h=0.02,
        current_market_return_4h=-0.4,
        volatility_percentile=47.5,
        volatility_percentile_provenance="h1_percentile_30d",
        range24=0.10,
        d_SL=0.01,
        d_TP=0.015,
        entry_tick_ts=signal_ts,
        entry_deadline_ts=signal_ts + 60_000,
        max_hold_4h_bars=12,
    )


def _s4_candidate(config: R3S4Config, signal_ts: int) -> S4Candidate:
    return S4Candidate(
        strategy="S4",
        config_id=config.config_id,
        decision_ts=signal_ts,
        pair="XRP-DOGE",
        side="short_a_long_b",
        symbol_a="XRPUSDT",
        symbol_b="DOGEUSDT",
        side_a="short",
        side_b="long",
        beta_a=1.2,
        beta_b=0.8,
        weight_a=0.5,
        weight_b=0.5,
        mu=0.01,
        mad=0.03,
        effective_mad_scale=0.05,
        observed_z=config.z_entry,
        prior_observed_z=2.0 * config.z_entry,
        D_fraction=0.02,
        D_bps=200.0,
        rho=0.72,
        half_life_4h_bars=4.0,
        beta_stability=0.10,
        sigma_pair_risk=0.01,
        observed_pair_return_fraction=0.02,
        gross_notional_usd=12.0,
        notional_a_usd=6.0,
        notional_b_usd=6.0,
        d_SL=0.01,
        d_TP=0.015,
        historical_notional_assumption=HISTORICAL_NOTIONAL_ASSUMPTION,
        historical_eligibility=True,
        historical_eligibility_authority=(
            "rob974_h1_parent_manifest_selected_universe"
        ),
        volatility_percentile=None,
        volatility_percentile_provenance="not_defined_for_s4",
        entry_tick_ts=signal_ts,
        entry_deadline_ts=signal_ts + 60_000,
        max_hold_4h_bars=9,
        leg_a_order_id=None,
        leg_b_order_id=None,
        leg_a_fill_id=None,
        leg_b_fill_id=None,
        pair_executor_provenance="not_evaluated_h3_generator",
    )


def _sealed_s3(
    result: S3EngineResult,
    intents: tuple[S3SignalIntent, ...],
    *,
    corpus_end_ts: int,
    horizon_end_ts: int | None,
) -> SealedS3Terminal:
    return SealedS3Terminal(
        result,
        seal_s3_engine_input(
            intents,
            corpus_end_ts=corpus_end_ts,
            horizon_end_ts=horizon_end_ts,
        ),
        seal_s3_engine_output(result),
    )


def _sealed_s4(
    result: R3S4EngineResult,
    intents: tuple[R3S4PairSignalIntent, ...],
    *,
    corpus_end_ts: int,
    horizon_end_ts: int | None,
) -> SealedR3S4Terminal:
    return SealedR3S4Terminal(
        result,
        seal_r3_s4_engine_input(
            intents,
            corpus_end_ts=corpus_end_ts,
            horizon_end_ts=horizon_end_ts,
        ),
        seal_r3_s4_engine_output(result),
    )


def _source(
    config: R3S3Config | R3S4Config,
    fold_id: str,
    trades: tuple[S3Trade, ...] | tuple[R3S4PairTrade, ...],
) -> R3H2CellFoldInput:
    fold = next(row for row in exact_h4_folds() if row.fold_id == fold_id)
    signal_ts = trades[0].signal_ts
    h3_candidates: tuple[S3Candidate, ...] | tuple[S4Candidate, ...]
    engine_intents: tuple[S3SignalIntent, ...] | tuple[R3S4PairSignalIntent, ...]
    if type(config) is R3S3Config:
        candidate = _s3_candidate(config, signal_ts)
        intent = adapt_s3_candidate(candidate, fold_id=fold_id)
        h3_candidates = (candidate,)
        engine_intents = (intent,)
        result = S3EngineResult(trades, (), ())
        terminal = _sealed_s3(
            result,
            engine_intents,
            corpus_end_ts=fold.oos_end_ms,
            horizon_end_ts=fold.oos_end_ms,
        )
    else:
        candidate = _s4_candidate(config, signal_ts)
        intent = adapt_r3_s4_candidate_for_execution(candidate, fold_id=fold_id)
        h3_candidates = (candidate,)
        engine_intents = (intent,)
        result = R3S4EngineResult(trades, (), ())
        terminal = _sealed_s4(
            result,
            engine_intents,
            corpus_end_ts=fold.oos_end_ms,
            horizon_end_ts=fold.oos_end_ms,
        )
    return R3H2CellFoldInput(
        config=config,
        fold_id=fold_id,
        h3_candidates=h3_candidates,
        engine_intents=engine_intents,
        corpus_end_ts=fold.oos_end_ms,
        horizon_end_ts=fold.oos_end_ms,
        terminal=terminal,
    )


def _oos_sources() -> tuple[R3H2CellFoldInput, ...]:
    rows: list[R3H2CellFoldInput] = []
    for config in FROZEN_R3_ROSTER:
        for fold in exact_h4_folds():
            signal_ts = fold.oos_start_ms + 60_000
            trades = (
                (_s3_trade(config, fold.fold_id, signal_ts),)
                if type(config) is R3S3Config
                else (_s4_trade(config, fold.fold_id, signal_ts),)
            )
            rows.append(_source(config, fold.fold_id, trades))
    return tuple(rows)


def _multi_s3_source() -> R3H2CellFoldInput:
    config = FROZEN_R3_ROSTER[0]
    assert type(config) is R3S3Config
    fold = exact_h4_folds()[0]
    candidates = (
        _s3_candidate(config, fold.oos_start_ms + 60_000, symbol="XRPUSDT"),
        _s3_candidate(config, fold.oos_start_ms + 120_000, symbol="DOGEUSDT"),
    )
    intents = tuple(
        adapt_s3_candidate(candidate, fold_id=fold.fold_id) for candidate in candidates
    )
    result = S3EngineResult(
        (),
        tuple(
            S3NoTradeRecord(
                symbol=candidate.symbol,
                side=candidate.side,
                config_id=config.config_id,
                fold_id=fold.fold_id,
                signal_ts=candidate.signal_ts,
                reason="next_tick_unavailable",
            )
            for candidate in candidates
        ),
        (),
    )
    terminal = _sealed_s3(
        result,
        intents,
        corpus_end_ts=fold.oos_end_ms,
        horizon_end_ts=fold.oos_end_ms,
    )
    return R3H2CellFoldInput(
        config=config,
        fold_id=fold.fold_id,
        h3_candidates=candidates,
        engine_intents=intents,
        corpus_end_ts=fold.oos_end_ms,
        horizon_end_ts=fold.oos_end_ms,
        terminal=terminal,
    )


def _replace_terminal_result(
    source: R3H2CellFoldInput, result: S3EngineResult | R3S4EngineResult
) -> R3H2CellFoldInput:
    terminal = replace(
        source.terminal,
        result=result,
        output_seal_sha256=(
            seal_s3_engine_output(result)
            if type(result) is S3EngineResult
            else seal_r3_s4_engine_output(result)
        ),
    )
    return replace(source, terminal=terminal)


def _replace_s3_authority(
    source: R3H2CellFoldInput,
    candidates: tuple[S3Candidate, ...],
    result: S3EngineResult,
) -> R3H2CellFoldInput:
    intents = tuple(
        adapt_s3_candidate(candidate, fold_id=source.fold_id)
        for candidate in candidates
    )
    terminal = _sealed_s3(
        result,
        intents,
        corpus_end_ts=source.corpus_end_ts,
        horizon_end_ts=source.horizon_end_ts,
    )
    return replace(
        source,
        h3_candidates=candidates,
        engine_intents=intents,
        terminal=terminal,
    )


def test_exact_manifest_major_12x8_builder_normalizes_sealed_h4_terminals() -> None:
    evidence = normalize_r3_phase_ledgers(phase="OOS", sources=_oos_sources())
    expected_headers = tuple(
        (config.config_id, fold.fold_id)
        for config in FROZEN_R3_ROSTER
        for fold in exact_h4_folds()
    )
    assert type(evidence) is PhaseLedgerEvidence
    assert evidence.phase == "OOS"
    assert evidence.operational_status == "COMPLETE"
    assert evidence.terminal_incompletes == ()
    assert len(evidence.ledgers) == 12 * 8 == 96
    assert (
        tuple((row.config_id, row.fold_id) for row in evidence.ledgers)
        == expected_headers
    )

    s3 = evidence.ledgers[0].trades[0]
    assert s3.execution.volatility_percentile == 47.5
    assert s3.execution.beta_a is None
    s4 = evidence.ledgers[3 * 8].trades[0]
    assert s4.event.direction == "short_a_long_b"
    assert s4.execution.leg_sides == ("short", "long")
    assert s4.execution.gross_notional == 12.0
    assert (
        s4.execution.beta_a,
        s4.execution.beta_b,
        s4.execution.spread_mu,
        s4.execution.spread_sigma,
        s4.execution.observed_z,
    ) == (1.2, 0.8, 0.01, 0.05, 1.1)


def test_config_lineage_is_removed_but_equal_economics_remain_equal() -> None:
    fold = exact_h4_folds()[0]
    signal_ts = fold.oos_start_ms + 60_000
    strict = FROZEN_R3_ROSTER[0]
    loose = FROZEN_R3_ROSTER[2]
    assert type(strict) is type(loose) is R3S3Config
    strict_trade = _s3_trade(strict, fold.fold_id, signal_ts)
    loose_trade = replace(strict_trade, config_id=loose.config_id)
    assert normalize_r3_s3_trade(
        trade=strict_trade, config=strict, fold_id=fold.fold_id
    ) == normalize_r3_s3_trade(trade=loose_trade, config=loose, fold_id=fold.fold_id)


@pytest.mark.parametrize("percentile", (None, -0.1, 100.1))
def test_s3_requires_real_h3_volatility_percentile(
    percentile: float | None,
) -> None:
    config = FROZEN_R3_ROSTER[0]
    assert type(config) is R3S3Config
    fold = exact_h4_folds()[0]
    trade = _s3_trade(config, fold.fold_id, fold.oos_start_ms + 60_000)
    with pytest.raises(RelaxationInputError, match=r"H3 value in \[0,100\]"):
        normalize_r3_s3_trade(
            trade=replace(trade, volatility_percentile=percentile),
            config=config,
            fold_id=fold.fold_id,
        )


def test_adapter_rejects_raw_prefix_wrong_family_lineage_order_and_phase() -> None:
    sources = _oos_sources()
    with pytest.raises(TypeError, match="unexpected keyword"):
        R3H2CellFoldInput(  # type: ignore[call-arg]
            config=sources[0].config,
            fold_id=sources[0].fold_id,
            basket_trade_count=1,
            trades=(sources[0].terminal.result.trades[0],),
        )
    with pytest.raises(TypeError, match="exact built-in tuple"):
        normalize_r3_phase_ledgers(phase="OOS", sources=list(sources))
    with pytest.raises(RelaxationInputError, match="manifest-major 12x8"):
        normalize_r3_phase_ledgers(
            phase="OOS", sources=(sources[1], sources[0], *sources[2:])
        )
    with pytest.raises(ValueError, match="TRAIN or OOS"):
        normalize_r3_phase_ledgers(phase="oos", sources=sources)

    s3_source = sources[0]
    s4_source = sources[3 * 8]
    with pytest.raises(TypeError, match="S3 cell requires exact"):
        replace(s3_source, terminal=s4_source.terminal)

    s3_trade = s3_source.terminal.result.trades[0]
    assert type(s3_trade) is S3Trade
    with pytest.raises(RelaxationInputError, match="config_id"):
        normalize_r3_s3_trade(
            trade=replace(s3_trade, config_id="S3-R3-02"),
            config=s3_source.config,
            fold_id=s3_source.fold_id,
        )
    with pytest.raises(TypeError, match="exact H2 S3Trade"):
        normalize_r3_s3_trade(
            trade=s4_source.terminal.result.trades[0],
            config=s3_source.config,
            fold_id=s3_source.fold_id,
        )

    subclass = _S3TradeSubclass(**s3_trade.__dict__)
    bad_result = S3EngineResult((subclass,), (), ())
    with pytest.raises(TypeError, match="exact S3Trade tuple"):
        _replace_terminal_result(s3_source, bad_result)


def test_trade_phase_exit_horizon_is_inclusive_and_one_ms_over_rejected() -> None:
    sources = _oos_sources()
    source = sources[0]
    trade = source.terminal.result.trades[0]
    assert type(trade) is S3Trade
    fold = exact_h4_folds()[0]

    exact_end = replace(trade, exit_ts=fold.oos_end_ms)
    exact_source = _replace_terminal_result(
        source, S3EngineResult((exact_end,), (), ())
    )
    exact_evidence = normalize_r3_phase_ledgers(
        phase="OOS", sources=(exact_source, *sources[1:])
    )
    assert exact_evidence.ledgers[0].trades[0].execution.exit_ts == fold.oos_end_ms

    after_end = replace(trade, exit_ts=fold.oos_end_ms + 1)
    after_source = _replace_terminal_result(
        source, S3EngineResult((after_end,), (), ())
    )
    with pytest.raises(RelaxationInputError, match="outside the exact OOS"):
        normalize_r3_phase_ledgers(phase="OOS", sources=(after_source, *sources[1:]))

    bad_no_trade = S3NoTradeRecord(
        symbol="XRPUSDT",
        side="long",
        config_id=source.config.config_id,
        fold_id=source.fold_id,
        signal_ts=0,
        reason="next_tick_unavailable",
    )
    assert type(source.config) is R3S3Config
    bad_no_trade_source = _replace_s3_authority(
        source,
        (_s3_candidate(source.config, 0),),
        S3EngineResult((), (bad_no_trade,), ()),
    )
    with pytest.raises(RelaxationInputError, match="no-trade signal"):
        normalize_r3_phase_ledgers(
            phase="OOS", sources=(bad_no_trade_source, *sources[1:])
        )


def test_s4_pair_order_low_z_and_frozen_g_tolerance_are_fail_closed() -> None:
    config = FROZEN_R3_ROSTER[3]
    assert type(config) is R3S4Config
    fold = exact_h4_folds()[0]
    trade = _s4_trade(config, fold.fold_id, fold.oos_start_ms + 60_000)
    with pytest.raises(RelaxationInputError, match="canonical H3 pair order"):
        normalize_r3_s4_trade(
            trade=replace(trade, pair=("DOGEUSDT", "XRPUSDT")),
            config=config,
            fold_id=fold.fold_id,
        )

    within = replace(trade, gross_notional=12.0 + 0.5e-9)
    normalized = normalize_r3_s4_trade(
        trade=within, config=config, fold_id=fold.fold_id
    )
    assert normalized.execution.gross_notional == within.gross_notional
    over = replace(trade, gross_notional=12.0 + 1.1e-9)
    with pytest.raises(RelaxationInputError, match="deterministic G_min"):
        normalize_r3_s4_trade(trade=over, config=config, fold_id=fold.fold_id)

    low_config = FROZEN_R3_ROSTER[8]
    assert type(low_config) is R3S4Config and low_config.z_entry == 0.8
    low_trade = _s4_trade(
        low_config,
        fold.fold_id,
        fold.oos_start_ms + 60_000,
        observed_z=0.8,
    )
    low = normalize_r3_s4_trade(
        trade=low_trade, config=low_config, fold_id=fold.fold_id
    )
    assert low.execution.observed_z == 0.8


def test_sealed_terminal_output_hash_is_recomputed_at_m3_ingress() -> None:
    source = _oos_sources()[3 * 8]
    assert type(source.terminal) is SealedR3S4Terminal
    forged = replace(source.terminal, output_seal_sha256="0" * 64)
    with pytest.raises(RelaxationInputError, match="output hash"):
        replace(source, terminal=forged)


def test_input_authority_rejects_alternate_valid_input_seal() -> None:
    source = _oos_sources()[3 * 8]
    assert type(source.terminal) is SealedR3S4Terminal
    alternate = "f" * 64
    assert alternate != source.terminal.input_seal_sha256
    forged = replace(source.terminal, input_seal_sha256=alternate)
    with pytest.raises(RelaxationInputError, match="input hash"):
        replace(source, terminal=forged)


def test_input_seal_failure_precedes_output_and_ledger_evidence() -> None:
    source = _oos_sources()[3 * 8]
    assert type(source.terminal) is SealedR3S4Terminal
    forged = replace(
        source.terminal,
        input_seal_sha256="f" * 64,
        output_seal_sha256="0" * 64,
    )
    with pytest.raises(RelaxationInputError, match="input hash"):
        replace(source, terminal=forged)


def test_input_authority_rejects_missing_reordered_and_duplicate_h3_candidates() -> (
    None
):
    source = _multi_s3_source()
    first_candidate, second_candidate = source.h3_candidates
    first_intent, second_intent = source.engine_intents

    with pytest.raises(RelaxationInputError, match="derived engine intents"):
        replace(source, h3_candidates=(first_candidate,))
    with pytest.raises(RelaxationInputError, match="input hash"):
        replace(
            source,
            h3_candidates=(second_candidate, first_candidate),
            engine_intents=(second_intent, first_intent),
        )
    with pytest.raises(RelaxationInputError, match="duplicate H3 candidate"):
        replace(
            source,
            h3_candidates=(first_candidate, first_candidate),
            engine_intents=(first_intent, first_intent),
        )


def test_input_authority_rejects_changed_config_fold_corpus_and_horizon() -> None:
    source = _multi_s3_source()
    other_config = FROZEN_R3_ROSTER[1]
    assert type(other_config) is R3S3Config
    other_fold = exact_h4_folds()[1]

    with pytest.raises(RelaxationInputError, match="candidate config_id"):
        replace(source, config=other_config)
    with pytest.raises(RelaxationInputError, match="derived engine intents"):
        replace(source, fold_id=other_fold.fold_id)
    with pytest.raises(RelaxationInputError, match="input hash"):
        replace(source, corpus_end_ts=source.corpus_end_ts + 1)
    assert source.horizon_end_ts is not None
    with pytest.raises(RelaxationInputError, match="input hash"):
        replace(source, horizon_end_ts=source.horizon_end_ts + 1)


def test_input_authority_rejects_nonexact_intent_type_and_order() -> None:
    source = _multi_s3_source()
    first_intent, second_intent = source.engine_intents
    with pytest.raises(TypeError, match="exact built-in tuple"):
        replace(source, engine_intents=list(source.engine_intents))
    with pytest.raises(RelaxationInputError, match="derived engine intents"):
        replace(source, engine_intents=(second_intent, first_intent))


def test_exact_empty_input_seal_cannot_authorize_unknown_output_identity() -> None:
    source = _oos_sources()[0]
    assert type(source.terminal.result) is S3EngineResult
    forged_terminal = _sealed_s3(
        source.terminal.result,
        (),
        corpus_end_ts=source.corpus_end_ts,
        horizon_end_ts=source.horizon_end_ts,
    )
    with pytest.raises(H4ContractDrift, match="unknown candidate identity"):
        replace(
            source,
            h3_candidates=(),
            engine_intents=(),
            terminal=forged_terminal,
        )


def test_terminal_incomplete_preserves_prefix_but_suppresses_all_phase_statistics() -> (
    None
):
    sources = _oos_sources()
    source = sources[0]
    config = source.config
    assert type(config) is R3S3Config
    fold = exact_h4_folds()[0]
    prefix_trade = source.terminal.result.trades[0]
    assert type(prefix_trade) is S3Trade
    incomplete = S3IncompleteRecord(
        symbol="DOGEUSDT",
        side="long",
        config_id=config.config_id,
        fold_id=fold.fold_id,
        signal_ts=prefix_trade.signal_ts + 4 * 60_000,
        entry_ts=prefix_trade.signal_ts + 5 * 60_000,
        entry_price=1.0,
        reason="data_gap_in_position",
    )
    original_candidate = source.h3_candidates[0]
    assert type(original_candidate) is S3Candidate
    incomplete_candidate = _s3_candidate(
        config, incomplete.signal_ts, symbol=incomplete.symbol
    )
    incomplete_source = _replace_s3_authority(
        source,
        (original_candidate, incomplete_candidate),
        S3EngineResult((prefix_trade,), (), (incomplete,)),
    )
    evidence = normalize_r3_phase_ledgers(
        phase="OOS", sources=(incomplete_source, *sources[1:])
    )

    assert evidence.operational_status == "INCOMPLETE"
    assert evidence.ledgers[0].basket_trade_count == 1
    assert len(evidence.ledgers[0].trades) == 1
    assert len(evidence.terminal_incompletes) == 1
    assert evidence.terminal_incompletes[0].reason == "data_gap_in_position"

    context = issue_r3_production_evidence_context(build_production_r3_plan())
    analysis = analyze_relaxation_campaign(
        evidence_context=context,
        oos_evidence=evidence,
    )
    assert analysis.operational_status == "INCOMPLETE"
    assert analysis.oos.statistics_computed is False
    assert analysis.oos.rays == ()
    assert any(
        "data_gap_in_position" in reason for reason in analysis.incomplete_reasons
    )


def test_s4_nextafter_threshold_guard_remains_exact_at_m3_boundary() -> None:
    config = FROZEN_R3_ROSTER[-1]
    assert type(config) is R3S4Config and config.z_entry == 0.6
    fold = exact_h4_folds()[0]
    exact = _s4_trade(config, fold.fold_id, fold.oos_start_ms + 60_000)
    assert (
        normalize_r3_s4_trade(
            trade=exact, config=config, fold_id=fold.fold_id
        ).execution.observed_z
        == 0.6
    )
    with pytest.raises(ValueError, match="below its registered R3 threshold"):
        replace(exact, observed_z=math.nextafter(0.6, 0.0))


def test_frozen_r2_s4_trade_is_not_accepted_by_r3_m3_boundary() -> None:
    config = FROZEN_R3_ROSTER[3]
    assert type(config) is R3S4Config
    fold = exact_h4_folds()[0]
    with pytest.raises(TypeError, match="exact R3S4PairTrade"):
        normalize_r3_s4_trade(
            trade=object.__new__(S4PairTrade),
            config=config,
            fold_id=fold.fold_id,
        )
