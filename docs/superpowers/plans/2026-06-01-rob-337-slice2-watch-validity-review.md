# ROB-337 Slice 2 — watch 유효성 review job Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** active watch를 default-disabled/dry-run/read-only로 평가해 keep/reprice/expire/review_now/data_gap으로 분류하고, material-change actionable verdict만 기존 Hermes review-trigger로 throttled 알림하는 review job을 추가한다.

**Architecture:** 순수 분류기(`watch_validity_policy.py`)가 저장된 watch_recommendation + 현재가 + 재계산으로 verdict를 결정한다. `WatchValidityReviewService`(InvestmentWatchScanner 미러, broker 없음)가 active alert를 읽어 분류하고, dry_run=False일 때만 `alert_metadata.last_review`로 throttle하며 actionable·material verdict를 Hermes로 보낸다. alert.status/watch_condition/watch_recommendation은 불변. scheduleless TaskIQ task + env-gated dry-run CLI. 마이그레이션 0.

**Tech Stack:** Python 3.13, SQLAlchemy async, TaskIQ, FastMCP/Hermes client, pytest, Decimal.

**Spec:** `docs/superpowers/specs/2026-06-01-rob-337-slice2-watch-validity-review-design.md`

---

## File Structure

- `app/services/investment_reports/watch_validity_policy.py` (신규) — `WatchValidityInput`/`WatchValidityResult` + `classify_watch_validity()` 순수 함수.
- `app/services/investment_reports/repository.py` — `update_alert_metadata` DAO.
- `app/core/config.py` — `WATCH_VALIDITY_REVIEW_ENABLED: bool = False`.
- `app/services/investment_reports/watch_validity_review.py` (신규) — `WatchValidityReviewService`.
- `app/tasks/watch_validity_review_tasks.py` (신규) — scheduleless env-gated task.
- `scripts/review_active_watches.py` (신규) — dry-run-default 운영 CLI.
- `tests/test_watch_validity_policy.py`, `tests/test_watch_validity_review.py`, `tests/test_review_active_watches_cli.py` (신규).

---

## Task 1: 순수 분류기 (TDD)

**Files:**
- Create: `app/services/investment_reports/watch_validity_policy.py`
- Test: `tests/test_watch_validity_policy.py` (신규)

- [ ] **Step 1: 실패 테스트 작성**

새 파일 `tests/test_watch_validity_policy.py`:

```python
"""ROB-337 Slice 2 — watch validity classifier."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.schemas.investment_reports import WatchRecommendationPayload, WatchRecommendationEvidence
from app.services.investment_reports.watch_validity_policy import (
    REPRICE_DRIFT_PCT,
    WatchValidityInput,
    classify_watch_validity,
)

_NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _stored(entry="100", inval="80") -> dict:
    return {
        "entry_review_below_price": entry,
        "invalidation": {"kind": "price_below", "price": inval},
    }


def _recomputed_ok(entry: str) -> WatchRecommendationPayload:
    return WatchRecommendationPayload(
        watch_reason="r",
        data_state="ok",
        reference_price=Decimal("100"),
        entry_review_below_price=Decimal(entry),
        suggested_limit_price_range={"low": Decimal(entry), "high": Decimal(entry)},
        max_chase_price=Decimal(entry),
        invalidation={"kind": "price_below", "price": Decimal("70")},
        source_evidence=WatchRecommendationEvidence(lookback_days=20),
        policy_version="v1",
        computed_at=_NOW,
    )


def _inp(**kw) -> WatchValidityInput:
    base = dict(
        stored_recommendation=_stored(),
        current_price=Decimal("90"),
        recomputed=None,
        valid_until=_NOW + timedelta(days=30),
        now=_NOW,
    )
    base.update(kw)
    return WatchValidityInput(**base)


def test_data_gap_when_no_current_price() -> None:
    assert classify_watch_validity(_inp(current_price=None)).verdict == "data_gap"


def test_expire_when_below_invalidation() -> None:
    r = classify_watch_validity(_inp(current_price=Decimal("79")))
    assert r.verdict == "expire"
    assert "invalidation" in r.reason


def test_expire_when_near_expiry() -> None:
    r = classify_watch_validity(
        _inp(current_price=Decimal("95"), valid_until=_NOW + timedelta(days=1))
    )
    assert r.verdict == "expire"


def test_review_now_when_in_zone() -> None:
    r = classify_watch_validity(_inp(current_price=Decimal("100")))
    assert r.verdict == "review_now"


def test_keep_when_above_zone_and_intact() -> None:
    r = classify_watch_validity(_inp(current_price=Decimal("120")))
    assert r.verdict == "keep"


def test_priority_invalidation_beats_zone() -> None:
    # price below invalidation AND below entry zone -> expire wins
    r = classify_watch_validity(_inp(current_price=Decimal("75")))
    assert r.verdict == "expire"


def test_reprice_on_material_drift() -> None:
    # entry stored 100, recomputed 120 -> drift 20% > 5%; price above zone so not review_now
    r = classify_watch_validity(
        _inp(current_price=Decimal("130"), recomputed=_recomputed_ok("120"))
    )
    assert r.verdict == "reprice"
    assert r.recomputed is not None


def test_no_stored_with_recompute_is_reprice() -> None:
    r = classify_watch_validity(
        _inp(stored_recommendation=None, current_price=Decimal("100"),
             recomputed=_recomputed_ok("90"))
    )
    assert r.verdict == "reprice"
```

- [ ] **Step 2: 실행 → 실패 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run pytest tests/test_watch_validity_policy.py -p no:randomly -q`
Expected: FAIL — `ModuleNotFoundError: ...watch_validity_policy`.

- [ ] **Step 3: 분류기 구현**

새 파일 `app/services/investment_reports/watch_validity_policy.py`:

```python
"""ROB-337 Slice 2 — deterministic watch validity classifier.

Pure function: given the stored watch_recommendation, current price, a
fresh recompute, and valid_until/now, classify whether an active watch is
still meaningful. No I/O. Advisory only — never mutates or orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from app.schemas.investment_reports import WatchRecommendationPayload

WatchValidityVerdict = Literal["keep", "reprice", "expire", "review_now", "data_gap"]

EXPIRE_SOON_DAYS = 2
REPRICE_DRIFT_PCT = Decimal("0.05")


@dataclass(frozen=True)
class WatchValidityInput:
    stored_recommendation: dict | None
    current_price: Decimal | None
    recomputed: WatchRecommendationPayload | None
    valid_until: datetime | None
    now: datetime


@dataclass(frozen=True)
class WatchValidityResult:
    verdict: WatchValidityVerdict
    reason: str
    recomputed: WatchRecommendationPayload | None
    signals: dict[str, Any]


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def classify_watch_validity(inp: WatchValidityInput) -> WatchValidityResult:
    stored = inp.stored_recommendation or {}
    price = inp.current_price
    entry = _dec(stored.get("entry_review_below_price"))
    inval = stored.get("invalidation") or {}
    inval_price = (
        _dec(inval.get("price")) if inval.get("kind") == "price_below" else None
    )
    days_to_expiry = None
    if inp.valid_until is not None:
        days_to_expiry = (inp.valid_until - inp.now).total_seconds() / 86400.0

    signals: dict[str, Any] = {
        "current_price": str(price) if price is not None else None,
        "entry_review_below_price": str(entry) if entry is not None else None,
        "invalidation_price": str(inval_price) if inval_price is not None else None,
        "days_to_expiry": days_to_expiry,
    }

    recomputed_gap = (
        inp.recomputed is not None and inp.recomputed.data_state == "data_gap"
    )
    if price is None or (not stored and recomputed_gap):
        return WatchValidityResult(
            verdict="data_gap",
            reason=(
                "no current price"
                if price is None
                else "no stored recommendation and recompute returned data_gap"
            ),
            recomputed=None,
            signals=signals,
        )

    if inval_price is not None and price < inval_price:
        return WatchValidityResult(
            verdict="expire",
            reason=f"price {price} fell below invalidation {inval_price} (thesis broken)",
            recomputed=None,
            signals=signals,
        )
    if days_to_expiry is not None and days_to_expiry <= EXPIRE_SOON_DAYS:
        return WatchValidityResult(
            verdict="expire",
            reason=f"expires in {days_to_expiry:.2f} days (<= {EXPIRE_SOON_DAYS})",
            recomputed=None,
            signals=signals,
        )

    if entry is not None and price <= entry:
        return WatchValidityResult(
            verdict="review_now",
            reason=f"price {price} entered review zone (<= {entry})",
            recomputed=None,
            signals=signals,
        )

    if inp.recomputed is not None and inp.recomputed.data_state == "ok":
        new_entry = inp.recomputed.entry_review_below_price
        if entry is None:
            return WatchValidityResult(
                verdict="reprice",
                reason="no stored thresholds; recommendation available to populate",
                recomputed=inp.recomputed,
                signals=signals,
            )
        if new_entry is not None and entry != 0:
            drift = abs(new_entry - entry) / entry
            signals["drift_pct"] = str(drift)
            if drift > REPRICE_DRIFT_PCT:
                return WatchValidityResult(
                    verdict="reprice",
                    reason=f"entry drift {drift} > {REPRICE_DRIFT_PCT}",
                    recomputed=inp.recomputed,
                    signals=signals,
                )

    return WatchValidityResult(
        verdict="keep",
        reason="still valid; price above review zone, thesis intact",
        recomputed=None,
        signals=signals,
    )
```

- [ ] **Step 4: 실행 → 통과 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run pytest tests/test_watch_validity_policy.py -p no:randomly -q`
Expected: PASS (8 tests).

- [ ] **Step 5: 커밋**

```bash
cd /Users/mgh3326/work/auto_trader.rob-337
git add app/services/investment_reports/watch_validity_policy.py tests/test_watch_validity_policy.py
git commit -m "feat(ROB-337): deterministic watch validity classifier (Slice 2)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 2: 리포지토리 `update_alert_metadata`

**Files:**
- Modify: `app/services/investment_reports/repository.py` (`insert_alert` 다음, 현 라인 234 부근)

- [ ] **Step 1: DAO 추가**

`insert_alert` 메서드(현 라인 229-234) 다음에 추가:

```python
    async def update_alert_metadata(self, alert_id: int, metadata: dict) -> None:
        """ROB-337 — replace an alert's alert_metadata JSONB (caller merges).

        Used by the validity review job to persist a ``last_review`` block.
        Does NOT touch status / threshold / valid_until. Flushes via the
        caller's transaction; never commits."""
        await self._session.execute(
            sa.update(InvestmentWatchAlert)
            .where(InvestmentWatchAlert.id == alert_id)
            .values(alert_metadata=metadata)
        )
```

(`InvestmentWatchAlert`, `sa`는 이미 import 되어 있음.)

- [ ] **Step 2: 구문 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run python -c "from app.services.investment_reports.repository import InvestmentReportsRepository as R; print(hasattr(R,'update_alert_metadata'))"`
Expected: `True`

- [ ] **Step 3: 커밋**

```bash
cd /Users/mgh3326/work/auto_trader.rob-337
git add app/services/investment_reports/repository.py
git commit -m "feat(ROB-337): repo.update_alert_metadata for review last_review block

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 3: 설정 플래그

**Files:**
- Modify: `app/core/config.py` (현 라인 389 `HERMES_ENABLED` 부근)

- [ ] **Step 1: 플래그 추가**

`app/core/config.py`의 `HERMES_ENABLED: bool = False`(현 라인 389) 다음 줄에 추가:

```python
    # ROB-337 Slice 2 — watch validity review job. Default off; the task and
    # CLI are scheduleless / dry-run-default even when this is set.
    WATCH_VALIDITY_REVIEW_ENABLED: bool = False
```

- [ ] **Step 2: 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run python -c "from app.core.config import settings; print(settings.WATCH_VALIDITY_REVIEW_ENABLED)"`
Expected: `False`

- [ ] **Step 3: 커밋**

```bash
cd /Users/mgh3326/work/auto_trader.rob-337
git add app/core/config.py
git commit -m "feat(ROB-337): WATCH_VALIDITY_REVIEW_ENABLED flag (default off)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 4: review 서비스 (TDD)

**Files:**
- Create: `app/services/investment_reports/watch_validity_review.py`
- Test: `tests/test_watch_validity_review.py` (신규)

- [ ] **Step 1: 실패 테스트 작성**

새 파일 `tests/test_watch_validity_review.py`:

```python
"""ROB-337 Slice 2 — watch validity review service."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.investment_reports import (
    ActivateWatchRequest,
    IngestReportRequest,
    RecordDecisionRequest,
)
from app.services.hermes_client import HermesDeliveryResult, ReviewTriggerPayload
from app.services.investment_reports import watch_validity_review as review_module
from app.services.investment_reports.decisions import InvestmentReportDecisionService
from app.services.investment_reports.ingestion import InvestmentReportIngestionService
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_reports.watch_activation import WatchActivationService
from app.services.investment_reports.watch_validity_review import (
    WatchValidityReviewService,
)
from tests._investment_reports_helpers import future_datetime


@dataclass
class _StubHermes:
    calls: list[ReviewTriggerPayload] = field(default_factory=list)
    delivery: HermesDeliveryResult = field(
        default_factory=lambda: HermesDeliveryResult(status="success", http_status=200)
    )

    async def send_review_trigger(self, payload: ReviewTriggerPayload) -> HermesDeliveryResult:
        self.calls.append(payload)
        return self.delivery

    async def close(self) -> None:
        pass


async def _seed_active_alert(session: AsyncSession, *, recommendation: dict) -> Any:
    """Ingest -> approve -> activate one KR watch, then set its
    watch_recommendation. Returns the activated alert."""
    ingest = InvestmentReportIngestionService(session)
    report = await ingest.ingest(
        IngestReportRequest(
            report_type="kr_morning",
            market="kr",
            market_session="regular",
            account_scope="kis_mock",
            execution_mode="mock_preview",
            created_by_profile="test",
            title="t",
            summary="s",
            kst_date="2026-05-18",
            items=[
                {
                    "client_item_key": "watch-1",
                    "item_kind": "watch",
                    "symbol": "005930",
                    "intent": "trend_recovery_review",
                    "rationale": "r",
                    "watch_condition": {
                        "metric": "price",
                        "operator": "below",
                        "threshold": 100,
                    },
                    "valid_until": future_datetime().isoformat(),
                }
            ],
        )
    )
    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    item = items[0]
    await InvestmentReportDecisionService(session).record(
        RecordDecisionRequest(item_uuid=item.item_uuid, decision="approve", actor="op")
    )
    alert = await WatchActivationService(session).activate(
        ActivateWatchRequest(item_uuid=item.item_uuid, actor="op")
    )
    await repo.update_item_watch_recommendation(item.id, recommendation)
    await session.commit()
    return alert


def _rec(entry: str = "100", inval: str = "80") -> dict:
    return {
        "watch_reason": "r",
        "data_state": "ok",
        "reference_price": "110",
        "entry_review_below_price": entry,
        "suggested_limit_price_range": {"low": entry, "high": entry},
        "max_chase_price": entry,
        "invalidation": {"kind": "price_below", "price": inval},
        "review_cadence": "daily",
        "source_evidence": {"lookback_days": 20},
        "policy_version": "v1",
        "computed_at": "2026-06-01T00:00:00+00:00",
    }


@pytest.fixture
def _stub_md(monkeypatch):
    async def fake_current_value(**_kwargs):
        return 90.0  # <= entry 100 -> review_now, > invalidation 80

    async def fake_ohlcv(*_a, **_k):
        return []  # recompute -> data_gap; stored present so classification uses stored

    monkeypatch.setattr(review_module, "get_current_value", fake_current_value)
    monkeypatch.setattr(review_module.market_data_service, "get_ohlcv", fake_ohlcv)


@pytest.mark.asyncio
async def test_dry_run_no_writes_no_notify(session: AsyncSession, _stub_md) -> None:
    alert = await _seed_active_alert(session, recommendation=_rec())
    hermes = _StubHermes()
    svc = WatchValidityReviewService(hermes_client=hermes)
    summary = await svc.review_market("kr", dry_run=True)
    assert summary["verdict_counts"].get("review_now") == 1
    assert hermes.calls == []
    # alert_metadata unchanged (no last_review)
    repo = InvestmentReportsRepository(session)
    reloaded = await repo.get_alert_by_idempotency_key(alert.idempotency_key)
    assert "last_review" not in (reloaded.alert_metadata or {})


@pytest.mark.asyncio
async def test_run_notifies_actionable_and_records_last_review(
    session: AsyncSession, _stub_md
) -> None:
    alert = await _seed_active_alert(session, recommendation=_rec())
    hermes = _StubHermes()
    svc = WatchValidityReviewService(hermes_client=hermes)
    summary = await svc.review_market("kr", dry_run=False)
    assert summary["notified"] == 1
    assert len(hermes.calls) == 1
    assert hermes.calls[0].scanner_snapshot["validity_verdict"] == "review_now"
    assert hermes.calls[0].outcome == "review_required"
    repo = InvestmentReportsRepository(session)
    reloaded = await repo.get_alert_by_idempotency_key(alert.idempotency_key)
    assert reloaded.alert_metadata["last_review"]["verdict"] == "review_now"
    assert reloaded.status == "active"  # no-mutation: status untouched


@pytest.mark.asyncio
async def test_throttle_suppresses_same_verdict_same_day(
    session: AsyncSession, _stub_md
) -> None:
    await _seed_active_alert(session, recommendation=_rec())
    hermes = _StubHermes()
    svc = WatchValidityReviewService(hermes_client=hermes)
    await svc.review_market("kr", dry_run=False)
    await svc.review_market("kr", dry_run=False)
    assert len(hermes.calls) == 1  # second run is material-unchanged -> no re-notify


@pytest.mark.asyncio
async def test_keep_is_not_notified(session: AsyncSession, monkeypatch) -> None:
    async def fake_cv(**_k):
        return 200.0  # well above entry -> keep

    async def fake_ohlcv(*_a, **_k):
        return []

    monkeypatch.setattr(review_module, "get_current_value", fake_cv)
    monkeypatch.setattr(review_module.market_data_service, "get_ohlcv", fake_ohlcv)
    await _seed_active_alert(session, recommendation=_rec())
    hermes = _StubHermes()
    svc = WatchValidityReviewService(hermes_client=hermes)
    summary = await svc.review_market("kr", dry_run=False)
    assert summary["verdict_counts"].get("keep") == 1
    assert hermes.calls == []
```

- [ ] **Step 2: 실행 → 실패 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run pytest tests/test_watch_validity_review.py -p no:randomly -q`
Expected: FAIL — `ModuleNotFoundError: ...watch_validity_review`.

- [ ] **Step 3: 서비스 구현**

새 파일 `app/services/investment_reports/watch_validity_review.py`:

```python
"""ROB-337 Slice 2 — watch validity review job (read-only, dry-run default).

Mirrors :class:`app.jobs.investment_watch_scanner.InvestmentWatchScanner`
read patterns but classifies each active watch's continued validity
(keep/reprice/expire/review_now/data_gap) instead of firing triggers.

Locked semantics:
* NO broker / order / order-intent mutation.
* alert.status / watch_condition / watch_recommendation are NOT mutated;
  the only write (dry_run=False) is the ``last_review`` block in
  alert_metadata, used for material-change notification throttling.
* Notification reuses the Hermes review-trigger contract; verdict + reason
  ride in ``scanner_snapshot`` with ``outcome='review_required'``. No
  investment_watch_events rows are created.
* HERMES_ENABLED default False -> deliveries are skipped.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.jobs.watch_market_data import get_current_value
from app.services import market_data as market_data_service
from app.services.hermes_client import (
    HermesNotificationClient,
    ReviewTriggerPayload,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_reports.watch_recommendation_policy import (
    ATR_PERIOD,
    LOOKBACK_DAYS,
    WatchPolicyInput,
    compute_watch_recommendation,
)
from app.services.investment_reports.watch_validity_policy import (
    WatchValidityInput,
    classify_watch_validity,
)

logger = logging.getLogger(__name__)

_ACTIONABLE = {"review_now", "expire", "data_gap"}
_MD_MARKET = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def _normalize_symbol(symbol: str, market: str) -> str:
    s = str(symbol or "").strip()
    if market == "crypto":
        up = s.upper()
        return up if "-" in up else f"KRW-{up}"
    if market == "us":
        return s.upper()
    return s


@dataclass
class _ReviewStats:
    market: str
    alerts_seen: int = 0
    notified: int = 0
    failed_lookups: int = 0
    verdict_counts: dict[str, int] = field(default_factory=dict)
    details: list[dict[str, Any]] = field(default_factory=list)

    def record(self, verdict: str) -> None:
        self.verdict_counts[verdict] = self.verdict_counts.get(verdict, 0) + 1

    def summary(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "alerts_seen": self.alerts_seen,
            "notified": self.notified,
            "failed_lookups": self.failed_lookups,
            "verdict_counts": self.verdict_counts,
            "details": self.details,
        }


class WatchValidityReviewService:
    def __init__(
        self,
        hermes_client: HermesNotificationClient | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._hermes = hermes_client or HermesNotificationClient()
        self._session_factory = session_factory or AsyncSessionLocal

    async def run(self, *, dry_run: bool = True) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for market in ("crypto", "kr", "us"):
            try:
                results[market] = await self.review_market(market, dry_run=dry_run)
            except Exception as exc:
                logger.exception("watch validity review_market raised: %s", market)
                results[market] = {"market": market, "status": "failed", "error": str(exc)}
        return results

    async def review_market(self, market: str, *, dry_run: bool = True) -> dict[str, Any]:
        stats = _ReviewStats(market=market)
        now_utc = datetime.now(UTC)
        async with self._session_factory() as db:
            repo = InvestmentReportsRepository(db)
            alerts = await repo.list_active_alerts(market=market, valid_at=now_utc)
            for alert in alerts:
                stats.alerts_seen += 1
                item = await repo.get_item_by_uuid(alert.source_item_uuid)
                stored = item.watch_recommendation if item is not None else None

                current_price = await self._current_price(alert)
                if current_price is None:
                    stats.failed_lookups += 1
                recomputed = await self._recompute(alert, now_utc)

                result = classify_watch_validity(
                    WatchValidityInput(
                        stored_recommendation=stored,
                        current_price=current_price,
                        recomputed=recomputed,
                        valid_until=alert.valid_until,
                        now=now_utc,
                    )
                )
                stats.record(result.verdict)
                stats.details.append(
                    {
                        "alert_uuid": str(alert.alert_uuid),
                        "symbol": alert.symbol,
                        "verdict": result.verdict,
                        "reason": result.reason,
                    }
                )

                if dry_run:
                    continue

                kst_date = now_kst().date().isoformat()
                last = (alert.alert_metadata or {}).get("last_review") or {}
                material = (
                    result.verdict != last.get("verdict")
                    or kst_date != last.get("kst_date")
                )
                if result.verdict in _ACTIONABLE and material:
                    if await self._notify(alert, result, current_price, kst_date):
                        stats.notified += 1

                new_meta = dict(alert.alert_metadata or {})
                new_meta["last_review"] = {
                    "verdict": result.verdict,
                    "kst_date": kst_date,
                    "computed_at": now_utc.isoformat(),
                }
                await repo.update_alert_metadata(alert.id, new_meta)
            if not dry_run:
                await db.commit()
        return stats.summary()

    async def _current_price(self, alert: Any) -> Decimal | None:
        try:
            val = await get_current_value(
                target_kind=alert.target_kind,
                metric="price",
                symbol=alert.symbol,
                market=alert.market,
            )
        except Exception:
            logger.exception("validity current-price lookup failed: %s", alert.symbol)
            return None
        return Decimal(str(val)) if val is not None else None

    async def _recompute(self, alert: Any, now_utc: datetime):
        md_market = _MD_MARKET.get(alert.market)
        if md_market is None:
            return None
        try:
            candles = await market_data_service.get_ohlcv(
                symbol=_normalize_symbol(alert.symbol, alert.market),
                market=md_market,
                period="day",
                count=LOOKBACK_DAYS + ATR_PERIOD + 6,
            )
        except Exception:
            logger.exception("validity recompute fetch failed: %s", alert.symbol)
            return None
        ordered = sorted(candles, key=lambda c: c.timestamp)
        closes = [Decimal(str(c.close)) for c in ordered]
        return compute_watch_recommendation(
            WatchPolicyInput(
                reference_price=closes[-1] if closes else None,
                best_bid=None,
                best_ask=None,
                daily_highs=[Decimal(str(c.high)) for c in ordered],
                daily_lows=[Decimal(str(c.low)) for c in ordered],
                daily_closes=closes,
            ),
            computed_at=now_utc,
            valid_until=alert.valid_until,
        )

    async def _notify(
        self, alert: Any, result: Any, current_price: Decimal | None, kst_date: str
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
        )
        try:
            res = await self._hermes.send_review_trigger(payload)
        except Exception:
            logger.exception("validity hermes send failed: %s", alert.symbol)
            return False
        return res.status == "success"

    async def close(self) -> None:
        await self._hermes.close()
```

- [ ] **Step 4: 실행 → 통과 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run pytest tests/test_watch_validity_review.py -p no:randomly -q`
Expected: PASS (4 tests).

- [ ] **Step 5: 커밋**

```bash
cd /Users/mgh3326/work/auto_trader.rob-337
git add app/services/investment_reports/watch_validity_review.py tests/test_watch_validity_review.py
git commit -m "feat(ROB-337): WatchValidityReviewService (read-only, throttled notify)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 5: scheduleless TaskIQ task

**Files:**
- Create: `app/tasks/watch_validity_review_tasks.py`

- [ ] **Step 1: task 작성**

새 파일 `app/tasks/watch_validity_review_tasks.py`:

```python
"""ROB-337 Slice 2 — scheduleless, env-gated watch validity review task.

No ``schedule=`` -> never auto-runs; manual entry point only. Gated by
``settings.WATCH_VALIDITY_REVIEW_ENABLED`` (default False) and defaults to
dry_run. Recurring activation is operator-gated (separate approval).
"""

from __future__ import annotations

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.services.investment_reports.watch_validity_review import (
    WatchValidityReviewService,
)


@broker.task(task_name="review.investment_watch_validity")
async def run_watch_validity_review_task(dry_run: bool = True) -> dict:
    if not settings.WATCH_VALIDITY_REVIEW_ENABLED:
        return {"status": "disabled"}
    service = WatchValidityReviewService()
    try:
        return await service.run(dry_run=dry_run)
    finally:
        await service.close()
```

- [ ] **Step 2: import 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run python -c "import app.tasks.watch_validity_review_tasks as t; print(t.run_watch_validity_review_task.task_name)"`
Expected: `review.investment_watch_validity` 출력 (에러 없음).

- [ ] **Step 3: 커밋**

```bash
cd /Users/mgh3326/work/auto_trader.rob-337
git add app/tasks/watch_validity_review_tasks.py
git commit -m "feat(ROB-337): scheduleless env-gated watch validity review task

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 6: 운영 CLI (TDD)

**Files:**
- Create: `scripts/review_active_watches.py`
- Test: `tests/test_review_active_watches_cli.py` (신규)

- [ ] **Step 1: 실패 테스트 작성**

새 파일 `tests/test_review_active_watches_cli.py`:

```python
"""ROB-337 Slice 2 — review_active_watches CLI gate/dry-run."""

from __future__ import annotations

from scripts.review_active_watches import main


def test_disabled_without_env(monkeypatch, capsys) -> None:
    monkeypatch.delenv("WATCH_VALIDITY_REVIEW_ENABLED", raising=False)
    assert main(["--dry-run"]) == 0
    assert "disabled" in capsys.readouterr().out.lower()


def test_dry_run_when_enabled(monkeypatch, capsys) -> None:
    monkeypatch.setenv("WATCH_VALIDITY_REVIEW_ENABLED", "true")
    assert main(["--dry-run"]) == 0
    assert "dry-run" in capsys.readouterr().out.lower()
```

- [ ] **Step 2: 실행 → 실패 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run pytest tests/test_review_active_watches_cli.py -p no:randomly -q`
Expected: FAIL — `ModuleNotFoundError: scripts.review_active_watches`.

- [ ] **Step 3: CLI 구현**

새 파일 `scripts/review_active_watches.py`:

```python
"""ROB-337 Slice 2 — operator CLI for the watch validity review job.

Default-disabled. --dry-run (default) prints the plan without DB/HTTP/secrets
and lazy-imports Settings-backed modules only in the --run path.

Modes:
  --dry-run : print plan; no DB, no HTTP, no secrets required.
  --run     : execute review (writes alert_metadata.last_review + throttled
              Hermes notifications). Read-only w.r.t. broker/orders.

Exit codes:
  0 — disabled, dry-run, or successful run
  1 — unexpected exception during --run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

logger = logging.getLogger("review_active_watches")

_ENABLE_ENV = "WATCH_VALIDITY_REVIEW_ENABLED"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="watch validity review (read-only)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="print plan; no writes")
    group.add_argument("--run", action="store_true", help="execute review")
    return parser.parse_args(argv)


async def _run() -> int:
    # Lazy imports — only here, so --help / --dry-run need no Settings/secrets.
    from app.services.investment_reports.watch_validity_review import (
        WatchValidityReviewService,
    )

    service = WatchValidityReviewService()
    try:
        results = await service.run(dry_run=False)
    finally:
        await service.close()
    print(f"review complete: {results}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not _truthy(os.environ.get(_ENABLE_ENV)):
        print(f"watch validity review disabled — set {_ENABLE_ENV}=true to opt in")
        return 0
    if args.run:
        try:
            return asyncio.run(_run())
        except Exception:
            logger.exception("watch validity review --run failed")
            return 1
    # default / --dry-run
    print(
        "dry-run: would review active watches across crypto/kr/us "
        "(read-only; writes only alert_metadata.last_review on --run). "
        "Pass --run to execute."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: 실행 → 통과 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run pytest tests/test_review_active_watches_cli.py -p no:randomly -q`
Expected: PASS (2 tests).

- [ ] **Step 5: 커밋**

```bash
cd /Users/mgh3326/work/auto_trader.rob-337
git add scripts/review_active_watches.py tests/test_review_active_watches_cli.py
git commit -m "feat(ROB-337): review_active_watches dry-run-default operator CLI

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 7: 전체 검증 + lint

**Files:** 없음 (품질 게이트만)

- [ ] **Step 1: Slice 2 + 회귀 테스트**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-337
uv run pytest tests/test_watch_validity_policy.py tests/test_watch_validity_review.py tests/test_review_active_watches_cli.py tests/test_investment_watch_scanner.py tests/test_investment_reports_repository.py -p no:randomly -q
```
Expected: 전부 PASS (신규 + 스캐너/리포지토리 회귀 무손상).

- [ ] **Step 2: lint (CI 게이트와 동일)**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run ruff check app/ tests/ scripts/ && uv run ruff format --check app/ tests/ scripts/`
Expected: 둘 다 통과. (format 실패 시 `uv run ruff format app/ tests/ scripts/` 후 amend.)

- [ ] **Step 3: 타입 체크**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run ty check app/services/investment_reports/watch_validity_policy.py app/services/investment_reports/watch_validity_review.py app/tasks/watch_validity_review_tasks.py`
Expected: 본 변경이 새로 만든 에러 없음.

- [ ] **Step 4: 정리 커밋 (필요 시)**

```bash
cd /Users/mgh3326/work/auto_trader.rob-337
git add -A && git commit -m "chore(ROB-337): lint/format Slice 2

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Post-implementation (PR 외 수동 단계)

- PR 전 사전-머지 full-CI 게이트: `ruff check app/ tests/ scripts/` + GitHub Test 워크플로우 green 확인 후 머지.
- Linear ROB-337 댓글: **Slice 2 완료 → ROB-337 전체 AC 충족**; no broker/order/order-intent mutation + scheduleless/env-gated/dry-run 경계, 테스트/CI evidence. **ROB-337 Done 처리.**
- 운영 활성화(spec §7): `WATCH_VALIDITY_REVIEW_ENABLED=true` + `--dry-run` verdict 분포 확인 → `--run` 스모크 → Hermes cutover/스케줄 등록은 별도 operator 승인.

## 비범위 (재확인)

- alert.status / watch_condition / watch_recommendation 자동 변경 없음(판정만; reprice/expire는 권고).
- 기존 trigger 스캐너(`investment_watch_scanner.py`, `*/5` KST) 무변경.
- 새 Hermes 계약/ROB-413 receiver 확장 없음(기존 review-trigger 재사용).
