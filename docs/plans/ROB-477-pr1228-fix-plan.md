# ROB-477 PR #1228 Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PR #1228(매도 래더 fill-safety 경고)의 리뷰 블로커 2건을 수정한다 — (1) 경고가 dead 모듈에만 있고 실제 주문 경로에 배선되지 않음, (2) `ladder_missing_near_market_anchor`가 `ladder_all_above_market`와 수학적으로 동일 조건이라 near-market 시맨틱 미구현.

**Architecture:** 순수 헬퍼 `evaluate_ladder_fill_safety`를 `app/services/orders/ladder_fill_safety.py`에 신설하고(시맨틱 수정 포함), 기존 `app/services/action_report/us/order_preview.py`는 이 헬퍼에 위임. 실 운영 경로 배선은 2종: (a) `kis_live_place_order` dry_run이 통과하는 `_preview_sell`(`app/mcp_server/tooling/order_validation.py`)에 per-order `sell_limit_above_market` 경고, (b) 래더 전체를 분석하는 read-only MCP 도구 `sell_ladder_fill_preview`(브로커 호출 0, mutation 0).

**Tech Stack:** Python 3.13 / uv / pytest / FastMCP / pydantic v2. 마이그레이션 0, 브로커 mutation 변경 0.

**작업 위치:** worktree `/Users/mgh3326/work/auto_trader.rob-477`, 브랜치 `rob-477` (PR #1228의 head — 추가 커밋으로 PR 갱신, 새 브랜치 만들지 말 것). 시작 전 `git status --short`가 clean인지 확인.

**커밋 규칙:** `Co-Authored-By: Paperclip <noreply@paperclip.ing>` 트레일러. 머지·gh pr merge 금지 — push 후 보고만.

**리뷰 근거 (왜 이 수정인가):**
- 블로커1: `preview_kis_us_live_order`는 프로덕션 호출자 0 (action_report/us `__init__` re-export + 테스트만). 실 주문은 `kis_live_place_order` → `_preview_order` → `_preview_sell` 경로인데 ladder 개념 없음 → 2026-06-09 사고 워크플로에서 경고 미발화.
- 블로커2: `order_preview.py`의 `near_market_anchor = marketable_anchor` — `above_market(>)`/`marketable(<=)`는 보수 관계라 두 경고가 iff로 동시 발화. `near_above_market`(임계 내 위쪽 rung) 변수는 계산만 되고 경고에 미사용 (ATR/0.3% 임계 전체가 장식).
- P2: rung <2 또는 `ladder_rungs` 미전달 시 분석 전체 silent skip, 본 주문 limit은 rung 미포함.
- P3: `bestBidUsd<=0` 시 fail-silent, rung limit<=0 검증 부재(경고 억제 가능), `(None, "holdingReferencePrice")` 소스 라벨 부정확, `reference_price_usd=0.0` 폴백 contract 변경.

**Out of scope (이 PR에서 하지 않음):**
- delayed KIS-overseas quote 기준가 경고 (이슈 보조 메모, 옵션) — 완료 보고에 follow-up 후보로만 명시.
- buy-side 래더 분석, KR/crypto 전용 로직 (헬퍼는 시장 무관 순수 계산이라 자연 호환).
- ROB-488(PR #1226, MCP 프로파일 분리)이 먼저 머지되면 도구 등록 위치 충돌 가능 — rebase 시 `register_order_tools` 쪽 등록 유지.

---

### Task 1: 공유 순수 헬퍼 `ladder_fill_safety.py` (시맨틱 수정의 본체)

**Files:**
- Create: `app/services/orders/ladder_fill_safety.py`
- Test: `tests/test_ladder_fill_safety.py` (신규)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_ladder_fill_safety.py`

```python
"""ROB-477: pure fill-safety analysis for sell limit ladders."""

import pytest

from app.services.orders.ladder_fill_safety import (
    LadderRung,
    evaluate_ladder_fill_safety,
)


def test_all_above_and_no_near_anchor_fires_both_warnings():
    warnings, details = evaluate_ladder_fill_safety(
        rungs=[LadderRung(limit_price=66.0, quantity=2.0),
               LadderRung(limit_price=68.0, quantity=3.0)],
        anchor_price=63.95,
    )
    assert "ladder_all_above_market" in warnings
    assert "ladder_missing_near_market_anchor" in warnings
    assert details["allRungsAboveMarket"] is True
    assert details["hasNearMarketAnchor"] is False


def test_all_above_but_lowest_rung_within_threshold_fires_only_all_above():
    # IONQ incident shape: 64.00 vs anchor 63.95 = +0.078% < 0.3% threshold.
    # Near-above rung IS a near-market anchor → second warning must be absent.
    warnings, details = evaluate_ladder_fill_safety(
        rungs=[LadderRung(limit_price=64.0, quantity=2.0),
               LadderRung(limit_price=66.0, quantity=3.0)],
        anchor_price=63.95,
    )
    assert "ladder_all_above_market" in warnings
    assert "ladder_missing_near_market_anchor" not in warnings
    assert details["hasNearMarketAnchor"] is True
    assert details["rungs"][0]["nearAboveMarket"] is True


def test_marketable_rung_clears_both_warnings():
    warnings, details = evaluate_ladder_fill_safety(
        rungs=[LadderRung(limit_price=63.95, quantity=2.0),
               LadderRung(limit_price=66.0, quantity=3.0)],
        anchor_price=63.95,
    )
    assert warnings == []
    assert details["allRungsAboveMarket"] is False
    assert details["hasMarketableAnchor"] is True


def test_atr_widens_near_threshold():
    # pct threshold = 63.95*0.3% ≈ 0.1919; ATR 4.0 * 0.3 = 1.2 → 64.9 is near.
    warnings, details = evaluate_ladder_fill_safety(
        rungs=[LadderRung(limit_price=64.9, quantity=1.0),
               LadderRung(limit_price=68.0, quantity=1.0)],
        anchor_price=63.95,
        atr=4.0,
    )
    assert "ladder_missing_near_market_anchor" not in warnings
    assert details["nearMarketThresholdUsd"] == pytest.approx(1.2)
    assert details["rungs"][1]["atrMultiple"] == pytest.approx(1.0125, abs=1e-4)


def test_single_rung_above_market_still_warns():
    warnings, _ = evaluate_ladder_fill_safety(
        rungs=[LadderRung(limit_price=70.0)],
        anchor_price=63.95,
    )
    assert "ladder_all_above_market" in warnings


def test_empty_rungs_or_bad_anchor_returns_no_analysis():
    assert evaluate_ladder_fill_safety(rungs=[], anchor_price=63.95) == ([], None)
    assert evaluate_ladder_fill_safety(
        rungs=[LadderRung(limit_price=64.0)], anchor_price=None
    ) == ([], None)
    assert evaluate_ladder_fill_safety(
        rungs=[LadderRung(limit_price=64.0)], anchor_price=0.0
    ) == ([], None)


def test_non_positive_rung_is_invalid_and_never_satisfies_anchor():
    # A garbage 0.0 rung must NOT suppress the warnings (review P3).
    warnings, details = evaluate_ladder_fill_safety(
        rungs=[LadderRung(limit_price=0.0, quantity=1.0),
               LadderRung(limit_price=66.0, quantity=2.0),
               LadderRung(limit_price=68.0, quantity=3.0)],
        anchor_price=63.95,
    )
    assert "ladder_all_above_market" in warnings
    assert "ladder_missing_near_market_anchor" in warnings
    assert details["invalidRungCount"] == 1
    assert details["rungs"][0]["invalid"] is True


def test_all_rungs_invalid_returns_no_analysis():
    assert evaluate_ladder_fill_safety(
        rungs=[LadderRung(limit_price=0.0), LadderRung(limit_price=-1.0)],
        anchor_price=63.95,
    ) == ([], None)


def test_suggested_anchor_rung_present_only_when_warning():
    warnings, details = evaluate_ladder_fill_safety(
        rungs=[LadderRung(limit_price=66.0), LadderRung(limit_price=68.0)],
        anchor_price=63.95,
    )
    assert details["suggestedAnchorRung"]["limitPriceUsd"] == 63.95
    clean_warnings, clean_details = evaluate_ladder_fill_safety(
        rungs=[LadderRung(limit_price=63.0), LadderRung(limit_price=66.0)],
        anchor_price=63.95,
    )
    assert clean_warnings == []
    assert "suggestedAnchorRung" not in clean_details
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_ladder_fill_safety.py -q`
Expected: FAIL — `ModuleNotFoundError: app.services.orders.ladder_fill_safety`

- [ ] **Step 3: 구현** — `app/services/orders/ladder_fill_safety.py` 생성

```python
"""Pure fill-safety analysis for multi-rung sell limit ladders (ROB-477).

No broker calls, no I/O, no DB. Shared by:
- app/services/action_report/us/order_preview.py (ROB-244 preview gate)
- app/mcp_server/tooling/orders_registration.py (sell_ladder_fill_preview tool)

Semantics (fixes the conflated PR #1228 first cut):
- ladder_all_above_market: every VALID rung is strictly above the anchor.
- ladder_missing_near_market_anchor: no VALID rung is marketable (<= anchor)
  NOR near-above-market (above anchor but within the near threshold).
  A rung slightly above the anchor counts as an anchor, so the two warnings
  are independent — an all-above ladder whose lowest rung sits within
  max(0.3% of anchor, 0.3 * ATR) gets only the first warning.
- Rungs with limit_price <= 0 are invalid: excluded from every aggregate and
  never satisfy the anchor requirement (a garbage rung must not silence
  warnings).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_NEAR_MARKET_ANCHOR_PCT = 0.3
DEFAULT_NEAR_MARKET_ANCHOR_ATR_MULTIPLE = 0.3

WARNING_ALL_ABOVE_MARKET = "ladder_all_above_market"
WARNING_MISSING_NEAR_MARKET_ANCHOR = "ladder_missing_near_market_anchor"


@dataclass(frozen=True)
class LadderRung:
    limit_price: float
    quantity: float | None = None


def _round_price(value: float) -> float:
    return round(value, 4)


def evaluate_ladder_fill_safety(
    *,
    rungs: Sequence[LadderRung],
    anchor_price: float | None,
    anchor_source: str | None = None,
    atr: float | None = None,
    near_market_pct: float = DEFAULT_NEAR_MARKET_ANCHOR_PCT,
    near_market_atr_multiple: float = DEFAULT_NEAR_MARKET_ANCHOR_ATR_MULTIPLE,
) -> tuple[list[str], dict[str, Any] | None]:
    """Return (warnings, details) for a sell limit ladder.

    Returns ([], None) when there is nothing to analyze: no valid rungs or a
    missing/non-positive anchor. Detail keys stay camelCase for byte-compat
    with the PR #1228 fillSafety payload.
    """
    if anchor_price is None or anchor_price <= 0:
        return [], None

    valid_rungs = [rung for rung in rungs if rung.limit_price > 0]
    invalid_rung_count = len(rungs) - len(valid_rungs)
    if not valid_rungs:
        return [], None

    near_threshold_usd = anchor_price * near_market_pct / 100.0
    if atr is not None and atr > 0:
        near_threshold_usd = max(near_threshold_usd, atr * near_market_atr_multiple)

    rung_details: list[dict[str, Any]] = []
    all_above_market = True
    has_marketable_anchor = False
    has_near_market_anchor = False

    for index, rung in enumerate(rungs, start=1):
        if rung.limit_price <= 0:
            rung_details.append(
                {
                    "index": index,
                    "quantity": rung.quantity,
                    "limitPriceUsd": _round_price(rung.limit_price),
                    "invalid": True,
                }
            )
            continue
        distance_usd = rung.limit_price - anchor_price
        distance_pct = distance_usd / anchor_price * 100.0
        above_market = rung.limit_price > anchor_price
        marketable_anchor = not above_market
        near_above_market = above_market and distance_usd <= near_threshold_usd
        is_near_market_anchor = marketable_anchor or near_above_market
        atr_multiple = (
            distance_usd / atr if atr is not None and atr > 0 else None
        )

        all_above_market = all_above_market and above_market
        has_marketable_anchor = has_marketable_anchor or marketable_anchor
        has_near_market_anchor = has_near_market_anchor or is_near_market_anchor
        rung_details.append(
            {
                "index": index,
                "quantity": rung.quantity,
                "limitPriceUsd": _round_price(rung.limit_price),
                "distanceUsd": _round_price(distance_usd),
                "distancePct": round(distance_pct, 4),
                "atrMultiple": (
                    round(atr_multiple, 4) if atr_multiple is not None else None
                ),
                "aboveMarket": above_market,
                "marketableAnchor": marketable_anchor,
                "nearAboveMarket": near_above_market,
                "nearMarketAnchor": is_near_market_anchor,
                "invalid": False,
            }
        )

    warnings: list[str] = []
    if all_above_market:
        warnings.append(WARNING_ALL_ABOVE_MARKET)
    if not has_near_market_anchor:
        warnings.append(WARNING_MISSING_NEAR_MARKET_ANCHOR)

    details: dict[str, Any] = {
        "anchorPriceUsd": _round_price(anchor_price),
        "anchorSource": anchor_source,
        "nearMarketThresholdPct": near_market_pct,
        "nearMarketThresholdUsd": _round_price(near_threshold_usd),
        "nearMarketAtrMultiple": near_market_atr_multiple,
        "atrUsd": _round_price(atr) if atr is not None else None,
        "allRungsAboveMarket": all_above_market,
        "hasMarketableAnchor": has_marketable_anchor,
        "hasNearMarketAnchor": has_near_market_anchor,
        "invalidRungCount": invalid_rung_count,
        "rungs": rung_details,
    }
    if warnings:
        details["suggestedAnchorRung"] = {
            "limitPriceUsd": _round_price(anchor_price),
            "rationale": (
                "place at least one sell rung at or near the anchor price "
                "(within the near-market threshold) to secure a partial fill"
            ),
        }

    return warnings, details
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_ladder_fill_safety.py -q`
Expected: 9 passed

- [ ] **Step 5: 커밋**

```bash
git add app/services/orders/ladder_fill_safety.py tests/test_ladder_fill_safety.py
git commit -m "feat(ROB-477): shared pure ladder fill-safety helper with distinct near-market anchor semantics

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 2: `action_report/us/order_preview.py`를 헬퍼에 위임 + anchor cascade 수정

**Files:**
- Modify: `app/services/action_report/us/order_preview.py` (PR #1228이 추가한 `_ladder_fill_safety`/`_round_price` 삭제, 위임으로 교체)
- Test: `tests/test_us_action_report_order_preview.py` (기존 테스트 1개 재작성 + 판별 테스트 추가)

- [ ] **Step 1: 기존 테스트를 올바른 시맨틱으로 먼저 수정 (RED 만들기)**

`tests/test_us_action_report_order_preview.py`의 `test_sell_preview_warns_when_ladder_is_entirely_above_market`에서 64.0 rung(anchor 63.95 대비 +0.078% < 0.3% 임계)은 near-market anchor이므로:

```python
    assert preview.status == "pass"
    assert "ladder_all_above_market" in preview.warnings
    assert "ladder_missing_near_market_anchor" not in preview.warnings  # ← in → not in
    fill_safety = preview.check_details["fillSafety"]
    assert fill_safety["allRungsAboveMarket"] is True
    assert fill_safety["hasMarketableAnchor"] is False
    assert fill_safety["hasNearMarketAnchor"] is True  # ← False → True
    assert fill_safety["suggestedAnchorRung"]["limitPriceUsd"] == 63.95
    assert fill_safety["rungs"][0]["distancePct"] == pytest.approx(0.0782)
    assert fill_safety["rungs"][0]["nearAboveMarket"] is True
```

그리고 두 경고를 판별하는 신규 테스트 2개를 같은 파일에 추가:

```python
def test_sell_preview_warns_missing_anchor_when_all_rungs_far_above_market():
    preview = preview_kis_us_live_order(
        account_snapshot=_snapshot(holdings=[_holding("IONQ", sellable_qty=8.0)]),
        request=KISUSOrderPreviewRequest(
            symbol="IONQ",
            side="sell",
            quantity=2.0,
            limit_price_usd=66.0,
            reference_price_usd=63.95,
            ladder_rungs=[
                {"quantity": 2.0, "limitPriceUsd": 66.0},
                {"quantity": 3.0, "limitPriceUsd": 68.0},
                {"quantity": 3.0, "limitPriceUsd": 70.0},
            ],
        ),
    )
    assert "ladder_all_above_market" in preview.warnings
    assert "ladder_missing_near_market_anchor" in preview.warnings


def test_sell_preview_without_explicit_ladder_analyzes_main_order_as_single_rung():
    # P2 fix: ladder_rungs 미전달이어도 본 주문 limit이 implicit rung으로 분석된다.
    preview = preview_kis_us_live_order(
        account_snapshot=_snapshot(holdings=[_holding("IONQ", sellable_qty=8.0)]),
        request=KISUSOrderPreviewRequest(
            symbol="IONQ",
            side="sell",
            quantity=2.0,
            limit_price_usd=70.0,
            reference_price_usd=63.95,
        ),
    )
    assert "ladder_all_above_market" in preview.warnings
    fill_safety = preview.check_details["fillSafety"]
    assert fill_safety["impliedFromSingleOrder"] is True
    assert len(fill_safety["rungs"]) == 1
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_us_action_report_order_preview.py -q`
Expected: 수정/신규 3개 FAIL (기존 구현은 중복 경고 + <2 rung skip)

- [ ] **Step 3: 구현 교체**

`app/services/action_report/us/order_preview.py`에서:

(a) import 추가, 로컬 중복 제거:

```python
from app.services.orders.ladder_fill_safety import (
    LadderRung,
    evaluate_ladder_fill_safety,
)
```

- 모듈 내 `_ladder_fill_safety` 함수 전체 삭제, `_round_price` 삭제(다른 사용처 없음), `_DEFAULT_NEAR_MARKET_ANCHOR_PCT`/`_DEFAULT_NEAR_MARKET_ANCHOR_ATR_MULTIPLE` 상수 삭제 (헬퍼의 DEFAULT_*로 일원화).

(b) `_reference_price_with_source` 교체 (P3: 0.0 폴백 복원 + None 소스 라벨 수정):

```python
def _reference_price_with_source(
    *,
    request: KISUSOrderPreviewRequest,
    holding: USHolding | None,
) -> tuple[float | None, str | None]:
    if request.reference_price_usd is not None and request.reference_price_usd > 0:
        return request.reference_price_usd, "referencePriceUsd"
    holding_price = _holding_reference_price(holding)
    if holding_price is None:
        return None, None
    return holding_price, "holdingReferencePrice"
```

(c) `_fill_anchor_price_with_source` 교체 (P3: bestBid<=0 fall-through + 소스 재사용):

```python
def _fill_anchor_price_with_source(
    *,
    request: KISUSOrderPreviewRequest,
    reference_price: float | None,
    reference_price_source: str | None,
) -> tuple[float | None, str | None]:
    if request.best_bid_usd is not None and request.best_bid_usd > 0:
        return request.best_bid_usd, "bestBidUsd"
    if reference_price is not None and reference_price > 0:
        return reference_price, reference_price_source
    return None, None
```

(d) `preview_kis_us_live_order` 내 `if side == "sell":` 블록 교체 (P2: implicit single rung):

```python
    if side == "sell":
        fill_anchor_price, fill_anchor_source = _fill_anchor_price_with_source(
            request=request,
            reference_price=reference_price,
            reference_price_source=reference_price_source,
        )
        implied_single_rung = not request.ladder_rungs
        ladder = request.ladder_rungs or [
            KISUSOrderPreviewLadderRung(
                quantity=request.quantity,
                limit_price_usd=request.limit_price_usd,
            )
        ]
        fill_warnings, fill_safety = evaluate_ladder_fill_safety(
            rungs=[
                LadderRung(limit_price=rung.limit_price_usd, quantity=rung.quantity)
                for rung in ladder
            ],
            anchor_price=fill_anchor_price,
            anchor_source=fill_anchor_source,
            atr=request.atr_usd,
        )
        warnings.extend(fill_warnings)
        if fill_safety is not None:
            fill_safety["impliedFromSingleOrder"] = implied_single_rung
            details["fillSafety"] = fill_safety
```

- [ ] **Step 4: 전체 파일 테스트 통과 확인 + 부수 영향 점검**

Run: `uv run pytest tests/test_us_action_report_order_preview.py -q`
Expected: 전부 PASS. 주의 — implicit single rung 도입으로 기존 다른 sell 테스트(예: `test_sell_preview_passes_with_kis_live_sellable_qty_and_submit_disabled`)에 `fillSafety`/`ladder_all_above_market`가 새로 나타날 수 있음. **경고는 informational이고 status="pass"는 불변이어야 함.** status가 바뀌면 구현 버그. 경고 목록을 exact-match로 비교하는 단언이 있으면 `in`/`not in`으로 완화.

- [ ] **Step 5: 커밋**

```bash
git add app/services/action_report/us/order_preview.py tests/test_us_action_report_order_preview.py
git commit -m "fix(ROB-477): delegate preview ladder analysis to shared helper; distinct near-market anchor warning + implicit single-rung analysis

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 3: 실 경로 배선 1 — `_preview_sell` per-order above-market 경고

`kis_live_place_order`(dry_run 포함)가 실제로 통과하는 경로. 2026-06-09 세션이었다면 8건 모두 이 경고를 받았다.

**Files:**
- Modify: `app/mcp_server/tooling/order_validation.py` (`_preview_sell`의 limit 분기)
- Test: `tests/test_mcp_place_order.py` (추가)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_mcp_place_order.py` 끝에 추가

파일 상단 import에 `from app.mcp_server.tooling import order_validation` 추가 (이미 있으면 생략), `from unittest.mock import AsyncMock`은 기존재.

```python
# ----------------------------------------------------------------------
# ROB-477: sell limit above market fill-risk warning (per-order)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_sell_limit_above_market_warns(monkeypatch):
    """limit 매도가 현재가 위면 informational 경고 + 거리 노출 (블록 아님)."""
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"quantity": 8.0, "avg_price": 40.0}),
    )
    result = await order_validation._preview_sell(
        symbol="IONQ",
        order_type="limit",
        quantity=2.0,
        price=64.0,
        current_price=63.95,
        market_type="equity_us",
    )
    assert "error" not in result
    assert "sell_limit_above_market" in result.get("warnings", [])
    assert result["fill_distance"]["distance_usd"] == pytest.approx(0.05)
    assert result["fill_distance"]["distance_pct"] == pytest.approx(0.0782, abs=1e-4)


@pytest.mark.asyncio
async def test_preview_sell_limit_at_market_no_warning(monkeypatch):
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"quantity": 8.0, "avg_price": 40.0}),
    )
    result = await order_validation._preview_sell(
        symbol="IONQ",
        order_type="limit",
        quantity=2.0,
        price=63.95,
        current_price=63.95,
        market_type="equity_us",
    )
    assert "error" not in result
    assert "sell_limit_above_market" not in result.get("warnings", [])
    assert "fill_distance" not in result
```

주의: `_get_holdings_for_order` 반환 dict 키는 `quantity`/`avg_price` — `_preview_sell` 본문이 그 두 키만 읽는지 먼저 확인하고, 추가 키를 읽으면 mock에 보강. avg_price=40.0은 `evaluate_sell_price_guards`의 avg*1.01 플로어(40.4 < 63.95)를 통과시키기 위함.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_mcp_place_order.py -q -k "preview_sell_limit"`
Expected: 2 FAIL (`warnings` 키 없음)

- [ ] **Step 3: 구현** — `app/mcp_server/tooling/order_validation.py`의 `_preview_sell`

limit 분기에서 `result["price"] = execution_price` 직후(분기 마지막, `order_quantity` 대입 다음 줄 근처)에 삽입:

```python
        # ROB-477: informational fill-risk surface — a sell limit above the
        # current price is a zero-fill tail risk if the market reverses
        # (2026-06-09: 8/8 all-above-market sell ladders filled nothing).
        # Never blocks; guards above already handled hard failures.
        if current_price > 0 and price > current_price:
            distance_usd = price - current_price
            result.setdefault("warnings", []).append("sell_limit_above_market")
            result["fill_distance"] = {
                "distance_usd": round(distance_usd, 4),
                "distance_pct": round(distance_usd / current_price * 100.0, 4),
            }
```

market 분기에는 넣지 않는다 (시장가는 fill-risk 없음). 블록/에러로 승격 금지 — 기존 guard 체계 불변.

- [ ] **Step 4: 통과 + 회귀 확인**

Run: `uv run pytest tests/test_mcp_place_order.py tests/test_kis_mock_loss_sell_guard.py tests/test_kis_mock_scalping_sell_guard.py tests/test_order_sell_routability_message.py -q`
Expected: 전부 PASS (sell into strength는 정상 패턴이라 기존 dry_run 테스트에 warnings 키가 새로 생겨도 동작 단언은 불변이어야 함; exact-dict 비교 단언이 깨지면 해당 단언만 키 단위 단언으로 완화)

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/order_validation.py tests/test_mcp_place_order.py
git commit -m "feat(ROB-477): warn sell_limit_above_market with fill distance in live/mock order preview path

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 4: 실 경로 배선 2 — read-only MCP 도구 `sell_ladder_fill_preview`

래더 전체(멀티-rung) 분석의 정식 surface. 순수 계산 — 브로커 호출 0, mutation 0, DB 0.

**Files:**
- Modify: `app/mcp_server/tooling/orders_registration.py` (도구 등록 + `ORDER_TOOL_NAMES` 추가)
- Modify: `app/mcp_server/tooling/orders_kis_variants.py` (kis_live 매도 관련 도구 description에 한 문장 너지)
- Test: `tests/test_mcp_place_order.py` (추가), `tests/test_mcp_profiles.py` (통과 확인만)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_mcp_place_order.py` 끝에 추가

```python
# ----------------------------------------------------------------------
# ROB-477: sell_ladder_fill_preview read-only tool
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_ladder_fill_preview_all_above_market():
    tools = build_tools()
    result = await tools["sell_ladder_fill_preview"](
        symbol="IONQ",
        anchor_price=63.95,
        rungs=[
            {"limit_price": 66.0, "quantity": 2.0},
            {"limit_price": 68.0, "quantity": 3.0},
        ],
    )
    assert result["success"] is True
    assert result["read_only"] is True
    assert "ladder_all_above_market" in result["warnings"]
    assert "ladder_missing_near_market_anchor" in result["warnings"]
    assert result["fill_safety"]["suggestedAnchorRung"]["limitPriceUsd"] == 63.95


@pytest.mark.asyncio
async def test_sell_ladder_fill_preview_near_anchor_only_all_above():
    tools = build_tools()
    result = await tools["sell_ladder_fill_preview"](
        symbol="IONQ",
        anchor_price=63.95,
        rungs=[
            {"limit_price": 64.0, "quantity": 2.0},
            {"limit_price": 68.0, "quantity": 3.0},
        ],
    )
    assert "ladder_all_above_market" in result["warnings"]
    assert "ladder_missing_near_market_anchor" not in result["warnings"]


@pytest.mark.asyncio
async def test_sell_ladder_fill_preview_rejects_bad_payload():
    tools = build_tools()
    result = await tools["sell_ladder_fill_preview"](
        symbol="IONQ",
        anchor_price=63.95,
        rungs=[{"price_typo": 64.0}],
    )
    assert result["success"] is False
    assert "limit_price" in result["error"] or "invalid" in result["error"]


@pytest.mark.asyncio
async def test_sell_ladder_fill_preview_rejects_non_positive_anchor():
    tools = build_tools()
    result = await tools["sell_ladder_fill_preview"](
        symbol="IONQ",
        anchor_price=0.0,
        rungs=[{"limit_price": 64.0}],
    )
    assert result["success"] is False
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_mcp_place_order.py -q -k "sell_ladder_fill_preview"`
Expected: 4 FAIL (KeyError: 'sell_ladder_fill_preview')

- [ ] **Step 3: 구현** — `app/mcp_server/tooling/orders_registration.py`

상단 import 추가:

```python
from app.services.orders.ladder_fill_safety import (
    LadderRung,
    evaluate_ladder_fill_safety,
)
```

`ORDER_TOOL_NAMES`에 추가:

```python
ORDER_TOOL_NAMES: set[str] = {
    "place_order",
    "modify_order",
    "cancel_order",
    "get_order_history",
    "kis_mock_reconciliation_run",
    "sell_ladder_fill_preview",
}
```

`register_order_tools(mcp)` 안에 도구 추가:

```python
    @mcp.tool(
        name="sell_ladder_fill_preview",
        description=(
            "[ROB-477] Read-only fill-safety analysis for a multi-rung SELL "
            "limit ladder. No broker calls, no order mutation. Pass "
            "anchor_price (current price or best bid from get_quote) and the "
            "FULL ladder as rungs=[{'limit_price': 64.0, 'quantity': 2.0}, ...]"
            "; atr optional (widens the near-market threshold to "
            "max(0.3% of anchor, 0.3*ATR)). Returns warnings: "
            "ladder_all_above_market (zero-fill tail risk on reversal — "
            "2026-06-09 incident: 8/8 all-above-market sell ladders filled "
            "nothing) and ladder_missing_near_market_anchor (no rung at or "
            "near the anchor), plus per-rung distance pct / ATR multiples and "
            "a suggested anchor rung. Run this BEFORE submitting multi-rung "
            "sell ladders via place_order / kis_live_place_order."
        ),
    )
    async def sell_ladder_fill_preview(
        symbol: str,
        anchor_price: float,
        rungs: list[dict[str, Any]],
        atr: float | None = None,
        anchor_source: str | None = None,
    ):
        try:
            parsed_rungs = [
                LadderRung(
                    limit_price=float(rung["limit_price"]),
                    quantity=(
                        float(rung["quantity"])
                        if rung.get("quantity") is not None
                        else None
                    ),
                )
                for rung in rungs
            ]
        except (KeyError, TypeError, ValueError) as exc:
            return {
                "success": False,
                "error": f"invalid rungs payload (need 'limit_price'): {exc!r}",
                "expected": "[{'limit_price': float, 'quantity': float|null}, ...]",
            }
        warnings, fill_safety = evaluate_ladder_fill_safety(
            rungs=parsed_rungs,
            anchor_price=anchor_price,
            anchor_source=anchor_source,
            atr=atr,
        )
        if fill_safety is None:
            return {
                "success": False,
                "error": (
                    "nothing to analyze: anchor_price must be > 0 and at least "
                    "one rung needs limit_price > 0"
                ),
                "symbol": symbol,
            }
        return {
            "success": True,
            "symbol": symbol,
            "read_only": True,
            "warnings": warnings,
            "fill_safety": fill_safety,
        }
```

- [ ] **Step 4: kis_live 도구 설명 너지** — `app/mcp_server/tooling/orders_kis_variants.py`

`grep -n "description" app/mcp_server/tooling/orders_kis_variants.py`로 `kis_live_place_order` 도구의 description 문자열을 찾아, 끝에 다음 한 문장을 덧붙인다 (기존 문구 수정 금지, append만):

```
"For multi-rung SELL limit ladders, run sell_ladder_fill_preview first to check zero-fill risk (ROB-477)."
```

- [ ] **Step 5: 통과 + 프로파일 회귀 확인**

Run: `uv run pytest tests/test_mcp_place_order.py tests/test_mcp_profiles.py -q`
Expected: 전부 PASS. `test_mcp_profiles.py`는 `ORDER_TOOL_NAMES <= mcp.tools.keys()` subset 단언이라 등록만 정확하면 통과. 만약 도구 개수/이름을 exact로 고정한 테스트가 있으면 신규 이름을 기대 집합에 추가.

- [ ] **Step 6: 커밋**

```bash
git add app/mcp_server/tooling/orders_registration.py app/mcp_server/tooling/orders_kis_variants.py tests/test_mcp_place_order.py
git commit -m "feat(ROB-477): read-only sell_ladder_fill_preview MCP tool + place-order description nudge

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 5: 전체 검증 스위프 + push + 보고

- [ ] **Step 1: 전체 게이트** (CI는 app/ + tests/ 둘 다 lint한다 — 과거 CI RED 교훈)

```bash
uv run pytest tests/test_ladder_fill_safety.py tests/test_us_action_report_order_preview.py tests/test_mcp_place_order.py tests/test_mcp_profiles.py tests/test_kis_mock_loss_sell_guard.py tests/test_kis_mock_scalping_sell_guard.py -q
uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/
uv run ty check app/ 2>&1 | tail -5
```

Expected: 테스트 전부 PASS, ruff/ty clean. format --check 실패 시 `uv run ruff format app/ tests/` 후 재커밋.

- [ ] **Step 2: 관련 영역 광역 스모크** (collector 시그니처 교훈 — 인접 테스트 전수)

```bash
uv run pytest tests/ -q -k "order or preview or ladder" -m "not integration" 2>&1 | tail -10
```

Expected: PASS (shared-DB run-ordering 오염 의심 시 단독 재실행으로 회귀 여부 판별)

- [ ] **Step 3: push (PR #1228 갱신, 머지 금지)**

```bash
git push origin rob-477
```

- [ ] **Step 4: 완료 보고** — 다음 형식으로 보고하고 종료 (PR 머지/Linear 상태 변경은 하지 말 것; 검증 세션이 수행):

```
DONE/BLOCKED 여부
- Task별 커밋 SHA 목록
- 테스트/린트 결과 요약 (개수, 실패 0 증거)
- 계획과 다르게 한 것 + 이유 (없으면 "없음")
- 미수행/보류 항목 (없으면 "없음")
- follow-up 후보: delayed-quote 기준가 경고 (이슈 보조메모, 미착수 고지)
```

---

## 검증 세션용 체크리스트 (플랜 작성자 본인 확인용 — 실행자는 무시)

- [ ] 두 경고가 독립인가: all-above + lowest-within-threshold 케이스에서 `ladder_missing_near_market_anchor` 부재 확인
- [ ] `near_above_market`가 경고 결정에 실사용되는가 (장식 아님)
- [ ] `_preview_sell` 경고가 informational인가 (어떤 경로에서도 error/block 승격 없음, 기존 guard 결과 불변)
- [ ] `sell_ladder_fill_preview`에 브로커/DB/주문 호출이 없는가 (순수 계산)
- [ ] `ORDER_TOOL_NAMES`/profiles 테스트 green, ROB-488 PR #1226 머지 시 충돌 재확인
- [ ] action_report 모듈의 `inspect.getsource` 기반 forbidden-method 테스트(`tests/test_us_action_report_order_preview.py:250`) 여전히 green
- [ ] 기존 PR #1228 테스트 2개의 단언이 새 시맨틱으로 갱신되었는가 (잘못된 동작 고정 해제)
- [ ] CI green 후: Linear ROB-477 코멘트 + 이슈의 acceptance 대비 잔여(delayed-quote 옵션) 명시
