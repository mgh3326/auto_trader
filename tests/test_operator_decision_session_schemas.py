from __future__ import annotations

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
