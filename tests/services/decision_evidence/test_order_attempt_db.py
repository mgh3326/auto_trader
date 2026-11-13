"""CI-only PostgreSQL round trips for the three attempt-resolution sequences."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.services.decision_evidence import DecisionEvidence, DecisionEvidenceService


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome_lost,link_lost,expected",
    [
        (False, False, "recorded"),
        (True, False, "ambiguous_attempts"),
        (True, True, "attempt_unconfirmed"),
    ],
)
async def test_attempt_sequences_round_trip(
    db_session, outcome_lost, link_lost, expected
):
    service = DecisionEvidenceService(db_session)

    async def decision():
        return await service.record(
            DecisionEvidence(
                decision_kind="proposal",
                decided_by="test",
                market="equity_kr",
                account_mode="kis_live",
                symbol="005930",
                side="buy",
                decided_at=datetime.now(UTC),
            ),
            [],
            [],
        )

    da, db = await decision(), await decision()
    a, b, key = uuid4(), uuid4(), str(uuid4())
    await service.link_order(
        attempt_id=a,
        decision_id=da,
        account_scope="kis_live",
        send_key=key,
        proposal_id=uuid4(),
        rung_index=0,
    )
    if not outcome_lost:
        await service.record_outcome(a, "proven_not_sent")
    if not link_lost:
        await service.link_order(
            attempt_id=b,
            decision_id=db,
            account_scope="kis_live",
            send_key=key,
            proposal_id=uuid4(),
            rung_index=0,
        )
        await service.record_outcome(b, "sent")
    result = await db_session.execute(
        text("""
        SELECT l.decision_id, o.outcome FROM review.decision_order_links l
        LEFT JOIN review.decision_order_link_outcomes o USING (attempt_id)
        WHERE l.account_scope='kis_live' AND l.send_key=:key
          AND o.outcome IS DISTINCT FROM 'proven_not_sent'
    """),
        {"key": key},
    )
    live = list(result)
    decisions = {row.decision_id for row in live}
    classification = (
        "ambiguous_attempts"
        if len(decisions) > 1
        else (
            "recorded"
            if any(row.outcome == "sent" for row in live)
            else "attempt_unconfirmed"
        )
    )
    assert classification == expected
    if expected == "recorded":
        assert decisions == {db}
