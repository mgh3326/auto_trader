from __future__ import annotations

import inspect
from decimal import Decimal

import pytest
from pydantic import ValidationError


@pytest.mark.unit
def test_maps_allowed_upbit_symbols_to_alpaca_paper_execution_metadata():
    from app.services.crypto_execution_mapping import map_upbit_to_alpaca_paper

    expected = {
        "KRW-BTC": "BTC/USD",
        "KRW-ETH": "ETH/USD",
        "KRW-SOL": "SOL/USD",
    }

    for signal_symbol, execution_symbol in expected.items():
        mapping = map_upbit_to_alpaca_paper(signal_symbol)

        assert mapping.signal_symbol == signal_symbol
        assert mapping.signal_venue == "upbit"
        assert mapping.execution_symbol == execution_symbol
        assert mapping.execution_venue == "alpaca_paper"
        assert mapping.asset_class == "crypto"
        assert mapping.execution_mode == "paper"


@pytest.mark.unit
def test_mapping_normalizes_whitespace_and_case():
    from app.services.crypto_execution_mapping import map_upbit_to_alpaca_paper

    mapping = map_upbit_to_alpaca_paper(" krw-btc ")

    assert mapping.signal_symbol == "KRW-BTC"
    assert mapping.execution_symbol == "BTC/USD"


@pytest.mark.unit
@pytest.mark.parametrize("signal_symbol", ["BTC/USD", "KRW-XRP", "USDT-BTC", "BTC", ""])
def test_unsupported_symbols_fail_closed(signal_symbol: str):
    from app.services.crypto_execution_mapping import (
        CryptoExecutionMappingError,
        map_upbit_to_alpaca_paper,
    )

    with pytest.raises(CryptoExecutionMappingError):
        map_upbit_to_alpaca_paper(signal_symbol)


@pytest.mark.unit
def test_default_preview_payload_is_plumbing_smoke():
    from app.services.crypto_execution_mapping import (
        build_alpaca_paper_crypto_preview_payload,
        map_upbit_to_alpaca_paper,
    )

    payload = build_alpaca_paper_crypto_preview_payload(
        map_upbit_to_alpaca_paper("KRW-BTC")
    )

    assert payload.model_dump(mode="json") == {
        "symbol": "BTC/USD",
        "side": "buy",
        "type": "limit",
        "notional": "10",
        "limit_price": "1.00",
        "time_in_force": "gtc",
        "asset_class": "crypto",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("notional", "limit_price"),
    [
        (Decimal("50.01"), Decimal("1.00")),
        (Decimal("0"), Decimal("1.00")),
        (Decimal("10"), Decimal("0")),
    ],
)
def test_preview_payload_rejects_over_cap_and_non_positive_values(
    notional: Decimal, limit_price: Decimal
):
    from app.services.crypto_execution_mapping import (
        build_alpaca_paper_crypto_preview_payload,
        map_upbit_to_alpaca_paper,
    )

    with pytest.raises(ValidationError):
        build_alpaca_paper_crypto_preview_payload(
            map_upbit_to_alpaca_paper("KRW-BTC"),
            notional=notional,
            limit_price=limit_price,
        )


@pytest.mark.unit
def test_approval_metadata_copy_preserves_both_venues():
    from app.services.crypto_execution_mapping import (
        build_crypto_paper_approval_metadata,
    )

    metadata = build_crypto_paper_approval_metadata("KRW-BTC")

    assert "Signal source: Upbit KRW-BTC" in metadata.approval_copy
    assert "Execution venue: Alpaca Paper BTC/USD" in metadata.approval_copy
    assert "Order: buy limit $10 @ $1.00 GTC" in metadata.approval_copy
    assert metadata.stage == "crypto_weekend"


@pytest.mark.unit
def test_operator_candidate_crypto_metadata_helper_is_json_ready():
    from app.services.crypto_execution_mapping import (
        build_operator_candidate_crypto_metadata,
    )

    metadata = build_operator_candidate_crypto_metadata("KRW-ETH")

    assert metadata == {
        "signal_symbol": "KRW-ETH",
        "signal_venue": "upbit",
        "execution_symbol": "ETH/USD",
        "execution_venue": "alpaca_paper",
        "execution_mode": "paper",
        "execution_asset_class": "crypto",
        "workflow_stage": "crypto_weekend",
        "purpose": "paper_plumbing_smoke",
        "preview_payload": {
            "symbol": "ETH/USD",
            "side": "buy",
            "type": "limit",
            "notional": "10",
            "limit_price": "1.00",
            "time_in_force": "gtc",
            "asset_class": "crypto",
        },
        "approval_copy": [
            "Signal source: Upbit KRW-ETH",
            "Execution venue: Alpaca Paper ETH/USD",
            "Purpose: paper_plumbing_smoke",
            "Order: buy limit $10 @ $1.00 GTC",
        ],
    }


@pytest.mark.unit
def test_service_has_no_broker_side_effect_imports():
    import app.services.crypto_execution_mapping as module

    source = inspect.getsource(module)

    forbidden_tokens = [
        "AlpacaPaperBrokerService",
        "alpaca_paper_submit_order",
        "alpaca_paper_cancel_order",
        "submit_order",
        "cancel_order",
        "place_order",
        "modify_order",
        "watch_alert",
    ]
    for token in forbidden_tokens:
        assert token not in source
