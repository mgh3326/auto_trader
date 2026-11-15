"""hk 4723 (3797 T-P6) — Kiwoom mock attribution resolver (pure, no DB).

The resolver decides who owns an order before any side effect. These tests pin
the failure contract shared with ``kis_mock``: a missing strategy is an error,
never a value, and nothing is substituted for it.
"""

from __future__ import annotations

import unicodedata

import pytest

from app.services.kis_mock_attribution import InvalidStrategy, MissingAttribution
from app.services.kiwoom_mock_attribution import (
    ACCOUNT_SCOPE,
    mint_correlation_id,
    resolve_attribution,
)

pytestmark = pytest.mark.unit

ORDER = {
    "symbol": "005930",
    "side": "buy",
    "price": 70000,
    "quantity": 3,
}


def test_resolve_returns_the_caller_strategy_and_a_kiwoom_scoped_id():
    attribution = resolve_attribution(**ORDER, strategy="support_reserve_net")

    assert attribution.strategy == "support_reserve_net"
    assert attribution.signal_source == "manual"
    assert attribution.correlation_id.startswith(f"live:{ACCOUNT_SCOPE}:")
    assert ACCOUNT_SCOPE == "kiwoom_mock"


def test_resolve_is_deterministic_and_ignores_the_clock():
    first = resolve_attribution(**ORDER, strategy="s", kst_trade_day="2026-11-16")
    second = resolve_attribution(**ORDER, strategy="s", kst_trade_day="2026-11-16")
    assert first.correlation_id == second.correlation_id
    assert first.correlation_id == mint_correlation_id(
        **ORDER, kst_trade_day="2026-11-16"
    )


def test_the_trading_day_separates_ids():
    day_one = resolve_attribution(**ORDER, strategy="s", kst_trade_day="2026-11-16")
    day_two = resolve_attribution(**ORDER, strategy="s", kst_trade_day="2026-11-17")
    assert day_one.correlation_id != day_two.correlation_id


def test_rung_separates_deliberate_identical_orders():
    base = resolve_attribution(**ORDER, strategy="s", kst_trade_day="2026-11-16")
    rung_one = resolve_attribution(
        **ORDER, strategy="s", kst_trade_day="2026-11-16", rung=1
    )
    rung_two = resolve_attribution(
        **ORDER, strategy="s", kst_trade_day="2026-11-16", rung=2
    )
    assert base.correlation_id != rung_one.correlation_id
    assert rung_one.correlation_id != rung_two.correlation_id
    # the same discriminator in its two accepted spellings is one order
    assert (
        resolve_attribution(
            **ORDER, strategy="s", kst_trade_day="2026-11-16", rung="2"
        ).correlation_id
        == rung_two.correlation_id
    )
    # None and 0 are DIFFERENT send keys (build_client_order_id uses "" vs "0"),
    # so they must be different signal correlation ids too.
    explicit_zero = resolve_attribution(
        **ORDER, strategy="s", kst_trade_day="2026-11-16", rung=0
    )
    assert explicit_zero.correlation_id != base.correlation_id
    assert (
        resolve_attribution(
            **ORDER, strategy="s", kst_trade_day="2026-11-16", rung=""
        ).correlation_id
        == base.correlation_id
    )


def test_an_explicit_correlation_id_is_honoured():
    attribution = resolve_attribution(**ORDER, strategy="s", correlation_id="given")
    assert attribution.correlation_id == "given"


def test_a_missing_strategy_raises_and_names_what_is_missing():
    with pytest.raises(MissingAttribution) as excinfo:
        resolve_attribution(**ORDER)
    assert excinfo.value.missing == ("strategy",)


@pytest.mark.parametrize("strategy", ["", "   ", "test", "null", "tbd", "N/A"])
def test_blank_and_placeholder_strategies_are_refused(strategy):
    with pytest.raises(InvalidStrategy) as excinfo:
        resolve_attribution(**ORDER, strategy=strategy)
    assert excinfo.value.error_code == "placeholder_strategy"


def test_a_real_strategy_is_nfkc_normalized_and_trimmed():
    raw = "  support_reserve_net  "
    attribution = resolve_attribution(**ORDER, strategy=raw)
    assert attribution.strategy == unicodedata.normalize("NFKC", raw).strip()


def test_signal_source_defaults_and_can_be_named():
    assert resolve_attribution(**ORDER, strategy="s").signal_source == "manual"
    assert (
        resolve_attribution(**ORDER, strategy="s", signal_source="smoke").signal_source
        == "smoke"
    )
