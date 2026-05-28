"""ROB-340 /invest 데이터 소스 계약 불변식 테스트.

이 테스트들이 계약의 "이빨"이다 (계약 모듈 자체는 선언적 데이터일 뿐).

- T2: authority-mixing guard (Toss/Naver는 primary/ranking 불가) + enum 유효성
  + 5개 surface 존재 + reports(frozen)/crypto 엔트리 + freshness_ttl=None 시드.
- T3: 양방향 drift guard — 계약의 collector-wired snapshot_kind 집합 ==
  production_collector_registry 등록 집합. collector를 추가/제거하면서 계약을
  안 고치거나, 계약이 존재하지 않는 collector를 주장하면 CI 실패.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services.action_report.snapshot_backed.collectors.registry import (
    production_collector_registry,
)
from app.services.invest_data_source_contract import (
    INVEST_DATA_SOURCE_CONTRACT,
    collector_wired_kinds,
    entries_for_surface,
    render_contract_matrix_markdown,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT_DOC = _REPO_ROOT / "docs" / "invest" / "data-source-contract.md"
_MATRIX_BEGIN = (
    "<!-- BEGIN GENERATED: data-source-matrix "
    "(rendered from app/services/invest_data_source_contract.py; "
    "do not hand-edit) -->"
)
_MATRIX_END = "<!-- END GENERATED: data-source-matrix -->"

# 계약이 허용하는 통제 어휘 (Literal과 일치해야 함 — 미래 loosening 방지).
_SURFACES = {"news", "screener", "stocks", "my", "reports"}
_AUTHORITY_TIERS = {"primary", "supplementary", "low_trust_attention"}
_FETCH_POLICIES = {
    "pre_collected",
    "report_time_on_demand",
    "never_request_path",
    "frozen_in_bundle",
}
_UNAVAILABLE_LABELS = {"확인 불가", "stale", "unavailable", "partial"}

# Toss/Naver 파생으로 간주하는 source 토큰 (authority guard 대상).
_LOW_TRUST_SOURCE_TOKENS = ("toss", "naver")


@pytest.mark.unit
class TestAuthorityMixingGuard:
    """ROB-323 교훈: Toss/Naver는 절대 KIS 권위를 덮지 못한다."""

    def test_toss_naver_sources_never_primary_or_ranking(self):
        """Toss/Naver 파생 source는 primary 금지 + may_affect_ranking False."""
        offenders = [
            e
            for e in INVEST_DATA_SOURCE_CONTRACT
            if any(tok in e.source_name.lower() for tok in _LOW_TRUST_SOURCE_TOKENS)
            and (e.authority_tier == "primary" or e.may_affect_ranking)
        ]
        assert offenders == [], (
            "Toss/Naver source가 primary이거나 ranking에 영향: "
            f"{[(e.surface, e.source_name) for e in offenders]}"
        )

    def test_low_trust_entries_never_affect_ranking(self):
        """low_trust_attention 티어는 buy/sell ranking에 영향 줄 수 없다."""
        offenders = [
            e
            for e in INVEST_DATA_SOURCE_CONTRACT
            if e.authority_tier == "low_trust_attention" and e.may_affect_ranking
        ]
        assert offenders == [], (
            f"low_trust_attention 엔트리가 ranking에 영향: "
            f"{[(e.surface, e.source_name) for e in offenders]}"
        )


@pytest.mark.unit
class TestContractValidity:
    """엔트리 구조/어휘 유효성 + surface 커버리지."""

    def test_all_five_surfaces_present(self):
        assert {e.surface for e in INVEST_DATA_SOURCE_CONTRACT} == _SURFACES

    def test_every_surface_has_at_least_one_entry(self):
        for surface in _SURFACES:
            assert entries_for_surface(surface), f"surface 엔트리 없음: {surface}"

    def test_enum_values_within_allowed_sets(self):
        for e in INVEST_DATA_SOURCE_CONTRACT:
            assert e.surface in _SURFACES
            assert e.authority_tier in _AUTHORITY_TIERS
            assert e.fetch_policy in _FETCH_POLICIES
            assert e.unavailable_label in _UNAVAILABLE_LABELS

    def test_freshness_ttl_none_or_positive_int(self):
        """deferred 카테고리는 None 시드(정책 lock, 값 TBD) — 유효해야 함."""
        for e in INVEST_DATA_SOURCE_CONTRACT:
            assert e.freshness_ttl is None or (
                isinstance(e.freshness_ttl, int) and e.freshness_ttl > 0
            ), f"잘못된 freshness_ttl: {e.source_name}={e.freshness_ttl}"

    def test_reports_surface_consumes_frozen_evidence(self):
        """investment_snapshots는 리포트 근거 freeze 전용 (frozen_in_bundle)."""
        frozen = [
            e
            for e in entries_for_surface("reports")
            if e.source_name == "investment_snapshots"
        ]
        assert len(frozen) == 1
        assert frozen[0].fetch_policy == "frozen_in_bundle"
        assert frozen[0].reusable_table == "investment_snapshots"

    def test_crypto_screener_entry_present(self):
        """crypto screener(upbit_live)가 screener surface에 존재 (review fix)."""
        crypto = [
            e
            for e in entries_for_surface("screener")
            if e.source_name == "upbit_live"
        ]
        assert len(crypto) == 1
        assert crypto[0].reusable_table == "invest_crypto_screener_snapshots"


@pytest.mark.unit
class TestDriftGuard:
    """T3: 계약 ↔ 런타임 collector registry 양방향 일치."""

    def test_collector_wired_kinds_match_runtime_registry(self):
        """계약의 collector-wired kind 집합 == 실제 등록된 snapshot_kind 집합.

        session은 등록 시점에 await되지 않으므로 MagicMock으로 충분.
        broker client는 자격증명 없이 None으로 안전하게 생성된다.
        """
        registry = production_collector_registry(MagicMock())
        runtime_kinds = registry.list_kinds()
        contract_kinds = collector_wired_kinds()

        assert contract_kinds == runtime_kinds, (
            "계약 ↔ 런타임 drift. "
            f"계약에만: {sorted(contract_kinds - runtime_kinds)}, "
            f"런타임에만: {sorted(runtime_kinds - contract_kinds)}"
        )

    def test_every_collector_kind_has_exactly_one_entry(self):
        """collector-wired snapshot_kind는 계약에서 중복되지 않는다."""
        wired = [
            e.collector_snapshot_kind
            for e in INVEST_DATA_SOURCE_CONTRACT
            if e.collector_snapshot_kind is not None
        ]
        assert len(wired) == len(set(wired)), f"중복 collector 엔트리: {wired}"


@pytest.mark.unit
class TestDocMatrixSync:
    """T4: 계약 문서의 matrix 표 == registry 직렬화 (doc↔code drift 차단)."""

    def test_doc_matrix_block_matches_registry(self):
        """문서의 GENERATED 블록이 render_contract_matrix_markdown()과 일치."""
        text = _CONTRACT_DOC.read_text(encoding="utf-8")
        assert _MATRIX_BEGIN in text, "doc에 matrix BEGIN 마커 없음"
        assert _MATRIX_END in text, "doc에 matrix END 마커 없음"

        block = text.split(_MATRIX_BEGIN, 1)[1].split(_MATRIX_END, 1)[0].strip()
        assert block == render_contract_matrix_markdown(), (
            "docs/invest/data-source-contract.md의 matrix 표가 registry와 drift. "
            "표는 손으로 고치지 말고 registry를 고친 뒤 재렌더할 것."
        )
