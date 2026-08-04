"""Adversarial unit tests for the frozen adjacent-window predicate.

Each invariant is deliberately broken at least once.  A happy-path-only suite
would not prove the required fail-closed behavior.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import sys
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from research.kr_backfill import adjacent_window_predicate as predicate

pytestmark = pytest.mark.unit

SESSION = date(2026, 7, 30)
HOLDOUT_SESSIONS = (date(2026, 7, 28), date(2026, 7, 29))
KST = timezone(timedelta(hours=9), name="KST")
T = datetime(2026, 7, 30, 14, 55, tzinfo=KST)
T_NEXT = T + timedelta(minutes=1)
REPO_ROOT = Path(__file__).resolve().parents[2]
PREDICATE_SOURCE = (
    REPO_ROOT / "research" / "kr_backfill" / "adjacent_window_predicate.py"
)
PROMOTION_RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "kr-research-candles-promotion.md"


def bar(open_, high, low, close, volume, *, value=None):
    record = {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }
    if value is not None:
        record["value"] = value
    return record


def context(**changes):
    base = predicate.ComparisonContext(
        source_a="KIWOOM_MOCK",
        source_b="KIS_LIVE",
        market="KRX",
        session_segment=predicate.KRX_REGULAR_SEGMENT,
        adjustment_a="UNADJUSTED",
        adjustment_b="UNADJUSTED",
        timezone_a=predicate.KST_TIMEZONE_NAME,
        timezone_b=predicate.KST_TIMEZONE_NAME,
        completed_session=True,
        latest_session_rule_applied_first=True,
        bar_label_convention=predicate.FROZEN_BAR_LABEL_CONVENTION,
        offset_minutes=predicate.FROZEN_OFFSET_MINUTES,
    )
    return replace(base, **changes)


def observed_pair():
    """The observed 000270 14:55/14:56 shape: exactly three raw cells differ."""

    source_a = {
        T: bar(119300, 119500, 119200, 119400, 11628),
        T_NEXT: bar(119400, 119600, 119300, 119500, 4538),
    }
    source_b = {
        T: bar(119300, 119500, 119200, 119500, 11613),
        T_NEXT: bar(119400, 119600, 119300, 119500, 4553),
    }
    return source_a, source_b


def with_neighbours(source_a, source_b, *, before=1, after=1):
    for step in range(1, before + 1):
        timestamp = T - timedelta(minutes=step)
        source_a[timestamp] = bar(100, 100, 100, 100, 10)
        source_b[timestamp] = bar(100, 100, 100, 100, 10)
    for step in range(1, after + 1):
        timestamp = T_NEXT + timedelta(minutes=step)
        source_a[timestamp] = bar(100, 100, 100, 100, 10)
        source_b[timestamp] = bar(100, 100, 100, 100, 10)
    return source_a, source_b


def accepted_input():
    return with_neighbours(*observed_pair())


def classify(source_a, source_b, **context_changes):
    return predicate.classify(
        "000270",
        SESSION,
        source_a,
        source_b,
        context=context(**context_changes),
    )


def holdout_result(symbol: str, session: date):
    """Create a long completed-session fixture outside the design session."""

    start = datetime.combine(session, time(14, 0), tzinfo=KST)
    source_a = {}
    source_b = {}
    for offset in range(60):
        timestamp = start + timedelta(minutes=offset)
        source_a[timestamp] = bar(100, 105, 95, 100, 100)
        source_b[timestamp] = bar(100, 105, 95, 100, 100)

    pair_start = start + timedelta(minutes=20)
    pair_next = pair_start + timedelta(minutes=1)
    source_a[pair_start] = bar(100, 105, 95, 100, 100)
    source_a[pair_next] = bar(100, 105, 95, 101, 100)
    source_b[pair_start] = bar(100, 105, 95, 101, 90)
    source_b[pair_next] = bar(100, 105, 95, 101, 110)
    return predicate.classify(symbol, session, source_a, source_b, context=context())


def completed_holdout_results():
    return [
        holdout_result(symbol, session)
        for session in HOLDOUT_SESSIONS
        for symbol in ("000270", "005930")
    ]


def invariant_ids(result):
    ids = {failure.invariant for failure in result.invariant_failures}
    for pair in result.rejected_pairs:
        ids.update(failure.invariant for failure in pair.failures)
    return ids


def full_window_with_two_pairs():
    source_a = {}
    source_b = {}
    for offset in range(-5, 13):
        timestamp = T + timedelta(minutes=offset)
        source_a[timestamp] = bar(100, 100, 100, 100, 10)
        source_b[timestamp] = bar(100, 100, 100, 100, 10)
    observed_a, observed_b = observed_pair()
    source_a.update(observed_a)
    source_b.update(observed_b)
    return source_a, source_b


def phase_b_evidence(**changes):
    base = predicate.PhaseBEvidence(
        design_sessions=(SESSION,),
        holdout_sessions=HOLDOUT_SESSIONS,
        holdout_revalidation_completed=True,
        holdout_sessions_completed=True,
        higher_timeframe_bucket_exact={
            "5m": "PASS",
            "15m": "PASS",
            "30m": "PASS",
            "1h": "PASS",
        },
    )
    return replace(base, **changes)


def import_predicate_copy(path: Path) -> None:
    module_name = f"_adjacent_window_predicate_copy_{path.stat().st_mtime_ns}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)


def test_observed_shape_is_accepted_but_raw_exact_stays_failed():
    result = classify(*accepted_input())

    assert result.adjacent_window_equivalent is True
    assert result.raw_1m_exact is False
    assert result.raw_mismatch_cells == 3
    assert result.raw_mismatch_minutes == 2
    assert len(result.accepted_pairs) == 1
    assert result.noncompliant_mismatch_cells == 0

    record = result.as_record()
    assert record["RAW_1M_EXACT"] == "FAIL"
    assert record["ADJACENT_WINDOW_EQUIVALENCE"] == "PASS"
    assert record["exception_pair_count"] == 1
    assert record["exception_pairs"][0]["t_kst"] == T.isoformat()
    assert record["raw_mismatch_locations"] == [
        {"minute_kst": T.isoformat(), "fields": ["close", "volume"]},
        {"minute_kst": T_NEXT.isoformat(), "fields": ["volume"]},
    ]


def test_classification_result_is_frozen_and_tuple_backed():
    result = classify(*accepted_input())

    assert isinstance(result.raw_mismatches, tuple)
    assert isinstance(result.accepted_pairs, tuple)
    assert isinstance(result.invariant_failures, tuple)
    with pytest.raises(FrozenInstanceError):
        result.noncompliant_mismatch_cells = 1
    with pytest.raises(AttributeError):
        result.accepted_pairs.clear()


# I1: same semantic input and both candidate minutes must exist in both sources.


def test_I1_one_sided_only_input_fails_even_without_raw_cells():
    source_a = {T: bar(100, 100, 100, 100, 10), T_NEXT: bar(100, 100, 100, 100, 10)}
    source_b = {T: bar(100, 100, 100, 100, 10)}
    result = classify(source_a, source_b)

    assert result.raw_mismatch_cells == 0
    assert result.one_sided_minutes == (T_NEXT,)
    assert result.adjacent_window_equivalent is False
    assert "I1" in invariant_ids(result)


def test_I1_no_data_cannot_be_reported_as_raw_exact():
    result = classify({}, {})

    assert result.common_minutes == 0
    assert result.raw_1m_exact is False
    assert result.adjacent_window_equivalent is False
    assert result.as_record()["RAW_1M_EXACT"] == "FAIL"
    assert "I1" in invariant_ids(result)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"market": "NXT"}, "KRX regular"),
        ({"adjustment_b": "ADJUSTED"}, "same adjustment"),
        ({"timezone_b": "UTC"}, "Asia/Seoul"),
        ({"completed_session": False}, "not declared complete"),
        ({"latest_session_rule_applied_first": False}, "not applied"),
        ({"completed_session": 1}, "not declared complete"),
        ({"latest_session_rule_applied_first": "yes"}, "not applied"),
    ],
)
def test_I1_semantic_preconditions_fail_closed(changes, expected):
    result = classify(*accepted_input(), **changes)

    assert result.adjacent_window_equivalent is False
    assert "I1" in invariant_ids(result)
    assert any(expected in failure.detail for failure in result.invariant_failures)


def test_I1_naive_timestamp_fails():
    source_a, source_b = accepted_input()
    naive = T.replace(tzinfo=None)
    source_b[naive] = source_b.pop(T)
    result = classify(source_a, source_b)

    assert result.adjacent_window_equivalent is False
    assert "I1" in invariant_ids(result)


# I2: no discovered or non-frozen timestamp convention.


@pytest.mark.parametrize(
    "changes",
    [
        {"offset_minutes": -1},
        {"bar_label_convention": "POST_HOC_OFFSET_SEARCH"},
    ],
)
def test_I2_non_frozen_timestamp_contract_fails(changes):
    result = classify(*accepted_input(), **changes)

    assert result.adjacent_window_equivalent is False
    assert "I2" in invariant_ids(result)


# I3: isolated, disjoint, adjacent pairs only.


def test_I3_three_minute_chain_fails():
    source_a, source_b = accepted_input()
    third = T_NEXT + timedelta(minutes=1)
    source_b[third] = {**source_b[third], "volume": 11}
    result = classify(source_a, source_b)

    assert result.adjacent_window_equivalent is False
    assert "I3" in invariant_ids(result)
    assert result.noncompliant_mismatch_cells == result.raw_mismatch_cells


def test_I3_lone_nonadjacent_mismatch_fails():
    source_a, source_b = accepted_input()
    far = T + timedelta(minutes=10)
    source_a[far] = bar(100, 100, 100, 100, 10)
    source_b[far] = bar(100, 100, 100, 101, 10)
    result = classify(source_a, source_b)

    assert result.adjacent_window_equivalent is False
    assert "I3" in invariant_ids(result)


def test_I3_duplicate_mismatch_minutes_fail_partition():
    _, failure = predicate._partition_mismatch_minutes((T, T_NEXT, T, T_NEXT))

    assert failure is not None
    assert failure.invariant == "I3"
    assert "duplicate" in failure.detail


def test_I3_missing_both_neighbour_bars_fails_isolation():
    source_a, source_b = accepted_input()
    previous = T - timedelta(minutes=1)
    del source_a[previous]
    del source_b[previous]
    result = classify(source_a, source_b)

    assert result.adjacent_window_equivalent is False
    assert "I3" in invariant_ids(result)


# I4: outer price exactness.


@pytest.mark.parametrize(
    ("timestamp", "field_name", "value"),
    [
        (T, "open", 119310),
        (T_NEXT, "close", 119510),
    ],
)
def test_I4_outer_price_difference_fails(timestamp, field_name, value):
    source_a, source_b = accepted_input()
    source_b[timestamp] = {**source_b[timestamp], field_name: value}
    result = classify(source_a, source_b)

    assert result.adjacent_window_equivalent is False
    assert "I4" in invariant_ids(result)


# I5: aggregate high/low exactness.


@pytest.mark.parametrize(
    ("timestamp", "field_name", "value"),
    [
        (T, "high", 119900),
        (T_NEXT, "low", 119000),
    ],
)
def test_I5_two_minute_high_or_low_difference_fails(timestamp, field_name, value):
    source_a, source_b = accepted_input()
    source_b[timestamp] = {**source_b[timestamp], field_name: value}
    result = classify(source_a, source_b)

    assert result.adjacent_window_equivalent is False
    assert "I5" in invariant_ids(result)


# I6: aggregate volume exactness.


def test_I6_two_minute_volume_sum_difference_fails():
    source_a, source_b = accepted_input()
    source_b[T_NEXT] = {**source_b[T_NEXT], "volume": 4600}
    result = classify(source_a, source_b)

    assert result.adjacent_window_equivalent is False
    assert "I6" in invariant_ids(result)


# I7: non-zero, cancelling volume movement.


def test_I7_zero_volume_movement_fails():
    source_a, source_b = observed_pair()
    source_b[T] = {**source_a[T], "close": source_a[T]["close"] + 1}
    source_b[T_NEXT] = {**source_a[T_NEXT], "open": source_a[T_NEXT]["open"] + 1}
    source_a, source_b = with_neighbours(source_a, source_b)
    result = classify(source_a, source_b)

    assert result.adjacent_window_equivalent is False
    assert "I7" in invariant_ids(result)


def test_I7_non_cancelling_movement_is_reported_alongside_sum_failure():
    source_a, source_b = accepted_input()
    source_b[T_NEXT] = {**source_b[T_NEXT], "volume": source_a[T_NEXT]["volume"] - 40}
    result = classify(source_a, source_b)

    assert result.adjacent_window_equivalent is False
    assert {"I6", "I7"}.issubset(invariant_ids(result))


# I8: no OHLCV mismatch may remain outside an accepted pair; value stays excluded.


def test_I8_ohlcv_difference_outside_candidate_pair_fails():
    source_a, source_b = accepted_input()
    far = T + timedelta(minutes=10)
    source_a[far] = bar(100, 100, 100, 100, 10)
    source_b[far] = bar(100, 100, 100, 101, 10)
    result = classify(source_a, source_b)

    assert result.adjacent_window_equivalent is False
    assert result.noncompliant_mismatch_cells > 0
    assert "I8" in invariant_ids(result)


def test_I8_synthetic_or_cumulative_value_is_ignored_even_when_it_differs():
    source_a, source_b = accepted_input()
    source_a[T]["value"] = 1
    source_b[T]["value"] = 99_999_999
    source_a[T_NEXT]["value"] = 2
    source_b[T_NEXT]["value"] = 88_888_888
    result = classify(source_a, source_b)

    assert "value" not in predicate.COMPARED_FIELDS
    assert result.adjacent_window_equivalent is True
    assert result.raw_mismatch_cells == 3


# I9: every failure remains a raw mismatch; there is no size/rate rescue.


def test_I9_rejected_pair_is_left_noncompliant():
    source_a, source_b = accepted_input()
    source_b[T] = {**source_b[T], "high": 999999}
    result = classify(source_a, source_b)

    assert result.adjacent_window_equivalent is False
    assert result.noncompliant_mismatch_cells == result.raw_mismatch_cells
    assert "I9" in invariant_ids(result)


def test_I9_one_share_error_is_not_rescued_by_its_small_size():
    source_a, source_b = accepted_input()
    source_b[T] = {**source_b[T], "volume": source_a[T]["volume"] - 1}
    source_b[T_NEXT] = {
        **source_b[T_NEXT],
        "volume": source_a[T_NEXT]["volume"] - 1,
    }
    result = classify(source_a, source_b)

    assert result.adjacent_window_equivalent is False
    assert result.noncompliant_mismatch_cells > 0


def test_I9_many_accepted_pairs_do_not_rescue_one_rejected_pair():
    source_a, source_b = full_window_with_two_pairs()
    bad = T + timedelta(minutes=6)
    bad_next = bad + timedelta(minutes=1)
    source_a[bad] = bar(100, 100, 100, 100, 50)
    source_b[bad] = bar(100, 100, 100, 100, 40)
    source_a[bad_next] = bar(100, 100, 100, 100, 50)
    source_b[bad_next] = bar(100, 100, 100, 100, 45)
    result = classify(source_a, source_b)

    assert len(result.accepted_pairs) == 1
    assert len(result.rejected_pairs) == 1
    assert result.adjacent_window_equivalent is False
    assert result.noncompliant_mismatch_cells > 0


def test_phase_a_evidence_cannot_issue_a_documented_exception_pass():
    result = classify(*accepted_input())
    decision = predicate.evaluate_shard_gate(
        [result],
        phase_b=phase_b_evidence(
            holdout_sessions=(),
            holdout_revalidation_completed=False,
            holdout_sessions_completed=False,
            higher_timeframe_bucket_exact={},
        ),
    )

    assert decision.status == "FAIL"
    assert "holdout_revalidation_not_completed" in decision.reasons
    assert decision.two_way_enabled is False


def test_shard_pass_requires_distinct_completed_holdout_and_all_bucket_results():
    results = completed_holdout_results()
    decision = predicate.evaluate_shard_gate(results, phase_b=phase_b_evidence())

    assert decision.status == "PASS_WITH_DOCUMENTED_EXCEPTION"
    assert decision.covered_symbols == predicate.MIN_HOLDOUT_SYMBOLS
    assert decision.covered_sessions == predicate.MIN_HOLDOUT_SESSIONS
    assert decision.compared_cells >= predicate.MIN_HOLDOUT_COMPARED_CELLS
    assert decision.two_way_enabled is False


def test_verifier_forged_evidence_now_fails_against_actual_result_session():
    # Reproduce the verifier's exact B1 forgery: a design-cell result plus
    # unrelated declared date tuples and PASS strings.
    design_result = classify(*accepted_input())
    forged = predicate.PhaseBEvidence(
        design_sessions=(date(1999, 1, 1),),
        holdout_sessions=(date(2000, 1, 1),),
        holdout_revalidation_completed=True,
        holdout_sessions_completed=True,
        higher_timeframe_bucket_exact={
            "5m": "PASS",
            "15m": "PASS",
            "30m": "PASS",
            "1h": "PASS",
        },
    )

    decision = predicate.evaluate_shard_gate([design_result], phase_b=forged)

    assert decision.status == "FAIL"
    assert (
        "result_session_not_in_holdout_sessions:000270:2026-07-30" in decision.reasons
    )
    assert "holdout_symbol_coverage_below_minimum:1<2" in decision.reasons
    assert "holdout_session_coverage_below_minimum:1<2" in decision.reasons
    assert decision.two_way_enabled is False


def test_design_session_result_cannot_be_retroactively_passed():
    design_result = classify(*accepted_input())
    decision = predicate.evaluate_shard_gate(
        [design_result],
        phase_b=phase_b_evidence(holdout_sessions=(SESSION,)),
    )

    assert decision.status == "FAIL"
    assert "holdout_overlaps_design_sessions" in decision.reasons
    assert "result_session_is_design_session:000270:2026-07-30" in decision.reasons


def test_shard_requires_minimum_actual_holdout_coverage():
    result = holdout_result("000270", HOLDOUT_SESSIONS[0])
    decision = predicate.evaluate_shard_gate(
        [result],
        phase_b=phase_b_evidence(holdout_sessions=(HOLDOUT_SESSIONS[0],)),
    )

    assert decision.status == "FAIL"
    assert "holdout_symbol_coverage_below_minimum:1<2" in decision.reasons
    assert "holdout_session_coverage_below_minimum:1<2" in decision.reasons
    assert "holdout_compared_cell_coverage_below_minimum:300<1000" in decision.reasons


def test_every_declared_holdout_session_must_have_an_actual_result():
    result = holdout_result("000270", HOLDOUT_SESSIONS[0])
    decision = predicate.evaluate_shard_gate([result], phase_b=phase_b_evidence())

    assert decision.status == "FAIL"
    assert "declared_holdout_session_has_no_result:2026-07-29" in decision.reasons


def test_predicate_name_version_and_hash_are_frozen():
    assert predicate.PREDICATE_NAME == "ADJACENT_WINDOW_EQUIVALENT_V1"
    assert predicate.PREDICATE_VERSION == "1.0.1"
    assert (
        predicate.PREDICATE_SPEC_SHA256
        == "abbd692cd91c0efc07ce21409ad043a199bad97d954d1a97f55852e29c929618"
    )
    assert predicate._spec_hash() == predicate.PREDICATE_SPEC_SHA256
    assert predicate._module_source_hash() == predicate.MODULE_SOURCE_SHA256
    assert predicate.predicate_spec_payload()["required_higher_timeframes"] == [
        "5m",
        "15m",
        "30m",
        "1h",
    ]
    assert [item.identifier for item in predicate.INVARIANT_SPEC] == [
        f"I{number}" for number in range(1, 10)
    ]


def test_module_source_freeze_assertion_runs_before_predicate_body():
    source = PREDICATE_SOURCE.read_text(encoding="utf-8")

    assert source.index("\n_assert_module_source_is_frozen()\n") < source.index(
        "\n@dataclass(frozen=True)\nclass ComparisonContext"
    )


def test_changing_the_canonical_spec_changes_its_hash():
    altered = copy.deepcopy(predicate.predicate_spec_payload())
    altered["invariants"][0]["rule"] += " changed"
    altered_hash = hashlib.sha256(
        json.dumps(
            altered,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    assert altered_hash != predicate.PREDICATE_SPEC_SHA256


def test_module_source_freeze_detects_removed_I7_implementation(tmp_path):
    source = PREDICATE_SOURCE.read_text(encoding="utf-8")
    i7_enforcement = """\
    if delta_t == 0 or delta_next == 0:
        failures.append(
            InvariantFailure("I7", "volume movement must be non-zero in both minutes")
        )
    if delta_t != -delta_next:
        failures.append(InvariantFailure("I7", "volume deltas do not cancel"))
"""
    assert i7_enforcement in source
    edited_path = tmp_path / "adjacent_window_predicate_i7_deleted.py"
    edited_path.write_text(source.replace(i7_enforcement, "", 1), encoding="utf-8")

    with pytest.raises(RuntimeError, match="module source changed"):
        import_predicate_copy(edited_path)


def test_verifier_two_source_edits_now_fail_at_import(tmp_path):
    """Reproduce the verifier's B3 edits without monkeypatching the module."""

    source = PREDICATE_SOURCE.read_text(encoding="utf-8")
    i7_enforcement = """\
    if delta_t == 0 or delta_next == 0:
        failures.append(
            InvariantFailure("I7", "volume movement must be non-zero in both minutes")
        )
    if delta_t != -delta_next:
        failures.append(InvariantFailure("I7", "volume deltas do not cancel"))
"""
    assert i7_enforcement in source
    assert (
        'REQUIRED_HIGHER_TIMEFRAMES: tuple[str, ...] = ("5m", "15m", "30m", "1h")'
        in source
    )
    edited = source.replace(i7_enforcement, "", 1).replace(
        'REQUIRED_HIGHER_TIMEFRAMES: tuple[str, ...] = ("5m", "15m", "30m", "1h")',
        "REQUIRED_HIGHER_TIMEFRAMES: tuple[str, ...] = ()",
        1,
    )
    edited_path = tmp_path / "adjacent_window_predicate_verifier_edits.py"
    edited_path.write_text(edited, encoding="utf-8")

    with pytest.raises(
        RuntimeError, match="specification changed|module source changed"
    ):
        import_predicate_copy(edited_path)


def test_predicate_is_pure_and_has_no_offset_search_or_match_rate_escape_hatch():
    source = PREDICATE_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.partition(".")[0])

    assert not imports.intersection(
        {"aiohttp", "app", "asyncpg", "httpx", "requests", "socket", "urllib"}
    )
    assert "best_offset" not in source
    assert "match_rate" not in source


def test_promotion_gate_is_registered_in_the_operator_runbook():
    runbook = PROMOTION_RUNBOOK.read_text(encoding="utf-8")

    assert "KR_1M_SOURCE_SENSITIVITY_V1" in runbook
    assert "signal" in runbook
    assert "trade" in runbook
    assert "PnL" in runbook
    assert "canonical" in runbook
    assert "holdout and design session declarations" in runbook
    assert "raw comparison artifact and its SHA-256" in runbook
    assert "bucket count, compared" in runbook
    assert "검사 호출 제거" in runbook
