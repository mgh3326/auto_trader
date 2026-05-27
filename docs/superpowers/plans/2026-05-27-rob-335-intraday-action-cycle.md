# ROB-335 — `/invest/reports` 장중 액션 사이클 MVP (PR1 백엔드) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** KR/KIS live operator-triggered `intraday_action` 리포트가 `items=[]`로 성공하지 않고, 모든 보유종목을 결정론 verdict(sell_review/keep/no_add/data_gap)로 분류하며, 신규후보 없음·데이터 부족 사유를 ActionPacket으로 read-time projection 한다.

**Architecture:** 결정론 sub-verdict(`evidence_snapshot["action_verdict"]`, JSON, 마이그레이션 없음)를 locked 5값 `decision_bucket` 위의 sub-label로 부여. `EvidenceAutoEmitter`가 모든 KIS 보유종목을 분류하고 신규후보-없음 마커를 emit(intraday_floor 모드). 생성 파이프라인 끝에 non-empty floor guard가 잔여 빈 케이스를 구조적 item으로 보강. `build_action_packet`은 ROB-322 `build_review_sections`와 동일한 read-time projection. 모든 경로 advisory-only, broker/order/watch/order-intent mutation 도달 불가.

**Tech Stack:** Python 3.13, Pydantic v2, FastAPI, SQLAlchemy async, pytest (`uv run pytest`), ruff. 설계 근거: `docs/superpowers/specs/2026-05-27-rob-335-intraday-action-cycle-design.md`.

---

## Scope

이 plan은 **PR1 (백엔드 코어)** 만 다룬다. PR2 (프론트엔드 surface)는 spec §3.6/§7 대로 PR1 머지 후 fresh `main`에서 별도 plan으로 작성한다 (frontend/invest 컴포넌트 탐색 선행 필요).

브랜치: 현재 worktree `rob-335` (base `c31168b3`, ROB-332 머지 직후). 마이그레이션 없음.

## 확정 설계 (spec §2)

- **A**: ActionPacket sub-verdict = locked 5값 `decision_bucket` 위 sub-label. enum 변경 없음.
- **B**: sub-verdict는 item의 `evidence_snapshot["action_verdict"]`(JSON)에 저장. ActionPacket section은 read-time projection.
- **C/C′**: 결정론은 정직한 verdict만 직접 부여 — `data_gap`/`keep`/`no_add`/`sell_review`. `trim_review`/`add_review`는 Hermes push에 유보.
- **D**: `limit_wait`은 evidence 상태만 표면화, 목표가 계산은 ROB-337.

### sub-verdict → decision_bucket 매핑 (locked)

| action_verdict | decision_bucket | 결정론 emit? |
|---|---|---|
| `buy_review` | `new_buy_candidate` | 예 (기존 auto_emit) |
| `limit_wait` | `new_buy_candidate` | 아니오 (Hermes) |
| `no_new_buy_candidates` (marker, symbol=None) | `new_buy_candidate` | 예 (intraday_floor) |
| `sell_review` | `open_action` | 예 |
| `trim_review` / `add_review` | `open_action` | 아니오 (Hermes) |
| `keep` | `completed_or_existing` | 예 (intraday_floor) |
| `no_add` | `completed_or_existing` | 예 (intraday_floor) |
| `watch_only` | `risk_watch` | 예 (기존 watch items) |
| `rejected` | `deferred_no_action` | 아니오 (Hermes) |
| `data_gap` | `deferred_no_action` | 예 |

## File Structure

**Create:**
- `app/services/action_report/snapshot_backed/action_verdict.py` — sub-verdict 어휘 + `VERDICT_TO_BUCKET` + 순수 `classify_held_symbol` 규칙 + `stamp_verdict` 헬퍼. (순수, DB/IO 없음)
- `app/services/action_report/snapshot_backed/intraday_floor.py` — non-empty floor guard (잔여 빈 케이스 → 구조적 item 합성).
- `app/services/investment_reports/action_packet.py` — `build_action_packet` read-time projection (ROB-322 `review_sections.py` 패턴).
- `tests/services/action_report/snapshot_backed/test_action_verdict.py`
- `tests/services/action_report/snapshot_backed/test_intraday_floor.py`
- `tests/services/investment_reports/test_action_packet.py`

**Modify:**
- `app/schemas/investment_reports.py` — `ActionVerdictLiteral` + ActionPacket 스키마 + `InvestmentReportBundle.action_packet` 필드.
- `app/services/action_report/snapshot_backed/auto_emit.py` — 기존 item에 verdict+bucket 스탬프; `intraday_floor` 모드에서 모든 KIS 보유종목 분류 + no_new_buy 마커.
- `app/services/action_report/snapshot_backed/generator.py` — intraday intent 감지, auto_emit에 intraday_floor 전달, classify 후 floor guard 호출.
- `app/routers/investment_reports.py` — `build_action_packet` 호출하여 bundle에 첨부.
- `tests/services/action_report/snapshot_backed/test_auto_emit.py` — 스탬프/intraday_floor 회귀 추가.
- `tests/services/action_report/snapshot_backed/test_generator_regression.py` — floor invariant 회귀 추가.

---

## Task 1: action_verdict 어휘 + 매핑 + 보유종목 분류 규칙

**Files:**
- Create: `app/services/action_report/snapshot_backed/action_verdict.py`
- Test: `tests/services/action_report/snapshot_backed/test_action_verdict.py`

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/services/action_report/snapshot_backed/test_action_verdict.py
"""ROB-335 — sub-verdict vocabulary, bucket mapping, held-symbol rules."""

from __future__ import annotations

import pytest

from app.models.investment_symbol_intermediate_reports import DECISION_BUCKETS
from app.services.action_report.snapshot_backed.action_verdict import (
    ACTION_VERDICTS,
    VERDICT_TO_BUCKET,
    classify_held_symbol,
)

pytestmark = pytest.mark.unit


def test_every_verdict_maps_to_a_locked_decision_bucket() -> None:
    # B/A: sub-verdicts are sub-labels over the locked 5-value enum — every
    # verdict must map onto an existing decision_bucket (no new enum value).
    assert set(VERDICT_TO_BUCKET) == set(ACTION_VERDICTS)
    for verdict, bucket in VERDICT_TO_BUCKET.items():
        assert bucket in DECISION_BUCKETS, (verdict, bucket)


def test_held_unactionable_quote_is_data_gap() -> None:
    holding = {"ticker": "005930", "sellable_quantity": 10}
    quote = {"status": "unavailable"}
    assert classify_held_symbol(holding, quote, in_candidate_universe=False) == "data_gap"


def test_held_missing_quote_is_data_gap() -> None:
    holding = {"ticker": "005930", "sellable_quantity": 10}
    assert classify_held_symbol(holding, None, in_candidate_universe=False) == "data_gap"


def test_held_sellable_with_actionable_quote_is_sell_review() -> None:
    holding = {"ticker": "005930", "sellable_quantity": 10}
    quote = {"status": "ok", "best_bid": 1.0, "best_ask": 2.0, "bid_depth": 5.0}
    assert classify_held_symbol(holding, quote, in_candidate_universe=False) == "sell_review"


def test_held_not_sellable_but_trending_is_no_add() -> None:
    holding = {"ticker": "005930", "sellable_quantity": 0}
    quote = {"status": "ok", "best_bid": 1.0, "best_ask": 2.0, "ask_depth": 5.0}
    assert classify_held_symbol(holding, quote, in_candidate_universe=True) == "no_add"


def test_held_not_sellable_not_trending_is_keep() -> None:
    holding = {"ticker": "005930", "sellable_quantity": 0}
    quote = {"status": "ok", "best_bid": 1.0, "best_ask": 2.0, "ask_depth": 5.0}
    assert classify_held_symbol(holding, quote, in_candidate_universe=False) == "keep"
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_action_verdict.py -v`
Expected: FAIL — `ModuleNotFoundError: ...action_verdict`.

- [ ] **Step 3: 최소 구현**

```python
# app/services/action_report/snapshot_backed/action_verdict.py
"""ROB-335 — deterministic ActionPacket sub-verdict vocabulary + rules.

Sub-verdicts are *sub-labels over the locked ROB-301 ``decision_bucket``
5-value enum* (spec §2 decision A). They are stored on each report item's
``evidence_snapshot["action_verdict"]`` (JSON; no migration, decision B) and
projected at read-time by ``build_action_packet``.

Per decision C′, only the *honest* verdicts are assigned deterministically
here: ``data_gap`` / ``keep`` / ``no_add`` / ``sell_review`` (held) and
``buy_review`` / ``no_new_buy_candidates`` (candidate). Directional
``trim_review`` / ``add_review`` / ``limit_wait`` / ``rejected`` exist in the
vocabulary for Hermes push to fill but are never fabricated here.
"""

from __future__ import annotations

from typing import Any

# action_verdict -> locked decision_bucket. Keys are the full vocabulary.
VERDICT_TO_BUCKET: dict[str, str] = {
    "buy_review": "new_buy_candidate",
    "limit_wait": "new_buy_candidate",
    "no_new_buy_candidates": "new_buy_candidate",
    "sell_review": "open_action",
    "trim_review": "open_action",
    "add_review": "open_action",
    "keep": "completed_or_existing",
    "no_add": "completed_or_existing",
    "watch_only": "risk_watch",
    "rejected": "deferred_no_action",
    "data_gap": "deferred_no_action",
}

ACTION_VERDICTS: frozenset[str] = frozenset(VERDICT_TO_BUCKET)


def _quote_is_actionable(quote: Any) -> bool:
    # Mirrors auto_emit._quote_is_actionable so held + candidate gates agree.
    if not isinstance(quote, dict):
        return False
    if quote.get("status") != "ok":
        return False
    best_bid = quote.get("best_bid") or 0
    best_ask = quote.get("best_ask") or 0
    bid_depth = quote.get("bid_depth") or 0
    ask_depth = quote.get("ask_depth") or 0
    return best_bid > 0 and best_ask > 0 and (bid_depth > 0 or ask_depth > 0)


def classify_held_symbol(
    holding: dict[str, Any],
    quote: dict[str, Any] | None,
    *,
    in_candidate_universe: bool,
) -> str:
    """Deterministic verdict for ONE KIS-primary held symbol (decision C′).

    Order (honest range only):
      1. quote missing / not actionable -> ``data_gap`` (no directional call)
      2. sellable_quantity > 0          -> ``sell_review`` (reviewable reduce)
      3. held + in screener universe    -> ``no_add`` (trending, don't add)
      4. otherwise                      -> ``keep`` (default hold)
    """
    if not _quote_is_actionable(quote):
        return "data_gap"
    if (holding.get("sellable_quantity") or 0) > 0:
        return "sell_review"
    if in_candidate_universe:
        return "no_add"
    return "keep"
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_action_verdict.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: 커밋**

```bash
git add app/services/action_report/snapshot_backed/action_verdict.py tests/services/action_report/snapshot_backed/test_action_verdict.py
git commit -m "feat(rob-335): deterministic action_verdict vocab + held-symbol rules

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: ActionPacket 스키마 + bundle 필드

**Files:**
- Modify: `app/schemas/investment_reports.py` (after `ReportReviewSections`, ~line 551; and `InvestmentReportBundle`, ~line 574)
- Test: `tests/services/investment_reports/test_action_packet.py` (schema 부분)

- [ ] **Step 1: 실패 테스트 작성** (스키마 구성 가능 + bundle 필드 default None)

```python
# tests/services/investment_reports/test_action_packet.py
"""ROB-335 — ActionPacket read-time projection + schema."""

from __future__ import annotations

import pytest

from app.schemas.investment_reports import (
    ActionPacket,
    InvestmentReportBundle,
)

pytestmark = pytest.mark.unit


def test_action_packet_defaults_are_empty() -> None:
    packet = ActionPacket()
    assert packet.held_actions == []
    assert packet.new_buy_candidates == []
    assert packet.no_new_buy_reason is None
    assert packet.risk_reviews == []
    assert packet.no_action_reason is None
    assert packet.data_gaps_for_next_cycle == []


def test_bundle_action_packet_field_is_optional() -> None:
    # Additive, null for legacy reports (mirrors review_sections).
    assert "action_packet" in InvestmentReportBundle.model_fields
    assert InvestmentReportBundle.model_fields["action_packet"].default is None
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/investment_reports/test_action_packet.py -v`
Expected: FAIL — `ImportError: cannot import name 'ActionPacket'`.

- [ ] **Step 3: 스키마 추가**

`app/schemas/investment_reports.py` — `ReportReviewSections` 클래스 정의 직후(현재 ~551행)에 추가:

```python
# ROB-335 — intraday ActionPacket. A read-time *view-layer projection* over
# the same persisted items (sub-verdict in evidence_snapshot["action_verdict"])
# + ROB-318 diagnostics. No new persisted classification / DB CHECK / migration.
ActionVerdictLiteral = Literal[
    "buy_review",
    "limit_wait",
    "no_new_buy_candidates",
    "sell_review",
    "trim_review",
    "add_review",
    "keep",
    "no_add",
    "watch_only",
    "rejected",
    "data_gap",
]


class ActionPacketEntry(BaseModel):
    """One symbol-level entry in an ActionPacket group."""

    verdict: ActionVerdictLiteral
    symbol: str | None = None
    side: ItemSideLiteral | None = None
    rationale: str
    item_uuid: UUID | None = None
    evidence_snapshot: dict[str, Any] = Field(default_factory=dict)


class DataGapEntry(BaseModel):
    """One data-gap surfaced for the next cycle."""

    source: str
    status: str | None = None
    reason: str | None = None


class ActionPacket(BaseModel):
    """ROB-335 four-question intraday surface (held / new / risk / data-gap).

    Always-explicit: ``no_new_buy_reason`` and ``no_action_reason`` answer the
    "why nothing" questions even when the corresponding groups are empty.
    """

    held_actions: list[ActionPacketEntry] = Field(default_factory=list)
    new_buy_candidates: list[ActionPacketEntry] = Field(default_factory=list)
    no_new_buy_reason: str | None = None
    risk_reviews: list[ActionPacketEntry] = Field(default_factory=list)
    no_action_reason: NoActionSummary | None = None
    data_gaps_for_next_cycle: list[DataGapEntry] = Field(default_factory=list)
```

`InvestmentReportBundle` (현재 ~574행, `review_sections` 필드 직후)에 추가:

```python
    # ROB-335 — additive intraday ActionPacket projection. Null for legacy /
    # non-intraday reports; existing items / review_sections remain the fallback.
    action_packet: ActionPacket | None = None
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/services/investment_reports/test_action_packet.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: 커밋**

```bash
git add app/schemas/investment_reports.py tests/services/investment_reports/test_action_packet.py
git commit -m "feat(rob-335): ActionPacket schema + bundle field

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: build_action_packet read-time projection

**Files:**
- Create: `app/services/investment_reports/action_packet.py`
- Test: `tests/services/investment_reports/test_action_packet.py` (projection 부분 추가)

- [ ] **Step 1: 실패 테스트 추가** (`test_action_packet.py` 하단에 append)

```python
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.schemas.investment_reports import InvestmentReportItemResponse
from app.services.investment_reports.action_packet import build_action_packet

_NOW = datetime(2026, 5, 27, tzinfo=UTC)


def _item(
    *,
    verdict: str | None,
    decision_bucket: str | None,
    symbol: str | None = "005930",
    item_kind: str = "action",
    side: str | None = "sell",
    intent: str = "sell_review",
) -> InvestmentReportItemResponse:
    evidence = {"action_verdict": verdict} if verdict is not None else {}
    return InvestmentReportItemResponse(
        item_uuid=uuid4(),
        item_kind=item_kind,  # type: ignore[arg-type]
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        intent=intent,  # type: ignore[arg-type]
        target_kind="asset",
        priority=0,
        confidence=Decimal("80"),
        rationale="r",
        evidence_snapshot=evidence,
        watch_condition=None,
        trigger_checklist=[],
        max_action={},
        valid_until=None,
        status="proposed",
        metadata={},
        created_at=_NOW,
        updated_at=_NOW,
        decision_bucket=decision_bucket,
    )


def test_held_and_new_and_risk_are_grouped_by_verdict() -> None:
    items = [
        _item(verdict="sell_review", decision_bucket="open_action"),
        _item(verdict="keep", decision_bucket="completed_or_existing", side=None),
        _item(verdict="buy_review", decision_bucket="new_buy_candidate", side="buy",
              intent="buy_review", symbol="000660"),
        _item(verdict="watch_only", decision_bucket="risk_watch", item_kind="watch",
              side=None, intent="trend_recovery_review", symbol="035720"),
    ]
    packet = build_action_packet(items, diagnostics=None)
    assert {e.verdict for e in packet.held_actions} == {"sell_review", "keep"}
    assert [e.verdict for e in packet.new_buy_candidates] == ["buy_review"]
    assert [e.verdict for e in packet.risk_reviews] == ["watch_only"]


def test_no_new_buy_marker_sets_reason_not_a_candidate_row() -> None:
    marker = _item(
        verdict="no_new_buy_candidates",
        decision_bucket="new_buy_candidate",
        symbol=None,
        side=None,
        intent="risk_review",
        item_kind="risk",
    )
    marker.rationale = "국내 스크리너 스냅샷이 최신 거래일 기준이 아닙니다 (stale)."
    packet = build_action_packet([marker], diagnostics=None)
    assert packet.new_buy_candidates == []
    assert packet.no_new_buy_reason == marker.rationale


def test_data_gaps_from_items_and_diagnostics() -> None:
    items = [_item(verdict="data_gap", decision_bucket="deferred_no_action",
                   symbol="005930", side=None, intent="risk_review", item_kind="risk")]
    diagnostics = {
        "why_no_action": {"kind": "data_insufficient", "reason_ko": "데이터 부족",
                          "blocking_sources": ["portfolio"]},
        "data_sufficiency_by_source": {
            "portfolio": {"status": "unavailable", "reason_code": "user_id_missing"},
            "symbol": {"status": "fresh"},
        },
    }
    packet = build_action_packet(items, diagnostics=diagnostics)
    assert packet.no_action_reason is not None
    assert packet.no_action_reason.kind == "data_insufficient"
    # symbol-level data_gap item + degraded source from diagnostics both surface.
    sources = {g.source for g in packet.data_gaps_for_next_cycle}
    assert "005930" in sources
    assert "portfolio" in sources


def test_items_without_verdict_are_not_projected() -> None:
    # Legacy items (no action_verdict) stay out of the packet (decision A/B).
    items = [_item(verdict=None, decision_bucket="open_action")]
    packet = build_action_packet(items, diagnostics=None)
    assert packet.held_actions == []
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/investment_reports/test_action_packet.py -v`
Expected: FAIL — `ModuleNotFoundError: ...action_packet`.

- [ ] **Step 3: 구현**

```python
# app/services/investment_reports/action_packet.py
"""ROB-335 — deterministic intraday ActionPacket projection.

Read-time *view-layer* projection (same pattern as ROB-322
``review_sections.py``): groups the flat report-item list by the
``evidence_snapshot["action_verdict"]`` sub-label into the four-question
intraday surface, and folds ROB-318 diagnostics into the no-action /
data-gap answers.

Pure + read-only: no new persisted classification, DB CHECK, or migration.
Items without an ``action_verdict`` (legacy / Hermes-not-yet) are not
projected; they remain available via the bundle's ``items``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.schemas.investment_reports import (
    ActionPacket,
    ActionPacketEntry,
    DataGapEntry,
    InvestmentReportItemResponse,
    NoActionSummary,
)
from app.services.action_report.snapshot_backed.action_verdict import (
    VERDICT_TO_BUCKET,
)

_HELD_VERDICTS = {"sell_review", "trim_review", "add_review", "keep", "no_add"}
_NEW_BUY_VERDICTS = {"buy_review", "limit_wait"}
_RISK_VERDICTS = {"watch_only"}
_DATA_GAP_VERDICTS = {"data_gap"}
_DEGRADED_STATUSES = {"unavailable", "failed", "hard_stale", "soft_stale", "partial"}


def _verdict(item: InvestmentReportItemResponse) -> str | None:
    evidence = item.evidence_snapshot or {}
    verdict = evidence.get("action_verdict") if isinstance(evidence, Mapping) else None
    if isinstance(verdict, str) and verdict in VERDICT_TO_BUCKET:
        return verdict
    return None


def _entry(item: InvestmentReportItemResponse, verdict: str) -> ActionPacketEntry:
    return ActionPacketEntry(
        verdict=verdict,  # type: ignore[arg-type]
        symbol=item.symbol,
        side=item.side,
        rationale=item.rationale,
        item_uuid=item.item_uuid,
        evidence_snapshot=dict(item.evidence_snapshot or {}),
    )


def build_action_packet(
    items: Sequence[InvestmentReportItemResponse],
    diagnostics: Mapping[str, Any] | None,
) -> ActionPacket:
    held: list[ActionPacketEntry] = []
    new_buy: list[ActionPacketEntry] = []
    risk: list[ActionPacketEntry] = []
    data_gaps: list[DataGapEntry] = []
    no_new_buy_reason: str | None = None

    for item in items:
        verdict = _verdict(item)
        if verdict is None:
            continue
        if verdict == "no_new_buy_candidates":
            no_new_buy_reason = item.rationale
            continue
        if verdict in _HELD_VERDICTS:
            held.append(_entry(item, verdict))
        elif verdict in _NEW_BUY_VERDICTS:
            new_buy.append(_entry(item, verdict))
        elif verdict in _RISK_VERDICTS:
            risk.append(_entry(item, verdict))
        elif verdict in _DATA_GAP_VERDICTS:
            data_gaps.append(
                DataGapEntry(source=item.symbol or "unknown", reason=item.rationale)
            )

    no_action_reason = _no_action_summary(diagnostics)
    data_gaps.extend(_diagnostics_gaps(diagnostics))

    return ActionPacket(
        held_actions=held,
        new_buy_candidates=new_buy,
        no_new_buy_reason=no_new_buy_reason,
        risk_reviews=risk,
        no_action_reason=no_action_reason,
        data_gaps_for_next_cycle=data_gaps,
    )


def _no_action_summary(
    diagnostics: Mapping[str, Any] | None,
) -> NoActionSummary | None:
    if not isinstance(diagnostics, Mapping):
        return None
    why = diagnostics.get("why_no_action")
    if not isinstance(why, Mapping):
        return None
    blocking = why.get("blocking_sources") or []
    return NoActionSummary(
        kind=why.get("kind"),
        reason_ko=why.get("reason_ko"),
        blocking_sources=[str(s) for s in blocking],
    )


def _diagnostics_gaps(diagnostics: Mapping[str, Any] | None) -> list[DataGapEntry]:
    if not isinstance(diagnostics, Mapping):
        return []
    by_source = diagnostics.get("data_sufficiency_by_source")
    if not isinstance(by_source, Mapping):
        return []
    out: list[DataGapEntry] = []
    for source, info in by_source.items():
        if not isinstance(info, Mapping):
            continue
        status = info.get("status")
        if status in _DEGRADED_STATUSES:
            out.append(
                DataGapEntry(
                    source=str(source),
                    status=str(status) if status is not None else None,
                    reason=info.get("reason") or info.get("reason_code"),
                )
            )
    return out
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/services/investment_reports/test_action_packet.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: 커밋**

```bash
git add app/services/investment_reports/action_packet.py tests/services/investment_reports/test_action_packet.py
git commit -m "feat(rob-335): build_action_packet read-time projection

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: auto_emit — verdict/bucket 스탬프 + intraday_floor 보유종목 전수분류 + no-candidate 마커

**Files:**
- Modify: `app/services/action_report/snapshot_backed/auto_emit.py`
- Test: `tests/services/action_report/snapshot_backed/test_auto_emit.py` (회귀 추가)

설계: `EvidenceAutoEmitter.__init__`에 `intraday_floor: bool = False` 추가. 기존 sell/buy/watch/held-trend item에 `evidence_snapshot["action_verdict"]` + `decision_bucket`을 스탬프(기본 모드에서도 안전한 additive). `intraday_floor=True`이면 (a) 모든 KIS 보유종목을 `classify_held_symbol`로 1건씩 분류(이미 sell_review로 emit된 심볼 제외), (b) candidate `usefulness != "useful"`이면 `no_new_buy_candidates` 마커 item 1건 emit.

- [ ] **Step 1: 실패 테스트 추가** (`test_auto_emit.py` 하단)

```python
def test_existing_sell_item_is_stamped_with_verdict_and_bucket() -> None:
    # Default mode (no intraday_floor): existing sell candidate now carries the
    # ActionPacket sub-verdict + decision_bucket so it projects.
    snapshots = [
        _make_snapshot(kind="portfolio",
                       payload=_kis_portfolio_payload(ticker="005930", sellable=5.0)),
        _make_snapshot(kind="symbol", symbol="005930",
                       payload=_ok_quote_payload("005930")),
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    sell = next(i for i in items if i.symbol == "005930" and i.side == "sell")
    assert sell.evidence_snapshot["action_verdict"] == "sell_review"
    assert sell.decision_bucket == "open_action"


def test_intraday_floor_classifies_every_held_symbol() -> None:
    # Held symbol with NO actionable quote -> data_gap item (would be skipped
    # entirely in default mode).
    snapshots = [
        _make_snapshot(kind="portfolio",
                       payload=_kis_portfolio_payload(ticker="005930", sellable=0.0)),
    ]
    items = EvidenceAutoEmitter(intraday_floor=True).propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    held = next(i for i in items if i.symbol == "005930")
    assert held.evidence_snapshot["action_verdict"] == "data_gap"
    assert held.decision_bucket == "deferred_no_action"


def test_intraday_floor_emits_no_new_buy_marker_when_stale_only() -> None:
    snapshots = [
        _make_snapshot(kind="portfolio",
                       payload=_kis_portfolio_payload(ticker="005930", sellable=0.0)),
        _make_snapshot(kind="candidate_universe",
                       payload=_candidate_payload("stale_only")),
    ]
    items = EvidenceAutoEmitter(intraday_floor=True).propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    marker = next(
        i for i in items
        if i.evidence_snapshot.get("action_verdict") == "no_new_buy_candidates"
    )
    assert marker.symbol is None
    assert marker.decision_bucket == "new_buy_candidate"
    assert marker.item_kind == "risk"


def test_default_mode_emits_no_marker_and_no_keep_items() -> None:
    # Backwards-compat: without intraday_floor, behaviour is unchanged.
    snapshots = [
        _make_snapshot(kind="portfolio",
                       payload=_kis_portfolio_payload(ticker="005930", sellable=0.0)),
        _make_snapshot(kind="candidate_universe",
                       payload=_candidate_payload("stale_only")),
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    verdicts = {i.evidence_snapshot.get("action_verdict") for i in items}
    assert "no_new_buy_candidates" not in verdicts
    assert "keep" not in verdicts
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_auto_emit.py -v -k "stamp or intraday_floor or marker or default_mode"`
Expected: FAIL — `TypeError: ... unexpected keyword 'intraday_floor'` / `KeyError: 'action_verdict'`.

- [ ] **Step 3: 구현 — `auto_emit.py` 수정**

3a. import + 헬퍼 추가 (파일 상단 import 블록 아래):

```python
from app.services.action_report.snapshot_backed.action_verdict import (
    VERDICT_TO_BUCKET,
    classify_held_symbol,
)


def _stamp(item: IngestReportItem, verdict: str) -> IngestReportItem:
    """Attach the ActionPacket sub-verdict + its locked decision_bucket."""
    item.evidence_snapshot["action_verdict"] = verdict
    item.decision_bucket = VERDICT_TO_BUCKET[verdict]
    return item
```

3b. `__init__` 시그니처에 플래그 추가:

```python
    def __init__(
        self, *, max_buy_candidates: int = 10, intraday_floor: bool = False
    ) -> None:
        self._max_buy_candidates = max_buy_candidates
        self._intraday_floor = intraday_floor
```

3c. 기존 emit 지점에 `_stamp` 적용 — sell loop의 `items.append(IngestReportItem(...))`를 다음으로 감싼다:

```python
            items.append(
                _stamp(
                    IngestReportItem(
                        client_item_key=f"auto-sell-{ticker}",
                        item_kind="action",
                        symbol=ticker,
                        side="sell",
                        intent="sell_review",
                        rationale=(
                            f"보유 종목 {ticker} sell 검토 — sellable {sellable}, "
                            f"best_bid {quote.get('best_bid')}, "
                            f"spread_bps {quote.get('spread_bps')}"
                        ),
                        operation="review",
                        apply_policy="requires_user_approval",
                        evidence_snapshot=evidence,
                    ),
                    "sell_review",
                )
            )
```

동일하게 buy candidate `items.append(...)`를 `_stamp(IngestReportItem(...), "buy_review")`로, watch (`auto-watch-`) 와 held-and-trending (`auto-hold-trend-`) item을 `_stamp(..., "watch_only")`로 감싼다.

3d. held-and-trending 루프 직후(= `run_card` citation 블록 직전)에 intraday_floor 보강 블록 추가:

```python
        # ROB-335 — intraday floor: classify EVERY held KIS symbol (not just
        # sellable+actionable) so held_actions is never empty, and surface an
        # explicit no-new-buy reason when the screener universe is not useful.
        if self._intraday_floor:
            already = {i.symbol for i in items if i.symbol}
            for ticker, holding in held.items():
                if ticker in already:
                    continue
                quote_pair = symbol_quotes.get(ticker)
                quote = quote_pair[1] if quote_pair else None
                verdict = classify_held_symbol(
                    holding, quote, in_candidate_universe=ticker in candidate_by_symbol
                )
                items.append(
                    _stamp(
                        IngestReportItem(
                            client_item_key=f"auto-held-{ticker}",
                            item_kind="risk" if verdict == "data_gap" else "action",
                            symbol=ticker,
                            side="sell" if verdict == "sell_review" else None,
                            intent=(
                                "sell_review" if verdict == "sell_review"
                                else "risk_review" if verdict == "data_gap"
                                else "rebalance_review"
                            ),
                            rationale=(
                                f"보유 종목 {ticker} {verdict} — sellable "
                                f"{holding.get('sellable_quantity')}, "
                                f"quote {quote.get('status') if quote else 'none'}"
                            ),
                            operation="review",
                            apply_policy="requires_user_approval",
                            evidence_snapshot=_make_evidence(
                                quote_pair[0] if quote_pair else portfolio_snapshot,
                                extra={
                                    "portfolio_snapshot_uuid": _snapshot_uuid(
                                        portfolio_snapshot
                                    ),
                                    "sellable_quantity": holding.get(
                                        "sellable_quantity"
                                    ),
                                    "quote_status": quote.get("status")
                                    if quote else "no_snapshot",
                                    "proposer": "auto_emit/intraday_held_floor",
                                },
                            ),
                        ),
                        verdict,
                    )
                )

            if candidate_usefulness != "useful":
                missing = (
                    candidate_snapshot is not None
                    and _snapshot_payload(candidate_snapshot).get("missing_data")
                ) or {}
                reason = (
                    f"{missing.get('what', '신규 매수 후보 없음')} "
                    f"{missing.get('next', '')}".strip()
                    if isinstance(missing, dict)
                    else "신규 매수 후보 없음"
                )
                items.append(
                    _stamp(
                        IngestReportItem(
                            client_item_key="auto-no-new-buy",
                            item_kind="risk",
                            symbol=None,
                            intent="risk_review",
                            rationale=reason,
                            operation="review",
                            apply_policy="requires_user_approval",
                            evidence_snapshot=_make_evidence(
                                candidate_snapshot,
                                extra={
                                    "candidate_usefulness": candidate_usefulness,
                                    "proposer": "auto_emit/no_new_buy_floor",
                                },
                            ),
                        ),
                        "no_new_buy_candidates",
                    )
                )
```

- [ ] **Step 4: 통과 확인 (신규 + 기존 회귀)**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_auto_emit.py -v`
Expected: PASS (기존 테스트 전부 + 신규 4건). 기존 테스트가 verdict 스탬프로 깨지지 않아야 함(스탬프는 additive).

- [ ] **Step 5: 커밋**

```bash
git add app/services/action_report/snapshot_backed/auto_emit.py tests/services/action_report/snapshot_backed/test_auto_emit.py
git commit -m "feat(rob-335): stamp action_verdict + intraday_floor held classification & no-buy marker

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: generator — intraday intent 감지 + auto_emit intraday_floor 전달 + non-empty floor guard

**Files:**
- Create: `app/services/action_report/snapshot_backed/intraday_floor.py`
- Modify: `app/services/action_report/snapshot_backed/generator.py`
- Test: `tests/services/action_report/snapshot_backed/test_intraday_floor.py`

설계: 순수 함수 `is_intraday_action(policy_version)` 와 `ensure_action_floor(items, why_no_action)`. generator는 (a) `_auto_emit_items_from_bundle`에서 intraday이면 `EvidenceAutoEmitter(intraday_floor=True)` 사용, (b) classify_items + why_no_action 계산 후, intraday이면 `ensure_action_floor`로 잔여 빈 케이스를 1건 구조적 item으로 보강.

- [ ] **Step 1: 실패 테스트 작성** (순수 함수 단위)

```python
# tests/services/action_report/snapshot_backed/test_intraday_floor.py
"""ROB-335 — intraday non-empty floor guard."""

from __future__ import annotations

import pytest

from app.schemas.investment_reports import IngestReportItem
from app.services.action_report.snapshot_backed.intraday_floor import (
    ensure_action_floor,
    is_intraday_action,
)

pytestmark = pytest.mark.unit


def test_is_intraday_action_matches_policy_version() -> None:
    assert is_intraday_action("intraday_action_report_v1") is True
    assert is_intraday_action("snapshot_backed_advisory_v1") is False
    assert is_intraday_action(None) is False


def test_floor_synthesizes_one_item_when_empty() -> None:
    why = {"kind": "data_insufficient", "reason_ko": "데이터 부족 — portfolio 확인 불가",
           "blocking_sources": ["portfolio"]}
    out = ensure_action_floor([], why_no_action=why)
    assert len(out) == 1
    item = out[0]
    assert item.evidence_snapshot["action_verdict"] == "data_gap"
    assert item.decision_bucket == "deferred_no_action"
    assert item.rationale == why["reason_ko"]


def test_floor_real_no_action_uses_no_action_verdict() -> None:
    why = {"kind": "real_no_action", "reason_ko": "데이터 충분 — 현 시점 신규 액션 없음(관망)",
           "blocking_sources": []}
    out = ensure_action_floor([], why_no_action=why)
    assert out[0].evidence_snapshot["action_verdict"] == "keep"
    assert out[0].decision_bucket == "completed_or_existing"


def test_floor_is_noop_when_items_present() -> None:
    existing = [
        IngestReportItem(
            client_item_key="x", item_kind="action", symbol="005930", side="sell",
            intent="sell_review", rationale="r", operation="review",
            apply_policy="requires_user_approval",
            evidence_snapshot={"action_verdict": "sell_review"},
            decision_bucket="open_action",
        )
    ]
    out = ensure_action_floor(existing, why_no_action=None)
    assert out == existing
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_intraday_floor.py -v`
Expected: FAIL — `ModuleNotFoundError: ...intraday_floor`.

- [ ] **Step 3: 구현 — `intraday_floor.py`**

```python
# app/services/action_report/snapshot_backed/intraday_floor.py
"""ROB-335 — intraday non-empty floor guard.

Last-resort guarantee that an ``intraday_action`` report never succeeds with
``items=[]`` (spec §3.1). When the deterministic emitter + classifier produced
nothing actionable (e.g. no holdings, no candidates, portfolio unavailable),
synthesize ONE structural review item that carries the report's no-action
reason as an explicit ActionPacket entry. Never fabricates a buy/sell call.
"""

from __future__ import annotations

from typing import Any

from app.schemas.investment_reports import IngestReportItem
from app.services.action_report.snapshot_backed.action_verdict import VERDICT_TO_BUCKET

_INTRADAY_POLICY_PREFIX = "intraday_action"


def is_intraday_action(policy_version: str | None) -> bool:
    return bool(policy_version) and policy_version.startswith(_INTRADAY_POLICY_PREFIX)


def ensure_action_floor(
    items: list[IngestReportItem],
    *,
    why_no_action: dict[str, Any] | None,
) -> list[IngestReportItem]:
    """Return ``items`` unchanged when non-empty; else a one-item floor."""
    if items:
        return items

    kind = (why_no_action or {}).get("kind")
    # real_no_action -> genuine hold (keep); data/stale-blocked -> data_gap.
    verdict = "keep" if kind == "real_no_action" else "data_gap"
    reason = (why_no_action or {}).get("reason_ko") or (
        "장중 액션 없음 — 데이터/후보 부족"
    )
    return [
        IngestReportItem(
            client_item_key="intraday-floor",
            item_kind="risk",
            symbol=None,
            intent="risk_review",
            rationale=reason,
            operation="review",
            apply_policy="requires_user_approval",
            evidence_snapshot={
                "action_verdict": verdict,
                "proposer": "intraday_floor",
                "why_no_action": why_no_action,
            },
            decision_bucket=VERDICT_TO_BUCKET[verdict],
        )
    ]
```

- [ ] **Step 4: 통과 확인 (단위)**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_intraday_floor.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: generator 연결**

`generator.py` 상단 import에 추가:

```python
from app.services.action_report.snapshot_backed.intraday_floor import (
    ensure_action_floor,
    is_intraday_action,
)
```

`_auto_emit_items_from_bundle` 의 emitter 생성부 수정 (현재 ~392행):

```python
        emitter = EvidenceAutoEmitter(
            intraday_floor=is_intraday_action(request.policy_version)
        )
```

`generate()` 에서 `why_no_action` 계산(현재 ~293-299행) 직후, `report_diagnostics` 빌드 전에 floor guard 삽입:

```python
        # ROB-335 — intraday non-empty floor: never let an intraday_action
        # report succeed with items=[]; synthesize an explicit no-action /
        # data-gap item from the deterministic why_no_action verdict.
        if is_intraday_action(request.policy_version):
            request = request.model_copy(
                update={
                    "items": ensure_action_floor(
                        list(request.items), why_no_action=why_no_action
                    )
                }
            )
```

- [ ] **Step 6: generator 회귀 — intraday이면 items가 절대 비지 않음**

`tests/services/action_report/snapshot_backed/test_generator_regression.py` 에 추가 (기존 fixture/헬퍼 재사용; 빈 번들 = no holdings/no candidates 케이스):

```python
async def test_intraday_report_never_empty_items(...):  # 기존 generator fixture 패턴 사용
    # market=kr/account=kis_live, user_id=None (portfolio unavailable),
    # 빈 후보 -> 생성 결과 items_count >= 1, floor item은 data_gap.
    resp = await generator.generate(_intraday_request(user_id=None))
    assert resp.items_count >= 1
```

> 구현 시 `test_generator_regression.py` 상단의 기존 fixture(번들/ensure mock, `_request(...)` 헬퍼)를 그대로 사용하고, `policy_version="intraday_action_report_v1"` + `user_id=None` 으로 요청을 구성한다. 어서션은 `resp.items_count >= 1` 와 (가능하면) 생성된 리포트 조회 후 floor item의 `decision_bucket == "deferred_no_action"`.

- [ ] **Step 7: 통과 확인 (generator 전체)**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_generator.py tests/services/action_report/snapshot_backed/test_generator_regression.py -v`
Expected: PASS (기존 + 신규).

- [ ] **Step 8: 커밋**

```bash
git add app/services/action_report/snapshot_backed/intraday_floor.py app/services/action_report/snapshot_backed/generator.py tests/services/action_report/snapshot_backed/test_intraday_floor.py tests/services/action_report/snapshot_backed/test_generator_regression.py
git commit -m "feat(rob-335): intraday non-empty floor guard + generator wiring

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: 읽기 모델 연결 — bundle에 action_packet 첨부

**Files:**
- Modify: `app/routers/investment_reports.py` (`_serialise_bundle`, ~line 43 import + ~line 78-96)
- Test: `tests/services/investment_reports/test_action_packet.py` (serialize 보강은 단위 projection으로 이미 커버; 라우터는 통합 경로) — 여기서는 projection이 bundle에 붙는지 가벼운 단위 확인 추가

- [ ] **Step 1: 실패 테스트 추가** (`test_action_packet.py` 하단) — bundle 직렬화가 action_packet을 채우는지 순수 확인

```python
def test_serialise_attaches_action_packet(monkeypatch) -> None:
    # _serialise_bundle should project items into bundle.action_packet using
    # the same build_action_packet path (additive, never None for intraday).
    from app.routers import investment_reports as mod

    items = [_item(verdict="sell_review", decision_bucket="open_action")]
    packet = build_action_packet(items, diagnostics=None)
    assert packet.held_actions  # sanity: projection works on these items
    # The router import wires build_action_packet:
    assert hasattr(mod, "build_action_packet")
```

> 라우터 전체 통합(DB)은 기존 `tests/routers` 통합 스위트가 커버한다. 여기서는 wiring 존재만 단위로 핀.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/investment_reports/test_action_packet.py -v -k serialise`
Expected: FAIL — `AttributeError: module ... has no attribute 'build_action_packet'`.

- [ ] **Step 3: 라우터 수정**

`investment_reports.py` import 추가 (현재 ~43행 `build_review_sections` 옆):

```python
from app.services.investment_reports.action_packet import build_action_packet
```

`_serialise_bundle` 의 `review_sections = build_review_sections(...)` 직후에 추가하고, `InvestmentReportBundle(...)` 생성자에 필드 전달:

```python
    # ROB-335 — additive intraday ActionPacket projection (view-layer only).
    action_packet = build_action_packet(
        item_responses, report_response.snapshot_report_diagnostics
    )
```

그리고 `return InvestmentReportBundle(` 호출에 `action_packet=action_packet,` 추가.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/services/investment_reports/test_action_packet.py -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add app/routers/investment_reports.py tests/services/investment_reports/test_action_packet.py
git commit -m "feat(rob-335): attach ActionPacket projection to report bundle read model

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: 회귀 — KIS vs Toss 권위 미혼합 + user_id fail-closed + broker no-mutation

**Files:**
- Test: `tests/services/action_report/snapshot_backed/test_auto_emit.py` (회귀 추가)
- 확인: `tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py` (broker/mutation import guard — 변경 없이 통과 확인)

- [ ] **Step 1: 회귀 테스트 추가**

```python
def test_intraday_floor_never_classifies_reference_holdings() -> None:
    # Toss/manual rows live in reference_holdings; primary_source != 'kis'
    # means _held_kis_symbols returns {} -> no held_actions promoted.
    payload = {
        "primary_source": "manual",
        "holdings": [{"ticker": "AAPL", "sellable_quantity": 3, "source": "manual"}],
        "reference_holdings": [{"ticker": "AAPL", "source": "toss"}],
        "count": 1, "market": "us",
    }
    snapshots = [_make_snapshot(kind="portfolio", payload=payload)]
    items = EvidenceAutoEmitter(intraday_floor=True).propose(
        snapshots=snapshots, request_market="us", account_scope="kis_live"
    )
    assert all(i.symbol != "AAPL" for i in items if i.evidence_snapshot.get("action_verdict") in
               {"sell_review", "keep", "no_add"})


def test_intraday_floor_user_id_missing_portfolio_yields_no_held_items() -> None:
    # primary_source='none' (user_id missing path) -> no holdings to classify;
    # the generator-level floor (Task 5) supplies the data_gap item instead.
    payload = {
        "primary_source": "none", "holdings": [], "reference_holdings": [],
        "count": 0, "market": "kr",
    }
    snapshots = [_make_snapshot(kind="portfolio", payload=payload)]
    items = EvidenceAutoEmitter(intraday_floor=True).propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    held_verdicts = {"sell_review", "keep", "no_add", "data_gap"}
    assert not [i for i in items if i.symbol and
                i.evidence_snapshot.get("action_verdict") in held_verdicts]
```

- [ ] **Step 2: 실패/통과 확인**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_auto_emit.py -v -k "reference_holdings or user_id_missing"`
Expected: PASS (Task 4 구현이 `_held_kis_symbols` 가드를 그대로 사용하므로 통과해야 함; 실패하면 가드 누락 — 구현 수정).

- [ ] **Step 3: mutation import guard 확인 (변경 없음)**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py -v`
Expected: PASS — 신규 모듈(`action_verdict.py`/`intraday_floor.py`/`action_packet.py`)이 broker/order/watch/LLM import을 들이지 않아 guard 유지.

- [ ] **Step 4: 커밋**

```bash
git add tests/services/action_report/snapshot_backed/test_auto_emit.py
git commit -m "test(rob-335): regression — KIS/Toss authority + user_id fail-closed in intraday_floor

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: 전체 검증 — lint, 풀 테스트, read-only smoke

**Files:** 없음 (검증 전용). 안전: 어떤 broker/order/watch mutation도 발생시키지 않는다.

- [ ] **Step 1: 변경 영역 lint**

Run: `uv run ruff check app/ tests/`
Expected: `All checks passed!` (없으면 수정 후 재실행).

- [ ] **Step 2: 관련 테스트 모듈 풀 실행**

Run:
```bash
uv run pytest tests/services/action_report/ tests/services/investment_reports/ -v
```
Expected: PASS (신규 + 기존 회귀 전부). 기존 baseline 실패가 있으면 [[project_local_public_base_url_test_failures]] 처럼 환경성 noise인지 구분하여 기록.

- [ ] **Step 3: import guard + no-internal-LLM 재확인**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py tests/services/action_report/snapshot_backed/test_generator_safety.py -v`
Expected: PASS.

- [ ] **Step 4: read-only 스모크 evidence (선택, 자격증명 있는 호스트에서만)**

KR/KIS live advisory intraday 리포트 1건을 operator-triggered 경로로 생성하고, 응답 `items_count >= 1` 및 조회 bundle의 `action_packet`(held_actions/new_buy_candidates/no_action_reason/data_gaps_for_next_cycle)에 명시 답변이 채워졌는지 확인. broker/order/watch row 변화가 없음을 확인(생성 전후 카운트 동일). 자격증명이 없으면 이 단계는 스킵하고 사유를 PR 본문에 남긴다 ([[project_rob319_kiwoom_mock_lifecycle]] 처럼 live-smoke deferred 표기).

- [ ] **Step 5: 최종 커밋 (검증 메모, 변경 없으면 스킵)**

검증 중 수정이 있었다면 해당 커밋. 없으면 커밋 없음.

---

## PR2 (프론트엔드, 별도 plan)

spec §3.6/§7 대로 PR1 머지 후 fresh `main`에서 별도 plan으로 진행한다. 범위: `/invest/reports` 최신 화면에 "오늘의 보유 액션 / 신규 후보 / 리스크 / 데이터 부족" 4헤더 렌더 + sub-verdict chip + `action_packet` payload 소비. ROB-322 5-section UI 컴포넌트(`frontend/invest/src/desktop/...`) 탐색을 선행해야 bite-sized step을 쓸 수 있어 본 plan에는 포함하지 않는다. (frontend vitest는 [[project_frontend_invest_vitest_threads_flaky]] 대로 `--pool=forks` 사용.)

---

## Self-Review

**Spec coverage:**
- §3.1 intent 분리 + non-empty invariant → Task 5 (`is_intraday_action` + `ensure_action_floor` + generator wiring). ✅
- §3.2 ActionPacket read-time projection → Task 2(스키마) + Task 3(`build_action_packet`) + Task 6(라우터 연결). ✅
- §3.3 보유종목 결정론 분류기 (전수 + Toss 분리 + user_id fail-closed) → Task 1(규칙) + Task 4(intraday_floor 전수분류) + Task 7(회귀). ✅
- §3.4 신규후보 freshness 게이트 + no_new_buy 사유 → Task 4(stale_only 마커, 기존 `usefulness=='useful'` 게이트 유지) + Task 3(`no_new_buy_reason` projection). ✅
- §3.5 data_gap / 진단 연결 → Task 3(`_diagnostics_gaps` + 심볼 data_gap item). ✅ (market/news는 기존 evidence_snapshot 참조로 유지, 새 수집기 없음.)
- §4 안전 경계 → Task 7/8(mutation import guard, no-internal-LLM, read-only smoke). ✅
- §5 Acceptance criteria: items≠[] (Task 5), 신규후보 없음+사유 (Task 4/3), 보유종목 분류 (Task 1/4), unavailable→data_gap (Task 1), stale screener 제외 (기존 게이트 + Task 4 사유), KIS/Toss 권위 (Task 7), broker row 무변화 (Task 7/8). ✅
- §7 PR 슬라이싱: 본 plan = PR1; PR2는 별도. ✅

**Placeholder scan:** Task 5 Step 6 / Task 6 Step 1 / Task 8 Step 4는 기존 통합 fixture·자격증명 의존이라 구현 시 기존 패턴 참조를 명시했다(코드 골격 + 정확한 어서션 제공). 그 외 모든 스텝은 실제 코드 포함. ✅

**Type consistency:** `action_verdict`(str, evidence_snapshot 키) ↔ `VERDICT_TO_BUCKET` ↔ `decision_bucket`(locked 5) 전 Task 일관. `IngestReportItem.intent`는 항상 5-literal(buy_review/sell_review/risk_review/trend_recovery_review/rebalance_review) 내에서만 사용(keep/no_add → `rebalance_review`, data_gap/marker → `risk_review`). `ActionPacketEntry.verdict`는 `ActionVerdictLiteral`. `build_action_packet(items, diagnostics)` 시그니처가 Task 3/6 동일. ✅
