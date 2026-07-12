import datetime as dt

import pytest
from pydantic import ValidationError

from app.schemas.analysis_snapshot_bundle import (
    ANALYSIS_SECTION_NAMES,
    AnalysisBundleCreateRequest,
    AnalysisFrozenDocument,
    AnalysisSection,
)


def test_analysis_section_names_are_stable() -> None:
    assert ANALYSIS_SECTION_NAMES == (
        "portfolio",
        "quotes_orderbooks",
        "indicators_support_resistance",
        "market_gate_inputs",
        "investor_flow",
        "decision_history",
    )


def test_frozen_document_requires_every_section() -> None:
    now = dt.datetime(2026, 7, 12, tzinfo=dt.UTC)
    section = AnalysisSection(
        status="ok",
        collected_at=now,
        as_of=now,
        source={"service": "test"},
        soft_ttl_seconds=60,
        hard_ttl_seconds=180,
        data={"x": 1},
    )
    with pytest.raises(ValidationError):
        AnalysisFrozenDocument(
            captured_at=now,
            request=AnalysisBundleCreateRequest(
                market="kr", account_scope="kis_live", symbols=["005930"]
            ),
            sections={"portfolio": section},
        )


def test_unavailable_section_keeps_original_error() -> None:
    now = dt.datetime(2026, 7, 12, tzinfo=dt.UTC)
    section = AnalysisSection(
        status="unavailable",
        collected_at=now,
        as_of=now,
        source={"provider": "breadth"},
        soft_ttl_seconds=180,
        hard_ttl_seconds=300,
        error="RuntimeError: provider off",
    )
    assert section.data is None
    assert section.error == "RuntimeError: provider off"


def test_create_request_rejects_empty_symbols() -> None:
    with pytest.raises(ValidationError):
        AnalysisBundleCreateRequest(
            market="crypto", account_scope="upbit_live", symbols=[]
        )
