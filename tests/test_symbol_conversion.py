"""
해외주식 심볼 변환 유틸리티 테스트

심볼 형식:
- DB (기준): `.` (예: BRK.B)
- Yahoo Finance: `-` (예: BRK-B)
- KIS API: `/` (예: BRK/B)
"""
import pytest

from app.core.symbol import to_db_symbol, to_kis_symbol, to_yahoo_symbol


class TestToKisSymbol:
    """to_kis_symbol 함수 테스트 (DB → KIS API)"""

    def test_converts_dot_to_slash(self):
        """`.`을 `/`로 변환"""
        assert to_kis_symbol("BRK.B") == "BRK/B"
        assert to_kis_symbol("BRK.A") == "BRK/A"

    def test_simple_symbol_unchanged(self):
        """구분자 없는 심볼은 그대로 유지"""
        assert to_kis_symbol("AAPL") == "AAPL"
        assert to_kis_symbol("NVDA") == "NVDA"
        assert to_kis_symbol("TSLA") == "TSLA"

    def test_multiple_dots(self):
        """다중 `.` 처리"""
        assert to_kis_symbol("A.B.C") == "A/B/C"

    def test_empty_string(self):
        """빈 문자열 처리"""
        assert to_kis_symbol("") == ""


class TestToYahooSymbol:
    """to_yahoo_symbol 함수 테스트 (DB → Yahoo Finance)"""

    def test_converts_dot_to_hyphen(self):
        """`.`을 `-`로 변환"""
        assert to_yahoo_symbol("BRK.B") == "BRK-B"
        assert to_yahoo_symbol("BRK.A") == "BRK-A"

    def test_simple_symbol_unchanged(self):
        """구분자 없는 심볼은 그대로 유지"""
        assert to_yahoo_symbol("AAPL") == "AAPL"
        assert to_yahoo_symbol("NVDA") == "NVDA"

    def test_multiple_dots(self):
        """다중 `.` 처리"""
        assert to_yahoo_symbol("A.B.C") == "A-B-C"

    def test_empty_string(self):
        """빈 문자열 처리"""
        assert to_yahoo_symbol("") == ""


class TestToDbSymbol:
    """to_db_symbol 함수 테스트 (외부 형식 → DB)"""

    def test_converts_hyphen_to_dot(self):
        """Yahoo 형식 `-`를 `.`으로 변환"""
        assert to_db_symbol("BRK-B") == "BRK.B"
        assert to_db_symbol("BRK-A") == "BRK.A"

    def test_converts_slash_to_dot(self):
        """KIS 형식 `/`를 `.`으로 변환"""
        assert to_db_symbol("BRK/B") == "BRK.B"
        assert to_db_symbol("BRK/A") == "BRK.A"

    def test_simple_symbol_unchanged(self):
        """구분자 없는 심볼은 그대로 유지"""
        assert to_db_symbol("AAPL") == "AAPL"
        assert to_db_symbol("NVDA") == "NVDA"

    def test_already_dot_format(self):
        """이미 `.` 형식인 심볼은 그대로 유지"""
        assert to_db_symbol("BRK.B") == "BRK.B"

    def test_mixed_separators(self):
        """혼합 구분자 처리 (모두 `.`로 변환)"""
        assert to_db_symbol("A-B/C") == "A.B.C"

    def test_empty_string(self):
        """빈 문자열 처리"""
        assert to_db_symbol("") == ""


class TestRoundTrip:
    """왕복 변환 테스트"""

    @pytest.mark.parametrize("symbol", [
        "AAPL",
        "BRK.B",
        "NVDA",
        "TSLA",
        "A.B.C",
    ])
    def test_kis_roundtrip(self, symbol: str):
        """DB → KIS → DB 왕복 변환"""
        kis_symbol = to_kis_symbol(symbol)
        back_to_db = to_db_symbol(kis_symbol)
        assert back_to_db == symbol

    @pytest.mark.parametrize("symbol", [
        "AAPL",
        "BRK.B",
        "NVDA",
        "TSLA",
    ])
    def test_yahoo_roundtrip(self, symbol: str):
        """DB → Yahoo → DB 왕복 변환"""
        yahoo_symbol = to_yahoo_symbol(symbol)
        back_to_db = to_db_symbol(yahoo_symbol)
        assert back_to_db == symbol


class TestRealWorldSymbols:
    """실제 사용되는 심볼 테스트"""

    @pytest.mark.parametrize("db_symbol,kis_symbol,yahoo_symbol", [
        ("BRK.B", "BRK/B", "BRK-B"),  # 버크셔 해서웨이 B
        ("BRK.A", "BRK/A", "BRK-A"),  # 버크셔 해서웨이 A
        ("AAPL", "AAPL", "AAPL"),     # 애플 (구분자 없음)
        ("NVDA", "NVDA", "NVDA"),     # 엔비디아 (구분자 없음)
        ("CONY", "CONY", "CONY"),     # CONY ETF (구분자 없음)
    ])
    def test_symbol_conversions(self, db_symbol: str, kis_symbol: str, yahoo_symbol: str):
        """실제 심볼 변환 검증"""
        assert to_kis_symbol(db_symbol) == kis_symbol
        assert to_yahoo_symbol(db_symbol) == yahoo_symbol
        assert to_db_symbol(kis_symbol) == db_symbol
        assert to_db_symbol(yahoo_symbol) == db_symbol
