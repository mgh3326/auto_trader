"""ORM shape and constraint tests for market_events tables (ROB-128)."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_market_event_model_columns():
    from app.models.market_events import MarketEvent

    cols = {c.name for c in MarketEvent.__table__.columns}
    expected = {
        "id",
        "event_uuid",
        "category",
        "market",
        "country",
        "symbol",
        "company_name",
        "title",
        "event_date",
        "release_time_utc",
        "release_time_local",
        "source_timezone",
        "time_hint",
        "importance",
        "status",
        "source",
        "source_event_id",
        "source_url",
        "fiscal_year",
        "fiscal_quarter",
        "raw_payload_json",
        "fetched_at",
        "created_at",
        "updated_at",
    }
    assert expected <= cols
    assert MarketEvent.__table__.schema is None  # public schema


@pytest.mark.unit
def test_market_event_partial_unique_indexes_exist():
    from app.models.market_events import MarketEvent

    index_names = {idx.name for idx in MarketEvent.__table__.indexes}
    assert "uq_market_events_source_event_id" in index_names
    assert "uq_market_events_natural_key" in index_names


@pytest.mark.unit
def test_market_event_value_model_columns():
    from app.models.market_events import MarketEventValue

    cols = {c.name for c in MarketEventValue.__table__.columns}
    expected = {
        "id",
        "event_id",
        "metric_name",
        "period",
        "actual",
        "forecast",
        "previous",
        "revised_previous",
        "unit",
        "surprise",
        "surprise_pct",
        "released_at",
        "created_at",
        "updated_at",
    }
    assert expected <= cols


@pytest.mark.unit
def test_market_event_ingestion_partition_model_columns():
    from app.models.market_events import MarketEventIngestionPartition

    cols = {c.name for c in MarketEventIngestionPartition.__table__.columns}
    expected = {
        "id",
        "source",
        "category",
        "market",
        "partition_date",
        "status",
        "event_count",
        "started_at",
        "finished_at",
        "last_error",
        "retry_count",
        "source_request_hash",
        "created_at",
        "updated_at",
    }
    assert expected <= cols

    constraint_names = {
        c.name for c in MarketEventIngestionPartition.__table__.constraints
    }
    assert "uq_market_event_ingestion_partitions_source" in constraint_names
