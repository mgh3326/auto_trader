from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

import pytest

from app.services.brokers.kis.client import KISClient

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_quote_raw_evidence_preserves_missing_timestamp_without_fallback(
    monkeypatch,
) -> None:
    client = KISClient()
    request = AsyncMock(
        return_value={
            "output": {
                "stck_shrn_iscd": "005930",
                "stck_bsop_date": None,
                "stck_prpr": "70000",
            }
        }
    )
    monkeypatch.setattr(client._market_data, "_request_with_token_retry", request)

    result = await client.inquire_price_raw_evidence("005930")

    assert result["stck_bsop_date"] is None
    assert result["stck_cntg_hour"] is None
    assert result["stck_prpr"] == "70000"
    call = request.await_args
    assert call is not None
    assert call.kwargs["api_name"] == "inquire_price_raw_evidence"


async def test_daily_raw_evidence_returns_only_exact_session_row(
    monkeypatch,
) -> None:
    client = KISClient()
    request = AsyncMock(
        return_value={
            "output2": [
                {
                    "stck_bsop_date": "20260729",
                    "stck_clpr": "70000",
                    "acml_vol": "100",
                    "acml_tr_pbmn": "7000000",
                }
            ]
        }
    )
    monkeypatch.setattr(client._market_data, "_request_with_token_retry", request)

    result = await client.inquire_daily_itemchartprice_raw_evidence(
        "005930", dt.date(2026, 7, 29)
    )

    assert result["stck_bsop_date"] == "20260729"
    assert result["stck_clpr"] == "70000"
    call = request.await_args
    assert call is not None
    assert call.kwargs["params"]["FID_INPUT_DATE_1"] == "20260729"
    assert call.kwargs["params"]["FID_INPUT_DATE_2"] == "20260729"
