"""Contract tests for news readiness payload/get_news_readiness (ROB-61).

These tests pin the exact behavior that the Prefect news-ingestor-pending-push
flow depends on: status whitelist, max_age_minutes default, source_counts empty
-> warning, latest-finished-at selection, and is_ready transitions.

Pure-unit: no DB connections, no network, no Prefect imports.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_run(
    *,
    run_uuid: str = "run-contract-test",
    market: str = "kr",
    feed_set: str = "kr-core",
    status: str = "success",
    finished_at: datetime | None = None,
    source_counts: dict | None = None,
    started_at: datetime | None = None,
    created_at: datetime | None = None,
) -> object:
    from app.models.news import NewsIngestionRun

    now = datetime.now(UTC)
    return NewsIngestionRun(
        run_uuid=run_uuid,
        market=market,
        feed_set=feed_set,
        started_at=started_at or now,
        finished_at=finished_at,
        status=status,
        source_counts=source_counts
        if source_counts is not None
        else {"browser_naver_mainnews": 20},
        inserted_count=20,
        skipped_count=0,
        created_at=created_at or now,
    )


@pytest.mark.unit
class TestNewsReadinessPayloadContract:
    """Direct tests for _news_readiness_payload — no DB required."""

    def test_success_status_fresh_finished_at_is_ready(self):
        from app.services.llm_news_service import _news_readiness_payload

        run = _make_run(status="success", finished_at=datetime.now(UTC))
        result = _news_readiness_payload(
            market="kr",
            latest_run=run,
            latest_article_published_at=datetime.now(UTC),
            max_age_minutes=180,
        )

        assert result.is_ready is True
        assert result.is_stale is False
        assert result.warnings == []

    def test_partial_status_fresh_finished_at_is_ready(self):
        """partial runs are acceptable; Prefect push may write partial on partial crawl."""
        from app.services.llm_news_service import _news_readiness_payload

        run = _make_run(status="partial", finished_at=datetime.now(UTC))
        result = _news_readiness_payload(
            market="kr",
            latest_run=run,
            latest_article_published_at=datetime.now(UTC),
            max_age_minutes=180,
        )

        assert result.is_ready is True
        assert result.is_stale is False

    def test_no_run_emits_news_unavailable(self):
        """No NewsIngestionRun rows for the market → news_unavailable."""
        from app.services.llm_news_service import _news_readiness_payload

        result = _news_readiness_payload(
            market="kr",
            latest_run=None,
            latest_article_published_at=None,
            max_age_minutes=180,
        )

        assert result.is_ready is False
        assert "news_unavailable" in result.warnings
        assert "news_stale" in result.warnings

    def test_finished_at_none_emits_news_run_unfinished(self):
        """Run exists but finished_at is null → news_run_unfinished."""
        from app.services.llm_news_service import _news_readiness_payload

        run = _make_run(finished_at=None)
        result = _news_readiness_payload(
            market="kr",
            latest_run=run,
            latest_article_published_at=None,
            max_age_minutes=180,
        )

        assert result.is_ready is False
        assert "news_run_unfinished" in result.warnings

    def test_empty_source_counts_emits_news_sources_empty(self):
        """Run with source_counts={} → news_sources_empty."""
        from app.services.llm_news_service import _news_readiness_payload

        run = _make_run(
            finished_at=datetime.now(UTC),
            source_counts={},
        )
        result = _news_readiness_payload(
            market="kr",
            latest_run=run,
            latest_article_published_at=None,
            max_age_minutes=180,
        )

        assert result.is_ready is False
        assert "news_sources_empty" in result.warnings

    def test_finished_at_older_than_default_max_age_is_stale(self):
        """finished_at > 180 min ago → is_stale=True, news_stale warning."""
        from app.services.llm_news_service import _news_readiness_payload

        stale_finished_at = datetime.now(UTC) - timedelta(minutes=200)
        run = _make_run(finished_at=stale_finished_at)
        result = _news_readiness_payload(
            market="kr",
            latest_run=run,
            latest_article_published_at=stale_finished_at,
            max_age_minutes=180,
        )

        assert result.is_ready is False
        assert result.is_stale is True
        assert "news_stale" in result.warnings

    def test_max_age_minutes_override_is_honored(self):
        """max_age_minutes=30 should flag a 60-min-old run as stale."""
        from app.services.llm_news_service import _news_readiness_payload

        finished_at = datetime.now(UTC) - timedelta(minutes=60)
        run = _make_run(finished_at=finished_at)
        result = _news_readiness_payload(
            market="kr",
            latest_run=run,
            latest_article_published_at=finished_at,
            max_age_minutes=30,
        )

        assert result.is_ready is False
        assert result.is_stale is True
        assert "news_stale" in result.warnings

    def test_run_just_within_max_age_is_ready(self):
        """Run finished exactly at max_age_minutes boundary is still ready."""
        from app.services.llm_news_service import _news_readiness_payload

        finished_at = datetime.now(UTC) - timedelta(minutes=30)
        run = _make_run(finished_at=finished_at)
        result = _news_readiness_payload(
            market="kr",
            latest_run=run,
            latest_article_published_at=finished_at,
            max_age_minutes=180,
        )

        assert result.is_ready is True
        assert result.is_stale is False

    def test_max_age_minutes_default_is_180(self):
        """get_news_readiness signature defaults max_age_minutes to 180."""
        import inspect

        from app.services.llm_news_service import get_news_readiness

        sig = inspect.signature(get_news_readiness)
        assert sig.parameters["max_age_minutes"].default == 180

    def test_warnings_are_deduplicated(self):
        """Duplicate warnings must not appear in the returned list."""
        from app.services.llm_news_service import _news_readiness_payload

        result = _news_readiness_payload(
            market="kr",
            latest_run=None,
            latest_article_published_at=None,
            max_age_minutes=180,
        )

        assert len(result.warnings) == len(set(result.warnings))


@pytest.mark.unit
class TestTimezoneHelpersForReadiness:
    """Cover timezone helper branches used by news readiness timestamps."""

    def test_now_kst_returns_aware_kst_datetime(self):
        from app.core.timezone import KST, now_kst

        result = now_kst()

        assert result.tzinfo == KST
        assert result.utcoffset() == timedelta(hours=9)

    def test_to_kst_naive_returns_naive_datetime_as_is(self):
        from app.core.timezone import to_kst_naive

        naive = datetime(2026, 4, 30, 9, 15, 0)

        assert to_kst_naive(naive) is naive

    def test_format_datetime_defaults_to_now_kst(self, monkeypatch):
        from app.core import timezone as tz

        fixed = datetime(2026, 4, 30, 9, 15, 0, tzinfo=tz.KST)
        monkeypatch.setattr(tz, "now_kst", lambda: fixed)

        assert tz.format_datetime(fmt="%Y-%m-%d %H:%M") == "2026-04-30 09:15"

    def test_format_datetime_uses_supplied_datetime_and_format(self):
        from app.core.timezone import format_datetime

        value = datetime(2026, 4, 30, 0, 15, 0, tzinfo=UTC)

        assert format_datetime(value, fmt="%H:%M %z") == "00:15 +0000"


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetNewsReadinessStatusWhitelist:
    """get_news_readiness filters on status IN ('success', 'partial').

    Tests use mock DB sessions so no real database is required.
    """

    async def test_no_matching_run_returns_news_unavailable(self):
        """When query returns no rows (e.g. only failed runs), result is news_unavailable."""
        from app.services.llm_news_service import get_news_readiness

        db = AsyncMock()
        run_result = MagicMock()
        run_result.scalars.return_value.first.return_value = None
        article_result = MagicMock()
        article_result.scalar_one_or_none.return_value = None
        db.execute.side_effect = [run_result, article_result]

        result = await get_news_readiness(market="kr", db=db)

        assert result.is_ready is False
        assert "news_unavailable" in result.warnings

    async def test_matching_success_run_returns_ready(self):
        """When a recent success run is returned, result is is_ready=True."""
        from app.services.llm_news_service import get_news_readiness

        db = AsyncMock()
        run = _make_run(
            status="success",
            finished_at=datetime.now(UTC),
            source_counts={"browser_naver_mainnews": 10},
        )
        run_result = MagicMock()
        run_result.scalars.return_value.first.return_value = run
        article_result = MagicMock()
        article_result.scalar_one_or_none.return_value = datetime.now(UTC)
        db.execute.side_effect = [run_result, article_result]

        result = await get_news_readiness(market="kr", db=db)

        assert result.is_ready is True
        assert result.latest_run_uuid == "run-contract-test"

    async def test_matching_partial_run_returns_ready(self):
        """Partial runs are included in the status whitelist."""
        from app.services.llm_news_service import get_news_readiness

        db = AsyncMock()
        run = _make_run(
            status="partial",
            finished_at=datetime.now(UTC),
            source_counts={"yna_market": 5},
        )
        run_result = MagicMock()
        run_result.scalars.return_value.first.return_value = run
        article_result = MagicMock()
        article_result.scalar_one_or_none.return_value = datetime.now(UTC)
        db.execute.side_effect = [run_result, article_result]

        result = await get_news_readiness(market="kr", db=db)

        assert result.is_ready is True
        assert result.latest_status == "partial"
