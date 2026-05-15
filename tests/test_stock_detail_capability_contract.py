from __future__ import annotations

import pytest

from app.schemas.invest_stock_detail import default_capabilities_for_market


@pytest.mark.parametrize(
    ("market", "orderbook_supported", "reason"),
    [
        ("kr", True, None),
        ("us", False, "us_unsupported"),
        ("crypto", True, None),
    ],
)
def test_stock_detail_capabilities_keep_read_only_contract(
    market, orderbook_supported, reason
):
    capabilities = default_capabilities_for_market(market)

    assert capabilities.execution.supported is False
    assert capabilities.execution.reason == "read_only_mvp"
    assert capabilities.options.supported is False
    assert capabilities.options.reason == "out_of_mvp_scope"
    assert capabilities.orderbook.supported is orderbook_supported
    assert capabilities.orderbook.reason == reason
