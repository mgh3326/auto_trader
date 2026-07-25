"""ROB-982 CP6 -- H4 narrow actual-H2 terminal adapter (RED first).

H4 owns no price/exit/PnL state machine of its own. These tests prove the
H4 adapter is a thin, adversarially-validating pass-through to the ACTUAL
merged H2 S3/S4 engines (never a forked re-implementation), that it reseals
(input, output) via two independent canonical hashes, that it raises
``H4ContractDrift`` rather than silently accepting malformed engine output,
and that a first-catch live exception is captured as bounded ROB-970
diagnostic evidence kept separate from the semantic seals.
"""

from __future__ import annotations

import math

import pytest
from rob944_diagnostic_evidence import ChildFailureEvidence
from rob974_h2_dtos import (
    MinuteBar,
    S3EngineResult,
    S3IncompleteRecord,
    S3NoTradeRecord,
    S3SignalIntent,
    S3Trade,
    S4PairLegClose,
    S4PairSignalIntent,
)
from rob974_h2_ingress import build_minute_index
from rob974_h2_s3_engine import FOUR_H_MS, run_s3_portfolio_stream
from rob974_h4_adapter import (
    H4ContractDrift,
    invoke_actual_s3_engine,
    invoke_actual_s4_engine,
    seal_s3_engine_output,
    validate_s3_terminal,
)

_MIN_MS = 60_000
_CORPUS_END = 10_000_000_000
_PAIR = ("XRPUSDT", "DOGEUSDT")


def _bars(symbol, start_ts, count, price=1.0, overrides=None):
    overrides = overrides or {}
    out = []
    for i in range(count):
        ts = start_ts + i * _MIN_MS
        o, h, low, c = overrides.get(i, (price, price, price, price))
        out.append(MinuteBar(symbol, ts, o, h, low, c))
    return out


def _s3_intent(symbol="XRPUSDT", side="long", signal_ts=0, sl=0.0080, tp=0.0128, **kw):
    fields = {
        "symbol": symbol,
        "side": side,
        "signal_ts": signal_ts,
        "entry_sl_distance": sl,
        "entry_tp_distance": tp,
        "config_id": "s3-00",
        "fold_id": "fold-00",
        "volatility_percentile": 55.0,
    }
    fields.update(kw)
    return S3SignalIntent(**fields)


def _s4_intent(
    pair=_PAIR,
    signal_ts=0,
    side_a="short",
    side_b="long",
    weight_a=0.4,
    weight_b=0.6,
    mu=0.0,
    sigma=0.05,
    z_entry=1.9,
    sl=0.0100,
    tp=0.0150,
    **kw,
):
    fields = {
        "pair": pair,
        "signal_ts": signal_ts,
        "side_a": side_a,
        "side_b": side_b,
        "weight_a": weight_a,
        "weight_b": weight_b,
        "beta_a": 1.2,
        "beta_b": 0.8,
        "mu": mu,
        "sigma": sigma,
        "z_entry": z_entry,
        "gross_notional": max(6 / weight_a, 6 / weight_b),
        "entry_sl_distance": sl,
        "entry_tp_distance": tp,
        "config_id": "s4-00",
        "fold_id": "fold-00",
    }
    fields.update(kw)
    return S4PairSignalIntent(**fields)


def _flat_pair_closes(pair, start_ts, n_boundaries, close_a=1.0, close_b=1.0):
    out = []
    for k in range(1, n_boundaries + 1):
        ts = start_ts + k * FOUR_H_MS
        out.append(S4PairLegClose(pair[0], ts, close_a))
        out.append(S4PairLegClose(pair[1], ts, close_b))
    return out


class TestS3AdapterIsThinPassThrough:
    def test_sl_result_matches_direct_engine_call_exactly(self):
        bars = _bars("XRPUSDT", 0, 3, price=1.0)
        bars[1] = MinuteBar("XRPUSDT", _MIN_MS, 0.90, 0.90, 0.90, 0.90)
        candidates = [_s3_intent(signal_ts=0)]
        minute_index = build_minute_index(bars)
        direct = run_s3_portfolio_stream(
            candidates, minute_index, {}, corpus_end_ts=_CORPUS_END
        )
        sealed = invoke_actual_s3_engine(
            candidates=candidates,
            minute_index=minute_index,
            close_feature_index={},
            corpus_end_ts=_CORPUS_END,
            strategy="S3",
            config_id="s3-00",
        )
        assert sealed.result == direct
        assert sealed.result.trades[0].exit_reason == "SL"

    def test_thesis_exit_reachable_through_adapter(self):
        from rob974_h2_dtos import S3CloseFeature

        bars = _bars("XRPUSDT", 0, 241, price=1.0)
        candidates = [_s3_intent(signal_ts=0)]
        minute_index = build_minute_index(bars)
        features = {
            ("XRPUSDT", FOUR_H_MS): S3CloseFeature(
                "XRPUSDT", FOUR_H_MS, 1.0, 1.0, -0.001
            )
        }
        sealed = invoke_actual_s3_engine(
            candidates=candidates,
            minute_index=minute_index,
            close_feature_index=features,
            corpus_end_ts=_CORPUS_END,
            strategy="S3",
            config_id="s3-00",
        )
        assert sealed.result.trades[0].exit_reason == "THESIS_EXIT"

    def test_timeout_reachable_through_adapter(self):
        from rob974_h2_dtos import S3CloseFeature

        deadline = 12 * FOUR_H_MS
        n = deadline // _MIN_MS + 1
        bars = _bars("XRPUSDT", 0, n, price=1.0)
        candidates = [_s3_intent(signal_ts=0)]
        minute_index = build_minute_index(bars)
        features = {
            ("XRPUSDT", k * FOUR_H_MS): S3CloseFeature(
                "XRPUSDT", k * FOUR_H_MS, 1.0, 1.0, 0.01
            )
            for k in range(1, 13)
        }
        sealed = invoke_actual_s3_engine(
            candidates=candidates,
            minute_index=minute_index,
            close_feature_index=features,
            corpus_end_ts=_CORPUS_END,
            strategy="S3",
            config_id="s3-00",
        )
        assert sealed.result.trades[0].exit_reason == "TIMEOUT"
        assert sealed.result.trades[0].exit_ts == deadline


class TestS4AdapterIsThinPassThrough:
    def test_mean_exit_reachable_through_adapter(self):
        bars_a = _bars("XRPUSDT", 0, 241, price=1.0)
        bars_b = _bars("DOGEUSDT", 0, 241, price=1.0)
        candidates = [_s4_intent(signal_ts=0, mu=0.0, sigma=0.05)]
        minute_index = build_minute_index(bars_a + bars_b)
        pair_closes = _flat_pair_closes(_PAIR, 0, 1, close_a=1.0, close_b=1.0)
        pair_close_index = {(c.symbol, c.close_ts): c for c in pair_closes}
        sealed = invoke_actual_s4_engine(
            candidates=candidates,
            minute_index=minute_index,
            pair_close_index=pair_close_index,
            corpus_end_ts=_CORPUS_END,
            strategy="S4",
            config_id="s4-00",
        )
        assert sealed.result.trades[0].exit_reason == "MEAN_EXIT"

    def test_stall_exit_reachable_through_adapter(self):
        bars_a = _bars("XRPUSDT", 0, 481, price=1.0)
        bars_b = _bars("DOGEUSDT", 0, 481, price=1.0)
        far_ca, far_cb = math.exp(2.0), 1.0
        candidates = [_s4_intent(signal_ts=0, mu=0.0, sigma=0.05, z_entry=1.9)]
        minute_index = build_minute_index(bars_a + bars_b)
        pair_closes = _flat_pair_closes(_PAIR, 0, 2, close_a=far_ca, close_b=far_cb)
        pair_close_index = {(c.symbol, c.close_ts): c for c in pair_closes}
        sealed = invoke_actual_s4_engine(
            candidates=candidates,
            minute_index=minute_index,
            pair_close_index=pair_close_index,
            corpus_end_ts=_CORPUS_END,
            strategy="S4",
            config_id="s4-00",
        )
        assert sealed.result.trades[0].exit_reason == "STALL_EXIT"

    def test_historical_null_posture_preserved_through_adapter(self):
        bars_a = _bars("XRPUSDT", 0, 3, price=1.0)
        bars_a[1] = MinuteBar("XRPUSDT", _MIN_MS, 1.20, 1.20, 1.20, 1.20)
        bars_b = _bars("DOGEUSDT", 0, 3, price=1.0)
        bars_b[1] = MinuteBar("DOGEUSDT", _MIN_MS, 1.20, 1.20, 1.20, 1.20)
        candidates = [_s4_intent(signal_ts=0)]
        minute_index = build_minute_index(bars_a + bars_b)
        sealed = invoke_actual_s4_engine(
            candidates=candidates,
            minute_index=minute_index,
            pair_close_index={},
            corpus_end_ts=_CORPUS_END,
            strategy="S4",
            config_id="s4-00",
        )
        if sealed.result.trades:
            trade = sealed.result.trades[0]
            assert trade.order_id_a is None
            assert trade.order_id_b is None
            assert trade.demo_eligible is False
            assert trade.pair_exec_fail == "not_evaluated"


class TestDualSealDeterminism:
    def test_input_and_output_seals_are_stable_and_distinct(self):
        bars = _bars("XRPUSDT", 0, 3, price=1.0)
        bars[1] = MinuteBar("XRPUSDT", _MIN_MS, 0.90, 0.90, 0.90, 0.90)
        candidates = [_s3_intent(signal_ts=0)]
        minute_index = build_minute_index(bars)
        first = invoke_actual_s3_engine(
            candidates=candidates,
            minute_index=minute_index,
            close_feature_index={},
            corpus_end_ts=_CORPUS_END,
            strategy="S3",
            config_id="s3-00",
        )
        second = invoke_actual_s3_engine(
            candidates=candidates,
            minute_index=minute_index,
            close_feature_index={},
            corpus_end_ts=_CORPUS_END,
            strategy="S3",
            config_id="s3-00",
        )
        assert first.input_seal_sha256 == second.input_seal_sha256
        assert first.output_seal_sha256 == second.output_seal_sha256
        assert first.input_seal_sha256 != first.output_seal_sha256

    def test_output_seal_changes_when_outcome_changes(self):
        bars_sl = _bars("XRPUSDT", 0, 3, price=1.0)
        bars_sl[1] = MinuteBar("XRPUSDT", _MIN_MS, 0.90, 0.90, 0.90, 0.90)
        bars_tp = _bars("XRPUSDT", 0, 3, price=1.0)
        bars_tp[1] = MinuteBar("XRPUSDT", _MIN_MS, 1.05, 1.05, 1.05, 1.05)
        candidates = [_s3_intent(signal_ts=0)]
        sl_sealed = invoke_actual_s3_engine(
            candidates=candidates,
            minute_index=build_minute_index(bars_sl),
            close_feature_index={},
            corpus_end_ts=_CORPUS_END,
            strategy="S3",
            config_id="s3-00",
        )
        tp_sealed = invoke_actual_s3_engine(
            candidates=candidates,
            minute_index=build_minute_index(bars_tp),
            close_feature_index={},
            corpus_end_ts=_CORPUS_END,
            strategy="S3",
            config_id="s3-00",
        )
        assert sl_sealed.result.trades[0].exit_reason == "SL"
        assert tp_sealed.result.trades[0].exit_reason == "TP"
        assert sl_sealed.output_seal_sha256 != tp_sealed.output_seal_sha256
        # inputs (candidates + corpus_end_ts + horizon) are byte-identical
        assert sl_sealed.input_seal_sha256 == tp_sealed.input_seal_sha256


class TestContractDriftAndDiagnostics:
    def test_duplicate_identity_raises_contract_drift_with_bounded_evidence(self):
        bars = _bars("XRPUSDT", 0, 3, price=1.0)
        candidates = [_s3_intent(signal_ts=0), _s3_intent(signal_ts=0)]
        minute_index = build_minute_index(bars)
        with pytest.raises(H4ContractDrift) as excinfo:
            invoke_actual_s3_engine(
                candidates=candidates,
                minute_index=minute_index,
                close_feature_index={},
                corpus_end_ts=_CORPUS_END,
                strategy="S3",
                config_id="s3-00",
                fold_id="fold-00",
            )
        drift = excinfo.value
        assert drift.evidence is not None
        assert type(drift.evidence) is ChildFailureEvidence
        assert drift.evidence.stage == "engine"
        assert drift.evidence.strategy == "S3"
        assert drift.evidence.config_id == "s3-00"
        assert drift.evidence.fold_id == "fold-00"
        # ROB-970: no filesystem path survives into the sanitized traceback
        assert "/Users/" not in drift.evidence.traceback_text
        assert "auto_trader" not in drift.evidence.traceback_text

    def test_wrong_result_type_is_contract_drift(self):
        candidates = [_s3_intent(signal_ts=0)]
        with pytest.raises(H4ContractDrift):
            validate_s3_terminal(candidates, object())

    def test_mae_gross_mfe_bound_violation_is_contract_drift(self):
        candidates = [_s3_intent(signal_ts=0)]
        bogus_trade = S3Trade(
            symbol="XRPUSDT",
            side="long",
            config_id="s3-00",
            fold_id="fold-00",
            signal_ts=0,
            entry_ts=0,
            entry_price=1.0,
            exit_ts=_MIN_MS,
            exit_price=1.10,
            exit_reason="TP",
            mfe_bps=1.0,  # violates mae<=gross<=mfe (gross_bps ~= 953bps here)
            mae_bps=-1.0,
            gross_bps=953.1,
        )
        bogus_result = S3EngineResult(
            trades=(bogus_trade,), no_trades=(), incompletes=()
        )
        with pytest.raises(H4ContractDrift):
            validate_s3_terminal(candidates, bogus_result)

    def test_unresolved_extra_identity_is_contract_drift(self):
        candidates = [_s3_intent(signal_ts=0)]
        phantom = S3NoTradeRecord(
            symbol="DOGEUSDT",
            side="long",
            config_id="s3-00",
            fold_id="fold-00",
            signal_ts=123,
            reason="global_position_open",
        )
        bogus_result = S3EngineResult(trades=(), no_trades=(phantom,), incompletes=())
        with pytest.raises(H4ContractDrift):
            validate_s3_terminal(candidates, bogus_result)

    def test_incomplete_halt_allows_unresolved_suffix_not_fabricated_rows(self):
        candidates = [
            _s3_intent(signal_ts=0),
            _s3_intent(signal_ts=FOUR_H_MS, symbol="DOGEUSDT"),
        ]
        incomplete = S3IncompleteRecord(
            symbol="XRPUSDT",
            side="long",
            config_id="s3-00",
            fold_id="fold-00",
            signal_ts=0,
            entry_ts=0,
            entry_price=1.0,
            reason="early_eof",
        )
        bogus_result = S3EngineResult(
            trades=(), no_trades=(), incompletes=(incomplete,)
        )
        # the second candidate is simply absent (never resolved) -- must not raise
        validate_s3_terminal(candidates, bogus_result)


class TestSealHelperIsExported:
    def test_seal_s3_engine_output_is_deterministic(self):
        bars = _bars("XRPUSDT", 0, 3, price=1.0)
        bars[1] = MinuteBar("XRPUSDT", _MIN_MS, 0.90, 0.90, 0.90, 0.90)
        result = run_s3_portfolio_stream(
            [_s3_intent(signal_ts=0)],
            build_minute_index(bars),
            {},
            corpus_end_ts=_CORPUS_END,
        )
        assert seal_s3_engine_output(result) == seal_s3_engine_output(result)
