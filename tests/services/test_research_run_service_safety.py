"""Safety: research_run_service must not import broker/order/watch/paper/fill modules."""

from __future__ import annotations

import pytest

from app.services import research_run_service
from tests.services.research_run_safety_helpers import (
    RESEARCH_RUN_FORBIDDEN_PREFIXES,
    assert_module_does_not_import_forbidden,
)


@pytest.mark.unit
def test_research_run_service_does_not_transitively_import_forbidden() -> None:
    assert_module_does_not_import_forbidden(
        "app.services.research_run_service",
        RESEARCH_RUN_FORBIDDEN_PREFIXES,
    )


@pytest.mark.unit
def test_news_brief_candidate_payload_rejects_execution_keys() -> None:
    for forbidden_key in [
        "quantity",
        "price",
        "side",
        "order_type",
        "dry_run",
        "watch",
        "order_intent",
    ]:
        with pytest.raises(ValueError, match="forbidden execution keys"):
            research_run_service._validate_news_brief_candidate_payload(  # noqa: SLF001
                {"symbol": "005930", forbidden_key: True}
            )


@pytest.mark.unit
def test_news_brief_candidate_payload_allows_advisory_only_fields() -> None:
    research_run_service._validate_news_brief_candidate_payload(  # noqa: SLF001
        {
            "symbol": "005930",
            "name": "삼성전자",
            "sector": "반도체",
            "direction": "positive",
            "confidence": 60,
            "reasons": ["news evidence"],
            "warnings": ["news_stale"],
        }
    )
