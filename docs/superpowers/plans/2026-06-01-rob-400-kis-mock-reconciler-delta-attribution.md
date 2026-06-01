# ROB-400 kis_mock reconciler delta-attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** kis_mock reconciler가 동일종목 다중주문의 단일 보유 델타를 한 번만 배분하도록 고치고(이중계상 제거), order-history의 `lifecycle_state`와 `status/filled_qty`를 정합시킨다.

**Architecture:** 순수 함수 `classify_orders`를 "주문 독립 평가"에서 "종목+side별 델타 예산(budget) 배분"으로 재작성한다. 매수=고가 우선/매도=저가 우선, 동가는 (trade_date,id) 오래된 주문 우선, 이미 `fill`인 주문이 예산을 최우선 소진. 각 주문에 귀속된 수량 `attributed_fill_qty`를 proposal·`last_reconcile_detail`(JSONB)에 실어, shadow order-history가 이 값으로 status/filled_qty를 파생한다. 새 lifecycle 상태·DB 컬럼·마이그레이션 없음.

**Tech Stack:** Python 3.13, dataclasses(frozen/slots), Decimal, pytest(`@pytest.mark.unit`), `uv run pytest`.

**Spec:** `docs/superpowers/specs/2026-06-01-rob-400-kis-mock-reconciler-delta-attribution-design.md`

---

## File Structure

| 파일 | 책임 | 변경 |
|------|------|------|
| `app/services/kis_mock_holdings_reconciler.py` | 순수 결정 로직 | `LedgerOrderInput`에 `price` 추가, `LifecycleTransitionProposal`에 `attributed_fill_qty` 추가, `classify_orders` 재작성 + 헬퍼 `_apportion_group`/`_proposal_for`. 커널 `classify_fill_by_delta` 보존. |
| `app/jobs/kis_mock_reconciliation_job.py` | DB↔reconciler 합성 | `LedgerOrderInput(price=...)` 전달, `detail`에 `attributed_fill_qty` 기록. |
| `app/mcp_server/tooling/kis_mock_ledger.py` | shadow order-history/exposure 투영 | `_shadow_row_to_order`가 status/filled_qty/remaining_qty를 lifecycle_state+attributed_fill_qty에서 파생(헬퍼 `_derive_shadow_fill`). |
| `tests/services/test_kis_mock_holdings_reconciler.py` | 순수 단위 테스트 | 신규 배분 케이스 추가. |
| `tests/mcp_server/test_kis_mock_shadow_order_history.py` (신규) | shadow 파생 정합 테스트 | 신규 파일. |

---

## Task 1: dataclass 확장 (price, attributed_fill_qty)

**Files:**
- Modify: `app/services/kis_mock_holdings_reconciler.py:34-66`
- Test: `tests/services/test_kis_mock_holdings_reconciler.py`

- [ ] **Step 1: 실패하는 테스트 추가**

`tests/services/test_kis_mock_holdings_reconciler.py` 맨 아래에 추가:

```python
@pytest.mark.unit
def test_ledger_order_input_has_price_default():
    from app.services.kis_mock_holdings_reconciler import LedgerOrderInput

    order = LedgerOrderInput(
        ledger_id=1,
        symbol="005930",
        side="buy",
        ordered_qty=Decimal("10"),
        lifecycle_state="accepted",
        holdings_baseline_qty=Decimal("0"),
        accepted_at=_now(),
    )
    assert order.price == Decimal("0")  # default keeps existing call sites working

    priced = LedgerOrderInput(
        ledger_id=2,
        symbol="005930",
        side="buy",
        ordered_qty=Decimal("10"),
        lifecycle_state="accepted",
        holdings_baseline_qty=Decimal("0"),
        accepted_at=_now(),
        price=Decimal("15900"),
    )
    assert priced.price == Decimal("15900")


@pytest.mark.unit
def test_proposal_attributed_fill_qty_defaults_none():
    from app.services.kis_mock_holdings_reconciler import LifecycleTransitionProposal

    p = LifecycleTransitionProposal(
        ledger_id=1,
        symbol="005930",
        prior_state="accepted",
        next_state="pending",
        reason_code="pending_unconfirmed",
        observed_holdings_qty=Decimal("0"),
        observed_delta=Decimal("0"),
    )
    assert p.attributed_fill_qty is None
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/services/test_kis_mock_holdings_reconciler.py::test_ledger_order_input_has_price_default tests/services/test_kis_mock_holdings_reconciler.py::test_proposal_attributed_fill_qty_defaults_none -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'price'` / AttributeError.

- [ ] **Step 3: dataclass에 필드 추가**

`app/services/kis_mock_holdings_reconciler.py` — `LedgerOrderInput`에 `price` 필드를 **맨 끝(default 포함)** 으로 추가:

```python
@dataclass(frozen=True, slots=True)
class LedgerOrderInput:
    ledger_id: int
    symbol: str
    side: Literal["buy", "sell"]
    ordered_qty: Decimal
    lifecycle_state: OrderLifecycleState
    holdings_baseline_qty: Decimal | None
    accepted_at: datetime
    price: Decimal = Decimal("0")
```

같은 파일 `LifecycleTransitionProposal`에 `attributed_fill_qty` 필드 추가(default None):

```python
@dataclass(frozen=True, slots=True)
class LifecycleTransitionProposal:
    ledger_id: int
    symbol: str
    prior_state: OrderLifecycleState
    next_state: OrderLifecycleState
    reason_code: ReasonCode
    observed_holdings_qty: Decimal | None
    observed_delta: Decimal | None
    attributed_fill_qty: Decimal | None = None
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/services/test_kis_mock_holdings_reconciler.py::test_ledger_order_input_has_price_default tests/services/test_kis_mock_holdings_reconciler.py::test_proposal_attributed_fill_qty_defaults_none -v`
Expected: PASS (2 passed).

- [ ] **Step 5: 기존 reconciler 테스트 회귀 확인**

Run: `uv run pytest tests/services/test_kis_mock_holdings_reconciler.py -v`
Expected: 기존 테스트 전부 PASS (single-order 케이스는 group=1이라 동작 불변).

- [ ] **Step 6: 커밋**

```bash
git add app/services/kis_mock_holdings_reconciler.py tests/services/test_kis_mock_holdings_reconciler.py
git commit -m "feat(ROB-400): add price + attributed_fill_qty fields to reconciler dataclasses

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: classify_orders를 종목별 델타 예산 배분으로 재작성 (데모 재현 케이스)

**Files:**
- Modify: `app/services/kis_mock_holdings_reconciler.py:103-204` (classify_orders 본문 + 헬퍼/임포트)
- Test: `tests/services/test_kis_mock_holdings_reconciler.py`

- [ ] **Step 1: 데모 재현 실패 테스트 추가**

`tests/services/test_kis_mock_holdings_reconciler.py`에 추가. 동일종목 2 매수, 보유 +10 → 고가 1건만 fill:

```python
def _buy(
    *,
    ledger_id: int,
    price: Decimal,
    ordered_qty: Decimal = Decimal("10"),
    baseline: Decimal = Decimal("0"),
    state: str = "accepted",
    accepted_age_sec: int = 0,
) -> LedgerOrderInput:
    return LedgerOrderInput(
        ledger_id=ledger_id,
        symbol="0148J0",
        side="buy",
        ordered_qty=ordered_qty,
        lifecycle_state=state,
        holdings_baseline_qty=baseline,
        accepted_at=_now() - timedelta(seconds=accepted_age_sec),
        price=price,
    )


@pytest.mark.unit
def test_same_symbol_double_buy_single_delta_attributed_to_higher_price():
    # ROB-400 demo: ledger23 @15,500 / ledger24 @15,900, actual holdings +10 (one fill)
    orders = [
        _buy(ledger_id=23, price=Decimal("15500"), accepted_age_sec=120),
        _buy(ledger_id=24, price=Decimal("15900"), accepted_age_sec=60),
    ]
    proposals = classify_orders(
        orders=orders,
        holdings={"0148J0": HoldingsSnapshot(
            symbol="0148J0", quantity=Decimal("10"), taken_at=_now()
        )},
        thresholds=ReconcilerThresholds(),
        now=_now(),
    )
    by_id = {p.ledger_id: p for p in proposals}
    # higher price (15,900) wins the single +10 budget
    assert by_id[24].next_state == "fill"
    assert by_id[24].reason_code == "fill_detected"
    assert by_id[24].attributed_fill_qty == Decimal("10")
    # the other stays pending — no double count
    assert by_id[23].next_state == "pending"
    assert by_id[23].reason_code == "pending_unconfirmed"
    assert by_id[23].attributed_fill_qty == Decimal("0")
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/services/test_kis_mock_holdings_reconciler.py::test_same_symbol_double_buy_single_delta_attributed_to_higher_price -v`
Expected: FAIL — 현재 로직은 ledger23도 `fill`(observed_delta +10)로 판정.

- [ ] **Step 3: classify_orders 재작성 + 헬퍼 추가**

`app/services/kis_mock_holdings_reconciler.py` 상단 import에 `defaultdict` 추가(line 14 근처):

```python
from collections import defaultdict
from collections.abc import Mapping, Sequence
```

`classify_orders`(현재 103~204행) 전체를 아래로 교체. `classify_fill_by_delta`(76~96) 및 `_pending_or_stale`(207~215)는 그대로 둔다:

```python
def _anomaly(
    order: LedgerOrderInput, reason: ReasonCode
) -> LifecycleTransitionProposal:
    return LifecycleTransitionProposal(
        ledger_id=order.ledger_id,
        symbol=order.symbol,
        prior_state=order.lifecycle_state,
        next_state="anomaly",
        reason_code=reason,
        observed_holdings_qty=None,
        observed_delta=None,
        attributed_fill_qty=None,
    )


def classify_orders(
    *,
    orders: Sequence[LedgerOrderInput],
    holdings: Mapping[str, HoldingsSnapshot],
    thresholds: ReconcilerThresholds,
    now: datetime,
) -> list[LifecycleTransitionProposal]:
    proposals: list[LifecycleTransitionProposal] = []
    groups: dict[tuple[str, str], list[LedgerOrderInput]] = defaultdict(list)

    for order in orders:
        if order.lifecycle_state in _TERMINAL:
            continue
        if order.lifecycle_state not in _RECONCILABLE_INPUTS:
            # planned/previewed/submitted/anomaly are out of scope here.
            continue
        if order.holdings_baseline_qty is None:
            proposals.append(_anomaly(order, "baseline_missing"))
            continue
        if holdings.get(order.symbol) is None:
            proposals.append(_anomaly(order, "holdings_snapshot_missing"))
            continue
        groups[(order.symbol, order.side)].append(order)

    for (symbol, side), group in groups.items():
        snapshot = holdings[symbol]
        proposals.extend(
            _apportion_group(
                group=group,
                snapshot=snapshot,
                side=side,
                now=now,
                thresholds=thresholds,
            )
        )

    return proposals


def _apportion_group(
    *,
    group: list[LedgerOrderInput],
    snapshot: HoldingsSnapshot,
    side: str,
    now: datetime,
    thresholds: ReconcilerThresholds,
) -> list[LifecycleTransitionProposal]:
    # Reference = position just before this competing batch. Terminal orders
    # already dropped out, so their fills are baked into later baselines.
    reference = min(o.holdings_baseline_qty for o in group)  # type: ignore[type-var]
    raw_budget = (
        snapshot.quantity - reference
        if side == "buy"
        else reference - snapshot.quantity
    )
    budget = raw_budget if raw_budget > 0 else Decimal("0")

    # Priority: already-fill first (consume prior attribution), then aggressive
    # price (buy DESC / sell ASC), then oldest order (accepted_at, ledger_id).
    def _key(o: LedgerOrderInput) -> tuple[int, Decimal, datetime, int]:
        already_fill = 0 if o.lifecycle_state == "fill" else 1
        price_key = -o.price if side == "buy" else o.price
        return (already_fill, price_key, o.accepted_at, o.ledger_id)

    out: list[LifecycleTransitionProposal] = []
    for order in sorted(group, key=_key):
        take = min(budget, order.ordered_qty) if budget > 0 else Decimal("0")
        budget -= take
        out.append(
            _proposal_for(
                order=order,
                snapshot=snapshot,
                take=take,
                now=now,
                thresholds=thresholds,
            )
        )
    return out


def _proposal_for(
    *,
    order: LedgerOrderInput,
    snapshot: HoldingsSnapshot,
    take: Decimal,
    now: datetime,
    thresholds: ReconcilerThresholds,
) -> LifecycleTransitionProposal:
    # Per-order diagnostic delta keeps its historical meaning; attributed_fill_qty
    # is the authoritative apportioned quantity.
    per_order_delta = snapshot.quantity - order.holdings_baseline_qty  # type: ignore[operator]

    if order.lifecycle_state == "fill":
        if take >= order.ordered_qty:
            next_state: OrderLifecycleState = "reconciled"
            reason: ReasonCode = "position_reconciled"
        else:
            next_state = "anomaly"
            reason = "holdings_mismatch"
    elif take >= order.ordered_qty:
        next_state, reason = "fill", "fill_detected"
    elif take > 0:
        next_state, reason = "fill", "partial_fill_detected"
    else:
        next_state, reason = _pending_or_stale(order, now, thresholds)

    return LifecycleTransitionProposal(
        ledger_id=order.ledger_id,
        symbol=order.symbol,
        prior_state=order.lifecycle_state,
        next_state=next_state,
        reason_code=reason,
        observed_holdings_qty=snapshot.quantity,
        observed_delta=per_order_delta,
        attributed_fill_qty=take,
    )
```

`__all__`(218~227행)에 헬퍼는 추가하지 않는다(내부 전용). 기존 export 유지.

- [ ] **Step 4: 데모 테스트 통과 확인**

Run: `uv run pytest tests/services/test_kis_mock_holdings_reconciler.py::test_same_symbol_double_buy_single_delta_attributed_to_higher_price -v`
Expected: PASS.

- [ ] **Step 5: 기존 reconciler 테스트 회귀 확인**

Run: `uv run pytest tests/services/test_kis_mock_holdings_reconciler.py -v`
Expected: 전부 PASS. (single-order 케이스는 group=1, budget = snap−baseline 으로 종전과 동일.)

- [ ] **Step 6: 커밋**

```bash
git add app/services/kis_mock_holdings_reconciler.py tests/services/test_kis_mock_holdings_reconciler.py
git commit -m "fix(ROB-400): apportion symbol holdings delta as a single budget across orders

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: 배분 엣지 케이스 테스트 (tiebreaker / partial / external excess / sell)

**Files:**
- Test: `tests/services/test_kis_mock_holdings_reconciler.py`

이 태스크는 Task 2 구현을 검증하는 케이스들을 추가한다. 통과해야 정상이며, 실패 시 Task 2 구현을 고친다.

- [ ] **Step 1: 케이스 테스트 추가**

```python
@pytest.mark.unit
def test_same_price_tiebreak_oldest_order_wins():
    orders = [
        _buy(ledger_id=30, price=Decimal("15500"), accepted_age_sec=60),
        _buy(ledger_id=31, price=Decimal("15500"), accepted_age_sec=600),  # older
    ]
    proposals = classify_orders(
        orders=orders,
        holdings={"0148J0": HoldingsSnapshot(
            symbol="0148J0", quantity=Decimal("10"), taken_at=_now()
        )},
        thresholds=ReconcilerThresholds(),
        now=_now(),
    )
    by_id = {p.ledger_id: p for p in proposals}
    assert by_id[31].next_state == "fill"          # oldest wins the budget
    assert by_id[30].next_state == "pending"


@pytest.mark.unit
def test_partial_budget_goes_to_priority_order_only():
    orders = [
        _buy(ledger_id=40, price=Decimal("15900")),
        _buy(ledger_id=41, price=Decimal("15500")),
    ]
    proposals = classify_orders(
        orders=orders,
        holdings={"0148J0": HoldingsSnapshot(
            symbol="0148J0", quantity=Decimal("6"), taken_at=_now()
        )},  # +6 budget, each ordered 10
        thresholds=ReconcilerThresholds(),
        now=_now(),
    )
    by_id = {p.ledger_id: p for p in proposals}
    assert by_id[40].next_state == "fill"
    assert by_id[40].reason_code == "partial_fill_detected"
    assert by_id[40].attributed_fill_qty == Decimal("6")
    assert by_id[41].next_state == "pending"
    assert by_id[41].attributed_fill_qty == Decimal("0")


@pytest.mark.unit
def test_external_holdings_excess_not_flagged_anomaly():
    orders = [_buy(ledger_id=50, price=Decimal("15900"))]  # ordered 10
    proposals = classify_orders(
        orders=orders,
        holdings={"0148J0": HoldingsSnapshot(
            symbol="0148J0", quantity=Decimal("15"), taken_at=_now()
        )},  # +15 > ordered 10 (manual/external position)
        thresholds=ReconcilerThresholds(),
        now=_now(),
    )
    assert proposals[0].next_state == "fill"
    assert proposals[0].reason_code == "fill_detected"
    assert proposals[0].attributed_fill_qty == Decimal("10")  # capped, leftover ignored


@pytest.mark.unit
def test_already_fill_pair_only_one_reconciles_other_anomaly():
    orders = [
        _buy(ledger_id=60, price=Decimal("15900"), state="fill"),
        _buy(ledger_id=61, price=Decimal("15500"), state="fill"),
    ]
    proposals = classify_orders(
        orders=orders,
        holdings={"0148J0": HoldingsSnapshot(
            symbol="0148J0", quantity=Decimal("10"), taken_at=_now()
        )},  # only +10 supports a single 10-share fill
        thresholds=ReconcilerThresholds(),
        now=_now(),
    )
    by_id = {p.ledger_id: p for p in proposals}
    assert by_id[60].next_state == "reconciled"
    assert by_id[60].reason_code == "position_reconciled"
    assert by_id[61].next_state == "anomaly"
    assert by_id[61].reason_code == "holdings_mismatch"


@pytest.mark.unit
def test_sell_lower_price_wins_budget():
    def _sell(ledger_id, price):
        return LedgerOrderInput(
            ledger_id=ledger_id,
            symbol="005930",
            side="sell",
            ordered_qty=Decimal("10"),
            lifecycle_state="accepted",
            holdings_baseline_qty=Decimal("10"),  # held 10 before selling
            accepted_at=_now(),
            price=price,
        )

    proposals = classify_orders(
        orders=[_sell(70, Decimal("16000")), _sell(71, Decimal("15500"))],
        holdings={"005930": _snap(Decimal("0"))},  # sold 10 (delta -10)
        thresholds=ReconcilerThresholds(),
        now=_now(),
    )
    by_id = {p.ledger_id: p for p in proposals}
    assert by_id[71].next_state == "fill"          # lower ask (15,500) fills first
    assert by_id[70].next_state == "pending"
```

- [ ] **Step 2: 케이스 테스트 실행**

Run: `uv run pytest tests/services/test_kis_mock_holdings_reconciler.py -v -k "tiebreak or partial_budget or external_holdings or already_fill_pair or sell_lower_price"`
Expected: 5 passed. 실패 시 Task 2의 `_apportion_group`/`_proposal_for`를 수정해 통과시킨다(설계 §4.2 규칙 기준).

- [ ] **Step 3: 커밋**

```bash
git add tests/services/test_kis_mock_holdings_reconciler.py
git commit -m "test(ROB-400): cover delta-budget tiebreak/partial/excess/sell cases

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: job이 price를 전달하고 attributed_fill_qty를 detail에 기록

**Files:**
- Modify: `app/jobs/kis_mock_reconciliation_job.py:104-144`
- Test: `tests/jobs/test_kis_mock_reconciliation_job.py`

- [ ] **Step 1: 기존 job 테스트 패턴 확인**

Run: `sed -n '1,60p' tests/jobs/test_kis_mock_reconciliation_job.py`
Expected: fake lifecycle 서비스/holdings로 `run_kis_mock_reconciliation`을 호출하는 패턴 파악(아래 Step 2 테스트를 이 파일 컨벤션에 맞춰 작성).

- [ ] **Step 2: 실패 테스트 추가**

`tests/jobs/test_kis_mock_reconciliation_job.py` 맨 아래에 추가. 두 매수 ledger(23 @15,500 / 24 @15,900), 보유 +10 → dry_run transition에서 24만 fill·attributed 10, 23은 pending·attributed 0이 detail에 실리는지 검증. (이 파일에 이미 있는 fake/factory 헬퍼 이름을 재사용한다; 없으면 기존 테스트의 fake 구성 그대로 복제한다.)

```python
@pytest.mark.unit
async def test_reconciliation_attributes_single_delta_and_records_attributed_qty():
    rows = [
        _make_ledger_row(
            ledger_id=23, symbol="0148J0", side="buy", quantity=Decimal("10"),
            price=Decimal("15500"), lifecycle_state="accepted",
            holdings_baseline_qty=Decimal("0"),
        ),
        _make_ledger_row(
            ledger_id=24, symbol="0148J0", side="buy", quantity=Decimal("10"),
            price=Decimal("15900"), lifecycle_state="accepted",
            holdings_baseline_qty=Decimal("0"),
        ),
    ]
    fake_db = _FakeSession()
    svc_rows = rows
    kis_client = _FakeKISClient(kr_holdings=[{"pdno": "0148J0", "hldg_qty": "10"}])

    result = await run_kis_mock_reconciliation(
        fake_db, dry_run=True, kis_client=kis_client,
        # inject open rows via the same mechanism existing tests use
    )

    transitions = {t["ledger_id"]: t for t in result["transitions"]}
    assert transitions[24]["next_state"] == "fill"
    assert transitions[23]["next_state"] == "pending"
    # attributed_fill_qty is recorded in the applied detail / event payload
    events = {e["detail"]["ledger_id"]: e["detail"] for e in result["events"]}
    assert events[24]["attributed_fill_qty"] == "10"
    assert events[23]["attributed_fill_qty"] == "0"
```

> 주의: `run_kis_mock_reconciliation`은 `lifecycle_svc.list_open_orders`로 행을 읽는다. 위 테스트의 `_make_ledger_row`/`_FakeSession`/`_FakeKISClient`/open-rows 주입은 **이 테스트 파일에 이미 존재하는 헬퍼와 동일한 방식**으로 작성한다(Step 1에서 확인). 새 픽스처를 발명하지 말 것.

- [ ] **Step 2b: 테스트 실패 확인**

Run: `uv run pytest tests/jobs/test_kis_mock_reconciliation_job.py::test_reconciliation_attributes_single_delta_and_records_attributed_qty -v`
Expected: FAIL — `attributed_fill_qty` 키가 detail/event에 없음(또는 ledger23이 fill).

- [ ] **Step 3: job 구현 수정**

`app/jobs/kis_mock_reconciliation_job.py` — `LedgerOrderInput` 생성에 `price` 추가(104-119 블록):

```python
    order_inputs: list[LedgerOrderInput] = [
        LedgerOrderInput(
            ledger_id=row.id,
            symbol=row.symbol,
            side=row.side,
            ordered_qty=_to_decimal(row.quantity),
            lifecycle_state=row.lifecycle_state,
            holdings_baseline_qty=(
                Decimal(str(row.holdings_baseline_qty))
                if row.holdings_baseline_qty is not None
                else None
            ),
            accepted_at=row.trade_date,
            price=_to_decimal(row.price),
        )
        for row in open_rows
    ]
```

같은 파일 `detail` 딕셔너리(132-144 블록)에 `attributed_fill_qty` 추가:

```python
        detail = {
            "observed_holdings_qty": (
                str(proposal.observed_holdings_qty)
                if proposal.observed_holdings_qty is not None
                else None
            ),
            "observed_delta": (
                str(proposal.observed_delta)
                if proposal.observed_delta is not None
                else None
            ),
            "attributed_fill_qty": (
                str(proposal.attributed_fill_qty)
                if proposal.attributed_fill_qty is not None
                else None
            ),
        }
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/jobs/test_kis_mock_reconciliation_job.py -v`
Expected: 신규 테스트 PASS + 기존 전부 PASS.

- [ ] **Step 5: 커밋**

```bash
git add app/jobs/kis_mock_reconciliation_job.py tests/jobs/test_kis_mock_reconciliation_job.py
git commit -m "fix(ROB-400): pass order price + persist attributed_fill_qty in reconcile detail

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: shadow order-history가 lifecycle+attributed_fill_qty에서 status/filled_qty 파생 (Fix #3)

**Files:**
- Modify: `app/mcp_server/tooling/kis_mock_ledger.py:97-120`
- Test: `tests/mcp_server/test_kis_mock_shadow_order_history.py` (신규)

- [ ] **Step 1: 신규 테스트 파일 작성(실패)**

`tests/mcp_server/test_kis_mock_shadow_order_history.py`:

```python
"""ROB-400: shadow order-history must not contradict lifecycle_state."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.mcp_server.tooling.kis_mock_ledger import _shadow_row_to_order


def _row(*, lifecycle_state, quantity, detail):
    return SimpleNamespace(
        id=24,
        order_no=None,
        symbol="0148J0",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=Decimal(str(quantity)),
        price=Decimal("15900"),
        amount=Decimal("159000"),
        currency="KRW",
        trade_date=datetime(2026, 6, 1, 9, 30, tzinfo=UTC),
        lifecycle_state=lifecycle_state,
        last_reconcile_detail=detail,
    )


@pytest.mark.unit
def test_pending_row_is_unfilled():
    out = _shadow_row_to_order(
        _row(lifecycle_state="pending", quantity=10, detail=None)
    )
    assert out["status"] == "pending"
    assert out["filled_qty"] == 0.0
    assert out["remaining_qty"] == 10.0


@pytest.mark.unit
def test_fill_row_with_full_attribution_reports_filled():
    out = _shadow_row_to_order(
        _row(
            lifecycle_state="fill",
            quantity=10,
            detail={"attributed_fill_qty": "10"},
        )
    )
    assert out["status"] == "filled"
    assert out["filled_qty"] == 10.0
    assert out["remaining_qty"] == 0.0


@pytest.mark.unit
def test_fill_row_with_partial_attribution_reports_partial():
    out = _shadow_row_to_order(
        _row(
            lifecycle_state="fill",
            quantity=10,
            detail={"attributed_fill_qty": "6"},
        )
    )
    assert out["status"] == "partial"
    assert out["filled_qty"] == 6.0
    assert out["remaining_qty"] == 4.0


@pytest.mark.unit
def test_fill_row_without_attribution_falls_back_to_full_fill():
    # legacy/confirm-path fill rows lacking attributed_fill_qty must still not
    # contradict lifecycle="fill" (never status=pending with filled_qty=0).
    out = _shadow_row_to_order(
        _row(lifecycle_state="fill", quantity=10, detail=None)
    )
    assert out["status"] == "filled"
    assert out["filled_qty"] == 10.0
    assert out["remaining_qty"] == 0.0
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/mcp_server/test_kis_mock_shadow_order_history.py -v`
Expected: FAIL — fill 행이 status="pending"/filled 0으로 나옴(현 하드코딩).

- [ ] **Step 3: `_derive_shadow_fill` 헬퍼 추가 + `_shadow_row_to_order` 수정**

`app/mcp_server/tooling/kis_mock_ledger.py` — `_shadow_row_to_order`(97행) 바로 위에 헬퍼 추가:

```python
def _derive_shadow_fill(row: KISMockOrderLedger, ordered_qty: float) -> tuple[float, float, str]:
    """Derive (filled_qty, remaining_qty, status) consistent with lifecycle_state.

    A row in ``fill`` must never report status=pending/filled_qty=0. When the
    reconciler recorded ``attributed_fill_qty`` (ROB-400) we honor it; legacy
    fill rows without it fall back to a full fill so lifecycle and status agree.
    """
    if row.lifecycle_state != "fill":
        return 0.0, ordered_qty, "pending"

    detail = row.last_reconcile_detail or {}
    raw = detail.get("attributed_fill_qty")
    if raw is None:
        filled = ordered_qty
    else:
        try:
            filled = float(raw)
        except (TypeError, ValueError):
            filled = ordered_qty
        filled = max(0.0, min(filled, ordered_qty))
    remaining = ordered_qty - filled
    status = "filled" if filled >= ordered_qty else "partial"
    return filled, remaining, status
```

`_shadow_row_to_order`(97-120행) 본문을 아래로 교체:

```python
def _shadow_row_to_order(row: KISMockOrderLedger) -> dict[str, Any]:
    ordered_at = row.trade_date.isoformat() if row.trade_date else None
    ordered_qty = _decimal_to_float(row.quantity)
    filled_qty, remaining_qty, status = _derive_shadow_fill(row, ordered_qty)
    return {
        "order_id": row.order_no or f"ledger:{row.id}",
        "ledger_id": row.id,
        "symbol": row.symbol,
        "market": "kr" if row.instrument_type == "equity_kr" else "us",
        "instrument_type": row.instrument_type,
        "side": row.side,
        "order_type": row.order_type,
        "status": status,
        "lifecycle_state": row.lifecycle_state,
        "ordered_qty": ordered_qty,
        "remaining_qty": remaining_qty,
        "filled_qty": filled_qty,
        "ordered_price": _decimal_to_float(row.price),
        "amount": _decimal_to_float(row.amount),
        "currency": row.currency,
        "ordered_at": ordered_at,
        "created_at": ordered_at,
        "source": KIS_MOCK_SHADOW_PENDING_SOURCE,
        "confidence": KIS_MOCK_SHADOW_PENDING_CONFIDENCE,
        "warning": KIS_MOCK_SHADOW_PENDING_WARNING,
    }
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/mcp_server/test_kis_mock_shadow_order_history.py -v`
Expected: 4 passed.

- [ ] **Step 5: shadow exposure 회귀 확인**

`_get_kis_mock_shadow_exposure`는 sell_reserved에 `remaining_qty`를 쓴다. fill된 sell의 remaining이 줄어드는 것은 의도된 개선이다. 기존 exposure 테스트가 깨지지 않는지 확인:

Run: `uv run pytest tests/ -v -k "shadow_exposure or shadow_pending or kis_mock_ledger"`
Expected: PASS (없으면 0 selected — 무방).

- [ ] **Step 6: 커밋**

```bash
git add app/mcp_server/tooling/kis_mock_ledger.py tests/mcp_server/test_kis_mock_shadow_order_history.py
git commit -m "fix(ROB-400): derive shadow order status/filled_qty from lifecycle+attributed_fill_qty

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: 전체 검증 + lint/typecheck

**Files:** (변경 없음 — 검증 단계)

- [ ] **Step 1: 관련 테스트 일괄 실행**

Run:
```bash
uv run pytest tests/services/test_kis_mock_holdings_reconciler.py \
  tests/jobs/test_kis_mock_reconciliation_job.py \
  tests/mcp_server/test_kis_mock_shadow_order_history.py \
  tests/test_kis_mock_holdings_delta_fill.py \
  tests/test_kis_mock_overseas_holdings_confirm.py -v
```
Expected: 전부 PASS.

- [ ] **Step 2: lint (ruff check + format) + typecheck**

Run:
```bash
uv run ruff check app/services/kis_mock_holdings_reconciler.py app/jobs/kis_mock_reconciliation_job.py app/mcp_server/tooling/kis_mock_ledger.py tests/services/test_kis_mock_holdings_reconciler.py tests/jobs/test_kis_mock_reconciliation_job.py tests/mcp_server/test_kis_mock_shadow_order_history.py
uv run ruff format --check app/services/kis_mock_holdings_reconciler.py app/jobs/kis_mock_reconciliation_job.py app/mcp_server/tooling/kis_mock_ledger.py tests/services/test_kis_mock_holdings_reconciler.py tests/jobs/test_kis_mock_reconciliation_job.py tests/mcp_server/test_kis_mock_shadow_order_history.py
make typecheck
```
Expected: clean. (format --check 실패 시 `uv run ruff format <files>` 후 재커밋.)

- [ ] **Step 3: 정합 커밋(필요 시)**

```bash
git add -A
git commit -m "chore(ROB-400): lint/format fixes

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review 노트 (작성자 확인)

- **Spec §4.2 배분 규칙** → Task 2/3 (예산, 우선순위, 이미-fill 최우선, pending 유지, anomaly 모순만). ✅
- **Spec §4.3 lifecycle↔status 정합** → Task 5. ✅
- **Spec §5 영향 범위 3파일** → Task 2/4/5. 마이그레이션 0, 새 상태 0. ✅
- **Spec §7 테스트 8케이스** → Task 2(데모) + Task 3(tiebreak/partial/excess/already-fill/sell) + Task 5(정합 4) + Task 1(dataclass) + Task 6(회귀). ✅
- 타입/시그니처 일관: `attributed_fill_qty`(Proposal), `price`(LedgerOrderInput), `_apportion_group`/`_proposal_for`/`_derive_shadow_fill` 이름 통일. ✅
- Out-of-scope(ROB-404 correlation, live ROB-395, ROB-406)는 Task 없음(의도). ✅
