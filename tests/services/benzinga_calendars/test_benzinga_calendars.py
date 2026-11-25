"""Pure tests for the Benzinga calendar client, normalizers and ingestion."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "benzinga_calendars"

OBSERVED = datetime(2026, 11, 25, 14, 0, tzinfo=UTC)


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


ROWS_BY_CATEGORY = {
    "earnings": load("earnings.json")["earnings"],
    "analyst_rating": load("ratings.json")["ratings"],
    "guidance": load("guidance.json")["guidance"],
    "dividend": load("dividends.json")["dividends"],
    "economic": load("economics.json")["economics"],
    "conference": load("conference_calls.json")["conference"],
    "short_interest": load("short_interest.json")["shortInterestData"]["AAPL"]["data"],
    "wiim": load("wiim.json"),
}


# -- normalizers ------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("category", sorted(ROWS_BY_CATEGORY))
def test_every_fixture_normalizes_to_a_benzinga_market_event(category):
    from app.services.benzinga_calendars.normalizers import normalize_row

    rows = ROWS_BY_CATEGORY[category]
    normalized = [
        normalize_row(category, row, first_received_at=OBSERVED, fetched_at=OBSERVED)
        for row in rows
    ]
    kept = [item for item in normalized if item is not None]
    assert kept, f"{category} fixture produced no in-scope rows"
    for item in kept:
        event = item.event
        assert event["source"] == "benzinga"
        assert event["category"] == category
        assert event["market"] == "us"
        assert event["first_received_at"] == OBSERVED
        assert event["fetched_at"] == OBSERVED
        assert event["source_event_id"].startswith(f"benzinga:{category}:")
        assert event["event_date"] is not None
        assert event["raw_payload_json"]


@pytest.mark.unit
def test_economics_normalizer_skips_non_us_rows():
    from app.services.benzinga_calendars.normalizers import normalize_row

    rows = ROWS_BY_CATEGORY["economic"]
    usa, deu = rows
    assert usa["country"] == "USA"
    assert deu["country"] == "DEU"
    kept = normalize_row(
        "economic", usa, first_received_at=OBSERVED, fetched_at=OBSERVED
    )
    skipped = normalize_row(
        "economic", deu, first_received_at=OBSERVED, fetched_at=OBSERVED
    )
    assert kept is not None and kept.event["title"] == "Challenger Job Cuts (YoY)"
    assert skipped is None


@pytest.mark.unit
def test_earnings_normalizer_keeps_provider_time_in_utc_and_raw():
    from app.services.benzinga_calendars.normalizers import normalize_row

    row = ROWS_BY_CATEGORY["earnings"][0]
    item = normalize_row(
        "earnings", row, first_received_at=OBSERVED, fetched_at=OBSERVED
    )
    assert item is not None
    event = item.event
    assert event["event_date"] == date(2026, 10, 29)
    assert event["status"] == "tentative"
    assert event["provider_updated_raw"] == "1766187011"
    assert event["provider_updated_at"] == datetime.fromtimestamp(1766187011, UTC)
    assert event["provider_created_at"] is None
    assert event["fiscal_year"] == 2026
    assert event["fiscal_quarter"] == 4
    eps = next(v for v in item.values if v["metric_name"] == "eps")
    assert eps["forecast"] == Decimal("1.990")
    assert eps["unit"] == "USD"


@pytest.mark.unit
def test_earnings_time_hint_uses_the_closed_vocabulary():
    from app.services.benzinga_calendars.normalizers import normalize_row

    after_close = ROWS_BY_CATEGORY["earnings"][1]
    item = normalize_row(
        "earnings", after_close, first_received_at=OBSERVED, fetched_at=OBSERVED
    )
    assert item is not None
    assert item.event["time_hint"] == "after_close"
    assert item.event["status"] == "released"

    unknown = normalize_row(
        "earnings",
        {**after_close, "time": "16:00:00"},
        first_received_at=OBSERVED,
        fetched_at=OBSERVED,
    )
    assert unknown is not None
    assert unknown.event["time_hint"] == "unknown"


@pytest.mark.unit
def test_rating_normalizer_keeps_old_and_new_price_target():
    from app.services.benzinga_calendars.normalizers import normalize_row

    row = ROWS_BY_CATEGORY["analyst_rating"][0]
    item = normalize_row(
        "analyst_rating", row, first_received_at=OBSERVED, fetched_at=OBSERVED
    )
    assert item is not None
    assert item.event["symbol"] == "MNDY"
    assert "Jefferies" in item.event["title"]
    target = next(v for v in item.values if v["metric_name"] == "price_target")
    assert target["actual"] == Decimal("260.0000")
    assert target["previous"] == Decimal("300.00")


@pytest.mark.unit
def test_wiim_keeps_rfc2822_times_as_given_and_as_utc():
    from app.services.benzinga_calendars.normalizers import normalize_row

    row = ROWS_BY_CATEGORY["wiim"][0]
    item = normalize_row("wiim", row, first_received_at=OBSERVED, fetched_at=OBSERVED)
    assert item is not None
    event = item.event
    assert event["provider_created_raw"] == "Mon, 01 Jan 2024 13:35:14 -0400"
    assert event["provider_created_at"] == datetime(2024, 1, 1, 17, 35, 14, tzinfo=UTC)
    assert event["event_date"] == date(2024, 1, 1)
    assert event["symbol"] == "AAPL"


@pytest.mark.unit
def test_content_hash_is_stable_and_changes_with_the_provider_version():
    from app.services.benzinga_calendars.normalizers import content_hash

    row = ROWS_BY_CATEGORY["analyst_rating"][0]
    assert content_hash(row) == content_hash(dict(row))
    revised = {**row, "updated": row["updated"] + 60, "pt_current": "270.0000"}
    assert content_hash(revised) != content_hash(row)


@pytest.mark.unit
def test_long_provider_text_is_bounded():
    from app.services.benzinga_calendars.normalizers import (
        MAX_RAW_STRING,
        normalize_row,
    )

    row = {
        **ROWS_BY_CATEGORY["wiim"][0],
        "body": "x" * (MAX_RAW_STRING + 500),
    }
    item = normalize_row("wiim", row, first_received_at=OBSERVED, fetched_at=OBSERVED)
    assert item is not None
    stored = item.event["raw_payload_json"]["body"]
    assert len(stored) < len(row["body"])
    assert stored.endswith("(truncated)")


@pytest.mark.unit
def test_malformed_rows_raise_instead_of_writing_a_partial_row():
    from app.services.benzinga_calendars.normalizers import normalize_row

    with pytest.raises(ValueError):
        normalize_row(
            "earnings",
            {"ticker": "AAPL"},
            first_received_at=OBSERVED,
            fetched_at=OBSERVED,
        )
    with pytest.raises(ValueError):
        normalize_row(
            "unknown_category", {}, first_received_at=OBSERVED, fetched_at=OBSERVED
        )


# -- client -----------------------------------------------------------------


class _FakeResponse:
    def __init__(
        self, payload, *, status_code: int = 200, content_type: str = "application/json"
    ):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHTTP:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, params=None):
        self.requests.append({"url": url, "params": dict(params or {})})
        return self._responses.pop(0)


def _client(responses, **kwargs):
    from app.services.benzinga_calendars.client import BenzingaCalendarClient

    fake_http = _FakeHTTP(responses)
    client = BenzingaCalendarClient(
        api_key="secret-key-value",
        client_factory=lambda timeout: fake_http,
        **kwargs,
    )
    return client, fake_http


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_sends_token_in_query_and_returns_rows():
    rows = ROWS_BY_CATEGORY["earnings"]
    client, http = _client(
        [_FakeResponse({"earnings": rows}), _FakeResponse({"earnings": []})]
    )
    got = await client.fetch(
        "earnings", from_date=date(2026, 10, 1), to_date=date(2026, 10, 31)
    )
    assert got == rows
    request = http.requests[0]
    assert request["params"]["token"] == "secret-key-value"
    assert request["params"]["parameters[date_from]"] == "2026-10-01"
    assert request["params"]["pagesize"] == 1000
    assert "secret-key-value" not in request["url"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_uses_endpoint_specific_pagination_names():
    client, http = _client([_FakeResponse(ROWS_BY_CATEGORY["wiim"]), _FakeResponse([])])
    await client.fetch("wiim", from_date=date(2024, 1, 1), to_date=date(2024, 1, 2))
    params = http.requests[0]["params"]
    assert params["channels"] == "WIIM"
    assert params["pageSize"] == 100
    assert params["dateFrom"] == "2024-01-01"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_flattens_nested_short_interest_map():
    payload = load("short_interest.json")
    client, _ = _client(
        [_FakeResponse(payload), _FakeResponse({"shortInterestData": {}})]
    )
    rows = await client.fetch(
        "short_interest",
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 31),
        symbols="AAPL",
    )
    assert [row["symbol"] for row in rows] == ["AAPL"]
    assert rows[0]["recordDate"] == "2026-01-12"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_short_interest_requires_symbols_before_any_request():
    from app.services.benzinga_http import BenzingaContractError

    client, http = _client([])
    with pytest.raises(BenzingaContractError):
        await client.fetch(
            "short_interest",
            from_date=date(2026, 1, 1),
            to_date=date(2026, 1, 31),
        )
    assert http.requests == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_error_envelope_fails_closed():
    """A 200 body carrying ``ok=false`` stays a failure.

    Only the documented **404** no-data answer is an empty page; the same code
    inside a 200 body is not that answer.
    """
    from app.services.benzinga_http import BenzingaContractError

    client, _ = _client(
        [
            _FakeResponse(
                {
                    "ok": False,
                    "errors": [
                        {
                            "code": "no_data_found",
                            "id": "not_found",
                            "value": "No data found",
                        }
                    ],
                }
            )
        ]
    )
    with pytest.raises(BenzingaContractError):
        await client.fetch(
            "earnings", from_date=date(2026, 10, 1), to_date=date(2026, 10, 31)
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_full_last_page_at_the_cap_is_not_read_as_complete():
    from app.services.benzinga_http import BenzingaPaginationCapExceeded

    full_page = ROWS_BY_CATEGORY["wiim"] * 100  # 100 rows = page size
    client, http = _client([_FakeResponse(full_page)], max_pages=1)
    with pytest.raises(BenzingaPaginationCapExceeded):
        await client.fetch("wiim", from_date=date(2024, 1, 1), to_date=date(2024, 1, 2))
    assert len(http.requests) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_error_message_never_carries_the_key():
    from app.services.benzinga_http import BenzingaHTTPError

    client, _ = _client([_FakeResponse({"ok": False}, status_code=401)])
    with pytest.raises(BenzingaHTTPError) as excinfo:
        await client.fetch(
            "earnings", from_date=date(2026, 10, 1), to_date=date(2026, 10, 31)
        )
    assert "secret-key-value" not in str(excinfo.value)
    assert excinfo.value.status_code == 401


# -- ingestion idempotency / revisions --------------------------------------


class FakeRepo:
    def __init__(self) -> None:
        self.partitions: dict[tuple, SimpleNamespace] = {}
        self.revisions: dict[tuple, list[SimpleNamespace]] = {}
        self.events: list[dict] = []
        self.fail_append = False

    async def list_succeeded_partitions_in_range(self, **_kwargs):
        return {
            key[1]
            for key, partition in self.partitions.items()
            if partition.status == "succeeded"
        }

    async def get_or_create_partition(
        self, *, source, category, market, partition_date
    ):
        key = (category, partition_date)
        return self.partitions.setdefault(
            key,
            SimpleNamespace(
                source=source,
                category=category,
                market=market,
                partition_date=partition_date,
                status="pending",
                event_count=0,
                error=None,
            ),
        )

    async def mark_partition_running(self, partition):
        partition.status = "running"

    async def mark_partition_succeeded(self, partition, *, event_count):
        partition.status = "succeeded"
        partition.event_count = event_count

    async def mark_partition_pending(self, partition):
        partition.status = "pending"
        partition.event_count = 0

    async def mark_partition_failed(self, partition, *, error):
        partition.status = "failed"
        partition.error = error

    async def latest_revision(self, *, source, category, market, source_event_id):
        rows = self.revisions.get((category, source_event_id), [])
        return rows[-1] if rows else None

    async def append_revision(
        self,
        *,
        source,
        category,
        market,
        source_event_id,
        content_hash,
        observed_at,
        provider_created_at=None,
        provider_updated_at=None,
        raw_payload_json=None,
    ):
        if self.fail_append:
            return False
        rows = self.revisions.setdefault((category, source_event_id), [])
        rows.append(
            SimpleNamespace(
                revision_seq=len(rows) + 1,
                content_hash=content_hash,
                observed_at=observed_at,
                raw_payload_json=raw_payload_json,
            )
        )
        return True

    async def upsert_event_with_values(
        self, event_data, values, *, replace_values=False
    ):
        self.events.append(
            {
                "event": dict(event_data),
                "values": list(values),
                "replace_values": replace_values,
            }
        )
        return SimpleNamespace(id=len(self.events))


class FakeClient:
    def __init__(self, rows_by_category: dict[str, list[dict]]) -> None:
        self.rows_by_category = rows_by_category
        self.calls: list[tuple] = []

    async def fetch(self, category, *, from_date, to_date, symbols=None):
        self.calls.append((category, from_date, to_date, symbols))
        return self.rows_by_category[category]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reingest_of_the_same_payload_adds_nothing():
    from app.services.benzinga_calendars.ingestion import (
        ingest_benzinga_category_for_range,
    )

    repo = FakeRepo()
    client = FakeClient({"earnings": ROWS_BY_CATEGORY["earnings"]})
    window = (date(2026, 10, 29), date(2026, 10, 29))

    first = await ingest_benzinga_category_for_range(
        None,
        "earnings",
        *window,
        client=client,
        repo=repo,
        fetched_at=OBSERVED,
        skip_succeeded=False,
    )
    assert [r.status for r in first] == ["succeeded"]
    assert (
        len(repo.revisions[("earnings", "benzinga:earnings:69030cfb619d3a00015b72a3")])
        == 1
    )
    assert len(repo.events) == 1

    second = await ingest_benzinga_category_for_range(
        None,
        "earnings",
        *window,
        client=client,
        repo=repo,
        fetched_at=OBSERVED,
        skip_succeeded=False,
    )
    assert [r.status for r in second] == ["succeeded"]
    # Same payload: the revision log and the event row are untouched.
    assert (
        len(repo.revisions[("earnings", "benzinga:earnings:69030cfb619d3a00015b72a3")])
        == 1
    )
    assert len(repo.events) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_revised_rating_is_a_revision_not_an_overwrite():
    from app.services.benzinga_calendars.ingestion import (
        ingest_benzinga_category_for_range,
    )

    repo = FakeRepo()
    original = ROWS_BY_CATEGORY["analyst_rating"][0]
    revised = {
        **original,
        "updated": original["updated"] + 3600,
        "pt_current": "270.0000",
    }
    window = (date(2026, 1, 5), date(2026, 1, 5))

    await ingest_benzinga_category_for_range(
        None,
        "analyst_rating",
        *window,
        client=FakeClient({"analyst_rating": [original]}),
        repo=repo,
        fetched_at=OBSERVED,
        skip_succeeded=False,
    )
    await ingest_benzinga_category_for_range(
        None,
        "analyst_rating",
        *window,
        client=FakeClient({"analyst_rating": [revised]}),
        repo=repo,
        fetched_at=OBSERVED,
        skip_succeeded=False,
    )

    key = ("analyst_rating", "benzinga:analyst_rating:695c16678f047b0001fee512")
    history = repo.revisions[key]
    assert [row.revision_seq for row in history] == [1, 2]
    assert history[0].raw_payload_json["pt_current"] == "260.0000"
    assert history[1].raw_payload_json["pt_current"] == "270.0000"
    # The current row carries the newer value; the old one survives as history.
    assert repo.events[-1]["event"]["raw_payload_json"]["pt_current"] == "270.0000"
    target = next(
        v for v in repo.events[-1]["values"] if v["metric_name"] == "price_target"
    )
    assert target["actual"] == Decimal("270.0000")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_succeeded_partitions_are_skipped_without_a_fetch():
    from app.services.benzinga_calendars.ingestion import (
        ingest_benzinga_category_for_range,
    )

    repo = FakeRepo()
    client = FakeClient({"earnings": ROWS_BY_CATEGORY["earnings"]})
    window = (date(2026, 10, 29), date(2026, 10, 29))
    await ingest_benzinga_category_for_range(
        None,
        "earnings",
        *window,
        client=client,
        repo=repo,
        fetched_at=OBSERVED,
        skip_succeeded=True,
    )
    results = await ingest_benzinga_category_for_range(
        None,
        "earnings",
        *window,
        client=client,
        repo=repo,
        fetched_at=OBSERVED,
        skip_succeeded=True,
    )
    assert results == []
    assert len(client.calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_failure_marks_every_partition_failed_and_reraises():
    from app.services.benzinga_calendars.ingestion import (
        ingest_benzinga_category_for_range,
    )
    from app.services.benzinga_http import BenzingaHTTPError

    repo = FakeRepo()

    class FailingClient:
        async def fetch(self, *_args, **_kwargs):
            raise BenzingaHTTPError("benzinga HTTP 500 (path /x)", status_code=500)

    with pytest.raises(BenzingaHTTPError):
        await ingest_benzinga_category_for_range(
            None,
            "earnings",
            date(2026, 10, 29),
            date(2026, 10, 30),
            client=FailingClient(),
            repo=repo,
            fetched_at=OBSERVED,
        )
    assert {p.status for p in repo.partitions.values()} == {"failed"}


# -- comparison -------------------------------------------------------------


@pytest.mark.unit
def test_compare_earnings_reports_coverage_and_time_only_on_co_observed():
    from app.services.benzinga_calendars.compare import compare_earnings

    finnhub = [
        {"symbol": "AAPL", "date": "2026-10-29", "hour": "amc"},
        {"symbol": "MSFT", "date": "2026-11-04", "hour": "amc"},
    ]
    benzinga = [
        {"symbol": "AAPL", "date": "2026-10-29", "time_hint": "after_close"},
        {"symbol": "NVDA", "date": "2026-11-19", "time_hint": "after_close"},
    ]
    report = compare_earnings(finnhub, benzinga)
    assert report["finnhub"]["items"] == 2
    assert report["benzinga"]["items"] == 2
    assert report["coverage"]["both_symbols"] == ["AAPL"]
    assert report["coverage"]["only_benzinga_symbols"] == ["NVDA"]
    assert report["coverage"]["only_finnhub_symbols"] == ["MSFT"]
    assert report["only_benzinga_items"] == 1
    assert report["only_finnhub_items"] == 1
    assert report["co_observed_items"] == 1
    # Only the co-observed (AAPL, 2026-10-29) pair is compared for time.
    assert report["time_agreement"] == {"agree": 1, "differ": 0, "unknown_either": 0}
    assert report["time_differences"] == []


@pytest.mark.unit
def test_compare_earnings_reports_time_differences_and_unknowns():
    from app.services.benzinga_calendars.compare import compare_earnings

    report = compare_earnings(
        [
            {"symbol": "AAPL", "date": "2026-10-29", "hour": "bmo"},
            {"symbol": "MSFT", "date": "2026-11-04", "hour": ""},
        ],
        [
            {"symbol": "AAPL", "date": "2026-10-29", "time_hint": "after_close"},
            {"symbol": "MSFT", "date": "2026-11-04", "time_hint": "after_close"},
        ],
    )
    assert report["time_agreement"]["differ"] == 1
    assert report["time_agreement"]["unknown_either"] == 1
    assert report["time_differences"][0]["symbol"] == "AAPL"


# -- registry ---------------------------------------------------------------


@pytest.mark.unit
def test_endpoint_registry_matches_the_taxonomy():
    from app.services.benzinga_calendars import spec
    from app.services.market_events import taxonomy

    spec.validate_registry()
    assert set(spec.CALENDAR_ENDPOINTS) == {
        "earnings",
        "analyst_rating",
        "guidance",
        "dividend",
        "economic",
        "conference",
        "short_interest",
        "wiim",
    }
    assert set(spec.CALENDAR_ENDPOINTS) <= taxonomy.CATEGORIES
    assert "benzinga" in taxonomy.SOURCES


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_page_size_cap_is_read_to_exhaustion():
    """A silent provider cap below the requested pagesize must not truncate.

    The endpoint is asked for 1000 rows; the provider returns 100 per page.
    Three non-empty pages + an empty page = all 250 rows.
    """
    row = ROWS_BY_CATEGORY["earnings"][0]
    pages = [
        _FakeResponse({"earnings": [row] * 100}),
        _FakeResponse({"earnings": [row] * 100}),
        _FakeResponse({"earnings": [row] * 50}),
        _FakeResponse({"earnings": []}),
    ]
    client, http = _client(pages)
    rows = await client.fetch(
        "earnings", from_date=date(2026, 10, 1), to_date=date(2026, 10, 31)
    )
    assert len(rows) == 250
    assert [request["params"]["page"] for request in http.requests] == [0, 1, 2, 3]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_revision_that_reverts_to_earlier_content_is_appended():
    """A → B → A is three revisions; the newest row must describe A."""
    from app.services.benzinga_calendars.ingestion import (
        ingest_benzinga_category_for_range,
    )

    repo = FakeRepo()
    original = ROWS_BY_CATEGORY["analyst_rating"][0]
    changed = {
        **original,
        "updated": original["updated"] + 60,
        "pt_current": "270.0000",
    }
    window = (date(2026, 1, 5), date(2026, 1, 5))
    key = ("analyst_rating", "benzinga:analyst_rating:695c16678f047b0001fee512")

    for payload in (original, changed, original):
        await ingest_benzinga_category_for_range(
            None,
            "analyst_rating",
            *window,
            client=FakeClient({"analyst_rating": [payload]}),
            repo=repo,
            fetched_at=OBSERVED,
            skip_succeeded=False,
        )

    history = repo.revisions[key]
    assert [row.revision_seq for row in history] == [1, 2, 3]
    assert history[-1].raw_payload_json["pt_current"] == "260.0000"
    assert repo.events[-1]["event"]["raw_payload_json"]["pt_current"] == "260.0000"

    # The newest revision now matches the stored content, so a fourth read of
    # the same payload writes nothing.
    await ingest_benzinga_category_for_range(
        None,
        "analyst_rating",
        *window,
        client=FakeClient({"analyst_rating": [original]}),
        repo=repo,
        fetched_at=OBSERVED,
        skip_succeeded=False,
    )
    assert len(repo.revisions[key]) == 3
    assert len(repo.events) == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unrecorded_revision_fails_the_partition_instead_of_updating():
    from app.services.benzinga_calendars.ingestion import (
        ingest_benzinga_category_for_range,
    )

    repo = FakeRepo()
    repo.fail_append = True
    results = await ingest_benzinga_category_for_range(
        None,
        "earnings",
        date(2026, 10, 29),
        date(2026, 10, 29),
        client=FakeClient({"earnings": ROWS_BY_CATEGORY["earnings"]}),
        repo=repo,
        fetched_at=OBSERVED,
        skip_succeeded=False,
    )
    assert [result.status for result in results] == ["failed"]
    assert repo.events == []
    assert {p.status for p in repo.partitions.values()} == {"failed"}


@pytest.mark.unit
def test_wiim_event_date_uses_eastern_time():
    from app.services.benzinga_calendars.normalizers import normalize_row

    after_hours = {
        **ROWS_BY_CATEGORY["wiim"][0],
        "created": "Tue, 02 Jan 2024 21:30:00 -0400",
        "updated": "Tue, 02 Jan 2024 21:30:00 -0400",
    }
    item = normalize_row(
        "wiim", after_hours, first_received_at=OBSERVED, fetched_at=OBSERVED
    )
    assert item is not None
    # 01:30 UTC on Jan 3, but still Jan 2 in New York — the partition day.
    assert item.event["provider_created_at"] == datetime(2024, 1, 3, 1, 30, tzinfo=UTC)
    assert item.event["event_date"] == date(2024, 1, 2)


# -- round 6 findings -------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ok_false_fails_the_page_even_with_the_row_key():
    """``{"ok": false, "earnings": []}`` is an error, not an empty day.

    The row-key exemption used to let it through, so the read looked complete
    and the partition was marked succeeded.
    """
    from app.services.benzinga_http import BenzingaContractError

    client, http = _client([_FakeResponse({"ok": False, "earnings": []})])
    with pytest.raises(BenzingaContractError):
        await client.fetch(
            "earnings", from_date=date(2026, 10, 1), to_date=date(2026, 10, 31)
        )
    assert len(http.requests) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_row_list_without_an_error_flag_ends_the_read():
    """A valid ``{"earnings": []}`` is still the end-of-read signal."""
    client, _ = _client([_FakeResponse({"earnings": []})])
    rows = await client.fetch(
        "earnings", from_date=date(2026, 10, 1), to_date=date(2026, 10, 31)
    )
    assert rows == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_symbol_filtered_read_does_not_complete_the_partition():
    """A subset read must not satisfy the partition for other symbols.

    AAPL-only used to mark the day ``succeeded``, so a later MSFT-only read was
    skipped before its fetch and an unfiltered read skipped the incomplete day.
    """
    from app.services.benzinga_calendars.ingestion import (
        ingest_benzinga_category_for_range,
    )

    repo = FakeRepo()
    client = FakeClient({"earnings": ROWS_BY_CATEGORY["earnings"]})
    day = date(2026, 10, 29)

    filtered = await ingest_benzinga_category_for_range(
        None,
        "earnings",
        day,
        day,
        symbols="AAPL",
        client=client,
        repo=repo,
        fetched_at=OBSERVED,
        skip_succeeded=True,
    )
    assert [result.status for result in filtered] == ["pending"]
    assert repo.partitions[("earnings", day)].status == "pending"

    # The next unfiltered read still fetches the day instead of skipping it.
    full = await ingest_benzinga_category_for_range(
        None,
        "earnings",
        day,
        day,
        client=client,
        repo=repo,
        fetched_at=OBSERVED,
        skip_succeeded=True,
    )
    assert [result.status for result in full] == ["succeeded"]
    assert [call[3] for call in client.calls] == ["AAPL", None]

    # A forced filtered refetch never downgrades a completed day.
    await ingest_benzinga_category_for_range(
        None,
        "earnings",
        day,
        day,
        symbols="AAPL",
        client=client,
        repo=repo,
        fetched_at=OBSERVED,
        skip_succeeded=False,
    )
    assert repo.partitions[("earnings", day)].status == "succeeded"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_observation_clock_starts_when_the_fetch_returns(monkeypatch):
    """``first_received_at`` is our clock at receipt, not at request time."""
    from app.services.benzinga_calendars import ingestion

    early = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)
    late = datetime(2026, 11, 25, 15, 1, tzinfo=UTC)

    class _Clock:
        after_fetch = False

        @classmethod
        def now(cls, tz=None):
            return late if cls.after_fetch else early

    class _SlowClient:
        async def fetch(self, category, *, from_date, to_date, symbols=None):
            _Clock.after_fetch = True
            return ROWS_BY_CATEGORY[category]

    monkeypatch.setattr(ingestion, "datetime", _Clock)

    repo = FakeRepo()
    day = date(2026, 10, 29)
    await ingestion.ingest_benzinga_category_for_range(
        None,
        "earnings",
        day,
        day,
        client=_SlowClient(),
        repo=repo,
        skip_succeeded=False,
    )
    revision = repo.revisions[
        ("earnings", "benzinga:earnings:69030cfb619d3a00015b72a3")
    ][0]
    assert revision.observed_at == late
    assert repo.events[0]["event"]["first_received_at"] == late


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingestion_replaces_the_superseded_value_set():
    """The provider row is the complete record, so values are replaced."""
    from app.services.benzinga_calendars.ingestion import (
        ingest_benzinga_category_for_range,
    )

    repo = FakeRepo()
    day = date(2026, 10, 29)
    await ingest_benzinga_category_for_range(
        None,
        "earnings",
        day,
        day,
        client=FakeClient({"earnings": ROWS_BY_CATEGORY["earnings"]}),
        repo=repo,
        fetched_at=OBSERVED,
        skip_succeeded=False,
    )
    assert repo.events[0]["replace_values"] is True


# -- documented "no data" answer --------------------------------------------

_NO_DATA_404 = {
    "ok": False,
    "errors": [
        {
            "code": "no_data_found",
            "id": "not_found",
            "value": "No data found for the specified parameters",
        }
    ],
}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_documented_no_data_404_on_page_zero_is_an_empty_window():
    """Benzinga answers "no rows" with 404 + no_data_found; that is not a failure.

    Failing it would fail every quiet day (no WIIM at the first tick, no
    conference calls) and, under ``--force``, flip succeeded partitions to
    failed.
    """
    client, http = _client([_FakeResponse(_NO_DATA_404, status_code=404)])
    rows = await client.fetch(
        "earnings", from_date=date(2026, 10, 1), to_date=date(2026, 10, 31)
    )
    assert rows == []
    assert len(http.requests) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_documented_no_data_404_after_data_ends_the_read():
    """The same answer on the page past the last one keeps the collected rows."""
    rows = ROWS_BY_CATEGORY["earnings"]
    client, http = _client(
        [
            _FakeResponse({"earnings": rows}),
            _FakeResponse(_NO_DATA_404, status_code=404),
        ]
    )
    fetched = await client.fetch(
        "earnings", from_date=date(2026, 10, 1), to_date=date(2026, 10, 31)
    )
    assert fetched == rows
    assert len(http.requests) == 2


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"ok": False, "errors": [{"code": "invalid_token"}]},
        {"ok": False, "errors": [{"code": "no_data_found"}, {"code": "other"}]},
        {"ok": True, "errors": []},
        {"message": "not found"},
        ["not", "an", "object"],
    ],
)
async def test_other_404_bodies_stay_failures(payload):
    from app.services.benzinga_http import BenzingaHTTPError

    client, _ = _client([_FakeResponse(payload, status_code=404)])
    with pytest.raises(BenzingaHTTPError):
        await client.fetch(
            "earnings", from_date=date(2026, 10, 1), to_date=date(2026, 10, 31)
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_window_is_a_succeeded_partition_with_zero_rows():
    """An empty window is a completed read, not a failed one."""
    from app.services.benzinga_calendars.ingestion import (
        ingest_benzinga_category_for_range,
    )

    repo = FakeRepo()
    day = date(2026, 10, 29)
    results = await ingest_benzinga_category_for_range(
        None,
        "earnings",
        day,
        day,
        client=FakeClient({"earnings": []}),
        repo=repo,
        fetched_at=OBSERVED,
        skip_succeeded=False,
    )
    assert [(result.status, result.event_count) for result in results] == [
        ("succeeded", 0)
    ]
    assert repo.partitions[("earnings", day)].status == "succeeded"
    assert repo.partitions[("earnings", day)].event_count == 0
