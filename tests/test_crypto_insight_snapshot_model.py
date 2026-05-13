from app.models import CryptoInsightSnapshot


def test_crypto_insight_snapshot_model_shape():
    assert CryptoInsightSnapshot.__tablename__ == "crypto_insight_snapshots"
    columns = CryptoInsightSnapshot.__table__.columns
    for name in (
        "metric",
        "provider",
        "symbol",
        "value",
        "unit",
        "label",
        "snapshot_at",
        "source_url",
        "freshness_seconds",
        "raw_payload",
    ):
        assert name in columns
    index_names = {index.name for index in CryptoInsightSnapshot.__table__.indexes}
    assert "ix_crypto_insight_snapshots_metric_at" in index_names
    assert "uq_crypto_insight_snapshots_global_identity" in index_names
    assert "uq_crypto_insight_snapshots_symbol_identity" in index_names
