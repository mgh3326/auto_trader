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
    assert result["stck_oprc"] is None
    assert result["stck_hgpr"] is None
    assert result["stck_lwpr"] is None
    assert result["stck_shrn_iscd"] is None
    assert "rt_cd" in result
    call = request.await_args
    assert call is not None
    assert call.kwargs["params"]["FID_INPUT_DATE_1"] == "20260729"
    assert call.kwargs["params"]["FID_INPUT_DATE_2"] == "20260729"


async def test_daily_raw_evidence_returns_full_raw_ohlcv_and_return_code(
    monkeypatch,
) -> None:
    """ROB-1172 AC2: the completion manifest reconciles OHLCV, not just close."""
    client = KISClient()
    request = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output2": [
                {
                    "stck_bsop_date": "20260729",
                    "stck_oprc": "69500",
                    "stck_hgpr": "70700",
                    "stck_lwpr": "69100",
                    "stck_clpr": "70000",
                    "acml_vol": "100",
                    "acml_tr_pbmn": "7000000",
                }
            ],
        }
    )
    monkeypatch.setattr(client._market_data, "_request_with_token_retry", request)

    result = await client.inquire_daily_itemchartprice_raw_evidence(
        "005930", dt.date(2026, 7, 29)
    )

    assert result["stck_oprc"] == "69500"
    assert result["stck_hgpr"] == "70700"
    assert result["stck_lwpr"] == "69100"
    assert result["acml_vol"] == "100"
    assert result["acml_tr_pbmn"] == "7000000"
    assert result["rt_cd"] == "0"


async def test_daily_raw_evidence_keeps_missing_ohlc_absent_without_fallback(
    monkeypatch,
) -> None:
    client = KISClient()
    request = AsyncMock(return_value={"rt_cd": "0", "output2": []})
    monkeypatch.setattr(client._market_data, "_request_with_token_retry", request)

    result = await client.inquire_daily_itemchartprice_raw_evidence(
        "005930", dt.date(2026, 7, 29)
    )

    assert result["stck_bsop_date"] is None
    for field in ("stck_oprc", "stck_hgpr", "stck_lwpr", "stck_clpr"):
        assert result[field] is None
