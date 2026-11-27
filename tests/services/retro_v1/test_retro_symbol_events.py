"""``retro_symbol_events``: the knowledge cutoff, versions, bars and gaps (hk 6410)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.models.market_events import MarketEvent
from app.services.market_events.repository import CollectionFailure
from app.services.retro_v1 import RetroReadError, read_retro_symbol_events
from app.services.retro_v1 import retro_symbol_events as rse
from app.services.retro_v1.market import resolve_market
from app.services.retro_v1.symbol_event_versions import (
    INSERT_ONLY_SOURCES,
    REVISIONED_SOURCES,
    SOURCES_BY_MARKET,
)
from tests.services.retro_v1.support import operational_error, validate
from tests.services.retro_v1.symbol_events_support import (
    AFTER,
    BEFORE,
    CUTOFF,
    DAY,
    bar,
    benzinga_event,
    benzinga_row,
    event,
    latest,
    notice_event,
    partition,
    revision,
    session,
)

pytestmark = pytest.mark.unit

KR = resolve_market("kr")
US = resolve_market("us")
CRYPTO = resolve_market("crypto")
SCHEMA = "RetroSymbolEventsResponse"


async def read(fake: Any, **kw: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "symbol": "005930",
        "known_cutoff": "2026-11-23T00:00:00Z",
        "from_": DAY,
        "to": DAY,
    }
    params.update(kw)
    response = await read_retro_symbol_events(fake, **params)
    validate(response, SCHEMA)
    return response


def events_of(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in response["data"] if r["row_type"] == "event"]


def bars_of(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in response["data"] if r["row_type"] == "bar"]


def unknown_pairs(response: dict[str, Any]) -> set[tuple[str, str]]:
    return {(u["field"], u["reason"]) for u in response["unknowns"]}


# --------------------------------------------------------------------------
# the knowledge cutoff
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_as_of_is_generation_time_and_the_cutoff_is_a_separate_echo() -> None:
    started = datetime.now(UTC)
    response = await read(session(events=[event(1)], bars=[bar()]))
    generated = datetime.fromisoformat(response["as_of"].replace("Z", "+00:00"))
    assert generated >= started
    assert response["known_cutoff"] == "2026-11-23T00:00:00Z"
    assert response["as_of"] != response["known_cutoff"]
    assert response["total"] == 2
    assert response["excluded_after_cutoff"] == {"events": 0, "bars": 0}
    assert response["excluded_unproven"] == {"events": 0, "bars": 0}


@pytest.mark.asyncio
async def test_rows_received_after_the_cutoff_are_only_counted() -> None:
    late = event(2, first_received_at=AFTER, updated_at=AFTER)
    exactly = event(3, first_received_at=CUTOFF, updated_at=CUTOFF)
    response = await read(
        session(
            events=[event(1), late, exactly],
            bars=[bar(venue="KRX"), bar(venue="NTX", stored_at=AFTER, close="999999")],
        )
    )
    shown = events_of(response)
    # first_received_at == cutoff is included (<=)
    assert [r["first_received_at"] for r in shown] == [
        "2026-11-21T00:00:00Z",
        "2026-11-23T00:00:00Z",
    ]
    assert response["excluded_after_cutoff"] == {"events": 1, "bars": 1}
    assert [r["venue"] for r in bars_of(response)] == ["KRX"]
    # nothing about the excluded rows leaves the reader, only the numbers
    body = json.dumps(response)
    assert late.source_event_id not in body
    assert rse.event_ref(late.event_uuid) not in body
    assert "999999" not in body
    assert response["total"] == 3


@pytest.mark.asyncio
async def test_a_row_without_a_receipt_time_is_unproven_not_zero_and_not_shown() -> (
    None
):
    legacy = event(2, first_received_at=None)
    response = await read(session(events=[event(1), legacy]))
    assert len(events_of(response)) == 1
    assert response["excluded_unproven"]["events"] == 1
    assert response["excluded_after_cutoff"]["events"] == 0
    assert ("events.unproven", "incomplete_evidence") in unknown_pairs(response)


@pytest.mark.asyncio
async def test_events_outside_the_window_are_neither_shown_nor_counted() -> None:
    outside = DAY - timedelta(days=3)
    rows = [
        event(1, event_date=outside),
        event(2, event_date=outside, first_received_at=AFTER, updated_at=AFTER),
        event(3, event_date=outside, first_received_at=None),
    ]
    response = await read(session(events=rows))
    assert events_of(response) == []
    assert response["excluded_after_cutoff"]["events"] == 0
    assert response["excluded_unproven"]["events"] == 0


# --------------------------------------------------------------------------
# versions
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_superseded_event_shows_the_revision_known_at_the_cutoff() -> None:
    current = benzinga_event(
        1,
        status="released",
        event_date=date(2026, 11, 24),
        updated_at=AFTER,
        revised_at=AFTER,
        relevance="direct",
        source_url="https://www.benzinga.com/later-version",
    )
    log = [
        revision(
            current, 1, BEFORE - timedelta(days=1), benzinga_row(date_confirmed=0)
        ),
        revision(current, 2, BEFORE, benzinga_row()),
        revision(current, 3, AFTER, benzinga_row(date="2026-11-24", eps="9.99")),
    ]
    response = await read(session(events=[current], revisions=log), symbol="NVDA")
    (row,) = events_of(response)
    # revision 2, not revision 1 and not the current row
    assert row["version_seq"] == 2
    assert row["status"] == "scheduled"
    assert row["date"] == "2026-11-20"
    assert row["superseded_later"] is True
    assert row["version_state"] == "as_of_revision"
    assert row["reason"] == "reconstructed_from_revision"
    # values only the current (later) row holds are not carried over
    assert row["revised_at"] is None
    assert row["relevance"] is None
    body = json.dumps(response)
    for later in ("2026-11-24", "9.99", "released", "later-version"):
        assert later not in body, later


@pytest.mark.asyncio
async def test_without_a_later_revision_the_current_row_is_the_version() -> None:
    current = benzinga_event(1, status="released", relevance="direct")
    log = [revision(current, 1, BEFORE, benzinga_row(eps="1.30"))]
    response = await read(session(events=[current], revisions=log), symbol="NVDA")
    (row,) = events_of(response)
    assert (row["status"], row["version_seq"]) == ("released", 1)
    assert row["superseded_later"] is False
    assert row["relevance"] == "direct"
    assert row["symbols"] == ["NVDA"]
    assert row["reason"] is None
    # a value the source never recorded is a named unknown
    assert ("data.0.effective_at", "missing_data") in unknown_pairs(response)


@pytest.mark.asyncio
async def test_a_version_that_moved_into_the_window_is_dated_as_it_was_known() -> None:
    # current row: moved out of the window by a later revision
    current = benzinga_event(1, event_date=date(2026, 12, 24), updated_at=AFTER)
    log = [
        revision(current, 1, BEFORE, benzinga_row()),
        revision(current, 2, AFTER, benzinga_row(date="2026-12-24")),
    ]
    response = await read(session(events=[current], revisions=log), symbol="NVDA")
    assert [r["date"] for r in events_of(response)] == ["2026-11-20"]
    # and the reverse: a version dated elsewhere at the cutoff is not in this window
    moved_in = benzinga_event(2, event_date=DAY, updated_at=AFTER)
    log = [
        revision(moved_in, 1, BEFORE, benzinga_row(date="2026-12-24")),
        revision(moved_in, 2, AFTER, benzinga_row()),
    ]
    response = await read(session(events=[moved_in], revisions=log), symbol="NVDA")
    assert events_of(response) == []


@pytest.mark.asyncio
async def test_no_revision_by_the_cutoff_is_unproven() -> None:
    current = benzinga_event(1)
    log = [revision(current, 1, AFTER, benzinga_row())]
    response = await read(session(events=[current], revisions=log), symbol="NVDA")
    assert events_of(response) == []
    assert response["excluded_unproven"]["events"] == 1


@pytest.mark.parametrize(
    "then",
    [
        pytest.param(benzinga_row(ticker="AMD"), id="linked_to_another_symbol_then"),
        pytest.param({"id": "bz-1"}, id="payload_cannot_be_rebuilt"),
        pytest.param(None, id="no_stored_payload"),
    ],
)
@pytest.mark.asyncio
async def test_a_superseded_version_that_cannot_be_proven_is_withheld(
    then: dict[str, Any] | None,
) -> None:
    current = benzinga_event(1, updated_at=AFTER)
    log = [
        revision(current, 1, BEFORE, then),
        revision(current, 2, AFTER, benzinga_row()),
    ]
    response = await read(session(events=[current], revisions=log), symbol="NVDA")
    assert events_of(response) == []
    assert response["excluded_unproven"]["events"] == 1


@pytest.mark.asyncio
async def test_a_superseded_notice_is_rebuilt_from_its_revision() -> None:
    created = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)
    current = notice_event(1, updated_at=AFTER)
    log = [
        revision(
            current,
            1,
            BEFORE,
            {"title": "거래유의종목 지정 (XYZ)"},
            provider_created_at=created,
        ),
        revision(
            current,
            2,
            AFTER,
            {"title": "거래유의종목 해제 (XYZ)"},
            provider_created_at=AFTER,
        ),
    ]
    fake = session(
        events=[current], revisions=log, bar_table="public.crypto_candles_1d"
    )
    response = await read(fake, symbol="KRW-XYZ")
    (row,) = events_of(response)
    # the notice writer records no publication precision for any version
    assert (row["published_at"], row["published_precision"]) == (None, None)
    assert row["date"] == "2026-11-20"
    assert row["symbols"] == ["XYZ"]
    assert row["superseded_later"] is True
    assert "2026-11-25" not in json.dumps(response["data"])
    # a title that did not attribute the symbol then cannot link it now
    log[0] = revision(
        current, 1, BEFORE, {"title": "안내"}, provider_created_at=created
    )
    response = await read(
        session(events=[current], revisions=log, bar_table="public.crypto_candles_1d"),
        symbol="KRW-XYZ",
    )
    assert events_of(response) == []
    assert response["excluded_unproven"]["events"] == 1


def sec_event(**kw: Any) -> Any:
    values: dict[str, Any] = {
        "source": "sec",
        "market": "us",
        "source_event_id": "0001045810-26-000001",
        "source_url": (
            "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000001/a.htm"
        ),
        "symbol": "NVDA",
        "symbols": ["NVDA", "NVDB"],
        "published_at": datetime(2026, 11, 20, 21, 5, 6, tzinfo=UTC),
        "published_date": None,
        "published_offset": "-05:00",
        "published_precision": "second",
        "relevance": "direct",
        "relevance_state": "known",
        "relevance_basis": "recorded",
        "receipt_text": None,
    }
    values.update(kw)
    return event(1, **values)


@pytest.mark.asyncio
async def test_a_superseded_sec_filing_uses_the_revision_acceptance_time() -> None:
    accepted = datetime(2026, 11, 20, 21, 0, 0, tzinfo=UTC)
    current = sec_event(updated_at=AFTER)
    log = [
        revision(
            current,
            1,
            BEFORE,
            {"filingDate": "2026-11-20"},
            provider_created_at=accepted,
        ),
        revision(
            current,
            2,
            AFTER,
            {"filingDate": "2026-11-20"},
            provider_created_at=current.published_at,
        ),
    ]
    response = await read(session(events=[current], revisions=log), symbol="NVDA")
    (row,) = events_of(response)
    assert row["published_at"] == "2026-11-20T21:00:00Z"
    assert row["published_offset"] == "-05:00"
    assert row["published_precision"] == "second"
    assert row["symbols"] is None
    # a co-registrant ticker has no proof of when it was linked
    response = await read(session(events=[current], revisions=log), symbol="NVDB")
    assert events_of(response) == []
    assert response["excluded_unproven"]["events"] == 1


@pytest.mark.asyncio
async def test_a_sec_row_merged_after_the_cutoff_withholds_its_ticker_union() -> None:
    current = sec_event(updated_at=AFTER)
    log = [revision(current, 1, BEFORE, {"filingDate": "2026-11-20"})]
    response = await read(session(events=[current], revisions=log), symbol="NVDA")
    (row,) = events_of(response)
    assert row["symbols"] is None
    assert row["superseded_later"] is False
    assert ("data.0.symbols", "unavailable") in unknown_pairs(response)
    assert "NVDB" not in json.dumps(response)
    response = await read(session(events=[current], revisions=log), symbol="NVDB")
    assert events_of(response) == []
    assert response["excluded_unproven"]["events"] == 1
    # untouched since the cutoff, the union is part of the version
    still = sec_event()
    response = await read(session(events=[still], revisions=log), symbol="NVDB")
    assert events_of(response)[0]["symbols"] == ["NVDA", "NVDB"]


@pytest.mark.asyncio
async def test_a_dart_row_rewritten_after_the_cutoff_withholds_changeable_values() -> (
    None
):
    rewritten = event(1, updated_at=AFTER, relevance="direct")
    response = await read(session(events=[rewritten]))
    (row,) = events_of(response)
    assert row["version_state"] == "rewritten_after_cutoff"
    assert row["reason"] == "rewritten_after_cutoff"
    assert row["superseded_later"] is None
    for name in (
        "status",
        "published_at",
        "published_date",
        "published_offset",
        "published_precision",
        "revised_at",
        "relevance",
        "symbols",
        "version_seq",
    ):
        assert row[name] is None, name
    # identity and the receipt time stay
    assert row["first_received_at"] == "2026-11-21T00:00:00Z"
    assert row["sources"][0]["source_event_id"] == rewritten.source_event_id


@pytest.mark.asyncio
async def test_a_backfilled_dart_symbol_proves_no_link_at_the_cutoff() -> None:
    # the backfill sets ``symbol`` and leaves ``updated_at`` before the cutoff
    backfilled = event(1, symbol_resolution_reason="registry_unavailable")
    response = await read(session(events=[backfilled]))
    assert events_of(response) == []
    assert response["excluded_unproven"]["events"] == 1
    assert rse.event_ref(backfilled.event_uuid) not in json.dumps(response)


@pytest.mark.asyncio
async def test_publication_precision_dart_time_and_insert_only_naver() -> None:
    unchanged = event(1)
    enriched = event(
        2,
        published_date=None,
        published_offset=None,
        published_precision=None,
        receipt_text="2026-11-20 16:31",
    )
    naver = event(
        3,
        source="naver_disclosure",
        source_url=None,
        updated_at=AFTER,
        published_date=None,
        published_offset=None,
        published_precision=None,
        receipt_text=None,
    )
    response = await read(session(events=[unchanged, enriched, naver]))
    by_ref = {r["event_ref"]: r for r in events_of(response)}
    date_only = by_ref[rse.event_ref(unchanged.event_uuid)]
    assert date_only["published_date"] == "2026-11-20"
    assert date_only["published_at"] is None
    assert date_only["version_state"] == "unchanged_since_cutoff"
    timed = by_ref[rse.event_ref(enriched.event_uuid)]
    assert timed["published_at"] == "2026-11-20T07:31:00Z"
    assert (timed["published_offset"], timed["published_precision"]) == (
        "+09:00",
        "minute",
    )
    insert_only = by_ref[rse.event_ref(naver.event_uuid)]
    assert insert_only["version_state"] == "unchanged_since_cutoff"
    assert insert_only["status"] == "released"
    assert "source_url" not in insert_only["sources"][0]


@pytest.mark.asyncio
async def test_an_unrecorded_publication_is_never_filled_from_another_column() -> None:
    current = notice_event(1)
    response = await read(
        session(events=[current], bar_table="public.crypto_candles_1d"),
        symbol="KRW-XYZ",
    )
    (row,) = events_of(response)
    for name in (
        "published_at",
        "published_date",
        "published_offset",
        "published_precision",
    ):
        assert row[name] is None, name
        assert (f"data.0.{name}", "not_recorded") in unknown_pairs(response)
    columns = rse.events_statement(CRYPTO, ("XYZ",), DAY, DAY, CUTOFF)
    assert "release_time_utc" not in columns.selected_columns.keys()


@pytest.mark.asyncio
async def test_provenance_rows_after_the_cutoff_are_left_out() -> None:
    current = sec_event()

    def provenance(received: datetime, source_event_id: str) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            event_id=1,
            source="sec",
            source_event_id=source_event_id,
            source_url=None,
            first_received_at=received,
        )

    response = await read(
        session(
            events=[current],
            sources=[provenance(BEFORE, "early-1"), provenance(AFTER, "late-1")],
        ),
        symbol="NVDA",
    )
    (row,) = events_of(response)
    assert row["sources"] == [
        {
            "source": "sec",
            "first_received_at": "2026-11-21T00:00:00Z",
            "source_event_id": "early-1",
        }
    ]
    assert "late-1" not in json.dumps(response)


@pytest.mark.asyncio
async def test_no_raw_payload_text_or_foreign_link_leaves_the_reader() -> None:
    current = notice_event(
        1,
        source_url="https://evil.example.com/notice?id=1",
        source_event_id="id with spaces and 한글",
    )
    log = [revision(current, 1, BEFORE, {"title": "비밀 제목 (XYZ)", "body": "본문"})]
    response = await read(
        session(events=[current], revisions=log, bar_table="public.crypto_candles_1d"),
        symbol="KRW-XYZ",
    )
    body = json.dumps(response, ensure_ascii=False)
    for secret in ("비밀 제목", "본문", "evil.example.com", "with spaces"):
        assert secret not in body, secret
    (row,) = events_of(response)
    assert row["sources"] == [
        {"source": "upbit_notice", "first_received_at": "2026-11-21T00:00:00Z"}
    ]


@pytest.mark.parametrize(
    ("source", "url", "ok"),
    [
        ("dart", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=2026", True),
        ("sec", "https://www.sec.gov/Archives/edgar/data/1/2/a.htm", True),
        ("sec", "http://www.sec.gov/Archives/a.htm", False),
        ("sec", "https://user:pw@www.sec.gov/Archives/a.htm", False),
        ("sec", "https://www.sec.gov:444/Archives/a.htm", False),
        ("sec", "https://www.sec.gov.evil.com/a.htm", False),
        ("sec", "https://dart.fss.or.kr/a", False),
        ("benzinga", "https://www.benzinga.com/a?token=abc", False),
        ("benzinga", "https://www.benzinga.com/a?apikey=abc", False),
        ("naver_disclosure", "https://m.stock.naver.com/a", False),
        ("upbit_notice", "javascript:alert(1)", False),
        ("upbit_notice", None, False),
    ],
)
def test_only_public_links_on_the_sources_own_host(
    source: str, url: str | None, ok: bool
) -> None:
    assert rse.safe_url(source, url) == (url if ok else None)


# --------------------------------------------------------------------------
# bars
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_bar_row_is_exact_decimal_text_with_its_proof_times() -> None:
    response = await read(session(bars=[bar()]))
    (row,) = bars_of(response)
    assert row["close_price"] == "70500.5000"
    assert row["quantity"] == "9007199254740993"
    assert row["bar_end_at"] == "2026-11-20T11:00:00Z"
    assert row["stored_at"] == "2026-11-21T00:00:00Z"
    assert row["availability_basis"] == "last_stored_write"
    assert (row["currency"], row["unit"], row["venue"]) == ("KRW", "shares", "KRX")
    assert row["adjusted"] is None
    assert row["missing_reason"] == "unavailable"


@pytest.mark.asyncio
async def test_bars_order_before_the_days_events_by_instant() -> None:
    early = event(1, first_received_at=datetime(2026, 11, 20, 1, 0, tzinfo=UTC))
    late = event(2, first_received_at=datetime(2026, 11, 20, 12, 0, tzinfo=UTC))
    response = await read(session(events=[late, early], bars=[bar()]))
    assert [r["row_type"] for r in response["data"]] == ["event", "bar", "event"]


@pytest.mark.parametrize(
    ("end", "stored", "verdict"),
    [
        (BEFORE, BEFORE, "shown"),
        (BEFORE, CUTOFF, "shown"),
        (BEFORE, AFTER, "after_cutoff"),
        (AFTER, BEFORE, "after_cutoff"),
        (BEFORE, BEFORE - timedelta(seconds=1), "unproven"),
        (None, BEFORE, "unproven"),
        (None, AFTER, "after_cutoff"),
        (BEFORE, None, "unproven"),
    ],
)
def test_judge_bar(end: datetime | None, stored: datetime | None, verdict: str) -> None:
    assert rse.judge_bar(end, stored, CUTOFF) == verdict


@pytest.mark.parametrize(
    ("market", "day", "end"),
    [
        (KR, DAY, datetime(2026, 11, 20, 11, 0, tzinfo=UTC)),
        (US, DAY, datetime(2026, 11, 20, 21, 0, tzinfo=UTC)),
        # US early close the day after Thanksgiving
        (US, date(2026, 11, 27), datetime(2026, 11, 27, 18, 0, tzinfo=UTC)),
        (CRYPTO, DAY, datetime(2026, 11, 21, 0, 0, tzinfo=UTC)),
        (KR, date(2026, 11, 21), None),
        (US, date(2026, 11, 21), None),
    ],
)
def test_bar_end_per_market(market: Any, day: date, end: datetime | None) -> None:
    assert rse.bar_end(day, market) == end


def test_the_session_date_is_the_markets_own_day() -> None:
    kst_midnight = datetime(2026, 11, 19, 15, 0, tzinfo=UTC)
    assert rse.session_date(kst_midnight, KR) == DAY
    assert rse.session_date(datetime(2026, 11, 20, tzinfo=UTC), KR) == DAY
    assert rse.session_date(datetime(2026, 11, 20, tzinfo=UTC), US) == DAY


@pytest.mark.asyncio
async def test_forming_and_later_bars_are_counted_not_shown() -> None:
    forming = bar(venue="NTX", stored_at=datetime(2026, 11, 20, 3, 0, tzinfo=UTC))
    rewritten = bar(venue="KRX", stored_at=AFTER)
    response = await read(session(bars=[forming, rewritten]))
    assert bars_of(response) == []
    assert response["excluded_unproven"]["bars"] == 1
    assert response["excluded_after_cutoff"]["bars"] == 1
    # stored bars exist, so this is not a missing-data gap
    assert ("bars", "missing_data") not in unknown_pairs(response)
    assert ("bars.unproven", "incomplete_evidence") in unknown_pairs(response)


@pytest.mark.asyncio
async def test_no_stored_bar_is_a_named_gap() -> None:
    response = await read(session())
    assert ("bars", "missing_data") in unknown_pairs(response)
    assert response["total"] == 0


# --------------------------------------------------------------------------
# failures are unknowns, never zero
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_failed_event_read_leaves_counts_and_total_unknown() -> None:
    response = await read(session(events=operational_error(), bars=[bar()]))
    assert response["total"] is None
    assert response["excluded_after_cutoff"] == {"events": None, "bars": 0}
    assert response["excluded_unproven"] == {"events": None, "bars": 0}
    pairs = unknown_pairs(response)
    assert ("source.events", "upstream_error") in pairs
    assert ("excluded_after_cutoff.events", "count_unavailable") in pairs
    assert ("total", "count_unavailable") in pairs
    assert "raw driver text" not in json.dumps(response)


@pytest.mark.asyncio
async def test_without_the_revision_log_a_revisioned_event_is_not_shown() -> None:
    sec = sec_event(symbol="NVDA")
    response = await read(
        session(events=[sec], revisions=operational_error()), symbol="NVDA"
    )
    assert events_of(response) == []
    assert response["total"] is None
    assert response["excluded_unproven"]["events"] is None
    assert ("source.event_revisions", "upstream_error") in unknown_pairs(response)
    # a source with no revision log does not depend on it
    response = await read(session(events=[event(1)], revisions=operational_error()))
    assert len(events_of(response)) == 1
    assert response["total"] == 1
    assert ("source.event_revisions", "upstream_error") in unknown_pairs(response)


@pytest.mark.asyncio
async def test_a_failed_bar_or_partition_read_is_unknown() -> None:
    response = await read(session(events=[event(1)], bars=operational_error()))
    assert response["total"] is None
    assert response["excluded_after_cutoff"]["bars"] is None
    assert ("source.bars", "upstream_error") in unknown_pairs(response)
    response = await read(session(partitions=operational_error()))
    assert response["collection"]["gaps"] is None
    assert set(response["collection"]["latest_collected"].values()) == {None}
    assert ("collection", "upstream_error") in unknown_pairs(response)


@pytest.mark.asyncio
async def test_a_capped_event_read_claims_no_total(monkeypatch) -> None:
    monkeypatch.setattr(rse, "MAX_SOURCE_ROWS", 2)
    response = await read(session(events=[event(1), event(2)]))
    assert response["total"] is None
    assert response["excluded_after_cutoff"]["events"] is None
    assert ("source.events", "incomplete_evidence") in unknown_pairs(response)


# --------------------------------------------------------------------------
# collection and gaps
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_collection_reports_latest_days_and_closed_gap_reasons() -> None:
    week = [date(2026, 11, d) for d in range(16, 23)]
    rows = [
        partition(week[0], "succeeded"),
        partition(week[1], "failed", last_error="quota"),
        partition(week[2], "failed", last_error="Traceback: secret-token=abc"),
        partition(week[3], "pending"),
        # 11-20 (Friday) has no row; 11-21/22 are the weekend
        partition(week[5], "succeeded", event_count=0),
        partition(week[6], "failed", last_error="timeout"),
    ]
    response = await read(
        session(partitions=rows, newest=[latest(week[0])]),
        from_=week[0],
        to=week[-1],
    )
    collection = response["collection"]
    assert collection["basis"] == "current_partitions"
    assert [(g["date"], g["reason"]) for g in collection["gaps"]] == [
        ("2026-11-17", "quota"),
        ("2026-11-18", "failed"),
        ("2026-11-19", "pending"),
        ("2026-11-20", "not_yet_run"),
    ]
    assert collection["latest_collected"] == {
        "dart": "2026-11-16",
        "naver_disclosure": None,
    }
    assert (
        "collection.latest_collected.naver_disclosure",
        "no_partition",
    ) in unknown_pairs(response)
    assert "secret-token" not in json.dumps(response)


def test_an_unproven_zero_is_a_gap_on_expected_days_only() -> None:
    kr = SOURCES_BY_MARKET["kr"]
    zero = [partition(DAY, "succeeded", event_count=0)]
    assert [g["reason"] for g in rse.collection_gaps(zero, kr, DAY, DAY, DAY)] == [
        "unproven_zero"
    ]
    saturday = date(2026, 11, 21)
    zero = [partition(saturday, "succeeded", event_count=0)]
    assert rse.collection_gaps(zero, kr, saturday, saturday, saturday) == []
    # no collector proves a zero yet
    assert rse.PROVEN_ZERO_SOURCES == frozenset()


@pytest.mark.asyncio
async def test_an_unproven_zero_never_advances_latest_collected() -> None:
    fake = session(partitions=[partition(DAY, "succeeded", event_count=0)])
    response = await read(fake)
    assert response["collection"]["latest_collected"]["dart"] is None
    assert ("collection.latest_collected.dart", "missing_data") in unknown_pairs(
        response
    )
    newest = next(s for s in fake.statements if "max(" in s.lower())
    assert "event_count >" in newest and "status =" in newest


def test_us_sources_have_no_expected_days() -> None:
    rows = [
        partition(DAY, "pending", source="sec"),
        partition(DAY, "failed", source="sec", last_error="timeout"),
        partition(DAY, "pending", source="benzinga", category="earnings"),
        partition(DAY, "succeeded", source="benzinga", category="guidance"),
        partition(DAY, "succeeded", source="benzinga", category="split", event_count=0),
    ]
    us = SOURCES_BY_MARKET["us"]
    gaps = rse.collection_gaps(rows, us, DAY, DAY, date(2026, 11, 28))
    # a missing row says nothing for these sources
    assert sorted((g["source"], g["category"], g["reason"]) for g in gaps) == [
        ("benzinga", "earnings", "pending"),
        ("benzinga", "split", "unproven_zero"),
        ("sec", "disclosure", "pending"),
        ("sec", "disclosure", "timeout"),
    ]


@pytest.mark.asyncio
async def test_expected_days_are_only_counted_for_the_kr_market() -> None:
    response = await read(
        session(bar_table="public.us_candles_1d"), symbol="NVDA", from_=DAY, to=DAY
    )
    assert response["collection"]["gaps"] == []
    assert response["collection"]["latest_collected"] == {
        "sec": None,
        "benzinga": None,
    }
    pairs = unknown_pairs(response)
    assert ("collection.latest_collected.sec", "missing_data") in pairs
    assert ("collection.latest_collected.benzinga", "missing_data") in pairs


def test_future_days_are_not_yet_gaps() -> None:
    kr = SOURCES_BY_MARKET["kr"]
    gaps = rse.collection_gaps([], kr, DAY, date(2026, 11, 27), DAY)
    assert [(g["date"], g["reason"]) for g in gaps] == [("2026-11-20", "not_yet_run")]


@pytest.mark.asyncio
async def test_a_long_gap_list_keeps_the_newest_and_says_so() -> None:
    response = await read(session(), from_=date(2025, 11, 21), to=DAY)
    collection = response["collection"]
    assert collection["gaps_truncated"] is True
    assert len(collection["gaps"]) == 200
    assert collection["gaps"][-1]["date"] == "2026-11-20"


def test_the_failure_codes_are_the_writers_codes() -> None:
    assert rse.FAILURE_CODES == {code.value for code in CollectionFailure}


# --------------------------------------------------------------------------
# request shape
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cutoff",
    [
        None,
        "",
        "2026-11-23",
        "2026-11-23T00:00:00",
        "2026-11-23T00:00:00+09:00",
        "2026-11-23T00:00:00+00:00",
        "2026-13-01T00:00:00Z",
        datetime(2026, 11, 23),
    ],
)
@pytest.mark.asyncio
async def test_the_cutoff_must_be_an_rfc3339_utc_instant(cutoff: Any) -> None:
    fake = session()
    with pytest.raises(RetroReadError) as err:
        await read_retro_symbol_events(
            fake, symbol="005930", known_cutoff=cutoff, from_=DAY, to=DAY
        )
    assert err.value.code == "validation_error"
    assert fake.statements == []


@pytest.mark.asyncio
async def test_the_window_is_bounded_and_ordered() -> None:
    for start, end in ((DAY, DAY - timedelta(days=1)), (date(2025, 1, 1), DAY)):
        with pytest.raises(RetroReadError):
            await read(session(), from_=start, to=end)


@pytest.mark.parametrize(
    ("given", "market", "expected"),
    [
        ("005930", None, ("005930",)),
        ("BRK-B", "us", ("BRK.B", "BRK-B", "BRK/B")),
        ("NVDA", None, ("NVDA",)),
        ("XYZ", "crypto", ("KRW-XYZ", "XYZ")),
    ],
)
def test_stored_spellings(
    given: str, market: str | None, expected: tuple[str, ...]
) -> None:
    from app.services.retro_v1.market import (
        normalize_symbol,
        select_market,
        symbol_for_market,
    )

    clean = symbol_for_market(given, market)
    info = select_market(clean, market)
    assert rse.stored_spellings(normalize_symbol(clean, info), info) == expected


def test_the_events_statement_is_scoped_to_symbol_market_and_official_sources() -> None:
    compiled = rse.events_statement(KR, ("005930",), DAY, DAY, CUTOFF).compile(
        dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True}
    )
    sql = str(compiled)
    params = list(compiled.params.values())
    assert "market_events.symbol IN" in sql
    assert "market_events.symbols &&" in sql
    assert "EXISTS (SELECT *" in sql and "observed_at >" in sql
    assert {"kr", "dart", "naver_disclosure", "005930"} <= {
        p for p in params if isinstance(p, str)
    }
    assert "finnhub" not in params
    # only the receipt time and the symbol reason are read from the raw payload
    assert sql.count("market_events.raw_payload_json") == 2
    assert {"rcept_dt", "symbol_resolution_reason"} <= {
        p for p in params if isinstance(p, str)
    }


def test_source_sets_agree() -> None:
    official = {name for names in SOURCES_BY_MARKET.values() for name in names}
    assert official == set(rse._OFFICIAL_SOURCES)
    assert (
        rse._PARTITIONED
        | {
            "naver_disclosure",
            "upbit_notice",
            "bithumb_notice",
        }
        == official
    )
    assert REVISIONED_SOURCES | INSERT_ONLY_SOURCES | {"dart"} == official
    assert not REVISIONED_SOURCES & INSERT_ONLY_SOURCES


def test_event_ref_matches_the_model_ref() -> None:
    row = event(7)
    assert (
        rse.event_ref(row.event_uuid)
        == MarketEvent(event_uuid=row.event_uuid).event_ref
    )


@pytest.mark.asyncio
async def test_pages_follow_the_cursor_of_the_same_cutoff() -> None:
    def fake() -> Any:
        return session(events=[event(1), event(2)], bars=[bar()])

    first = await read(fake(), limit=2)
    assert (first["total"], first["truncated"]) == (3, True)
    rest = await read(fake(), limit=2, cursor=first["next_cursor"])
    assert len(rest["data"]) == 1
    with pytest.raises(RetroReadError):
        await read(
            fake(),
            limit=2,
            cursor=first["next_cursor"],
            known_cutoff="2026-11-24T00:00:00Z",
        )


@pytest.mark.asyncio
async def test_a_timezone_aware_datetime_cutoff_is_accepted() -> None:
    response = await read(session(), known_cutoff=CUTOFF)
    assert response["known_cutoff"] == "2026-11-23T00:00:00Z"


@pytest.mark.asyncio
async def test_data_as_of_never_passes_the_cutoff() -> None:
    response = await read(
        session(
            events=[event(1), event(2, first_received_at=AFTER, updated_at=AFTER)],
            bars=[bar(stored_at=AFTER)],
        )
    )
    assert response["data_as_of"] == "2026-11-21T00:00:00Z"
    assert BEFORE < CUTOFF
