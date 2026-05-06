# tests/test_kis_market_adapters_helpers.py
import pytest

from app.jobs.kis_market_adapters import (
    extract_domestic_stock_info,
    extract_overseas_stock_info,
    match_domestic_stock,
    match_overseas_stock,
)


class TestExtractDomesticStockInfo:
    def test_extracts_all_fields(self):
        stock = {
            "pdno": "005930",
            "prdt_name": "삼성전자",
            "pchs_avg_pric": "50000",
            "prpr": "51000",
            "hldg_qty": "10",
        }
        ctx = extract_domestic_stock_info(stock)
        assert ctx.symbol == "005930"
        assert ctx.name == "삼성전자"
        assert ctx.avg_price == pytest.approx(50000.0)
        assert ctx.current_price == pytest.approx(51000.0)
        assert ctx.qty == 10
        assert ctx.is_manual is False
        assert ctx.exchange_code is None

    def test_prefers_ord_psbl_qty_over_hldg_qty(self):
        stock = {
            "pdno": "005935",
            "prdt_name": "삼성전자우",
            "pchs_avg_pric": "76300",
            "prpr": "77500",
            "hldg_qty": "8",
            "ord_psbl_qty": "5",
        }
        ctx = extract_domestic_stock_info(stock)
        assert ctx.qty == 5

    def test_manual_flag(self):
        stock = {
            "pdno": "005935",
            "prdt_name": "삼성전자우",
            "pchs_avg_pric": "73800",
            "prpr": "73800",
            "hldg_qty": "5",
            "_is_manual": True,
        }
        ctx = extract_domestic_stock_info(stock)
        assert ctx.is_manual is True


class TestExtractOverseasStockInfo:
    def test_extracts_all_fields(self):
        stock = {
            "ovrs_pdno": "AAPL",
            "ovrs_item_name": "애플",
            "pchs_avg_pric": "170.00",
            "now_pric2": "175.00",
            "ovrs_cblc_qty": "10",
            "ovrs_excg_cd": "NASD",
        }
        ctx = extract_overseas_stock_info(stock)
        assert ctx.symbol == "AAPL"
        assert ctx.name == "애플"
        assert ctx.avg_price == pytest.approx(170.0)
        assert ctx.current_price == pytest.approx(175.0)
        assert ctx.qty == 10
        assert ctx.is_manual is False
        assert ctx.exchange_code == "NASD"

    def test_prefers_ord_psbl_qty(self):
        stock = {
            "ovrs_pdno": "AAPL",
            "ovrs_item_name": "애플",
            "pchs_avg_pric": "170.00",
            "now_pric2": "175.00",
            "ovrs_cblc_qty": "10",
            "ord_psbl_qty": "7",
            "ovrs_excg_cd": "NASD",
        }
        ctx = extract_overseas_stock_info(stock)
        assert ctx.qty == 7


class TestMatchStock:
    def test_match_domestic_found(self):
        stocks = [{"pdno": "005930"}, {"pdno": "005935"}]
        assert match_domestic_stock(stocks, "005935") == {"pdno": "005935"}

    def test_match_domestic_not_found(self):
        stocks = [{"pdno": "005930"}]
        assert match_domestic_stock(stocks, "XXXX") is None

    def test_match_overseas_normalizes_symbol(self):
        stocks = [{"ovrs_pdno": "BRK/B"}, {"ovrs_pdno": "AAPL"}]
        # to_db_symbol("BRK/B") == "BRK.B", to_db_symbol("BRK.B") == "BRK.B"
        result = match_overseas_stock(stocks, "BRK.B")
        assert result is not None
        assert result["ovrs_pdno"] == "BRK/B"

    def test_match_overseas_not_found(self):
        stocks = [{"ovrs_pdno": "AAPL"}]
        assert match_overseas_stock(stocks, "TSLA") is None
