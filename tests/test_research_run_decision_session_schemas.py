from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest


@pytest.mark.unit
def test_research_run_selector_accepts_uuid_only():
    from app.schemas.research_run_decision_session import ResearchRunSelector

    selector = ResearchRunSelector(
        run_uuid=UUID("11111111-1111-1111-1111-111111111111")
    )

    assert selector.run_uuid == UUID("11111111-1111-1111-1111-111111111111")
    assert selector.market_scope is None
    assert selector.stage is None


@pytest.mark.unit
def test_research_run_selector_accepts_scope_and_stage_only():
    from app.schemas.research_run_decision_session import ResearchRunSelector

    selector = ResearchRunSelector(market_scope="kr", stage="preopen")

    assert selector.run_uuid is None
    assert selector.market_scope == "kr"
    assert selector.stage == "preopen"
    assert selector.status == "open"


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        {
            "run_uuid": "11111111-1111-1111-1111-111111111111",
            "market_scope": "kr",
            "stage": "preopen",
        },
        {},
        {"market_scope": "kr"},
        {"stage": "preopen"},
    ],
)
def test_research_run_selector_rejects_invalid_xor(payload):
    from app.schemas.research_run_decision_session import ResearchRunSelector

    with pytest.raises(ValueError):
        ResearchRunSelector.model_validate(payload)


@pytest.mark.unit
def test_live_refresh_snapshot_round_trips_decimal_as_string():
    from app.schemas.research_run_decision_session import (
        LiveRefreshQuote,
        LiveRefreshSnapshot,
        OrderbookLevel,
        OrderbookSnapshot,
        PendingOrderSnapshot,
        SupportResistanceLevel,
        SupportResistanceSnapshot,
    )

    snapshot = LiveRefreshSnapshot(
        refreshed_at=datetime(2026, 4, 28, 12, 0, tzinfo=UTC),
        quote_by_symbol={
            "AAPL": LiveRefreshQuote(
                price=Decimal("123.4500"),
                as_of=datetime(2026, 4, 28, 12, 1, tzinfo=UTC),
            )
        },
        orderbook_by_symbol={
            "AAPL": OrderbookSnapshot(
                best_bid=OrderbookLevel(
                    price=Decimal("123.40"), quantity=Decimal("10")
                ),
                best_ask=OrderbookLevel(
                    price=Decimal("123.50"), quantity=Decimal("20")
                ),
                total_bid_qty=Decimal("100.5"),
                total_ask_qty=Decimal("200.25"),
            )
        },
        support_resistance_by_symbol={
            "AAPL": SupportResistanceSnapshot(
                nearest_support=SupportResistanceLevel(
                    price=Decimal("120.00"), distance_pct=Decimal("2.50")
                )
            )
        },
        cash_balances={"USD": Decimal("1000.75")},
        holdings_by_symbol={"AAPL": Decimal("3.500")},
        pending_orders=[
            PendingOrderSnapshot(
                order_id="ord-1",
                symbol="AAPL",
                market="us",
                side="buy",
                ordered_price=Decimal("123.45"),
                ordered_qty=Decimal("1.25"),
                remaining_qty=Decimal("0.25"),
            )
        ],
        warnings=["stale_quote"],
    )

    json_payload = snapshot.model_dump_json()

    assert '"123.4500"' in json_payload
    assert '"1000.75"' in json_payload
    assert '"3.500"' in json_payload

    round_tripped = LiveRefreshSnapshot.model_validate_json(json_payload)

    assert round_tripped == snapshot
    assert round_tripped.quote_by_symbol["AAPL"].price == pytest.approx(
        Decimal("123.4500")
    )
    assert round_tripped.cash_balances["USD"] == pytest.approx(Decimal("1000.75"))
    assert round_tripped.holdings_by_symbol["AAPL"] == pytest.approx(Decimal("3.500"))


@pytest.mark.unit
def test_research_run_decision_session_response_shape_validation():
    from app.schemas.research_run_decision_session import (
        ResearchRunDecisionSessionResponse,
    )

    payload = {
        "session_uuid": "22222222-2222-2222-2222-222222222222",
        "session_url": "/sessions/22222222-2222-2222-2222-222222222222",
        "status": "open",
        "research_run_uuid": "11111111-1111-1111-1111-111111111111",
        "refreshed_at": "2026-04-28T12:00:00Z",
        "proposal_count": 3,
        "reconciliation_count": 1,
    }

    response = ResearchRunDecisionSessionResponse.model_validate(payload)

    assert response.session_uuid == UUID("22222222-2222-2222-2222-222222222222")
    assert response.status == "open"
    assert response.advisory_used is False
    assert response.advisory_skipped_reason is None
    assert response.warnings == []


@pytest.mark.unit
def test_research_run_decision_session_response_rejects_bad_shape():
    from app.schemas.research_run_decision_session import (
        ResearchRunDecisionSessionResponse,
    )

    with pytest.raises(ValueError):
        ResearchRunDecisionSessionResponse.model_validate(
            {
                "session_uuid": "22222222-2222-2222-2222-222222222222",
                "session_url": "/sessions/22222222-2222-2222-2222-222222222222",
                "status": "open",
                "research_run_uuid": "11111111-1111-1111-1111-111111111111",
                "refreshed_at": "2026-04-28T12:00:00Z",
                "proposal_count": 3,
                "reconciliation_count": 1,
                "unexpected": True,
            }
        )


@pytest.mark.unit
def test_research_run_decision_session_response_requires_required_fields():
    from app.schemas.research_run_decision_session import (
        ResearchRunDecisionSessionResponse,
    )

    with pytest.raises(ValueError):
        ResearchRunDecisionSessionResponse.model_validate(
            {
                "session_uuid": "22222222-2222-2222-2222-222222222222",
                "session_url": "/sessions/22222222-2222-2222-2222-222222222222",
                "status": "open",
                "research_run_uuid": "11111111-1111-1111-1111-111111111111",
                "proposal_count": 3,
                "reconciliation_count": 1,
            }
        )
