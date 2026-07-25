from sqlalchemy import Index

from app.models.review import KISMockOrderLedger


def test_kis_mock_mirror_report_item_has_unique_partial_index():
    indexes = {
        idx.name: idx
        for idx in KISMockOrderLedger.__table__.indexes
        if isinstance(idx, Index)
    }
    idx = indexes["ux_kis_mock_mirror_report_item_once"]
    assert idx.unique is True
    assert tuple(col.name for col in idx.columns) == (
        "mirror_cohort",
        "report_item_uuid",
    )
    where = str(idx.dialect_options["postgresql"]["where"])
    assert "mock_counterfactual" in where
    assert "report_item_uuid IS NOT NULL" in where
