from datetime import UTC, datetime, tzinfo
from unittest.mock import AsyncMock

import pytest

from app.core.timezone import KST
from app.services.kis_websocket import KISExecutionWebSocket
from app.services.kis_websocket_internal import parsers as parsers_module
from app.services.kis_websocket_internal.events import build_lifecycle_event
from tests.services.kis_websocket import (
    build_domestic_message,
    build_official_h0gscni0_message,
)


@pytest.fixture
def client():
    return KISExecutionWebSocket(on_execution=lambda x: x, mock_mode=True)


class _FrozenDateTime(datetime):
    """datetime subclass with a fixed `now()` for deterministic KST boundary tests."""

    _now: datetime = datetime(2026, 7, 16, 15, 20, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz: tzinfo | None = None):
        current = cls._now
        if tz is None:
            return current.replace(tzinfo=None)
        return current.astimezone(tz)


@pytest.mark.unit
class TestExtractTimestampKST:
    """ROB-934: HHMMSS-only tokens are KST wall-clock time and must be
    combined with the KST calendar date, not the UTC date."""

    def _freeze(self, monkeypatch, *, utc_now: datetime) -> None:
        frozen = type("_Frozen", (_FrozenDateTime,), {"_now": utc_now})
        monkeypatch.setattr(parsers_module, "datetime", frozen)

    def test_midnight_adjacent_hhmmss_uses_kst_calendar_date(self, client, monkeypatch):
        # Wall clock: KST 2026-07-17 00:20:00 (= UTC 2026-07-16 15:20:00).
        # A fill reported as "001728" (00:17:28 KST) must land on 2026-07-17,
        # not 2026-07-16 (the UTC calendar date at this instant).
        self._freeze(monkeypatch, utc_now=datetime(2026, 7, 16, 15, 20, 0, tzinfo=UTC))

        result = client._extract_timestamp("001728")

        parsed = datetime.fromisoformat(result)
        assert parsed.tzinfo is not None
        assert parsed.astimezone(KST).strftime("%Y-%m-%d") == "2026-07-17"
        assert parsed.astimezone(KST).strftime("%H:%M:%S") == "00:17:28"

    def test_kst_midnight_hhmmss_uses_kst_calendar_date(self, client, monkeypatch):
        # Wall clock: KST 2026-07-17 00:00:30 (= UTC 2026-07-16 15:00:30).
        self._freeze(monkeypatch, utc_now=datetime(2026, 7, 16, 15, 0, 30, tzinfo=UTC))

        result = client._extract_timestamp("000000")

        parsed = datetime.fromisoformat(result)
        assert parsed.astimezone(KST).strftime("%Y-%m-%d") == "2026-07-17"

    def test_kst_end_of_day_hhmmss_stays_on_same_kst_date(self, client, monkeypatch):
        # Wall clock: KST 2026-07-16 23:59:30 (= UTC 2026-07-16 14:59:30).
        self._freeze(monkeypatch, utc_now=datetime(2026, 7, 16, 14, 59, 30, tzinfo=UTC))

        result = client._extract_timestamp("235959")

        parsed = datetime.fromisoformat(result)
        assert parsed.astimezone(KST).strftime("%Y-%m-%d") == "2026-07-16"
        assert parsed.astimezone(KST).strftime("%H:%M:%S") == "23:59:59"

    def test_daytime_hhmmss_regression(self, client, monkeypatch):
        # Wall clock: KST 2026-07-17 09:00:30 (= UTC 2026-07-17 00:00:30) —
        # no UTC/KST date discrepancy at this instant; must still resolve
        # correctly (regression guard for the ordinary trading-hours path).
        self._freeze(monkeypatch, utc_now=datetime(2026, 7, 17, 0, 0, 30, tzinfo=UTC))

        result = client._extract_timestamp("090000")

        parsed = datetime.fromisoformat(result)
        assert parsed.astimezone(KST).strftime("%Y-%m-%d") == "2026-07-17"
        assert parsed.astimezone(KST).strftime("%H:%M:%S") == "09:00:00"

    def test_full_14_digit_timestamp_unaffected_by_now(self, client, monkeypatch):
        # Full YYYYMMDDHHMMSS tokens must not regress: they carry their own
        # date and are unaffected by "now" framing. ROB-957: KIS 14-digit
        # tokens are KST wall-clock (confirmed prod evidence, ROB-958) and
        # must be tz-aware, not naive.
        self._freeze(monkeypatch, utc_now=datetime(2026, 7, 16, 15, 20, 0, tzinfo=UTC))

        result = client._extract_timestamp("20260716153045")

        parsed = datetime.fromisoformat(result)
        assert parsed.tzinfo is not None
        assert parsed.astimezone(KST).strftime("%Y-%m-%d") == "2026-07-16"
        assert parsed.astimezone(KST).strftime("%H:%M:%S") == "15:30:45"

    def test_full_14_digit_timestamp_evening_window_not_misattributed_to_next_utc_day(
        self, client, monkeypatch
    ):
        # ROB-957: a 14-digit token reported inside KST 15:00-23:59 is the
        # window where naive-then-assume-UTC downstream handling
        # (events.py _resolve_occurred_at) would shift the fill forward by
        # 9h, rolling it onto the wrong KST calendar date. With the fix,
        # the KST-aware value must convert to the correct UTC instant.
        self._freeze(monkeypatch, utc_now=datetime(2026, 7, 16, 15, 20, 0, tzinfo=UTC))

        result = client._extract_timestamp("20260716223000")

        parsed = datetime.fromisoformat(result)
        assert parsed.tzinfo is not None
        assert parsed.astimezone(UTC).isoformat() == "2026-07-16T13:30:00+00:00"
        assert (
            parsed.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
            == "2026-07-16 22:30:00"
        )

    def test_full_14_digit_timestamp_near_kst_midnight_is_kst_aware(
        self, client, monkeypatch
    ):
        self._freeze(monkeypatch, utc_now=datetime(2026, 7, 16, 15, 20, 0, tzinfo=UTC))

        result = client._extract_timestamp("20260717000030")

        parsed = datetime.fromisoformat(result)
        assert parsed.tzinfo is not None
        assert (
            parsed.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
            == "2026-07-17 00:00:30"
        )

    def test_full_14_digit_timestamp_via_full_message_parse_not_misattributed_forward(
        self, client, monkeypatch
    ):
        # End-to-end: a domestic execution message with a 14-digit KST
        # ord_tmd inside the 15:00-23:59 misattribution window must produce
        # a filled_at that events.py's naive->UTC fallback does not shift
        # onto the wrong KST calendar date.
        self._freeze(monkeypatch, utc_now=datetime(2026, 7, 16, 15, 20, 0, tzinfo=UTC))

        message = build_domestic_message(
            symbol="005930",
            filled_qty="10",
            filled_price="70000",
            ord_tmd="20260716223000",
        )
        result = client._parse_message(message)

        assert result is not None
        event = build_lifecycle_event(result, account_mode="kis_mock")
        assert event.occurred_at.astimezone(KST).strftime("%Y-%m-%d") == "2026-07-16"
        assert event.occurred_at.astimezone(KST).strftime("%H:%M:%S") == "22:30:00"

    def test_already_iso_timestamp_passthrough_unaffected(self, client, monkeypatch):
        self._freeze(monkeypatch, utc_now=datetime(2026, 7, 16, 15, 20, 0, tzinfo=UTC))

        result = client._extract_timestamp("2026-07-16T15:30:45+09:00")

        assert result == "2026-07-16T15:30:45+09:00"

    def test_midnight_adjacent_hhmmss_via_full_message_parse(self, client, monkeypatch):
        # End-to-end: a domestic execution message with a midnight-adjacent
        # KST ord_tmd must produce a filled_at that round-trips to the
        # correct KST calendar date once parsed.
        self._freeze(monkeypatch, utc_now=datetime(2026, 7, 16, 15, 20, 0, tzinfo=UTC))

        message = build_domestic_message(
            symbol="005930", filled_qty="10", filled_price="70000", ord_tmd="001728"
        )
        result = client._parse_message(message)

        assert result is not None
        parsed = datetime.fromisoformat(result["filled_at"])
        assert parsed.astimezone(KST).strftime("%Y-%m-%d") == "2026-07-17"


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
    assert result["filled_qty"] == pytest.approx(5.0)
    assert result["filled_price"] == pytest.approx(150.25)
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
        assert result["filled_qty"] == pytest.approx(10.0)
        assert result["filled_price"] == pytest.approx(248.50)
        assert result["filled_amount"] == pytest.approx(2485.0)
        assert result["order_qty"] == pytest.approx(10.0)
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
        assert result["filled_qty"] == pytest.approx(5.0)
        assert result["filled_price"] == pytest.approx(175.25)
        assert result["order_qty"] == pytest.approx(10.0)
        assert result["currency"] == "USD"
        assert result["execution_status"] == "partial"
        assert client._is_execution_event(result) is True
