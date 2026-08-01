# tests/test_analyze_stock_kr_live_price.py
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.mcp_server.tooling import analysis_analyze, market_data_quotes
from app.services.brokers.kis.domestic_market_data import DomesticMarketDataMixin
from tests._mcp_tooling_support import build_tools

KST = ZoneInfo("Asia/Seoul")

pytestmark = [pytest.mark.integration]


@pytest.fixture(autouse=True)
def _no_nxt_overlay_by_default(monkeypatch):
    """ROB-725: keep _resolve_kr_quote tests hermetic.

    Without this guard, tests that don't mock the overlay would trigger the REAL
    _apply_nxt_quote_overlay during the 15:30–20:00 KST NXT-after wall-clock
    window (session detection is wall-clock gated, not data_state gated), firing
    a live get_orderbook network call that can overwrite the mocked price. Tests
    that exercise the overlay set their own fake, which overrides this default.
    """

    async def _noop(symbol, quote, *, data_state):
        return False

    monkeypatch.setattr(analysis_analyze, "_apply_nxt_quote_overlay", _noop)


def _ohlcv():
    # 전일 일봉(어제 날짜) — fallback 경로용
    yesterday = pd.Timestamp(datetime.now(KST).date() - timedelta(days=1))
    return pd.DataFrame(
        {
            "open": [100.0],
            "high": [110.0],
            "low": [90.0],
            "close": [105.0],
            "volume": [1000],
            "value": [105000.0],
        },
        index=[yesterday],
    )


class _RawKISPriceParser(DomesticMarketDataMixin):
    def __init__(self, raw_output):
        self._raw_output = raw_output

    def _kis_url(self, path: str) -> str:
        return f"https://mock.kis/{path}"

    async def _request_with_token_retry(self, **kwargs):
        return {"output": self._raw_output}


@pytest.mark.asyncio
async def test_kr_live_price_today_is_not_stale(monkeypatch):
    today = datetime.now(KST)

    async def fake_live(symbol):
        return {
            "symbol": symbol,
            "instrument_type": "equity_kr",
            "price": 1225000.0,
            "open": 1200000.0,
            "high": 1230000.0,
            "low": 1190000.0,
            "volume": 5,
            "value": 6,
            "source": "kis",
            "price_as_of": today.isoformat(),
        }

    monkeypatch.setattr(analysis_analyze, "_fetch_kr_live_quote", fake_live)
    quote = await analysis_analyze._resolve_kr_quote("012450", _ohlcv())
    assert quote["price"] == 1225000.0
    assert quote["is_stale_price"] is False


@pytest.mark.asyncio
async def test_kr_prev_day_quote_is_stale(monkeypatch):
    prev = datetime.now(KST) - timedelta(days=1)

    async def fake_live(symbol):
        return {
            "symbol": symbol,
            "instrument_type": "equity_kr",
            "price": 1173000.0,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "volume": 1,
            "value": 1,
            "source": "kis",
            "price_as_of": prev.isoformat(),
        }

    monkeypatch.setattr(analysis_analyze, "_fetch_kr_live_quote", fake_live)
    quote = await analysis_analyze._resolve_kr_quote("012450", _ohlcv())
    assert quote["is_stale_price"] is True


@pytest.mark.asyncio
async def test_kr_live_failure_falls_back_to_ohlcv_stale(monkeypatch):
    async def fake_live(symbol):
        return None  # inquire_price 실패/빈응답

    monkeypatch.setattr(analysis_analyze, "_fetch_kr_live_quote", fake_live)
    quote = await analysis_analyze._resolve_kr_quote("012450", _ohlcv())
    assert quote["price"] == 105.0  # 일봉 종가 fallback
    assert quote["is_stale_price"] is True
    assert quote["price_as_of"] is not None


@pytest.mark.asyncio
async def test_kr_quote_overlays_nxt_price_in_premarket(monkeypatch):
    today = datetime.now(KST)

    async def fake_live(symbol):
        return {
            "symbol": symbol,
            "instrument_type": "equity_kr",
            "price": 168300.0,  # stale KRX prior close
            "source": "kis",
            "price_as_of": (today - timedelta(days=1)).isoformat(),
        }

    async def fake_overlay(symbol, quote, *, data_state):
        quote["price"] = 173500.0
        quote["price_source"] = "nxt_expected_price"
        quote["session"] = "nxt_premarket"
        quote["data_state"] = "fresh"
        return True

    monkeypatch.setattr(analysis_analyze, "_fetch_kr_live_quote", fake_live)
    monkeypatch.setattr(analysis_analyze, "_apply_nxt_quote_overlay", fake_overlay)
    monkeypatch.setattr(
        analysis_analyze,
        "kr_market_data_state",
        lambda *a, **k: "premarket_unavailable",
    )

    quote = await analysis_analyze._resolve_kr_quote("192820", _ohlcv())

    assert quote["price"] == 173500.0
    assert quote["price_source"] == "nxt_expected_price"
    # ROB-1121: NXT orderbook overlay에는 provider 체결 timestamp가 없으므로
    # freshness를 unavailable/fail-closed로 표현. 벽시계를 as_of로 위장하지 않는다.
    assert quote["is_stale_price"] is True
    assert quote["price_freshness"] == "unavailable"
    assert quote["price_usable"] is False
    assert quote["price_as_of"] is None


@pytest.mark.asyncio
async def test_kr_quote_surfaces_self_describing_fields_end_to_end(monkeypatch):
    """ROB-888: through the analyze path with the REAL overlay, the SK하이닉스
    premarket case surfaces krx_prev_close / change_pct / session_state so the
    operator can cross-check the gap from the MCP quote without CDP naver."""
    from app.mcp_server.tooling import market_data_quotes

    async def fake_live(symbol):
        return {
            "symbol": symbol,
            "instrument_type": "equity_kr",
            "price": 1913000.0,  # KRX prior close (premarket "개장전" block)
            "source": "kis",
            "price_as_of": (datetime.now(KST) - timedelta(days=1)).isoformat(),
        }

    async def fake_session(data_state, *, now=None):
        return "nxt_premarket"

    async def fake_inner_overlay(symbol, *, session):
        return {
            "price": 2082500.0,  # NXT premarket realtime (nxt_mid)
            "session": session,
            "venue": "nxt",
            "price_source": "nxt_mid",
        }

    monkeypatch.setattr(analysis_analyze, "_fetch_kr_live_quote", fake_live)
    # Restore the REAL overlay (autouse fixture noop'd it) and mock its internals.
    monkeypatch.setattr(
        analysis_analyze,
        "_apply_nxt_quote_overlay",
        market_data_quotes._apply_nxt_quote_overlay,
    )
    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", fake_session)
    monkeypatch.setattr(
        market_data_quotes, "_fetch_nxt_quote_overlay", fake_inner_overlay
    )
    monkeypatch.setattr(
        analysis_analyze,
        "kr_market_data_state",
        lambda *a, **k: "premarket_unavailable",
    )

    quote = await analysis_analyze._resolve_kr_quote("000660", _ohlcv())

    assert quote["price"] == 2082500.0
    assert quote["price_source"] == "nxt_mid"
    assert quote["session_state"] == "premarket"
    assert quote["krx_prev_close"] == 1913000.0
    assert quote["change_pct"] == pytest.approx(8.86, abs=0.01)


@pytest.mark.asyncio
async def test_kr_quote_keeps_kis_price_when_no_overlay(monkeypatch):
    today = datetime.now(KST)

    async def fake_live(symbol):
        return {
            "symbol": symbol,
            "instrument_type": "equity_kr",
            "price": 168300.0,
            "source": "kis",
            "price_as_of": today.isoformat(),
        }

    async def fake_overlay(symbol, quote, *, data_state):
        return False  # not an NXT session / empty book

    monkeypatch.setattr(analysis_analyze, "_fetch_kr_live_quote", fake_live)
    monkeypatch.setattr(analysis_analyze, "_apply_nxt_quote_overlay", fake_overlay)
    monkeypatch.setattr(
        analysis_analyze, "kr_market_data_state", lambda *a, **k: "fresh"
    )

    quote = await analysis_analyze._resolve_kr_quote("192820", _ohlcv())

    assert quote["price"] == 168300.0
    assert "price_source" not in quote
    assert quote["is_stale_price"] is False  # today's KIS as_of, unchanged


@pytest.mark.asyncio
async def test_kr_malformed_live_timestamp_does_not_fallback_to_fresh_daily(
    monkeypatch,
):
    """AC3 / Finding 1: malformed provider date/time in live quote must remain unavailable

    and must not collapse into same-day daily OHLCV fallback that would evaluate fresh.
    """
    today = pd.Timestamp(datetime.now(KST).date())
    same_day_ohlcv = pd.DataFrame(
        {
            "open": [100.0],
            "high": [110.0],
            "low": [90.0],
            "close": [105.0],
            "volume": [1000],
            "value": [105000.0],
        },
        index=[today],
    )

    async def fake_live_malformed(symbol):
        return {
            "symbol": symbol,
            "instrument_type": "equity_kr",
            "price": 105.0,
            "open": 100.0,
            "high": 110.0,
            "low": 90.0,
            "volume": 1000,
            "value": 105000,
            "source": "kis",
            "price_as_of": None,
            "fetched_at": datetime.now(KST).isoformat(),
        }

    monkeypatch.setattr(analysis_analyze, "_fetch_kr_live_quote", fake_live_malformed)
    quote = await analysis_analyze._resolve_kr_quote("012450", same_day_ohlcv)

    assert quote is not None
    assert quote["price_as_of"] is None
    assert quote["is_stale_price"] is True
    assert quote["price_freshness"] == "unavailable"
    assert quote["price_usable"] is False
    assert quote["price_unavailable_reason"] == "missing_price_asof"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_date", "raw_time"),
    [
        pytest.param("not-a-date", "093000", id="malformed-date-valid-time"),
        pytest.param("20260601", "not-a-time", id="valid-date-malformed-time"),
        pytest.param("20260601", "NaT", id="raw-nat-time"),
    ],
)
async def test_registered_analyze_stock_preserves_malformed_live_clock_provenance(
    monkeypatch,
    raw_date,
    raw_time,
):
    """Exercise raw KIS parser -> real live fetch -> registered MCP analyze_stock."""
    raw_payload = {
        "stck_shrn_iscd": "012450",
        "stck_bsop_date": raw_date,
        "stck_cntg_hour": raw_time,
        "stck_oprc": "200",
        "stck_hgpr": "230",
        "stck_lwpr": "190",
        "stck_prpr": "222",
        "acml_vol": "10",
        "acml_tr_pbmn": "2220",
    }
    parser = _RawKISPriceParser(raw_payload)
    monkeypatch.setattr(market_data_quotes, "KISClient", lambda: parser)

    same_day_ohlcv = pd.DataFrame(
        {
            "date": [pd.Timestamp("2026-06-01")],
            "open": [100.0],
            "high": [110.0],
            "low": [90.0],
            "close": [105.0],
            "volume": [1000],
            "value": [105000.0],
        }
    )

    async def fake_ohlcv(symbol, market_type, count):
        return same_day_ohlcv

    async def no_tradability(symbols):
        return {}

    async def no_overlay(symbol, quote, *, data_state):
        return False

    async def no_market_tasks(
        named_tasks,
        normalized_symbol,
        market_type,
        loop,
        refresh=False,
    ):
        return None

    monkeypatch.setattr(analysis_analyze, "_fetch_ohlcv_for_indicators", fake_ohlcv)
    monkeypatch.setattr(analysis_analyze, "get_kr_nxt_tradability", no_tradability)
    monkeypatch.setattr(analysis_analyze, "_apply_nxt_quote_overlay", no_overlay)
    monkeypatch.setattr(
        analysis_analyze, "_append_common_tasks", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        analysis_analyze, "_append_market_specific_tasks", no_market_tasks
    )

    result = await build_tools()["analyze_stock"]("012450", market="kr")
    quote = result["quote"]

    assert quote["price"] == 222.0
    assert quote["price_as_of"] is None
    assert quote["price_freshness"] == "unavailable"
    assert quote["price_usable"] is False
    assert quote["price_unavailable_reason"] == "missing_price_asof"
