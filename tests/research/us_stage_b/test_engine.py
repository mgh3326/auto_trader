from __future__ import annotations

import json
from datetime import date, timedelta

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
