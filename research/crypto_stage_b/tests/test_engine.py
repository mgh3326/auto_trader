from __future__ import annotations

import json
from datetime import date, timedelta

from research.crypto_stage_b.contracts import CryptoStageBRunContract
from research.crypto_stage_b.engine import run_candidate_pair, run_execution_arm
from research.crypto_stage_b.report import build_harness_report
from research.crypto_stage_b.source import InMemoryDailyBarSource, TerminalEvent
from research.crypto_stage_b.tests.conftest import candidate, cost, etr_bars


def _contract(*, end_index: int = 255) -> CryptoStageBRunContract:
    start = date(2024, 1, 1)
    return CryptoStageBRunContract(
        candidate=candidate("CR-SPOT-ETR-01"),
        venue="upbit_krw",
        exploration_start=start,
        exploration_end=start + timedelta(days=end_index),
        cost=cost("upbit_krw"),
    )


def test_entry_is_next_utc_calendar_day_not_next_observed_bar() -> None:
    result = run_execution_arm(
        source=InMemoryDailyBarSource(etr_bars(include_entry=False)),
        contract=_contract(),
        arm="full",
    )

    assert [item.status for item in result.outcomes] == ["entry_no_fill"]


def test_scheduled_exit_missing_is_not_forward_filled_or_next_observation() -> None:
    result = run_execution_arm(
        source=InMemoryDailyBarSource(etr_bars(include_exit=False)),
        contract=_contract(),
        arm="full",
    )

    assert [item.status for item in result.outcomes] == ["missing_exit"]


def test_observed_delisting_uses_last_valid_close_and_marks_delisted_exit() -> None:
    start = date(2024, 1, 1)
    result = run_execution_arm(
        source=InMemoryDailyBarSource(
            etr_bars(include_exit=False),
            terminal_events=(
                TerminalEvent(
                    venue="upbit_krw",
                    symbol="TEST",
                    session=start + timedelta(days=254),
                ),
            ),
        ),
        contract=_contract(),
        arm="full",
    )

    outcome = result.outcomes[0]
    assert outcome.status == "delisted_exit"
    assert outcome.delisted_exit is True
    assert outcome.exit_session == start + timedelta(days=254)
    assert outcome.exit_close == 100.0


def test_venue_capacity_is_five_and_tie_breaks_by_symbol() -> None:
    bars = tuple(
        bar
        for symbol in ("A", "B", "C", "D", "E", "F")
        for bar in etr_bars(symbol=symbol)
    )
    result = run_execution_arm(
        source=InMemoryDailyBarSource(bars),
        contract=_contract(),
        arm="full",
    )

    assert [item.symbol for item in result.outcomes[:5]] == ["A", "B", "C", "D", "E"]
    assert result.outcomes[-1].symbol == "F"
    assert result.outcomes[-1].status == "capacity_rejected"


def test_venue_result_never_reads_or_ranks_other_venue() -> None:
    source = InMemoryDailyBarSource(
        etr_bars(venue="upbit_krw", symbol="KRW-ONLY")
        + etr_bars(venue="binance_usdt_spot", symbol="USDT-OTHER")
    )
    result = run_execution_arm(source=source, contract=_contract(), arm="full")

    assert {item.venue for item in result.observations} == {"upbit_krw"}
    assert {item.venue for item in result.outcomes} == {"upbit_krw"}


def test_boundary_censors_outcome_without_any_outside_read() -> None:
    result = run_execution_arm(
        source=InMemoryDailyBarSource(etr_bars()),
        contract=_contract(end_index=253),
        arm="full",
    )

    assert [item.status for item in result.outcomes] == [
        "censored_at_exploration_boundary"
    ]
    assert result.access_summary["outside_boundary_reads"] == 0


def test_ablation_pair_and_harness_have_only_venue_year_records() -> None:
    pair = run_candidate_pair(
        source=InMemoryDailyBarSource(etr_bars(quote_volume_on_signal=100.0)),
        contract=_contract(),
    )
    report = build_harness_report(pair).to_dict()
    rendered = json.dumps(report, sort_keys=True)

    assert pair.full.outcomes == ()
    assert [item.status for item in pair.ablation.outcomes][:1] == ["completed"]
    assert "pooled" not in rendered
    assert report["records"]
    full = report["records"][0]["full"]
    assert full["low_liquidity_break_even_round_trip_bp"] == 30
    assert full["sensitivity_break_even_round_trip_bp"] == 70
