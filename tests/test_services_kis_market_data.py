import logging
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pandas as pd
import pytest


class TestKISOverseasDailyPrice:
    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_overseas_daily_price_parses_output2(
        self, mock_settings, mock_client_class
    ):
        from app.services.brokers.kis.client import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output2": [
                {
                    "xymd": "20260102",
                    "open": "190.5",
                    "high": "193.0",
                    "low": "189.8",
                    "clos": "192.2",
                    "tvol": "1000",
                },
                {
                    "xymd": "20260103",
                    "open": "192.3",
                    "high": "194.1",
                    "low": "191.0",
                    "clos": "193.8",
                    "tvol": "1200",
                },
            ],
        }
        mock_client.get.return_value = mock_response

        client = KISClient()
        client._ensure_token = AsyncMock(return_value=None)
        client._token_manager = AsyncMock()

        result = await client.inquire_overseas_daily_price(symbol="AAPL", n=2)

        assert len(result) == 2
        assert list(result.columns) == [
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]
        assert float(result.iloc[-1]["close"]) == 193.8

        params = mock_client.get.call_args.kwargs["params"]
        assert params["GUBN"] == "0"
        assert params["SYMB"] == "AAPL"

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_overseas_daily_price_retries_on_expired_token(
        self, mock_settings, mock_client_class
    ):
        from app.services.brokers.kis.client import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        expired_response = MagicMock()
        expired_response.status_code = 200
        expired_response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "EGW00123",
            "msg1": "token expired",
        }

        success_response = MagicMock()
        success_response.status_code = 200
        success_response.json.return_value = {
            "rt_cd": "0",
            "output2": [
                {
                    "xymd": "20260103",
                    "open": "192.3",
                    "high": "194.1",
                    "low": "191.0",
                    "clos": "193.8",
                    "tvol": "1200",
                }
            ],
        }

        mock_client.get.side_effect = [expired_response, success_response]

        client = KISClient()
        client._ensure_token = AsyncMock(return_value=None)
        client._token_manager = AsyncMock()
        client._token_manager.clear_token = AsyncMock(return_value=None)

        result = await client.inquire_overseas_daily_price(symbol="AAPL", n=1)

        assert len(result) == 1
        assert mock_client.get.call_count == 2
        client._token_manager.clear_token.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exchange_code", "expected_excd"),
    [("NASD", "NAS"), ("NYSE", "NYS"), ("AMEX", "AMS")],
)
async def test_kis_inquire_overseas_minute_chart_maps_exchange_codes_and_returns_empty_page(
    monkeypatch,
    exchange_code,
    expected_excd,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={"rt_cd": "0", "output1": {"next": "", "more": "N"}, "output2": []}
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    page = await client.inquire_overseas_minute_chart(
        "BRK.B", exchange_code=exchange_code
    )

    assert page.frame.empty
    assert list(page.frame.columns) == [
        "datetime",
        "date",
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "value",
    ]
    assert page.has_more is False
    assert page.next_keyb is None

    request_mock.assert_awaited_once()
    await_args = request_mock.await_args
    assert await_args is not None
    assert await_args.args[0] == "GET"
    assert await_args.args[1].endswith("/inquire-time-itemchartprice")
    assert await_args.kwargs["tr_id"] == "HHDFS76950200"
    assert await_args.kwargs["api_name"] == "inquire_overseas_minute_chart"

    params = await_args.kwargs["params"]
    assert params["AUTH"] == ""
    assert params["EXCD"] == expected_excd
    assert params["SYMB"] == "BRK/B"
    assert params["NMIN"] == "1"
    assert params["PINC"] == "1"
    assert params["NEXT"] == ""
    assert params["NREC"] == "120"
    assert params["FILL"] == ""
    assert params["KEYB"] == ""


@pytest.mark.asyncio
async def test_kis_inquire_overseas_minute_chart_marks_continuation_when_keyb_given(
    monkeypatch,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={"rt_cd": "0", "output1": {"next": "", "more": "N"}, "output2": []}
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    await client.inquire_overseas_minute_chart(
        "AAPL", exchange_code="NASD", keyb="20260219100000"
    )

    await_args = request_mock.await_args
    assert await_args is not None
    params = await_args.kwargs["params"]
    assert params["NEXT"] == "1"
    assert params["KEYB"] == "20260219100000"


@pytest.mark.asyncio
async def test_kis_inquire_overseas_minute_chart_parses_rows(monkeypatch):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output1": {"next": "", "more": "N"},
            "output2": [
                {
                    "xymd": "20260219",
                    "xhms": "093000",
                    "open": "180.1",
                    "high": "181.0",
                    "low": "179.8",
                    "last": "180.5",
                    "evol": "100",
                    "eamt": "18050",
                },
                {
                    "xymd": "20260219",
                    "xhms": "093100",
                    "open": "180.5",
                    "high": "180.7",
                    "low": "180.2",
                    "clos": "180.4",
                    "evol": "80",
                    "eamt": "14432",
                },
            ],
        }
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    page = await client.inquire_overseas_minute_chart("AAPL")

    assert list(page.frame.columns) == [
        "datetime",
        "date",
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "value",
    ]
    assert len(page.frame) == 2
    assert list(page.frame["close"]) == [180.5, 180.4]
    assert list(page.frame["volume"]) == [100, 80]
    assert list(page.frame["value"]) == [18050, 14432]
    assert page.frame.iloc[0]["datetime"] == pd.Timestamp("2026-02-19 09:30:00")
    assert page.frame.iloc[0]["date"] == date(2026, 2, 19)
    assert page.frame.iloc[0]["time"].isoformat() == "09:30:00"


@pytest.mark.asyncio
async def test_kis_inquire_overseas_minute_chart_falls_back_to_tvol_and_tamt(
    monkeypatch,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output1": {"next": "", "more": "N"},
            "output2": [
                {
                    "xymd": "20260219",
                    "xhms": "093000",
                    "open": "180.1",
                    "high": "181.0",
                    "low": "179.8",
                    "last": "180.5",
                    "tvol": "101",
                    "tamt": "18230",
                }
            ],
        }
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    page = await client.inquire_overseas_minute_chart("AAPL")

    assert list(page.frame["volume"]) == [101]
    assert list(page.frame["value"]) == [18230]


@pytest.mark.asyncio
async def test_kis_inquire_overseas_minute_chart_raises_controlled_error_on_falsy_string_payload(
    monkeypatch,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output1": {"next": "", "more": "N"},
            "output2": "",
        }
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    with pytest.raises(RuntimeError, match="expected list"):
        await client.inquire_overseas_minute_chart("AAPL")


@pytest.mark.asyncio
async def test_kis_inquire_overseas_minute_chart_raises_controlled_error_on_invalid_numeric_value(
    monkeypatch,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output1": {"next": "", "more": "N"},
            "output2": [
                {
                    "xymd": "20260219",
                    "xhms": "093000",
                    "open": "bad-open",
                    "high": "181.0",
                    "low": "179.8",
                    "last": "180.5",
                    "evol": "100",
                    "eamt": "18050",
                }
            ],
        }
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    with pytest.raises(RuntimeError, match="invalid numeric field open"):
        await client.inquire_overseas_minute_chart("AAPL")


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", ["EGW00123", "EGW00121"])
async def test_kis_inquire_overseas_minute_chart_retries_on_expired_token(
    monkeypatch,
    error_code,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    ensure_token = AsyncMock()
    monkeypatch.setattr(client, "_ensure_token", ensure_token)
    request_mock = AsyncMock(
        side_effect=[
            {"rt_cd": "1", "msg_cd": error_code, "msg1": "token expired"},
            {
                "rt_cd": "0",
                "output1": {"next": "", "more": "N"},
                "output2": [
                    {
                        "xymd": "20260219",
                        "xhms": "093000",
                        "open": "180.1",
                        "high": "181.0",
                        "low": "179.8",
                        "last": "180.5",
                        "evol": "100",
                        "eamt": "18050",
                    }
                ],
            },
        ]
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)
    client._token_manager = AsyncMock()
    client._token_manager.clear_token = AsyncMock(return_value=None)

    page = await client.inquire_overseas_minute_chart("AAPL")

    assert len(page.frame) == 1
    assert request_mock.await_count == 2
    assert ensure_token.await_count == 2
    client._token_manager.clear_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_kis_inquire_overseas_minute_chart_raises_controlled_error_on_non_list_payload(
    monkeypatch,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output1": {"next": "", "more": "N"},
            "output2": {"foo": "bar"},
        }
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    with pytest.raises(RuntimeError, match="expected list"):
        await client.inquire_overseas_minute_chart("AAPL")


@pytest.mark.asyncio
async def test_kis_inquire_overseas_minute_chart_computes_next_keyb_from_oldest_row(
    monkeypatch,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output1": {"next": "Y", "more": "Y"},
            "output2": [
                {
                    "xymd": "20260219",
                    "xhms": "100200",
                    "open": "180.6",
                    "high": "180.8",
                    "low": "180.4",
                    "last": "180.7",
                    "evol": "110",
                    "eamt": "19877",
                },
                {
                    "xymd": "20260219",
                    "xhms": "100100",
                    "open": "180.5",
                    "high": "180.6",
                    "low": "180.3",
                    "last": "180.4",
                    "evol": "90",
                    "eamt": "16236",
                },
            ],
        }
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    page = await client.inquire_overseas_minute_chart("AAPL")

    assert page.has_more is True
    assert page.next_keyb == "20260219100000"


@pytest.mark.asyncio
async def test_kis_inquire_time_dailychartprice_parses_rows(monkeypatch):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output2": [
                {
                    "stck_bsop_date": "20260219",
                    "stck_cntg_hour": "100000",
                    "stck_oprc": "70000",
                    "stck_hgpr": "70200",
                    "stck_lwpr": "69900",
                    "stck_prpr": "70100",
                    "cntg_vol": "100",
                    "acml_tr_pbmn": "7010000",
                }
            ],
        }
    )
    monkeypatch.setattr(
        client,
        "_request_with_rate_limit",
        request_mock,
    )

    df = await client.inquire_time_dailychartprice("005930", market="UN", n=1)

    assert len(df) == 1
    assert {"datetime", "open", "high", "low", "close", "volume", "value"} <= set(
        df.columns
    )
    request_mock.assert_awaited_once()
    await_args = request_mock.await_args
    assert await_args is not None
    assert await_args.args[0] == "GET"
    assert await_args.args[1].endswith("/inquire-time-dailychartprice")
    assert await_args.kwargs["tr_id"] == "FHKST03010230"
    assert await_args.kwargs["api_name"] == "inquire_time_dailychartprice"
    assert await_args.kwargs["params"]["FID_FAKE_TICK_INCU_YN"] == ""
    assert "FID_INPUT_DATE_2" not in await_args.kwargs["params"]
    assert "FID_INPUT_TIME_1" not in await_args.kwargs["params"]
    assert "FID_INPUT_TIME_2" not in await_args.kwargs["params"]


@pytest.mark.asyncio
async def test_kis_inquire_time_dailychartprice_uses_end_time_when_provided(
    monkeypatch,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(return_value={"rt_cd": "0", "output2": []})
    monkeypatch.setattr(
        client,
        "_request_with_rate_limit",
        request_mock,
    )

    await client.inquire_time_dailychartprice(
        "005930",
        market="J",
        n=1,
        end_date=pd.Timestamp(date(2026, 2, 19)),
        end_time="153000",
    )

    request_mock.assert_awaited_once()
    await_args = request_mock.await_args
    assert await_args is not None
    assert await_args.kwargs["params"]["FID_COND_MRKT_DIV_CODE"] == "J"
    assert await_args.kwargs["params"]["FID_INPUT_DATE_1"] == "20260219"
    assert await_args.kwargs["params"]["FID_INPUT_HOUR_1"] == "153000"


@pytest.mark.asyncio
async def test_kis_inquire_daily_itemchartprice_returns_empty_dataframe_on_empty_payload(
    monkeypatch,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(return_value={"rt_cd": "0", "output2": []})
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    df = await client.inquire_daily_itemchartprice("005930", market="UN", n=5)

    assert df.empty
    assert list(df.columns) == [
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "value",
    ]
    request_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_kis_inquire_daily_itemchartprice_rejects_non_positive_n(monkeypatch):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())

    with pytest.raises(ValueError, match="greater than or equal to 1"):
        await client.inquire_daily_itemchartprice("005930", market="UN", n=0)


@pytest.mark.asyncio
async def test_kis_inquire_daily_itemchartprice_clamps_oversized_n_to_200(monkeypatch):
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

    df = await client.inquire_daily_itemchartprice("005930", market="UN", n=9999)

    assert len(df) == 200
    request_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_kis_inquire_daily_itemchartprice_raises_controlled_error_on_missing_date(
    monkeypatch,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output2": [
                {
                    "stck_oprc": "70000",
                    "stck_hgpr": "70200",
                    "stck_lwpr": "69900",
                    "stck_clpr": "70100",
                    "acml_vol": "100",
                    "acml_tr_pbmn": "7010000",
                }
            ],
        }
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    with pytest.raises(RuntimeError, match="stck_bsop_date"):
        await client.inquire_daily_itemchartprice("005930", market="UN", n=1)


@pytest.mark.asyncio
async def test_kis_inquire_daily_itemchartprice_raises_controlled_error_on_non_list_payload(
    monkeypatch,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(return_value={"rt_cd": "0", "output2": {"foo": "bar"}})
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    with pytest.raises(RuntimeError, match="expected list"):
        await client.inquire_daily_itemchartprice("005930", market="UN", n=1)


def test_aggregate_to_hourly_keeps_partial_bucket():
    from app.services.brokers.kis.client import KISClient

    df = pd.DataFrame(
        {
            "datetime": pd.to_datetime(["2026-02-19 10:10:00", "2026-02-19 10:20:00"]),
            "open": [1, 2],
            "high": [3, 4],
            "low": [1, 2],
            "close": [2, 3],
            "volume": [10, 20],
            "value": [100, 200],
        }
    )

    out = KISClient._aggregate_intraday_to_hour(df)

    assert len(out) == 1
    assert out.iloc[0]["close"] == 3


class TestKISRequestWithRateLimit:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "params", "json_body", "expected_call"),
        [
            ("GET", {"foo": "bar"}, None, "get"),
            ("POST", None, {"foo": "bar"}, "post"),
        ],
    )
    @patch("app.services.brokers.kis.client.get_limiter")
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_request_with_rate_limit_passes_timeout_keyword(
        self,
        mock_client_class,
        mock_get_limiter,
        method,
        params,
        json_body,
        expected_call,
    ):
        from app.services.brokers.kis.client import KISClient

        timeout_value = 7.5
        mock_limiter = AsyncMock()
        mock_get_limiter.return_value = mock_limiter

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"rt_cd": "0", "output": []}

        mock_client = AsyncMock()
        if expected_call == "get":
            mock_client.get.return_value = mock_response
        else:
            mock_client.post.return_value = mock_response

        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None

        client = KISClient()
        result = await client._request_with_rate_limit(
            method,
            "https://example.com/uapi/domestic-stock/v1/quotations/inquire-price",
            headers={"authorization": "Bearer token"},
            params=params,
            json_body=json_body,
            timeout=timeout_value,
            api_name="test_api",
            tr_id="TEST123",
        )

        assert result == {"rt_cd": "0", "output": []}
        mock_get_limiter.assert_awaited_once()
        mock_client_class.assert_called_once_with(timeout=timeout_value)

        request_call = getattr(mock_client, expected_call)
        request_call.assert_awaited_once()
        assert request_call.await_args.kwargs["timeout"] == timeout_value

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.client.get_limiter")
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_request_with_rate_limit_returns_json_body_on_http_500(
        self,
        mock_client_class,
        mock_get_limiter,
    ):
        from app.services.brokers.kis.client import KISClient

        mock_limiter = AsyncMock()
        mock_get_limiter.return_value = mock_limiter

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.headers = {}
        mock_response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "EGW00123",
            "msg1": "token expired",
        }

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None

        client = KISClient()
        result = await client._request_with_rate_limit(
            "GET",
            "https://example.com/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            headers={"authorization": "Bearer token"},
            params={"FID_INPUT_ISCD": "005930"},
            timeout=5.0,
            api_name="inquire_orderbook",
            tr_id="FHKST01010200",
        )

        assert result["msg_cd"] == "EGW00123"
        mock_response.raise_for_status.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.client.get_limiter")
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_request_with_rate_limit_raises_http_error_on_http_500_non_json(
        self,
        mock_client_class,
        mock_get_limiter,
    ):
        from app.services.brokers.kis.client import KISClient

        mock_limiter = AsyncMock()
        mock_get_limiter.return_value = mock_limiter

        request = httpx.Request("GET", "https://example.com/failing")
        status_error = httpx.HTTPStatusError(
            "Server Error",
            request=request,
            response=httpx.Response(500, request=request),
        )

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.headers = {}
        mock_response.json.side_effect = ValueError("invalid json")
        mock_response.raise_for_status.side_effect = status_error

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None

        client = KISClient()
        with pytest.raises(httpx.HTTPStatusError):
            await client._request_with_rate_limit(
                "GET",
                "https://example.com/failing",
                headers={"authorization": "Bearer token"},
                params={"FID_INPUT_ISCD": "005930"},
                timeout=5.0,
                api_name="inquire_orderbook",
                tr_id="FHKST01010200",
            )


class TestKISInquireOrderbook:
    @pytest.mark.asyncio
    async def test_inquire_orderbook_returns_output1_payload(self):
        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        client._ensure_token = AsyncMock(return_value=None)
        client._request_with_rate_limit = AsyncMock(
            return_value={
                "rt_cd": "0",
                "output1": {"askp1": "70100", "askp_rsqn1": "111"},
            }
        )

        result = await client.inquire_orderbook("005930")
        assert result == {"askp1": "70100", "askp_rsqn1": "111"}

    @pytest.mark.asyncio
    async def test_inquire_orderbook_fallbacks_to_output_payload(self):
        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        client._ensure_token = AsyncMock(return_value=None)
        client._request_with_rate_limit = AsyncMock(
            return_value={
                "rt_cd": "0",
                "output": {"askp1": "70100", "askp_rsqn1": "111"},
            }
        )

        result = await client.inquire_orderbook("005930")
        assert result == {"askp1": "70100", "askp_rsqn1": "111"}

    @pytest.mark.asyncio
    async def test_inquire_orderbook_raises_when_output_payload_missing(self):
        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        client._ensure_token = AsyncMock(return_value=None)
        client._request_with_rate_limit = AsyncMock(
            return_value={"rt_cd": "0", "msg_cd": "0", "msg1": "ok"}
        )

        with pytest.raises(RuntimeError, match="output1"):
            await client.inquire_orderbook("005930")


class TestKISRateLimitLookup:
    @pytest.mark.parametrize(
        ("api_key", "expected_rate", "expected_period"),
        [
            (
                "FHKST03010100|/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                20,
                1.0,
            ),
            (
                "FHKST03010230|/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice",
                20,
                1.0,
            ),
            (
                "TTTC8434R|/uapi/domestic-stock/v1/trading/inquire-balance",
                10,
                1.0,
            ),
            (
                "TTTC8001R|/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                10,
                1.0,
            ),
            (
                "TTTC8036R|/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
                10,
                1.0,
            ),
        ],
    )
    def test_get_rate_limit_for_seeded_api_keys(
        self, api_key: str, expected_rate: int, expected_period: float
    ):
        from app.services.brokers.kis.client import KISClient

        client = KISClient()

        assert client._get_rate_limit_for_api(api_key) == (
            expected_rate,
            expected_period,
        )

    def test_get_rate_limit_for_unknown_api_key_warns_once_and_falls_back(self, caplog):
        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        api_key = "UNKNOWN|/uapi/test"

        with caplog.at_level(logging.WARNING):
            first = client._get_rate_limit_for_api(api_key)
            second = client._get_rate_limit_for_api(api_key)

        assert first == (19, 1.0)
        assert second == (19, 1.0)
        warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING and api_key in record.message
        ]
        assert len(warnings) == 1
