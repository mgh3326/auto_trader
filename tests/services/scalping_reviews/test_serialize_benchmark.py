from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.models.scalping_reviews import ScalpingDailyReview
from app.routers.invest_scalping import _serialize_review

_NOW = dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.UTC)


def test_serialize_review_benchmark_none_serializes_to_null() -> None:
    review = ScalpingDailyReview(
        review_date=dt.date(2026, 6, 20),
        product="usdm_futures",
        account_scope="binance_demo",
        session_tag="",
        decision="review",
        status="draft",
        created_at=_NOW,
        updated_at=_NOW,
    )
    out = _serialize_review(review)
    assert out["metrics"]["benchmarkReturnBps"] is None


def test_serialize_review_includes_benchmark_return_bps() -> None:
    review = ScalpingDailyReview(
        review_date=dt.date(2026, 6, 20),
        product="usdm_futures",
        account_scope="binance_demo",
        session_tag="",
        decision="review",
        status="draft",
        created_at=_NOW,
        updated_at=_NOW,
        benchmark_return_bps=Decimal("7.5"),
    )
    out = _serialize_review(review)
    assert out["metrics"]["benchmarkReturnBps"] == "7.5"
