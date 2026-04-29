"""ROB-26 manual-run script unit tests."""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, patch

from scripts import run_research_run_refresh as mod


@pytest.mark.asyncio
async def test_dry_run_no_operator(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "research_run_refresh_user_id", None, raising=False)
    result = await mod._dry_run(stage="preopen", market_scope="kr")
    assert result == {"status": "dry_run", "reason": "no_operator_user_configured"}


def test_main_dry_run_default(monkeypatch, capsys):
    from app.core.config import settings

    monkeypatch.setattr(settings, "research_run_refresh_user_id", None, raising=False)
    monkeypatch.setattr("sys.argv", ["prog", "--stage", "preopen"])
    mod.main()
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["status"] == "dry_run"
