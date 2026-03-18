import pytest

@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_kr_names_by_symbols_returns_name_map(mocker):
    """symbol 목록으로 name 매핑을 반환한다."""
    from app.services.kr_symbol_universe_service import get_kr_names_by_symbols

    class MockRow:
        def __init__(self, symbol, name):
            self.symbol = symbol
            self.name = name

    mock_row_1 = MockRow("064350", "현대로템")
    mock_row_2 = MockRow("035420", "NAVER")
    mock_result = mocker.MagicMock()
    mock_result.all.return_value = [mock_row_1, mock_row_2]
    mock_session = mocker.AsyncMock()
    mock_session.execute.return_value = mock_result

    result = await get_kr_names_by_symbols(
        ["064350", "035420", "999999"],
        db=mock_session,
    )

    assert result == {"064350": "현대로템", "035420": "NAVER"}
    assert "999999" not in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_kr_names_by_symbols_empty_input(mocker):
    """빈 목록이면 DB 호출 없이 빈 dict 반환."""
    from app.services.kr_symbol_universe_service import get_kr_names_by_symbols

    result = await get_kr_names_by_symbols([], db=mocker.AsyncMock())
    assert result == {}
