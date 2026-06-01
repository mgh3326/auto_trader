# ROB-393 Watch Activation Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** review-operation watch 항목이 `watch_condition` 없이 생성·승인된 뒤 `activate_watch`에서 `"corrupt state"`로 영구 활성화 불가가 되는 계약 모순을, 활성화 시점 조건 주입 + actionable 거부로 해소한다.

**Architecture:** `ActivateWatchRequest`에 선택적 `watch_condition`/`valid_until`를 추가하고, `WatchActivationService.activate`가 item에 조건이 없을 때 (1) request로 주입되면 item에 영속화 후 진행, (2) 없으면 actionable 메시지로 거부, (3) 이미 있는데 또 주면 충돌 거부한다. 생성·승인 경로와 DB 스키마는 변경하지 않는다(마이그레이션 없음). 조건 자동 파생은 ROB-337 seam으로 남긴다.

**Tech Stack:** Python 3.13, FastMCP, Pydantic v2, SQLAlchemy async, pytest (`-n auto` shared Postgres).

**Spec:** `docs/superpowers/specs/2026-06-01-rob-393-watch-activation-contract-design.md`

---

## File Structure

- `app/schemas/investment_reports.py` — `ActivateWatchRequest`에 `watch_condition`/`valid_until` 추가 (재사용: `WatchConditionPayload`).
- `app/services/investment_reports/repository.py` — `update_item_watch_condition` DAO 메서드 추가.
- `app/services/investment_reports/watch_activation.py` — `activate()`의 condition/valid_until 가드 블록 교체.
- `app/mcp_server/tooling/investment_reports_handlers.py` — `investment_report_activate_watch_impl` 파라미터 패스스루 + 툴 description 갱신.
- `tests/test_investment_reports_mcp.py` — 재현/주입/충돌/회귀 테스트.

---

## Task 1: 재현 테스트 + review-watch 헬퍼 (RED)

**Files:**
- Test: `tests/test_investment_reports_mcp.py` (헬퍼 추가 ~라인 92 부근, 테스트 추가 ~라인 238 `test_activate_watch_copies_snapshot` 다음)

- [ ] **Step 1: review-watch 헬퍼 추가**

`_watch_item_dict`(라인 78-91) 바로 아래에 추가:

```python
def _review_watch_item_dict(client_item_key: str = "review-watch-1") -> dict:
    """operation='review' watch — 생성 시 watch_condition/valid_until 면제(ROB-274).

    ROB-393 재현용: 이 항목은 condition 없이 approve까지 도달하지만 종전
    activate_watch에서 'corrupt state'로 막혔다.
    """
    return {
        "client_item_key": client_item_key,
        "item_kind": "watch",
        "operation": "review",
        "symbol": "005930",
        "intent": "trend_recovery_review",
        "rationale": "r",
    }
```

- [ ] **Step 2: 재현 테스트 추가**

`test_activate_watch_copies_snapshot`(라인 220-237) 다음에 추가:

```python
@pytest.mark.asyncio
async def test_activate_review_watch_without_condition_is_actionable(
    session: AsyncSession,
) -> None:
    """ROB-393 재현: operation='review' watch는 condition 없이 approve되지만,
    인자 없이 activate하면 'corrupt state'가 아니라 actionable 에러여야 한다."""
    created = await investment_report_create_impl(
        items=[_review_watch_item_dict()], **_create_kwargs()
    )
    bundle = await investment_report_get_impl(created["report"]["report_uuid"])
    watch_uuid = bundle["items"][0]["item_uuid"]
    await investment_report_decide_item_impl(
        item_uuid=watch_uuid, decision="approve", actor="operator"
    )

    with pytest.raises(ValueError) as exc_info:
        await investment_report_activate_watch_impl(
            item_uuid=watch_uuid, actor="operator"
        )
    message = str(exc_info.value)
    assert "corrupt state" not in message
    assert "watch_condition not set" in message
```

- [ ] **Step 3: 재현 테스트 실행 → 실패 확인**

Run: `uv run pytest tests/test_investment_reports_mcp.py::test_activate_review_watch_without_condition_is_actionable -p no:randomly -v`
Expected: FAIL — 현재 메시지가 `"watch_condition missing on item (corrupt state)"`라 `"corrupt state" not in message` 어서션이 깨진다.

- [ ] **Step 4: 커밋**

```bash
git add tests/test_investment_reports_mcp.py
git commit -m "test(ROB-393): failing repro — review-watch activate must be actionable, not 'corrupt state'

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 2: 스키마 — `ActivateWatchRequest` 확장

**Files:**
- Modify: `app/schemas/investment_reports.py:310-315`

- [ ] **Step 1: 필드 추가**

`ActivateWatchRequest`(현 라인 310-315)를 교체:

```python
class ActivateWatchRequest(BaseModel):
    """Activate an approved watch item into ``investment_watch_alerts``."""

    item_uuid: UUID
    actor: str
    idempotency_key: str | None = None
    # ROB-393 — operation='review' watches are created without a condition
    # (schema + DB CHECK both exempt them). Allow supplying the condition /
    # expiry at activation time so such a watch can still be armed. Auto
    # derivation of the condition is out of scope (ROB-337 seam).
    watch_condition: WatchConditionPayload | None = None
    valid_until: datetime | None = None
```

(`WatchConditionPayload`와 `datetime`은 이 파일 상단에 이미 import/정의되어 있음 — 추가 import 불필요.)

- [ ] **Step 2: 임포트/구문 확인**

Run: `uv run python -c "from app.schemas.investment_reports import ActivateWatchRequest; ActivateWatchRequest(item_uuid='00000000-0000-0000-0000-000000000000', actor='x')"`
Expected: 에러 없이 종료(필드 기본값으로 인스턴스화 성공).

- [ ] **Step 3: 커밋**

```bash
git add app/schemas/investment_reports.py
git commit -m "feat(ROB-393): ActivateWatchRequest accepts optional watch_condition/valid_until

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 3: 리포지토리 — `update_item_watch_condition`

**Files:**
- Modify: `app/services/investment_reports/repository.py` (`update_item_status` 다음, 현 라인 182 뒤)

- [ ] **Step 1: DAO 메서드 추가**

`update_item_status`(라인 177-182) 바로 다음에 추가:

```python
    async def update_item_watch_condition(
        self,
        item_id: int,
        watch_condition: dict | None,
        valid_until: datetime | None,
    ) -> None:
        """ROB-393 — persist a watch_condition / valid_until injected at
        activation time onto a review-watch item. Only non-None values are
        written; a None field is left unchanged. Flushes but never commits
        (caller owns the transaction)."""
        values: dict[str, Any] = {}
        if watch_condition is not None:
            values["watch_condition"] = watch_condition
        if valid_until is not None:
            values["valid_until"] = valid_until
        if not values:
            return
        await self._session.execute(
            sa.update(InvestmentReportItem)
            .where(InvestmentReportItem.id == item_id)
            .values(**values)
        )
```

(`datetime`, `Any`, `sa`, `InvestmentReportItem`는 이미 import 되어 있음.)

- [ ] **Step 2: 임포트/구문 확인**

Run: `uv run python -c "from app.services.investment_reports.repository import InvestmentReportsRepository; print(hasattr(InvestmentReportsRepository, 'update_item_watch_condition'))"`
Expected: `True`

- [ ] **Step 3: 커밋**

```bash
git add app/services/investment_reports/repository.py
git commit -m "feat(ROB-393): repo.update_item_watch_condition to persist injected watch condition

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 4: 서비스 — `activate()` 조건 가드 교체 (Task 1 GREEN)

**Files:**
- Modify: `app/services/investment_reports/watch_activation.py:68-100`

- [ ] **Step 1: condition/valid_until 가드 블록 교체**

현재 라인 68-73:

```python
        if item.watch_condition is None:
            raise ValueError("watch_condition missing on item (corrupt state)")
        if item.valid_until is None:
            raise ValueError("valid_until missing on watch item (corrupt state)")
        if item.symbol is None:
            raise ValueError("symbol missing on watch item")
```

를 아래로 교체:

```python
        # ROB-393 — operation='review' watches are created without a
        # watch_condition/valid_until (schema + DB CHECK both exempt them).
        # Allow supplying them at activation time; persist back onto the item
        # so the item stays the source of truth and re-activation is idempotent.
        watch_condition = item.watch_condition
        valid_until = item.valid_until

        if request.watch_condition is not None:
            if watch_condition is not None:
                raise ValueError(
                    "watch_condition already set on item; refusing to override "
                    "at activation"
                )
            watch_condition = request.watch_condition.model_dump(mode="json")
        if request.valid_until is not None:
            if valid_until is not None:
                raise ValueError(
                    "valid_until already set on item; refusing to override "
                    "at activation"
                )
            valid_until = request.valid_until

        if watch_condition is None:
            raise ValueError(
                "watch_condition not set (operation='review' watch); pass "
                "watch_condition to activate, or recreate the watch with a "
                "condition"
            )
        if valid_until is None:
            raise ValueError(
                "valid_until not set (operation='review' watch); pass "
                "valid_until to activate, or recreate the watch with an expiry"
            )
        if item.symbol is None:
            raise ValueError("symbol missing on watch item")

        # Persist any injected fields before building the alert.
        await self._repo.update_item_watch_condition(
            item.id,
            watch_condition=(
                watch_condition if item.watch_condition is None else None
            ),
            valid_until=(valid_until if item.valid_until is None else None),
        )
```

- [ ] **Step 2: alert-build에서 주입값 사용**

현재 라인 79-101 블록에서 `item.watch_condition` / `item.valid_until` 참조를 위에서 만든 지역변수로 교체.

현재:
```python
        condition: dict[str, Any] = item.watch_condition
        threshold = _to_decimal(condition.get("threshold"))
```
→
```python
        condition: dict[str, Any] = watch_condition
        threshold = _to_decimal(condition.get("threshold"))
```

그리고 `insert_alert(...)` 호출의 마지막 인자:
```python
            valid_until=item.valid_until,
```
→
```python
            valid_until=valid_until,
```

(나머지 `insert_alert` 인자는 그대로 둔다.)

- [ ] **Step 3: 재현 테스트 통과 확인**

Run: `uv run pytest tests/test_investment_reports_mcp.py::test_activate_review_watch_without_condition_is_actionable -p no:randomly -v`
Expected: PASS — 인자 없는 activate가 `"watch_condition not set ..."` actionable 에러를 낸다(`"corrupt state"` 미포함).

- [ ] **Step 4: 기존 정상경로 회귀 확인**

Run: `uv run pytest tests/test_investment_reports_mcp.py::test_activate_watch_copies_snapshot -p no:randomly -v`
Expected: PASS — condition 있는 watch는 종전과 동일하게 활성화(인자 없이).

- [ ] **Step 5: 커밋**

```bash
git add app/services/investment_reports/watch_activation.py
git commit -m "fix(ROB-393): activate review-watch via injected condition + actionable refusal

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 5: 주입/충돌 테스트 (RED — 핸들러 파라미터 필요)

**Files:**
- Test: `tests/test_investment_reports_mcp.py` (Task 1 테스트 다음)

- [ ] **Step 1: 주입 성공 + 영속화 테스트 추가**

```python
@pytest.mark.asyncio
async def test_activate_review_watch_with_injected_condition_succeeds(
    session: AsyncSession,
) -> None:
    """ROB-393: review-watch도 activate 시 watch_condition/valid_until을 주면
    활성화되고, 주입된 조건이 item에 영속화된다."""
    created = await investment_report_create_impl(
        items=[_review_watch_item_dict()], **_create_kwargs()
    )
    bundle = await investment_report_get_impl(created["report"]["report_uuid"])
    watch_uuid = bundle["items"][0]["item_uuid"]
    await investment_report_decide_item_impl(
        item_uuid=watch_uuid, decision="approve", actor="operator"
    )

    response = await investment_report_activate_watch_impl(
        item_uuid=watch_uuid,
        actor="operator",
        watch_condition={"metric": "price", "operator": "below", "threshold": 70000},
        valid_until=future_datetime().isoformat(),
    )
    assert response["success"] is True
    assert response["alert"]["metric"] == "price"
    assert response["alert"]["operator"] == "below"
    assert response["item"]["status"] == "activated"

    # 주입 조건이 item에 영속화되었는지 확인.
    bundle_post = await investment_report_get_impl(created["report"]["report_uuid"])
    item_post = bundle_post["items"][0]
    assert item_post["watch_condition"]["metric"] == "price"
    assert item_post["valid_until"] is not None
```

- [ ] **Step 2: 충돌(override 거부) 테스트 추가**

```python
@pytest.mark.asyncio
async def test_activate_watch_rejects_condition_override(
    session: AsyncSession,
) -> None:
    """ROB-393: condition이 이미 있는 watch에 activate로 또 주면 silent override
    하지 않고 거부한다."""
    created = await investment_report_create_impl(
        items=[_watch_item_dict()], **_create_kwargs()
    )
    bundle = await investment_report_get_impl(created["report"]["report_uuid"])
    watch_uuid = bundle["items"][0]["item_uuid"]
    await investment_report_decide_item_impl(
        item_uuid=watch_uuid, decision="approve", actor="operator"
    )

    with pytest.raises(ValueError) as exc_info:
        await investment_report_activate_watch_impl(
            item_uuid=watch_uuid,
            actor="operator",
            watch_condition={"metric": "price", "operator": "below", "threshold": 1},
        )
    assert "already set" in str(exc_info.value)
```

- [ ] **Step 3: 실행 → 실패 확인**

Run: `uv run pytest tests/test_investment_reports_mcp.py::test_activate_review_watch_with_injected_condition_succeeds tests/test_investment_reports_mcp.py::test_activate_watch_rejects_condition_override -p no:randomly -v`
Expected: 주입 테스트는 FAIL — `investment_report_activate_watch_impl`가 아직 `watch_condition` kwarg를 받지 않아 `TypeError: unexpected keyword argument`. (충돌 테스트는 kwarg를 넘기므로 동일 `TypeError`.)

- [ ] **Step 4: 커밋**

```bash
git add tests/test_investment_reports_mcp.py
git commit -m "test(ROB-393): failing injection + override-refusal tests for activate_watch

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 6: MCP 핸들러 패스스루 (Task 5 GREEN)

**Files:**
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py:387-398` (impl), `:739-745` (description)

- [ ] **Step 1: impl 시그니처 + request 매핑 교체**

현재 라인 387-398:

```python
async def investment_report_activate_watch_impl(
    item_uuid: str,
    actor: str,
    idempotency_key: str | None = None,
) -> dict:
    request = ActivateWatchRequest.model_validate(
        {
            "item_uuid": item_uuid,
            "actor": actor,
            "idempotency_key": idempotency_key,
        }
    )
```

를 교체:

```python
async def investment_report_activate_watch_impl(
    item_uuid: str,
    actor: str,
    idempotency_key: str | None = None,
    watch_condition: dict | None = None,
    valid_until: str | None = None,
) -> dict:
    request = ActivateWatchRequest.model_validate(
        {
            "item_uuid": item_uuid,
            "actor": actor,
            "idempotency_key": idempotency_key,
            "watch_condition": watch_condition,
            "valid_until": valid_until,
        }
    )
```

- [ ] **Step 2: 툴 description 갱신**

현재 라인 739-745의 `activate_watch` 등록 description을 교체:

```python
    mcp.tool(
        name="investment_report_activate_watch",
        description=(
            "Activate an approved watch item into investment_watch_alerts "
            "as an immutable activation snapshot. Idempotent per source item. "
            "For operation='review' watches created without a condition, pass "
            "watch_condition (metric/operator/threshold) and valid_until to arm "
            "them; activating such a watch without a condition fails with an "
            "actionable error rather than 'corrupt state'."
        ),
    )(investment_report_activate_watch_impl)
```

- [ ] **Step 3: 주입/충돌 테스트 통과 확인**

Run: `uv run pytest tests/test_investment_reports_mcp.py::test_activate_review_watch_with_injected_condition_succeeds tests/test_investment_reports_mcp.py::test_activate_watch_rejects_condition_override -p no:randomly -v`
Expected: PASS (둘 다).

- [ ] **Step 4: 커밋**

```bash
git add app/mcp_server/tooling/investment_reports_handlers.py
git commit -m "feat(ROB-393): pass watch_condition/valid_until through activate_watch MCP tool

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 7: 전체 검증 + lint

**Files:** 없음 (검증/품질 게이트만)

- [ ] **Step 1: 투자리포트 테스트 전체 실행**

Run: `uv run pytest tests/test_investment_reports_mcp.py tests/test_investment_reports_model.py -p no:randomly -v`
Expected: 전부 PASS (신규 4 테스트 + 기존 회귀 무손상; model 테스트 불변).

- [ ] **Step 2: lint (CI 게이트와 동일하게)**

Run: `uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/`
Expected: 둘 다 통과. (format 실패 시 `uv run ruff format app/ tests/` 후 재확인하고 변경분 amend 커밋.)

- [ ] **Step 3: import guard / 타입 체크**

Run: `uv run ty check app/services/investment_reports app/schemas/investment_reports.py app/mcp_server/tooling/investment_reports_handlers.py`
Expected: 신규 변경 관련 에러 없음. (기존 베이스라인 노이즈는 무시하되, 본 변경이 새로 만든 에러는 없어야 함.)

- [ ] **Step 4: (정리 커밋 필요 시)**

```bash
git add -A
git commit -m "chore(ROB-393): lint/format

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Post-implementation (PR 외 수동 단계)

- PR 생성 전: 사전-머지 full-CI 게이트 — `ruff check app/ tests/` + import guards 통과 및 GitHub Test 워크플로우 green 확인 후에만 머지(`feedback_premerge_full_ci_gate`).
- Linear ROB-393 댓글: no broker/order/order-intent mutation + no scheduler activation 경계와 테스트/CI evidence, 그리고 ROB-337(조건 자동 파생) seam을 명시.
- ROB-412 완료 기준의 "scheduler/automation 비활성 유지"는 본 변경에 자동화 배선이 전혀 없음을 근거로 충족.

## 비범위 (재확인)

- DB migration 없음 (컬럼·CHECK 불변).
- approve 상태머신 변경 없음.
- 조건 자동 파생/매수가 정책 = ROB-337.
- live/mock 자동집행 = ROB-402.
