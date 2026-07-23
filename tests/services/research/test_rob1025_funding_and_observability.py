"""ROB-1025 PIT funding-unit and no-trade observability regressions."""

from __future__ import annotations

import pytest
from funding_oi_archive import FundingRow
from rob941_funding_sidecar import FundingSidecar
from rob974_features import MINUTE_MS, SYMBOLS, MinuteBar
from rob974_h2_dtos import S3SignalIntent

from app.services import rob974_h6b_materializer as materializer

_START_MS = 1_751_328_000_000
_EIGHT_HOURS_MS = 8 * 60 * 60 * 1000
_OLD_FULL_CAMPAIGN_HASH = (
    "c8bb8e88e129e0072d0ea174adca5c4cce8158f2726c6397030d2ae6e4619f39"
)
_OLD_ENGINE_SOURCE_SHA256 = (
    "a3449251714eeca12806143d8b046aff0d3917cbe4f13ea11b79cb0f1d3f9339"
)


def _input(*, rate: float, annual_rows: bool = False):
    rows = {
        symbol: (MinuteBar(_START_MS, 1.0, 1.0, 1.0, 1.0, 1.0),) for symbol in SYMBOLS
    }
    row_count = 1095 if annual_rows else 1
    sidecars = {
        symbol: FundingSidecar.from_rows(
            symbol,
            (
                FundingRow(
                    _START_MS + index * _EIGHT_HOURS_MS + (7 if index == 0 else 5),
                    8,
                    rate,
                )
                for index in range(row_count)
            ),
        )
        for symbol in SYMBOLS
    }
    return materializer.ActualH4InputData.from_mapping(
        rows,
        corpus_end_ts=_START_MS + MINUTE_MS,
        persisted_corpus_hash="a" * 64,
        persisted_feature_hash="b" * 64,
        funding_sidecars=sidecars,
    )


def _intent(*, signal_ts: int) -> S3SignalIntent:
    return S3SignalIntent(
        symbol="XRPUSDT",
        side="long",
        signal_ts=signal_ts,
        entry_sl_distance=0.01,
        entry_tp_distance=0.02,
        config_id="S3-00",
        fold_id="fold-00",
        volatility_percentile=0.5,
    )


def test_real_ms_epoch_annual_sidecar_is_pit_aligned_and_passes_gate() -> None:
    runner = materializer.ActualMergedH4Runner(
        _input(rate=0.0001, annual_rows=True),
        require_pit_funding=True,
    )
    sidecar = runner._funding_by_symbol["XRPUSDT"]
    assert len(sidecar.rows) == 1095
    assert sidecar.rows[0].calc_time == _START_MS + 7
    assert sidecar.rows[-1].calc_time > 1_000_000_000_000
    assert {row.funding_interval_hours for row in sidecar.rows} == {8}

    gate = runner._s3_funding_gate(_intent(signal_ts=_START_MS + 4 * 60 * 60 * 1000))
    assert gate.accepted is True
    assert gate.reason is None
    assert gate.expected_signed_bps == 1.0


def test_funding_rejection_is_captured_as_exact_terminal_no_trade_reason() -> None:
    runner = materializer.ActualMergedH4Runner(
        _input(rate=0.0004),
        require_pit_funding=True,
    )
    signal_ts = _START_MS + 7
    intent = _intent(signal_ts=signal_ts)
    terminal = runner._invoke_s3(
        intents=(intent,),
        minute_index={(intent.symbol, signal_ts): object()},
        close_feature_index={},
        horizon_end_ts=signal_ts + 48 * 60 * 60 * 1000,
        strategy="S3",
        config_id="S3-00",
        fold_id="fold-00",
    )
    assert terminal.result.trades == ()
    assert tuple(row.reason for row in terminal.result.no_trades) == (
        "expected_funding_cost_above_3bps",
    )
    assert materializer._terminal_reason_counts(terminal, engine_input_count=1) == {
        "expected_funding_cost_above_3bps": 1
    }


def test_required_pit_funding_cannot_silently_fall_back_to_empty() -> None:
    data = _input(rate=0.0)
    without_sidecars = materializer.ActualH4InputData(
        data.h1_minutes,
        data.corpus_end_ts,
        data.persisted_corpus_hash,
        data.persisted_feature_hash,
    )
    with pytest.raises(
        materializer.H6BPlanError,
        match="required PIT funding sidecars are absent",
    ):
        materializer.ActualMergedH4Runner(
            without_sidecars,
            require_pit_funding=True,
        )


def test_rob1025_funding_and_composition_pins_rederive_campaign_identity() -> None:
    identity = materializer.build_production_identity_plan()
    assert identity.full_campaign_hash == (
        "70f352c3c477e27a36111f1daa584deb4ca570ec57ae9555727d6bc6c68b4248"
    )
    assert identity.full_campaign_hash != _OLD_FULL_CAMPAIGN_HASH
    assert identity.campaign_run_id == (
        "rob974h6a-wbSyHLi2OCMA167TwGlSF70ZIXd98KCRUJ88OaQG-Zo"
    )
    assert identity.source_pins.engine_source_sha256 == (
        "33e36cd5a7bdc63729e004dd96f3720ede5c436deca8f46ac214bb53a9b0ac98"
    )
    assert identity.source_pins.engine_source_sha256 != _OLD_ENGINE_SOURCE_SHA256
    assert identity.source_pins.runner_source_sha256 == (
        "7cfe205dcc05face7df95165e71e7002255895365deaa9c0b4f0a8026821c8b5"
    )
    assert identity.exact_48_mapping_hash == (
        "9ec3fdac35c3a98ed0f17bb5f10ab75fb1d68abf89a964e471c3182f53a11bf0"
    )
