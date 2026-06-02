"""ROB-359 Scope A — doc-sync guard for the Toss parity gap matrix.

The matrix lives in docs/invest-screener-toss-parity-matrix.md. These tests keep
it honest against code so a preset cannot be added/removed without the matrix
noticing, and so the 11 Toss baseline presets stay enumerated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.invest_view_model.screener_presets import SCREENER_PRESETS

_MATRIX_DOC = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "invest-screener-toss-parity-matrix.md"
)

# The 11 toss증권 baseline presets per ROB-359 (2026-05-29 browser capture).
_TOSS_BASELINE_PRESETS = (
    "연속 상승세",
    "저평가 성장주",
    "아직 저렴한 가치주",
    "꾸준한 배당주",
    "돈 잘버는 회사 찾기",
    "저평가 탈출",
    "미래의 배당왕 찾기",
    "성장 기대주",
    "쌍끌이 매수",
    "고수익 저평가",
    "안정 성장주",
)


@pytest.fixture(scope="module")
def matrix_text() -> str:
    assert _MATRIX_DOC.exists(), f"missing parity matrix doc: {_MATRIX_DOC}"
    return _MATRIX_DOC.read_text(encoding="utf-8")


def test_all_kr_presets_present_in_matrix(matrix_text: str) -> None:
    """Every KR preset id in code must appear in the matrix so none drifts
    out of the gap analysis silently."""
    kr_ids = [p.id for p in SCREENER_PRESETS if p.market == "kr"]
    # Guard the count the matrix was written against (9 KR presets originally,
    # plus high_yield_value in ROB-359 PR4, and 4 fundamentals presets in ROB-422).
    assert len(kr_ids) == 13, kr_ids
    missing = [pid for pid in kr_ids if pid not in matrix_text]
    assert not missing, f"preset ids absent from parity matrix: {missing}"


def test_all_toss_baseline_presets_enumerated(matrix_text: str) -> None:
    """All 11 Toss baseline preset names must be enumerated in the matrix."""
    missing = [name for name in _TOSS_BASELINE_PRESETS if name not in matrix_text]
    assert not missing, f"Toss baseline presets absent from matrix: {missing}"


def test_already_implemented_presets_marked_full(matrix_text: str) -> None:
    """ROB-170/276 parity work means consecutive_gainers and double_buy are
    treated as implemented (full), not re-investigated."""
    for done_id in ("consecutive_gainers", "double_buy"):
        assert done_id in matrix_text
    assert "**full**" in matrix_text
