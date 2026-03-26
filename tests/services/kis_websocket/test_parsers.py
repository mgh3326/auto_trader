from unittest.mock import AsyncMock

import pytest

from app.services.kis_websocket import KISExecutionWebSocket
from tests.services.kis_websocket import (
    build_domestic_message,
    build_official_h0gscni0_message,
)


@pytest.fixture
def client():
    return KISExecutionWebSocket(on_execution=lambda x: x, mock_mode=True)


def test_parse_domestic_execution(client):
    """Pin parsing of a domestic execution message"""
    message = build_domestic_message(
        symbol="005930", filled_qty="10", filled_price="70000", ord_tmd="093001"
    )
    result = client._parse_message(message)

    assert result is not None
    assert result["symbol"] == "005930"
    assert result["filled_qty"] == 10
    assert result["filled_price"] == 70000
    assert result["market"] == "kr"


def test_parse_overseas_execution(client):
    """Pin parsing of an overseas execution message"""
    message = build_official_h0gscni0_message(
        symbol="AAPL", filled_qty="5", filled_price="150.25", ord_tmd="153045"
    )
    result = client._parse_message(message)

    assert result is not None
    assert result["symbol"] == "AAPL"
    assert result["filled_qty"] == 5.0
    assert result["filled_price"] == 150.25
    assert result["market"] == "us"


def test_parse_pingpong(client):
    """Pin parsing of pingpong message"""
    assert client._parse_message("0|pingpong") == {"system": "pingpong"}
    assert client._parse_message('{"header": {"tr_id": "PINGPONG"}}') == {
        "system": "pingpong"
    }


def test_is_execution_event_domestic(client):
    """Pin _is_execution_event for domestic data"""
    # fill_yn="2" is filled
    assert client._is_execution_event({"tr_code": "H0STCNI0", "fill_yn": "2"}) is True
    assert client._is_execution_event({"tr_code": "H0STCNI0", "fill_yn": "1"}) is False
    assert client._is_execution_event({"tr_code": "H0STCNI0", "fill_yn": ""}) is False


def test_is_execution_event_overseas(client):
    """Pin _is_execution_event for overseas data"""
    # execution_status="filled" or "partial"
    assert (
        client._is_execution_event(
            {"tr_code": "H0GSCNI0", "execution_status": "filled"}
        )
        is True
    )
    assert (
        client._is_execution_event(
            {"tr_code": "H0GSCNI0", "execution_status": "partial"}
        )
        is True
    )
    assert (
        client._is_execution_event(
            {"tr_code": "H0GSCNI0", "execution_status": "rejected"}
        )
        is False
    )


def test_extract_envelope_domestic_unencrypted(client):
    """Pin _extract_envelope for unencrypted domestic TR"""
    parts = ["0", "H0STCNI0", "1", "payload^fields"]
    envelope = client._extract_envelope(parts)
    assert envelope["tr_code"] == "H0STCNI0"
    assert envelope["execution_type"] == 1
    assert envelope["encrypted"] is False
    assert envelope["payload_source"] == "payload^fields"


def test_extract_envelope_overseas_unencrypted(client):
    """Pin _extract_envelope for unencrypted overseas TR"""
    parts = ["0", "H0GSCNI0", "1", "payload^fields"]
    envelope = client._extract_envelope(parts)
    assert envelope["tr_code"] == "H0GSCNI0"
    assert envelope["execution_type"] == 1
    assert envelope["encrypted"] is False
    assert envelope["payload_source"] == "payload^fields"


@pytest.mark.unit
class TestKISWebSocketIndexSafety:
    """Tests for message parsing index safety (from monolith)"""

    @pytest.mark.asyncio
    async def test_parse_message_with_insufficient_parts(self):
        """인덱스 안전 처리: 부족한 파트 수 테스트"""
        client = KISExecutionWebSocket(on_execution=AsyncMock(), mock_mode=True)

        # 2개 파트만 있는 메시지 (최소 3개 필요)
        message = "0|H0STCNI0"
        result = client._parse_message(message)

        assert result is None

    @pytest.mark.asyncio
    async def test_parse_message_with_non_digit_execution_type(self):
        """execution_type이 숫자가 아닌 경우 None 반환하지 않고 dict 반환"""
        client = KISExecutionWebSocket(on_execution=AsyncMock(), mock_mode=True)

        # parts[1]이 숫자가 아님 -> execution_type은 None이지만 dict는 반환됨
        message = "0|abc|005930|..."
        result = client._parse_message(message)

        # dict는 반환되지만 execution_type은 None
        assert result is not None
        assert result["execution_type"] is None


@pytest.mark.unit
class TestH0GSCNI0SyntheticContract:
    """
    Synthetic contract tests for H0GSCNI0 (overseas execution) parsing (from monolith).
    """

    @pytest.mark.asyncio
    async def test_synthetic_h0gscni0_full_fill_parsing(self) -> None:
        client = KISExecutionWebSocket(on_execution=AsyncMock(), mock_mode=True)

        # 0: CANO, 1: ACNT_PRDT_CD, 2: ODNO, 3: ORGN_ODNO, 4: SIDE, 5: RCTF_CLS, 6: ORD_TMD, 7: OVRS_PDNO
        # 8: FT_CCLD_QTY, 9: FT_CCLD_UNPR3, 10: FT_ORD_QTY, 11: CCLD_YN, 12: RFUS_YN, 13: ACPT_YN
        payload = (
            "12345678^01^ORD1^0000000000^02^0^153045^TSLA^10^248.50^0000000010^2^0^1"
        )
        message = f"0|H0GSCNI0|1|{payload}"

        result = client._parse_message(message)

        assert result is not None
        assert result["tr_code"] == "H0GSCNI0"
        assert result["market"] == "us"
        assert result["symbol"] == "TSLA"
        assert result["side"] == "bid"
        assert result["filled_qty"] == 10.0
        assert result["filled_price"] == 248.50
        assert result["filled_amount"] == 2485.0
        assert result["order_qty"] == 10.0
        assert result["currency"] == "USD"
        assert result["execution_status"] == "filled"
        assert client._is_execution_event(result) is True

    @pytest.mark.asyncio
    async def test_synthetic_h0gscni0_partial_fill_parsing(self) -> None:
        client = KISExecutionWebSocket(on_execution=AsyncMock(), mock_mode=True)

        payload = (
            "12345678^01^ORD1^0000000000^01^0^153045^AAPL^5^175.25^0000000010^2^0^1"
        )
        message = f"0|H0GSCNI0|1|{payload}"

        result = client._parse_message(message)

        assert result is not None
        assert result["symbol"] == "AAPL"
        assert result["side"] == "ask"
        assert result["filled_qty"] == 5.0
        assert result["filled_price"] == 175.25
        assert result["order_qty"] == 10.0
        assert result["currency"] == "USD"
        assert result["execution_status"] == "partial"
        assert client._is_execution_event(result) is True
