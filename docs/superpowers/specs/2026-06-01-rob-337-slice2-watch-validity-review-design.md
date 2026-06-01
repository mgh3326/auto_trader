# ROB-337 Slice 2 — watch 유효성 review job (설계)

- **이슈**: ROB-337 (오케스트레이션 ROB-412 D라인; Slice 1 머지 후 follow-up)
- **날짜**: 2026-06-01
- **상태**: 설계 승인됨 → 플랜 작성 단계
- **분해**: ROB-337 = 2 slice. Slice 1(추천 payload + 결정적 policy) **DONE/MERGED** (PR #1076, `c5ae5a3e`). **Slice 2(본 문서)** = 유효성 review job. 이 Slice 완료로 AC2/AC3 충족 → ROB-337 Done.
- **범위 경계**: broker/order/order-intent mutation 없음. scheduler recurring activation 없음(scheduleless + env-gated, 기본 off). alert.status / watch_condition / watch_recommendation **무변경**(판정만). 마이그레이션 0.

## 1. 목표

active watch를 주기적으로 **dry-run/read-only**로 평가해 각 watch가 계속 유의미한지 `keep / reprice / expire / review_now / data_gap`으로 분류하고, "왜 더는 유의미하지 않은지"를 설명한다. 알림은 noisy success-only가 아니라 **material change/actionable/failure 중심**으로 제한한다.

## 2. 결정 요약

- **throttle/감사 상태**: `InvestmentWatchAlert.alert_metadata` JSONB에 `last_review = {verdict, kst_date, computed_at}` 블록. 별도 테이블/마이그레이션 없음.
- **알림 경로**: 기존 `HermesNotificationClient.send_review_trigger` 재사용. verdict/reason은 `scanner_snapshot`에 싣고 `outcome="review_required"`. **watch_events row는 생성하지 않음**(payload만 합성). `HERMES_ENABLED` 기본 False → skipped.
- **무변경 원칙**: review job은 alert.status / watch_condition / watch_recommendation을 바꾸지 않는다. 유일한 write는 dry_run=False일 때의 `alert_metadata.last_review`뿐.

## 3. 컴포넌트

### 3.1 순수 분류기 — `app/services/investment_reports/watch_validity_policy.py`

I/O 없는 결정적 함수. Slice 1 policy(`compute_watch_recommendation`)와 동일 스타일.

```python
WatchValidityVerdict = Literal["keep", "reprice", "expire", "review_now", "data_gap"]

@dataclass(frozen=True)
class WatchValidityInput:
    stored_recommendation: dict | None   # item.watch_recommendation (JSONB) or None
    current_price: Decimal | None
    recomputed: WatchRecommendationPayload | None  # fresh compute for reprice/data_gap
    valid_until: datetime | None
    now: datetime

@dataclass(frozen=True)
class WatchValidityResult:
    verdict: WatchValidityVerdict
    reason: str
    recomputed: WatchRecommendationPayload | None  # attached when verdict == "reprice"
    signals: dict[str, Any]   # {current_price, entry, invalidation_price, drift_pct, days_to_expiry}

def classify_watch_validity(inp: WatchValidityInput) -> WatchValidityResult: ...
```

**상수** (모듈 레벨, 튜닝 가능):
```python
EXPIRE_SOON_DAYS = 2
REPRICE_DRIFT_PCT = Decimal("0.05")   # 5%
```

**우선순위 분류** (위→아래, 먼저 매칭되는 것 채택):
1. **data_gap**: `current_price is None` OR (`stored_recommendation is None` AND `recomputed.data_state == "data_gap"`). reason: 현재가/데이터 부족.
2. **expire**:
   - thesis broken — stored `invalidation.kind == "price_below"` AND `current_price < invalidation.price` (dip이 낙하는 칼이 됨), 또는
   - 만료 임박 — `valid_until is not None AND (valid_until - now) <= timedelta(days=EXPIRE_SOON_DAYS)`.
   - reason: 어느 사유인지 명시("price {x} fell below invalidation {y}" / "expires in {d}").
3. **review_now**: stored `entry_review_below_price is not None` AND `current_price <= entry_review_below_price` (매수 검토 zone 진입). reason: "price {x} entered review zone (<= {entry})".
4. **reprice**: `recomputed.data_state == "ok"` AND stored `entry_review_below_price` 존재 AND `abs(new_entry - old_entry)/old_entry > REPRICE_DRIFT_PCT`. reason: drift%, recomputed 첨부.
5. **keep**: 그 외. reason: "still valid; price above review zone, thesis intact".

`stored_recommendation`이 None인데 위 1에 안 걸리면(=current_price 있고 recomputed ok) → review_now/expire 판단 기준(entry/invalidation)이 없으므로 **reprice**로 분류(추천을 처음 채워넣으라는 신호), recomputed 첨부.

### 3.2 review 서비스 — `app/services/investment_reports/watch_validity_review.py`

`InvestmentWatchScanner` 미러. broker 없음.

```python
class WatchValidityReviewService:
    def __init__(self, hermes_client=None, session_factory=None) -> None: ...
    async def review_market(self, market: str, *, dry_run: bool = True) -> dict: ...
    async def run(self, *, dry_run: bool = True) -> dict:  # markets: crypto, kr, us
    async def close(self) -> None: ...
```

alert별 흐름:
1. `repo.list_active_alerts(market=..., valid_at=now_utc)` (Slice 0 read).
2. `item = repo.get_item_by_uuid(alert.source_item_uuid)` → `stored = item.watch_recommendation`, `valid_until = alert.valid_until`.
3. 현재가: `watch_market_data.get_current_value(target_kind=alert.target_kind, metric="price", symbol=alert.symbol, market=alert.market)` (read-only). 일봉으로 `compute_watch_recommendation` 재계산(reprice/data_gap 판단).
4. `result = classify_watch_validity(...)`.
5. **dry_run=True**: 계산·verdict 수집만. write/notify 없음.
6. **dry_run=False**:
   - 알림 게이트: `verdict in {review_now, expire, data_gap}` (actionable) AND material change(`verdict != last_review.verdict` 또는 `kst_date != last_review.kst_date`).
   - 통과 시 `_notify(alert, result)` → Hermes `send_review_trigger`.
   - `repo.update_alert_metadata(alert.id, {..., "last_review": {verdict, kst_date, computed_at}})`.
7. per-market 요약 stats 반환: `{market, alerts_seen, verdict_counts, notified, failed_lookups, details}`.

`_notify`는 `ReviewTriggerPayload`를 합성(event_uuid/correlation_id = uuid4, kst_date = now KST, threshold/metric/operator/threshold_key = alert에서, `scanner_snapshot = {"validity_verdict": verdict, "reason": reason, "current_price": ..., "signals": ...}`, `outcome = "review_required"`). watch_events 미생성.

### 3.3 리포지토리 — `update_alert_metadata`
```python
async def update_alert_metadata(self, alert_id: int, metadata: dict) -> None:
    # alert_metadata 전체를 주어진 dict로 교체(호출측이 기존 metadata + last_review 병합해서 전달)
```
서비스가 `dict(alert.alert_metadata or {})`에 `last_review`를 얹어 전달. flush-only(commit은 호출측/서비스).

### 3.4 진입점 (모두 default-disabled)
- **TaskIQ** `app/tasks/watch_validity_review_tasks.py`: `@broker.task(task_name="review.investment_watch_validity")` — **schedule 없음**(수동 진입만). 본문에서 env `WATCH_VALIDITY_REVIEW_ENABLED`(기본 False) 확인 → 미설정 시 `{"status": "disabled"}` 반환. 기본 `dry_run=True`.
- **운영 CLI** `scripts/review_active_watches.py`: `invest_reports_us_schedule.py` 미러. env 게이트 `WATCH_VALIDITY_REVIEW_ENABLED`. `--dry-run`(기본, plan/verdict 출력, secret 불요·lazy Settings import) / `--run`(metadata write + 알림). exit codes 0(disabled/dry-run/성공)·1(예외).

### 3.5 설정
`app/core/config.py`: `WATCH_VALIDITY_REVIEW_ENABLED: bool = False` 추가(있는 패턴 따라). 알림은 기존 `HERMES_ENABLED`(기본 False) 게이트 그대로.

## 4. 테스트

- **분류기 단위** (`tests/test_watch_validity_policy.py`): keep / reprice(drift>5%) / expire(invalidation 깨짐) / expire(만료 임박) / review_now(zone 진입) / data_gap(현재가 없음) / 우선순위(invalidation 깨짐 + zone 동시 → expire) / stored=None→reprice.
- **서비스** (`tests/test_watch_validity_review.py`, scanner 테스트 미러): active alert+item(watch_recommendation 포함) seed, market_data + Hermes stub.
  - dry_run=True: alert_metadata 불변 + Hermes 미호출 + verdict 수집.
  - dry_run=False & actionable: Hermes 1회 호출 + `last_review` 기록.
  - throttle: 동일 verdict·동일 kst_date 재실행 → Hermes 재호출 없음.
  - keep verdict: actionable 아니므로 알림 없음.
  - **no-mutation 경계**: 실행 후 alert.status=="active" 불변, item.watch_condition 불변, broker 클라이언트 import/호출 0.
- **CLI** (`tests/test_review_active_watches_cli.py`): env 미설정 → disabled exit 0; `--dry-run` → plan 출력 exit 0 (DB/secret 불요).

## 5. 안전경계 / 비범위

- broker/order/order-intent mutation 없음. alert.status / watch_condition / watch_recommendation 불변(판정만).
- scheduler scheduleless + env-gated 기본 off, dry-run 기본. Hermes 기본 off.
- 마이그레이션 0 (alert_metadata JSONB 재사용).
- sell-side/index/fx watch는 현재가 조회만 시도하되 분류는 buy-review 기준(entry/invalidation) 의존 — stored recommendation 없으면 data_gap/reprice로 안전 분류.

## 6. 완료 기준 매핑 (ROB-337 AC)

- ✅ AC2: review job이 dry-run/read-only로 active watches를 평가해 keep/reprice/expire/review_now/data_gap 산출.
- ✅ AC3: stale/thesis-broken 시 "왜 더는 유의미하지 않은지" reason으로 설명.
- ✅ AC4(나머지): trigger 근처(review_now) watch에 매수 검토 기준 표시(알림 payload), 주문 미생성.
- ✅ AC5(나머지): stale watch classification + no-mutation 경계 + notification throttling/material-change 테스트.
- → Slice 1 + Slice 2로 ROB-337 전체 AC 충족.

## 7. 운영 활성화 절차 (PR 외, operator-gated)

1. non-prod에서 `WATCH_VALIDITY_REVIEW_ENABLED=true` + `--dry-run`으로 verdict 분포 확인.
2. `--run`(여전히 read-only-근접, metadata만) 스모크.
3. Hermes 라우팅(ROB-413 receiver)·`HERMES_ENABLED` cutover는 별도 operator 승인.
4. TaskIQ 반복 스케줄 등록은 별도 승인(현재 scheduleless).
