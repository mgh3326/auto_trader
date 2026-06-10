# ROB-500 — Watch Trigger Discord 메시지 개선 (Invest 딥링크 + 가격 가이드) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** watch trigger → Hermes webhook 페이로드에 Invest 딥링크(`invest_links`), 운영 액션 의미(`operator_action_guidance`), 소스 item의 `watch_recommendation` 기반 가격 가이드(`price_guidance`)를 additive로 추가하고, Invest 리포트 상세 UI에 anchor + active/triggered 섹션 분리를 넣는다.

**Architecture:** `ReviewTriggerPayload`(`app/services/hermes_client.py`, `extra="forbid"`)에 3개 optional 중첩 모델을 추가하고, 두 생성 지점(스캐너 `_upsert_event`, validity review `_notify`)에서 채운다. 가격 가이드는 `investment_report_items.watch_recommendation` JSONB의 4개 advisory 필드를 **그대로 복사**만 한다(없으면 `None` — 추론/생성 금지). 프론트는 `InvestmentReportBundleContent.tsx`에 row id anchor(`watch-event-{uuid}`/`watch-alert-{uuid}`/`watch-item-{uuid}`), hash 스크롤, active/triggered 섹션 분리를 추가한다. Discord 렌더링 자체는 Hermes(외부 시스템) 작업 — 계약 런북으로 인계한다.

**Tech Stack:** Python 3.13 + Pydantic v2 + SQLAlchemy async + pytest / React 19 + TypeScript + vitest + testing-library.

**중요 설계 결정:**
- **Migration 없음.** DB 스키마 변경 없음. 브로커/주문/감시 mutation 없음 (기존 locked semantics 유지).
- **Feature flag 없음.** 새 필드는 additive optional. Hermes 수신측이 strict 거부하더라도 스캐너는 delivery `failed` 기록 후 alert를 `active`로 남겨 다음 루프에 재시도하므로(기존 Plan 4 semantics) self-healing — 데이터 유실 없음. Hermes 렌더러 배포 순서는 운영 인계 문서에 명시.
- **가격 가이드는 verbatim-only.** `entry_review_below_price`, `suggested_limit_price_range`, `max_chase_price`, `invalidation` 4개만 추출. 익절/매도 목표는 스키마에 없으므로 절대 생성하지 않는다.
- **fail-open:** item 조회/파싱 실패는 경고 로그 후 `price_guidance=None`으로 전송. 트리거 통지 자체를 막지 않는다.

**Worktree/branch:** `/Users/mgh3326/work/auto_trader.rob-500`, branch `rob-500` (이미 체크아웃됨). PR base는 `main`. 커밋 트레일러: `Co-Authored-By: Paperclip <noreply@paperclip.ing>`.

---

### Task 1: Hermes 페이로드 계약 확장 — 모델 + 순수 헬퍼

**Files:**
- Modify: `app/services/hermes_client.py`
- Test: `tests/test_hermes_client.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_hermes_client.py`에 추가 (기존 import에 더해 필요한 것 import):

```python
from decimal import Decimal
from uuid import UUID

from app.services.hermes_client import (
    InvestLinks,
    OperatorActionGuidance,
    PriceGuidance,
    build_invest_links,
    build_operator_action_guidance,
    price_guidance_from_watch_recommendation,
)

_REPORT_UUID = UUID("70019e8d-1ee6-493f-adeb-5d9301d5ea48")
_EVENT_UUID = UUID("f912d55f-d1b3-4971-a362-998bd9ffa6b4")
_ALERT_UUID = UUID("5e32ec11-f4ed-4ef7-9a84-561a5fb2be79")


def test_build_invest_links_full() -> None:
    links = build_invest_links(
        market="crypto",
        symbol="KRW-BTC",
        source_report_uuid=_REPORT_UUID,
        event_uuid=_EVENT_UUID,
        alert_uuid=_ALERT_UUID,
    )
    assert links.report_path == f"/invest/reports/{_REPORT_UUID}"
    assert links.stock_path == "/invest/stocks/crypto/KRW-BTC"
    assert links.event_anchor == (
        f"/invest/reports/{_REPORT_UUID}#watch-event-{_EVENT_UUID}"
    )
    assert links.alert_anchor == (
        f"/invest/reports/{_REPORT_UUID}#watch-alert-{_ALERT_UUID}"
    )


def test_build_invest_links_without_event_uuid_omits_event_anchor() -> None:
    links = build_invest_links(
        market="kr", symbol="005930", source_report_uuid=_REPORT_UUID
    )
    assert links.event_anchor is None
    assert links.alert_anchor is None
    assert links.stock_path == "/invest/stocks/kr/005930"


def test_build_invest_links_quotes_symbol() -> None:
    links = build_invest_links(
        market="us", symbol="BRK.B", source_report_uuid=_REPORT_UUID
    )
    assert links.stock_path == "/invest/stocks/us/BRK.B"


def test_operator_action_guidance_mapping() -> None:
    g = build_operator_action_guidance(action_mode="notify_only", outcome="notified")
    assert g.requires_operator_review is False
    assert g.order_behavior == "none"
    assert "자동 주문 없음" in g.headline

    g = build_operator_action_guidance(
        action_mode="approval_required", outcome="review_required"
    )
    assert g.requires_operator_review is True
    assert g.order_behavior == "none"

    g = build_operator_action_guidance(
        action_mode="preview_only", outcome="preview_attached"
    )
    assert g.order_behavior == "preview_only"

    g = build_operator_action_guidance(
        action_mode="auto_execute_mock", outcome="executed"
    )
    assert g.order_behavior == "mock_only"


def test_operator_action_guidance_review_required_overrides() -> None:
    # validity review path: notify_only watch이지만 outcome=review_required
    g = build_operator_action_guidance(
        action_mode="notify_only", outcome="review_required"
    )
    assert g.requires_operator_review is True


def _full_recommendation() -> dict:
    return {
        "watch_reason": "r",
        "data_state": "ok",
        "reference_price": "110",
        "entry_review_below_price": "100",
        "suggested_limit_price_range": {"low": "95", "high": "100"},
        "max_chase_price": "102",
        "invalidation": {"kind": "price_below", "price": "80"},
        "review_cadence": "daily",
        "source_evidence": {"lookback_days": 20},
        "policy_version": "v1",
        "computed_at": "2026-06-01T00:00:00+00:00",
    }


def test_price_guidance_extracts_advisory_subset() -> None:
    guidance = price_guidance_from_watch_recommendation(_full_recommendation())
    assert guidance is not None
    assert guidance.entry_review_below_price == Decimal("100")
    assert guidance.suggested_limit_price_range is not None
    assert guidance.suggested_limit_price_range.low == Decimal("95")
    assert guidance.suggested_limit_price_range.high == Decimal("100")
    assert guidance.max_chase_price == Decimal("102")
    assert guidance.invalidation is not None
    assert guidance.invalidation.kind == "price_below"
    assert guidance.invalidation.price == Decimal("80")


def test_price_guidance_none_when_recommendation_missing() -> None:
    assert price_guidance_from_watch_recommendation(None) is None
    assert price_guidance_from_watch_recommendation("not-a-dict") is None  # type: ignore[arg-type]


def test_price_guidance_none_when_all_advisory_fields_absent() -> None:
    rec = _full_recommendation()
    for key in (
        "entry_review_below_price",
        "suggested_limit_price_range",
        "max_chase_price",
        "invalidation",
    ):
        rec[key] = None
    assert price_guidance_from_watch_recommendation(rec) is None


def test_price_guidance_none_when_subset_malformed() -> None:
    rec = _full_recommendation()
    rec["suggested_limit_price_range"] = {"low": "100", "high": "90"}  # low > high
    assert price_guidance_from_watch_recommendation(rec) is None


def test_payload_accepts_new_optional_fields_and_still_forbids_extras() -> None:
    payload = _base_payload()  # 기존 헬퍼 (tests/test_hermes_client.py:18)
    assert payload.invest_links is None
    assert payload.operator_action_guidance is None
    assert payload.price_guidance is None
    with pytest.raises(ValidationError):
        _base_payload(unknown_field=1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hermes_client.py -v`
Expected: 신규 테스트들이 `ImportError: cannot import name 'build_invest_links'` 류로 FAIL. 기존 테스트는 PASS 유지.

- [ ] **Step 3: Implement models + helpers in `app/services/hermes_client.py`**

import 수정:

```python
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, ValidationError

from app.schemas.investment_reports import (
    ItemIntentLiteral,
    MarketLiteral,
    TargetKindLiteral,
    WatchActionModeLiteral,
    WatchClauseOpLiteral,
    WatchInvalidation,
    WatchMetricLiteral,
    WatchPriceRange,
)
```

`ReviewTriggerPayload` 정의 **앞에** 추가:

```python
class InvestLinks(BaseModel):
    """ROB-500 — operator-facing Invest deep links (path only, no host).

    Hermes prepends its configured Invest base URL when rendering.
    """

    report_path: str
    stock_path: str
    event_anchor: str | None = None
    alert_anchor: str | None = None

    model_config = ConfigDict(extra="forbid")


class OperatorActionGuidance(BaseModel):
    """ROB-500 — what this notification means for the operator.

    Deterministically derived from action_mode/outcome; rendered at the
    top of the Discord card so the operator doesn't have to decode UUIDs.
    """

    headline: str
    requires_operator_review: bool
    order_behavior: Literal["none", "preview_only", "mock_only"]

    model_config = ConfigDict(extra="forbid")


class PriceGuidance(BaseModel):
    """ROB-500 — advisory price thresholds copied **verbatim** from the
    source item's ``watch_recommendation``. Never derived or invented in
    this path; absence means '가격 가이드 없음'. No take-profit / sell
    targets — the stored schema doesn't have them (locked scope).
    """

    entry_review_below_price: Decimal | None = None
    suggested_limit_price_range: WatchPriceRange | None = None
    max_chase_price: Decimal | None = None
    invalidation: WatchInvalidation | None = None

    model_config = ConfigDict(extra="forbid")


def build_invest_links(
    *,
    market: str,
    symbol: str,
    source_report_uuid: Any,
    event_uuid: Any | None = None,
    alert_uuid: Any | None = None,
) -> InvestLinks:
    report_path = f"/invest/reports/{source_report_uuid}"
    stock_path = (
        f"/invest/stocks/{quote(str(market).lower(), safe='')}"
        f"/{quote(str(symbol), safe='')}"
    )
    return InvestLinks(
        report_path=report_path,
        stock_path=stock_path,
        event_anchor=(
            f"{report_path}#watch-event-{event_uuid}" if event_uuid is not None else None
        ),
        alert_anchor=(
            f"{report_path}#watch-alert-{alert_uuid}" if alert_uuid is not None else None
        ),
    )


_GUIDANCE_BY_ACTION_MODE: dict[str, OperatorActionGuidance] = {
    "notify_only": OperatorActionGuidance(
        headline="알림 전용 — 자동 주문 없음, 필요 시 수동 검토",
        requires_operator_review=False,
        order_behavior="none",
    ),
    "approval_required": OperatorActionGuidance(
        headline="운영자 검토 필요 — 승인 전 주문 없음",
        requires_operator_review=True,
        order_behavior="none",
    ),
    "preview_only": OperatorActionGuidance(
        headline="주문 프리뷰 첨부 — 실제 주문 없음",
        requires_operator_review=False,
        order_behavior="preview_only",
    ),
    "auto_execute_mock": OperatorActionGuidance(
        headline="모의계좌 자동 실행 — 실계좌 주문 없음",
        requires_operator_review=False,
        order_behavior="mock_only",
    ),
}

_FALLBACK_GUIDANCE = OperatorActionGuidance(
    headline="알림 — 자동 주문 없음",
    requires_operator_review=False,
    order_behavior="none",
)

_REVIEW_REQUIRED_GUIDANCE = OperatorActionGuidance(
    headline="운영자 검토 필요 — 승인 전 주문 없음",
    requires_operator_review=True,
    order_behavior="none",
)


def build_operator_action_guidance(
    *, action_mode: str, outcome: str
) -> OperatorActionGuidance:
    base = _GUIDANCE_BY_ACTION_MODE.get(action_mode, _FALLBACK_GUIDANCE)
    if outcome == "review_required" and not base.requires_operator_review:
        # validity-review path reuses the trigger contract with
        # outcome='review_required' regardless of the watch's action_mode.
        return _REVIEW_REQUIRED_GUIDANCE
    return base


_PRICE_GUIDANCE_KEYS = (
    "entry_review_below_price",
    "suggested_limit_price_range",
    "max_chase_price",
    "invalidation",
)


def price_guidance_from_watch_recommendation(
    recommendation: dict[str, Any] | None,
) -> PriceGuidance | None:
    """Extract the advisory price subset, or ``None`` for '가격 가이드 없음'.

    Fail-open: malformed stored JSON logs a warning and returns ``None``
    rather than blocking the trigger notification.
    """
    if not isinstance(recommendation, dict):
        return None
    subset = {key: recommendation.get(key) for key in _PRICE_GUIDANCE_KEYS}
    if all(value is None for value in subset.values()):
        return None
    try:
        return PriceGuidance.model_validate(subset)
    except ValidationError:
        logger.warning(
            "watch_recommendation price-guidance subset failed validation — "
            "omitting guidance"
        )
        return None
```

`ReviewTriggerPayload`의 `outcome` 필드 아래(= `model_config` 위)에 추가:

```python
    # ROB-500 — operator-facing additions. Optional + additive so older
    # constructors keep working; populated by both send paths.
    invest_links: InvestLinks | None = None
    operator_action_guidance: OperatorActionGuidance | None = None
    price_guidance: PriceGuidance | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_hermes_client.py -v`
Expected: 전부 PASS (기존 + 신규).

- [ ] **Step 5: Commit**

```bash
git add app/services/hermes_client.py tests/test_hermes_client.py
git commit -m "feat(ROB-500): add invest_links/operator_action_guidance/price_guidance to Hermes payload contract

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 2: 스캐너 발화 경로 배선

**Files:**
- Modify: `app/jobs/investment_watch_scanner.py` (import 블록 + `_upsert_event` 내 payload 생성부, 현재 315–343행 부근)
- Test: `tests/test_investment_watch_scanner.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_investment_watch_scanner.py`에 추가 (기존 `_seed_active_kr_alert`, `_StubHermesClient` 재사용):

```python
def _recommendation_fixture() -> dict:
    return {
        "watch_reason": "r",
        "data_state": "ok",
        "reference_price": "110",
        "entry_review_below_price": "100",
        "suggested_limit_price_range": {"low": "95", "high": "100"},
        "max_chase_price": "102",
        "invalidation": {"kind": "price_below", "price": "80"},
        "review_cadence": "daily",
        "source_evidence": {"lookback_days": 20},
        "policy_version": "v1",
        "computed_at": "2026-06-01T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_trigger_payload_carries_links_guidance_and_price_guidance(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROB-500 — 발화 페이로드에 invest_links + 액션 가이드 + 가격 가이드."""
    alert = await _seed_active_kr_alert(session)
    repo = InvestmentReportsRepository(session)
    item = await repo.get_item_by_uuid(alert.source_item_uuid)
    assert item is not None
    await repo.update_item_watch_recommendation(item.id, _recommendation_fixture())
    await session.commit()

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)

    async def _fake_current_value(**_kwargs) -> float:
        return 25.0  # rsi below 30 → triggered

    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)

    stub = _StubHermesClient()
    scanner = InvestmentWatchScanner(hermes_client=stub)
    await scanner.scan_market("kr")

    assert len(stub.calls) == 1
    payload = stub.calls[0]

    assert payload.invest_links is not None
    assert payload.invest_links.report_path == (
        f"/invest/reports/{alert.source_report_uuid}"
    )
    assert payload.invest_links.stock_path == "/invest/stocks/kr/005930"
    assert payload.invest_links.event_anchor == (
        f"/invest/reports/{alert.source_report_uuid}"
        f"#watch-event-{payload.event_uuid}"
    )
    assert payload.invest_links.alert_anchor == (
        f"/invest/reports/{alert.source_report_uuid}"
        f"#watch-alert-{alert.alert_uuid}"
    )

    assert payload.operator_action_guidance is not None
    assert payload.operator_action_guidance.requires_operator_review is False
    assert payload.operator_action_guidance.order_behavior == "none"

    assert payload.price_guidance is not None
    assert payload.price_guidance.entry_review_below_price == Decimal("100")
    assert payload.price_guidance.suggested_limit_price_range.low == Decimal("95")
    assert payload.price_guidance.suggested_limit_price_range.high == Decimal("100")
    assert payload.price_guidance.max_chase_price == Decimal("102")
    assert payload.price_guidance.invalidation.kind == "price_below"


@pytest.mark.asyncio
async def test_trigger_payload_price_guidance_none_without_recommendation(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROB-500 — watch_recommendation 없는 watch는 가이드 추론 금지(None)."""
    await _seed_active_kr_alert(session)
    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)

    async def _fake_current_value(**_kwargs) -> float:
        return 25.0

    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)

    stub = _StubHermesClient()
    scanner = InvestmentWatchScanner(hermes_client=stub)
    await scanner.scan_market("kr")

    assert len(stub.calls) == 1
    assert stub.calls[0].price_guidance is None
    assert stub.calls[0].invest_links is not None  # 링크는 항상 채움
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_investment_watch_scanner.py -v -k "links_guidance or without_recommendation"`
Expected: `payload.invest_links is None` 으로 assert FAIL.

- [ ] **Step 3: Wire the scanner**

`app/jobs/investment_watch_scanner.py` import 블록 수정:

```python
from app.services.hermes_client import (
    HermesNotificationClient,
    ReviewTriggerPayload,
    build_invest_links,
    build_operator_action_guidance,
    price_guidance_from_watch_recommendation,
)
```

`_upsert_event` 안에서, `# Build the Hermes payload from the event row's ...` 주석 직전에 추가:

```python
        # ROB-500 — operator-facing price guidance from the source item's
        # advisory watch_recommendation. Fail-open: a lookup problem must
        # never block the trigger notification itself.
        price_guidance = None
        try:
            item = await repo.get_item_by_uuid(event.source_item_uuid)
            price_guidance = price_guidance_from_watch_recommendation(
                item.watch_recommendation if item is not None else None
            )
        except Exception:  # noqa: BLE001 - guidance is advisory, never fatal
            logger.warning(
                "watch_recommendation lookup failed for item %s — "
                "sending trigger without price guidance",
                event.source_item_uuid,
            )
```

`payload = ReviewTriggerPayload(...)` 호출의 `outcome=event.outcome,` 다음 줄에 추가:

```python
            invest_links=build_invest_links(
                market=event.market,
                symbol=event.symbol,
                source_report_uuid=event.source_report_uuid,
                event_uuid=event.event_uuid,
                alert_uuid=alert_uuid_value,
            ),
            operator_action_guidance=build_operator_action_guidance(
                action_mode=event.action_mode, outcome=event.outcome
            ),
            price_guidance=price_guidance,
```

- [ ] **Step 4: Run the full scanner test file**

Run: `uv run pytest tests/test_investment_watch_scanner.py -v`
Expected: 전부 PASS (기존 트리거/재시도/idempotency 테스트 포함 — 새 필드는 optional이라 기존 assert 깨지지 않아야 함).

- [ ] **Step 5: Commit**

```bash
git add app/jobs/investment_watch_scanner.py tests/test_investment_watch_scanner.py
git commit -m "feat(ROB-500): scanner trigger payload carries invest links + action/price guidance

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 3: validity review 경로 배선

**Files:**
- Modify: `app/services/investment_reports/watch_validity_review.py` (import + `review_market`의 `_notify` 호출 + `_notify` 본문)
- Test: `tests/test_watch_validity_review.py`

- [ ] **Step 1: Write the failing test**

`tests/test_watch_validity_review.py`에 추가 (기존 `_seed_active_alert`, `_rec`, `_StubHermes`, `_stub_md` 재사용):

```python
@pytest.mark.asyncio
async def test_notify_payload_carries_links_and_price_guidance(
    session: AsyncSession, _stub_md
) -> None:
    """ROB-500 — validity review 통지에도 링크/가이드 포함."""
    alert = await _seed_active_alert(session, recommendation=_rec())
    hermes = _StubHermes()

    @asynccontextmanager
    async def fake_factory():
        yield session

    svc = WatchValidityReviewService(hermes_client=hermes, session_factory=fake_factory)
    await svc.review_market("kr", dry_run=False)

    assert len(hermes.calls) == 1
    payload = hermes.calls[0]

    assert payload.invest_links is not None
    assert payload.invest_links.report_path == (
        f"/invest/reports/{alert.source_report_uuid}"
    )
    assert payload.invest_links.stock_path == "/invest/stocks/kr/005930"
    # 이벤트 row가 없는 경로 — event anchor는 없고 alert anchor만.
    assert payload.invest_links.event_anchor is None
    assert payload.invest_links.alert_anchor == (
        f"/invest/reports/{alert.source_report_uuid}"
        f"#watch-alert-{alert.alert_uuid}"
    )

    assert payload.operator_action_guidance is not None
    assert payload.operator_action_guidance.requires_operator_review is True

    assert payload.price_guidance is not None
    assert payload.price_guidance.entry_review_below_price == Decimal("100")
```

`Decimal` import가 파일에 없으면 추가.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_watch_validity_review.py -v -k "links_and_price"`
Expected: `payload.invest_links is None` assert FAIL.

- [ ] **Step 3: Wire the validity review service**

`app/services/investment_reports/watch_validity_review.py` import 수정:

```python
from app.services.hermes_client import (
    HermesNotificationClient,
    ReviewTriggerPayload,
    build_invest_links,
    build_operator_action_guidance,
    price_guidance_from_watch_recommendation,
)
```

`review_market` 안의 호출부를 변경 (기존 161행 부근):

```python
                if result.verdict in _ACTIONABLE and material:
                    if await self._notify(
                        alert,
                        result,
                        current_price,
                        kst_date,
                        stored_recommendation=stored,
                    ):
                        stats.notified += 1
```

`_notify` 시그니처/본문 변경:

```python
    async def _notify(
        self,
        alert: Any,
        result: Any,
        current_price: Decimal | None,
        kst_date: str,
        *,
        stored_recommendation: dict[str, Any] | None = None,
    ) -> bool:
        payload = ReviewTriggerPayload(
            event_uuid=uuid4(),
            alert_uuid=alert.alert_uuid,
            source_report_uuid=alert.source_report_uuid,
            source_item_uuid=alert.source_item_uuid,
            correlation_id=uuid4().hex,
            kst_date=kst_date,
            market=alert.market,
            target_kind=alert.target_kind,
            symbol=alert.symbol,
            metric=alert.metric,
            operator=alert.operator,
            threshold=Decimal(str(alert.threshold)),
            threshold_key=alert.threshold_key,
            intent=alert.intent,
            action_mode=alert.action_mode,
            current_value=current_price,
            scanner_snapshot={
                "validity_verdict": result.verdict,
                "reason": result.reason,
                "signals": result.signals,
            },
            outcome="review_required",
            # ROB-500 — no event row on this path, so no event anchor;
            # the alert row anchor is the operator's landing point.
            invest_links=build_invest_links(
                market=alert.market,
                symbol=alert.symbol,
                source_report_uuid=alert.source_report_uuid,
                alert_uuid=alert.alert_uuid,
            ),
            operator_action_guidance=build_operator_action_guidance(
                action_mode=alert.action_mode, outcome="review_required"
            ),
            price_guidance=price_guidance_from_watch_recommendation(
                stored_recommendation
            ),
        )
        try:
            res = await self._hermes.send_review_trigger(payload)
        except Exception:
            logger.exception("validity hermes send failed: %s", alert.symbol)
            return False
        return res.status == "success"
```

- [ ] **Step 4: Run the full validity test file**

Run: `uv run pytest tests/test_watch_validity_review.py -v`
Expected: 전부 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_reports/watch_validity_review.py tests/test_watch_validity_review.py
git commit -m "feat(ROB-500): validity-review notification carries invest links + guidance

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 4: Invest UI — row anchor + hash 스크롤 + active/triggered 섹션 분리

**Files:**
- Modify: `frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx`
- Test: `frontend/invest/src/__tests__/InvestmentReportBundleContent.watchAnchors.test.tsx` (신규)

- [ ] **Step 1: Write the failing test**

`frontend/invest/src/__tests__/InvestmentReportBundleContent.watchAnchors.test.tsx` 생성:

```tsx
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { InvestmentReportBundleContent } from "../components/investment-reports/InvestmentReportBundleContent";
import type {
  InvestmentReportBundle,
  InvestmentWatchAlert,
  InvestmentWatchEvent,
} from "../types/investmentReports";

vi.mock("../hooks/useInvestmentReportBundle", () => ({
  useInvestmentReportBundle: vi.fn(),
}));
import { useInvestmentReportBundle } from "../hooks/useInvestmentReportBundle";

const REPORT = {
  reportUuid: "00000000-0000-0000-0000-000000000001",
  reportType: "kr_morning", market: "kr", marketSession: "regular",
  accountScope: "kis_live", executionMode: "advisory_only", createdByProfile: "t",
  title: "KR", summary: "s", riskSummary: null, thesisText: null, noActionNote: null,
  marketSnapshot: {}, portfolioSnapshot: {}, previousReportUuid: null, status: "draft",
  metadata: {}, createdAt: "2026-05-27T00:00:00Z", updatedAt: "2026-05-27T00:00:00Z",
  publishedAt: null, validUntil: null, snapshotBundleUuid: null,
  snapshotPolicyVersion: null, snapshotCoverageSummary: null,
  snapshotFreshnessSummary: null, sourceConflicts: null, unavailableSources: null,
  snapshotReportDiagnostics: null,
} as InvestmentReportBundle["report"];

const ACTIVE_ALERT = {
  alertUuid: "alert-active-1",
  sourceReportUuid: "00000000-0000-0000-0000-000000000001",
  sourceItemUuid: "item-1",
  market: "crypto", targetKind: "asset", symbol: "KRW-BTC",
  metric: "price", operator: "below", threshold: "100000000",
  thresholdKey: "below:100000000", intent: "buy_review", actionMode: "notify_only",
  rationale: "r", triggerChecklist: [], maxAction: {},
  validUntil: "2026-12-31T00:00:00Z", status: "active", metadata: {},
  createdAt: "2026-06-10T00:00:00Z", activatedAt: "2026-06-10T00:00:00Z",
  updatedAt: "2026-06-10T00:00:00Z",
} as InvestmentWatchAlert;

const TRIGGERED_ALERT = {
  ...ACTIVE_ALERT,
  alertUuid: "alert-triggered-1",
  status: "triggered",
} as InvestmentWatchAlert;

const EVENT = {
  eventUuid: "event-1",
  sourceReportUuid: "00000000-0000-0000-0000-000000000001",
  sourceItemUuid: "item-1",
  market: "crypto", targetKind: "asset", symbol: "KRW-BTC",
  metric: "price", operator: "below", threshold: "100000000",
  thresholdKey: "below:100000000", intent: "buy_review", actionMode: "notify_only",
  currentValue: "99000000", scannerSnapshot: {}, outcome: "notified",
  correlationId: "c1", kstDate: "2026-06-10",
  deliveryStatus: "delivered", deliveryAttempts: 1,
  createdAt: "2026-06-10T00:00:00Z",
} as InvestmentWatchEvent;

function makeBundle(): InvestmentReportBundle {
  return {
    report: REPORT, items: [], decisionsByItemUuid: {},
    alerts: [ACTIVE_ALERT, TRIGGERED_ALERT], events: [EVENT],
    reviewSections: null, actionPacket: null,
  };
}

function renderWith(
  bundle: InvestmentReportBundle,
  initialEntry = "/reports/00000000-0000-0000-0000-000000000001",
) {
  (useInvestmentReportBundle as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
    status: "ready", bundle, error: null, reload: vi.fn(),
  });
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <InvestmentReportBundleContent />
    </MemoryRouter>,
  );
}

beforeAll(() => {
  // jsdom does not implement scrollIntoView.
  Element.prototype.scrollIntoView = vi.fn();
});

describe("InvestmentReportBundleContent — watch anchors & sections (ROB-500)", () => {
  it("renders stable id anchors on alert and event rows", () => {
    const { container } = renderWith(makeBundle());
    expect(container.querySelector("#watch-alert-alert-active-1")).not.toBeNull();
    expect(container.querySelector("#watch-alert-alert-triggered-1")).not.toBeNull();
    expect(container.querySelector("#watch-event-event-1")).not.toBeNull();
  });

  it("splits active and triggered watches into separate sections", () => {
    renderWith(makeBundle());
    expect(
      screen.getByRole("heading", { name: /active watches \(1\)/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /triggered \/ closed watches \(1\)/ }),
    ).toBeInTheDocument();
  });

  it("scrolls the anchored row into view when arriving with a hash", () => {
    const spy = vi.spyOn(Element.prototype, "scrollIntoView");
    renderWith(
      makeBundle(),
      "/reports/00000000-0000-0000-0000-000000000001#watch-event-event-1",
    );
    expect(spy).toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend/invest && npm test -- InvestmentReportBundleContent.watchAnchors`
Expected: anchor querySelector null + heading 미존재로 FAIL.

- [ ] **Step 3: Implement the component changes**

`InvestmentReportBundleContent.tsx`:

(a) import 수정 — 파일 상단:

```tsx
import { useEffect, type ReactNode } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
```

(b) `InvestmentReportBundleContent` 본문, `useInvestmentReportBundle` 호출 직후(early return **앞**)에 추가:

```tsx
  const location = useLocation();

  // ROB-500 — Discord 딥링크(`#watch-event-…` / `#watch-alert-…`)로 진입하면
  // bundle 렌더 후 해당 row로 스크롤한다.
  useEffect(() => {
    if (!bundle || !location.hash) return;
    const el = document.getElementById(location.hash.slice(1));
    el?.scrollIntoView({ block: "center" });
  }, [bundle, location.hash]);
```

(c) `AlertRow` 루트 `<section`에 id 추가:

```tsx
function AlertRow({ alert }: { alert: InvestmentWatchAlert }) {
  return (
    <section
      id={`watch-alert-${alert.alertUuid}`}
      style={{
```

(d) `EventRow` 루트 `<section`에 id 추가:

```tsx
function EventRow({ event }: { event: InvestmentWatchEvent }) {
  const deliveryLabel = DELIVERY_STATUS_LABELS[event.deliveryStatus];
  const deliveryColor = DELIVERY_STATUS_COLORS[event.deliveryStatus];
  return (
    <section
      id={`watch-event-${event.eventUuid}`}
      style={{
```

(e) `ItemRow` 루트 `<section`(218행 부근)에 id 추가 (source item anchor):

```tsx
  return (
    <section
      id={`watch-item-${item.itemUuid}`}
      style={{
```

(f) 본문에서 `const buckets = groupItems(bundle.items);` 다음 줄에 추가:

```tsx
  // ROB-500 — `active watches`에 triggered가 섞여 operator가 혼동하던 문제:
  // 상태별로 섹션을 분리한다.
  const activeAlerts = bundle.alerts.filter((alert) => alert.status === "active");
  const settledAlerts = bundle.alerts.filter((alert) => alert.status !== "active");
```

(g) 기존 alerts 섹션(678–687행)을 다음으로 교체:

```tsx
      {activeAlerts.length > 0 ? (
        <section style={{ display: "grid", gap: 10 }}>
          <h2 style={{ margin: 0, fontSize: 18 }}>
            active watches ({activeAlerts.length})
          </h2>
          {activeAlerts.map((alert) => (
            <AlertRow key={alert.alertUuid} alert={alert} />
          ))}
        </section>
      ) : null}

      {settledAlerts.length > 0 ? (
        <section style={{ display: "grid", gap: 10 }}>
          <h2 style={{ margin: 0, fontSize: 18 }}>
            triggered / closed watches ({settledAlerts.length})
          </h2>
          {settledAlerts.map((alert) => (
            <AlertRow key={alert.alertUuid} alert={alert} />
          ))}
        </section>
      ) : null}
```

- [ ] **Step 4: Run frontend tests + typecheck**

Run: `cd frontend/invest && npm test && npm run typecheck`
Expected: 신규 테스트 포함 전부 PASS, tsc 에러 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx frontend/invest/src/__tests__/InvestmentReportBundleContent.watchAnchors.test.tsx
git commit -m "feat(ROB-500): invest report detail — watch row anchors, hash scroll, active/triggered split

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 5: 계약 런북 + 전체 게이트

**Files:**
- Create: `docs/runbooks/watch-trigger-hermes-payload.md`

- [ ] **Step 1: Write the contract runbook**

`docs/runbooks/watch-trigger-hermes-payload.md` 생성:

```markdown
# Watch Trigger → Hermes Payload Contract (ROB-265 Plan 4 + ROB-500)

auto_trader가 watch 발화/유효성 재검토 시 `HERMES_WEBHOOK_URL`로 POST하는
`ReviewTriggerPayload`(`app/services/hermes_client.py`) 계약 문서.
Hermes Discord 렌더러가 이 문서를 기준으로 카드를 구성한다.

## ROB-500 추가 필드 (additive, 모두 optional)

```json
{
  "invest_links": {
    "report_path": "/invest/reports/70019e8d-1ee6-493f-adeb-5d9301d5ea48",
    "stock_path": "/invest/stocks/crypto/KRW-BTC",
    "event_anchor": "/invest/reports/70019e8d-…#watch-event-f912d55f-…",
    "alert_anchor": "/invest/reports/70019e8d-…#watch-alert-5e32ec11-…"
  },
  "operator_action_guidance": {
    "headline": "알림 전용 — 자동 주문 없음, 필요 시 수동 검토",
    "requires_operator_review": false,
    "order_behavior": "none"
  },
  "price_guidance": {
    "entry_review_below_price": "100",
    "suggested_limit_price_range": {"low": "95", "high": "100"},
    "max_chase_price": "102",
    "invalidation": {"kind": "price_below", "price": "80", "text": null}
  }
}
```

- 링크는 **path-only** — Hermes가 Invest base URL을 prepend.
- `event_anchor`는 스캐너 발화 경로에만 존재 (validity review는 `alert_anchor`만).
- `price_guidance: null` 이면 **"가격 가이드 없음"으로 표시**한다. Hermes가
  가격을 추론/생성하는 것은 금지.
- 익절/매도 목표 필드는 계약에 없다. 렌더러가 임의 생성하지 않는다 (locked scope).

## Hermes Discord 렌더러 요구사항 (ROB-500 §4)

1. 카드 상단: `operator_action_guidance.headline` + 발화 조건
   (`symbol metric operator threshold`, `current_value`).
2. 그 다음: `invest_links` (event_anchor 우선, 없으면 alert_anchor → report_path,
   stock_path는 보조 context link).
3. 그 다음: `price_guidance` 4개 값 (또는 "가격 가이드 없음").
4. 하단 `Trace` 섹션: event/alert/report/item UUID + correlation_id.

## 배포 순서

새 필드는 additive-optional이다. Hermes 수신측이 unknown field를 strict 거부하는
경우 auto_trader 배포 → Hermes 업데이트 사이에 delivery가 `failed`로 기록되지만,
alert는 `active`로 남아 다음 스캔 루프가 재시도하므로 유실은 없다 (Plan 4
semantics). 그래도 권장 순서는 **Hermes(수신 tolerant + 렌더러) 먼저 → auto_trader**.
```

- [ ] **Step 2: Run full quality gates**

```bash
uv run pytest tests/test_hermes_client.py tests/test_investment_watch_scanner.py tests/test_watch_validity_review.py -v
make lint
cd frontend/invest && npm test && npm run typecheck
```

Expected: 전부 PASS / clean. (lint는 `app/` + `tests/` 둘 다 검사됨 — 신규 테스트 파일 포함.)

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/watch-trigger-hermes-payload.md
git commit -m "docs(ROB-500): watch trigger Hermes payload contract runbook

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Out of Scope (Scope Guardrail 준수)

- 익절/매도 목표 정책, 자동 주문, live order approval boundary 변경 — 없음.
- Hermes Discord 렌더러 실제 구현 — 외부 시스템, 런북 + Linear 코멘트로 인계.
- 종목 상세 페이지에 watch/event 표시 추가 — ROB-500 본문에서 비스코프로 명시됨.
- DB migration — 없음.

## Acceptance Criteria 매핑

| AC | Task |
|----|------|
| Discord 메시지에 `/invest/reports/{uuid}` 포함 | Task 1+2+3 (`invest_links.report_path`) + Hermes 렌더러(런북 인계) |
| event row 직행 anchor URL | Task 1+2 (`event_anchor`) + Task 4 (row id + hash scroll) |
| `/invest/stocks/{market}/{symbol}` 보조 링크 | Task 1 (`stock_path`) |
| 상단에 운영 액션 의미 | Task 1 (`operator_action_guidance`) + 런북 §렌더러 |
| watch_recommendation 4개 값 표시 | Task 1+2+3 (`price_guidance`) |
| 없으면 추론 금지, "없음" 표시 | Task 1 (`None` semantics) + 런북 |
| 익절/매도 목표 임의 생성 금지 | subset 추출 설계 + 런북 명시 |
| 단위 테스트 + E2E smoke | Tasks 1–4 단위/컴포넌트 테스트; live E2E smoke는 operator 인계 단계 |
