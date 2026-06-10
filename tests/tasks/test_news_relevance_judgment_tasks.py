"""ROB-506 — news_relevance.judge_pending task gating/registration."""

from __future__ import annotations

import pytest

from app.core.config import Settings


@pytest.mark.unit
def test_async_judgment_settings_default_off() -> None:
    fields = Settings.model_fields
    assert fields["NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED"].default is False
    assert fields["NEWS_RELEVANCE_JUDGMENT_WEBHOOK_URL"].default == ""
    assert fields["NEWS_RELEVANCE_JUDGMENT_TOKEN"].default == ""
    assert fields["NEWS_RELEVANCE_JUDGMENT_TIMEOUT_S"].default == 120.0
    assert fields["NEWS_RELEVANCE_JUDGMENT_BATCH_LIMIT"].default == 50
