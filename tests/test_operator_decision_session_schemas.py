from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest


@pytest.mark.unit
def test_operator_candidate_minimum_fields():
    from app.schemas.operator_decision_session import OperatorCandidate

    cand = OperatorCandidate(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        confidence=70,
        proposal_kind="enter",
    )
    assert cand.symbol == "005930"
    assert cand.instrument_type == "equity_kr"
    assert cand.side == "buy"


@pytest.mark.unit
def test_operator_candidate_rejects_unsupported_symbol_chars():
    from app.schemas.operator_decision_session import OperatorCandidate

    with pytest.raises(ValueError):
        OperatorCandidate(
            symbol="bad symbol!",
            instrument_type="equity_kr",
            confidence=50,
        )


@pytest.mark.unit
def test_operator_request_default_advisory_off():
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )

    req = OperatorDecisionRequest(
        market_scope="kr",
        candidates=[
            OperatorCandidate(
                symbol="005930", instrument_type="equity_kr", confidence=50
            )
        ],
    )
    assert req.include_tradingagents is False
    assert req.source_profile == "operator_request"


@pytest.mark.unit
def test_operator_request_rejects_extra_fields():
    from app.schemas.operator_decision_session import OperatorDecisionRequest

    with pytest.raises(ValueError):
        OperatorDecisionRequest.model_validate(
            {
                "market_scope": "kr",
                "candidates": [
                    {
                        "symbol": "005930",
                        "instrument_type": "equity_kr",
                        "confidence": 50,
                    }
                ],
                "place_order": True,
            }
        )


@pytest.mark.unit
def test_operator_request_caps_candidates():
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )

    too_many = [
        OperatorCandidate(
            symbol=f"AAA{i:03d}", instrument_type="equity_us", confidence=50
        )
        for i in range(21)
    ]
    with pytest.raises(ValueError):
        OperatorDecisionRequest(market_scope="us", candidates=too_many)


@pytest.mark.unit
def test_operator_request_validates_analyst_token_charset():
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )

    cand = [
        OperatorCandidate(symbol="AAPL", instrument_type="equity_us", confidence=50)
    ]
    with pytest.raises(ValueError):
        OperatorDecisionRequest(
            market_scope="us",
            candidates=cand,
            analysts=["BAD-TOKEN"],
        )


@pytest.mark.unit
def test_operator_request_accepts_valid_or_missing_analyst_tokens():
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )

    cand = [
        OperatorCandidate(symbol="AAPL", instrument_type="equity_us", confidence=50)
    ]

    assert (
        OperatorDecisionRequest(
            market_scope="us",
            candidates=cand,
            analysts=None,
        ).analysts
        is None
    )
    assert OperatorDecisionRequest(
        market_scope="us",
        candidates=cand,
        analysts=["market_news", "technical"],
    ).analysts == ["market_news", "technical"]


@pytest.mark.unit
def test_operator_request_accepts_decimal_quantity():
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )

    cand = OperatorCandidate(
        symbol="BTC",
        instrument_type="crypto",
        confidence=40,
        side="buy",
        amount=Decimal("100000"),
    )
    OperatorDecisionRequest(market_scope="crypto", candidates=[cand])


@pytest.mark.unit
def test_crypto_candidate_accepts_complete_paper_workflow_metadata():
    from app.schemas.operator_decision_session import OperatorCandidate
    from app.services.crypto_execution_mapping import (
        build_operator_candidate_crypto_metadata,
    )

    cand = OperatorCandidate(
        symbol="KRW-BTC",
        instrument_type="crypto",
        confidence=45,
        side="buy",
        proposal_kind="pullback_watch",
        **build_operator_candidate_crypto_metadata("KRW-BTC"),
    )

    assert cand.signal_symbol == "KRW-BTC"
    assert cand.signal_venue == "upbit"
    assert cand.execution_symbol == "BTC/USD"
    assert cand.execution_venue == "alpaca_paper"
    assert cand.execution_mode == "paper"
    assert cand.execution_asset_class == "crypto"
    assert cand.workflow_stage == "crypto_weekend"
    assert cand.purpose == "paper_plumbing_smoke"
    assert cand.preview_payload == {
        "symbol": "BTC/USD",
        "side": "buy",
        "type": "limit",
        "notional": "10",
        "limit_price": "1.00",
        "time_in_force": "gtc",
        "asset_class": "crypto",
    }
    assert "Signal source: Upbit KRW-BTC" in cand.approval_copy


@pytest.mark.unit
def test_crypto_candidate_rejects_partial_paper_workflow_metadata():
    from app.schemas.operator_decision_session import OperatorCandidate
    from app.services.crypto_execution_mapping import (
        build_operator_candidate_crypto_metadata,
    )

    metadata = build_operator_candidate_crypto_metadata("KRW-BTC")
    metadata.pop("execution_venue")

    with pytest.raises(ValueError):
        OperatorCandidate(
            symbol="KRW-BTC",
            instrument_type="crypto",
            confidence=45,
            **metadata,
        )


@pytest.mark.unit
def test_crypto_candidate_rejects_loose_nested_workflow_metadata():
    from app.schemas.operator_decision_session import OperatorCandidate
    from app.services.crypto_execution_mapping import (
        build_operator_candidate_crypto_metadata,
    )

    with pytest.raises(ValueError):
        OperatorCandidate(
            symbol="KRW-BTC",
            instrument_type="crypto",
            confidence=45,
            crypto_paper_workflow=build_operator_candidate_crypto_metadata("KRW-BTC"),
        )


@pytest.mark.unit
def test_non_crypto_candidate_rejects_crypto_paper_workflow_metadata():
    from app.schemas.operator_decision_session import OperatorCandidate
    from app.services.crypto_execution_mapping import (
        build_operator_candidate_crypto_metadata,
    )

    with pytest.raises(ValueError):
        OperatorCandidate(
            symbol="AAPL",
            instrument_type="equity_us",
            confidence=50,
            **build_operator_candidate_crypto_metadata("KRW-BTC"),
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_preview_payload",
    [
        {"symbol": "BTC/USD", "side": "sell"},
        {"symbol": "BTC/USD", "confirm": True},
        {"symbol": "BTC/USD", "order_id": "paper-order-1"},
    ],
)
def test_crypto_candidate_rejects_submit_like_or_sell_preview_payload(
    bad_preview_payload: dict[str, object],
):
    from app.schemas.operator_decision_session import OperatorCandidate
    from app.services.crypto_execution_mapping import (
        build_operator_candidate_crypto_metadata,
    )

    metadata = build_operator_candidate_crypto_metadata("KRW-BTC")
    metadata["preview_payload"] = bad_preview_payload

    with pytest.raises(ValueError):
        OperatorCandidate(
            symbol="KRW-BTC",
            instrument_type="crypto",
            confidence=45,
            **metadata,
        )


@pytest.mark.unit
def test_crypto_candidate_rejects_mismatched_signal_execution_mapping():
    from app.schemas.operator_decision_session import OperatorCandidate
    from app.services.crypto_execution_mapping import (
        build_operator_candidate_crypto_metadata,
    )

    metadata = build_operator_candidate_crypto_metadata("KRW-BTC")
    metadata["execution_symbol"] = "ETH/USD"
    metadata["preview_payload"]["symbol"] = "ETH/USD"

    with pytest.raises(ValueError):
        OperatorCandidate(
            symbol="KRW-BTC",
            instrument_type="crypto",
            confidence=45,
            **metadata,
        )


@pytest.mark.unit
def test_crypto_candidate_rejects_unsupported_signal_execution_mapping():
    from app.schemas.operator_decision_session import OperatorCandidate
    from app.services.crypto_execution_mapping import (
        build_operator_candidate_crypto_metadata,
    )

    metadata = build_operator_candidate_crypto_metadata("KRW-BTC")
    metadata["signal_symbol"] = "KRW-XRP"
    metadata["execution_symbol"] = "XRP/USD"
    metadata["preview_payload"]["symbol"] = "XRP/USD"

    with pytest.raises(ValueError):
        OperatorCandidate(
            symbol="KRW-XRP",
            instrument_type="crypto",
            confidence=45,
            **metadata,
        )


@pytest.mark.unit
def test_crypto_candidate_rejects_preview_payload_symbol_mismatch():
    from app.schemas.operator_decision_session import OperatorCandidate
    from app.services.crypto_execution_mapping import (
        build_operator_candidate_crypto_metadata,
    )

    metadata = build_operator_candidate_crypto_metadata("KRW-BTC")
    metadata["preview_payload"]["symbol"] = "ETH/USD"

    with pytest.raises(ValueError):
        OperatorCandidate(
            symbol="KRW-BTC",
            instrument_type="crypto",
            confidence=45,
            **metadata,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("metadata_patch", "error_match"),
    [
        ({"approval_copy": []}, "approval_copy must be a non-empty list"),
        ({"approval_copy": [""]}, "approval_copy must contain non-empty strings"),
        ({"signal_symbol": "BTC"}, "crypto signal_symbol must be an Upbit KRW symbol"),
        (
            {"execution_symbol": "BTC-USDT"},
            "crypto execution_symbol must be an Alpaca USD pair",
        ),
    ],
)
def test_crypto_candidate_rejects_invalid_workflow_scalar_or_copy_fields(
    metadata_patch: dict[str, object], error_match: str
):
    from app.schemas.operator_decision_session import OperatorCandidate
    from app.services.crypto_execution_mapping import (
        build_operator_candidate_crypto_metadata,
    )

    metadata = build_operator_candidate_crypto_metadata("KRW-BTC")
    metadata.update(metadata_patch)

    with pytest.raises(ValueError, match=error_match):
        OperatorCandidate(
            symbol="KRW-BTC",
            instrument_type="crypto",
            confidence=45,
            **metadata,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("preview_patch", "error_match"),
    [
        ({"side": "sell"}, "crypto preview side must be buy"),
        ({"type": "market"}, "crypto preview type must be limit"),
        ({"asset_class": "us_equity"}, "crypto preview asset_class must be crypto"),
        ({"time_in_force": "day"}, "crypto preview time_in_force is unsupported"),
        ({"symbol": "BTC-USDT"}, "crypto preview symbol must be an Alpaca USD pair"),
        (
            {"notional": "not-a-number"},
            "crypto preview notional and limit_price must be numeric",
        ),
        (
            {"notional": Decimal("NaN")},
            "crypto preview notional and limit_price must be finite",
        ),
        (
            {"limit_price": Decimal("Infinity")},
            "crypto preview notional and limit_price must be finite",
        ),
        ({"notional": "51"}, "crypto preview notional must be > 0 and <= 50"),
        ({"limit_price": "0"}, "crypto preview limit_price must be > 0"),
    ],
)
def test_crypto_candidate_rejects_invalid_preview_field_values(
    preview_patch: dict[str, object], error_match: str
):
    from app.schemas.operator_decision_session import OperatorCandidate
    from app.services.crypto_execution_mapping import (
        build_operator_candidate_crypto_metadata,
    )

    metadata = build_operator_candidate_crypto_metadata("KRW-BTC")
    metadata["preview_payload"] = deepcopy(metadata["preview_payload"])
    metadata["preview_payload"].update(preview_patch)

    with pytest.raises(ValueError, match=error_match):
        OperatorCandidate(
            symbol="KRW-BTC",
            instrument_type="crypto",
            confidence=45,
            **metadata,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("metadata_patch", "error_match"),
    [
        ({"signal_venue": "binance"}, "crypto signal_venue must be upbit"),
        (
            {"execution_venue": "alpaca_live"},
            "crypto execution_venue must be alpaca_paper",
        ),
        ({"execution_mode": "live"}, "crypto execution_mode must be paper"),
        (
            {"execution_asset_class": "us_equity"},
            "crypto execution_asset_class must be crypto",
        ),
        ({"workflow_stage": "weekday_open"}, "crypto workflow_stage is unsupported"),
        ({"purpose": "submit_order"}, "crypto purpose is unsupported"),
    ],
)
def test_crypto_workflow_scalar_helper_rejects_invalid_values(
    metadata_patch: dict[str, object], error_match: str
):
    from app.schemas.operator_decision_session import OperatorCandidate
    from app.services.crypto_execution_mapping import (
        build_operator_candidate_crypto_metadata,
    )

    metadata = build_operator_candidate_crypto_metadata("KRW-BTC")
    metadata.update(metadata_patch)

    with pytest.raises(ValueError, match=error_match):
        OperatorCandidate._validate_crypto_workflow_scalars(metadata)


@pytest.mark.unit
def test_crypto_preview_payload_helper_rejects_non_object_payload():
    from app.schemas.operator_decision_session import OperatorCandidate

    with pytest.raises(ValueError, match="preview_payload must be an object"):
        OperatorCandidate._validate_crypto_preview_payload(
            None,
            execution_symbol="BTC/USD",
        )
