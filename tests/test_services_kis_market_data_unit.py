"""Pure input-boundary tests split from the mixed KIS market-data suite."""

from unittest.mock import AsyncMock

import pandas as pd
import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kis_inquire_daily_itemchartprice_rejects_non_positive_n(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())

    with pytest.raises(ValueError, match="greater than or equal to 1"):
        await client.inquire_daily_itemchartprice("005930", market="J", n=0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kis_inquire_daily_itemchartprice_clamps_oversized_n_to_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())

    chunk = [
        {
            "stck_bsop_date": (
                pd.Timestamp("2026-01-01") + pd.Timedelta(days=index)
            ).strftime("%Y%m%d"),
            "stck_oprc": str(70000 + index),
            "stck_hgpr": str(70100 + index),
            "stck_lwpr": str(69900 + index),
            "stck_clpr": str(70050 + index),
            "acml_vol": str(1000 + index),
            "acml_tr_pbmn": str(70050000 + index),
        }
        for index in range(250)
    ]
    request_mock = AsyncMock(return_value={"rt_cd": "0", "output2": chunk})
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    frame = await client.inquire_daily_itemchartprice("005930", market="J", n=9999)

    assert len(frame) == 200
    request_mock.assert_awaited_once()
