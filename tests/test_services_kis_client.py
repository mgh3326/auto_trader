from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestKISService:
    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_kis_client_initialization(self, mock_client_class):
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        from app.services.brokers.kis.client import KISClient

        client = KISClient()

        assert client is not None
        assert hasattr(client, "_hdr_base")

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_kis_volume_rank(self, mock_client_class):
        mock_client = AsyncMock()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output": [
                {
                    "hts_kor_isnm": "삼성전자",
                    "stck_cntg_hour": "15:30:00",
                    "stck_prpr": "50000",
                }
            ],
        }

        mock_client.get.return_value = mock_response

        mock_client_class.return_value = mock_client
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None

        from app.services.brokers.kis.client import KISClient

        client = KISClient()

        with patch.object(client, "_ensure_token"):
            result = await client.volume_rank()

            assert isinstance(result, list)
            assert len(result) > 0

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_fetch_my_stocks_inqr_dvsn_domestic(self, mock_client_class):
        mock_client = AsyncMock()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output1": [],
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

        mock_client.get.return_value = mock_response

        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None

        from app.services.brokers.kis.client import KISClient

        client = KISClient()

        with patch.object(client, "_ensure_token"):
            await client.fetch_my_stocks(is_mock=False, is_overseas=False)

            call_args = mock_client.get.call_args
            assert "params" in call_args.kwargs
            params = call_args.kwargs["params"]

            assert params["INQR_DVSN"] == "00"
            assert params["AFHR_FLPR_YN"] == "N"
            assert params["UNPR_DVSN"] == "01"
            assert params["PRCS_DVSN"] == "01"

            call_kwargs = mock_client.get.call_args.kwargs
            assert "headers" in call_kwargs
            headers = call_kwargs["headers"]
            assert headers["tr_id"] == "TTTC8434R"

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_inquire_domestic_cash_balance_success(
        self, mock_client_class, monkeypatch
    ):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output2": [
                {
                    "dnca_tot_amt": "1140000",
                    "stck_cash_ord_psbl_amt": "1110000",
                }
            ],
        }
        mock_client.get.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None
        monkeypatch.setattr(
            "app.services.brokers.kis.client.settings.kis_account_no",
            "12345678-01",
            raising=False,
        )

        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        with patch.object(client, "_ensure_token"):
            result = await client.inquire_domestic_cash_balance(is_mock=False)

        assert result["dnca_tot_amt"] == 1140000.0
        assert result["stck_cash_ord_psbl_amt"] == 1110000.0
        assert result["raw"]["dnca_tot_amt"] == "1140000"

        call_args = mock_client.get.call_args
        params = call_args.kwargs["params"]
        headers = call_args.kwargs["headers"]
        assert params["INQR_DVSN"] == "00"
        assert headers["tr_id"] == "TTTC8434R"

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_inquire_domestic_cash_balance_fallback_ord_psbl_cash(
        self, mock_client_class, monkeypatch
    ):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output2": [
                {
                    "dnca_tot_amt": "1140000",
                    "ord_psbl_cash": "950000",
                }
            ],
        }
        mock_client.get.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None
        monkeypatch.setattr(
            "app.services.brokers.kis.client.settings.kis_account_no",
            "12345678-01",
            raising=False,
        )

        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        with patch.object(client, "_ensure_token"):
            result = await client.inquire_domestic_cash_balance(is_mock=False)

        assert result["dnca_tot_amt"] == 1140000.0
        assert result["stck_cash_ord_psbl_amt"] == 950000.0

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_inquire_domestic_cash_balance_api_error_raises(
        self, mock_client_class, monkeypatch
    ):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "EGW99999",
            "msg1": "failure",
        }
        mock_client.get.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None
        monkeypatch.setattr(
            "app.services.brokers.kis.client.settings.kis_account_no",
            "12345678-01",
            raising=False,
        )

        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        with patch.object(client, "_ensure_token"):
            with pytest.raises(RuntimeError, match="EGW99999"):
                await client.inquire_domestic_cash_balance(is_mock=False)

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_inquire_overseas_margin_parses_extended_orderable_fields(
        self, mock_client_class, monkeypatch
    ):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output": [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": "5856.200000",
                    "frcr_ord_psbl_amt1": "0.000000",
                    "frcr_gnrl_ord_psbl_amt": "5824.17",
                    "itgr_ord_psbl_amt": "5824.27",
                    "frcr_buy_amt_smtl": "0.00",
                    "tot_evlu_pfls_amt": "0.00",
                    "ovrs_tot_pfls": "0.00",
                }
            ],
        }
        mock_client.get.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None
        monkeypatch.setattr(
            "app.services.brokers.kis.client.settings.kis_account_no",
            "12345678-01",
            raising=False,
        )

        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        with patch.object(client, "_ensure_token"):
            result = await client.inquire_overseas_margin(is_mock=False)

        assert len(result) == 1
        assert result[0]["natn_name"] == "미국"
        assert result[0]["crcy_cd"] == "USD"
        assert result[0]["frcr_dncl_amt1"] == 5856.2
        assert result[0]["frcr_ord_psbl_amt1"] == 0.0
        assert result[0]["frcr_gnrl_ord_psbl_amt"] == 5824.17
        assert result[0]["itgr_ord_psbl_amt"] == 5824.27

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_inquire_overseas_margin_safe_float_handles_blank_values(
        self, mock_client_class, monkeypatch
    ):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output": [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": "",
                    "frcr_ord_psbl_amt1": None,
                    "frcr_gnrl_ord_psbl_amt": "",
                    "itgr_ord_psbl_amt": None,
                    "frcr_buy_amt_smtl": "",
                    "tot_evlu_pfls_amt": None,
                    "ovrs_tot_pfls": "",
                }
            ],
        }
        mock_client.get.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None
        monkeypatch.setattr(
            "app.services.brokers.kis.client.settings.kis_account_no",
            "12345678-01",
            raising=False,
        )

        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        with patch.object(client, "_ensure_token"):
            result = await client.inquire_overseas_margin(is_mock=False)

        assert len(result) == 1
        assert result[0]["frcr_dncl_amt1"] == 0.0
        assert result[0]["frcr_ord_psbl_amt1"] == 0.0
        assert result[0]["frcr_gnrl_ord_psbl_amt"] == 0.0
        assert result[0]["itgr_ord_psbl_amt"] == 0.0


class TestKISIntegratedMarginParams:
    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_integrated_margin_params_includes_cma_field(
        self, mock_settings, mock_client_class
    ):
        from app.services.brokers.kis.client import (
            INTEGRATED_MARGIN_TR,
            INTEGRATED_MARGIN_URL,
            KISClient,
        )

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output1": {
                "dnca_tot_amt": "1000000",
                "stck_cash_objt_amt": "950000",
                "stck_itgr_cash100_ord_psbl_amt": "900000",
            },
        }
        mock_client.get.return_value = mock_response

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        await client.inquire_integrated_margin()

        call_args = mock_client.get.call_args
        params = call_args.kwargs["params"]
        headers = call_args.kwargs["headers"]

        assert "CMA_EVLU_AMT_ICLD_YN" in params
        assert params["CMA_EVLU_AMT_ICLD_YN"] == "N"
        assert params["WCRC_FRCR_DVSN_CD"] == "01"
        assert params["FWEX_CTRT_FRCR_DVSN_CD"] == "01"
        assert "CANO" in params
        assert "ACNT_PRDT_CD" in params
        assert headers["tr_id"] == INTEGRATED_MARGIN_TR
        assert INTEGRATED_MARGIN_URL in call_args.args[0]

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_integrated_margin_parses_stck_cash100_max_ord_psbl_amt(
        self, mock_settings, mock_client_class
    ):
        from app.services.brokers.kis.client import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output1": {
                "dnca_tot_amt": "5000000",
                "stck_cash_objt_amt": "5000000",
                "stck_itgr_cash100_ord_psbl_amt": "0",
                "stck_cash100_max_ord_psbl_amt": "3534890.5473",
            },
        }
        mock_client.get.return_value = mock_response

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        result = await client.inquire_integrated_margin()

        assert result["stck_cash100_max_ord_psbl_amt"] == 3534890.5473

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_integrated_margin_opsq2001_retry_with_y(
        self, mock_settings, mock_client_class
    ):
        from app.services.brokers.kis.client import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        first_response = MagicMock()
        first_response.status_code = 200
        first_response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "OPSQ2001",
            "msg1": "필수항목 누락: CMA_EVLU_AMT_ICLD_YN",
        }

        second_response = MagicMock()
        second_response.status_code = 200
        second_response.json.return_value = {
            "rt_cd": "0",
            "output1": {
                "dnca_tot_amt": "1000000",
                "stck_cash_objt_amt": "850000",
                "stck_itgr_cash100_ord_psbl_amt": "800000",
            },
        }

        mock_client.get.side_effect = [first_response, second_response]

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        result = await client.inquire_integrated_margin()

        assert mock_client.get.call_count == 2

        first_call_params = mock_client.get.call_args_list[0].kwargs["params"]
        assert first_call_params["CMA_EVLU_AMT_ICLD_YN"] == "N"

        second_call_params = mock_client.get.call_args_list[1].kwargs["params"]
        assert second_call_params["CMA_EVLU_AMT_ICLD_YN"] == "Y"

        assert result["dnca_tot_amt"] == 1000000.0
        assert result["stck_cash_objt_amt"] == 850000.0
        assert result["stck_itgr_cash100_ord_psbl_amt"] == 800000.0

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_integrated_margin_missing_domestic_fields_defaults_zero(
        self, mock_settings, mock_client_class
    ):
        from app.services.brokers.kis.client import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output1": {
                "dnca_tot_amt": "1000000",
                "stck_cash_objt_amt": "",
                "stck_itgr_cash100_ord_psbl_amt": None,
            },
        }
        mock_client.get.return_value = mock_response

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        result = await client.inquire_integrated_margin()

        assert result["stck_cash_objt_amt"] == 0.0
        assert result["stck_itgr_cash100_ord_psbl_amt"] == 0.0

    def test_extract_domestic_cash_summary_from_integrated_margin(self):
        from app.services.brokers.kis.client import (
            extract_domestic_cash_summary_from_integrated_margin,
        )

        summary = extract_domestic_cash_summary_from_integrated_margin(
            {
                "stck_cash_objt_amt": "777000",
                "stck_itgr_cash100_ord_psbl_amt": "555000",
                "raw": {
                    "stck_cash_objt_amt": "777000",
                    "stck_itgr_cash100_ord_psbl_amt": "555000",
                },
            }
        )

        assert summary["balance"] == 777000.0
        assert summary["orderable"] == 555000.0
        assert summary["raw"]["stck_cash_objt_amt"] == "777000"

    def test_extract_domestic_cash_summary_prefers_stck_cash100_max_orderable(self):
        from app.services.brokers.kis.client import (
            extract_domestic_cash_summary_from_integrated_margin,
        )

        summary = extract_domestic_cash_summary_from_integrated_margin(
            {
                "stck_cash_objt_amt": "5000000",
                "stck_itgr_cash100_ord_psbl_amt": "0",
                "stck_cash100_max_ord_psbl_amt": "3534890.5473",
                "raw": {
                    "stck_cash_objt_amt": "5000000",
                    "stck_itgr_cash100_ord_psbl_amt": "0",
                    "stck_cash100_max_ord_psbl_amt": "3534890.5473",
                },
            }
        )

        assert summary["balance"] == 5000000.0
        assert summary["orderable"] == 3534890.5473

    def test_extract_domestic_cash_summary_falls_back_to_stck_cash_ord_psbl_amt(self):
        from app.services.brokers.kis.client import (
            extract_domestic_cash_summary_from_integrated_margin,
        )

        summary = extract_domestic_cash_summary_from_integrated_margin(
            {
                "stck_cash_objt_amt": "5000000",
                "stck_cash_ord_psbl_amt": "2100000.25",
                "raw": {
                    "stck_cash_objt_amt": "5000000",
                    "stck_cash_ord_psbl_amt": "2100000.25",
                },
            }
        )

        assert summary["orderable"] == 2100000.25

    def test_extract_domestic_cash_summary_skips_zero_priority_orderables_for_lower_positive(
        self,
    ):
        from app.services.brokers.kis.client import (
            extract_domestic_cash_summary_from_integrated_margin,
        )

        summary = extract_domestic_cash_summary_from_integrated_margin(
            {
                "stck_cash_objt_amt": "5000000",
                "stck_cash100_max_ord_psbl_amt": "0",
                "stck_itgr_cash100_ord_psbl_amt": "0",
                "stck_cash_ord_psbl_amt": "2100000.25",
                "raw": {
                    "stck_cash_objt_amt": "5000000",
                    "stck_cash100_max_ord_psbl_amt": "0",
                    "stck_itgr_cash100_ord_psbl_amt": "0",
                    "stck_cash_ord_psbl_amt": "2100000.25",
                },
            }
        )

        assert summary["balance"] == 5000000.0
        assert summary["orderable"] == 2100000.25

    def test_extract_domestic_cash_summary_returns_zero_when_all_orderables_zero(self):
        from app.services.brokers.kis.client import (
            extract_domestic_cash_summary_from_integrated_margin,
        )

        summary = extract_domestic_cash_summary_from_integrated_margin(
            {
                "stck_cash_objt_amt": "0",
                "stck_cash100_max_ord_psbl_amt": "0",
                "stck_itgr_cash100_ord_psbl_amt": "0",
                "stck_cash_ord_psbl_amt": "0",
                "raw": {
                    "stck_cash_objt_amt": "0",
                    "stck_cash100_max_ord_psbl_amt": "0",
                    "stck_itgr_cash100_ord_psbl_amt": "0",
                    "stck_cash_ord_psbl_amt": "0",
                },
            }
        )

        assert summary["balance"] == 0.0
        assert summary["orderable"] == 0.0

    def test_extract_domestic_cash_summary_falls_back_to_stck_cash_objt_amt(self):
        from app.services.brokers.kis.client import (
            extract_domestic_cash_summary_from_integrated_margin,
        )

        summary = extract_domestic_cash_summary_from_integrated_margin(
            {
                "stck_cash_objt_amt": "5000000",
                "raw": {
                    "stck_cash_objt_amt": "5000000",
                },
            }
        )

        assert summary["orderable"] == 5000000.0
