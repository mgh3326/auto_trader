from __future__ import annotations

from typing import Any

import pytest

from app.services.brokers.kis.account import AccountClient


class FakeSettings:
    kis_account_no = "12345678-01"
    kis_access_token = "test-token"
    kis_app_key = "key"
    kis_app_secret = "secret"


class FakeParent:
    _settings = FakeSettings()
    _hdr_base = {"appkey": "key", "appsecret": "secret", "tr_id": "X", "custtype": "P"}


def _make_settings(**overrides: Any) -> object:
    attrs = {
        "kis_account_no": "12345678-01",
        "kis_access_token": "t",
        "kis_app_key": "k",
        "kis_app_secret": "s",
    }
    attrs.update(overrides)
    return type("S", (), attrs)()


class TestResolveAccountParts:
    def test_parses_with_dash(self):
        client = AccountClient(FakeParent())
        cano, acnt = client._resolve_account_parts()
        assert cano == "12345678"
        assert acnt == "01"

    def test_parses_without_dash(self):
        parent = FakeParent()
        parent._settings = _make_settings(kis_account_no="9876543210")
        client = AccountClient(parent)
        cano, acnt = client._resolve_account_parts()
        assert cano == "98765432"
        assert acnt == "10"

    def test_raises_when_missing(self):
        parent = FakeParent()
        parent._settings = _make_settings(kis_account_no="")
        client = AccountClient(parent)
        with pytest.raises(ValueError, match="환경변수"):
            client._resolve_account_parts()

    def test_raises_when_too_short(self):
        parent = FakeParent()
        parent._settings = _make_settings(kis_account_no="12345")
        client = AccountClient(parent)
        with pytest.raises(ValueError, match="형식"):
            client._resolve_account_parts()


class TestBuildBalanceRequestConfig:
    def test_domestic(self):
        client = AccountClient(FakeParent())
        config = client._build_balance_request_config(is_overseas=False, is_mock=False)
        assert config["ctx_key_fk"] == "CTX_AREA_FK100"
        assert config["ctx_key_nk"] == "CTX_AREA_NK100"

    def test_overseas(self):
        client = AccountClient(FakeParent())
        config = client._build_balance_request_config(is_overseas=True, is_mock=False)
        assert config["ctx_key_fk"] == "CTX_AREA_FK200"
        assert config["ctx_key_nk"] == "CTX_AREA_NK200"

    def test_mock_tr_id_differs(self):
        client = AccountClient(FakeParent())
        real = client._build_balance_request_config(is_overseas=False, is_mock=False)
        mock = client._build_balance_request_config(is_overseas=False, is_mock=True)
        assert real["tr_id"] != mock["tr_id"]


class TestFilterNonzeroHoldings:
    def test_filters_domestic_zero_qty(self):
        client = AccountClient(FakeParent())
        stocks = [
            {"hldg_qty": "10", "prdt_name": "삼성전자"},
            {"hldg_qty": "0", "prdt_name": "LG전자"},
        ]
        result = client._filter_nonzero_holdings(stocks, is_overseas=False)
        assert len(result) == 1
        assert result[0]["prdt_name"] == "삼성전자"

    def test_filters_overseas_zero_qty(self):
        client = AccountClient(FakeParent())
        stocks = [
            {"ovrs_cblc_qty": "5", "ovrs_item_name": "AAPL"},
            {"ovrs_cblc_qty": "0", "ovrs_item_name": "TSLA"},
        ]
        result = client._filter_nonzero_holdings(stocks, is_overseas=True)
        assert len(result) == 1
        assert result[0]["ovrs_item_name"] == "AAPL"


class TestParseMarginResponse:
    def test_extracts_key_fields(self):
        client = AccountClient(FakeParent())
        output = {
            "dnca_tot_amt": "5000000",
            "stck_cash_objt_amt": "4000000",
            "stck_cash100_max_ord_psbl_amt": "3500000",
            "stck_itgr_cash100_ord_psbl_amt": "3400000",
            "stck_cash_ord_psbl_amt": "3300000",
            "usd_ord_psbl_amt": "1500.50",
            "frcr_dncl_amt_2": "2000.00",
        }
        result = client._parse_margin_response(output)
        assert result["dnca_tot_amt"] == 5_000_000.0
        assert result["usd_ord_psbl_amt"] == 1500.50
        assert result["usd_balance"] == 2000.00
        assert result["raw"] is output

    def test_handles_empty_strings(self):
        client = AccountClient(FakeParent())
        output = {"dnca_tot_amt": "", "stck_cash_objt_amt": ""}
        result = client._parse_margin_response(output)
        assert result["dnca_tot_amt"] == 0.0

    def test_fallback_fields(self):
        client = AccountClient(FakeParent())
        output = {
            "stck_cash_objt_amt": "4000000",
            "ord_psbl_cash": "3000000",
            "frcr_ord_psbl_amt": "1200.00",
            "FRCR_DNCL_AMT_2": "900.00",
        }
        result = client._parse_margin_response(output)
        assert result["dnca_tot_amt"] == 4_000_000.0
        assert result["stck_cash_ord_psbl_amt"] == 3_000_000.0
        assert result["usd_ord_psbl_amt"] == 1200.00
        assert result["usd_balance"] == 900.00
