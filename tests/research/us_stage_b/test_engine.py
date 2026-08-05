from __future__ import annotations

import json
from datetime import date, timedelta

import research.us_stage_b.engine as engine_module
from research.us_stage_b.engine import (
    CohortComparison,
    rank_signal_observations,
    run_us_stage_b,
)
from research.us_stage_b.registry import US_CANDIDATE_ORDER
from research.us_stage_b.signals import SignalObservation, tie_break_digest
from research.us_stage_b.source import InMemoryUSBarSource, USStageBDailyBar

from .conftest import candidate, contract, sequential_sessions, volbreak_bars


def _observation(
    *,
    strategy_id: str,
    contract_hash: str,
    labels: tuple[str, ...],
    symbol: str,
    session: date,
    adv: float,
    z: float,
) -> SignalObservation:
    return SignalObservation(
        strategy_id=strategy_id,
        contract_hash=contract_hash,
        labels=labels,
        symbol=symbol,
        session_date=session,
        universe_eligible=True,
        technical_signal=True,
        no_active_position=True,
        signal=True,
        exclusion_reason=None,
        adv20_pre_proxy=adv,
        tie_break_sha256=tie_break_digest(strategy_id, session, symbol).hex(),
        metrics={"z126": z},
        stages={"signal": True},
    )


def test_rank_is_adv_only_then_exact_sha_byte_tie_break(registry) -> None:
    binding = candidate(registry, US_CANDIDATE_ORDER[0])
    session = date(2024, 3, 1)
    observations = (
        _observation(
            strategy_id=binding.strategy_id,
            contract_hash=binding.contract_hash,
            labels=binding.labels,
            symbol="LOW_HIGH_Z",
            session=session,
            adv=5_000_001.0,
            z=100.0,
        ),
        _observation(
            strategy_id=binding.strategy_id,
            contract_hash=binding.contract_hash,
            labels=binding.labels,
            symbol="HIGH_LOW_Z",
            session=session,
            adv=5_000_002.0,
            z=1.0,
        ),
    )
    assert [item.symbol for item in rank_signal_observations(observations)] == [
        "HIGH_LOW_Z",
        "LOW_HIGH_Z",
    ]

    equal_adv = tuple(
        _observation(
            strategy_id=binding.strategy_id,
            contract_hash=binding.contract_hash,
            labels=binding.labels,
            symbol=symbol,
            session=session,
            adv=5_000_001.0,
            z=1.0,
        )
        for symbol in ("ZZZ", "AAA", "MID")
    )
    assert [item.symbol for item in rank_signal_observations(equal_adv)] == [
        item.symbol
        for item in sorted(
            equal_adv, key=lambda item: bytes.fromhex(item.tie_break_sha256)
        )
    ]


def test_entry_and_maturity_follow_corpus_session_index_not_calendar_days(
    registry,
) -> None:
    first = tuple(date(2023, 1, 3) + timedelta(days=index) for index in range(56))
    second = tuple(date(2023, 7, 1) + timedelta(days=2 * index) for index in range(14))
    sessions = (*first, *second)
    binding = candidate(registry, US_CANDIDATE_ORDER[2])
    result = run_us_stage_b(
        source=InMemoryUSBarSource(volbreak_bars("IDX", sessions)),
        contract=contract(binding, sessions),
        corpus_sessions=sessions,
    )

    outcome = next(
        item
        for item in result.outcomes
        if item.symbol == "IDX" and item.signal_session == sessions[55]
    )
    assert outcome.status == "completed"
    assert outcome.entry_session == sessions[56]
    assert outcome.exit_session == sessions[66]
    assert outcome.entry_session != outcome.signal_session + timedelta(days=1)
    assert outcome.exit_session != outcome.entry_session + timedelta(days=10)
    assert result.access_summary["outside_boundary_reads"] == 0


def test_missing_selected_maturity_close_invalidates_the_whole_run(registry) -> None:
    sessions = sequential_sessions(70)
    binding = candidate(registry, US_CANDIDATE_ORDER[2])
    result = run_us_stage_b(
        source=InMemoryUSBarSource(volbreak_bars("MISS", sessions, omit_exit=True)),
        contract=contract(binding, sessions),
        corpus_sessions=sessions,
    )

    assert result.run_invalid is True
    assert result.verdict.state == "RUN_INVALID"
    assert [item.status for item in result.outcomes] == ["run_invalid_missing_exit"]
    assert result.invalid_reasons[0].startswith("RUN_INVALID_MISSING_EXIT:MISS:")
    assert "missing_exit" not in result.to_dict()["outcome_status_counts"]


def test_terminal_maturity_without_a_close_invalidates_the_whole_run(registry) -> None:
    """A selected terminal hold is not KR-style censoring or exclusion."""

    sessions = sequential_sessions(57)
    binding = candidate(registry, US_CANDIDATE_ORDER[2])
    result = run_us_stage_b(
        source=InMemoryUSBarSource(volbreak_bars("TERMINAL", sessions)),
        contract=contract(binding, sessions),
        corpus_sessions=sessions,
    )

    assert result.run_invalid is True
    assert result.verdict.state == "RUN_INVALID"
    assert len(result.outcomes) == 1
    outcome = result.outcomes[0]
    assert outcome.status == "run_invalid_missing_exit"
    assert outcome.entry_session == sessions[56]
    assert outcome.exit_session is None
    assert result.invalid_reasons == (
        "RUN_INVALID_MISSING_EXIT_TERMINAL:"
        "TERMINAL:entry=2023-02-28:required_exit_session_index=66",
    )
    assert "censored" not in json.dumps(result.to_dict(), sort_keys=True)


def test_terminal_signal_without_t_plus_1_open_is_no_fill_not_censoring(
    registry,
) -> None:
    """No executable entry at the boundary stays the frozen no-fill rule."""

    sessions = sequential_sessions(56)
    binding = candidate(registry, US_CANDIDATE_ORDER[2])
    result = run_us_stage_b(
        source=InMemoryUSBarSource(volbreak_bars("LAST", sessions)),
        contract=contract(binding, sessions),
        corpus_sessions=sessions,
    )

    assert result.run_invalid is False
    assert [outcome.status for outcome in result.outcomes] == ["entry_no_fill"]
    assert result.outcomes[0].entry_session is None
    assert "censored" not in json.dumps(result.to_dict(), sort_keys=True)


def test_rev_engine_emits_independent_cost_profile_verdicts(
    monkeypatch, registry
) -> None:
    """Engine assembly preserves a 10bp/5bp pair that can genuinely diverge."""

    binding = candidate(registry, US_CANDIDATE_ORDER[1])
    sessions = tuple(date(2023, 1, 1) + timedelta(days=index) for index in range(731))
    symbols = tuple(f"R{index:02d}" for index in range(10))
    session_index = {session: index for index, session in enumerate(sessions)}
    source = InMemoryUSBarSource(
        tuple(
            USStageBDailyBar(
                symbol=symbol,
                session_date=session,
                open=100.0,
                adjusted_close=101.0,
                volume=50_000.0,
            )
            for symbol in symbols
            for session in sessions
        )
    )

    def staged_rev_signal(
        candidate_binding,
        *,
        symbol,
        session_date,
        history,
        no_active_position,
    ) -> SignalObservation:
        del history
        index = session_index[session_date]
        scheduled = index <= len(sessions) - 5 and index % 4 == int(symbol[1:]) % 4
        signal = scheduled and no_active_position
        return SignalObservation(
            strategy_id=candidate_binding.strategy_id,
            contract_hash=candidate_binding.contract_hash,
            labels=candidate_binding.labels,
            symbol=symbol,
            session_date=session_date,
            universe_eligible=True,
            technical_signal=scheduled,
            no_active_position=no_active_position,
            signal=signal,
            exclusion_reason=None if signal else "ENGINE_SEMANTIC_FIXTURE",
            adv20_pre_proxy=5_000_001.0,
            tie_break_sha256=tie_break_digest(
                candidate_binding.strategy_id, session_date, symbol
            ).hex(),
            metrics={"z3": -2.0},
            stages={"engine_semantic_fixture": True},
        )

    def divergent_profile_cohorts(
        *, contract, outcomes, observations, bars_by_symbol, sessions
    ) -> tuple[CohortComparison, ...]:
        del contract, observations, bars_by_symbol, sessions
        comparisons: list[CohortComparison] = []
        for outcome in outcomes:
            if outcome.status != "completed":
                continue
            assert outcome.entry_session is not None
            assert outcome.exit_session is not None
            assert outcome.base_net_return is not None
            assert outcome.sensitivity_net_return is not None
            comparisons.append(
                CohortComparison(
                    strategy_id=outcome.strategy_id,
                    contract_hash=outcome.contract_hash,
                    labels=outcome.labels,
                    symbol=outcome.symbol,
                    signal_session=outcome.signal_session,
                    entry_session=outcome.entry_session,
                    exit_session=outcome.exit_session,
                    liquidity_decile=9,
                    eligible_universe_size=2,
                    leave_one_out_member_count=1,
                    excluded_entry_no_fill_count=0,
                    excluded_maturity_close_count=0,
                    status="completed",
                    candidate_base_net_return=outcome.base_net_return,
                    candidate_sensitivity_net_return=outcome.sensitivity_net_return,
                    baseline_base_net_return=outcome.base_net_return + 0.001,
                    baseline_sensitivity_net_return=(
                        outcome.sensitivity_net_return - 0.001
                    ),
                    base_excess_return=-0.001,
                    sensitivity_excess_return=0.001,
                    volume_ratio20=None,
                    tie_break_sha256=outcome.tie_break_sha256,
                )
            )
        return tuple(comparisons)

    monkeypatch.setattr(engine_module, "evaluate_signal", staged_rev_signal)
    monkeypatch.setattr(
        engine_module, "_build_cohort_comparisons", divergent_profile_cohorts
    )
    result = run_us_stage_b(
        source=source,
        contract=contract(binding, sessions),
        corpus_sessions=sessions,
    )

    assert len(result.completed_outcomes) >= 1_000
    profiles = result.cost_profile_verdicts
    assert profiles is not None
    assert profiles.base_10bp_per_side is not profiles.sensitivity_5bp_per_side
    assert profiles.base_10bp_per_side.state == "FALSIFIED_VALIDATION_COHORT_EXCESS"
    assert profiles.sensitivity_5bp_per_side.state == "NOT_FALSIFIED_EXPLORATORY_ONLY"
    assert result.verdict.state == "FALSIFIED_COST_SENSITIVE"
    rendered = result.to_dict()["cost_profile_verdicts"]
    assert rendered["base_10bp_per_side"]["cost_profile"] == "base_10bp_per_side"
    assert (
        rendered["sensitivity_5bp_per_side"]["cost_profile"]
        == "sensitivity_5bp_per_side"
    )


def test_no_fill_does_not_promote_a_lower_ranked_same_session_signal(registry) -> None:
    sessions = sequential_sessions(70)
    binding = candidate(registry, US_CANDIDATE_ORDER[2])
    bars = tuple(
        bar
        for index in range(1, 12)
        for bar in volbreak_bars(
            f"S{index:02d}",
            sessions,
            adv_multiplier=float(index),
            no_entry_open=index == 11,
        )
    )
    result = run_us_stage_b(
        source=InMemoryUSBarSource(bars),
        contract=contract(binding, sessions),
        corpus_sessions=sessions,
    )
    outcomes = [item for item in result.outcomes if item.signal_session == sessions[55]]

    assert len(outcomes) == 11
    assert outcomes[0].symbol == "S11"
    assert outcomes[0].status == "entry_no_fill"
    assert [item.status for item in outcomes].count("completed") == 9
    rejected = [item for item in outcomes if item.status == "capacity_rejected"]
    assert [item.symbol for item in rejected] == ["S01"]


def test_empty_result_is_labeled_serializable_and_not_reported_as_success(
    registry,
) -> None:
    sessions = sequential_sessions(3)
    binding = candidate(registry, US_CANDIDATE_ORDER[2])
    source = InMemoryUSBarSource(
        (
            USStageBDailyBar(
                symbol="EMPTY",
                session_date=sessions[0],
                open=100.0,
                adjusted_close=100.0,
                volume=50_000.0,
            ),
        )
    )
    result = run_us_stage_b(
        source=source,
        contract=contract(binding, sessions),
        corpus_sessions=sessions,
    )
    rendered = json.dumps(result.to_dict(), sort_keys=True)

    assert result.outcomes == ()
    assert result.verdict.state == "FALSIFIED_FREQUENCY"
    for label in (
        "EXPLORATORY_FALSIFICATION_ONLY",
        "SURVIVORSHIP_BIASED=TRUE",
        "PIT_DELIST_MISSING",
        "EXECUTION_ENVELOPE_UNBOUND",
        "VOLUME_CA_UNRESOLVED",
    ):
        assert label in rendered
