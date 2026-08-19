from __future__ import annotations

import inspect
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.routers import invest_funding


@pytest.mark.asyncio
async def test_page_get_refreshes_view_without_delivery_api(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    class FakeService:
        def __init__(self, _db):
            pass

        async def refresh_detail(self, **kwargs):
            calls.append(("refresh", kwargs))
            return {"status": "triggered", "delivery": {"action": "none"}}

    monkeypatch.setattr(invest_funding, "FundingAdvisoryService", FakeService)
    monkeypatch.setattr(
        invest_funding,
        "_now",
        lambda: datetime(2026, 8, 15, 0, 0, tzinfo=UTC),
    )
    advisory_id = uuid4()

    result = await invest_funding.get_advisory(
        advisory_id=advisory_id,
        user=SimpleNamespace(id=11),
        db=object(),
        refresh=True,
    )

    assert result["delivery"] == {"action": "none"}
    assert calls == [
        (
            "refresh",
            {
                "advisory_id": advisory_id,
                "owner_user_id": 11,
                "now": datetime(2026, 8, 15, 0, 0, tzinfo=UTC),
            },
        )
    ]
    source = inspect.getsource(invest_funding.get_advisory)
    assert "deliver_claimed_advisory" not in source
    assert "_claim_delivery" not in source


def test_router_write_delegates_to_service_and_never_commits_directly() -> None:
    source = inspect.getsource(invest_funding.declare_external_cash)
    assert "ExternalCashDeclarationService" in source
    assert ".declare(" in source
    assert ".commit(" not in source


def test_invest_api_prefix_remains_csrf_protected() -> None:
    main_source = (invest_funding.__file__ and inspect.getsource(invest_funding)) or ""
    assert 'prefix="/invest/api/funding"' in main_source
    app_main = (Path(invest_funding.__file__).parents[1] / "main.py").read_text(
        encoding="utf-8"
    )
    assert 're.compile(r"^/invest/api/")' not in app_main
