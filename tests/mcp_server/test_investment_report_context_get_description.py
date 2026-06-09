"""ROB-459 P3 — context_get description이 확장된 advisory 집합을 반영하는지."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import investment_reports_handlers as h

pytestmark = pytest.mark.unit


def test_context_get_description_mentions_claude_advisor():
    desc = h.CONTEXT_GET_DESCRIPTION
    assert "CLAUDE_ADVISOR" in desc
    assert "advisory_only" in desc
    # 운영자 확장 설정도 노출되어야 한다.
    assert "INVESTMENT_ADVISORY_DRAFT_PROFILES" in desc
