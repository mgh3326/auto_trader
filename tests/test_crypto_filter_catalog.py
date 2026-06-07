"""ROB-443 follow-up: crypto screener filter catalog (screen_stocks_snapshot)."""

from __future__ import annotations

import pytest

from app.services.invest_view_model.screener_filters import (
    SNAPSHOT_FILTER_FIELDS,
    ScreenerFilterCondition,
    ScreenerFilterError,
    apply_filter_conditions,
    snapshot_kind_for_preset,
    validate_conditions,
)

_KIND = "invest_crypto_screener_snapshots"


def test_crypto_kind_catalog_fields() -> None:
    catalog = SNAPSHOT_FILTER_FIELDS[_KIND]
    assert {
        "trade_amount_24h",
        "rsi",
        "change_rate",
        "oi_change_24h",
        "long_short_account_ratio",
        "funding_rate",
    } <= set(catalog)


@pytest.mark.parametrize(
    "preset",
    [
        "crypto_high_volume",
        "crypto_oversold",
        "crypto_momentum",
        "crypto_funding_squeeze",
        "crypto_funding_overheated",
        "crypto_oi_surge",
        "crypto_long_short_skew",
    ],
)
def test_all_crypto_presets_map_to_crypto_kind(preset) -> None:
    assert snapshot_kind_for_preset(preset) == _KIND


def test_validate_conditions_crypto_ok_clamp_and_unknown() -> None:
    ok = validate_conditions(
        [ScreenerFilterCondition("trade_amount_24h", "gte", 5_000_000_000)],
        snapshot_kind=_KIND,
    )
    assert ok[0].field == "trade_amount_24h"

    # value clamped to the definition bound (rsi max 100)
    clamped = validate_conditions(
        [ScreenerFilterCondition("rsi", "lte", 999)], snapshot_kind=_KIND
    )
    assert clamped[0].value == 100.0

    # unknown field is fail-closed (never silently dropped)
    with pytest.raises(ScreenerFilterError):
        validate_conditions(
            [ScreenerFilterCondition("per", "lte", 10)], snapshot_kind=_KIND
        )


def test_apply_filter_conditions_on_crypto_rows() -> None:
    rows = [
        {"symbol": "KRW-AAA", "trade_amount_24h": 1.0e10, "rsi": 28.0},
        {"symbol": "KRW-BBB", "trade_amount_24h": 1.0e8, "rsi": 25.0},  # below floor
        {"symbol": "KRW-CCC", "trade_amount_24h": 5.0e10, "rsi": 60.0},  # rsi too high
        {
            "symbol": "KRW-DDD",
            "trade_amount_24h": None,
            "rsi": 20.0,
        },  # null → fail-closed
    ]
    conds = [
        ScreenerFilterCondition("trade_amount_24h", "gte", 1_000_000_000),
        ScreenerFilterCondition("rsi", "lte", 35),
    ]
    out = apply_filter_conditions(rows, conds)
    assert [r["symbol"] for r in out] == ["KRW-AAA"]
