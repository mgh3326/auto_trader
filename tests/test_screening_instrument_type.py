import pytest

from app.mcp_server.tooling.screening.instrument_type import (
    classify_kr_instrument,
    classify_us_instrument,
)


@pytest.mark.parametrize(
    ("symbol", "name", "subtype", "expected"),
    [
        ("005930", "삼성전자", None, "common"),
        ("000660", "SK하이닉스", None, "common"),
        ("950170", "JTC", None, "common"),
        ("005935", "삼성전자우", None, "preferred"),
        ("005387", "현대차2우B", None, "preferred"),
        ("005389", "현대차3우B", None, "preferred"),
        ("003545", "대신증권우", None, "preferred"),
        ("001527", "동양2우B", None, "preferred"),
        ("001529", "동양3우B", None, "preferred"),
        ("000995", "DB하이텍1우", None, "preferred"),
        ("000547", "흥국화재2우B", None, "preferred"),
        ("000545", "흥국화재우", None, "preferred"),
        ("33626K", "두산퓨얼셀1우", None, "preferred"),
        ("33626L", "두산퓨얼셀2우B", None, "preferred"),
        ("123450", "이지스밸류리츠", None, "reit"),
        ("357120", "코람코라이프인프라리츠", None, "reit"),
        ("330590", "롯데리츠", None, "reit"),
        ("451800", "한화리츠", None, "reit"),
        ("950210", "프레스티지바이오파마", None, "common"),
        ("451060", "KODEX CD금리액티브", "ETF", "etf"),
        ("069500", "KODEX 200", "exchange traded fund", "etf"),
        ("114800", "KODEX 반도체", "ETF", "etf"),
        ("458730", "TIGER 미국배당다우존스", "ETF", "etf"),
        ("457480", "ACE 테슬라밸류체인액티브", "ETF", "etf"),
        ("430220", "IBKS제17호스팩", None, "spac"),
        ("469480", "하나32호스팩", None, "spac"),
        ("477380", "미래에셋비전스팩7호", None, "spac"),
        ("477760", "KB제30호스팩", None, "spac"),
        ("475240", "유안타제16호스팩", None, "spac"),
        ("999999", "", None, "unknown"),
        ("", "", None, "unknown"),
    ],
)
def test_classify_kr_instrument_cases(symbol, name, subtype, expected):
    assert classify_kr_instrument(symbol, name, subtype) == expected


@pytest.mark.parametrize(
    ("symbol", "name", "type_", "subtype", "expected"),
    [
        ("AAPL", "Apple Inc.", "stock", "common stock", "common"),
        ("BRK.B", "Berkshire Hathaway Inc.", "stock", "common stock", "common"),
        ("SPY", "SPDR S&P 500 ETF Trust", "fund", "ETF", "etf"),
        ("QQQ", "Invesco QQQ Trust", "fund", "exchange traded fund", "etf"),
        ("O", "Realty Income Corporation", "stock", "reit", "reit"),
        ("PLD", "Prologis, Inc.", "stock", "equity reit", "reit"),
        ("DWAC", "Digital World Acquisition Corp.", "stock", "spac", "spac"),
        ("XYZ.U", "Example Acquisition Corp Unit", "stock", "unit", "spac"),
        ("BAC.PR.K", "Bank of America Preferred Series K", "stock", "", "preferred"),
        ("WFC-PD", "Wells Fargo Preferred", "stock", "", "preferred"),
        ("UNKNOWN", "", None, None, "unknown"),
    ],
)
def test_classify_us_instrument_cases(symbol, name, type_, subtype, expected):
    assert classify_us_instrument(symbol, name, type_, subtype) == expected
