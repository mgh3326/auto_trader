from __future__ import annotations

import json
from datetime import date, timedelta
from statistics import mean

import pytest

from research.us_stage_b.engine import rank_signal_observations, run_us_stage_b
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
    assert result.observation_summary["total_evaluated"] == len(sessions)
    assert result.observation_summary["persisted"] == "signal_true_only"
    assert result.observation_summary["signal_true"] == len(result.observations)
    assert all(item.signal for item in result.observations)
    rendered = result.to_dict()
    assert rendered["observation_summary"]["total_evaluated"] == len(sessions)
    assert all(item["signal"] is True for item in rendered["observations"])


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


def _raw_rev_cost_discriminator_bars(
    sessions: tuple[date, ...],
) -> tuple[tuple[str, ...], tuple[USStageBDailyBar, ...]]:
    """Build 29,240 deterministic raw bars that straddle the frozen REV costs."""

    phase_count = 20
    symbols = tuple(
        f"R{phase:02d}_{member}" for phase in range(phase_count) for member in range(2)
    )
    first_signal_index = 126
    last_viable_signal_index = len(sessions) - 5
    initial_history_last_shock_index = first_signal_index - 4
    closes: dict[str, list[float]] = {}
    for phase in range(phase_count):
        for member in range(2):
            symbol = f"R{phase:02d}_{member}"
            close = 100.0
            values: list[float] = []
            for index in range(len(sessions)):
                if index:
                    shock = index % phase_count == phase and (
                        index <= initial_history_last_shock_index
                        or first_signal_index <= index <= last_viable_signal_index
                    )
                    close *= 0.8 if shock else 1.014
                values.append(close)
            closes[symbol] = values

    # Two same-phase candidates are the only signal symbols on a session.  Set
    # their actual D+3 close so each raw cohort has +15bp gross excess.  Under
    # §13's strategy-only costs that is -5bp at 10bp/side and +5bp at 5bp/side.
    target_gross_excess = 0.0015
    adjusted_close_overrides: dict[tuple[str, int], float] = {}
    for signal_index in range(first_signal_index, last_viable_signal_index + 1):
        phase = signal_index % phase_count
        signal_symbols = (f"R{phase:02d}_0", f"R{phase:02d}_1")
        entry_index = signal_index + 1
        exit_index = signal_index + 4
        non_signal_gross_sum = sum(
            closes[symbol][exit_index] / closes[symbol][entry_index] - 1.0
            for symbol in symbols
            if symbol not in signal_symbols
        )
        target_candidate_gross = (
            non_signal_gross_sum + (len(symbols) - 1) * target_gross_excess
        ) / (len(symbols) - 2)
        for symbol in signal_symbols:
            adjusted_close_overrides[(symbol, exit_index)] = closes[symbol][
                entry_index
            ] * (1.0 + target_candidate_gross)

    bars: list[USStageBDailyBar] = []
    for symbol in symbols:
        for index, session in enumerate(sessions):
            adjusted_close = adjusted_close_overrides.get(
                (symbol, index), closes[symbol][index]
            )
            bars.append(
                USStageBDailyBar(
                    symbol=symbol,
                    session_date=session,
                    open=closes[symbol][index],
                    adjusted_close=adjusted_close,
                    # Every same-session eligible member has the same ADV20-pre.
                    volume=10_000_000.0 / adjusted_close,
                )
            )
    return symbols, tuple(bars)


def test_rev_cost_profiles_diverge_from_unpatched_raw_ohlcv(registry) -> None:
    """The actual 10bp/5bp verdict split must survive the untouched engine."""

    sessions = tuple(date(2023, 1, 1) + timedelta(days=index) for index in range(731))
    symbols, bars = _raw_rev_cost_discriminator_bars(sessions)
    binding = candidate(registry, US_CANDIDATE_ORDER[1])
    result = run_us_stage_b(
        source=InMemoryUSBarSource(bars),
        contract=contract(binding, sessions),
        corpus_sessions=sessions,
    )

    assert len(bars) == 29_240
    assert result.run_invalid is False
    assert len(result.completed_outcomes) == 1_202
    assert len(result.cohorts) == 1_202
    assert all(cohort.status == "completed" for cohort in result.cohorts)
    assert len({outcome.entry_session for outcome in result.completed_outcomes}) == 601
    assert (
        sum(
            outcome.entry_session is not None and outcome.entry_session.year == 2023
            for outcome in result.completed_outcomes
        )
        == 476
    )
    assert (
        sum(
            outcome.entry_session is not None and outcome.entry_session.year == 2024
            for outcome in result.completed_outcomes
        )
        == 726
    )

    profiles = result.cost_profile_verdicts
    assert profiles is not None
    assert profiles.base_10bp_per_side is not profiles.sensitivity_5bp_per_side
    assert profiles.base_10bp_per_side.state == "FALSIFIED_VALIDATION_COHORT_EXCESS"
    assert profiles.sensitivity_5bp_per_side.state == "NOT_FALSIFIED_EXPLORATORY_ONLY"
    assert result.verdict.state == "FALSIFIED_COST_SENSITIVE"
    assert profiles.base_10bp_per_side.gate_results[
        "profile_entry_session_equal_weighted_excess"
    ] == pytest.approx(-0.0005)
    assert profiles.sensitivity_5bp_per_side.gate_results[
        "profile_entry_session_equal_weighted_excess"
    ] == pytest.approx(0.0005)
    rendered_profiles = result.to_dict()["cost_profile_verdicts"]
    assert rendered_profiles is not None
    assert rendered_profiles["base_10bp_per_side"]["cost_profile"] == (
        "base_10bp_per_side"
    )
    assert rendered_profiles["sensitivity_5bp_per_side"]["cost_profile"] == (
        "sensitivity_5bp_per_side"
    )

    bars_by_identity = {(bar.symbol, bar.session_date): bar for bar in bars}
    first_cohort = result.cohorts[0]
    assert first_cohort.entry_session is not None
    assert first_cohort.exit_session is not None
    baseline_gross_returns: list[float] = []
    for symbol in symbols:
        if symbol == first_cohort.symbol:
            continue
        entry_bar = bars_by_identity[(symbol, first_cohort.entry_session)]
        exit_bar = bars_by_identity[(symbol, first_cohort.exit_session)]
        assert entry_bar.open is not None
        assert exit_bar.adjusted_close is not None
        baseline_gross_returns.append(exit_bar.adjusted_close / entry_bar.open - 1.0)
    baseline_gross = mean(baseline_gross_returns)
    assert first_cohort.leave_one_out_member_count == 39
    assert first_cohort.baseline_base_net_return == pytest.approx(baseline_gross)
    assert first_cohort.baseline_sensitivity_net_return == pytest.approx(baseline_gross)
    assert first_cohort.base_excess_return == pytest.approx(-0.0005)
    assert first_cohort.sensitivity_excess_return == pytest.approx(0.0005)
    for cohort in result.cohorts:
        assert cohort.base_excess_return is not None
        assert cohort.sensitivity_excess_return is not None
        assert cohort.sensitivity_excess_return == pytest.approx(
            cohort.base_excess_return + 0.001
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
