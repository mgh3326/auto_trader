"""Adversarial CI coverage for the KR-A0 exact packet binding.

The fixture bundle is self-contained.  Its hash-pinned reference generator
materializes the five base files and all seventeen variants into ``tmp_path``; no
corpus or holdout path is included in this test's input list.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import shutil
import sys
from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from research.kr_corpus.backtest.packet_engine import (
    COST_BASE,
    COST_SENSITIVITY,
    Bar,
    PacketEngine,
    PacketWorld,
    PacketWorldAdapter,
    RunInvalid,
    canonical_sample_stdev,
    clustered_lcb,
    compose_verdict,
    entry_ok,
    high_c,
    low_c,
    maturity_ok,
    membership_intervals_from_presence_parquet,
    membership_intervals_from_presence_table,
    pct_asc,
    rev3_rank_key,
    valid_bar,
)
from research.kr_corpus.registry.exact_binding import (
    CANDIDATES_SHA256,
    CONVENTION_SHA256,
    GOLDEN_V6_SHA256,
    ArtifactPaths,
    CandidateRegistry,
    NeedsUpstream,
    RegistryStartRejected,
    sha256_file,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "kr_a0_golden_v6"


@pytest.fixture(scope="session")
def generated_v6(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Execute the v6 generator on canonical plain CPython 3.13."""

    output = tmp_path_factory.mktemp("kr_a0_golden") / "vectors"
    output.mkdir()
    generator = output / "reference_generator_v4.py"
    shutil.copy2(FIXTURE_BUNDLE / "reference_generator_v4.py.fixture", generator)

    loader = SourceFileLoader("kr_a0_reference", str(generator))
    spec = importlib.util.spec_from_loader("kr_a0_reference", loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module.OUT = str(output)
    module.INBOX = str(FIXTURE_BUNDLE / "inbox")
    module.main()

    assert sha256_file(output / "golden_v6.json") == GOLDEN_V6_SHA256
    return output


@pytest.fixture
def artifact_paths(generated_v6: Path) -> ArtifactPaths:
    inbox = FIXTURE_BUNDLE / "inbox"
    return ArtifactPaths(
        candidates_yaml=FIXTURE_BUNDLE / "02-active-candidates.yaml",
        golden_v6=generated_v6 / "golden_v6.json",
        amendment_a1_a9=inbox / "amendment-kr-engine-conventions-v2-draft-20260805.md",
        amendment_a10_a12=inbox / "amendment-kr-engine-a10-a12-draft-20260805.md",
        amendment_a13_a14=inbox / "amendment-kr-engine-a13-a14-draft-20260805.md",
        amendment_a15=inbox / "amendment-kr-engine-a15-20260805.md",
        generator=generated_v6 / "reference_generator_v4.py",
        fixture_root=generated_v6,
    )


@pytest.fixture
def registry(artifact_paths: ArtifactPaths) -> CandidateRegistry:
    return CandidateRegistry.start(artifact_paths)


@pytest.fixture
def golden(generated_v6: Path) -> dict[str, Any]:
    return _strict_json_object(
        (generated_v6 / "golden_v6.json").read_text(encoding="utf-8")
    )


def _normalized(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


def _strict_json_object(source: str) -> dict[str, Any]:
    def reject(constant: str) -> None:
        raise ValueError(f"non-standard JSON constant {constant}")

    parsed = json.loads(source, parse_constant=reject)
    assert isinstance(parsed, dict)
    return parsed


def _decode_special_floats(value: Any) -> Any:
    if isinstance(value, dict):
        if value == {"special_float": "+inf"}:
            return float("inf")
        return {key: _decode_special_floats(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_special_floats(item) for item in value]
    return value


def _engine_run(
    registry: CandidateRegistry, root: Path, strategy_id: str = "rev3_reclaim"
):
    world = PacketWorldAdapter.from_fixture_directory(root)
    result = PacketEngine(registry).run(world, strategy_id)
    return world, result


def _baseline_key(market: str, symbol: str, entry_idx: int) -> str:
    return f"{market}:{symbol}@e{entry_idx}"


def test_v6_inputs_are_recomputed_and_generator_is_byte_exact(
    artifact_paths: ArtifactPaths,
    generated_v6: Path,
    registry: CandidateRegistry,
    golden: Mapping[str, Any],
) -> None:
    """Acceptance 1/2: all base, variant, convention, and generator pins fire."""

    assert (
        sha256_file(FIXTURE_BUNDLE / "02-active-candidates.yaml") == CANDIDATES_SHA256
    )
    assert sha256_file(generated_v6 / "golden_v6.json") == GOLDEN_V6_SHA256
    assert (
        sha256_file(generated_v6 / "reference_generator_v4.py")
        == CONVENTION_SHA256["generator"]
    )
    assert "golden v6" in (FIXTURE_BUNDLE / "CONTRACT.md").read_text(encoding="utf-8")
    assert registry.inputs.candidates_sha256 == golden["candidates_yaml_sha256"]
    assert registry.inputs.golden_sha256 == GOLDEN_V6_SHA256
    assert dict(registry.inputs.convention_sha256) == golden["convention_sha256"]
    assert dict(registry.inputs.fixture_sha256) == golden["fixture_sha256"]
    assert (
        dict(registry.inputs.variant_fixture_sha256) == golden["variant_fixture_sha256"]
    )
    assert len(registry.inputs.fixture_sha256) == 5
    assert len(registry.inputs.variant_fixture_sha256) == 17
    assert artifact_paths.fixture_root == generated_v6
    assert sys.version_info[:2] == (3, 13)
    assert sys.flags.optimize == 0


def test_v1_to_v5_primitives_match_exact_oracles(golden: Mapping[str, Any]) -> None:
    """V1–V5 are tested independently as well as through all candidate E2Es."""

    v1 = golden["V1_indicators"]
    closes = v1["r3"]["closes"]
    assert closes[-1] / closes[-4] - 1 == pytest.approx(v1["r3"]["expected_r3"])

    clamped = v1["clv_clamped"]
    bar = Bar(**clamped["bar"], volume=1)
    assert high_c(bar) == clamped["expected_high_c"]
    assert low_c(bar) == clamped["expected_low_c"]
    assert (bar.close - low_c(bar)) / (high_c(bar) - low_c(bar)) == clamped[
        "expected_clv"
    ]

    undefined = Bar(**v1["clv_undefined"]["bar"], volume=1)
    assert high_c(undefined) == low_c(undefined)
    assert v1["clv_undefined"]["expected_signal"] is None

    volume = v1["volume_ratio"]
    assert statistics_median(volume["prior20_volumes"]) == volume["expected_median"]
    assert volume["volume_t"] / volume["expected_median"] == volume["expected_ratio"]

    margin = v1["brk20_margin"]
    assert max(margin["prior20_high_c"]) == margin["expected_prior_high20"]
    assert (
        margin["close_t"] / margin["expected_prior_high20"] - 1
        == margin["expected_margin"]
    )
    assert not valid_bar(Bar(open=1.0, high=1.0, low=1.0, close=1.0, volume=0))
    assert valid_bar(Bar(open=2.0, high=1.0, low=1.0, close=1.0, volume=1))

    v2 = golden["V2_percentile"]
    values = v2["pct_asc_with_ties"]["values"]
    assert [pct_asc(values, value) for value in values] == v2["pct_asc_with_ties"][
        "expected_pct"
    ]
    assert all(v2["exact_thresholds"].values())
    assert v2["population_floor"]["population_size"] < v2["population_floor"]["floor"]

    ranking = golden["V3_ranking_selection"]["rev3_ranking"]
    assert [
        row["symbol"] for row in sorted(ranking["rows"], key=rev3_rank_key)
    ] == ranking["expected_order_literal"]

    v4 = golden["V4_execution"]
    assert 0.05 - COST_BASE == v4["normal_fill"]["net_43bp"]
    assert 0.05 - COST_SENSITIVITY == v4["normal_fill"]["net_83bp"]
    assert v4["delisted_no_price"]["expected_gross"] == -1.0

    v5 = golden["V5_gates"]["clustered_lcb_n36"]
    assert canonical_sample_stdev(v5["session_means"]) == v5["expected_sd"]
    assert clustered_lcb(v5["session_means"]) == v5["expected_lcb"]
    assert (clustered_lcb(v5["session_means"]) > 0) is v5["expected_gate_pass"]


def test_a15_predicate_oracles_decode_tagged_infinity_and_reject_bare_output(
    generated_v6: Path, golden: Mapping[str, Any]
) -> None:
    """All 14 real predicate calls include the tagged ``+inf`` wire form."""

    raw = (generated_v6 / "golden_v6.json").read_text(encoding="utf-8")
    assert "Infinity" not in raw
    assert "NaN" not in raw
    assert raw.count('"special_float": "+inf"') == 3

    cases = golden["E2E"]["predicate_oracles"]["maturity_entry"]["cases"]
    assert len(cases) == 14
    for case in cases:
        decoded = _decode_special_floats(case["input_bar"])
        bar = Bar(**decoded)
        assert maturity_ok(bar) is case["maturity_ok"], case["case"]
        assert entry_ok(bar) is case["entry_ok"], case["case"]

    # Bare non-standard JSON constants are a mutation, not an alternative wire
    # format.  The exact same strict parser used for golden loading kills it.
    bare_infinity = json.dumps({"input_bar": {"close": float("inf")}})
    with pytest.raises(ValueError, match="non-standard JSON constant Infinity"):
        _strict_json_object(bare_infinity)


def test_all_three_packet_e2es_are_json_normalized_exact(
    generated_v6: Path,
    registry: CandidateRegistry,
    golden: Mapping[str, Any],
) -> None:
    engine = PacketEngine(registry)
    candidates = (
        ("rev3_reclaim", "rev3"),
        ("brk20_confirm", "brk20"),
        ("lowvol_up_month", "lowvol"),
    )
    for strategy_id, golden_key in candidates:
        world = PacketWorldAdapter.from_fixture_directory(generated_v6)
        result = engine.run(world, strategy_id)
        expected = golden["E2E"][golden_key]
        actual = result.golden_payload()
        expected_keys = ("log", "trades", "baselines", "verdict")
        if golden_key != "rev3":
            expected_keys = ("log", "trades")
        for key in expected_keys:
            assert _normalized(actual[key]) == expected[key]
        stamped = result.to_dict()
        assert stamped["strategy_id"] == strategy_id
        assert (
            stamped["contract_hash"] == registry.binding_for(strategy_id).contract_hash
        )
        assert stamped["log"]["contract_hash"] == stamped["contract_hash"]
        assert stamped["verdict"]["strategy_id"] == strategy_id
        assert all(
            trade["contract_hash"] == stamped["contract_hash"]
            for trade in stamped["trades"]
        )
        assert world.spy.total_oob_reads == golden["access_spy_oob_reads"] == 0

    rev_world = PacketWorldAdapter.from_fixture_directory(generated_v6)
    assert (
        engine.signals(rev_world, "rev3_reclaim", 35)[0]
        == golden["E2E"]["rev3"]["signals_s35_per_market_proof"]
    )
    assert (
        engine.signals(rev_world, "rev3_reclaim", 45)[0]
        == golden["E2E"]["rev3"]["signals_s45_exact_clv_ratio"]
    )
    assert (
        engine.signals(rev_world, "rev3_reclaim", 49)[0]
        == golden["E2E"]["rev3"]["signals_s49_exact_boundary"]
    )
    assert (
        engine.signals(
            PacketWorldAdapter.from_fixture_directory(generated_v6), "brk20_confirm", 38
        )[0]
        == golden["E2E"]["brk20"]["signals_s38_exact_clv_ratio"]
    )
    assert (
        engine.signals(
            PacketWorldAdapter.from_fixture_directory(generated_v6),
            "lowvol_up_month",
            44,
        )[0]
        == golden["E2E"]["lowvol"]["signals_s44_exact_vol20_pct"]
    )


def test_compose_oracle_and_entry_year_schema_are_exact(
    golden: Mapping[str, Any],
) -> None:
    """A11/A14: all four gates, full labels, and entry-year-only attribution."""

    oracle = golden["E2E"]["compose_oracle"]
    dates40 = _dates40()

    for expected_key, excess in (
        ("pass_case", 0.01),
        ("falsified_case", -0.01),
        ("cost_sensitive_case", 0.002),
    ):
        trades, baselines = _compose_case(40, [8] * 40, [excess] * 40)
        assert (
            _normalized(compose_verdict(dates40, trades, baselines))
            == oracle[expected_key]
        )

    trades, baselines = _compose_case(30, [10] * 30, [0.01] * 30)
    assert (
        _normalized(compose_verdict(dates40, trades, baselines))
        == oracle["boundary_filled_300"]
    )
    trades, baselines = _compose_case(30, [10] * 29 + [9], [0.01] * 30)
    assert (
        _normalized(compose_verdict(dates40, trades, baselines))
        == oracle["boundary_filled_299"]
    )
    trades, baselines = _compose_case(29, [11] * 29, [0.01] * 29)
    assert (
        _normalized(compose_verdict(dates40, trades, baselines))
        == oracle["boundary_clusters_29"]
    )
    trades, baselines = _compose_case(30, [10] * 30, [0.0] * 30)
    assert (
        _normalized(compose_verdict(dates40, trades, baselines))
        == oracle["boundary_zero_excess"]
    )

    for expected_key, positive_years in (
        ("boundary_years_6_full", 6),
        ("boundary_years_5_full", 5),
    ):
        excesses = [
            value
            for year in range(10)
            for value in ([0.01] if year < positive_years else [-0.001]) * 4
        ]
        trades, baselines = _compose_case(40, [8] * 40, excesses)
        assert (
            _normalized(compose_verdict(dates40, trades, baselines))
            == oracle[expected_key]
        )

    entry_dates = _entry_year_dates()
    trades, baselines = _compose_case(
        31,
        [8] * 31,
        [0.05] + [-0.001] * 30,
        include_exit_session=True,
    )
    assert all("entry_idx" in trade and "exit_session" in trade for trade in trades)
    assert all("exit_idx" not in trade for trade in trades)
    assert (
        _normalized(compose_verdict(entry_dates, trades, baselines))
        == oracle["entry_year_attribution"]["verdict"]
    )

    weighted_excesses = [0.04, -0.01, -0.01, -0.01] + [0.01] * 36
    weighted_counts = [1, 9, 9, 9] + [8] * 36
    trades, baselines = _compose_case(40, weighted_counts, weighted_excesses)
    weighted = compose_verdict(dates40, trades, baselines)
    assert (
        weighted["profiles"]["43bp"]["positive_years"]
        == oracle["year_weight_session_equal"]["positive_years"]
    )

    trades, baselines = _compose_case(40, [8] * 40, [0.01] * 40)
    baselines[_baseline_key("KOSPI", "000100", 0)] = {"gross_mean": None}
    with pytest.raises(RunInvalid, match="RUN_INVALID_EMPTY_BASELINE"):
        compose_verdict(dates40, trades, baselines)


def test_all_seventeen_variants_match_their_expected_outcome(
    generated_v6: Path,
    registry: CandidateRegistry,
    golden: Mapping[str, Any],
) -> None:
    expected = golden["E2E"]["variants"]
    assert set(expected) == set(registry.inputs.variant_fixture_sha256)
    assert len(expected) == 17
    _, base = _engine_run(registry, generated_v6)

    run_invalid = (
        "data_gap",
        "duplicate_row",
        "identity_conflict",
        "market_transfer",
        "market_transfer_missing_maturity",
    )
    assert len(run_invalid) == 5
    for name in run_invalid:
        with pytest.raises(RunInvalid) as caught:
            _engine_run(registry, generated_v6 / "variants" / name)
        assert caught.value.label == expected[name]

    for name in ("market_transfer", "market_transfer_missing_maturity"):
        with pytest.raises(RunInvalid) as caught:
            _engine_run(registry, generated_v6 / "variants" / name)
        assert caught.value.evidence["occurrence_count"] >= 1
        assert caught.value.evidence["affected_identities"][0]["market"] == "KOSDAQ"
        assert caught.value.evidence["affected_identities"][0]["symbol"] == "900350"

    missing_exit_variants = (
        "invalid_maturity",
        "invalid_maturity_close_blank",
        "invalid_maturity_volume_blank",
        "invalid_maturity_close_zero",
        "invalid_maturity_close_inf",
        "invalid_maturity_volume_zero",
        "invalid_maturity_volume_negative",
    )
    for name in missing_exit_variants:
        _, result = _engine_run(registry, generated_v6 / "variants" / name)
        actual = {
            "result": "COMPLETES_WITH_MISSING_EXIT",
            "missing_exit": [
                [item["symbol"], item["exit_due"]]
                for item in result.log["missing_exit"]
            ],
            "trades": len(result.trades),
            "final_verdict": result.verdict["verdict"],
            "missing_exit_ratio": result.verdict["missing_exit_ratio"],
        }
        expected_slice = {
            key: expected[name][key]
            for key in (
                "result",
                "missing_exit",
                "trades",
                "final_verdict",
                "missing_exit_ratio",
            )
        }
        assert actual == expected_slice

    _, entry_zero = _engine_run(
        registry, generated_v6 / "variants" / "entry_open_zero_no_fill"
    )
    entry_zero_expected = expected["entry_open_zero_no_fill"]
    assert len(entry_zero.trades) == entry_zero_expected["trades"]
    assert (
        sorted([item["session"], item["symbol"]] for item in entry_zero.log["no_fill"])
        == entry_zero_expected["no_fill"]
    )
    assert entry_zero.verdict["verdict"] == entry_zero_expected["final_verdict"]
    assert entry_zero.log["skipped_max10"] == []
    assert entry_zero.log["missing_exit"] == []

    _, nofill = _engine_run(
        registry, generated_v6 / "variants" / "nofill_no_substitution"
    )
    nofill_actual = {
        "QA6": "NO_FILL"
        if any(entry["symbol"] == "900360" for entry in nofill.log["no_fill"])
        else "UNEXPECTED",
        "KA5": "SKIPPED_MAX10 (no substitution)"
        if any(entry["symbol"] == "000350" for entry in nofill.log["skipped_max10"])
        else "UNEXPECTED",
        "trades": len(nofill.trades),
    }
    assert nofill_actual == expected["nofill_no_substitution"]

    _, all_invalid = _engine_run(
        registry, generated_v6 / "variants" / "data_gap_control_all_invalid"
    )
    assert {
        "result": "COMPLETES_NOT_DATA_GAP",
        "trades": len(all_invalid.trades),
        "note": "rows present but invalid -> universe exclusion, not RUN_INVALID_DATA_GAP",
    } == expected["data_gap_control_all_invalid"]

    _, stale = _engine_run(registry, generated_v6 / "variants" / "stale_delist_bar")
    ka4 = next(trade for trade in stale.trades if trade["symbol"] == "000340")
    stale_actual = {
        "result": "KA4_TRADE_IDENTICAL_TO_BASE",
        "qa5_cohort_baseline_exact": stale.baselines[
            _baseline_key("KOSDAQ", "900350", 29)
        ],
        "note": (
            "delist evidence precedes stale-valid scheduled maturity bar — for the "
            "strategy trade AND the A4 cohort member"
        ),
    }
    assert ka4 == next(trade for trade in base.trades if trade["symbol"] == "000340")
    assert _normalized(stale_actual) == expected["stale_delist_bar"]

    holdout_world, holdout = _engine_run(
        registry, generated_v6 / "variants" / "holdout_leg_control"
    )
    holdout_key = ("KOSDAQ", "900490")
    holdout_actual = {
        "result": "IDENTICAL_TO_BASE",
        "trades": len(holdout.trades),
        "oob_reads": holdout_world.spy.total_oob_reads,
        "holdout_key_in_symbols": holdout_key in holdout_world.symbols,
        "holdout_key_in_membership": holdout_key in holdout_world.membership,
        "note": (
            "membership leg starting beyond exploration_end is structurally invisible —"
            " implementations must assert the clamped symbol/membership key sets"
        ),
    }
    assert _normalized(list(holdout.trades)) == _normalized(list(base.trades))
    assert holdout_actual == expected["holdout_leg_control"]


def test_mutations_are_each_killed_by_the_golden_vectors(
    generated_v6: Path,
    registry: CandidateRegistry,
    golden: Mapping[str, Any],
) -> None:
    """Bidirectional proof: known wrong implementations do not pass golden v6."""

    expected = golden["E2E"]["rev3"]

    class StrictComparatorMutant(PacketEngine):
        def _at_least(self, actual: float, threshold: float) -> bool:
            return actual > threshold

        def _at_most(self, actual: float, threshold: float) -> bool:
            return actual < threshold

    class RawHighMutant(PacketEngine):
        def _high_c(self, bar: Bar) -> float:
            assert bar.high is not None
            return bar.high

    class GlobalPercentileMutant(PacketEngine):
        def _per_market_percentiles(
            self, values_by_market: Mapping[str, Mapping[tuple[str, str], float]]
        ) -> dict[tuple[str, str], float]:
            keys = tuple(key for values in values_by_market.values() for key in values)
            values = [
                value
                for market_values in values_by_market.values()
                for value in market_values.values()
            ]
            return {
                key: pct_asc(values, value)
                for key, value in zip(keys, values, strict=True)
            }

    class FullBarExecutionMutant(PacketEngine):
        def _entry_ok(self, bar: Bar | None) -> bool:
            return valid_bar(bar)

        def _maturity_ok(self, bar: Bar | None) -> bool:
            return valid_bar(bar)

    for mutant in (StrictComparatorMutant, GlobalPercentileMutant):
        world = PacketWorldAdapter.from_fixture_directory(generated_v6)
        result = mutant(registry).run(world, "rev3_reclaim")
        actual = result.golden_payload()
        assert any(
            _normalized(actual[key]) != expected[key]
            for key in ("log", "trades", "baselines", "verdict")
        )

    raw_high = (
        RawHighMutant(registry)
        .run(PacketWorldAdapter.from_fixture_directory(generated_v6), "brk20_confirm")
        .golden_payload()
    )
    raw_high_expected = golden["E2E"]["brk20"]
    assert any(
        _normalized(raw_high[key]) != raw_high_expected[key]
        for key in ("log", "trades")
    )

    full_bar = FullBarExecutionMutant(registry).run(
        PacketWorldAdapter.from_fixture_directory(generated_v6), "rev3_reclaim"
    )
    assert _normalized(full_bar.golden_payload()) != _normalized(expected)
    assert full_bar.log["missing_exit"]

    class NoFillSubstitutionMutant(PacketEngine):
        def _substitute_after_no_fill(self) -> bool:
            return True

    nofill = NoFillSubstitutionMutant(registry).run(
        PacketWorldAdapter.from_fixture_directory(
            generated_v6 / "variants" / "nofill_no_substitution"
        ),
        "rev3_reclaim",
    )
    assert any(trade["symbol"] == "000350" for trade in nofill.trades)

    class SymbolOnlyIdentityMutant(PacketEngine):
        def _check_identity(self, world: PacketWorld) -> None:
            # An identity keyed by (market, symbol) cannot see cross-market overlap.
            assert len(set(world.membership)) == len(world.membership)

    result = SymbolOnlyIdentityMutant(registry).run(
        PacketWorldAdapter.from_fixture_directory(
            generated_v6 / "variants" / "identity_conflict"
        ),
        "rev3_reclaim",
    )
    assert len(result.trades) == 17


def test_transfer_precedence_mutants_are_killed_in_both_directions(
    generated_v6: Path, registry: CandidateRegistry
) -> None:
    class InvalidOnlyTransferMutant(PacketEngine):
        def _transfer_gate(
            self, world: PacketWorld, position: Mapping[str, Any]
        ) -> bool:
            due = int(position["exit_due"])
            return not self._maturity_ok(
                world.bar(position["key"], due)
            ) and super()._transfer_gate(world, position)

    class MaturityFirstTransferMutant(PacketEngine):
        def _transfer_precedes_maturity(self) -> bool:
            return False

    valid_transfer = generated_v6 / "variants" / "market_transfer"
    missing_transfer = generated_v6 / "variants" / "market_transfer_missing_maturity"
    completed = InvalidOnlyTransferMutant(registry).run(
        PacketWorldAdapter.from_fixture_directory(valid_transfer), "rev3_reclaim"
    )
    assert len(completed.trades) == 17
    maturity_first = MaturityFirstTransferMutant(registry).run(
        PacketWorldAdapter.from_fixture_directory(missing_transfer), "rev3_reclaim"
    )
    assert maturity_first.log["missing_exit"]
    assert maturity_first.verdict["verdict"] == "INCONCLUSIVE_MISSING_EXITS"


def test_a15_mutations_are_each_killed_by_ci(
    generated_v6: Path, registry: CandidateRegistry, golden: Mapping[str, Any]
) -> None:
    """A15's count, ordering, finite-positive, and JSON guards all fire."""

    class CountDropMutant(PacketEngine):
        def _compose_verdict(
            self,
            dates: Mapping[int, date],
            trades: Iterable[Mapping[str, Any]],
            baselines: Mapping[str, Mapping[str, Any]],
            *,
            missing_exit_count: int,
        ) -> dict[str, Any]:
            return super()._compose_verdict(
                dates,
                trades,
                baselines,
                missing_exit_count=0,
            )

    missing_root = generated_v6 / "variants" / "invalid_maturity"
    dropped = CountDropMutant(registry).run(
        PacketWorldAdapter.from_fixture_directory(missing_root), "rev3_reclaim"
    )
    expected_missing = golden["E2E"]["variants"]["invalid_maturity"]
    assert dropped.verdict["missing_exit_count"] == 0
    assert dropped.verdict["verdict"] != expected_missing["final_verdict"]

    class NonNonePredicateMutant(PacketEngine):
        def _entry_ok(self, bar: Bar | None) -> bool:
            return bar is not None and bar.open is not None and bar.volume is not None

        def _maturity_ok(self, bar: Bar | None) -> bool:
            return bar is not None and bar.close is not None and bar.volume is not None

    # A zero entry open becomes a trade under the non-None mutant and reaches a
    # division by zero.  The real finite-and-positive predicate records NO_FILL.
    with pytest.raises(ZeroDivisionError):
        NonNonePredicateMutant(registry).run(
            PacketWorldAdapter.from_fixture_directory(
                generated_v6 / "variants" / "entry_open_zero_no_fill"
            ),
            "rev3_reclaim",
        )

    class PriceAtLeastOneMutant(PacketEngine):
        def _entry_ok(self, bar: Bar | None) -> bool:
            return (
                bar is not None
                and bar.open is not None
                and bar.open >= 1
                and bar.volume is not None
                and bar.volume > 0
            )

        def _maturity_ok(self, bar: Bar | None) -> bool:
            return (
                bar is not None
                and bar.close is not None
                and bar.close >= 1
                and bar.volume is not None
                and bar.volume > 0
            )

    class VolumeAtLeastTwoMutant(PacketEngine):
        def _entry_ok(self, bar: Bar | None) -> bool:
            return (
                bar is not None
                and bar.open is not None
                and bar.open > 0
                and bar.volume is not None
                and bar.volume >= 2
            )

        def _maturity_ok(self, bar: Bar | None) -> bool:
            return (
                bar is not None
                and bar.close is not None
                and bar.close > 0
                and bar.volume is not None
                and bar.volume >= 2
            )

    small_case = next(
        case
        for case in golden["E2E"]["predicate_oracles"]["maturity_entry"]["cases"]
        if case["case"] == "valid_small_positive"
    )
    small_bar = Bar(**_decode_special_floats(small_case["input_bar"]))
    assert entry_ok(small_bar) and maturity_ok(small_bar)
    for mutant in (PriceAtLeastOneMutant(registry), VolumeAtLeastTwoMutant(registry)):
        assert not mutant._entry_ok(small_bar)
        assert not mutant._maturity_ok(small_bar)

    with pytest.raises(ValueError, match="non-standard JSON constant Infinity"):
        _strict_json_object(json.dumps({"result": float("inf")}))


def test_presence_native_membership_and_absent_sidecar_do_not_infer_delisting(
    generated_v6: Path, registry: CandidateRegistry, tmp_path: Path
) -> None:
    """A15-1/2: observation presence is not a hidden delist status channel."""

    table = pa.table(
        {
            "market": ["KOSPI", "KOSPI", "KOSPI", "KOSPI"],
            "symbol": ["000001", "000001", "000001", "000001"],
            "session_idx": [0, 1, 3, 4],
            # The adapter deliberately does not consume this legacy-shaped
            # column; only separate DART evidence can form C8.
            "status": ["delisted", "delisted", "delisted", "delisted"],
        }
    )
    expected_intervals = {("KOSPI", "000001"): ((0, 1), (3, 4))}
    assert (
        membership_intervals_from_presence_table(table, exploration_end_session_idx=4)
        == expected_intervals
    )
    presence_parquet = tmp_path / "membership-presence.parquet"
    pq.write_table(table, presence_parquet)
    assert (
        membership_intervals_from_presence_parquet(
            presence_parquet, exploration_end_session_idx=4
        )
        == expected_intervals
    )
    with pytest.raises(RunInvalid, match="presence-only rows required"):
        membership_intervals_from_presence_table(
            pa.table(
                {
                    "market": ["KOSPI"],
                    "symbol": ["000001"],
                    "start_idx": [0],
                    "end_idx": [4],
                }
            ),
            exploration_end_session_idx=4,
        )

    class PresenceOnlySource:
        def __init__(self, directory: Path) -> None:
            self.sessions = _csv_rows(directory / "fixture_sessions.csv")
            self.bars = _csv_rows(directory / "fixture_bars.csv")
            self.membership = []
            for row in _csv_rows(directory / "fixture_membership.csv"):
                for session_idx in range(
                    int(row["start_idx"]), int(row["end_idx"]) + 1
                ):
                    self.membership.append(
                        {
                            "market": row["market"],
                            "symbol": row["symbol"],
                            "session_idx": session_idx,
                        }
                    )

        def fetch_sessions(self, *, max_session_idx: int):
            return [
                row
                for row in self.sessions
                if int(row["session_idx"]) <= max_session_idx
            ]

        def fetch_bars(self, *, max_session_idx: int):
            return [
                row for row in self.bars if int(row["session_idx"]) <= max_session_idx
            ]

        def fetch_membership(self, *, max_session_idx: int):
            return [
                row
                for row in self.membership
                if int(row["session_idx"]) <= max_session_idx
            ]

    # No fetch_delist_events method: the DART sidecar is intentionally absent.
    world = PacketWorldAdapter.from_source(
        PresenceOnlySource(generated_v6), exploration_end_session_idx=52
    )
    assert world.delist_events == {}
    result = PacketEngine(registry).run(world, "rev3_reclaim")
    assert result.log["missing_exit"]
    assert not any(trade["delisted_exit"] for trade in result.trades)
    assert result.verdict["verdict"] == "INCONCLUSIVE_MISSING_EXITS"


def test_dated_delist_sidecar_is_explicit_evidence(
    generated_v6: Path, tmp_path: Path
) -> None:
    """The production-shaped ``delist_date`` + reason sidecar maps explicitly."""

    dated = tmp_path / "dated-sidecar"
    shutil.copytree(generated_v6, dated)
    dates = {
        int(row["session_idx"]): row["date_informational"]
        for row in _csv_rows(dated / "fixture_sessions.csv")
    }
    events = _csv_rows(dated / "fixture_delist_events.csv")
    with (dated / "fixture_delist_events.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["market", "symbol", "delist_date", "reason"],
        )
        writer.writeheader()
        for event in events:
            writer.writerow(
                {
                    "market": event["market"],
                    "symbol": event["symbol"],
                    "delist_date": dates[int(event["delist_session_idx"])],
                    "reason": "sidecar-evidence",
                }
            )
    assert PacketWorldAdapter.from_fixture_directory(dated).delist_events == (
        PacketWorldAdapter.from_fixture_directory(generated_v6).delist_events
    )


def test_access_spy_and_source_materialization_are_boundary_sealed(
    generated_v6: Path, registry: CandidateRegistry
) -> None:
    """Acceptance 5/6: query and materialization both carry the <= end predicate."""

    class SourceSpy:
        def __init__(self, directory: Path) -> None:
            self.calls: list[tuple[str, int]] = []
            self.sessions = _csv_rows(directory / "fixture_sessions.csv")
            self.bars = _csv_rows(directory / "fixture_bars.csv")
            self.membership = []
            for row in _csv_rows(directory / "fixture_membership.csv"):
                for session_idx in range(
                    int(row["start_idx"]), int(row["end_idx"]) + 1
                ):
                    self.membership.append(
                        {
                            "market": row["market"],
                            "symbol": row["symbol"],
                            "session_idx": session_idx,
                        }
                    )
            self.delist = _csv_rows(directory / "fixture_delist_events.csv")

        def fetch_sessions(self, *, max_session_idx: int):
            self.calls.append(("sessions", max_session_idx))
            return [
                row
                for row in self.sessions
                if int(row["session_idx"]) <= max_session_idx
            ]

        def fetch_bars(self, *, max_session_idx: int):
            self.calls.append(("bars", max_session_idx))
            return [
                row for row in self.bars if int(row["session_idx"]) <= max_session_idx
            ]

        def fetch_membership(self, *, max_session_idx: int):
            self.calls.append(("membership", max_session_idx))
            return [
                row
                for row in self.membership
                if int(row["session_idx"]) <= max_session_idx
            ]

        def fetch_delist_events(self, *, max_session_idx: int):
            self.calls.append(("delist", max_session_idx))
            return [
                row
                for row in self.delist
                if int(row["delist_session_idx"]) <= max_session_idx
            ]

    source = SourceSpy(generated_v6 / "variants" / "holdout_leg_control")
    world = PacketWorldAdapter.from_source(source, exploration_end_session_idx=52)
    assert source.calls == [
        ("sessions", 52),
        ("bars", 52),
        ("membership", 52),
        ("delist", 52),
    ]
    assert all(session <= 52 for (_, session) in world.bars)
    assert all(
        end <= 52 for intervals in world.membership.values() for _, end in intervals
    )
    assert ("KOSDAQ", "900490") not in world.symbols
    assert ("KOSDAQ", "900490") not in world.membership
    assert world.spy.source_oob_records == 0
    result = PacketEngine(registry).run(world, "rev3_reclaim")
    assert len(result.trades) == 17
    assert world.spy.total_oob_reads == 0

    class BoundaryReadMutant(PacketEngine):
        def _execute_candidate(self, world: PacketWorld, binding):
            world.bar(world.symbols[0], world.exploration_end_session_idx + 1)
            return super()._execute_candidate(world, binding)

    with pytest.raises(RunInvalid, match="RUN_INVALID_BOUNDARY_ACCESS"):
        BoundaryReadMutant(registry).run(
            PacketWorldAdapter.from_fixture_directory(generated_v6), "rev3_reclaim"
        )


def test_tamper_and_fallback_paths_refuse_registry_startup(
    generated_v6: Path, tmp_path: Path
) -> None:
    """Acceptance 7: input tamper, golden mismatch, and fallback all fail closed."""

    mutable = tmp_path / "mutable_bundle"
    shutil.copytree(generated_v6, mutable)
    shutil.copy2(
        FIXTURE_BUNDLE / "02-active-candidates.yaml",
        mutable / "02-active-candidates.yaml",
    )
    shutil.copytree(FIXTURE_BUNDLE / "inbox", mutable / "inbox")
    paths = ArtifactPaths.from_fixture_bundle(mutable)
    registry = CandidateRegistry.start(paths)
    with pytest.raises(RegistryStartRejected, match="shared fallback is forbidden"):
        registry.binding_for("calculate_signal")

    cases = (
        ("02-active-candidates.yaml", b"\n# tamper\n"),
        ("golden_v6.json", b" "),
        ("fixture_bars.csv", b"\n"),
    )
    for relative, addition in cases:
        case_root = tmp_path / relative.replace("/", "_")
        shutil.copytree(mutable, case_root)
        target = case_root / relative
        target.write_bytes(target.read_bytes() + addition)
        with pytest.raises(RegistryStartRejected):
            CandidateRegistry.start(ArtifactPaths.from_fixture_bundle(case_root))

    with pytest.raises(
        NeedsUpstream, match="NEEDS_UPSTREAM\\(percentile convention 충돌\\)"
    ):
        CandidateRegistry.start(
            paths, observed_percentile_convention="global_rank_average"
        )


def test_new_packet_surface_does_not_import_legacy_or_runtime_paths() -> None:
    """Acceptance 8 and legacy isolation are statically visible in CI."""

    new_sources = (
        REPO_ROOT / "research" / "kr_corpus" / "registry" / "exact_binding.py",
        REPO_ROOT / "research" / "kr_corpus" / "backtest" / "packet_engine.py",
    )
    combined = "\n".join(source.read_text(encoding="utf-8") for source in new_sources)
    assert "research.three_market_shadow" not in combined
    assert ".stage_b" not in combined
    assert "calculate_signal" not in combined
    assert "sqlalchemy" not in combined
    assert "taskiq" not in combined.casefold()


def _dates40() -> dict[int, date]:
    result: dict[int, date] = {}
    index = 0
    for year in range(2015, 2025):
        for quarter in range(4):
            result[index] = date(year, 3 + quarter, 1)
            index += 1
    return result


def _entry_year_dates() -> dict[int, date]:
    result = {0: date(2015, 12, 30)}
    cursor = date(2016, 1, 4)
    while len(result) < 31:
        if cursor.weekday() < 5:
            result[len(result)] = cursor
        cursor += timedelta(days=7)
    return result


def _compose_case(
    sessions: int,
    per_counts: list[int],
    excesses: list[float],
    *,
    include_exit_session: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, float]]]:
    trades: list[dict[str, Any]] = []
    baselines: dict[str, dict[str, float]] = {}
    for session in range(sessions):
        for symbol_index in range(per_counts[session]):
            symbol = f"{100 + symbol_index:06d}"
            trade = {
                "market": "KOSPI",
                "symbol": symbol,
                "entry_idx": session,
                "gross": excesses[session] + COST_BASE,
            }
            if include_exit_session:
                trade["exit_session"] = min(session + 1, sessions - 1)
            trades.append(trade)
            baselines[_baseline_key("KOSPI", symbol, session)] = {"gross_mean": 0.0}
    return trades, baselines


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def statistics_median(values: Iterable[int]) -> float:
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2
