"""hk 6968: publication rendering and stored disclosure timing precedence."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.services.disclosures.timing import StoredDisclosureRow, disclosure_timing
from app.services.publication_time import render_publication

pytestmark = pytest.mark.unit

INSTANT = datetime(2026, 11, 26, 15, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("offset", "precision", "market", "expected"),
    [
        ("+09:00", "minute", "kr", "2026-11-27T00:30:00+09:00"),
        ("-05:00", "second", "us", "2026-11-26T10:30:00-05:00"),
        (None, None, "kr", "2026-11-27T00:30:00+09:00"),
        (None, None, "us", "2026-11-26T15:30:00+00:00"),
        ("bad", "second", "us", "2026-11-26T15:30:00+00:00"),
        ("+09:00", "date", "kr", "2026-11-27"),
    ],
)
def test_render_publication(offset, precision, market, expected):
    assert (
        render_publication(INSTANT, offset=offset, precision=precision, market=market)
        == expected
    )


def test_unknown_publication_stays_unknown():
    assert (
        render_publication(None, offset="+09:00", precision=None, market="kr") is None
    )


def test_stored_publication_columns_win():
    rows = [
        StoredDisclosureRow(
            source="dart",
            published_date=date(2026, 11, 25),
            published_precision="date",
            receipt_text="2026-11-26T09:00:00",
        )
    ]

    timing = disclosure_timing("2026-11-26", rows)

    assert timing["published_at"] == "2026-11-25"
    assert timing["published_at_source"] == "market_events_publication"
    assert timing["first_received_at"] is None


def test_stored_publication_keeps_the_source_offset():
    rows = [
        StoredDisclosureRow(
            source="dart",
            published_at=INSTANT,
            published_offset="+00:00",
            published_precision="datetime",
        )
    ]

    timing = disclosure_timing("2026-11-26", rows)

    assert timing["published_at"] == "2026-11-26T15:30:00+00:00"
    assert timing["published_offset"] == "+00:00"
    assert timing["published_precision"] == "datetime"


def test_no_evidence_at_all_is_unknown():
    assert disclosure_timing(None, []) == {
        "published_at": None,
        "published_offset": None,
        "published_precision": None,
        "published_at_source": None,
        "first_received_at": None,
        "first_received_source": None,
    }


def test_first_receipt_is_the_earliest_instant_of_either_source():
    rows = [
        StoredDisclosureRow(source="dart", first_received_at=INSTANT),
        StoredDisclosureRow(
            source="naver_disclosure",
            first_received_at=INSTANT.replace(hour=16),
        ),
    ]

    timing = disclosure_timing("2026-11-26", rows)

    assert timing["first_received_at"] == "2026-11-26T15:30:00+00:00"
    assert timing["first_received_source"] == "dart"
