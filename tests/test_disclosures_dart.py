from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.services.disclosures import dart


@pytest.fixture
def restore_dart_indices():
    prev_name = dict(dart.NAME_TO_CORP)
    try:
        yield
    finally:
        dart.NAME_TO_CORP.clear()
        dart.NAME_TO_CORP.update(prev_name)


@pytest.mark.asyncio
async def test_list_filings_missing_api_key_returns_explicit_error(monkeypatch):
    monkeypatch.setattr(settings, "opendart_api_key", "")
    prime_mock = AsyncMock(side_effect=AssertionError("prime_index should not run"))
    monkeypatch.setattr(dart, "prime_index", prime_mock)

    result = await dart.list_filings("삼성전자", days=30, limit=5)

    assert result["success"] is False
    assert result["error_code"] == "missing_api_key"
    assert result["filings"] == []
    prime_mock.assert_not_called()


@pytest.mark.asyncio
async def test_list_filings_unresolved_symbol_returns_failure(
    monkeypatch, restore_dart_indices
):
    monkeypatch.setattr(settings, "opendart_api_key", "dummy-key")
    dart.NAME_TO_CORP.clear()
    monkeypatch.setattr(dart, "prime_index", AsyncMock(return_value=None))
    monkeypatch.setattr(dart, "resolve_symbol", lambda symbol: (None, None))

    result = await dart.list_filings("없는회사", days=30, limit=5)

    assert result["success"] is False
    assert result["error_code"] == "symbol_not_resolved"
    assert result["filings"] == []


@pytest.mark.asyncio
async def test_list_filings_returns_list_on_success(monkeypatch, restore_dart_indices):
    monkeypatch.setattr(settings, "opendart_api_key", "dummy-key")
    dart.NAME_TO_CORP["삼성전자"] = "00126380"
    monkeypatch.setattr(
        dart,
        "resolve_symbol",
        lambda symbol: ("00126380", "삼성전자"),
    )
    monkeypatch.setattr(dart.dart_fss, "set_api_key", lambda *args, **kwargs: None)

    class FakeCorp:
        def __init__(self, corp_code):
            self.corp_code = corp_code

        def search_filings(self, **kwargs):
            _ = kwargs
            return [
                SimpleNamespace(
                    rcept_dt="20260213",
                    report_nm="기타경영사항(자율공시)",
                    rcept_no="20260213000123",
                )
            ]

    monkeypatch.setattr(dart.dart_fss.corp, "Corp", FakeCorp)

    async def run_sync(sync_fn):
        return sync_fn()

    monkeypatch.setattr(dart.asyncio, "to_thread", run_sync)

    result = await dart.list_filings("삼성전자", days=30, limit=5)

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["corp_name"] == "삼성전자"
    assert result[0]["report_nm"] == "기타경영사항(자율공시)"
    assert result[0]["rcp_no"] == "20260213000123"
