"""ROB-473 — report_item_uuid 컬럼이 라이브 ledger 모델에 존재."""

from __future__ import annotations

import pytest

from app.models.review import KISLiveOrderLedger, LiveOrderLedger

pytestmark = pytest.mark.unit


def test_kis_live_ledger_has_report_item_uuid_column():
    assert "report_item_uuid" in KISLiveOrderLedger.__table__.columns
    col = KISLiveOrderLedger.__table__.columns["report_item_uuid"]
    assert col.nullable is True


def test_live_ledger_has_report_item_uuid_column():
    assert "report_item_uuid" in LiveOrderLedger.__table__.columns
    assert LiveOrderLedger.__table__.columns["report_item_uuid"].nullable is True
