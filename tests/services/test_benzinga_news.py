"""hk 6395: Benzinga shadow client tests (recorded-shape fixtures only)."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx
import pytest

from app.services import benzinga_news as bz

# Recorded response shape per the design-session findings (2026-11-25):
# GET /api/v2/news -> JSON list; created/updated are RFC 2822 with -0400.
BENZINGA_ITEM = {
    "id": 44901111,
    "title": "Nvidia pops on strong datacenter demand",
    "body": "Full body text.",
    "teaser": "Nvidia shares rose...",
    "url": "https://www.benzinga.com/news/25/11/44901111/nvidia-pops?utm_source=x",
    "author": "Benzinga Desk",
    "created": "Tue, 25 Nov 2025 09:30:00 -0400",
    "updated": "Tue, 25 Nov 2025 10:15:00 -0400",
    "stocks": [{"name": "NVDA"}, {"name": "AAPL"}],
    "channels": [{"name": "News"}, {"name": "Markets"}],
    "tags": [{"name": "datacenter"}],
    "image": "https://example.com/img.jpg",
    "importance_rank": 3,
}

FIXED_NOW = datetime(2025, 11, 25, 14, 0, 0, tzinfo=UTC)


def _client_factory(payload: object, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    def factory(timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )

    return factory


def test_parse_provider_time_minus_0400_to_utc() -> None:
    parsed = bz.parse_provider_time("Tue, 25 Nov 2025 09:30:00 -0400")
    assert parsed is not None
    assert parsed.isoformat() == "2025-11-25T13:30:00+00:00"


def test_parse_provider_time_invalid_is_none() -> None:
    assert bz.parse_provider_time(None) is None
    assert bz.parse_provider_time("") is None
    assert bz.parse_provider_time("not a date") is None


def test_normalize_record_preserves_raw_and_uses_injected_clock() -> None:
    record = bz.normalize_record(
        BENZINGA_ITEM, poll_id="poll-1", first_received_at=FIXED_NOW
    )
    assert record["record_type"] == "article"
    assert record["source"] == "benzinga"
    assert record["provider_id"] == "44901111"
    assert record["created_raw"] == "Tue, 25 Nov 2025 09:30:00 -0400"
    assert record["updated_raw"] == "Tue, 25 Nov 2025 10:15:00 -0400"
    assert record["created_basis"] == "provider_rfc2822"
    assert record["created_utc"] == "2025-11-25T13:30:00+00:00"
    assert record["updated_utc"] == "2025-11-25T14:15:00+00:00"
    assert record["first_received_at"] == "2025-11-25T14:00:00+00:00"
    assert record["symbols"] == ["AAPL", "NVDA"]
    assert record["channels"] == ["News", "Markets"]
    assert record["poll_id"] == "poll-1"
    # Query-only junk never leaks into the record identity.
    assert record["url"] == BENZINGA_ITEM["url"]


@pytest.mark.asyncio
async def test_fetch_sends_token_as_query_param() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        assert request.url.params.get("primaryTickers") == "NVDA"
        assert "tickers" not in request.url.params
        assert request.url.params.get("token") == "KEY-123"
        assert "Authorization" not in request.headers
        return httpx.Response(200, json=[BENZINGA_ITEM])

    def factory(timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )

    records = await bz.fetch_benzinga_news(
        "nvda",
        api_key="KEY-123",
        poll_id="poll-1",
        now=FIXED_NOW,
        client_factory=factory,
    )
    assert len(records) == 1
    assert records[0]["created_utc"] == "2025-11-25T13:30:00+00:00"
    assert "token=KEY-123" in seen["url"]  # on the wire only, never logged


@pytest.mark.asyncio
async def test_fetch_tickers_param_is_evidence_query() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        assert request.url.params.get("tickers") == "NVDA"
        assert "primaryTickers" not in request.url.params
        return httpx.Response(200, json=[])

    def factory(timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )

    records = await bz.fetch_benzinga_news(
        "NVDA",
        api_key="KEY-123",
        poll_id="poll-1",
        now=FIXED_NOW,
        client_factory=factory,
        ticker_param="tickers",
    )
    assert records == []
    assert "tickers=NVDA" in seen["url"]

@pytest.mark.asyncio
async def test_fetch_http_error_never_carries_the_key() -> None:
    with pytest.raises(bz.BenzingaNewsHTTPError) as excinfo:
        await bz.fetch_benzinga_news(
            "NVDA",
            api_key="SECRET-KEY-999",
            poll_id="poll-1",
            now=FIXED_NOW,
            client_factory=_client_factory({"oops": True}, status=500),
        )
    assert excinfo.value.status_code == 500
    assert "SECRET-KEY-999" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_fetch_error_row_fails_closed() -> None:
    with pytest.raises(bz.BenzingaNewsHTTPError):
        await bz.fetch_benzinga_news(
            "NVDA",
            api_key="KEY-123",
            poll_id="poll-1",
            now=FIXED_NOW,
            client_factory=_client_factory([{"error": "denied"}]),
        )


def test_redact_token_scrubs_query_material() -> None:
    dirty = "benzinga news HTTP 401 for 'NVDA'?token=SECRET-KEY-999&tickers=NVDA oops"
    clean = bz.redact_token(dirty)
    assert "SECRET-KEY-999" not in clean
    assert "token=***" in clean


def test_gate_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(bz.SHADOW_ENABLED_ENV, raising=False)
    assert bz.shadow_enabled() is False
    with pytest.raises(bz.BenzingaShadowDisabled):
        bz.require_enabled()


def test_gate_requires_exact_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(bz.SHADOW_ENABLED_ENV, "TRUE")
    assert bz.shadow_enabled() is False
    monkeypatch.setenv(bz.SHADOW_ENABLED_ENV, "true")
    assert bz.shadow_enabled() is True


def test_api_key_missing_never_echoes_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(bz.API_KEY_ENV, raising=False)
    with pytest.raises(bz.BenzingaApiKeyMissing) as excinfo:
        bz.read_api_key()
    assert bz.API_KEY_ENV in str(excinfo.value)


def test_normalize_url_keeps_identifying_query_but_strips_tracking() -> None:
    kept = bz.normalize_url("https://finnhub.io/api/news?id=abc123&utm_source=x")
    assert kept == "finnhub.io/api/news?id=abc123"
    assert bz.normalize_url("https://finnhub.io/api/news?id=abc123") == kept
    assert bz.normalize_url("https://finnhub.io/api/news?id=other") != kept
    assert bz.normalize_url("HTTPS://X.com/A/?gclid=1") == "x.com/A"
    assert bz.normalize_url("https://X.com/A/?utm_x=1") == "x.com/A"
    assert bz.normalize_url(None) is None
    assert bz.normalize_url("") is None


def test_finnhub_naive_local_time_is_converted_to_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-UTC host (Kiritimati, UTC+14) must not record local time as UTC."""
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    time.tzset()
    try:
        record = bz.normalize_finnhub_record(
            {
                "headline": "H",
                "url": "https://finnhub.io/api/news?id=h1",
                "source": "Finnhub",
                "datetime": "2025-11-25T14:00:00",
                "summary": "s",
            },
            poll_id="p1",
            first_received_at=FIXED_NOW,
        )
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()
    assert record["created_raw"] == "2025-11-25T14:00:00"
    assert record["created_basis"] == "finnhub_naive_interpreted_as_local"
    assert record["created_utc"] == "2025-11-25T00:00:00+00:00"


def _record(source: str, url: str | None, title: str, poll: str, seen: str) -> dict:
    return {
        "record_type": "article",
        "source": source,
        "provider_id": url or title,
        "query_symbol": "NVDA",
        "url": url,
        "title": title,
        "poll_id": poll,
        "first_received_at": seen,
    }


def _status(source: str, symbol: str, poll: str, status: str) -> dict:
    return {
        "record_type": "poll_status",
        "source": source,
        "query_symbol": symbol,
        "poll_id": poll,
        "status": status,
        "count": 0,
        "error_class": "BenzingaNewsHTTPError" if status == "error" else None,
        "first_received_at": "2025-11-25T14:00:00+00:00",
    }


def test_summarize_overlap_unique_and_latency() -> None:
    ben = [
        _record(
            "benzinga",
            "https://x.com/a?utm=x",
            "Shared story",
            "p1",
            "2025-11-25T14:00:10+00:00",
        ),
        _record(
            "benzinga",
            "https://x.com/only-b",
            "Benzinga only",
            "p1",
            "2025-11-25T14:00:10+00:00",
        ),
        _record(
            "benzinga",
            "https://x.com/only-b",
            "Benzinga only dup",
            "p1",
            "2025-11-25T14:00:11+00:00",
        ),
    ]
    # Same provider_id as the second row: a cross-symbol re-observation.
    ben[2]["provider_id"] = ben[1]["provider_id"]
    fin = [
        _record(
            "finnhub",
            "https://x.com/a",
            "Shared story ",
            "p1",
            "2025-11-25T14:00:00+00:00",
        ),
        _record(
            "finnhub",
            "https://y.com/only-f",
            "Finnhub only",
            "p1",
            "2025-11-25T14:00:00+00:00",
        ),
    ]
    summary = bz.summarize_shadow(ben, fin)
    assert summary["benzinga_count"] == 2
    assert summary["finnhub_count"] == 2
    assert summary["overlap_articles"] == 1
    assert summary["benzinga_unique"] == 1
    assert summary["finnhub_unique"] == 1
    assert summary["latency_pairs_same_poll"] == 1
    assert summary["latency_seconds"]["min"] == 10.0
    assert summary["latency_seconds"]["p50"] == 10.0
    assert summary["latency_seconds"]["max"] == 10.0
    assert summary["benzinga_duplicates_removed"] == 1
    assert summary["benzinga_duplicate_rate"] == pytest.approx(1 / 3)
    assert summary["finnhub_duplicate_rate"] == 0.0
    assert summary["unreliable_symbol_polls"] == []


def test_summarize_matches_by_title_across_different_urls() -> None:
    ben = [
        _record(
            "benzinga",
            "https://www.benzinga.com/news/25/11/1/x",
            "Same headline here",
            "p1",
            "2025-11-25T14:00:10+00:00",
        )
    ]
    fin = [
        _record(
            "finnhub",
            "https://finnhub.io/api/news?id=hash9",
            "Same  headline   here",
            "p1",
            "2025-11-25T14:00:00+00:00",
        )
    ]
    summary = bz.summarize_shadow(ben, fin)
    assert summary["overlap_articles"] == 1
    assert summary["benzinga_unique"] == 0
    assert summary["finnhub_unique"] == 0
    assert summary["latency_pairs_same_poll"] == 1


def test_summarize_excludes_unreliable_cells() -> None:
    ben = [
        _record(
            "benzinga",
            "https://x.com/a",
            "Shared",
            "p1",
            "2025-11-25T14:00:10+00:00",
        ),
        _status("benzinga", "NVDA", "p1", "error"),
    ]
    fin = [
        _record(
            "finnhub", "https://x.com/a", "Shared", "p1", "2025-11-25T14:00:00+00:00"
        ),
        _status("finnhub", "NVDA", "p1", "ok"),
    ]
    summary = bz.summarize_shadow(ben, fin)
    assert summary["overlap_articles"] == 0
    assert summary["benzinga_unique"] == 0
    assert summary["finnhub_unique"] == 0
    assert summary["unreliable_symbol_polls"] == [{"poll_id": "p1", "symbol": "NVDA"}]
    assert summary["excluded_unreliable_articles"] == {"benzinga": 1, "finnhub": 1}


def test_summarize_cross_poll_pairs_have_no_latency() -> None:
    ben = [
        _record(
            "benzinga", "https://x.com/a", "Shared", "p2", "2025-11-25T15:00:00+00:00"
        )
    ]
    fin = [
        _record(
            "finnhub", "https://x.com/a", "Shared", "p1", "2025-11-25T14:00:00+00:00"
        )
    ]
    summary = bz.summarize_shadow(ben, fin)
    assert summary["overlap_articles"] == 1
    assert summary["latency_pairs_same_poll"] == 0
    assert summary["latency_seconds"] == {"min": None, "p50": None, "max": None}


def test_normalize_url_ignores_scheme_query_and_case() -> None:
    assert bz.normalize_url("https://X.com/A/?utm=x") == bz.normalize_url(
        "http://x.com/A"
    )
    assert bz.normalize_url(None) is None
    assert bz.normalize_url("") is None

def test_normalize_record_missing_id_keeps_provider_id_none() -> None:
    item = dict(BENZINGA_ITEM)
    item.pop("id")
    record = bz.normalize_record(item, poll_id="p1", first_received_at=FIXED_NOW)
    assert record["provider_id"] is None


def test_dedupe_keeps_empty_provider_id_rows() -> None:
    rows = [
        _record("benzinga", "https://x.com/a", "First", "p1", "2025-11-25T14:00:00+00:00"),
        _record("benzinga", "https://x.com/b", "Second", "p1", "2025-11-25T14:00:00+00:00"),
    ]
    for row in rows:
        row["provider_id"] = None
    rows.append(
        _record("benzinga", "https://x.com/c", "Third", "p1", "2025-11-25T14:00:00+00:00")
    )
    rows[-1]["provider_id"] = ""
    kept, removed = bz._dedupe_articles(rows)
    assert len(kept) == 3
    assert removed == 0


def test_finnhub_naive_time_uses_offset_at_record_instant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Across a US DST switch the July record must use EDT, not November's EST."""
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        july, _ = bz._finnhub_created_utc("2025-07-01T12:00:00")
        january, _ = bz._finnhub_created_utc("2025-01-15T12:00:00")
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()
    assert july == "2025-07-01T16:00:00+00:00"
    assert january == "2025-01-15T17:00:00+00:00"


def test_summarize_killed_poll_is_unreliable() -> None:
    ben = [
        _record(
            "benzinga",
            "https://x.com/a",
            "Orphan story",
            "p-killed",
            "2025-11-25T14:00:00+00:00",
        )
    ]
    frames = [
        {
            "record_type": "poll_header",
            "poll_id": "p-killed",
            "symbols": ["NVDA", "AMD"],
        }
    ]
    summary = bz.summarize_shadow(ben, [], frames)
    assert summary["incomplete_polls"] == [
        {
            "poll_id": "p-killed",
            "reason": "no_completion_record",
            "symbols": ["AMD", "NVDA"],
        }
    ]
    assert summary["unreliable_symbol_polls"] == [
        {"poll_id": "p-killed", "symbol": "AMD"},
        {"poll_id": "p-killed", "symbol": "NVDA"},
    ]
    assert summary["benzinga_count"] == 0
    assert summary["excluded_unreliable_articles"] == {"benzinga": 1, "finnhub": 0}


def test_summarize_completed_poll_with_unpolled_symbol() -> None:
    frames = [
        {
            "record_type": "poll_header",
            "poll_id": "p-part",
            "symbols": ["NVDA", "AMD"],
        },
        {
            "record_type": "poll_completion",
            "poll_id": "p-part",
            "symbols_completed": ["NVDA"],
        },
    ]
    summary = bz.summarize_shadow([], [], frames)
    assert summary["incomplete_polls"] == [
        {"poll_id": "p-part", "reason": "symbols_unpolled", "symbols": ["AMD"]}
    ]
    assert summary["unreliable_symbol_polls"] == [
        {"poll_id": "p-part", "symbol": "AMD"}
    ]


def test_summarize_completed_poll_has_no_incomplete_entry() -> None:
    frames = [
        {"record_type": "poll_header", "poll_id": "p-ok", "symbols": ["NVDA"]},
        {
            "record_type": "poll_completion",
            "poll_id": "p-ok",
            "symbols_completed": ["NVDA"],
        },
    ]
    summary = bz.summarize_shadow([], [], frames)
    assert summary["incomplete_polls"] == []
    assert summary["unreliable_symbol_polls"] == []


def test_summarize_counts_benzinga_by_query_param() -> None:
    ben = [
        _record(
            "benzinga",
            "https://x.com/a",
            "Primary story",
            "p1",
            "2025-11-25T14:00:00+00:00",
        ),
        _record(
            "benzinga",
            "https://x.com/b",
            "Tickers story",
            "p1",
            "2025-11-25T14:00:10+00:00",
        ),
    ]
    ben[0]["query_param"] = "primaryTickers"
    ben[1]["query_param"] = "tickers"
    summary = bz.summarize_shadow(ben, [])
    assert summary["benzinga_by_query_param"] == {"primaryTickers": 1, "tickers": 1}
    assert summary["incomplete_polls"] == []
