"""hk 6661: KIS news-headline normalizer, summary and client request shape.

No network, no broker construction. Fixtures are doc-shaped (see
``tests/fixtures/kis_news/*.json``); their values are synthetic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services import kis_news_shadow as kn
from app.services.brokers.kis import _base_market_data as base
from app.services.brokers.kis.domestic_market_data import DomesticMarketDataMixin
from app.services.brokers.kis.overseas_market_data import OverseasMarketDataMixin

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kis_news"
SEEN = datetime(2025, 11, 25, 14, 0, 0, tzinfo=UTC)


def _load(name: str, key: str) -> list[dict]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))[key]


# ── time parsing ──


def test_provider_time_is_read_as_kst_and_stored_in_utc() -> None:
    raw, utc, basis = kn.parse_kis_provider_time("20251125", "093015")
    assert raw == "20251125 093015"
    assert utc == "2025-11-25T00:30:15+00:00"
    assert basis == kn.KIS_TIME_BASIS


@pytest.mark.parametrize(
    ("date_raw", "time_raw", "reason"),
    [
        (None, None, "missing"),
        ("", "", "missing"),
        ("2025-11-25", "093015", "unparsable"),
        ("20251125", "25:99:00", "unparsable"),
        ("20251125", None, "unparsable"),
        ("20251332", "093015", "unparsable"),
    ],
)
def test_unparsable_provider_time_is_null_with_reason(
    date_raw: str | None, time_raw: str | None, reason: str
) -> None:
    _raw, utc, basis = kn.parse_kis_provider_time(date_raw, time_raw)
    assert utc is None
    assert basis == reason


# ── brknews matching (market-wide) ──


def test_brknews_matches_polled_symbols_and_keeps_unmatched_with_null_symbol() -> None:
    rows = kn.normalize_brknews_rows(
        _load("brknews_title", "output"),
        poll_id="p1",
        first_received_at=SEEN,
        polled_symbols=["NVDA", "BRK.B", "AMD"],
    )
    by_title: dict[str, list[dict]] = {}
    for row in rows:
        by_title.setdefault(row["title"], []).append(row)

    # One headline tagged NVDA and AMD yields one row per polled symbol.
    nvda_story = by_title["NVIDIA unveils new data center chip"]
    assert sorted(r["query_symbol"] for r in nvda_story) == ["AMD", "NVDA"]
    assert all(r["symbol_match"] == "iscd_exact" for r in nvda_story)
    assert all(r["source"] == "kis" for r in nvda_story)
    assert nvda_story[0]["provider_id"] == f"{kn.TR_BRKNEWS}:2025112500001"
    assert nvda_story[0]["created_utc"] == "2025-11-25T00:30:15+00:00"
    assert nvda_story[0]["first_received_at"] == SEEN.isoformat()

    # No iscd code matches a polled symbol: kept once, symbol null.
    unmatched = by_title["Oil prices edge higher"]
    assert len(unmatched) == 1
    assert unmatched[0]["query_symbol"] is None
    assert unmatched[0]["symbol_match"] == "unmatched"
    assert unmatched[0]["symbols"] == []

    # "BRK/B" normalizes to the DB form "BRK.B" through app/core/symbol.py.
    brk = by_title["Berkshire B item"][0]
    assert brk["query_symbol"] == "BRK.B"
    assert brk["symbols"] == ["BRK.B"]
    # Unparsable provider time (bad date format and bad clock) is null + reason.
    assert brk["created_utc"] is None
    assert brk["created_reason"] == "unparsable"


def test_overseas_and_domestic_rows_keep_query_symbol_and_time_basis() -> None:
    overseas = kn.normalize_overseas_rows(
        _load("overseas_news_title", "outblock1"),
        poll_id="p1",
        first_received_at=SEEN,
        symbol="NVDA",
    )
    assert overseas[0]["query_symbol"] == "NVDA"
    assert overseas[0]["title"] == "NVIDIA raises guidance"
    assert overseas[0]["provider_id"] == f"{kn.TR_OVERSEAS_NEWS}:20251125BSN000123"
    assert overseas[0]["created_basis"] == kn.KIS_QUERY_LABEL_BASIS

    domestic = kn.normalize_domestic_rows(
        _load("domestic_news_title", "output"),
        poll_id="p1",
        first_received_at=SEEN,
        symbol="005930",
    )
    assert domestic[0]["query_symbol"] == "005930"
    assert domestic[0]["title"] == "삼성전자 공급계약 공시"
    assert domestic[0]["created_utc"] == "2025-11-25T00:05:00+00:00"
    assert domestic[0]["created_basis"] == kn.KIS_TIME_BASIS


# ── summary ──


def _article(
    source: str, title: str, poll: str, symbol: str | None, when: str, provider_id: str
) -> dict:
    return {
        "record_type": "article",
        "source": source,
        "title": title,
        "poll_id": poll,
        "query_symbol": symbol,
        "query_param": "x",
        "provider_id": provider_id,
        "first_received_at": when,
    }


def test_summary_reports_kis_coverage_ordering_and_overlap() -> None:
    kis = [
        _article(
            "kis", "Shared story", "p1", "NVDA", "2025-11-25T14:00:01+00:00", "k1"
        ),
        _article("kis", "KIS only", "p1", "NVDA", "2025-11-25T14:00:02+00:00", "k2"),
        _article("kis", "Unmatched", "p1", None, "2025-11-25T14:00:03+00:00", "k3"),
    ]
    ben = [
        _article(
            "benzinga", "Shared story", "p1", "NVDA", "2025-11-25T14:00:05+00:00", "b1"
        ),
    ]
    fin = [
        _article(
            "finnhub", "Shared story", "p1", "NVDA", "2025-11-25T14:00:04+00:00", "f1"
        ),
    ]
    frames = [
        {"record_type": "poll_header", "poll_id": "p1", "symbols": ["NVDA"]},
        {
            "record_type": "poll_completion",
            "poll_id": "p1",
            "symbols_completed": ["NVDA"],
        },
    ]
    summary = kn.summarize_kis(kis, ben, fin, frames)
    assert summary["kis_count"] == 3
    assert summary["coverage_by_symbol"]["NVDA"] == {
        "kis": 2,
        "benzinga": 1,
        "finnhub": 1,
    }
    assert summary["kis_unmatched_headlines"] == 1
    assert summary["headline_overlap"] == {
        "kis_with_benzinga": 1,
        "kis_with_finnhub": 1,
    }
    # The shared title was seen first by KIS (14:00:01) before Finnhub and Benzinga.
    assert summary["first_seen_titles_compared"] == 1
    assert summary["first_seen_wins"] == {"kis": 1, "benzinga": 0, "finnhub": 0}


def test_kis_error_is_excluded_per_cell_and_market_error_marks_planned_symbols() -> (
    None
):
    kis = [
        _article(
            "kis", "Per-symbol ok", "p1", "AMD", "2025-11-25T14:00:01+00:00", "k1"
        ),
        {
            "record_type": "poll_status",
            "source": "kis",
            "poll_id": "p1",
            "query_symbol": "NVDA",
            "query_param": "overseas_news_title",
            "status": "error",
            "count": 0,
        },
        {
            "record_type": "poll_status",
            "source": "kis",
            "poll_id": "p1",
            "query_symbol": None,
            "query_param": "brknews_title",
            "status": "error",
            "count": 0,
        },
    ]
    frames = [
        {"record_type": "poll_header", "poll_id": "p1", "symbols": ["AMD", "NVDA"]},
        {
            "record_type": "poll_completion",
            "poll_id": "p1",
            "symbols_completed": ["AMD", "NVDA"],
        },
    ]
    summary = kn.summarize_kis(kis, [], [], frames)
    assert summary["kis_count"] == 1
    assert summary["coverage_by_symbol"]["AMD"]["kis"] == 1
    assert {(p["symbol"], p["reason"]) for p in summary["unreliable_polls"]} == {
        ("AMD", "market_wide_error"),
        ("NVDA", "market_wide_error"),
    }


# ── client request shape (fake self, no broker) ──


class _FakeMarket(DomesticMarketDataMixin, OverseasMarketDataMixin):
    def __init__(self, responses: dict[str, object]) -> None:
        self._responses = responses
        self.calls: list[dict] = []

    def _kis_url(self, path: str) -> str:
        return f"https://openapi.koreainvestment.com:9443{path}"

    async def _request_with_token_retry(self, *, tr_id, url, params, api_name, **kw):
        self.calls.append(
            {"tr_id": tr_id, "url": url, "params": params, "api": api_name}
        )
        return self._responses[api_name]


@pytest.mark.asyncio
async def test_news_title_methods_send_documented_tr_and_params() -> None:
    fake = _FakeMarket(
        {
            "news_titles_overseas_brknews": {"output": [{"cntt_usiq_srno": "1"}]},
            "news_titles_overseas": {"outblock1": []},
            "news_titles_domestic": {"output": {"cntt_usiq_srno": "2"}},
        }
    )
    brk = await fake.news_titles_overseas_brknews()
    ov = await fake.news_titles_overseas("BRK.B")
    dom = await fake.news_titles_domestic("005930")
    assert brk == [{"cntt_usiq_srno": "1"}]
    assert ov == []
    assert dom == [{"cntt_usiq_srno": "2"}]  # single object becomes one row

    by_api = {c["api"]: c for c in fake.calls}
    assert by_api["news_titles_overseas_brknews"]["tr_id"] == "FHKST01011801"
    assert by_api["news_titles_overseas_brknews"]["url"].endswith(
        "/uapi/overseas-price/v1/quotations/brknews-title"
    )
    assert by_api["news_titles_overseas_brknews"]["params"]["FID_INPUT_ISCD"] == ""
    assert by_api["news_titles_overseas"]["tr_id"] == "HHPSTH60100C1"
    assert by_api["news_titles_overseas"]["params"]["SYMB"] == "BRK/B"  # to_kis_symbol
    assert by_api["news_titles_domestic"]["tr_id"] == "FHKST01011800"
    assert by_api["news_titles_domestic"]["params"]["FID_INPUT_ISCD"] == "005930"


@pytest.mark.asyncio
async def test_malformed_news_payload_raises_instead_of_empty() -> None:
    fake = _FakeMarket({"news_titles_overseas": {"rt_cd": "0"}})
    with pytest.raises(RuntimeError, match="missing outblock1"):
        await fake.news_titles_overseas("NVDA")
    assert base.news_title_rows({"output": []}, "output") == []
