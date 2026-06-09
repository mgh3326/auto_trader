# ROB-347 — US 리포트 신규매수 예산·환전 전제 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** US kis_live 후보에 예산 전제 정책을 적용한다. `available_usd` 기본 basis에서 USD buying power가 0이어도 후보는 발굴하되, `buy_review` 를 `watch_only` 로 강등하고 `budget_gap`/`fx_required`/`operator_budget_required` 를 명시한다. KRW는 reference로만(USD로 날조 금지).

**Architecture:** PR-B와 동일하게 `classify_candidate_symbol` signature 불변. 순수 헬퍼 `demote_for_budget` 를 추가해 auto_emit candidate 루프에서 **품질 데모션 다음, count-cap 이전**에 적용. 예산 신호는 portfolio snapshot의 `buying_power`(이미 적재됨) + request의 basis/override. migration-0.

**Tech Stack:** Python 3.13, async SQLAlchemy, pytest. 스펙: `docs/superpowers/specs/2026-06-09-rob-347-us-budget-fx-basis-design.md`. **선행: PR-B(ROB-346) 머지 후 origin/main 기준으로 시작** (같은 candidate 루프 수정).

---

## File Structure
- Modify: `app/services/action_report/snapshot_backed/request.py` — `budget_basis` + `operator_budget_override_usd`.
- Modify: `app/services/action_report/snapshot_backed/action_verdict.py` — `demote_for_budget` 순수 헬퍼.
- Modify: `app/services/action_report/snapshot_backed/auto_emit.py` — propose budget 파라미터, budget_state, 데모션, evidence surface.
- Modify: `app/services/action_report/snapshot_backed/generator.py:594-598` — request budget을 propose로 전달.
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py` — 도구에 budget 파라미터 노출 → request.
- Tests: 대응 신규/확장.

---

## Task 1: request 예산 필드

**Files:**
- Modify: `app/services/action_report/snapshot_backed/request.py`
- Test: `tests/services/action_report/snapshot_backed/test_request_budget.py`

- [ ] **Step 1: 실패 테스트**

`tests/services/action_report/snapshot_backed/test_request_budget.py`:
```python
from decimal import Decimal

from app.services.action_report.snapshot_backed.request import ReportGenerationRequest


def _base(**kw):
    return ReportGenerationRequest(
        market="us", account_scope="kis_live", created_by_profile="p",
        title="t", summary="s", kst_date="2026-06-09", **kw,
    )


def test_budget_basis_defaults_available_usd():
    assert _base().budget_basis == "available_usd"
    assert _base().operator_budget_override_usd is None


def test_budget_override_accepts_decimal():
    r = _base(budget_basis="operator_budget_override",
              operator_budget_override_usd=Decimal("1000"))
    assert r.operator_budget_override_usd == Decimal("1000")
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_request_budget.py -v`
Expected: FAIL — 필드/Decimal import 없음(extra="forbid"로 거부).

- [ ] **Step 3: 구현** — `request.py` 상단 import에 `from decimal import Decimal` 추가.
`candidate_limit: int | None = None` (line 64) 아래에 추가:
```python
    # ROB-347 — US new-buy budget basis policy. Default available_usd: USD
    # buying power gates buy_review. USD<=0 demotes buy_review → watch_only with
    # budget_gap/fx_required/operator_budget_required (never a silent buy). KRW
    # is reference-only; no KRW→USD fabrication.
    budget_basis: Literal[
        "available_usd", "krw_orderable_reference", "operator_budget_override"
    ] = "available_usd"
    operator_budget_override_usd: Decimal | None = None
```

- [ ] **Step 4: 통과 확인 + 커밋**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_request_budget.py -v` → PASS
```bash
git add app/services/action_report/snapshot_backed/request.py \
        tests/services/action_report/snapshot_backed/test_request_budget.py
git commit -m "feat(ROB-347): budget_basis + operator_budget_override_usd request fields"
```

---

## Task 2: `demote_for_budget` 순수 헬퍼

**Files:**
- Modify: `app/services/action_report/snapshot_backed/action_verdict.py`
- Test: `tests/services/action_report/snapshot_backed/test_action_verdict_budget.py`

- [ ] **Step 1: 실패 테스트**

`tests/services/action_report/snapshot_backed/test_action_verdict_budget.py`:
```python
from app.services.action_report.snapshot_backed.action_verdict import demote_for_budget


def _state(basis="available_usd", usd=None, krw=0, override=None):
    return {"basis": basis, "usd": usd, "krw": krw, "override_usd": override}


def test_usd_zero_demotes_to_watch_budget_gap():
    v, reasons = demote_for_budget("buy_review", _state(usd=0, krw=0))
    assert v == "watch_only"
    assert "budget_gap" in reasons
    assert "operator_budget_required" in reasons  # override 없음


def test_usd_zero_with_krw_adds_fx_required():
    _, reasons = demote_for_budget("buy_review", _state(usd=0, krw=500000))
    assert "fx_required" in reasons and "budget_gap" in reasons


def test_usd_positive_keeps_buy():
    assert demote_for_budget("buy_review", _state(usd=1000)) == ("buy_review", [])


def test_override_takes_precedence_over_basis():
    assert demote_for_budget("buy_review",
        _state(basis="available_usd", usd=0, override=500)) == ("buy_review", [])


def test_krw_reference_basis_flags_fx_required():
    v, reasons = demote_for_budget("buy_review",
        _state(basis="krw_orderable_reference", usd=0, krw=500000))
    assert v == "watch_only" and reasons == ["fx_required"]


def test_non_buy_unchanged():
    assert demote_for_budget("watch_only", _state(usd=0)) == ("watch_only", [])
    assert demote_for_budget("rejected", _state(usd=0)) == ("rejected", [])
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_action_verdict_budget.py -v`
Expected: FAIL — `demote_for_budget` 없음.

- [ ] **Step 3: 구현** — `action_verdict.py` 끝에 추가:
```python
from typing import Any  # (이미 import되어 있으면 생략)


def demote_for_budget(
    verdict: str, budget_state: dict[str, Any]
) -> tuple[str, list[str]]:
    """ROB-347 — post-verdict budget demotion. Only buy_review is touched;
    budget never upgrades. KRW is reference-only (no KRW→USD fabrication).
    Returns (new_verdict, reasons)."""
    if verdict != "buy_review":
        return verdict, []
    basis = budget_state.get("basis") or "available_usd"
    override = budget_state.get("override_usd")
    krw = budget_state.get("krw") or 0
    # request override (operator/report budget) takes precedence when present.
    usd = override if override is not None else budget_state.get("usd")
    if basis == "krw_orderable_reference" and override is None:
        return "watch_only", ["fx_required"]
    if usd is not None and usd > 0:
        return "buy_review", []
    reasons = ["budget_gap"]
    if krw and krw > 0:
        reasons.append("fx_required")
    if override is None:
        reasons.append("operator_budget_required")
    return "watch_only", reasons
```

- [ ] **Step 4: 통과 확인 + 커밋**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_action_verdict_budget.py -v` → PASS
```bash
git add app/services/action_report/snapshot_backed/action_verdict.py \
        tests/services/action_report/snapshot_backed/test_action_verdict_budget.py
git commit -m "feat(ROB-347): demote_for_budget pure helper (buy_review-only, no fabrication)"
```

---

## Task 3: auto_emit — budget 파라미터 + budget_state + 데모션 + surface

**Files:**
- Modify: `app/services/action_report/snapshot_backed/auto_emit.py`
- Test: `tests/services/action_report/snapshot_backed/test_auto_emit_budget.py`

- [ ] **Step 1: 실패 테스트**

`tests/services/action_report/snapshot_backed/test_auto_emit_budget.py`:
```python
import datetime as dt
from types import SimpleNamespace

from app.services.action_report.snapshot_backed.auto_emit import EvidenceAutoEmitter


def _snap(kind, payload, symbol=None):
    return SimpleNamespace(snapshot_uuid=None, snapshot_kind=kind,
                           payload_json=payload, symbol=symbol)


def _q():
    return {"status": "ok", "best_bid": 10, "best_ask": 10.1,
            "bid_depth": 100, "ask_depth": 100}


def _snaps(buying_power):
    cands = [{"symbol": "GOOD", "rank": 1, "candidate_rank": 1, "data_state": "fresh",
              "quality_flags": [], "priority_score": 0.9}]
    return [
        _snap("portfolio", {"buying_power": buying_power,
                            "primary_source": "kis", "holdings": []}),
        _snap("candidate_universe", {"usefulness": "useful", "candidates": cands}),
        _snap("symbol", {"symbol": "GOOD", "quote": _q()}, symbol="GOOD"),
    ]


def _item(snaps, **budget):
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps, request_market="us", account_scope="kis_live",
        now=dt.datetime(2026, 6, 9), **budget)
    return next(i for i in items if i.symbol == "GOOD")


def test_usd_zero_demotes_with_budget_gap():
    item = _item(_snaps({"usd": 0, "krw": 0}))
    ev = item.evidence_snapshot
    assert ev["action_verdict"] == "watch_only"
    assert "budget_gap" in ev["budget_reasons"]
    assert ev["budget_basis"] == "available_usd"


def test_usd_zero_with_krw_reference_adds_fx_required_not_summed():
    ev = _item(_snaps({"usd": 0, "krw": 500000})).evidence_snapshot
    assert "fx_required" in ev["budget_reasons"]
    assert ev["available_usd"] in (0, 0.0)
    assert ev["krw_orderable_reference"] == 500000  # reference, not summed into USD


def test_operator_override_keeps_buy():
    ev = _item(_snaps({"usd": 0, "krw": 0}),
               budget_basis="operator_budget_override",
               operator_budget_override_usd=500).evidence_snapshot
    assert ev["action_verdict"] == "buy_review"


def test_usd_positive_keeps_buy():
    ev = _item(_snaps({"usd": 2000, "krw": 0})).evidence_snapshot
    assert ev["action_verdict"] == "buy_review"
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_auto_emit_budget.py -v`
Expected: FAIL — propose에 budget 파라미터/데모션/evidence 부재.

- [ ] **Step 3: propose 시그니처 + budget_state**

import 확장:
```python
from app.services.action_report.snapshot_backed.action_verdict import (
    classify_candidate_symbol, classify_held_symbol, demote_for_budget,
    demote_for_quality,
)
```
`propose` 시그니처에 파라미터 추가:
```python
    def propose(
        self,
        *,
        snapshots: list[Any],
        request_market: str,
        account_scope: str | None,
        budget_basis: str = "available_usd",
        operator_budget_override_usd: Any | None = None,
        now: dt.datetime | None = None,
    ) -> list[IngestReportItem]:
```
`held = _held_kis_symbols(portfolio_payload)` 근처(candidate 루프 전)에 budget_state 구성:
```python
        buying_power = portfolio_payload.get("buying_power") or {}
        budget_state = {
            "basis": budget_basis,
            "override_usd": _to_float(operator_budget_override_usd),
            "usd": _to_float(buying_power.get("usd")),
            "krw": _to_float(buying_power.get("krw")),
        }
```
(`_to_float` 는 모듈에 이미 존재 — `_candidate_sort_key` 가 사용.)

- [ ] **Step 4: 루프에 budget 데모션 삽입 (품질 다음, count-cap 이전)**

ROB-346에서 추가된 `verdict, reject_or_wait_reason = demote_for_quality(...)` **직후**,
기존 `if verdict == "data_gap" ...` 블록 **이전**에 삽입:
```python
            # ROB-347 — budget demotion (buy_review only; never fabricates USD).
            verdict, budget_reasons = demote_for_budget(verdict, budget_state)
            if budget_reasons and reject_or_wait_reason is None:
                reject_or_wait_reason = budget_reasons[0]
```
(이로써 예산-강등된 후보는 buy slot/count-cap을 소비하지 않는다.)

- [ ] **Step 5: budget evidence surface**

`_candidate_item` 호출에 `budget_evidence` 전달. 루프에서 호출부 직전:
```python
            budget_evidence = {
                "budget_basis": budget_state["basis"],
                "available_usd": budget_state["usd"],
                "krw_orderable_reference": budget_state["krw"],
                "operator_budget_override_usd": budget_state["override_usd"],
                "budget_reasons": budget_reasons,
                "budget_fit": verdict == "buy_review" and not budget_reasons,
            }
```
`_candidate_item(...)` 호출에 `budget_evidence=budget_evidence` 추가. `_candidate_item`
시그니처에 `budget_evidence: dict[str, Any] | None = None` 추가하고, `extra` 구성 후:
```python
    if budget_evidence:
        extra.update(budget_evidence)
```

- [ ] **Step 6: 통과 확인 + 커밋**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_auto_emit_budget.py -v` → PASS
```bash
git add app/services/action_report/snapshot_backed/auto_emit.py \
        tests/services/action_report/snapshot_backed/test_auto_emit_budget.py
git commit -m "feat(ROB-347): auto_emit budget demotion + budget evidence surface"
```

---

## Task 4: generator가 request budget을 propose로 전달

**Files:**
- Modify: `app/services/action_report/snapshot_backed/generator.py:594-598`
- Test: `tests/services/action_report/snapshot_backed/test_generator_budget_threads.py` (또는 기존 generator 테스트 확장)

- [ ] **Step 1: 실패 테스트 (스레딩 단언)** — 가장 가벼운 방식은 propose를 monkeypatch해 kwargs 캡처:
```python
import pytest

from app.services.action_report.snapshot_backed import generator as gen_mod


@pytest.mark.asyncio
async def test_auto_emit_threads_budget(monkeypatch):
    captured = {}

    class FakeEmitter:
        def __init__(self, **_kw):
            pass

        def propose(self, **kwargs):
            captured.update(kwargs)
            return []

    monkeypatch.setattr(
        "app.services.action_report.snapshot_backed.auto_emit.EvidenceAutoEmitter",
        FakeEmitter,
    )
    # _auto_emit_items_from_bundle 호출에 필요한 최소 stub는 기존 generator 테스트
    # 픽스처를 재사용(bundle/snapshots repo). 핵심 단언:
    # captured["budget_basis"] == request.budget_basis 등.
```
> 기존 generator 단위 테스트가 있으면 그 픽스처(bundle repo stub)를 재사용해
> `_auto_emit_items_from_bundle` 를 호출하고 `captured` 의 budget kwargs를 단언한다.
> 격리 stub 작성이 과하면 Task 4를 통합 테스트(end-to-end generate)로 대체 가능.

- [ ] **Step 2: 실패 확인** — Run 후 FAIL(budget kwargs 미전달).

- [ ] **Step 3: 구현** — `generator.py` 의 propose 호출(594-598)을 교체:
```python
        return emitter.propose(
            snapshots=[s for _i, s in item_snapshot_pairs],
            request_market=request.market,
            account_scope=request.account_scope,
            budget_basis=request.budget_basis,
            operator_budget_override_usd=request.operator_budget_override_usd,
        )
```

- [ ] **Step 4: 통과 확인 + 커밋**

```bash
git add app/services/action_report/snapshot_backed/generator.py \
        tests/services/action_report/snapshot_backed/test_generator_budget_threads.py
git commit -m "feat(ROB-347): thread request budget basis/override into auto_emit"
```

---

## Task 5: MCP 도구 파라미터 노출

**Files:**
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py` (investment_report_generate_from_bundle, ~854-936)
- Test: 기존 핸들러 테스트 확장 또는 신규 `tests/.../test_generate_from_bundle_budget_param.py`

- [ ] **Step 1: 핸들러에서 ReportGenerationRequest 생성부 확인**

`investment_report_generate_from_bundle` 함수 시그니처와 `ReportGenerationRequest(...)`
생성 위치를 연다(`grep -n "ReportGenerationRequest(" app/mcp_server/tooling/investment_reports_handlers.py`).

- [ ] **Step 2: 실패 테스트** — 도구에 `budget_basis`/`operator_budget_override_usd` 인자가
받아들여지고 request로 전달되는지(핸들러 단위 테스트). 핸들러 호출 패턴은 기존 테스트
(`tests/.../test_investment_reports_handlers*` 또는 유사)를 모델로 작성.

- [ ] **Step 3: 구현** — 도구 함수 시그니처에 추가(기존 선택 파라미터들과 같은 자리):
```python
    budget_basis: str = "available_usd",
    operator_budget_override_usd: float | None = None,
```
`ReportGenerationRequest(...)` 생성에 전달:
```python
        budget_basis=budget_basis,
        operator_budget_override_usd=operator_budget_override_usd,
```
도구 docstring에 한 줄 추가: "budget_basis(기본 available_usd)/operator_budget_override_usd로
US 신규매수 예산 전제를 지정. USD=0이면 후보는 watch_only + budget_gap/fx_required."

- [ ] **Step 4: 통과 확인 + 커밋**

```bash
git add app/mcp_server/tooling/investment_reports_handlers.py tests/...
git commit -m "feat(ROB-347): expose budget_basis/operator_budget_override on generate_from_bundle"
```

---

## Task 6: lint / typecheck / 회귀

- [ ] **Step 1:** `uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/` → clean
- [ ] **Step 2:** `uv run ty check app/` → no new errors
- [ ] **Step 3:** `uv run pytest tests/services/action_report/snapshot_backed -v` → PASS
  (USD>0 정상 buy 유지, USD=0 데모션, override, KRW reference 비혼동 전부 green; PR-B 품질
  데모션과의 순서 상호작용도 통과)
- [ ] **Step 4:** 필요 시 `git commit -m "style(ROB-347): ruff"`

---

## Self-review (작성자 체크)
- 스펙 §3.1~3.5 → Task 1(request)/2(헬퍼)/3(데모션+surface)/4(generator)/5(MCP) 매핑. AC 5건 테스트 보유.
- 순서: base → demote_for_quality(B) → demote_for_budget(C) → count-cap. budget은 buy_review만 하향, 절대 상향 없음.
- KRW 비혼동: `krw_orderable_reference` 별도 필드, USD로 합산/날조 안 함(`portfolio_journal.py:90` 정직 동작 보존).
- override precedence: non-null이면 basis 무관 우선.
- 무충돌: classify_candidate_symbol signature 불변; B와 같은 루프에 3줄 추가(인접).
- 안전: 환전/주문/broker mutation 없음, migration 없음.
- placeholder 없음(Task 4/5의 픽스처 재사용 지점만 구현자가 기존 테스트에서 확정 — 코드/명령은 구체).
