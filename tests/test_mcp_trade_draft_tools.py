from __future__ import annotations

from importlib import import_module

import pandas as pd
import pytest

import app.mcp_server.tooling.trade_profile_tools as trade_profile_tools


def _load_engine_module():
    return import_module("app.mcp_server.tooling.trade_profile_draft_engine")


def _base_market_inputs(currency: str = "KRW") -> dict[str, object]:
    return {
        "profiles": [],
        "tier_rules": {},
        "market_filters": [],
        "holdings": [],
        "cash": {"balance": 0.0, "orderable": 0.0, "currency": currency},
        "buy_universe": [],
        "indicator_map": {},
        "fear_greed": None,
        "funding_rates": {},
    }


def _profile(
    symbol: str,
    instrument_type: str,
    *,
    tier: int = 1,
    profile: str = "balanced",
    buy_allowed: bool = True,
    sell_mode: str = "any",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "instrument_type": instrument_type,
        "tier": tier,
        "profile": profile,
        "buy_allowed": buy_allowed,
        "sell_mode": sell_mode,
    }


def _indicator_row(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "rsi": 25.0,
        "stoch_rsi": {"k": 10.0, "d": 8.0},
        "macd": {"macd": 1.0, "signal": 0.5, "histogram": 0.5},
        "adx": {"adx": 20.0, "plus_di": 25.0, "minus_di": 15.0},
        "atr": {"14": 1.2},
        "obv": {"obv": 120.0, "signal": 100.0, "divergence": "none"},
        "ema": {"200": 100.0},
        "ema200": 100.0,
        "price_above_ema200": True,
    }
    data.update(overrides)
    return data


def _rule_bundle(
    symbol: str,
    *,
    tier: int = 1,
    profile: str = "balanced",
    common: dict[str, object] | None = None,
    buy: dict[str, object] | None = None,
    sell: dict[str, object] | None = None,
    stop: dict[str, object] | None = None,
    rebalance: dict[str, object] | None = None,
) -> dict[tuple[str, int, str], dict[str, dict[str, object]]]:
    bundle: dict[str, dict[str, object]] = {}
    if common is not None:
        bundle["common"] = common
    if buy is not None:
        bundle["buy"] = buy
    if sell is not None:
        bundle["sell"] = sell
    if stop is not None:
        bundle["stop"] = stop
    if rebalance is not None:
        bundle["rebalance"] = rebalance
    return {(symbol, tier, profile): bundle}


def _patch_market_inputs(
    monkeypatch, engine, market: str, inputs: dict[str, object]
) -> None:
    monkeypatch.setattr(engine, "_resolve_markets", lambda instrument_type: [market])
    monkeypatch.setattr(engine, "_load_market_inputs", lambda requested_market: inputs)


@pytest.mark.asyncio
async def test_prepare_trade_draft_invalid_instrument_type_returns_error_payload() -> (
    None
):
    result = await trade_profile_tools.prepare_trade_draft(instrument_type="bond")

    assert result["success"] is False
    assert "instrument_type must be one of: kr, us, crypto" in str(result["error"])


@pytest.mark.asyncio
async def test_prepare_trade_draft_invalid_action_type_returns_error_payload() -> None:
    result = await trade_profile_tools.prepare_trade_draft(
        instrument_type="crypto",
        action_type="hold",
    )

    assert result == {
        "success": False,
        "error": "action_type must be one of: all, buy, sell",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market", "instrument_type", "profile_symbol", "universe_row", "currency"),
    [
        (
            "equity_kr",
            "equity_kr",
            "005930",
            {"code": "5930", "name": "Samsung Electronics"},
            "KRW",
        ),
        (
            "equity_us",
            "equity_us",
            "AAPL",
            {"code": "aapl", "name": "Apple"},
            "USD",
        ),
    ],
)
async def test_prepare_trade_draft_equity_code_rows_match_asset_profiles(
    monkeypatch,
    market: str,
    instrument_type: str,
    profile_symbol: str,
    universe_row: dict[str, object],
    currency: str,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(currency=currency),
        "profiles": [_profile(profile_symbol, instrument_type)],
        "tier_rules": _rule_bundle(
            profile_symbol,
            common={"cash_reserve_pct": 10, "max_positions": 1},
            buy={"rsi14_max": 30, "position_size_pct": 100},
        ),
        "cash": {"balance": 1000.0, "orderable": 1000.0, "currency": currency},
        "buy_universe": [universe_row],
        "indicator_map": {profile_symbol: _indicator_row()},
    }
    _patch_market_inputs(monkeypatch, engine, market, inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type=instrument_type)

    draft = result["markets"][0]["buy_drafts"][0]
    assert draft["symbol"] == profile_symbol
    assert draft["price_type"] == "market"
    assert draft["suggested_amount"] == 900.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("btc_change_rate", "expected_kill_switch"),
    [(-6.0, True), (-4.0, False)],
)
async def test_prepare_trade_draft_kill_switch_only_fires_after_threshold(
    monkeypatch,
    btc_change_rate: float,
    expected_kill_switch: bool,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(),
        "market_filters": [
            {
                "filter_name": "kill_switch",
                "enabled": True,
                "params": {"btc_drop_24h_pct": 5},
            }
        ],
        "buy_universe": [{"symbol": "KRW-BTC", "change_rate": btc_change_rate}],
    }
    _patch_market_inputs(monkeypatch, engine, "crypto", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="crypto")

    assert result["markets"][0]["kill_switch"] is expected_kill_switch


@pytest.mark.asyncio
async def test_prepare_trade_draft_kill_switch_warns_when_btc_metric_missing(
    monkeypatch,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(),
        "profiles": [_profile("KRW-ETH", "crypto")],
        "tier_rules": _rule_bundle(
            "KRW-ETH",
            common={"cash_reserve_pct": 0, "max_positions": 1},
            buy={"rsi14_max": 30, "position_size_pct": 100},
        ),
        "cash": {"balance": 500.0, "orderable": 500.0, "currency": "KRW"},
        "buy_universe": [{"symbol": "KRW-ETH", "change_rate": 1.0}],
        "indicator_map": {"KRW-ETH": _indicator_row()},
        "market_filters": [
            {
                "filter_name": "kill_switch",
                "enabled": True,
                "params": {"btc_drop_24h_pct": 5},
            }
        ],
    }
    _patch_market_inputs(monkeypatch, engine, "crypto", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="crypto")

    market = result["markets"][0]
    assert market["kill_switch"] is False
    assert market["warnings"] == ["kill_switch btc_drop_24h_pct metric unavailable"]
    assert len(market["buy_drafts"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("buy_params", "indicator_row"),
    [
        ({"rsi14_max": 30}, _indicator_row(rsi=45.0)),
        (
            {"stoch_rsi_k_max": 20},
            _indicator_row(stoch_rsi={"k": 35.0, "d": 18.0}),
        ),
        (
            {"adx_max": 25},
            _indicator_row(adx={"adx": 40.0, "plus_di": 25.0, "minus_di": 15.0}),
        ),
        (
            {"macd_cross_required": True},
            _indicator_row(macd={"macd": -0.1, "signal": 0.2, "histogram": -0.3}),
        ),
        (
            {"obv_rising_required": True},
            _indicator_row(obv={"obv": 80.0, "signal": 100.0, "divergence": "none"}),
        ),
    ],
)
async def test_prepare_trade_draft_buy_rules_skip_when_conditions_not_met(
    monkeypatch,
    buy_params: dict[str, object],
    indicator_row: dict[str, object],
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(),
        "profiles": [_profile("KRW-BTC", "crypto")],
        "tier_rules": _rule_bundle(
            "KRW-BTC",
            common={"cash_reserve_pct": 10, "max_positions": 1},
            buy={**buy_params, "position_size_pct": 100},
        ),
        "cash": {"balance": 1000.0, "orderable": 1000.0, "currency": "KRW"},
        "buy_universe": [{"symbol": "KRW-BTC", "change_rate": 1.0}],
        "indicator_map": {"KRW-BTC": indicator_row},
    }
    _patch_market_inputs(monkeypatch, engine, "crypto", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="crypto")

    market = result["markets"][0]
    assert market["buy_drafts"] == []
    assert market["skipped"] == [
        {"symbol": "KRW-BTC", "reason": "buy conditions not met"}
    ]


@pytest.mark.asyncio
async def test_prepare_trade_draft_buy_rules_skip_when_required_indicator_missing(
    monkeypatch,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(),
        "profiles": [_profile("KRW-BTC", "crypto")],
        "tier_rules": _rule_bundle(
            "KRW-BTC",
            common={"cash_reserve_pct": 0, "max_positions": 1},
            buy={"macd_cross_required": True, "position_size_pct": 100},
        ),
        "cash": {"balance": 1000.0, "orderable": 1000.0, "currency": "KRW"},
        "buy_universe": [{"symbol": "KRW-BTC", "change_rate": 1.0}],
        "indicator_map": {"KRW-BTC": _indicator_row(macd=None)},
    }
    _patch_market_inputs(monkeypatch, engine, "crypto", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="crypto")

    market = result["markets"][0]
    assert market["buy_drafts"] == []
    assert "macd" in market["skipped"][0]["reason"]


@pytest.mark.asyncio
async def test_load_indicator_map_keeps_price_above_ema200_unknown_when_ema200_missing(
    monkeypatch,
) -> None:
    engine = _load_engine_module()

    async def fake_fetch(symbol: str, market: str, count: int = 250) -> pd.DataFrame:
        del symbol, market, count
        return pd.DataFrame({"close": [100.0]})

    def fake_compute(df: pd.DataFrame, indicators: list[str]) -> dict[str, object]:
        del df, indicators
        return {"rsi": {"14": 25.0}, "ema": {}}

    monkeypatch.setattr(engine, "_fetch_ohlcv_for_indicators", fake_fetch)
    monkeypatch.setattr(engine, "_compute_indicators", fake_compute)

    indicator_map = await engine._load_indicator_map("crypto", ["KRW-BTC"])

    assert indicator_map["KRW-BTC"]["ema200"] is None
    assert indicator_map["KRW-BTC"]["price_above_ema200"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("buy_params", [None, {}])
async def test_prepare_trade_draft_skips_buy_when_buy_params_missing_or_empty(
    monkeypatch,
    buy_params: dict[str, object] | None,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(),
        "profiles": [_profile("KRW-BTC", "crypto")],
        "tier_rules": _rule_bundle(
            "KRW-BTC",
            common={"cash_reserve_pct": 0, "max_positions": 1},
            buy=buy_params,
        ),
        "cash": {"balance": 1000.0, "orderable": 1000.0, "currency": "KRW"},
        "buy_universe": [{"symbol": "KRW-BTC", "change_rate": 1.0}],
        "indicator_map": {"KRW-BTC": _indicator_row()},
    }
    _patch_market_inputs(monkeypatch, engine, "crypto", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="crypto")

    market = result["markets"][0]
    assert market["buy_drafts"] == []
    assert market["skipped"] == [
        {"symbol": "KRW-BTC", "reason": "buy rule params missing"}
    ]


@pytest.mark.asyncio
async def test_prepare_trade_draft_valid_predicate_bearing_buy_rule_still_generates_draft(
    monkeypatch,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(),
        "profiles": [_profile("KRW-BTC", "crypto")],
        "tier_rules": _rule_bundle(
            "KRW-BTC",
            common={"cash_reserve_pct": 0, "max_positions": 1},
            buy={"rsi14_max": 30, "position_size_pct": 100},
        ),
        "cash": {"balance": 1000.0, "orderable": 1000.0, "currency": "KRW"},
        "buy_universe": [{"symbol": "KRW-BTC", "change_rate": 1.0}],
        "indicator_map": {"KRW-BTC": _indicator_row(rsi=25.0)},
    }
    _patch_market_inputs(monkeypatch, engine, "crypto", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="crypto")

    market = result["markets"][0]
    assert market["skipped"] == []
    assert market["buy_drafts"][0]["symbol"] == "KRW-BTC"
    assert market["buy_drafts"][0]["triggers"] == ["rsi14_max"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "buy_params",
    [
        {"position_size_pct": 100},
        {"dca_stages": 3, "ema200_budget_factor": {"above": 1.0, "below": 0.7}},
        {"macd_cross_required": False, "position_size_pct": 100},
        {"obv_rising_required": False, "dca_stages": 3},
    ],
)
async def test_prepare_trade_draft_skips_buy_when_no_active_predicates(
    monkeypatch,
    buy_params: dict[str, object],
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(),
        "profiles": [_profile("KRW-BTC", "crypto")],
        "tier_rules": _rule_bundle(
            "KRW-BTC",
            common={"cash_reserve_pct": 0, "max_positions": 1},
            buy=buy_params,
        ),
        "cash": {"balance": 1000.0, "orderable": 1000.0, "currency": "KRW"},
        "buy_universe": [{"symbol": "KRW-BTC", "change_rate": 1.0}],
        "indicator_map": {"KRW-BTC": _indicator_row()},
    }
    _patch_market_inputs(monkeypatch, engine, "crypto", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="crypto")

    market = result["markets"][0]
    assert market["buy_drafts"] == []
    assert market["skipped"] == [
        {"symbol": "KRW-BTC", "reason": "no active buy predicates"}
    ]


@pytest.mark.asyncio
async def test_prepare_trade_draft_mixed_profiles_skip_only_invalid_zero_active_buy_rule(
    monkeypatch,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(),
        "profiles": [_profile("KRW-BTC", "crypto"), _profile("KRW-ETH", "crypto")],
        "tier_rules": {
            **_rule_bundle(
                "KRW-BTC",
                common={"cash_reserve_pct": 0, "max_positions": 1},
                buy={"rsi14_max": 30, "position_size_pct": 100},
            ),
            **_rule_bundle(
                "KRW-ETH",
                common={"cash_reserve_pct": 0, "max_positions": 1},
                buy={"position_size_pct": 100},
            ),
        },
        "cash": {"balance": 1000.0, "orderable": 1000.0, "currency": "KRW"},
        "buy_universe": [
            {"symbol": "KRW-BTC", "change_rate": 1.0},
            {"symbol": "KRW-ETH", "change_rate": 1.0},
        ],
        "indicator_map": {
            "KRW-BTC": _indicator_row(rsi=25.0),
            "KRW-ETH": _indicator_row(rsi=25.0),
        },
    }
    _patch_market_inputs(monkeypatch, engine, "crypto", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="crypto")

    market = result["markets"][0]
    assert [draft["symbol"] for draft in market["buy_drafts"]] == ["KRW-BTC"]
    assert market["buy_drafts"][0]["triggers"] == ["rsi14_max"]
    assert market["skipped"] == [
        {"symbol": "KRW-ETH", "reason": "no active buy predicates"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("price_above_ema200", "expected_amount"),
    [(True, 900.0), (False, 450.0)],
)
async def test_prepare_trade_draft_ema200_budget_factor_uses_regime_branch(
    monkeypatch,
    price_above_ema200: bool,
    expected_amount: float,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(),
        "profiles": [_profile("KRW-BTC", "crypto")],
        "tier_rules": _rule_bundle(
            "KRW-BTC",
            common={"cash_reserve_pct": 10, "max_positions": 1},
            buy={
                "rsi14_max": 30,
                "position_size_pct": 100,
                "ema200_budget_factor": {"above": 1.0, "below": 0.5},
            },
        ),
        "cash": {"balance": 1000.0, "orderable": 1000.0, "currency": "KRW"},
        "buy_universe": [{"symbol": "KRW-BTC", "change_rate": 1.0}],
        "indicator_map": {
            "KRW-BTC": _indicator_row(price_above_ema200=price_above_ema200)
        },
    }
    _patch_market_inputs(monkeypatch, engine, "crypto", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="crypto")

    draft = result["markets"][0]["buy_drafts"][0]
    assert draft["suggested_amount"] == expected_amount


@pytest.mark.asyncio
async def test_prepare_trade_draft_market_regime_budget_factor_reduces_amount_when_below_ema200(
    monkeypatch,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(),
        "profiles": [_profile("KRW-BTC", "crypto")],
        "tier_rules": _rule_bundle(
            "KRW-BTC",
            common={"cash_reserve_pct": 10, "max_positions": 1},
            buy={"rsi14_max": 30, "position_size_pct": 100},
        ),
        "cash": {"balance": 1000.0, "orderable": 1000.0, "currency": "KRW"},
        "buy_universe": [{"symbol": "KRW-BTC", "change_rate": 1.0}],
        "indicator_map": {"KRW-BTC": _indicator_row(price_above_ema200=False)},
        "market_filters": [
            {
                "filter_name": "regime_ema200",
                "enabled": True,
                "params": {"budget_factor": 0.7},
            }
        ],
    }
    _patch_market_inputs(monkeypatch, engine, "crypto", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="crypto")

    draft = result["markets"][0]["buy_drafts"][0]
    assert draft["suggested_amount"] == 630.0


@pytest.mark.asyncio
async def test_prepare_trade_draft_regime_market_and_buy_budget_factors_compose(
    monkeypatch,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(),
        "profiles": [_profile("KRW-BTC", "crypto")],
        "tier_rules": _rule_bundle(
            "KRW-BTC",
            common={"cash_reserve_pct": 10, "max_positions": 1},
            buy={
                "rsi14_max": 30,
                "position_size_pct": 100,
                "ema200_budget_factor": {"above": 1.0, "below": 0.5},
            },
        ),
        "cash": {"balance": 1000.0, "orderable": 1000.0, "currency": "KRW"},
        "buy_universe": [{"symbol": "KRW-BTC", "change_rate": 1.0}],
        "indicator_map": {"KRW-BTC": _indicator_row(price_above_ema200=False)},
        "market_filters": [
            {
                "filter_name": "regime_ema200",
                "enabled": True,
                "params": {"budget_factor": 0.7},
            }
        ],
    }
    _patch_market_inputs(monkeypatch, engine, "crypto", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="crypto")

    draft = result["markets"][0]["buy_drafts"][0]
    assert draft["suggested_amount"] == 315.0


@pytest.mark.asyncio
async def test_prepare_trade_draft_regime_gate_style_filters_still_block_buys(
    monkeypatch,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(),
        "profiles": [_profile("KRW-BTC", "crypto")],
        "tier_rules": _rule_bundle(
            "KRW-BTC",
            common={"cash_reserve_pct": 0, "max_positions": 1},
            buy={"rsi14_max": 30, "position_size_pct": 100},
        ),
        "cash": {"balance": 1000.0, "orderable": 1000.0, "currency": "KRW"},
        "buy_universe": [{"symbol": "KRW-BTC", "change_rate": 1.0}],
        "indicator_map": {"KRW-BTC": _indicator_row(price_above_ema200=False)},
        "market_filters": [
            {
                "filter_name": "regime_ema200",
                "enabled": True,
                "params": {"above": True, "below": False},
            }
        ],
    }
    _patch_market_inputs(monkeypatch, engine, "crypto", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="crypto")

    market = result["markets"][0]
    assert market["buy_drafts"] == []
    assert market["skipped"] == [
        {
            "symbol": "KRW-BTC",
            "reason": "regime_ema200 blocked buy while price is below EMA200",
        }
    ]


@pytest.mark.asyncio
async def test_prepare_trade_draft_dca_stages_populates_stage_string(
    monkeypatch,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(),
        "profiles": [_profile("KRW-BTC", "crypto")],
        "tier_rules": _rule_bundle(
            "KRW-BTC",
            common={"cash_reserve_pct": 10, "max_positions": 1},
            buy={
                "rsi14_max": 30,
                "position_size_pct": 100,
                "dca_stages": 3,
            },
        ),
        "cash": {"balance": 1000.0, "orderable": 1000.0, "currency": "KRW"},
        "buy_universe": [{"symbol": "KRW-BTC", "change_rate": 1.0}],
        "indicator_map": {"KRW-BTC": _indicator_row()},
    }
    _patch_market_inputs(monkeypatch, engine, "crypto", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="crypto")

    assert result["markets"][0]["buy_drafts"][0]["dca_stage"] == "1/3"


@pytest.mark.asyncio
async def test_prepare_trade_draft_sell_mode_none_suppresses_sells(monkeypatch) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(currency="USD"),
        "profiles": [
            _profile(
                "AAPL",
                "equity_us",
                buy_allowed=False,
                sell_mode="none",
            )
        ],
        "tier_rules": _rule_bundle(
            "AAPL",
            sell={"take_profit_full_rsi": 65},
        ),
        "holdings": [
            {"symbol": "AAPL", "instrument_type": "equity_us", "quantity": 10.0}
        ],
        "indicator_map": {"AAPL": _indicator_row(rsi=70.0)},
    }
    _patch_market_inputs(monkeypatch, engine, "equity_us", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="us")

    assert result["markets"][0]["sell_drafts"] == []


@pytest.mark.asyncio
async def test_prepare_trade_draft_exit_profile_sells_full_position(
    monkeypatch,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(currency="USD"),
        "profiles": [
            _profile(
                "AAPL",
                "equity_us",
                tier=2,
                profile="exit",
                buy_allowed=False,
            )
        ],
        "holdings": [
            {"symbol": "AAPL", "instrument_type": "equity_us", "quantity": 7.0}
        ],
        "indicator_map": {"AAPL": _indicator_row(rsi=70.0)},
    }
    _patch_market_inputs(monkeypatch, engine, "equity_us", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="us")

    draft = result["markets"][0]["sell_drafts"][0]
    assert draft["symbol"] == "AAPL"
    assert draft["profile"] == "exit"
    assert draft["suggested_qty"] == 7.0
    assert draft["price_type"] == "market"


@pytest.mark.asyncio
async def test_prepare_trade_draft_rebalance_only_without_params_is_skipped(
    monkeypatch,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(),
        "profiles": [
            _profile(
                "005930",
                "equity_kr",
                tier=2,
                buy_allowed=False,
                sell_mode="rebalance_only",
            )
        ],
        "holdings": [
            {"symbol": "005930", "instrument_type": "equity_kr", "quantity": 3.0}
        ],
        "tier_rules": _rule_bundle(
            "005930",
            tier=2,
            sell={"take_profit_partial_rsi": 60},
        ),
        "indicator_map": {"005930": _indicator_row(rsi=65.0)},
    }
    _patch_market_inputs(monkeypatch, engine, "equity_kr", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="kr")

    market = result["markets"][0]
    assert market["sell_drafts"] == []
    assert market["skipped"] == [
        {"symbol": "005930", "reason": "rebalance_only requires rebalance params"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sell_params", "expected_qty", "expected_trigger"),
    [
        ({"take_profit_partial_rsi": 60}, 5.0, "take_profit_partial_rsi"),
        ({"take_profit_full_rsi": 60}, 10.0, "take_profit_full_rsi"),
    ],
)
async def test_prepare_trade_draft_sell_thresholds_emit_partial_or_full_drafts(
    monkeypatch,
    sell_params: dict[str, object],
    expected_qty: float,
    expected_trigger: str,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(currency="USD"),
        "profiles": [_profile("AAPL", "equity_us", buy_allowed=False)],
        "tier_rules": _rule_bundle(
            "AAPL",
            sell=sell_params,
            stop={"atr_multiple": 2.0},
        ),
        "holdings": [
            {"symbol": "AAPL", "instrument_type": "equity_us", "quantity": 10.0}
        ],
        "indicator_map": {"AAPL": _indicator_row(rsi=70.0)},
    }
    _patch_market_inputs(monkeypatch, engine, "equity_us", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="us")

    draft = result["markets"][0]["sell_drafts"][0]
    assert draft["suggested_qty"] == expected_qty
    assert draft["price_type"] == "market"
    assert draft["triggers"] == [expected_trigger]
    assert draft["risk_metadata"] == {"stop": {"atr_multiple": 2.0}}


@pytest.mark.asyncio
async def test_prepare_trade_draft_equity_partial_sell_floors_to_whole_shares(
    monkeypatch,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(currency="USD"),
        "profiles": [_profile("AAPL", "equity_us", buy_allowed=False)],
        "tier_rules": _rule_bundle(
            "AAPL",
            sell={"take_profit_partial_rsi": 60},
        ),
        "holdings": [
            {"symbol": "AAPL", "instrument_type": "equity_us", "quantity": 3.0}
        ],
        "indicator_map": {"AAPL": _indicator_row(rsi=70.0)},
    }
    _patch_market_inputs(monkeypatch, engine, "equity_us", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="us")

    draft = result["markets"][0]["sell_drafts"][0]
    assert draft["suggested_qty"] == 1


@pytest.mark.asyncio
async def test_prepare_trade_draft_tiny_equity_partial_sell_is_skipped(
    monkeypatch,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(currency="USD"),
        "profiles": [_profile("AAPL", "equity_us", buy_allowed=False)],
        "tier_rules": _rule_bundle(
            "AAPL",
            sell={"take_profit_partial_rsi": 60},
        ),
        "holdings": [
            {"symbol": "AAPL", "instrument_type": "equity_us", "quantity": 1.0}
        ],
        "indicator_map": {"AAPL": _indicator_row(rsi=70.0)},
    }
    _patch_market_inputs(monkeypatch, engine, "equity_us", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="us")

    market = result["markets"][0]
    assert market["sell_drafts"] == []
    assert market["skipped"] == [
        {"symbol": "AAPL", "reason": "partial sell quantity below 1 share"}
    ]


@pytest.mark.asyncio
async def test_prepare_trade_draft_crypto_partial_sell_keeps_fractional_quantity(
    monkeypatch,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(),
        "profiles": [_profile("KRW-BTC", "crypto", buy_allowed=False)],
        "tier_rules": _rule_bundle(
            "KRW-BTC",
            sell={"take_profit_partial_rsi": 60},
        ),
        "holdings": [
            {"symbol": "KRW-BTC", "instrument_type": "crypto", "quantity": 3.0}
        ],
        "indicator_map": {"KRW-BTC": _indicator_row(rsi=70.0)},
    }
    _patch_market_inputs(monkeypatch, engine, "crypto", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="crypto")

    draft = result["markets"][0]["sell_drafts"][0]
    assert draft["suggested_qty"] == 1.5


@pytest.mark.asyncio
async def test_prepare_trade_draft_fear_greed_blocks_buys_with_skip_reason(
    monkeypatch,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(),
        "profiles": [_profile("005930", "equity_kr")],
        "tier_rules": _rule_bundle(
            "005930",
            common={"cash_reserve_pct": 0, "max_positions": 1},
            buy={"rsi14_max": 30, "position_size_pct": 100},
        ),
        "cash": {"balance": 1000.0, "orderable": 1000.0, "currency": "KRW"},
        "buy_universe": [{"symbol": "005930", "change_rate": 1.0}],
        "indicator_map": {"005930": _indicator_row()},
        "market_filters": [
            {
                "filter_name": "fear_greed",
                "enabled": True,
                "params": {"extreme_greed": 75},
            }
        ],
        "fear_greed": {"current": {"value": 80}},
    }
    _patch_market_inputs(monkeypatch, engine, "equity_kr", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="kr")

    market = result["markets"][0]
    assert market["buy_drafts"] == []
    assert "fear_greed" in market["skipped"][0]["reason"]


@pytest.mark.asyncio
async def test_prepare_trade_draft_fear_greed_warns_and_continues_when_unavailable(
    monkeypatch,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(),
        "profiles": [_profile("005930", "equity_kr")],
        "tier_rules": _rule_bundle(
            "005930",
            common={"cash_reserve_pct": 0, "max_positions": 1},
            buy={"rsi14_max": 30, "position_size_pct": 100},
        ),
        "cash": {"balance": 1000.0, "orderable": 1000.0, "currency": "KRW"},
        "buy_universe": [{"symbol": "005930", "change_rate": 1.0}],
        "indicator_map": {"005930": _indicator_row()},
        "market_filters": [
            {
                "filter_name": "fear_greed",
                "enabled": True,
                "params": {"extreme_greed": 75},
            }
        ],
        "fear_greed": None,
    }
    _patch_market_inputs(monkeypatch, engine, "equity_kr", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="kr")

    market = result["markets"][0]
    assert market["warnings"] == ["fear_greed data unavailable"]
    assert len(market["buy_drafts"]) == 1


@pytest.mark.asyncio
async def test_prepare_trade_draft_funding_rate_blocks_crypto_buys(monkeypatch) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(),
        "profiles": [_profile("KRW-BTC", "crypto")],
        "tier_rules": _rule_bundle(
            "KRW-BTC",
            common={"cash_reserve_pct": 0, "max_positions": 1},
            buy={"rsi14_max": 30, "position_size_pct": 100},
        ),
        "cash": {"balance": 1000.0, "orderable": 1000.0, "currency": "KRW"},
        "buy_universe": [{"symbol": "KRW-BTC", "change_rate": 1.0}],
        "indicator_map": {"KRW-BTC": _indicator_row()},
        "market_filters": [
            {
                "filter_name": "funding_rate",
                "enabled": True,
                "params": {"hot_threshold": 0.05},
            }
        ],
        "funding_rates": {"BTC": {"current_funding_rate_pct": 0.06}},
    }
    _patch_market_inputs(monkeypatch, engine, "crypto", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="crypto")

    market = result["markets"][0]
    assert market["buy_drafts"] == []
    assert "funding_rate" in market["skipped"][0]["reason"]


@pytest.mark.asyncio
async def test_prepare_trade_draft_funding_rate_warns_and_continues_when_unavailable(
    monkeypatch,
) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(),
        "profiles": [_profile("KRW-BTC", "crypto")],
        "tier_rules": _rule_bundle(
            "KRW-BTC",
            common={"cash_reserve_pct": 0, "max_positions": 1},
            buy={"rsi14_max": 30, "position_size_pct": 100},
        ),
        "cash": {"balance": 1000.0, "orderable": 1000.0, "currency": "KRW"},
        "buy_universe": [{"symbol": "KRW-BTC", "change_rate": 1.0}],
        "indicator_map": {"KRW-BTC": _indicator_row()},
        "market_filters": [
            {
                "filter_name": "funding_rate",
                "enabled": True,
                "params": {"hot_threshold": 0.05},
            }
        ],
        "funding_rates": {},
    }
    _patch_market_inputs(monkeypatch, engine, "crypto", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="crypto")

    market = result["markets"][0]
    assert market["warnings"] == ["funding_rate data unavailable"]
    assert len(market["buy_drafts"]) == 1


@pytest.mark.asyncio
async def test_prepare_trade_draft_unknown_filter_adds_warning(monkeypatch) -> None:
    engine = _load_engine_module()
    inputs = {
        **_base_market_inputs(),
        "market_filters": [
            {
                "filter_name": "custom_unsupported_filter",
                "enabled": True,
                "params": {"threshold": 1},
            }
        ],
    }
    _patch_market_inputs(monkeypatch, engine, "crypto", inputs)

    result = await engine.prepare_trade_draft_impl(instrument_type="crypto")

    assert result["markets"][0]["warnings"] == [
        "Unknown filter_name 'custom_unsupported_filter' ignored"
    ]
