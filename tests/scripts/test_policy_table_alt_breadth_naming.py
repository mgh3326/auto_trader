"""ROB-1315 §7-2 — the crypto policy table's breadth header is renamed.

``market_context.alt_breadth`` counted markets whose 24h change was
*absolutely* positive, but the ``no_chasing`` / ``recovery_gate`` breadth
clause reads the *BTC-relative* ``breadth.alts_beating_btc_pct``. Sharing the
colloquial name "alt breadth" let a table row be read as a gate verdict; on
2026-08-20 the two were 78.09% and 17.38%. The block is now
``market_context.alt_positive_24h`` and says so in its own payload.

Pure compute — no DB, no network, no broker.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.build_policy_table import _render_summary_md
from scripts.policy_table.adapters import crypto as crypto_adapter

pytestmark = pytest.mark.unit


def _bars(n: int = 150, *, start: Decimal, step: Decimal) -> list[list[str]]:
    bars: list[list[str]] = []
    price = start
    for i in range(n):
        price = price + (step if i % 5 else -step * 2 // 3)
        bars.append(
            [str(price), str(price + step * 4), str(price - step * 3), "120000"]
        )
    return bars


def _raw(change_rates: dict[str, str]) -> crypto_adapter.RawInputs:
    candles = {
        symbol: _bars(start=Decimal("1000000"), step=Decimal("1000"))
        for symbol in change_rates
    }
    return crypto_adapter.RawInputs(
        as_of="2026-08-20T02:20:00+00:00",
        holdings=[],
        watch_alerts=[],
        top_traded=[
            {
                "market": symbol,
                "trade_price": candles[symbol][-1][0],
                "acc_trade_price_24h": "1000000000",
                "signed_change_rate": rate,
            }
            for symbol, rate in change_rates.items()
        ],
        orderable_krw="1000000",
        candles=candles,
    )


def test_market_context_block_is_named_alt_positive_24h():
    payload = crypto_adapter.compute_policy_table(
        _raw({"KRW-BTC": "0.03", "KRW-ETH": "0.02", "KRW-XRP": "-0.01"})
    )

    context = payload["market_context"]
    assert "alt_breadth" not in context
    block = context["alt_positive_24h"]
    assert block["basis"] == "absolute_24h_change_gt_zero"
    assert block["positive_24h_count"] == 2
    assert block["negative_24h_count"] == 1
    assert block["swept_market_count"] == 3


def test_block_declares_it_is_not_the_gate_input():
    payload = crypto_adapter.compute_policy_table(_raw({"KRW-BTC": "0.03"}))

    block = payload["market_context"]["alt_positive_24h"]
    assert block["gate_input"] is False
    assert block["gate_input_metric"] == "breadth.alts_beating_btc_pct"


def test_counts_are_unchanged_by_the_rename():
    """Only the key moved; the arithmetic behind it is identical."""

    payload = crypto_adapter.compute_policy_table(
        _raw({"KRW-A": "0.05", "KRW-B": "0.00", "KRW-C": "-0.02", "KRW-D": "0.01"})
    )

    block = payload["market_context"]["alt_positive_24h"]
    # a zero rate is neither positive nor negative, as before
    assert (block["positive_24h_count"], block["negative_24h_count"]) == (2, 1)
    assert block["swept_market_count"] == 4
    assert block["positive_pct"] == Decimal(2) / Decimal(4)


def test_rendered_summary_uses_the_new_name_and_disclaims_the_gate():
    payload = crypto_adapter.compute_policy_table(
        _raw({"KRW-BTC": "0.03", "KRW-ETH": "0.02"})
    )
    summary = _render_summary_md(
        {**payload, "stamps": {"policy_table_hash": "x", "auto_trader_head": "y"}}
    )

    assert "alt_positive_24h:" in summary
    assert "not the no_chasing gate input" in summary
    assert "alts_beating_btc_pct" in summary
