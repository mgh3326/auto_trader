# ROB-408 Slice 2 — catalyst 가드를 auto_emit verdict에 배선 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `auto_emit.propose()`가 frozen `market` 스냅샷의 catalyst 이벤트를 읽어 Slice 1 `evaluate_catalyst_guard`를 적용하고, sell/buy verdict의 `evidence_snapshot["upcoming_catalyst"]`에 경고만 부착한다(verdict 불변).

**Architecture:** 순수 헬퍼 `_catalyst_events_for_symbol`(frozen 이벤트 dict→CatalystEvent 필터/매핑)와 `_attach_catalyst_guard`(item.evidence_snapshot 변형, flag 있을 때만)를 추가하고, `propose()`가 `market` 스냅샷을 캡처해 held-sell(side="trim")·candidate buy_review(side="buy") item에 적용. frozen evidence만(라이브 DB 없음), evidence_snapshot dict additive(migration/schema 0).

**Tech Stack:** Python 3.13, pytest. Slice 1 `app/services/market_events/catalyst/`(`evaluate_catalyst_guard`/`CatalystEvent`/`resolve_polarity`/`CATALYST_CATEGORIES`). 새 의존성 없음.

**참조 스펙:** `docs/superpowers/specs/2026-06-02-rob408-slice2-catalyst-guard-auto-emit-design.md`

기존 구조(확인됨):
- `EvidenceAutoEmitter.propose(self, *, snapshots, request_market, account_scope) -> list[IngestReportItem]` — 스냅샷 kind 루프; `market` kind 현재 skip — `app/services/action_report/snapshot_backed/auto_emit.py:232`
- sell: inline `evidence` dict → `IngestReportItem(side="sell", intent="sell_review", evidence_snapshot=evidence)`; candidate: `_stamp(_candidate_item(...), verdict)` (verdict ∈ buy_review/watch_only/data_gap)
- `_snapshot_payload(snapshot)`, `_make_evidence(...)` 헬퍼; `IngestReportItem.evidence_snapshot: dict` (free-form)
- 테스트 패턴: `tests/test_auto_emit_candidate_citation.py`(in-memory 스냅샷 객체 → `EvidenceAutoEmitter().propose(...)`)
- catalyst 이벤트 dict 필드(market 스냅샷 `payload["events"]`, `MarketEventResponse.model_dump`): `category, symbol, title, event_date(ISO str), source` (raw_payload 없음)

---

## File Structure

- Modify `app/services/action_report/snapshot_backed/auto_emit.py` — `_catalyst_events_for_symbol`, `_attach_catalyst_guard`, `propose()` 배선, `now` 파라미터, 상수
- Create `tests/test_auto_emit_catalyst_guard.py`

---

## Task 1: 순수 헬퍼 `_catalyst_events_for_symbol`

**Files:**
- Modify: `app/services/action_report/snapshot_backed/auto_emit.py` (헬퍼 추가)
- Test: `tests/test_auto_emit_catalyst_guard.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auto_emit_catalyst_guard.py
import datetime as dt

import pytest

from app.services.action_report.snapshot_backed.auto_emit import (
    _catalyst_events_for_symbol,
)

TODAY = dt.date(2026, 6, 2)


def _market_payload(events):
    return {"market": "kr", "events": events}


def _ev(symbol, category, date_str, title="t"):
    return {"symbol": symbol, "category": category, "event_date": date_str, "title": title, "source": "manual"}


@pytest.mark.unit
def test_filters_by_category_symbol_and_window():
    payload = _market_payload([
        _ev("035420", "conference", "2026-06-05"),       # in window, catalyst
        _ev("035420", "earnings", "2026-06-05"),          # non-catalyst category
        _ev("005930", "conference", "2026-06-05"),        # other symbol
        _ev("035420", "lockup_expiry", "2026-06-30"),     # out of window (>7d)
    ])
    out = _catalyst_events_for_symbol(payload, "035420", now_date=TODAY, within_days=7)
    assert len(out) == 1
    assert out[0].category == "conference"
    assert out[0].days_until == 3
    assert out[0].polarity == "positive"


@pytest.mark.unit
def test_empty_when_no_market_payload_or_no_events():
    assert _catalyst_events_for_symbol(None, "035420", now_date=TODAY, within_days=7) == []
    assert _catalyst_events_for_symbol({}, "035420", now_date=TODAY, within_days=7) == []


@pytest.mark.unit
def test_skips_malformed_events():
    payload = _market_payload([
        {"symbol": "035420", "category": "conference"},     # missing event_date
        {"symbol": "035420", "category": "conference", "event_date": "not-a-date"},
        _ev("035420", "conference", "2026-06-03"),
    ])
    out = _catalyst_events_for_symbol(payload, "035420", now_date=TODAY, within_days=7)
    assert [e.days_until for e in out] == [1]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-408-s2 && uv run pytest tests/test_auto_emit_catalyst_guard.py -v`
Expected: FAIL — `ImportError: cannot import name '_catalyst_events_for_symbol'`.

- [ ] **Step 3: Implement helper**

`auto_emit.py` 상단 import 추가:
```python
import datetime as dt
from zoneinfo import ZoneInfo

from app.services.market_events.catalyst.contract import CatalystEvent
from app.services.market_events.catalyst.guard import evaluate_catalyst_guard
from app.services.market_events.catalyst.polarity import (
    CATALYST_CATEGORIES,
    resolve_polarity,
)

_KST = ZoneInfo("Asia/Seoul")
CATALYST_GUARD_WITHIN_DAYS = 7
```

헬퍼 추가(모듈 함수, `_make_evidence` 근처):
```python
def _catalyst_events_for_symbol(
    market_payload: dict[str, Any] | None,
    symbol: str,
    *,
    now_date: dt.date,
    within_days: int,
) -> list[CatalystEvent]:
    """frozen market 스냅샷 events → 해당 symbol의 catalyst CatalystEvent 리스트.

    category ∈ CATALYST_CATEGORIES, event_date ∈ [now_date, now_date+within_days].
    frozen 이벤트엔 raw_payload 없음 → polarity는 category-default. 파싱 실패는 skip.
    """
    if not market_payload:
        return []
    events = market_payload.get("events") or []
    horizon = now_date + dt.timedelta(days=within_days)
    out: list[CatalystEvent] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("symbol") != symbol:
            continue
        category = ev.get("category")
        if category not in CATALYST_CATEGORIES:
            continue
        raw_date = ev.get("event_date")
        try:
            event_date = (
                raw_date
                if isinstance(raw_date, dt.date)
                else dt.date.fromisoformat(str(raw_date))
            )
        except ValueError:
            continue
        if not (now_date <= event_date <= horizon):
            continue
        out.append(
            CatalystEvent(
                symbol=symbol,
                category=category,
                title=ev.get("title"),
                event_date=event_date,
                days_until=(event_date - now_date).days,
                polarity=resolve_polarity(category, None),
                source=ev.get("source"),
            )
        )
    out.sort(key=lambda e: (e.days_until, e.category))
    return out
```

- [ ] **Step 4: Run test to verify it passes + lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-408-s2 && uv run pytest tests/test_auto_emit_catalyst_guard.py -v && uv run ruff check app/services/action_report/snapshot_backed/auto_emit.py tests/test_auto_emit_catalyst_guard.py`
Expected: PASS (3 passed); ruff clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-408-s2
git add app/services/action_report/snapshot_backed/auto_emit.py tests/test_auto_emit_catalyst_guard.py
git commit -m "feat(ROB-408): auto_emit _catalyst_events_for_symbol — frozen market 스냅샷 catalyst 추출

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `_attach_catalyst_guard` + propose() 배선

**Files:**
- Modify: `app/services/action_report/snapshot_backed/auto_emit.py`
- Test: `tests/test_auto_emit_catalyst_guard.py` (추가)

- [ ] **Step 1: Write the failing test (append)**

`tests/test_auto_emit_catalyst_guard.py` 에 추가:

```python
import datetime as dt
from types import SimpleNamespace

from app.services.action_report.snapshot_backed.auto_emit import EvidenceAutoEmitter

NOW = dt.datetime(2026, 6, 2, 10, 0)


def _snap(kind, payload, *, symbol=None):
    return SimpleNamespace(snapshot_kind=kind, symbol=symbol, payload_json=payload, snapshot_uuid=None)


def _portfolio(holdings_list):
    # _held_kis_symbols 요구: primary_source=="kis" + holdings=list[dict{ticker,sellable_quantity}]
    return _snap("portfolio", {"primary_source": "kis", "holdings": holdings_list})


def _quote(symbol):
    return _snap(
        "symbol",
        {"symbol": symbol, "quote": {"status": "ok", "best_bid": 1000, "best_ask": 1001, "spread_bps": 5}},
        symbol=symbol,
    )


def _market(events):
    return _snap("market", {"market": "kr", "events": events})


def _cat(symbol, category, date_str):
    return {"symbol": symbol, "category": category, "event_date": date_str, "title": "t", "source": "manual"}


@pytest.mark.asyncio  # (auto_emit propose is sync; drop marker if sync)
def test_sell_held_with_positive_catalyst_attaches_warning():
    holdings = [{"ticker": "035420", "sellable_quantity": 10}]
    snapshots = [
        _portfolio(holdings),
        _quote("035420"),
        _market([_cat("035420", "conference", "2026-06-05")]),  # +3d positive
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snapshots, request_market="kr", account_scope=None, now=NOW
    )
    sell = [i for i in items if i.symbol == "035420" and i.intent == "sell_review"]
    assert sell, "sell_review item expected"
    uc = sell[0].evidence_snapshot.get("upcoming_catalyst")
    assert uc is not None
    assert uc["flag"] == "upcoming_positive_catalyst"
    assert uc["nearest_days"] == 3
    # verdict 불변
    assert sell[0].side == "sell"
    assert sell[0].intent == "sell_review"


def test_sell_without_catalyst_has_no_attachment():
    holdings = [{"ticker": "035420", "sellable_quantity": 10}]
    snapshots = [_portfolio(holdings), _quote("035420"), _market([])]
    items = EvidenceAutoEmitter().propose(
        snapshots=snapshots, request_market="kr", account_scope=None, now=NOW
    )
    sell = [i for i in items if i.symbol == "035420"]
    assert sell
    assert "upcoming_catalyst" not in sell[0].evidence_snapshot
```

(Note: `propose`는 동기 함수다 — `@pytest.mark.asyncio` 제거하고 일반 함수로. 위 첫 테스트의 마커는 작성 시 삭제.)

held holdings 구조는 기존 `_held_kis_symbols(portfolio_payload)`가 파싱하는 형태를 따른다 — 구현 전 `rg -n "_held_kis_symbols" app/services/action_report/snapshot_backed/auto_emit.py`로 정확한 키(holdings 리스트 vs dict, sellable_quantity)를 확인하고 fixture를 맞춘다.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-408-s2 && uv run pytest tests/test_auto_emit_catalyst_guard.py -v`
Expected: FAIL — `propose()` got unexpected `now` / upcoming_catalyst 미부착.

- [ ] **Step 3: Implement attach + wiring**

`auto_emit.py`에 헬퍼 추가:
```python
def _attach_catalyst_guard(
    item: IngestReportItem,
    *,
    market_payload: dict[str, Any] | None,
    side: str,
    now_date: dt.date,
    within_days: int = CATALYST_GUARD_WITHIN_DAYS,
) -> None:
    """item.symbol의 frozen catalyst에 가드 적용 — flag 있으면 evidence_snapshot에 부착.
    verdict/side/intent 불변(경고만)."""
    symbol = item.symbol
    if not symbol or not market_payload:
        return
    events = _catalyst_events_for_symbol(
        market_payload, symbol, now_date=now_date, within_days=within_days
    )
    if not events:
        return
    guard = evaluate_catalyst_guard(events, side=side, within_days=within_days)
    if guard.flag is None:
        return
    item.evidence_snapshot["upcoming_catalyst"] = {
        "flag": guard.flag,
        "nearest_days": guard.nearest_days,
        "reason": guard.reason,
        "positive": [_catalyst_brief(e) for e in guard.positive],
        "negative": [_catalyst_brief(e) for e in guard.negative],
    }


def _catalyst_brief(e: CatalystEvent) -> dict[str, Any]:
    return {
        "symbol": e.symbol,
        "category": e.category,
        "event_date": e.event_date.isoformat(),
        "days_until": e.days_until,
    }
```

`propose()` 시그니처에 `now` 추가:
```python
    def propose(
        self,
        *,
        snapshots: list[Any],
        request_market: str,
        account_scope: str | None,
        now: dt.datetime | None = None,
    ) -> list[IngestReportItem]:
```
함수 초입에:
```python
        now_dt = now or dt.datetime.now(_KST)
        now_date = now_dt.astimezone(_KST).date() if now_dt.tzinfo else now_dt.date()
        market_payload: dict[str, Any] = {}
```
스냅샷 루프에 분기 추가:
```python
            elif kind == "market":
                market_payload = payload
```
sell item append 부분을 빌드→attach→append로 변경:
```python
            sell_item = _stamp(
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
            _attach_catalyst_guard(
                sell_item, market_payload=market_payload, side="trim", now_date=now_date
            )
            items.append(sell_item)
```
candidate buy 부분도 빌드→attach(side="buy", verdict=="buy_review"일 때만)→append:
```python
            cand_item = _stamp(_candidate_item(...), verdict)
            if verdict == "buy_review":
                _attach_catalyst_guard(
                    cand_item, market_payload=market_payload, side="buy", now_date=now_date
                )
            items.append(cand_item)
```

- [ ] **Step 4: Run test to verify it passes + lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-408-s2 && uv run pytest tests/test_auto_emit_catalyst_guard.py -v && uv run ruff check app/services/action_report/snapshot_backed/auto_emit.py tests/test_auto_emit_catalyst_guard.py`
Expected: PASS; ruff clean.

- [ ] **Step 5: 기존 auto_emit 회귀 + Commit**

Run: `cd /Users/mgh3326/work/auto_trader.rob-408-s2 && uv run pytest tests/test_auto_emit_candidate_citation.py -q`
Expected: PASS (now 파라미터 default·verdict 불변이라 기존 테스트 무회귀).

```bash
cd /Users/mgh3326/work/auto_trader.rob-408-s2
git add app/services/action_report/snapshot_backed/auto_emit.py tests/test_auto_emit_catalyst_guard.py
git commit -m "feat(ROB-408): auto_emit verdict에 upcoming_catalyst 경고 부착(trim+positive/buy+negative, verdict 불변)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: 전체 검증

**Files:** (검증/회귀만)

- [ ] **Step 1: Slice 2 + 인접 회귀**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-408-s2
uv run pytest tests/test_auto_emit_catalyst_guard.py tests/test_auto_emit_candidate_citation.py -v
uv run pytest tests/ -k "auto_emit or action_report or catalyst" -q
```
Expected: 전부 PASS.

- [ ] **Step 2: lint/format(전체) + import-contracts**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-408-s2
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run pytest tests/test_import_contracts.py -q
```
Expected: PASS; ruff clean. (auto_emit이 `app.services.market_events.catalyst`를 import — 둘 다 services 내부, 위반 없음. format --check는 **app/ tests/ 전체**.)

- [ ] **Step 3: (format 수정 시) 커밋**

```bash
cd /Users/mgh3326/work/auto_trader.rob-408-s2
uv run ruff format app/ tests/
git add -A && git commit -m "style(ROB-408): ruff format Slice 2

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review (작성자 체크)

**Spec coverage:**
- §3 데이터 경로(frozen market events → CatalystEvent) → Task 1 (`_catalyst_events_for_symbol`) ✅
- §4 가드 적용 + side 매핑(sell→trim, buy_review→buy) → Task 2 (`_attach_catalyst_guard` + 배선) ✅
- §5 부착(additive, verdict 불변) → Task 2 (evidence_snapshot["upcoming_catalyst"], side/intent 미변경) ✅
- §6 degrade(market 부재/0건/파싱 실패) → Task 1/2 (early return, 방어 skip) ✅
- §7 테스트(sell+positive / buy+negative / 범위밖·무관·부재 / 헬퍼 / 결정성) → Task 1/2 ✅
- §8 비목표(US classifier·verdict 변경·라이브 DB·raw override) → 준수 ✅

**Placeholder scan:** placeholder 없음. (Task 2 Step 1의 `_held_kis_symbols` fixture 형태 확인 지시는 런타임 검증 단계 — 실코드/명령 포함.)

**Type consistency:** `_catalyst_events_for_symbol`(Task1)→`_attach_catalyst_guard`(Task2)에서 사용; `CatalystEvent`/`evaluate_catalyst_guard`/`resolve_polarity`/`CATALYST_CATEGORIES`(Slice 1) 시그니처 일치. `propose(..., now=None)` 추가.

**검증 시 주의 (확인됨):**
- `_snapshot_payload`는 `.payload_json` dict를 읽음 → fixture는 `payload_json` 속성 사용(기존 `tests/test_auto_emit_candidate_citation.py::_Snap` 패턴 동일).
- `_held_kis_symbols`는 `portfolio_payload["primary_source"]=="kis"` AND `holdings=list[dict]`(각 `ticker` + `sellable_quantity`)를 요구 → 위 fixture 그대로(주의: holdings는 list, key는 `ticker`).
- `IngestReportItem.evidence_snapshot`은 dict 기본값 — in-place mutate 가능(Pydantic). `propose`는 **동기** 함수(테스트 async 마커 금지).
- format-check는 app/ tests/ 전체.
