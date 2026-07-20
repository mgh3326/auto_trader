"""ROB-983 (H5, CP1) -- strict input envelope + H6-A accounting gate.

Exact strategies/configs/folds/paths/dimensions, one-metric-trade-per-round-
trip unit (leg count never a denominator), strict envelope verification, the
H6-A exact-48 accounting gate, and AC34 structural-incomplete provenance
classification (never historical_fail, regardless of how good the reported
economics look).
"""

from __future__ import annotations

import pytest
from rob974_h5_contracts import (
    CONFIGS_PER_STRATEGY,
    FOLD_IDS,
    PATH_SCENARIOS,
    S3_SYMBOLS,
    S4_PAIRS,
    STRATEGIES,
    STRUCTURAL_INCOMPLETE_REASONS,
    CampaignEnvelope,
    H5InputError,
    H6AAccountingSeal,
    MetricTrade,
    classify_provenance_violation,
    config_ids_for,
    validate_envelope_and_accounting,
)


def _envelope(**overrides) -> CampaignEnvelope:
    base = {
        "full_campaign_hash": "a" * 64,
        "campaign_run_id": "run-1",
        "parent_corpus_hash": "b" * 64,
        "parent_projection_hash": "c" * 64,
        "feature_contract_hash": "d" * 64,
        "strategy_contract_hashes": {"S3": "e" * 64, "S4": "f" * 64},
        "h4_runner_source_hash": "1" * 64,
        "h4_pbo_source_hash": "2" * 64,
        "h2_engine_source_hash": "3" * 64,
        "h3_generator_source_hash": "4" * 64,
        "run_schema_version": "rob974-h5.v1",
        "generator_version": "rob974-h5-scorecard/1.0.0",
        "expected_experiment_ids": (
            tuple(config_ids_for("S3")) + tuple(config_ids_for("S4"))
        ),
        "h6a_trial_accounting_hash": "5" * 64,
    }
    base.update(overrides)
    return CampaignEnvelope(**base)


def _accounting_seal(**overrides) -> H6AAccountingSeal:
    base = {
        "expected_total": 48,
        "registered_total": 48,
        "primary_attempts": 48,
        "status_counts": {"completed": 48, "rejected": 0, "crashed": 0, "timeout": 0},
        "retry_attempts": 0,
        "accounting_complete": True,
        "performance_usable": True,
        "trial_accounting_hash": "5" * 64,
        "reason_codes": (),
    }
    base.update(overrides)
    return H6AAccountingSeal(**base)


class TestExactDomainConstants:
    def test_strategies_are_exact(self):
        assert STRATEGIES == ("S3", "S4")

    def test_config_ids_are_exact_24_each(self):
        assert config_ids_for("S3") == tuple(f"S3-{i:02d}" for i in range(24))
        assert config_ids_for("S4") == tuple(f"S4-{i:02d}" for i in range(24))
        assert CONFIGS_PER_STRATEGY == 24

    def test_fold_ids_are_exact_eight(self):
        assert FOLD_IDS == tuple(f"fold-{i:02d}" for i in range(8))

    def test_path_scenarios_are_exact(self):
        assert PATH_SCENARIOS == ("base13", "primary_stress17", "upward_stress22")

    def test_s3_symbol_order(self):
        assert S3_SYMBOLS == ("XRPUSDT", "DOGEUSDT", "SOLUSDT")

    def test_s4_pair_order(self):
        assert S4_PAIRS == ("XRP-DOGE", "XRP-SOL", "DOGE-SOL")


class TestMetricTradeLegCountNeverDenominator:
    def test_s3_trade_is_one_trade_regardless_of_representation(self):
        trade = MetricTrade(
            strategy="S3",
            config_id="S3-00",
            fold_id="fold-00",
            path_scenario="primary_stress17",
            dimension="XRPUSDT",
            direction="long",
            entry_ts=1_000,
            exit_ts=2_000,
            holding_minutes=15.0,
            exit_reason="TP",
            gross_bps=50.0,
            net_bps=44.0,
            tp_bps=68.0,
            sl_bps=40.0,
            gross_notional=None,
            market_return_4h=0.01,
            volatility_percentile=55.0,
        )
        # one MetricTrade == exactly one metric-trade unit; there is no
        # separate "leg" field/count that could ever be summed as if it
        # were additional trades.
        assert not hasattr(trade, "leg_count")
        assert not hasattr(trade, "legs")

    def test_s4_two_leg_basket_is_one_trade(self):
        trade = MetricTrade(
            strategy="S4",
            config_id="S4-00",
            fold_id="fold-00",
            path_scenario="primary_stress17",
            dimension="XRP-DOGE",
            direction="short_a_long_b",
            entry_ts=1_000,
            exit_ts=2_000,
            holding_minutes=120.0,
            exit_reason="MEAN_EXIT",
            gross_bps=30.0,
            net_bps=24.0,
            tp_bps=60.0,
            sl_bps=35.0,
            gross_notional=15_000.0,
            market_return_4h=0.005,
            volatility_percentile=None,
        )
        assert not hasattr(trade, "leg_count")
        assert not hasattr(trade, "legs")
        assert trade.gross_notional == 15_000.0

    def test_s4_volatility_percentile_must_be_none(self):
        with pytest.raises(H5InputError):
            MetricTrade(
                strategy="S4",
                config_id="S4-00",
                fold_id="fold-00",
                path_scenario="base13",
                dimension="XRP-DOGE",
                direction="short_a_long_b",
                entry_ts=1_000,
                exit_ts=2_000,
                holding_minutes=120.0,
                exit_reason="TP",
                gross_bps=30.0,
                net_bps=24.0,
                tp_bps=60.0,
                sl_bps=35.0,
                gross_notional=15_000.0,
                market_return_4h=0.005,
                volatility_percentile=1.0,  # forbidden -- S4 must be None
            )

    def test_s3_gross_notional_must_be_none(self):
        with pytest.raises(H5InputError):
            MetricTrade(
                strategy="S3",
                config_id="S3-00",
                fold_id="fold-00",
                path_scenario="base13",
                dimension="XRPUSDT",
                direction="long",
                entry_ts=1_000,
                exit_ts=2_000,
                holding_minutes=15.0,
                exit_reason="TP",
                gross_bps=50.0,
                net_bps=44.0,
                tp_bps=68.0,
                sl_bps=40.0,
                gross_notional=100.0,  # forbidden -- S3 is equal-weight/None
                market_return_4h=0.01,
                volatility_percentile=55.0,
            )

    def _s4_trade_kwargs(self, **overrides):
        fields = {
            "strategy": "S4",
            "config_id": "S4-00",
            "fold_id": "fold-00",
            "path_scenario": "primary_stress17",
            "dimension": "XRP-DOGE",
            "direction": "short_a_long_b",
            "entry_ts": 1_000,
            "exit_ts": 2_000,
            "holding_minutes": 120.0,
            "exit_reason": "MEAN_EXIT",
            "gross_bps": 30.0,
            "net_bps": 24.0,
            "tp_bps": 60.0,
            "sl_bps": 35.0,
            "gross_notional": 15_000.0,
            "market_return_4h": 0.005,
            "volatility_percentile": None,
        }
        fields.update(overrides)
        return fields

    def test_s4_gross_notional_none_rejected(self):
        # D12 fix (adversarial verify R1, finding 2): S4's required gross
        # basket notional G can never be None -- a missing G must never
        # silently fall back to S3's equal-weight (1.0) convention.
        with pytest.raises(H5InputError):
            MetricTrade(**self._s4_trade_kwargs(gross_notional=None))

    def test_s4_gross_notional_zero_rejected(self):
        with pytest.raises(H5InputError):
            MetricTrade(**self._s4_trade_kwargs(gross_notional=0.0))

    def test_s4_gross_notional_negative_rejected(self):
        with pytest.raises(H5InputError):
            MetricTrade(**self._s4_trade_kwargs(gross_notional=-100.0))

    def test_s4_gross_notional_bool_rejected(self):
        with pytest.raises(H5InputError):
            MetricTrade(**self._s4_trade_kwargs(gross_notional=True))

    def test_s4_gross_notional_int_rejected(self):
        with pytest.raises(H5InputError):
            MetricTrade(**self._s4_trade_kwargs(gross_notional=100))

    def test_s4_gross_notional_nan_rejected(self):
        with pytest.raises(H5InputError):
            MetricTrade(**self._s4_trade_kwargs(gross_notional=float("nan")))


class TestMetricTradeTypeStrictness:
    def test_bool_net_bps_rejected(self):
        with pytest.raises(H5InputError):
            MetricTrade(
                strategy="S3",
                config_id="S3-00",
                fold_id="fold-00",
                path_scenario="base13",
                dimension="XRPUSDT",
                direction="long",
                entry_ts=1_000,
                exit_ts=2_000,
                holding_minutes=15.0,
                exit_reason="TP",
                gross_bps=50.0,
                net_bps=True,  # bool masquerading as float
                tp_bps=68.0,
                sl_bps=40.0,
                gross_notional=None,
                market_return_4h=0.01,
                volatility_percentile=55.0,
            )

    def test_int_net_bps_rejected(self):
        with pytest.raises(H5InputError):
            MetricTrade(
                strategy="S3",
                config_id="S3-00",
                fold_id="fold-00",
                path_scenario="base13",
                dimension="XRPUSDT",
                direction="long",
                entry_ts=1_000,
                exit_ts=2_000,
                holding_minutes=15.0,
                exit_reason="TP",
                gross_bps=50.0,
                net_bps=44,  # int masquerading as float
                tp_bps=68.0,
                sl_bps=40.0,
                gross_notional=None,
                market_return_4h=0.01,
                volatility_percentile=55.0,
            )

    def test_nan_net_bps_rejected(self):
        with pytest.raises(H5InputError):
            MetricTrade(
                strategy="S3",
                config_id="S3-00",
                fold_id="fold-00",
                path_scenario="base13",
                dimension="XRPUSDT",
                direction="long",
                entry_ts=1_000,
                exit_ts=2_000,
                holding_minutes=15.0,
                exit_reason="TP",
                gross_bps=50.0,
                net_bps=float("nan"),
                tp_bps=68.0,
                sl_bps=40.0,
                gross_notional=None,
                market_return_4h=0.01,
                volatility_percentile=55.0,
            )

    def test_bool_entry_ts_rejected(self):
        with pytest.raises(H5InputError):
            MetricTrade(
                strategy="S3",
                config_id="S3-00",
                fold_id="fold-00",
                path_scenario="base13",
                dimension="XRPUSDT",
                direction="long",
                entry_ts=True,
                exit_ts=2_000,
                holding_minutes=15.0,
                exit_reason="TP",
                gross_bps=50.0,
                net_bps=44.0,
                tp_bps=68.0,
                sl_bps=40.0,
                gross_notional=None,
                market_return_4h=0.01,
                volatility_percentile=55.0,
            )


class TestEnvelopeVerification:
    def test_valid_envelope_and_accounting_passes(self):
        result = validate_envelope_and_accounting(_envelope(), _accounting_seal())
        assert result.incomplete_reasons == ()
        assert result.ok is True

    def test_missing_49th_config_id_rejected(self):
        ids = tuple(config_ids_for("S3")) + tuple(config_ids_for("S4")) + ("S3-99",)
        with pytest.raises(H5InputError):
            _envelope(expected_experiment_ids=ids)

    def test_missing_config_id_rejected(self):
        ids = (tuple(config_ids_for("S3")) + tuple(config_ids_for("S4")))[:-1]
        with pytest.raises(H5InputError):
            _envelope(expected_experiment_ids=ids)

    def test_duplicate_config_id_rejected(self):
        ids = list(tuple(config_ids_for("S3")) + tuple(config_ids_for("S4")))
        ids[1] = ids[0]
        with pytest.raises(H5InputError):
            _envelope(expected_experiment_ids=tuple(ids))

    def test_reordered_config_ids_rejected(self):
        ids = tuple(config_ids_for("S3")) + tuple(config_ids_for("S4"))
        reordered = (ids[1], ids[0]) + ids[2:]
        with pytest.raises(H5InputError):
            _envelope(expected_experiment_ids=reordered)

    def test_bad_hash_format_rejected(self):
        with pytest.raises(H5InputError):
            _envelope(full_campaign_hash="not-a-hash")


class TestH6AAccountingGate:
    def test_47_of_48_makes_campaign_incomplete(self):
        seal = _accounting_seal(
            registered_total=47,
            primary_attempts=47,
            status_counts={"completed": 47, "rejected": 0, "crashed": 0, "timeout": 0},
            accounting_complete=False,
            performance_usable=False,
            reason_codes=("h6_accounting_incomplete",),
        )
        result = validate_envelope_and_accounting(_envelope(), seal)
        assert result.ok is False
        assert "h6_accounting_incomplete" in result.incomplete_reasons

    def test_49_registered_rejected(self):
        with pytest.raises(H5InputError):
            _accounting_seal(registered_total=49)

    def test_expected_total_decimal_rejected(self):
        # Discovered gap (adversarial verify R1, finding 7): expected_total
        # was checked via `==48` only, not exact-int type --
        # Decimal(48)==48 is True in Python, so it silently slipped past
        # the equality check.
        from decimal import Decimal

        with pytest.raises(H5InputError):
            _accounting_seal(expected_total=Decimal(48))

    def test_expected_total_float_rejected(self):
        # 48.0 == 48 is True in Python -- must still be type-rejected.
        with pytest.raises(H5InputError):
            _accounting_seal(expected_total=48.0)

    def test_status_sum_not_48_rejected(self):
        with pytest.raises(H5InputError):
            _accounting_seal(
                status_counts={
                    "completed": 47,
                    "rejected": 0,
                    "crashed": 0,
                    "timeout": 0,
                }
            )

    def test_nonzero_retry_makes_campaign_incomplete(self):
        seal = _accounting_seal(
            retry_attempts=1,
            performance_usable=False,
            reason_codes=("h6_accounting_has_retries",),
        )
        result = validate_envelope_and_accounting(_envelope(), seal)
        assert result.ok is False

    def test_complete_true_with_usable_false_is_rejected_shape(self):
        # accounting_complete=True but performance_usable=False (e.g. a
        # primary that didn't complete) is a VALID, well-formed-incomplete
        # combination -- must surface as campaign incomplete, never raise.
        seal = _accounting_seal(
            status_counts={"completed": 47, "rejected": 1, "crashed": 0, "timeout": 0},
            performance_usable=False,
            reason_codes=("h6_primary_attempt_not_completed",),
        )
        result = validate_envelope_and_accounting(_envelope(), seal)
        assert result.ok is False
        assert "h6_primary_attempt_not_completed" in result.incomplete_reasons

    def test_hidden_rejected_row_under_status_sum_48_is_incomplete(self):
        seal = _accounting_seal(
            status_counts={"completed": 47, "rejected": 1, "crashed": 0, "timeout": 0},
            performance_usable=False,
            reason_codes=("h6_primary_attempt_not_completed",),
        )
        result = validate_envelope_and_accounting(_envelope(), seal)
        assert result.ok is False
        # status sum is still 48 -- this must not be mistaken for a fully
        # usable, all-completed accounting seal.
        assert sum(seal.status_counts.values()) == 48
        assert result.ok is False

    def test_excellent_partial_subset_is_still_incomplete(self):
        # A caller must never be tempted to score a highly profitable
        # 47-of-48 subset as if it were the full campaign.
        seal = _accounting_seal(
            registered_total=47,
            primary_attempts=47,
            status_counts={"completed": 47, "rejected": 0, "crashed": 0, "timeout": 0},
            accounting_complete=False,
            performance_usable=False,
            reason_codes=("h6_accounting_incomplete",),
        )
        result = validate_envelope_and_accounting(_envelope(), seal)
        assert result.ok is False


class TestAC34StructuralIncompleteClassification:
    @pytest.mark.parametrize(
        "violation",
        sorted(STRUCTURAL_INCOMPLETE_REASONS),
    )
    def test_every_ac34_violation_classifies_as_incomplete_never_fail(self, violation):
        assert classify_provenance_violation(violation) == "incomplete"

    def test_ac34_violation_set_covers_required_categories(self):
        required_substrings = (
            "missing_bar",
            "future_bar",
            "lookahead",
            "signal_close_fill",
            "unpriced_gap",
            "double_charge",
            "single_leg_notional_denominator",
            "fabricated",
            "nonfinite",
            "type_invalid",
            "missing_pbo",
            "missing_accounting",
            "missing_evidence",
        )
        for substring in required_substrings:
            assert any(
                substring in reason for reason in STRUCTURAL_INCOMPLETE_REASONS
            ), f"no structural-incomplete reason covers {substring!r}"

    def test_unknown_violation_code_raises_rather_than_silently_passing(self):
        with pytest.raises(H5InputError):
            classify_provenance_violation("not_a_real_violation_code")

    def test_excellent_economics_do_not_downgrade_ac34_violation_to_fail(self):
        # Even a caller asserting outstanding P&L cannot turn a structural
        # provenance violation into historical_fail -- the classification is
        # a pure function of the violation code alone.
        for violation in STRUCTURAL_INCOMPLETE_REASONS:
            assert classify_provenance_violation(violation) == "incomplete"
