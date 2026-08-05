from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow as pa
import pytest

from research.crypto_corpus.loader import LabeledCorpus
from research.crypto_corpus.policy import venue_policy
from research.crypto_stage_b.source import (
    CryptoStageBInputError,
    source_from_labeled_corpus,
)


def _corpus(
    *, session: datetime, consumer_intent: str = "time_series"
) -> LabeledCorpus:
    table = pa.table(
        {
            "venue": ["upbit_krw"],
            "symbol": ["KRW-TEST"],
            "frequency": ["1d"],
            "bucket_timezone": ["UTC"],
            "open_time_utc": [session],
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.0],
            "base_volume": [1.0],
            "quote_volume": [100.0],
        }
    )
    return LabeledCorpus(
        table=table,
        policy=venue_policy("upbit_krw"),
        source_paths=(Path("/sealed/exploration.parquet"),),
        consumer_intent=consumer_intent,  # type: ignore[arg-type]
    )


def test_labeled_corpus_adapter_requires_time_series_daily_utc_extract() -> None:
    source = source_from_labeled_corpus(
        _corpus(session=datetime(2024, 1, 1, tzinfo=UTC)),
        exploration_start=date(2024, 1, 1),
        exploration_end=date(2024, 1, 1),
    )

    assert source.get("upbit_krw", "KRW-TEST", date(2024, 1, 1)) is not None


def test_labeled_corpus_adapter_refuses_outside_window_or_xsec_intent() -> None:
    with pytest.raises(CryptoStageBInputError, match="outside explicit exploration"):
        source_from_labeled_corpus(
            _corpus(session=datetime(2024, 1, 2, tzinfo=UTC)),
            exploration_start=date(2024, 1, 1),
            exploration_end=date(2024, 1, 1),
        )
    with pytest.raises(CryptoStageBInputError, match="time_series"):
        source_from_labeled_corpus(
            _corpus(session=datetime(2024, 1, 1, tzinfo=UTC), consumer_intent="xsec"),
            exploration_start=date(2024, 1, 1),
            exploration_end=date(2024, 1, 1),
        )
