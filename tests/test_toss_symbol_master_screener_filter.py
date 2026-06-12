from app.services.invest_view_model.screener_service import _is_toss_common_stock_row


def test_toss_common_stock_row_prefers_explicit_false() -> None:
    assert (
        _is_toss_common_stock_row(
            symbol="005935",
            name="삼성전자우",
            security_type="STOCK",
            is_common_share=False,
            trading_suspended=False,
        )
        is False
    )


def test_toss_common_stock_row_excludes_etf_even_when_common_unknown() -> None:
    assert (
        _is_toss_common_stock_row(
            symbol="069500",
            name="KODEX 200",
            security_type="ETF",
            is_common_share=None,
            trading_suspended=False,
        )
        is False
    )


def test_toss_common_stock_row_falls_back_to_name_heuristic() -> None:
    assert (
        _is_toss_common_stock_row(
            symbol="005930",
            name="삼성전자",
            security_type=None,
            is_common_share=None,
            trading_suspended=None,
        )
        is True
    )
