"""ROB-287 — fail-closed regression for the removed in-process LLM
composition branch.

The ROB-279 ``auto_compose=True`` path used to build a
``RateLimitedGeminiProvider(GeminiProvider(...))`` and synthesize the
report inside auto_trader. ROB-287 retired that path: LLM
reasoning/composition is owned by Hermes, and the request schema
rejects ``auto_compose=True`` at validation time so callers cannot
silently re-enable an in-process LLM path.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.action_report.snapshot_backed.request import ReportGenerationRequest


def test_auto_compose_true_rejected_at_validation() -> None:
    """``auto_compose=True`` is fail-closed since ROB-287."""
    with pytest.raises(ValidationError) as excinfo:
        ReportGenerationRequest(
            market="crypto",
            account_scope="upbit_live",
            created_by_profile="AI_ADVISOR",
            title="DUMMY",
            summary="DUMMY",
            kst_date="2026-05-20",
            auto_compose=True,
        )
    assert "ROB-287" in str(excinfo.value)


def test_auto_compose_default_remains_false() -> None:
    """Default still admits the deterministic path without any LLM."""
    request = ReportGenerationRequest(
        market="crypto",
        account_scope="upbit_live",
        created_by_profile="AI_ADVISOR",
        title="DUMMY",
        summary="DUMMY",
        kst_date="2026-05-20",
    )
    assert request.auto_compose is False
