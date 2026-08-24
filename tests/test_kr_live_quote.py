"""ROB-396 follow-up: direct coverage for `_fetch_kr_live_quote`.

The analyze-path tests monkeypatch `_fetch_kr_live_quote` out, so its real body
(KIS `inquire_price` call + row parsing + as_of composition) was uncovered.
These tests exercise the body directly with a faked `KISClient.inquire_price`.
"""

import datetime
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.mcp_server.tooling import market_data_quotes
from app.services.brokers.kis.domestic_market_data import DomesticMarketDataMixin
from app.services.market_data import service as market_data_service
from app.services.market_data.contracts import OrderbookLevel, OrderbookSnapshot


def _make_kis(df=None, *, raises=False):
    class _FakeKIS:
        def __init__(self, *args, **kwargs):
            pass

        async def inquire_price(self, code, market="J"):
            if raises:
                raise RuntimeError("kis down")
            return df

    return _FakeKIS


@pytest.mark.asyncio
async def test_fetch_kr_live_quote_parses_price_and_as_of(monkeypatch):
    df = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2026-06-01"),
                "time": datetime.time(9, 30, 0),
                "open": 1200000.0,
                "high": 1230000.0,
                "low": 1190000.0,
                "close": 1225000.0,
                "volume": 5,
                "value": 6,
            }
        ],
        index=["012450"],
    )
    monkeypatch.setattr(market_data_quotes, "KISClient", _make_kis(df))

    quote = await market_data_quotes._fetch_kr_live_quote("012450")

    assert quote is not None
    assert quote["price"] == 1225000.0  # stck_prpr → close (live, not prev close)
    assert quote["source"] == "kis"
    assert quote["instrument_type"] == "equity_kr"
    assert quote["price_as_of"] == "2026-06-01T09:30:00+09:00"
    assert quote["fetched_at"] is not None


@pytest.mark.asyncio
async def test_fetch_kr_live_quote_as_of_without_time_is_unavailable(monkeypatch):
    # ROB-1121: provider가 time을 주지 않으면 날짜만으로 자정을 합성하지 않는다.
    df = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2026-06-01"),
                "time": None,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1225000.0,
                "volume": 1,
                "value": 1,
            }
        ],
        index=["012450"],
    )
    monkeypatch.setattr(market_data_quotes, "KISClient", _make_kis(df))

    quote = await market_data_quotes._fetch_kr_live_quote("012450")

    assert quote is not None
    assert quote["price_as_of"] is None
    assert quote["fetched_at"] is not None


@pytest.mark.asyncio
async def test_fetch_kr_live_quote_as_of_missing_date_time_is_unavailable(monkeypatch):
    # ROB-1121: provider가 date와 time 둘 다 주지 않으면 price_as_of는 None.
    df = pd.DataFrame(
        [
            {
                "date": None,
                "time": None,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1225000.0,
                "volume": 1,
                "value": 1,
            }
        ],
        index=["012450"],
    )
    monkeypatch.setattr(market_data_quotes, "KISClient", _make_kis(df))

    quote = await market_data_quotes._fetch_kr_live_quote("012450")

    assert quote is not None
    assert quote["price_as_of"] is None
    assert quote["fetched_at"] is not None


@pytest.mark.asyncio
async def test_fetch_kr_live_quote_pd_nat_time_is_unavailable(monkeypatch):
    df = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2026-06-01"),
                "time": pd.NaT,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1225000.0,
                "volume": 1,
                "value": 1,
            }
        ],
        index=["012450"],
    )
    monkeypatch.setattr(market_data_quotes, "KISClient", _make_kis(df))

    quote = await market_data_quotes._fetch_kr_live_quote("012450")

    assert quote is not None
    assert quote["price_as_of"] is None
    assert quote["fetched_at"] is not None


@pytest.mark.asyncio
async def test_fetch_kr_live_quote_empty_df_returns_none(monkeypatch):
    monkeypatch.setattr(market_data_quotes, "KISClient", _make_kis(pd.DataFrame()))

    assert await market_data_quotes._fetch_kr_live_quote("012450") is None


@pytest.mark.asyncio
async def test_fetch_kr_live_quote_swallows_kis_error_returns_none(monkeypatch):
    monkeypatch.setattr(market_data_quotes, "KISClient", _make_kis(raises=True))

    assert await market_data_quotes._fetch_kr_live_quote("012450") is None


class _DummyMarketData(DomesticMarketDataMixin):
    def __init__(self, raw_output):
        self._raw_output = raw_output

    def _kis_url(self, path: str) -> str:
        return f"https://mock.kis/{path}"

    async def _request_with_token_retry(self, **kwargs):
        return {"output": self._raw_output}


@pytest.mark.asyncio
async def test_inquire_price_parser_does_not_synthesize_local_clock():
    raw_payload = {
        "stck_shrn_iscd": "005930",
        "stck_bsop_date": "",
        "stck_cntg_hour": "",
        "stck_oprc": "100",
        "stck_hgpr": "110",
        "stck_lwpr": "90",
        "stck_prpr": "105",
        "acml_vol": "1000",
        "acml_tr_pbmn": "105000",
    }
    client = _DummyMarketData(raw_payload)
    df = await client.inquire_price("005930")

    assert not df.empty
    row = df.iloc[0].to_dict()
    assert row["date"] is None
    assert row["time"] is None


@pytest.mark.asyncio
async def test_inquire_price_parser_date_only():
    raw_payload = {
        "stck_shrn_iscd": "005930",
        "stck_bsop_date": "20260601",
        "stck_cntg_hour": "",
        "stck_oprc": "100",
        "stck_hgpr": "110",
        "stck_lwpr": "90",
        "stck_prpr": "105",
        "acml_vol": "1000",
        "acml_tr_pbmn": "105000",
    }
    client = _DummyMarketData(raw_payload)
    df = await client.inquire_price("005930")

    assert not df.empty
    row = df.iloc[0].to_dict()
    assert row["date"] == pd.Timestamp("2026-06-01")
    assert row["time"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_date", "raw_time"),
    [
        pytest.param("not-a-date", "093000", id="malformed-date-valid-time"),
        pytest.param("20260601", "not-a-time", id="valid-date-malformed-time"),
        pytest.param("20260601", "NaT", id="raw-nat-time"),
    ],
)
async def test_inquire_price_parser_preserves_quote_with_unusable_clock(
    raw_date,
    raw_time,
):
    raw_payload = {
        "stck_shrn_iscd": "005930",
        "stck_bsop_date": raw_date,
        "stck_cntg_hour": raw_time,
        "stck_oprc": "100",
        "stck_hgpr": "110",
        "stck_lwpr": "90",
        "stck_prpr": "105",
        "acml_vol": "1000",
        "acml_tr_pbmn": "105000",
    }

    df = await _DummyMarketData(raw_payload).inquire_price("005930")

    assert not df.empty
    row = df.iloc[0].to_dict()
    assert row["close"] == 105.0
    if raw_date == "not-a-date":
        assert row["date"] is None
        assert row["time"] == datetime.time(9, 30)
    else:
        assert row["date"] == pd.Timestamp("2026-06-01")
        assert row["time"] is None


def test_annotate_kr_price_freshness_timezone_naive_is_unavailable():
    naive_as_of = datetime.datetime(2026, 6, 1, 9, 30, 0)
    quote = {}
    market_data_quotes._annotate_kr_price_freshness(quote, naive_as_of)

    assert quote["price_as_of"] is None
    assert quote["is_stale_price"] is True
    assert quote["price_freshness"] == "unavailable"
    assert quote["price_usable"] is False
    assert quote["price_unavailable_reason"] == "missing_price_asof"


@pytest.mark.parametrize(
    ("as_of", "expected_as_of", "expected_freshness"),
    [
        pytest.param(
            "2026-05-31T15:30:00Z",
            "2026-06-01T00:30:00+09:00",
            "fresh",
            id="utc-z-same-kst-trading-date",
        ),
        pytest.param(
            "2026-05-31T11:30:00-04:00",
            "2026-06-01T00:30:00+09:00",
            "fresh",
            id="minus-four-same-kst-trading-date",
        ),
        pytest.param(
            "2026-05-30T15:30:00Z",
            "2026-05-31T00:30:00+09:00",
            "stale",
            id="prior-kst-date-control",
        ),
    ],
)
def test_annotate_kr_price_freshness_compares_kst_trading_date(
    as_of,
    expected_as_of,
    expected_freshness,
):
    quote = {}

    market_data_quotes._annotate_kr_price_freshness(
        quote,
        as_of,
        trading_date=datetime.date(2026, 6, 1),
    )

    assert quote["price_as_of"] == expected_as_of
    assert quote["price_freshness"] == expected_freshness
    assert quote["price_usable"] is (expected_freshness == "fresh")


@pytest.mark.asyncio
async def test_apply_nxt_quote_overlay_stamps_unavailable_freshness(monkeypatch):
    async def fake_session(data_state, *, now=None):
        return "nxt_premarket"

    async def fake_overlay(symbol, *, session):
        return {
            "price": 105.0,
            "session": session,
            "venue": "nxt",
            "price_source": "nxt_expected_price",
        }

    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", fake_session)
    monkeypatch.setattr(market_data_quotes, "_fetch_nxt_quote_overlay", fake_overlay)

    quote = {"price": 100.0}
    applied = await market_data_quotes._apply_nxt_quote_overlay(
        "005930", quote, data_state="premarket_unavailable"
    )

    assert applied is True
    assert quote["price"] == 105.0
    assert quote["price_as_of"] is None
    assert quote["price_freshness"] == "unavailable"
    assert quote["price_usable"] is False
    assert quote["is_stale_price"] is True
    assert quote["price_unavailable_reason"] == "missing_price_asof"


@pytest.mark.asyncio
async def test_apply_nxt_quote_overlay_uses_snapshot_as_of_for_freshness(monkeypatch):
    async def fake_session(data_state, *, now=None):
        return "nxt_premarket"

    async def fake_overlay(symbol, *, session):
        return {
            "price": 54500.0,
            "session": session,
            "venue": "nxt",
            "price_source": "nxt_mid",
            "price_as_of": "2026-08-24T08:01:35+09:00",
        }

    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", fake_session)
    monkeypatch.setattr(market_data_quotes, "_fetch_nxt_quote_overlay", fake_overlay)
    monkeypatch.setattr(
        market_data_quotes,
        "now_kst",
        lambda: datetime.datetime(2026, 8, 24, 8, 2, 0, tzinfo=market_data_quotes._KST),
    )

    quote = {"price": 54000.0}
    applied = await market_data_quotes._apply_nxt_quote_overlay(
        "011170", quote, data_state="premarket_unavailable"
    )

    assert applied is True
    assert quote["price_source"] == "nxt_mid"
    assert quote["price_as_of"] == "2026-08-24T08:01:35+09:00"
    assert quote["price_freshness"] == "fresh"
    assert quote["price_usable"] is True


def _nxt_snapshot_for_asof_tests(
    *,
    as_of: datetime.datetime | None,
    expected_price: int | None = None,
    asks: list[tuple[int, int]] | None = None,
    bids: list[tuple[int, int]] | None = None,
) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        symbol="011170",
        instrument_type="equity_kr",
        source="kis",
        asks=[OrderbookLevel(price=price, quantity=qty) for price, qty in (asks or [])],
        bids=[OrderbookLevel(price=price, quantity=qty) for price, qty in (bids or [])],
        total_ask_qty=100,
        total_bid_qty=100,
        bid_ask_ratio=1.0,
        as_of=as_of,
        expected_price=expected_price,
        venue="nxt",
        venue_label="NXT",
        kis_market_code="NX",
        is_empty_book=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expected_price", "asks", "bids", "expected_source", "expected_value"),
    [
        pytest.param(
            54500, [(54800, 10)], [(54200, 12)], "nxt_expected_price", 54500.0
        ),
        pytest.param(None, [(54800, 10)], [(54200, 12)], "nxt_mid", 54500.0),
        pytest.param(None, [(54800, 10)], [], "nxt_best_ask", 54800.0),
        pytest.param(None, [], [(54200, 12)], "nxt_best_bid", 54200.0),
    ],
)
async def test_fetch_nxt_quote_overlay_propagates_as_of_to_all_price_branches(
    monkeypatch,
    expected_price,
    asks,
    bids,
    expected_source,
    expected_value,
):
    as_of = datetime.datetime(2026, 8, 24, 8, 1, 35, tzinfo=market_data_quotes._KST)
    snapshot = _nxt_snapshot_for_asof_tests(
        as_of=as_of,
        expected_price=expected_price,
        asks=asks,
        bids=bids,
    )
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(return_value=snapshot),
    )

    overlay = await market_data_quotes._fetch_nxt_quote_overlay(
        "011170", session="nxt_premarket"
    )

    assert overlay is not None
    assert overlay["price"] == expected_value
    assert overlay["price_source"] == expected_source
    assert overlay["price_as_of"] == "2026-08-24T08:01:35+09:00"


@pytest.mark.asyncio
async def test_apply_nxt_quote_overlay_stale_snapshot_remains_fail_closed(monkeypatch):
    async def fake_session(data_state, *, now=None):
        return "nxt_premarket"

    stale_as_of = datetime.datetime(
        2026, 8, 23, 8, 1, 35, tzinfo=market_data_quotes._KST
    )
    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", fake_session)
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(
            return_value=_nxt_snapshot_for_asof_tests(
                as_of=stale_as_of,
                asks=[(54800, 10)],
                bids=[(54200, 12)],
            )
        ),
    )
    monkeypatch.setattr(
        market_data_quotes,
        "now_kst",
        lambda: datetime.datetime(2026, 8, 24, 8, 2, 0, tzinfo=market_data_quotes._KST),
    )

    quote = {"price": 54000.0}
    applied = await market_data_quotes._apply_nxt_quote_overlay(
        "011170", quote, data_state="premarket_unavailable"
    )

    assert applied is True
    assert quote["price_source"] == "nxt_mid"
    assert quote["is_stale_price"] is True
    assert quote["price_freshness"] == "stale"
    assert quote["price_usable"] is False
    assert quote["price_unavailable_reason"] == "stale_price_asof"


@pytest.mark.asyncio
async def test_apply_nxt_quote_overlay_missing_snapshot_as_of_remains_fail_closed(
    monkeypatch,
):
    async def fake_session(data_state, *, now=None):
        return "nxt_premarket"

    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", fake_session)
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(
            return_value=_nxt_snapshot_for_asof_tests(
                as_of=None,
                asks=[(54800, 10)],
                bids=[(54200, 12)],
            )
        ),
    )

    quote = {"price": 54000.0}
    applied = await market_data_quotes._apply_nxt_quote_overlay(
        "011170", quote, data_state="premarket_unavailable"
    )

    assert applied is True
    assert quote["price_source"] == "nxt_mid"
    assert quote["price_as_of"] is None
    assert quote["is_stale_price"] is True
    assert quote["price_freshness"] == "unavailable"
    assert quote["price_usable"] is False
    assert quote["price_unavailable_reason"] == "missing_price_asof"


NXT_ASOF_VERIFY_NOW = datetime.datetime(
    2026, 8, 24, 8, 2, 0, tzinfo=market_data_quotes._KST
)
NXT_ASOF_VERIFY_BOOK = {
    "askp1": "54800",
    "askp_rsqn1": "10",
    "bidp1": "54200",
    "bidp_rsqn1": "12",
    "total_askp_rsqn": "100",
    "total_bidp_rsqn": "100",
}


async def _run_nxt_asof_verify_case(
    monkeypatch,
    output1: dict[str, object],
    *,
    received_at: datetime.datetime = NXT_ASOF_VERIFY_NOW,
) -> dict[str, object]:
    class DummyKIS:
        async def inquire_orderbook_snapshot(self, code: str, market: str = "J"):
            return output1, None

    monkeypatch.setattr(market_data_service, "now_kst", lambda: received_at)
    monkeypatch.setattr(market_data_service, "KISClient", DummyKIS)
    monkeypatch.setattr(market_data_quotes, "now_kst", lambda: NXT_ASOF_VERIFY_NOW)

    async def fake_session(data_state, *, now=None):
        return "nxt_premarket"

    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", fake_session)
    quote = {"price": 54000.0}
    applied = await market_data_quotes._apply_nxt_quote_overlay(
        "011170", quote, data_state="premarket_unavailable"
    )
    assert applied is True
    return quote


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "case_id",
        "output1",
        "received_at",
        "expected_as_of",
        "expected_freshness",
        "expected_usable",
    ),
    [
        # The original A/B/C/D/F cases are retained with their post-fix
        # expected decisions; A/F are transport-time evidence, not fabricated
        # broker-date evidence.
        pytest.param(
            "A1",
            {**NXT_ASOF_VERIFY_BOOK, "aspr_acpt_hour": "153000"},
            NXT_ASOF_VERIFY_NOW,
            "2026-08-24T08:02:00+09:00",
            "fresh",
            True,
            id="A1-clock-only-transport-time",
        ),
        pytest.param(
            "A2",
            {**NXT_ASOF_VERIFY_BOOK, "aspr_acpt_hour": "080135"},
            NXT_ASOF_VERIFY_NOW,
            "2026-08-24T08:02:00+09:00",
            "fresh",
            True,
            id="A2-clock-only-transport-time",
        ),
        pytest.param(
            "A3",
            {**NXT_ASOF_VERIFY_BOOK, "aspr_acpt_hour": "235959"},
            NXT_ASOF_VERIFY_NOW,
            "2026-08-24T08:02:00+09:00",
            "fresh",
            True,
            id="A3-clock-only-no-future-synthesis",
        ),
        pytest.param(
            "A4",
            {**NXT_ASOF_VERIFY_BOOK, "aspr_acpt_hour": "000000"},
            NXT_ASOF_VERIFY_NOW,
            "2026-08-24T08:02:00+09:00",
            "fresh",
            True,
            id="A4-clock-only-transport-time",
        ),
        pytest.param(
            "B",
            dict(NXT_ASOF_VERIFY_BOOK),
            NXT_ASOF_VERIFY_NOW,
            None,
            "unavailable",
            False,
            id="B-no-broker-clock-missing",
        ),
        *[
            pytest.param(
                f"C-{junk!r}",
                {**NXT_ASOF_VERIFY_BOOK, "aspr_acpt_hour": junk},
                NXT_ASOF_VERIFY_NOW,
                None,
                "unavailable",
                False,
                id=f"C-malformed-{junk!r}",
            )
            for junk in ["", "garbage", "99:99:99", None, "1", "9999999"]
        ],
        pytest.param(
            "D",
            {
                **NXT_ASOF_VERIFY_BOOK,
                "aspr_acpt_hour": "153000",
                "stck_bsop_date": "20260823",
            },
            NXT_ASOF_VERIFY_NOW,
            "2026-08-23T15:30:00+09:00",
            "stale",
            False,
            id="D-provider-date-stale",
        ),
        pytest.param(
            "F",
            {**NXT_ASOF_VERIFY_BOOK, "aspr_acpt_hour": "235959"},
            NXT_ASOF_VERIFY_NOW,
            "2026-08-24T08:02:00+09:00",
            "fresh",
            True,
            id="F-clock-only-does-not-create-future",
        ),
    ],
)
async def test_nxt_asof_adversarial_regression_cases(
    monkeypatch,
    case_id,
    output1,
    received_at,
    expected_as_of,
    expected_freshness,
    expected_usable,
):
    """All 14 verifier cases have explicit post-fix expectations."""
    quote = await _run_nxt_asof_verify_case(
        monkeypatch, output1, received_at=received_at
    )

    assert quote["price_as_of"] == expected_as_of, case_id
    assert quote["price_freshness"] == expected_freshness, case_id
    assert quote["price_usable"] is expected_usable, case_id
    if expected_usable:
        assert "price_unavailable_reason" not in quote, case_id
    else:
        assert quote["price_unavailable_reason"] == (
            "missing_price_asof" if expected_as_of is None else "stale_price_asof"
        ), case_id


@pytest.mark.asyncio
async def test_nxt_asof_transport_receive_30_minutes_old_is_stale(monkeypatch):
    received_at = NXT_ASOF_VERIFY_NOW - datetime.timedelta(minutes=30)
    quote = await _run_nxt_asof_verify_case(
        monkeypatch,
        {**NXT_ASOF_VERIFY_BOOK, "aspr_acpt_hour": "073200"},
        received_at=received_at,
    )

    assert quote["price_as_of"] == "2026-08-24T07:32:00+09:00"
    assert quote["price_freshness"] == "stale"
    assert quote["price_usable"] is False


@pytest.mark.asyncio
async def test_nxt_asof_provider_timestamp_in_future_is_stale(monkeypatch):
    quote = await _run_nxt_asof_verify_case(
        monkeypatch,
        {
            **NXT_ASOF_VERIFY_BOOK,
            "aspr_acpt_hour": "080300",
            "stck_bsop_date": "20260824",
        },
    )

    assert quote["price_as_of"] == "2026-08-24T08:03:00+09:00"
    assert quote["price_freshness"] == "stale"
    assert quote["price_usable"] is False


@pytest.mark.asyncio
async def test_nxt_asof_previous_date_is_stale_even_within_five_minutes(monkeypatch):
    quote = await _run_nxt_asof_verify_case(
        monkeypatch,
        {
            **NXT_ASOF_VERIFY_BOOK,
            "aspr_acpt_hour": "080135",
            "stck_bsop_date": "20260823",
        },
    )

    assert quote["price_as_of"] == "2026-08-23T08:01:35+09:00"
    assert quote["price_freshness"] == "stale"
    assert quote["price_usable"] is False
