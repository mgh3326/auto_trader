"""ROB-983 (H5, CP7) -- JSON-only Markdown renderer and contract-fixture
scorecard smoke.

``render_markdown`` accepts only ``json.loads(canonical_json_bytes)``,
never raw H4/H6-A/DB objects, and never recomputes a metric. Explicit
display order + dict-construction-order reversal produce byte-identical
Markdown; nonzero rejections/attribution bins/exit reasons/direct verdicts/
incomplete reasons survive the JSON->Markdown boundary; presentation
changes can never move the semantic hash. The smoke section runs a
deterministic, non-vacuous contract-fixture pipeline (contracts ->
dual_evidence -> gates -> S3/S4 falsification -> canonical -> markdown)
covering every campaign-decision branch.

``contract_fixture_scorecard_smoke=PASS`` / ``actual_h4_contract=
FIXTURE_ONLY`` / ``actual_h6a_contract=FIXTURE_ONLY`` /
``db_sessions_created=0`` / ``db_queries=0`` / ``db_writes=0`` /
``commit_calls=0`` / ``rollback_calls=0`` / ``empirical_runs=0`` /
``corpus_campaign_runs=0`` / ``physical_stage_publish_calls=0`` -- this
module contains no DB engine/session, no broker/network call, and never
stages/publishes a physical file; it only builds in-memory dataclasses and
returns bytes.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from rob974_h5_canonical import (
    StrategyCanonicalInputs,
    canonical_json_bytes,
    hash_canonical_bytes,
)
from rob974_h5_canonical import (
    build_canonical_scorecard as _build_canonical_scorecard,
)
from rob974_h5_contracts import (
    FOLD_IDS,
    CampaignEnvelope,
    H5InputError,
    H6AAccountingSeal,
    MetricTrade,
    fixture_h4_attribution_result,
)
from rob974_h5_dual_evidence import (
    PathInvocationEvidence,
    PboEvidence,
    UniqueGeneratorEvidence,
)
from rob974_h5_gates import evaluate_common_gates
from rob974_h5_markdown import render_markdown
from rob974_h5_s3 import evaluate_s3_falsification
from rob974_h5_s4 import (
    S4_HISTORICAL_PAIR_EXECUTOR_STATE,
    StrategyRankMetrics,
    compute_campaign_decision,
    compute_direct_verdict,
    evaluate_s4_falsification,
)

_HEX64_A = "a" * 64
_HEX64_B = "b" * 64
_HEX64_C = "c" * 64
_DAY_MS = 24 * 60 * 60 * 1000
_MONTH_MS = 30 * _DAY_MS


def build_canonical_scorecard(**kwargs):
    """All predecessor CP7 cases are explicitly fixture-provenance cases."""
    return _build_canonical_scorecard(
        h4_attribution=fixture_h4_attribution_result(), **kwargs
    )


def _envelope(**overrides) -> CampaignEnvelope:
    ids = tuple(f"S3-{i:02d}" for i in range(24)) + tuple(
        f"S4-{i:02d}" for i in range(24)
    )
    fields = {
        "full_campaign_hash": _HEX64_A,
        "campaign_run_id": "run-cp7",
        "parent_corpus_hash": _HEX64_A,
        "parent_projection_hash": _HEX64_A,
        "feature_contract_hash": _HEX64_A,
        "strategy_contract_hashes": {"S3": _HEX64_A, "S4": _HEX64_B},
        "h4_runner_source_hash": _HEX64_A,
        "h4_pbo_source_hash": _HEX64_A,
        "h2_engine_source_hash": _HEX64_A,
        "h3_generator_source_hash": _HEX64_A,
        "run_schema_version": "v1",
        "generator_version": "g1",
        "expected_experiment_ids": ids,
        "h6a_trial_accounting_hash": _HEX64_B,
    }
    fields.update(overrides)
    return CampaignEnvelope(**fields)


def _seal(**overrides) -> H6AAccountingSeal:
    fields = {
        "expected_total": 48,
        "registered_total": 48,
        "primary_attempts": 48,
        "status_counts": {"completed": 48, "rejected": 0, "crashed": 0, "timeout": 0},
        "retry_attempts": 0,
        "accounting_complete": True,
        "performance_usable": True,
        "trial_accounting_hash": _HEX64_B,
        "reason_codes": (),
    }
    fields.update(overrides)
    return H6AAccountingSeal(**fields)


def _s3_trade(
    fold_id,
    net_bps,
    *,
    exit_ts=0,
    exit_reason="TP",
    dimension="XRPUSDT",
    path_scenario="primary_stress17",
    holding_minutes=10.0,
):
    return MetricTrade(
        strategy="S3",
        config_id="S3-00",
        fold_id=fold_id,
        path_scenario=path_scenario,
        dimension=dimension,
        direction="long",
        entry_ts=exit_ts,
        exit_ts=exit_ts + 60_000,
        holding_minutes=holding_minutes,
        exit_reason=exit_reason,
        gross_bps=net_bps + 5.0,
        net_bps=net_bps,
        tp_bps=68.0,
        sl_bps=40.0,
        gross_notional=None,
        market_return_4h=0.01,
        volatility_percentile=50.0,
    )


def _s4_trade(
    fold_id,
    net_bps,
    *,
    exit_ts=0,
    exit_reason="TP",
    dimension="XRP-DOGE",
    path_scenario="primary_stress17",
    holding_minutes=10.0,
    market_return_4h=0.01,
):
    return MetricTrade(
        strategy="S4",
        config_id="S4-00",
        fold_id=fold_id,
        path_scenario=path_scenario,
        dimension=dimension,
        direction="long",
        entry_ts=exit_ts,
        exit_ts=exit_ts + 60_000,
        holding_minutes=holding_minutes,
        exit_reason=exit_reason,
        gross_bps=net_bps + 5.0,
        net_bps=net_bps,
        tp_bps=68.0,
        sl_bps=40.0,
        gross_notional=100.0,
        market_return_4h=market_return_4h,
        volatility_percentile=None,
    )


def _s3_primary_trades(
    n: int = 40, exit_reason: str = "TP", net_bps: float = 10.0
) -> tuple:
    return tuple(
        _s3_trade(FOLD_IDS[i % 8], net_bps, exit_ts=i, exit_reason=exit_reason)
        for i in range(n)
    )


def _s3_upward_trades(n: int = 10, net_bps: float = 10.0) -> tuple:
    return tuple(
        _s3_trade(FOLD_IDS[i % 8], net_bps, exit_ts=i, path_scenario="upward_stress22")
        for i in range(n)
    )


def _s4_primary_trades(
    n: int = 40, exit_reason: str = "TP", net_bps: float = 10.0
) -> tuple:
    return tuple(
        _s4_trade(FOLD_IDS[i % 8], net_bps, exit_ts=i, exit_reason=exit_reason)
        for i in range(n)
    )


def _s4_upward_trades(n: int = 10, net_bps: float = 10.0) -> tuple:
    return tuple(
        _s4_trade(FOLD_IDS[i % 8], net_bps, exit_ts=i, path_scenario="upward_stress22")
        for i in range(n)
    )


def _s3_unique_evidence() -> UniqueGeneratorEvidence:
    return UniqueGeneratorEvidence(
        strategy="S3",
        config_id="S3-00",
        fold_id="fold-00",
        accepted=90,
        rejected=10,
        accepted_input_hash=_HEX64_A,
        rejection_reason_histogram={"lookahead_bar": 6, "signal_close_fill": 4},
    )


def _s3_path_evidence(path_scenario: str) -> PathInvocationEvidence:
    return PathInvocationEvidence(
        strategy="S3",
        config_id="S3-00",
        fold_id="fold-00",
        path_scenario=path_scenario,
        unique_evidence_hash=_HEX64_A,
        unique_evidence_accepted_count=90,
        engine_input_hash=_HEX64_B,
        engine_input_count=90,
        no_trade_reason_counts={"unpriced_gap": 3},
        ledger_status="completed",
        trade_count=40,
        artifact_hash=_HEX64_C,
    )


def _s3_pbo() -> PboEvidence:
    return PboEvidence(
        strategy="S3",
        config_count=24,
        day_count=365,
        slices=4,
        scenario_name="primary_stress17",
        value=0.35,
        reason_codes=(),
        source_hash=_HEX64_A,
        input_hash=_HEX64_B,
        artifact_hash=_HEX64_C,
    )


def _build_s3_inputs(*, with_evidence: bool = True) -> StrategyCanonicalInputs:
    primary = _s3_primary_trades()
    common_gates = evaluate_common_gates(
        primary_trades=primary, upward_trades=_s3_upward_trades(10)
    )
    falsification = evaluate_s3_falsification(
        primary_trades=primary, upward_trades=_s3_upward_trades(10)
    )
    direct_verdict = compute_direct_verdict(
        incomplete_reasons=falsification.incomplete_reasons,
        hard_gate_reasons=common_gates.reasons + falsification.reasons,
    )
    if with_evidence:
        unique_by_key = {("S3-00", "fold-00"): _s3_unique_evidence()}
        paths_by_key = {
            ("S3-00", "fold-00", "base13"): _s3_path_evidence("base13"),
            ("S3-00", "fold-00", "primary_stress17"): _s3_path_evidence(
                "primary_stress17"
            ),
            ("S3-00", "fold-00", "upward_stress22"): _s3_path_evidence(
                "upward_stress22"
            ),
        }
        pbo = _s3_pbo()
    else:
        unique_by_key, paths_by_key, pbo = {}, {}, None
    return StrategyCanonicalInputs(
        strategy="S3",
        common_gates=common_gates,
        falsification=falsification,
        direct_verdict=direct_verdict,
        exit_reason_order=("TP", "SL", "THESIS_EXIT", "TIMEOUT"),
        dimension_order=("XRPUSDT", "DOGEUSDT", "SOLUSDT"),
        unique_by_key=unique_by_key,
        paths_by_key=paths_by_key,
        pbo=pbo,
    )


def _build_s4_inputs() -> StrategyCanonicalInputs:
    primary = _s4_primary_trades()
    common_gates = evaluate_common_gates(
        primary_trades=primary, upward_trades=_s4_upward_trades(10)
    )
    falsification = evaluate_s4_falsification(
        primary_trades=primary, upward_trades=_s4_upward_trades(10)
    )
    direct_verdict = compute_direct_verdict(
        incomplete_reasons=falsification.incomplete_reasons,
        hard_gate_reasons=common_gates.reasons + falsification.reasons,
    )
    return StrategyCanonicalInputs(
        strategy="S4",
        common_gates=common_gates,
        falsification=falsification,
        direct_verdict=direct_verdict,
        exit_reason_order=("TP", "SL", "MEAN_EXIT", "STALL_EXIT", "TIMEOUT"),
        dimension_order=("XRP-DOGE", "XRP-SOL", "DOGE-SOL"),
        unique_by_key={},
        paths_by_key={},
        pbo=None,
        pair_executor_state=S4_HISTORICAL_PAIR_EXECUTOR_STATE,
    )


def _s3_clean_primary_trades(win_net: float) -> tuple:
    """Full 3-symbol coverage with a small first-4h-excluded loss tail --
    complete (no incomplete_reasons) regardless of ``win_net``. At
    ``win_net=40.0`` every common/falsification gate clears
    (historical_pass); at ``win_net=1.0`` E17/PF/positive-folds/monthly-
    concentration all fail while staying complete (historical_fail)."""
    trades = []
    for fi, fold_id in enumerate(FOLD_IDS):
        month_ts = fi * _MONTH_MS
        for symbol in ("XRPUSDT", "DOGEUSDT", "SOLUSDT"):
            for j in range(5):
                if j < 4:
                    net, reason, holding = win_net, "TP", 10.0
                else:
                    net, reason, holding = -5.0, "SL", 500.0
                trades.append(
                    _s3_trade(
                        fold_id,
                        net,
                        exit_ts=month_ts + j * _DAY_MS,
                        exit_reason=reason,
                        dimension=symbol,
                        holding_minutes=holding,
                    )
                )
    return tuple(trades)


def _s3_clean_upward_trades() -> tuple:
    return tuple(
        _s3_trade("fold-00", 20.0, exit_ts=i, path_scenario="upward_stress22")
        for i in range(5)
    )


def _s4_clean_primary_trades(win_net: float) -> tuple:
    """Full 3-pair coverage, a decorrelated market_return_4h pattern
    (period-2, independent of the win/loss period-5 cycle -- numerically
    verified |corr|<<0.15), and mid/slow holding-bucket filler trades
    (keeps the mid bucket's mean positive so slow-only-failure never
    fires). Complete regardless of ``win_net``."""
    trades = []
    idx = 0
    for fi, fold_id in enumerate(FOLD_IDS):
        month_ts = fi * _MONTH_MS
        for pair in ("XRP-DOGE", "XRP-SOL", "DOGE-SOL"):
            for j in range(5):
                if j < 4:
                    net, reason, holding = win_net, "TP", 10.0
                else:
                    net, reason, holding = -5.0, "SL", 500.0
                m = 0.01 if idx % 2 == 0 else 0.02
                trades.append(
                    _s4_trade(
                        fold_id,
                        net,
                        exit_ts=month_ts + j * _DAY_MS,
                        exit_reason=reason,
                        dimension=pair,
                        holding_minutes=holding,
                        market_return_4h=m,
                    )
                )
                idx += 1
    for fi, fold_id in enumerate(FOLD_IDS):
        month_ts = fi * _MONTH_MS
        trades.append(
            _s4_trade(
                fold_id,
                50.0,
                exit_ts=month_ts,
                dimension="XRP-DOGE",
                holding_minutes=1000.0,
            )
        )
        trades.append(
            _s4_trade(
                fold_id,
                10.0,
                exit_ts=month_ts + 1,
                dimension="XRP-DOGE",
                holding_minutes=2000.0,
            )
        )
    return tuple(trades)


def _s4_clean_upward_trades() -> tuple:
    return tuple(
        _s4_trade(
            "fold-00",
            20.0,
            exit_ts=i,
            market_return_4h=0.05,
            path_scenario="upward_stress22",
        )
        for i in range(5)
    )


def _s3_inputs_with_verdict(*, passing: bool) -> StrategyCanonicalInputs:
    primary = _s3_clean_primary_trades(40.0 if passing else 1.0)
    upward = _s3_clean_upward_trades()
    common_gates = evaluate_common_gates(primary_trades=primary, upward_trades=upward)
    falsification = evaluate_s3_falsification(
        primary_trades=primary, upward_trades=upward
    )
    direct_verdict = compute_direct_verdict(
        incomplete_reasons=falsification.incomplete_reasons,
        hard_gate_reasons=common_gates.reasons + falsification.reasons,
    )
    return StrategyCanonicalInputs(
        strategy="S3",
        common_gates=common_gates,
        falsification=falsification,
        direct_verdict=direct_verdict,
        exit_reason_order=("TP", "SL", "THESIS_EXIT", "TIMEOUT"),
        dimension_order=("XRPUSDT", "DOGEUSDT", "SOLUSDT"),
        unique_by_key={},
        paths_by_key={},
        pbo=None,
    )


def _s4_inputs_with_verdict(*, passing: bool) -> StrategyCanonicalInputs:
    primary = _s4_clean_primary_trades(40.0 if passing else 1.0)
    upward = _s4_clean_upward_trades()
    common_gates = evaluate_common_gates(primary_trades=primary, upward_trades=upward)
    falsification = evaluate_s4_falsification(
        primary_trades=primary, upward_trades=upward
    )
    direct_verdict = compute_direct_verdict(
        incomplete_reasons=falsification.incomplete_reasons,
        hard_gate_reasons=common_gates.reasons + falsification.reasons,
    )
    return StrategyCanonicalInputs(
        strategy="S4",
        common_gates=common_gates,
        falsification=falsification,
        direct_verdict=direct_verdict,
        exit_reason_order=("TP", "SL", "MEAN_EXIT", "STALL_EXIT", "TIMEOUT"),
        dimension_order=("XRP-DOGE", "XRP-SOL", "DOGE-SOL"),
        unique_by_key={},
        paths_by_key={},
        pbo=None,
        pair_executor_state=S4_HISTORICAL_PAIR_EXECUTOR_STATE,
    )


def _rank_metrics(common_gates, falsification) -> StrategyRankMetrics:
    # This session's clean fixtures are fold-symmetric by construction, so
    # min-fold E17 == pooled E17 exactly.
    return StrategyRankMetrics(
        min_fold_e17=common_gates.pooled_e17_bps,
        pooled_e17=common_gates.pooled_e17_bps,
        monthly_concentration=common_gates.monthly_concentration,
        timeout_ratio=falsification.pooled_timeout_ratio,
    )


def _build_full_scorecard() -> dict:
    s3_inputs = _build_s3_inputs(with_evidence=True)
    s4_inputs = _build_s4_inputs()
    campaign_decision = compute_campaign_decision(
        s3_direct_verdict=s3_inputs.direct_verdict,
        s4_direct_verdict=s4_inputs.direct_verdict,
    )
    return build_canonical_scorecard(
        envelope=_envelope(),
        h6a_seal=_seal(),
        envelope_ok=True,
        envelope_incomplete_reasons=(),
        s3_inputs=s3_inputs,
        s4_inputs=s4_inputs,
        campaign_decision=campaign_decision,
    )


class TestPureRendererBoundary:
    def test_renderer_accepts_only_json_loads_round_trip(self):
        scorecard = _build_full_scorecard()
        canonical_bytes = canonical_json_bytes(scorecard)
        decoded = json.loads(canonical_bytes)
        # Must not raise / must not require any object beyond the plain
        # decoded dict (no H4/H6-A/DB object is passed in).
        markdown_bytes = render_markdown(decoded)
        assert isinstance(markdown_bytes, bytes)
        assert markdown_bytes

    def test_rendering_never_changes_canonical_bytes_or_hash(self):
        scorecard = _build_full_scorecard()
        canonical_bytes_before = canonical_json_bytes(scorecard)
        hash_before = hash_canonical_bytes(canonical_bytes_before)
        render_markdown(json.loads(canonical_bytes_before))
        canonical_bytes_after = canonical_json_bytes(scorecard)
        assert canonical_bytes_after == canonical_bytes_before
        assert hash_canonical_bytes(canonical_bytes_after) == hash_before


class TestNonzeroContentSurvivesJsonToMarkdown:
    def setup_method(self):
        self.scorecard = _build_full_scorecard()
        self.markdown = render_markdown(
            json.loads(canonical_json_bytes(self.scorecard))
        ).decode("utf-8")

    def test_nonzero_rejections_survive(self):
        assert "rejected=10" in self.markdown
        assert "lookahead_bar=6" in self.markdown

    def test_attribution_bins_survive(self):
        assert "### Attribution: by_symbol" in self.markdown
        assert "XRPUSDT: trades=" in self.markdown

    def test_exit_reason_bins_survive(self):
        assert "### Attribution: by_exit_reason" in self.markdown
        assert "TP: trades=" in self.markdown

    def test_direct_verdicts_survive(self):
        # This fixture's single-symbol/single-pair, all-TP (no losses)
        # trades naturally produce "incomplete" direct verdicts (missing
        # symbol/pair evidence, undefined SL-dependence denominator) --
        # exercised deliberately so incomplete-reasons survival (below) has
        # real content, not a vacuous empty-list case.
        assert self.markdown.count("Direct Verdict: incomplete") == 2

    def test_incomplete_reasons_survive(self):
        # S3's first-4h SL denominator is undefined for an all-TP fixture
        # (zero losing trades) -- proves incomplete_reasons is not dropped.
        assert "s3_first_4h_sl_denominator_undefined" in self.markdown

    def test_pbo_and_pair_executor_state_survive(self):
        assert "### PBO" in self.markdown
        assert "value: 0.35" in self.markdown
        assert "### Pair Executor State (historical)" in self.markdown
        assert "readiness: historical_screen_only" in self.markdown

    def test_campaign_decision_survives(self):
        assert "campaign_decision: incomplete" in self.markdown

    def test_campaign_historical_verdict_and_preferred_survive(self):
        # AC44 fields (adversarial verify R1, finding 5) -- distinct from
        # the branch-label campaign_decision/demo_candidate.
        assert "campaign_historical_verdict: incomplete" in self.markdown
        assert "historical_preferred: null" in self.markdown


class TestPermutationInvarianceOfMarkdown:
    def test_reversed_attribution_dict_order_produces_identical_markdown(self):
        scorecard = _build_full_scorecard()
        decoded = json.loads(canonical_json_bytes(scorecard))
        baseline = render_markdown(decoded)

        reordered = json.loads(canonical_json_bytes(scorecard))
        by_symbol = reordered["strategies"]["S3"]["falsification"]["attribution"][
            "by_symbol"
        ]
        reordered["strategies"]["S3"]["falsification"]["attribution"]["by_symbol"] = (
            dict(reversed(list(by_symbol.items())))
        )
        status_counts = reordered["h6a_accounting"]["status_counts"]
        reordered["h6a_accounting"]["status_counts"] = dict(
            reversed(list(status_counts.items()))
        )

        assert render_markdown(reordered) == baseline

    def test_dict_key_reversal_alone_does_not_change_rendered_bytes(self):
        scorecard = _build_full_scorecard()
        canonical_bytes = canonical_json_bytes(scorecard)
        decoded_a = json.loads(canonical_bytes)
        decoded_b = json.loads(canonical_bytes)

        # A whole-tree key-order reversal at every dict level, simulating a
        # hypothetical alternate (still-valid) JSON encoder.
        def _reverse_dicts(value):
            if isinstance(value, dict):
                return {
                    k: _reverse_dicts(v)
                    for k in reversed(list(value.keys()))
                    for v in [value[k]]
                }
            if isinstance(value, list):
                return [_reverse_dicts(v) for v in value]
            return value

        assert render_markdown(_reverse_dicts(decoded_b)) == render_markdown(decoded_a)


class TestFrozenGoldenMarkdown:
    """Frozen byte-for-byte proof (mirrors the ROB-945/960 frozen-bytes
    pattern): ``render_markdown(json.loads(canonical_json_bytes))`` must
    equal this exact, previously-captured Markdown -- any future change to
    either the renderer or the canonical builder that alters presentation
    must consciously re-freeze this constant, never drift silently."""

    def test_golden_markdown_bytes_match(self):
        scorecard = _build_full_scorecard()
        markdown_bytes = render_markdown(json.loads(canonical_json_bytes(scorecard)))
        assert markdown_bytes == _GOLDEN_MARKDOWN_BYTES


class TestContractFixtureCampaignDecisionSmoke:
    """Deterministic, non-vacuous smoke across every campaign-decision
    branch -- proves the full contracts -> dual_evidence -> gates ->
    falsification -> canonical -> markdown pipeline produces sane,
    non-empty output for each, without recomputing metrics inside the
    renderer.

    Post-verify-R1 fix 4, ``build_canonical_scorecard`` recomputes and
    cross-checks each strategy's ``direct_verdict`` (rejecting a
    caller-supplied value that disagrees with what ``common_gates``/
    ``falsification`` actually imply) and cross-checks the campaign
    decision's embedded verdicts against those same recomputed values.
    These fixtures therefore build GENUINE, gate-verified S3/S4 inputs for
    each branch rather than asserting an arbitrary label."""

    def _render(self, s3_inputs, s4_inputs, campaign_decision) -> str:
        scorecard = build_canonical_scorecard(
            envelope=_envelope(),
            h6a_seal=_seal(),
            envelope_ok=True,
            envelope_incomplete_reasons=(),
            s3_inputs=s3_inputs,
            s4_inputs=s4_inputs,
            campaign_decision=campaign_decision,
        )
        return render_markdown(json.loads(canonical_json_bytes(scorecard))).decode(
            "utf-8"
        )

    def test_both_pass_s3_demo_candidate(self):
        s3_inputs = _s3_inputs_with_verdict(passing=True)
        s4_inputs = _s4_inputs_with_verdict(passing=True)
        assert s3_inputs.direct_verdict == "historical_pass"
        assert s4_inputs.direct_verdict == "historical_pass"
        campaign_decision = compute_campaign_decision(
            s3_direct_verdict=s3_inputs.direct_verdict,
            s4_direct_verdict=s4_inputs.direct_verdict,
            s3_rank_metrics=_rank_metrics(
                s3_inputs.common_gates, s3_inputs.falsification
            ),
            s4_rank_metrics=_rank_metrics(
                s4_inputs.common_gates, s4_inputs.falsification
            ),
        )
        md = self._render(s3_inputs, s4_inputs, campaign_decision)
        assert "campaign_decision: both_pass_s3_demo_candidate" in md
        assert "demo_candidate: S3" in md

    @pytest.mark.parametrize(
        "field",
        (
            "campaign_decision",
            "campaign_historical_verdict",
            "s3_direct_verdict",
            "s4_direct_verdict",
            "demo_candidate",
            "historical_preferred",
            "s4_observable_superiority",
        ),
    )
    def test_each_both_pass_campaign_field_is_bound_to_ranked_recomputation(
        self, field
    ):
        s3_inputs = _s3_inputs_with_verdict(passing=True)
        s4_inputs = _s4_inputs_with_verdict(passing=True)
        honest = compute_campaign_decision(
            s3_direct_verdict=s3_inputs.direct_verdict,
            s4_direct_verdict=s4_inputs.direct_verdict,
            s3_rank_metrics=_rank_metrics(
                s3_inputs.common_gates, s3_inputs.falsification
            ),
            s4_rank_metrics=_rank_metrics(
                s4_inputs.common_gates, s4_inputs.falsification
            ),
        )
        forged_values = {
            "campaign_decision": "both_fail",
            "campaign_historical_verdict": "historical_fail",
            "s3_direct_verdict": "historical_fail",
            "s4_direct_verdict": "historical_fail",
            "demo_candidate": None,
            "historical_preferred": (
                "S4" if honest.historical_preferred == "S3" else "S3"
            ),
            "s4_observable_superiority": not honest.s4_observable_superiority,
        }
        forged = dataclasses.replace(honest, **{field: forged_values[field]})
        with pytest.raises(
            H5InputError, match="canonical_campaign_decision_forged_or_stale"
        ):
            self._render(s3_inputs, s4_inputs, forged)

    def test_s3_only(self):
        s3_inputs = _s3_inputs_with_verdict(passing=True)
        s4_inputs = _s4_inputs_with_verdict(passing=False)
        assert s3_inputs.direct_verdict == "historical_pass"
        assert s4_inputs.direct_verdict == "historical_fail"
        campaign_decision = compute_campaign_decision(
            s3_direct_verdict=s3_inputs.direct_verdict,
            s4_direct_verdict=s4_inputs.direct_verdict,
        )
        md = self._render(s3_inputs, s4_inputs, campaign_decision)
        assert "campaign_decision: s3_only" in md

    def test_s4_only_no_demo(self):
        s3_inputs = _s3_inputs_with_verdict(passing=False)
        s4_inputs = _s4_inputs_with_verdict(passing=True)
        assert s3_inputs.direct_verdict == "historical_fail"
        assert s4_inputs.direct_verdict == "historical_pass"
        campaign_decision = compute_campaign_decision(
            s3_direct_verdict=s3_inputs.direct_verdict,
            s4_direct_verdict=s4_inputs.direct_verdict,
        )
        md = self._render(s3_inputs, s4_inputs, campaign_decision)
        assert "campaign_decision: s4_only_no_demo" in md
        assert "demo_candidate: null" in md

    def test_both_fail(self):
        s3_inputs = _s3_inputs_with_verdict(passing=False)
        s4_inputs = _s4_inputs_with_verdict(passing=False)
        assert s3_inputs.direct_verdict == "historical_fail"
        assert s4_inputs.direct_verdict == "historical_fail"
        campaign_decision = compute_campaign_decision(
            s3_direct_verdict=s3_inputs.direct_verdict,
            s4_direct_verdict=s4_inputs.direct_verdict,
        )
        md = self._render(s3_inputs, s4_inputs, campaign_decision)
        assert "campaign_decision: both_fail" in md

    def test_incomplete(self):
        # The default single-dimension fixture is genuinely incomplete
        # (missing symbol/pair coverage).
        s3_inputs = _build_s3_inputs(with_evidence=True)
        s4_inputs = _build_s4_inputs()
        assert s3_inputs.direct_verdict == "incomplete"
        campaign_decision = compute_campaign_decision(
            s3_direct_verdict=s3_inputs.direct_verdict,
            s4_direct_verdict=s4_inputs.direct_verdict,
        )
        md = self._render(s3_inputs, s4_inputs, campaign_decision)
        assert "campaign_decision: incomplete" in md


class TestNeverCallsWriterOrPhysicalIO:
    """Static proof this module never imports/calls the physical scorecard
    writer or does file/DB/network I/O (contract: 'never call or modify
    rob960_scorecard_writer; H6-B alone owns physical canonical-JSON
    readback, Markdown staging and publication')."""

    def test_markdown_module_source_has_no_forbidden_references(self):
        import inspect

        import rob974_h5_markdown

        source = inspect.getsource(rob974_h5_markdown)
        for forbidden in (
            "rob960_scorecard_writer",
            "open(",
            "Path(",
            "requests.",
            "httpx.",
            "socket.",
        ):
            assert forbidden not in source, f"found forbidden reference: {forbidden!r}"


# Captured once via an initial GREEN run and frozen here -- any future
# semantic OR presentation change requires a conscious re-freeze, never a
# silent drift.
_GOLDEN_MARKDOWN_BYTES = (
    b"# H5 Scorecard (h5_scorecard_v3)\n\n"
    b"## Lineage\n"
    b"- campaign_run_id: run-cp7\n"
    b"- full_campaign_hash: " + b"a" * 64 + b"\n"
    b"- run_schema_version: v1\n"
    b"- generator_version: g1\n"
    b"- actual_h4_ledger_key: NOT_EVALUATED\n\n"
    b"## H6-A Accounting\n"
    b"- actual_h6a_contract: FIXTURE_ONLY\n"
    b"- expected_total: 48\n"
    b"- registered_total: 48\n"
    b"- accounting_complete: true\n"
    b"- performance_usable: true\n"
    b"  - status[completed]: 48\n"
    b"  - status[rejected]: 0\n"
    b"  - status[crashed]: 0\n"
    b"  - status[timeout]: 0\n"
    b"- reason_codes: (none)\n\n"
    b"## Envelope Validation\n"
    b"- ok: true\n"
    b"- incomplete_reasons: (none)\n\n"
    b"## H4 Attribution Contract\n"
    b"- actual_h4_contract: FIXTURE_ONLY\n"
    b"- contract_provenance: fixture\n"
    b"- schema_version: null\n"
    b"- market_return_semantic: M_t_24h_median_log_return\n"
    b"- typed_path_cross_check: NOT_APPLICABLE\n"
    b"- path_count: 0\n"
    b"- trade_count: 0\n"
    b"- raw_member_key_cross_seal: DEFERRED_TO_H6B_INTEGRATION_E2E\n"
    b"- fake_free_empirical_closure: DEFERRED_TO_H6B_INTEGRATION_E2E\n"
    b"- incomplete_reasons: fixture_h4_attribution_not_actual\n\n"
    b"## Strategy S3\n\n"
    b"### Common Gates\n"
    b"- passed: false\n"
    b"- reasons: e0_below_25bp, monthly_concentration_above_50pct\n"
    b"- pooled_e17_bps: 10.0\n"
    b"- pf17: null (pf_infinite_zero_loss_with_profit)\n"
    b"- win_margin: 0.4722222222222222\n"
    b"- monthly_concentration: 1.0\n\n"
    b"### Falsification\n"
    b"- reasons: (none)\n"
    b"- incomplete_reasons: s3_first_4h_sl_denominator_undefined, "
    b"s3_symbol_evidence_missing\n\n"
    b"### Attribution: by_exit_reason\n"
    b"- TP: trades=40 e17_bps=10.0 e0_bps=15.0 pf=null "
    b"(pf_infinite_zero_loss_with_profit) avg_holding_minutes=10.0\n\n"
    b"### Attribution: by_symbol\n"
    b"- XRPUSDT: trades=40 e17_bps=10.0 e0_bps=15.0 pf=null "
    b"(pf_infinite_zero_loss_with_profit) avg_holding_minutes=10.0\n\n"
    b"### Dual Evidence\n\n"
    b"- S3-00/fold-00: accepted=90 rejected=10\n"
    b"  - rejection_reasons: lookahead_bar=6, signal_close_fill=4\n"
    b"  - path[base13]: ledger_status=completed trade_count=40\n"
    b"  - path[primary_stress17]: ledger_status=completed trade_count=40\n"
    b"  - path[upward_stress22]: ledger_status=completed trade_count=40\n\n"
    b"### PBO\n\n"
    b"- value: 0.35\n"
    b"- reason_codes: (none)\n\n"
    b"### Direct Verdict: incomplete\n\n"
    b"## Strategy S4\n\n"
    b"### Common Gates\n"
    b"- passed: false\n"
    b"- reasons: e0_below_25bp, monthly_concentration_above_50pct\n"
    b"- pooled_e17_bps: 10.0\n"
    b"- pf17: null (pf_infinite_zero_loss_with_profit)\n"
    b"- win_margin: 0.4722222222222222\n"
    b"- monthly_concentration: 1.0\n\n"
    b"### Falsification\n"
    b"- reasons: s4_high_market_return_e22_not_positive\n"
    b"- incomplete_reasons: s4_correlation_undefined, s4_pair_evidence_missing, "
    b"s4_slow_bucket_evidence_missing\n\n"
    b"### Attribution: by_exit_reason\n"
    b"- TP: trades=40 e17_bps=10.0 e0_bps=15.0 pf=null "
    b"(pf_infinite_zero_loss_with_profit) avg_holding_minutes=10.0\n\n"
    b"### Attribution: by_pair\n"
    b"- XRP-DOGE: trades=40 e17_bps=10.0 e0_bps=15.0 pf=null "
    b"(pf_infinite_zero_loss_with_profit) avg_holding_minutes=10.0\n\n"
    b"### Pair Executor State (historical)\n\n"
    b"- pair_executor_state: not_evaluated\n"
    b"- readiness: historical_screen_only\n"
    b"- demo_eligible: false\n\n"
    b"### Direct Verdict: incomplete\n\n"
    b"## Campaign Decision\n"
    b"- campaign_decision: incomplete\n"
    b"- campaign_historical_verdict: incomplete\n"
    b"- s3_direct_verdict: incomplete\n"
    b"- s4_direct_verdict: incomplete\n"
    b"- demo_candidate: null\n"
    b"- historical_preferred: null\n"
    b"- s4_observable_superiority: null\n\n"
)
