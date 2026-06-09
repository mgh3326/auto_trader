# ROB-415 report_quality_summary grade partial 강등 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `build_report_quality_summary`가 `candidate_universe`(매수 후보 소스)가 stale이고 usable external cross-check가 없을 때 집계 커버리지 %와 무관하게 `high_confidence`를 `informational_only`로 강등하도록 한다.

**Architecture:** 기존 강등 조건 `core_incomplete OR (thin_coverage AND no_cross_check)`에 `candidate_universe_non_fresh`를 thin_coverage와 동일한 rescue 게이트로 OR 추가. `candidate_universe` 상태는 기존 집계 루프에서 캡처(추가 순회 없음). ROB-323 외부 fail-open 보존, migration 0, read-only.

**Tech Stack:** Python 3.13, pytest. 순수 함수(`app/services/action_report/common/diagnostics.py`), DB/IO 없음 — 테스트는 dict in/out.

---

## File Structure

- Modify: `app/services/action_report/common/diagnostics.py` — `build_report_quality_summary` (그레이드 데모션 조건 + candidate_universe 상태 캡처)
- Test: `tests/services/action_report/common/test_diagnostics.py` — 신규 3 케이스

단일 함수 변경. 신규 파일 없음.

---

## Task 1: candidate_universe stale + no-cross-check 데모션 (repro)

**Files:**
- Modify: `app/services/action_report/common/diagnostics.py`
- Test: `tests/services/action_report/common/test_diagnostics.py`

배경: 현재 데모션은 집계 internal % (`thin_coverage`)에만 의존(`diagnostics.py:362`). `candidate_universe`가 단일 stale이어도 다른 optional이 fresh면 %가 ≥70으로 희석되어 강등 안 됨. repro: core fresh + candidate_universe stale + news/symbol fresh(%≥70) + 외부 unavailable → 잘못된 high_confidence.

- [ ] **Step 1: Write the failing test**

`tests/services/action_report/common/test_diagnostics.py` 끝(파일 마지막 테스트 뒤)에 추가. `build_report_quality_summary`는 파일 상단(라인 7-11)에서 이미 import됨:

```python
def test_quality_grade_demotes_when_candidate_universe_stale_no_cross_check() -> None:
    # ROB-415: candidate_universe (the buy-candidate source) is stale while other
    # optional kinds are fresh, so aggregate internal coverage stays >=70% and the
    # old thin_coverage rule never fired. With no usable external cross-check, a
    # stale candidate_universe must demote high_confidence → informational_only.
    out = build_report_quality_summary(
        freshness_summary={
            "overall": "partial",
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
            "news": {"status": "fresh"},
            "symbol": {"status": "fresh"},
            "invest_page": {"status": "fresh"},
            "candidate_universe": {"status": "soft_stale"},
            "toss_remote_debug": {"status": "unavailable"},  # external, no rescue
        },
        bundle_status="partial",
    )
    # Core fully fresh and internal coverage is high (7/8 ≈ 88%), so the old rule
    # left it high_confidence — the bug.
    assert out["core_fresh_coverage_pct"] == 100
    assert out["grade"] == "informational_only"
    assert out["external_cross_check_status"] == "unavailable"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-415 && uv run pytest tests/services/action_report/common/test_diagnostics.py::test_quality_grade_demotes_when_candidate_universe_stale_no_cross_check -v`
Expected: FAIL — `grade == "high_confidence"` (데모션 미발화).

- [ ] **Step 3: Write minimal implementation**

`app/services/action_report/common/diagnostics.py`:

(a) 집계 루프에서 `candidate_universe` 상태 캡처. 현재 루프(라인 307-322)는:

```python
    summary = freshness_summary or {}
    counts: dict[str, int] = {}
    critical_statuses: list[str | None] = []
    core_fresh = core_total = 0
    optional_fresh = optional_total = 0
    for kind, info in summary.items():
        if kind == "overall" or not isinstance(info, Mapping):
            continue
        status = info.get("status")
        counts[str(status)] = counts.get(str(status), 0) + 1
        if kind in EXTERNAL_AUDIT_KINDS:
            continue  # surfaced via external_cross_check_status, not coverage
        if kind in CRITICAL_SNAPSHOT_KINDS:
            critical_statuses.append(status)
            core_total += 1
            if status == "fresh":
                core_fresh += 1
        else:
            optional_total += 1
            if status == "fresh":
                optional_fresh += 1
```

이것을 다음으로 교체(루프 앞에 `candidate_universe` 캡처 변수 2개 추가, 루프 안에서 세팅):

```python
    summary = freshness_summary or {}
    counts: dict[str, int] = {}
    critical_statuses: list[str | None] = []
    core_fresh = core_total = 0
    optional_fresh = optional_total = 0
    candidate_universe_present = False
    candidate_universe_status: str | None = None
    for kind, info in summary.items():
        if kind == "overall" or not isinstance(info, Mapping):
            continue
        status = info.get("status")
        counts[str(status)] = counts.get(str(status), 0) + 1
        if kind in EXTERNAL_AUDIT_KINDS:
            continue  # surfaced via external_cross_check_status, not coverage
        if kind in CRITICAL_SNAPSHOT_KINDS:
            critical_statuses.append(status)
            core_total += 1
            if status == "fresh":
                core_fresh += 1
        else:
            optional_total += 1
            if status == "fresh":
                optional_fresh += 1
            if kind == "candidate_universe":
                candidate_universe_present = True
                candidate_universe_status = status
```

(b) 데모션 조건(라인 353-363)을 교체. 현재:

```python
        core_incomplete = core_total > 0 and core_fresh < core_total
        thin_coverage = internal_pct < HIGH_CONFIDENCE_MIN_COVERAGE_PCT
        # A cross-check only corroborates when it is present and not itself
        # degrading — a hard_stale/unavailable/failed probe is stale-expired
        # evidence and must not rescue thin coverage.
        no_cross_check = (
            external_status is None
            or external_status in CRITICAL_KIND_DEGRADING_STATUSES
        )
        if core_incomplete or (thin_coverage and no_cross_check):
            grade = "informational_only"
```

교체:

```python
        core_incomplete = core_total > 0 and core_fresh < core_total
        thin_coverage = internal_pct < HIGH_CONFIDENCE_MIN_COVERAGE_PCT
        # ROB-415 — candidate_universe is the buy-candidate source: a stale one
        # degrades the report's core purpose even when other optional kinds keep
        # aggregate coverage above the thin threshold. Gated like thin_coverage
        # (a usable cross-check can still rescue), so ROB-323's external fail-open
        # holds: an un-run external probe alone never demotes.
        candidate_universe_non_fresh = (
            candidate_universe_present and candidate_universe_status != "fresh"
        )
        # A cross-check only corroborates when it is present and not itself
        # degrading — a hard_stale/unavailable/failed probe is stale-expired
        # evidence and must not rescue thin coverage.
        no_cross_check = (
            external_status is None
            or external_status in CRITICAL_KIND_DEGRADING_STATUSES
        )
        if core_incomplete or (
            (thin_coverage or candidate_universe_non_fresh) and no_cross_check
        ):
            grade = "informational_only"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/mgh3326/work/auto_trader.rob-415 && uv run pytest tests/services/action_report/common/test_diagnostics.py::test_quality_grade_demotes_when_candidate_universe_stale_no_cross_check -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-415
git add app/services/action_report/common/diagnostics.py tests/services/action_report/common/test_diagnostics.py
git commit -m "fix(ROB-415): candidate_universe stale + no cross-check면 grade 강등

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: candidate_universe stale라도 passing cross-check면 high 유지 (rescue 가드)

**Files:**
- Test: `tests/services/action_report/common/test_diagnostics.py`

ROB-323 보존: usable external cross-check(fresh/soft_stale 등 비-degrading)가 있으면 candidate_universe stale이어도 rescue되어 high_confidence 유지. Task 1 구현의 `no_cross_check` 게이트가 이를 보장(회귀 가드).

- [ ] **Step 1: Write the failing test**

```python
def test_quality_grade_candidate_universe_stale_rescued_by_cross_check() -> None:
    # ROB-415 / ROB-323: a usable external cross-check still corroborates a stale
    # candidate_universe, so the bundle stays high_confidence. Guards the demotion
    # from over-firing when there IS fresh external evidence.
    out = build_report_quality_summary(
        freshness_summary={
            "overall": "partial",
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
            "news": {"status": "fresh"},
            "symbol": {"status": "fresh"},
            "invest_page": {"status": "fresh"},
            "candidate_universe": {"status": "soft_stale"},
            "toss_remote_debug": {"status": "fresh"},  # usable cross-check
        },
        bundle_status="partial",
    )
    assert out["grade"] == "high_confidence"
    assert out["external_cross_check_status"] == "fresh"
```

- [ ] **Step 2: Run test to verify it passes**

Run: `cd /Users/mgh3326/work/auto_trader.rob-415 && uv run pytest tests/services/action_report/common/test_diagnostics.py::test_quality_grade_candidate_universe_stale_rescued_by_cross_check -v`
Expected: PASS (Task 1의 `no_cross_check=False` → 데모션 미발화).

- [ ] **Step 3: (구현 변경 없음 — Task 1로 충족)**

- [ ] **Step 4: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-415
git add tests/services/action_report/common/test_diagnostics.py
git commit -m "test(ROB-415): candidate_universe stale라도 passing cross-check면 high 유지

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: candidate_universe fresh면 데모션 미발화 (over-fire 가드)

**Files:**
- Test: `tests/services/action_report/common/test_diagnostics.py`

candidate_universe가 fresh면 `candidate_universe_non_fresh=False` → 데모션 미발화, high_confidence 유지. 데모션이 candidate_universe 존재만으로 발화하지 않음을 가드.

- [ ] **Step 1: Write the failing test**

```python
def test_quality_grade_candidate_universe_fresh_stays_high() -> None:
    # candidate_universe present and fresh must NOT trigger the ROB-415 demotion.
    out = build_report_quality_summary(
        freshness_summary={
            "overall": "partial",
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
            "news": {"status": "fresh"},
            "candidate_universe": {"status": "fresh"},
            "toss_remote_debug": {"status": "unavailable"},  # external, no rescue
        },
        bundle_status="partial",
    )
    assert out["grade"] == "high_confidence"
```

- [ ] **Step 2: Run test to verify it passes**

Run: `cd /Users/mgh3326/work/auto_trader.rob-415 && uv run pytest tests/services/action_report/common/test_diagnostics.py::test_quality_grade_candidate_universe_fresh_stays_high -v`
Expected: PASS.

- [ ] **Step 3: (구현 변경 없음 — Task 1로 충족)**

- [ ] **Step 4: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-415
git add tests/services/action_report/common/test_diagnostics.py
git commit -m "test(ROB-415): candidate_universe fresh면 데모션 미발화 가드

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: 전체 회귀 + lint 검증

**Files:**
- (없음 — 검증만)

- [ ] **Step 1: diagnostics 테스트 전체 (기존 회귀 무변경 확인)**

Run: `cd /Users/mgh3326/work/auto_trader.rob-415 && uv run pytest tests/services/action_report/common/test_diagnostics.py -v`
Expected: PASS — 신규 3 + 기존 전부 green. 특히 `test_quality_summary_splits_core_optional_external_coverage`(high_confidence 유지), `test_quality_grade_thin_coverage_stays_high_with_soft_stale_cross_check`(high_confidence 유지) 무변경.

- [ ] **Step 2: grade 소비자 회귀 (hermes_context / snapshot metadata)**

Run: `cd /Users/mgh3326/work/auto_trader.rob-415 && uv run pytest tests/services/investment_stages/test_hermes_context.py tests/test_investment_reports_snapshot_metadata.py -q`
Expected: PASS — grade 소비자 무영향.

- [ ] **Step 3: Lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-415 && uv run ruff check app/services/action_report/common/diagnostics.py && uv run ruff format --check app/services/action_report/common/diagnostics.py`
Expected: All checks passed / already formatted.

- [ ] **Step 4: docstring 갱신 + commit**

`build_report_quality_summary` docstring(라인 287-296 부근)의 grade 설명에 ROB-415 데모션을 한 줄 반영. 현재 `informational_only` 불릿:

```python
    * ``informational_only`` — a critical kind is degrading, OR (ROB-366 B10)
      the core is not fully fresh, OR internal coverage is below
      ``HIGH_CONFIDENCE_MIN_COVERAGE_PCT`` with no usable external cross-check to
      corroborate (a degrading cross-check does not count).
```

교체:

```python
    * ``informational_only`` — a critical kind is degrading, OR (ROB-366 B10)
      the core is not fully fresh, OR internal coverage is below
      ``HIGH_CONFIDENCE_MIN_COVERAGE_PCT``, OR (ROB-415) ``candidate_universe``
      (the buy-candidate source) is non-fresh — the latter two only when no usable
      external cross-check corroborates (a degrading cross-check does not count).
```

그 후:

```bash
cd /Users/mgh3326/work/auto_trader.rob-415
uv run ruff format app/services/action_report/common/diagnostics.py
git add app/services/action_report/common/diagnostics.py
git commit -m "docs(ROB-415): grade docstring에 candidate_universe 데모션 반영

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review 결과

**Spec 커버리지:**
- 데모션 규칙(candidate_universe_non_fresh OR thin_coverage, no_cross_check 게이트) → Task 1 ✅
- ROB-323 rescue 보존 → Task 2 ✅
- candidate fresh over-fire 가드 → Task 3 ✅
- 기존 회귀 무변경 + 소비자 + lint + docstring → Task 4 ✅

**Placeholder 스캔:** 없음 — 모든 코드 step에 실제 코드 포함.

**Type 일관성:** `candidate_universe_present: bool`, `candidate_universe_status: str | None`, `candidate_universe_non_fresh: bool` 변수명 Task 1 내에서 일관. 기존 `no_cross_check`/`thin_coverage`/`core_incomplete` 재사용. 시그니처/반환 dict 키 무변경(`grade`/`external_cross_check_status` 등 그대로).

**안전 경계 재확인:** grade는 display/audit 메타데이터(백엔드 게이팅 안 함), 강등 단방향, migration 0, broker/order/watch/order-intent mutation 없음, read-only. fundamentals/sentiment per-symbol 입력 미추가(scope 고정).
