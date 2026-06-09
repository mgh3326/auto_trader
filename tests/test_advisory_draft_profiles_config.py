"""ROB-459 P3 — INVESTMENT_ADVISORY_DRAFT_PROFILES env 파싱.

context_get(draft_policy="advisory_only")에서 admit할 advisory 프로필을
운영자가 comma-separated / JSON-list env로 확장할 수 있어야 한다(default와 UNION).
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


def _settings(**env: str):
    """Fresh Settings with the given env overrides applied."""
    with patch.dict(os.environ, env, clear=False):
        from app.core.config import Settings

        return Settings()


def test_advisory_profiles_default_empty():
    s = _settings()
    assert s.INVESTMENT_ADVISORY_DRAFT_PROFILES == []


def test_advisory_profiles_parses_comma_separated():
    s = _settings(INVESTMENT_ADVISORY_DRAFT_PROFILES="OPERATOR_ADVISOR, FOO_ADVISOR")
    assert s.INVESTMENT_ADVISORY_DRAFT_PROFILES == ["OPERATOR_ADVISOR", "FOO_ADVISOR"]


def test_advisory_profiles_parses_json_list():
    s = _settings(INVESTMENT_ADVISORY_DRAFT_PROFILES='["A_ADVISOR", "B_ADVISOR"]')
    assert s.INVESTMENT_ADVISORY_DRAFT_PROFILES == ["A_ADVISOR", "B_ADVISOR"]


def test_advisory_profiles_blank_is_empty():
    s = _settings(INVESTMENT_ADVISORY_DRAFT_PROFILES="")
    assert s.INVESTMENT_ADVISORY_DRAFT_PROFILES == []
