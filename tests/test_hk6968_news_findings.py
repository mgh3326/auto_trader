"""hk 6968: the seven findings deferred from PR #2631 (hk 6767)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.analysis.stages import news_stage
from app.core.timezone import KST
from app.schemas.news import NewsArticleCreate, NewsBulkIngestRequest
from app.services import symbol_news_store
from app.services.market_events import ingestion
from app.services.news_payload_normalizer import (
    _article_values_from_ingestor_payload,
    _related_symbol_values_from_ingestor_payload,
    extract_source_document_ids,
)

pytestmark = pytest.mark.unit


def _payload(**article) -> dict:
    return {
        "ingestion_run": {"run_uuid": "run-6968", "market": "kr", "feed_set": "t"},
        "articles": [
            {
                "url": "https://example.com/a",
                "title": "제목",
                "fingerprint": "f1",
                "market": "kr",
                "source": "feed",
                **article,
            }
        ],
    }


def _article(**article):
    return NewsBulkIngestRequest.model_validate(_payload(**article)).articles[0]


# --- 1. news_stage freshness uses the right instant ---------------------------


@pytest.mark.parametrize("aware", [True, False])
def test_news_stage_age_converts_to_kst_before_dropping_tzinfo(aware):
    instant = datetime.now(UTC) - timedelta(minutes=5)
    published = instant if aware else instant.astimezone(KST).replace(tzinfo=None)

    signals = news_stage._compute_signals_from_articles(
        [SimpleNamespace(title="t", article_published_at=published, keywords=[])]
    )

    assert 4 <= signals["newest_age_minutes"] <= 6


def test_news_stage_compares_mixed_aware_and_naive_times():
    newest = datetime.now(UTC) - timedelta(minutes=10)
    older_naive_kst = (newest - timedelta(hours=2)).astimezone(KST)

    signals = news_stage._compute_signals_from_articles(
        [
            SimpleNamespace(
                title="a",
                article_published_at=older_naive_kst.replace(tzinfo=None),
                keywords=[],
            ),
            SimpleNamespace(title="b", article_published_at=newest, keywords=[]),
        ]
    )

    assert 9 <= signals["newest_age_minutes"] <= 11


# --- 2 / 4. offset and precision validation before persistence ----------------


@pytest.mark.parametrize(
    "offset",
    ["+09:00:00", "Asia/Seoul", "+0900", "Z", "", "+24:00", "+09:60", "+9:00"],
)
def test_malformed_offset_rejects_the_whole_batch(offset):
    with pytest.raises(ValidationError, match="published_offset"):
        NewsBulkIngestRequest.model_validate(_payload(published_offset=offset))
    with pytest.raises(ValidationError):
        NewsArticleCreate(url="https://e/x", title="t", published_offset=offset)


@pytest.mark.parametrize("precision", ["millisecond", "datetime", "hour", ""])
def test_unknown_precision_rejects_the_whole_batch(precision):
    with pytest.raises(ValidationError, match="published_precision"):
        NewsBulkIngestRequest.model_validate(_payload(published_precision=precision))


@pytest.mark.parametrize("offset", ["+09:00", "-05:30", "+00:00", None])
@pytest.mark.parametrize("precision", ["second", "minute", "date", None])
def test_valid_offset_and_precision_are_accepted(offset, precision):
    article = _article(published_offset=offset, published_precision=precision)

    assert (article.published_offset, article.published_precision) == (
        offset,
        precision,
    )


def test_raw_offset_with_seconds_is_not_stored():
    article = _article(
        published_at="2026-11-26T10:00:00",
        raw={"published_at_raw": "2026-11-26T10:00:00+09:30:15"},
    )

    values = _article_values_from_ingestor_payload(article)

    assert values["published_offset"] is None
    assert values["published_at"].utcoffset() == timedelta(hours=9)


# --- 3. relations are never asserted direct without evidence ------------------


class _CaptureDb:
    def __init__(self) -> None:
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(rowcount=1)


async def _written_relations(rows: list[dict]) -> list[object]:
    db = _CaptureDb()
    await symbol_news_store.upsert_related_symbols(db, rows, commit=False)
    params = db.statements[0].compile(dialect=postgresql.dialect()).params
    return [params[f"relation_type_m{index}"] for index in range(len(rows))]


def _relation_row(**extra) -> dict:
    return {
        "article_id": 1,
        "market": "kr",
        "symbol": "005930",
        "source": "test",
        **extra,
    }


@pytest.mark.asyncio
async def test_writer_keeps_unevidenced_relations_unknown():
    evidence = {"kind": "title_or_summary_name_match"}

    written = await _written_relations(
        [
            _relation_row(),
            _relation_row(relation_type="direct"),
            _relation_row(relation_type=None, relation_evidence=evidence),
            _relation_row(relation_type="sector", relation_evidence=evidence),
            _relation_row(relation_type="direct", relation_evidence=evidence),
        ]
    )

    assert written == [None, None, None, "sector", "direct"]


@pytest.mark.asyncio
@pytest.mark.parametrize("relation", ["competitor", ["direct"], {"a": 1}, 3])
async def test_writer_rejects_relations_outside_the_vocabulary(relation):
    with pytest.raises(ValueError, match="invalid_news_symbol_relation"):
        await _written_relations([_relation_row(relation_type=relation)])


def test_provider_candidate_without_a_relation_is_unknown():
    article = _article(raw={"stock_candidates": [{"symbol": "005930"}]})

    [row] = _related_symbol_values_from_ingestor_payload(
        article_id=1, article_data=article
    )

    assert row["relation_type"] is None
    assert row["relation_evidence"]["judgment_state"] == "pending"


# --- 5. nested document ids accumulate ----------------------------------------


def test_nested_records_keep_every_cited_document_id():
    raw = {
        "filings": [{"rcept_no": "A"}, {"rcept_no": "B"}, {"rcept_no": "A"}],
        "rcept_no": "C",
    }

    assert extract_source_document_ids(raw) == {"rcept_no": ["A", "B", "C"]}


# --- 6. the provider's raw offset wins over the parsed instant's --------------


@pytest.mark.parametrize(
    "carrier",
    [
        {"raw": {"published_at_raw": "2026-11-26T10:00:00+09:00"}},
        {"published_at_raw": "2026-11-26T10:00:00+09:00"},
    ],
)
def test_raw_provider_offset_precedes_parsed_offset(carrier):
    article = _article(published_at="2026-11-26T01:00:00+00:00", **carrier)

    values = _article_values_from_ingestor_payload(article)

    assert values["published_offset"] == "+09:00"
    assert values["published_at"] == datetime(2026, 11, 26, 1, 0, tzinfo=UTC)
    assert values["article_published_at"] == datetime(2026, 11, 26, 10, 0)
    assert values["published_at_raw"] == "2026-11-26T10:00:00+09:00"


def test_explicit_offset_precedes_raw_offset():
    article = _article(
        published_at="2026-11-26T01:00:00+00:00",
        published_at_raw="2026-11-26T10:00:00+09:00",
        published_offset="+00:00",
    )

    assert _article_values_from_ingestor_payload(article)["published_offset"] == (
        "+00:00"
    )


# --- 7. non-string relation_type is dropped, not a TypeError ------------------


@pytest.mark.parametrize("relation", [["direct"], {"type": "direct"}, 1, True])
def test_non_string_candidate_relation_is_rejected(relation):
    article = _article(
        raw={
            "stock_candidates": [
                {"symbol": "005930", "relation_type": relation},
                {"symbol": "000660", "relation_type": "sector"},
            ]
        }
    )

    rows = _related_symbol_values_from_ingestor_payload(
        article_id=1, article_data=article
    )

    assert [(row["symbol"], row["relation_type"]) for row in rows] == [
        ("000660", "sector")
    ]


# --- DART first receipt -------------------------------------------------------


@pytest.mark.asyncio
async def test_dart_ingestion_stamps_first_receipt_after_the_fetch(monkeypatch):
    repo = AsyncMock()
    monkeypatch.setattr(ingestion, "MarketEventsRepository", Mock(return_value=repo))
    fetched_at: list[datetime] = []

    async def fetch(_day):
        fetched_at.append(datetime.now(UTC))
        return [
            {
                "rcept_no": "20261126000101",
                "rcept_dt": "2026-11-26T17:59:00",
                "report_nm": "자기주식취득결정",
                "corp_name": "삼성전자",
                "stock_code": "005930",
            }
        ]

    await ingestion.ingest_kr_disclosures_for_date(
        MagicMock(), date(2026, 11, 26), fetch_rows=fetch
    )

    event = repo.upsert_event_with_values.await_args.args[0]
    assert fetched_at[0] <= event["first_received_at"] <= datetime.now(UTC)
