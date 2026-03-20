from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestKISFailureLogging:
    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_domestic_cash_balance_logs_failure_details(
        self, mock_settings, mock_client_class, caplog
    ):
        import logging

        from app.services.brokers.kis.client import BALANCE_TR, BALANCE_URL, KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "TEST_ERROR",
            "msg1": "Test error message",
        }
        mock_client.get.return_value = mock_response

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError):
                await client.inquire_domestic_cash_balance()

        error_logs = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(error_logs) >= 1

        error_log = error_logs[0].message
        assert "inquire_domestic_cash_balance" in error_log
        assert BALANCE_URL in error_log
        assert BALANCE_TR in error_log
        assert "CANO" in error_log
        assert "ACNT_PRDT_CD" in error_log

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_integrated_margin_logs_failure_details(
        self, mock_settings, mock_client_class, caplog
    ):
        import logging

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
            "rt_cd": "1",
            "msg_cd": "OTHER_ERROR",
            "msg1": "Some other error",
        }
        mock_client.get.return_value = mock_response

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError):
                await client.inquire_integrated_margin()

        error_logs = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(error_logs) >= 1

        error_log = error_logs[0].message
        assert "inquire_integrated_margin" in error_log
        assert INTEGRATED_MARGIN_URL in error_log
        assert INTEGRATED_MARGIN_TR in error_log
        assert "CANO" in error_log
        assert "ACNT_PRDT_CD" in error_log
        assert "CMA_EVLU_AMT_ICLD_YN" in error_log
        assert "OTHER_ERROR" in error_log

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_integrated_margin_msg1_none_no_typeerror(
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
            "rt_cd": "1",
            "msg_cd": "OPSQ2001",
            "msg1": None,
        }
        mock_client.get.return_value = mock_response

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        with pytest.raises(RuntimeError) as exc_info:
            await client.inquire_integrated_margin()

        assert "OPSQ2001" in str(exc_info.value)

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_integrated_margin_opsq2001_cma_warning_logged(
        self, mock_settings, mock_client_class, caplog
    ):
        import logging

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
            "msg1": "CMA_EVLU_AMT_ICLD_YN 파라미터 오류입니다.",
        }

        second_response = MagicMock()
        second_response.status_code = 200
        second_response.json.return_value = {
            "rt_cd": "0",
            "output1": {"dnca_tot_amt": "500000"},
        }

        mock_client.get.side_effect = [first_response, second_response]

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        with caplog.at_level(logging.WARNING):
            result = await client.inquire_integrated_margin()

        assert any(
            "OPSQ2001" in record.message and "CMA_EVLU_AMT_ICLD_YN" in record.message
            for record in caplog.records
            if record.levelno == logging.WARNING
        )
        assert result["dnca_tot_amt"] == 500000.0

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_order_korea_stock_logs_failure_details(
        self, mock_settings, mock_client_class, caplog
    ):
        import logging

        from app.services.brokers.kis.client import KOREA_ORDER_URL, KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "ORDER_ERROR",
            "msg1": "Order failed",
        }
        mock_client.post.return_value = mock_response

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError):
                await client.order_korea_stock(
                    stock_code="005930",
                    order_type="buy",
                    quantity=10,
                    price=80000,
                )

        error_logs = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(error_logs) >= 1

        error_log = error_logs[0].message
        assert "order_korea_stock" in error_log
        assert KOREA_ORDER_URL in error_log
        assert "CANO" in error_log
        assert "ACNT_PRDT_CD" in error_log
        assert "PDNO" in error_log
        assert "ORD_QTY" in error_log
        assert "ORD_UNPR" in error_log
        assert "EXCG_ID_DVSN_CD" in error_log

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("order_type", "is_mock", "expected_tr_id"),
        [
            ("buy", False, "TTTC0012U"),
            ("buy", True, "VTTC0012U"),
            ("sell", False, "TTTC0011U"),
            ("sell", True, "VTTC0011U"),
        ],
    )
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_order_korea_stock_uses_new_tr_and_sor(
        self,
        mock_settings,
        mock_client_class,
        order_type,
        is_mock,
        expected_tr_id,
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
        mock_response.headers = {}
        mock_response.json.return_value = {
            "rt_cd": "0",
            "msg1": "ok",
            "output": {"ODNO": "1", "ORD_TMD": "100000"},
        }
        mock_client.post.return_value = mock_response

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        result = await client.order_korea_stock(
            stock_code="005930",
            order_type=order_type,
            quantity=3,
            price=81000,
            is_mock=is_mock,
        )

        assert result["odno"] == "1"

        headers = mock_client.post.call_args.kwargs["headers"]
        body = mock_client.post.call_args.kwargs["json"]

        assert headers["tr_id"] == expected_tr_id
        assert body["EXCG_ID_DVSN_CD"] == "SOR"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("is_mock", "expected_tr_id"),
        [
            (False, "TTTC0013U"),
            (True, "VTTC0013U"),
        ],
    )
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_cancel_korea_order_uses_new_tr_sor_and_explicit_orgno(
        self,
        mock_settings,
        mock_client_class,
        is_mock,
        expected_tr_id,
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
        mock_response.headers = {}
        mock_response.json.return_value = {
            "rt_cd": "0",
            "msg1": "ok",
            "output": {"ODNO": "2", "ORD_TMD": "100100"},
        }
        mock_client.post.return_value = mock_response

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        result = await client.cancel_korea_order(
            order_number="10001",
            stock_code="005930",
            quantity=3,
            price=81000,
            order_type="buy",
            krx_fwdg_ord_orgno="06010",
            is_mock=is_mock,
        )

        assert result["odno"] == "2"

        headers = mock_client.post.call_args.kwargs["headers"]
        body = mock_client.post.call_args.kwargs["json"]

        assert headers["tr_id"] == expected_tr_id
        assert body["EXCG_ID_DVSN_CD"] == "SOR"
        assert body["KRX_FWDG_ORD_ORGNO"] == "06010"
        assert body["RVSE_CNCL_DVSN_CD"] == "02"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("is_mock", "expected_tr_id"),
        [
            (False, "TTTC0013U"),
            (True, "VTTC0013U"),
        ],
    )
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_modify_korea_order_uses_new_tr_sor_and_resolved_orgno(
        self,
        mock_settings,
        mock_client_class,
        is_mock,
        expected_tr_id,
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
        mock_response.headers = {}
        mock_response.json.return_value = {
            "rt_cd": "0",
            "msg1": "ok",
            "output": {"ODNO": "3", "ORD_TMD": "100200"},
        }
        mock_client.post.return_value = mock_response

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")
        client.inquire_korea_orders = AsyncMock(
            return_value=[
                {
                    "ord_no": "10002",
                    "pdno": "005930",
                    "ord_gno_brno": "06010",
                }
            ]
        )

        result = await client.modify_korea_order(
            order_number="10002",
            stock_code="005930",
            quantity=4,
            new_price=81500,
            is_mock=is_mock,
        )

        assert result["odno"] == "3"

        headers = mock_client.post.call_args.kwargs["headers"]
        body = mock_client.post.call_args.kwargs["json"]

        assert headers["tr_id"] == expected_tr_id
        assert body["EXCG_ID_DVSN_CD"] == "SOR"
        assert body["KRX_FWDG_ORD_ORGNO"] == "06010"
        assert body["RVSE_CNCL_DVSN_CD"] == "01"

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_cancel_korea_order_raises_when_orgno_resolution_fails(
        self,
        mock_settings,
        mock_client_class,
    ):
        from app.services.brokers.kis.client import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")
        client.inquire_korea_orders = AsyncMock(
            return_value=[
                {
                    "ord_no": "other-order",
                    "pdno": "005930",
                    "ord_gno_brno": "06010",
                }
            ]
        )

        with pytest.raises(
            ValueError,
            match="KRX_FWDG_ORD_ORGNO not found for order 10001",
        ):
            await client.cancel_korea_order(
                order_number="10001",
                stock_code="005930",
                quantity=3,
                price=81000,
                order_type="buy",
                is_mock=False,
            )

        mock_client.post.assert_not_called()
