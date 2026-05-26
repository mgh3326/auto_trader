"""Unit tests for KIS mock scalping sell-guard separation (ROB-321 PR1)."""

from __future__ import annotations

import pytest

from app.core.config import settings


@pytest.mark.unit
def test_kis_mock_scalping_disabled_by_default() -> None:
    assert settings.kis_mock_scalping_enabled is False
