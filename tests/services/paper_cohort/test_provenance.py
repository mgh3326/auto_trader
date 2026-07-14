from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.services.brokers.paper.contracts import PaperOrderRequest
from app.services.paper_cohort.contracts import PaperCohortError
from app.services.paper_cohort.provenance import PaperCohortProvenanceVerifier

pytestmark = pytest.mark.unit


@dataclass
class UnusedValidationService:
    calls: int = 0

    async def authorize_order_submission(self, caller_id, identity):
        self.calls += 1
        raise AssertionError("authorization must not run for unknown intent")


@pytest.mark.asyncio
async def test_unknown_intent_fails_before_rob848_authorization(db_session) -> None:
    service = UnusedValidationService()
    verifier = PaperCohortProvenanceVerifier(
        db_session,
        validation_service=service,
        caller_id="paper-cohort-runner",
    )
    request = PaperOrderRequest.model_construct(intent_id="missing-intent")

    with pytest.raises(PaperCohortError) as exc_info:
        await verifier.verify(request)
    assert exc_info.value.reason_code == "provenance_mismatch"
    assert service.calls == 0
