# ROB-472 — Claude lite 리포트 결정적 품질 grade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Claude advisory lite 리포트(`investment_report_create`)가 P1 per-item evidence에서 파생한 결정적 품질 `report_quality_summary`를 갖게 한다 — `high_confidence`는 구조적으로 불가(정직), advisory 프로파일만, additive·migration 0.

**Architecture:** 순수 헬퍼 `build_lite_report_quality_summary(items)`가 item evidence/freshness에서 2-레벨 grade(`no_action`|`informational_only`)를 산출. 핸들러 글루 `_maybe_attach_lite_quality(request)`가 advisory 프로파일일 때만(그리고 caller가 안 넘겼을 때만) `snapshot_report_diagnostics`에 주입. 기존 optional 필드라 schema/service/DB 변경 0.

**Tech Stack:** Python 3.13, Pydantic v2, FastMCP, SQLAlchemy async, pytest. 스펙: `docs/superpowers/specs/2026-06-09-rob472-lite-report-grade-design.md`. 워크트리: `auto_trader.rob-472` (branch `rob-472`, off main `74dee6ee`).

**전 슬라이스 제약:** broker/order/watch mutation 0 · migration 0 · schema 필드 추가 0(`snapshot_report_diagnostics` 이미 존재). 1 PR.

---

## 파일 맵
- Create: `app/services/investment_reports/lite_grade.py` — 순수 grade 헬퍼.
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py` — `_maybe_attach_lite_quality` 글루 + `create_impl`(:420 직후) 1줄 배선.
- Test: `tests/test_investment_report_lite_grade.py` (신규, 헬퍼 + 글루 DB-free), `tests/test_investment_report_item_evidence.py` (DB round-trip 1개 추가) 또는 신규 round-trip 테스트.

---

### Task 1: 순수 헬퍼 `build_lite_report_quality_summary`

**Files:**
- Create: `app/services/investment_reports/lite_grade.py`
- Test: `tests/test_investment_report_lite_grade.py`

- [ ] **Step 1: 실패 테스트 작성**

신규 `tests/test_investment_report_lite_grade.py`:

```python
"""ROB-472 — lite report quality grade 순수 헬퍼."""

from __future__ import annotations

import pytest

from app.schemas.investment_reports import IngestReportItem
from app.services.investment_reports.lite_grade import (
    build_lite_report_quality_summary,
)

pytestmark = pytest.mark.unit


def _item(**over):
    base = dict(
        client_item_key="k1",
        item_kind="action",
        intent="buy_review",
        rationale="r",
    )
    base.update(over)
    return IngestReportItem(**base)


def test_no_actionable_items_grades_no_action():
    items = [_item(item_kind="risk", intent="risk_review")]
    out = build_lite_report_quality_summary(items)
    assert out["grade"] == "no_action"
    assert out["basis"] == "item_evidence_lite"


def test_actionable_but_no_evidence_grades_no_action():
    items = [_item()]  # action item, evidence=[] (default)
    out = build_lite_report_quality_summary(items)
    assert out["grade"] == "no_action"
    assert "evidence" in out["reason"]


def test_evidence_backed_action_grades_informational_only():
    items = [
        _item(
            evidence=[{"source": "consensus", "freshness": "fresh"}],
            freshness="fresh",
        )
    ]
    out = build_lite_report_quality_summary(items)
    assert out["grade"] == "informational_only"
    assert out["evidence_item_count"] == 1
    assert out["actionable_item_count"] == 1


def test_never_returns_high_confidence_even_with_rich_evidence():
    items = [
        _item(
            client_item_key=f"k{i}",
            evidence=[
                {"source": "consensus", "freshness": "fresh"},
                {"source": "foreign_flow", "freshness": "fresh"},
            ],
            freshness="fresh",
        )
        for i in range(5)
    ]
    out = build_lite_report_quality_summary(items)
    assert out["grade"] != "high_confidence"
    assert out["grade"] == "informational_only"


def test_freshness_breakdown_counts_item_and_evidence():
    items = [
        _item(
            evidence=[
                {"source": "a", "freshness": "fresh"},
                {"source": "b", "freshness": "stale"},
            ],
            freshness="soft_stale",
        )
    ]
    out = build_lite_report_quality_summary(items)
    # item.freshness(soft_stale) + evidence(fresh, stale)
    assert out["freshness_breakdown"] == {
        "fresh": 1,
        "soft_stale": 1,
        "stale": 1,
        "unknown": 0,
    }
    assert out["evidence_source_count"] == 2


def test_empty_items_grades_no_action():
    out = build_lite_report_quality_summary([])
    assert out["grade"] == "no_action"
    assert out["total_item_count"] == 0
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_investment_report_lite_grade.py -p no:randomly -q`
Expected: FAIL — `ModuleNotFoundError: app.services.investment_reports.lite_grade`

- [ ] **Step 3: 헬퍼 구현**

신규 `app/services/investment_reports/lite_grade.py`:

```python
"""ROB-472 — deterministic lite quality grade for Claude advisory reports.

The snapshot-backed generator grades reports from snapshot bundle coverage
(build_report_quality_summary). The lite create path has no snapshot bundle, so
this derives an HONEST grade from the per-item structured evidence shipped in
ROB-459 P1. By construction it NEVER returns high_confidence — a lite report
lacks the snapshot coverage that grade is defined around — so an evidence-thin
report can never masquerade as snapshot-backed high confidence.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.schemas.investment_reports import IngestReportItem

_LITE_BASIS = "item_evidence_lite"
_ACTIONABLE_KINDS = frozenset({"action", "watch"})
_FRESHNESS_VALUES = ("fresh", "soft_stale", "stale", "unknown")


def build_lite_report_quality_summary(
    items: list[IngestReportItem],
) -> dict[str, Any]:
    """Derive a lite report_quality_summary from per-item evidence/freshness.

    Grade is capped at ``informational_only`` (never ``high_confidence``):
    - ``no_action``: no actionable (action|watch) items, OR no item carries any
      structured evidence — genuinely insufficient to advise.
    - ``informational_only``: otherwise — an evidence-backed lite advisory.

    ``freshness_breakdown`` counts both item-level ``freshness`` and each
    evidence row's ``freshness`` (None values are not counted).
    """
    total_item_count = len(items)
    actionable_item_count = sum(
        1 for it in items if it.item_kind in _ACTIONABLE_KINDS
    )
    evidence_item_count = sum(1 for it in items if it.evidence)

    sources: set[str] = set()
    freshness_counter: Counter[str] = Counter()
    for it in items:
        if it.freshness is not None:
            freshness_counter[it.freshness] += 1
        for ev in it.evidence:
            sources.add(ev.source)
            if ev.freshness is not None:
                freshness_counter[ev.freshness] += 1

    if actionable_item_count == 0 or evidence_item_count == 0:
        grade = "no_action"
        reason = (
            "no actionable (action|watch) items"
            if actionable_item_count == 0
            else "no structured evidence on any item"
        )
    else:
        grade = "informational_only"
        reason = "evidence-backed lite advisory (no snapshot coverage)"

    return {
        "grade": grade,
        "basis": _LITE_BASIS,
        "reason": reason,
        "total_item_count": total_item_count,
        "actionable_item_count": actionable_item_count,
        "evidence_item_count": evidence_item_count,
        "evidence_source_count": len(sources),
        "freshness_breakdown": {
            k: freshness_counter.get(k, 0) for k in _FRESHNESS_VALUES
        },
    }
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_investment_report_lite_grade.py -p no:randomly -q`
Expected: PASS (6 tests)

- [ ] **Step 5: 커밋**

```bash
git add app/services/investment_reports/lite_grade.py tests/test_investment_report_lite_grade.py
git commit -m "feat(ROB-472): lite report quality grade 순수 헬퍼 (high_confidence 불가)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: 핸들러 글루 `_maybe_attach_lite_quality` + create_impl 배선

**Files:**
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py` (`:420` 직후 배선 + 글루 함수 추가)
- Test: `tests/test_investment_report_lite_grade.py` (append, DB-free)

- [ ] **Step 1: 글루 테스트 작성 (append)**

`tests/test_investment_report_lite_grade.py`에 append:

```python
from app.mcp_server.tooling import investment_reports_handlers as h
from app.schemas.investment_reports import IngestReportRequest


def _request(profile="CLAUDE_ADVISOR", **over):
    base = dict(
        report_type="advisory_lite_v1",
        market="kr",
        created_by_profile=profile,
        title="t",
        summary="s",
        kst_date="2026-06-09",
        status="draft",
        items=[
            _item(
                evidence=[{"source": "consensus", "freshness": "fresh"}],
                freshness="fresh",
            )
        ],
    )
    base.update(over)
    return IngestReportRequest(**base)


def test_attach_lite_quality_advisory_profile_populates():
    out = h._maybe_attach_lite_quality(_request(profile="CLAUDE_ADVISOR"))
    rqs = out.snapshot_report_diagnostics["report_quality_summary"]
    assert rqs["grade"] == "informational_only"
    assert rqs["basis"] == "item_evidence_lite"


def test_attach_lite_quality_non_advisory_profile_skips():
    out = h._maybe_attach_lite_quality(_request(profile="t"))
    assert out.snapshot_report_diagnostics is None


def test_attach_lite_quality_does_not_clobber_caller_diagnostics():
    caller = {"report_quality_summary": {"grade": "no_action", "basis": "caller"}}
    out = h._maybe_attach_lite_quality(
        _request(profile="CLAUDE_ADVISOR", snapshot_report_diagnostics=caller)
    )
    assert out.snapshot_report_diagnostics == caller
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_investment_report_lite_grade.py -k attach_lite -p no:randomly -q`
Expected: FAIL — `AttributeError: ... has no attribute '_maybe_attach_lite_quality'`

- [ ] **Step 3: 글루 함수 추가 + import**

`app/mcp_server/tooling/investment_reports_handlers.py` 상단 import 영역에 추가:

```python
from app.services.investment_reports.lite_grade import (
    build_lite_report_quality_summary,
)
from app.services.investment_reports.query_service import (
    _advisory_draft_profiles,
)
```

(주의: `query_service`는 이미 다른 심볼을 import 중일 수 있음 — 기존 `from app.services.investment_reports.query_service import InvestmentReportQueryService` 라인에 `_advisory_draft_profiles`를 합치거나 별도 import 추가. `git grep "from app.services.investment_reports.query_service import"`로 확인 후 병합.)

그리고 `investment_report_create_impl` 정의 **위**(또는 인접 헬퍼 영역)에 글루 함수 추가:

```python
def _maybe_attach_lite_quality(
    request: IngestReportRequest,
) -> IngestReportRequest:
    """ROB-472 — attach a deterministic lite quality grade to advisory reports.

    Pure metadata (grade gates nothing). Only for advisory profiles; never
    clobbers caller-supplied diagnostics; fail-open (a helper error never blocks
    report creation). snapshot_freshness_summary/coverage_summary stay None so
    the published-report DB CHECK is never triggered.
    """
    if request.snapshot_report_diagnostics is not None:
        return request
    if request.created_by_profile not in _advisory_draft_profiles():
        return request
    try:
        summary = build_lite_report_quality_summary(request.items)
    except Exception:
        return request
    return request.model_copy(
        update={"snapshot_report_diagnostics": {"report_quality_summary": summary}}
    )
```

- [ ] **Step 4: 통과 확인 (글루 단위)**

Run: `uv run pytest tests/test_investment_report_lite_grade.py -p no:randomly -q`
Expected: PASS (9 tests)

- [ ] **Step 5: create_impl 배선 (1줄)**

`investment_report_create_impl`에서 `request = IngestReportRequest.model_validate(payload)`(현재 `:420`) **직후** 1줄 추가:

```python
    request = IngestReportRequest.model_validate(payload)
    # ROB-472 — advisory lite reports get a deterministic, evidence-derived
    # quality grade (display/audit metadata only). No-op for non-advisory
    # profiles and when the caller already supplied diagnostics.
    request = _maybe_attach_lite_quality(request)
```

- [ ] **Step 6: 핸들러 회귀 확인**

Run: `uv run pytest tests/mcp_server/test_investment_report_create_handler.py tests/test_investment_report_lite_grade.py -p no:randomly -q`
Expected: PASS (기존 create 핸들러 테스트 무회귀 + 신규 9)

- [ ] **Step 7: ruff + 커밋**

Run: `uv run ruff check app/mcp_server/tooling/investment_reports_handlers.py app/services/investment_reports/lite_grade.py tests/test_investment_report_lite_grade.py`
Expected: All checks passed!

```bash
git add app/mcp_server/tooling/investment_reports_handlers.py tests/test_investment_report_lite_grade.py
git commit -m "feat(ROB-472): advisory lite create에 lite quality grade 주입(글루)

advisory 프로파일만, caller diagnostics 미clobber, fail-open. snapshot_*는
freshness/coverage 미설정(DB CHECK 안전).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: DB round-trip 영속화 테스트

**Files:**
- Test: `tests/test_investment_report_lite_grade.py` (append, DB)

- [ ] **Step 1: round-trip 테스트 작성 (append)**

`tests/test_investment_report_lite_grade.py`에 append:

```python
@pytest.mark.asyncio
async def test_lite_diagnostics_persists_and_round_trips(session) -> None:
    """글루가 붙인 lite grade가 저장되고 ORM으로 round-trip된다."""
    from app.services.investment_reports.ingestion import (
        InvestmentReportIngestionService,
    )
    from app.services.investment_reports.repository import (
        InvestmentReportsRepository,
    )

    request = h._maybe_attach_lite_quality(_request(profile="CLAUDE_ADVISOR"))
    repo = InvestmentReportsRepository(session)
    svc = InvestmentReportIngestionService(session, repository=repo)
    report = await svc.ingest(request)
    await session.flush()

    diag = report.snapshot_report_diagnostics
    assert diag is not None
    assert diag["report_quality_summary"]["grade"] == "informational_only"
    assert diag["report_quality_summary"]["basis"] == "item_evidence_lite"
```

(주의: 기존 `tests/test_investment_report_item_evidence.py`의 DB round-trip 테스트와 동일한 `session` 픽스처 사용 — 동작 확인됨. xdist 충돌 회피가 필요하면 그 파일의 관례를 따른다. `status="draft"`라 published freshness CHECK 미적용.)

- [ ] **Step 2: 통과 확인**

Run: `uv run pytest tests/test_investment_report_lite_grade.py -p no:randomly -q`
Expected: PASS (10 tests)

- [ ] **Step 3: 슬라이스 전체 검증 + 커밋**

Run: `uv run pytest tests/test_investment_report_lite_grade.py tests/mcp_server/test_investment_report_create_handler.py tests/test_investment_report_item_evidence.py -p no:randomly -q`
Run: `uv run ruff check $(git diff --name-only origin/main...HEAD)`
Expected: 전체 PASS, ruff clean.

```bash
git add tests/test_investment_report_lite_grade.py
git commit -m "test(ROB-472): lite grade DB round-trip 영속화 검증

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

→ PR 생성 준비 완료.

---

## Self-Review (스펙 대비)

**1. Spec coverage**
- 스펙 §3-1(순수 헬퍼 + grade 규칙) → Task 1. ✅ no_action/informational_only 경계, high_confidence 부재, freshness_breakdown(item+evidence), source_count, basis 모두 테스트.
- 스펙 §3-2(배선 + advisory-only + clobber 금지 + fail-open + freshness 미설정) → Task 2. ✅
- 스펙 §3-3/3-4(데이터 흐름 + 에러 처리) → Task 2 글루(fail-open try/except). ✅
- 스펙 §4(테스트: 헬퍼/핸들러/round-trip) → Task 1/2/3. ✅
- 스펙 §5(additive/migration0/mutation0/no-flag) → 코드에 schema/DB/flag 변경 없음. ✅

**2. Placeholder scan** — 모든 스텝 실제 코드/명령/기대출력 포함. TBD/TODO 없음. ✅

**3. Type consistency**
- `build_lite_report_quality_summary(items) -> dict` — Task 1 정의, Task 2 글루에서 동일 호출. ✅
- `_maybe_attach_lite_quality(request) -> IngestReportRequest` — Task 2 정의·사용·테스트 일관. ✅
- 반환 dict 키(`grade`/`basis`/`reason`/`*_count`/`freshness_breakdown`) — Task 1 정의, Task 2/3 단언에서 동일. ✅
- `snapshot_report_diagnostics = {"report_quality_summary": summary}` — 글루·round-trip·핸들러 단언 일관. ✅

**열린 실행-시 확인(차단 아님):** query_service import 라인 병합(기존 import 존재 시); DB 테스트 xdist 픽스처 관례.

---

## Execution Handoff
(상위 세션에서 안내)
