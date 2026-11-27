"""Unit tests for the scraping-backed DART daily filing helper (ROB-1071)."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from app.services.market_events import dart_helpers
from app.services.market_events.normalizers import normalize_dart_disclosure_row


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_returns_json_safe_official_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.market_events import dart_capital

    page = {
        "status": "000",
        "list": [
            {
                "corp_code": "00000001",
                "corp_name": "테스트",
                "stock_code": "123456",
                "corp_cls": "Y",
                "report_nm": "주요사항보고서",
                "rcept_no": "20260724000001",
                "flr_nm": "테스트",
                "rcept_dt": "20260724",
                "rm": "",
            }
        ],
        "total_page": 1,
    }
    monkeypatch.setattr(dart_capital, "request_list_page", AsyncMock(return_value=page))

    rows = await dart_helpers.fetch_dart_filings_for_date(date(2026, 7, 24))

    assert rows == page["list"]
    event, _ = normalize_dart_disclosure_row(rows[0])
    assert event["event_date"] == date(2026, 7, 24)
    assert event["symbol"] == "123456"
    json.dumps(event["raw_payload_json"])


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_refuses_a_page_without_a_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.market_events import dart_capital

    monkeypatch.setattr(
        dart_capital,
        "request_list_page",
        AsyncMock(return_value={"status": "000", "total_page": 1}),
    )

    with pytest.raises(dart_capital.DartDetailError) as raised:
        await dart_helpers.fetch_dart_filings_for_date(date(2026, 7, 24))

    assert raised.value.failure_code == "bad_response"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_corp_registry_is_cached_and_refreshed_after_ttl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache_path = tmp_path / "registry.json"
    monkeypatch.setattr(dart_helpers, "DART_CORP_REGISTRY_CACHE_PATH", cache_path)
    monkeypatch.setattr(
        dart_helpers, "DART_CORP_REGISTRY_LOCK_PATH", tmp_path / "registry.lock"
    )
    cache_path.write_text("{}", encoding="utf-8")
    frames = [
        pd.DataFrame(
            [
                {
                    "corp_code": "00000001",
                    "corp_name": "Synthetic Co",
                    "stock_code": "123456",
                    "modify_date": "20260101",
                }
            ]
        ),
        pd.DataFrame(
            [
                {
                    "corp_code": "00000002",
                    "corp_name": "Synthetic Co",
                    "stock_code": "654321",
                    "modify_date": "20260102",
                }
            ]
        ),
    ]
    fetcher = MagicMock(side_effect=frames)
    monkeypatch.setattr(
        "app.services.disclosures.dart._opendartreader",
        lambda _name: fetcher,
    )

    first = await dart_helpers.fetch_dart_corp_registry()
    second = await dart_helpers.fetch_dart_corp_registry()
    assert first == second
    assert fetcher.call_count == 1

    expired_at = (
        cache_path.stat().st_mtime - dart_helpers.DART_CORP_REGISTRY_TTL_SECONDS - 1
    )
    os.utime(cache_path, (expired_at, expired_at))
    refreshed = await dart_helpers.fetch_dart_corp_registry()

    assert fetcher.call_count == 2
    assert refreshed["Synthetic Co"][0]["stock_code"] == "654321"
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_corp_registry_is_unavailable_and_not_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache_path = tmp_path / "registry.json"
    monkeypatch.setattr(dart_helpers, "DART_CORP_REGISTRY_CACHE_PATH", cache_path)
    monkeypatch.setattr(
        dart_helpers, "DART_CORP_REGISTRY_LOCK_PATH", tmp_path / "registry.lock"
    )
    fetcher = MagicMock(
        return_value=pd.DataFrame(
            columns=["corp_code", "corp_name", "stock_code", "modify_date"]
        )
    )
    monkeypatch.setattr(
        "app.services.disclosures.dart._opendartreader",
        lambda _name: fetcher,
    )

    result = await dart_helpers.fetch_dart_corp_registry()

    assert result is None
    assert fetcher.call_count == 1
    assert not cache_path.exists()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_corp_registry_failure_never_exposes_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sentinel = "SYNTHETIC_DART_KEY_SENTINEL"
    cache_path = tmp_path / "registry.json"
    monkeypatch.setattr(dart_helpers, "DART_CORP_REGISTRY_CACHE_PATH", cache_path)
    monkeypatch.setattr(
        dart_helpers, "DART_CORP_REGISTRY_LOCK_PATH", tmp_path / "registry.lock"
    )
    monkeypatch.setattr(dart_helpers.settings, "opendart_api_key", sentinel)

    def fail_with_key(_api_key: str) -> None:
        raise RuntimeError(f"request failed for {sentinel}")

    monkeypatch.setattr(
        "app.services.disclosures.dart._opendartreader",
        lambda _name: fail_with_key,
    )

    result = await dart_helpers.fetch_dart_corp_registry()

    assert result is None
    assert sentinel not in caplog.text
    assert all(sentinel not in path.read_text() for path in tmp_path.iterdir())
    assert not cache_path.exists()
