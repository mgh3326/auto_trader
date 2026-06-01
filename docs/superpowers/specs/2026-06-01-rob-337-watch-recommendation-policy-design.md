# ROB-337 Slice 1 — watch 추천 payload + 결정적 가격기준 policy (설계)

- **이슈**: ROB-337 (오케스트레이션 ROB-412 D라인; ROB-393 다음)
- **날짜**: 2026-06-01
- **상태**: 설계 승인됨 → 플랜 작성 단계
- **분해**: ROB-337 = 2 slice. **Slice 1(본 문서)** = 추천 payload + 결정적 policy (read-only). **Slice 2(별도 이슈/스펙)** = 유효성 review job (default-disabled/dry-run) + keep/reprice/expire/review_now/data_gap 분류 + material-change 알림 throttling.
- **범위 경계**: broker/order/order-intent mutation 없음. scheduler/job 없음(Slice 2). 주문 preview/submit/cancel/modify 없음.

## 1. 목표 (Slice 1)

`/invest/reports`에서 후보/보유-추가매수 항목이 `watch_only` 또는 `limit_wait`일 때, "얼마 미만이면 매수 검토"·추천 지정가·추격 상한·무효화 조건을 **근거와 함께** 산정·저장한다. 산정은 **결정적(deterministic) policy**가 시장 evidence(현재가·일봉·호가 spread)로 수행하며, **실제 주문은 생성/제출하지 않는다**(advisory 표시 데이터).

## 2. 결정 요약

- **저장**: `InvestmentReportItem`에 nullable JSONB 컬럼 `watch_recommendation` 추가. trigger 계약(`watch_condition`)과 별개 관심사 — 추천은 advisory 표시, watch_condition은 스캐너 trigger 계약.
- **생성 seam**: 순수 결정적 policy 모듈 + 온디맨드 read-only MCP 도구. dry-run 기본, `commit=True`일 때만 item에 영속화.
- **ROB-403와 독립**: 추천은 표시용 가격기준이며 스캐너 operator 확장(`between`/zone)이 불필요. ROB-403 스키마에 의존하지 않음.

## 3. 데이터 모델 & 스키마

### 3.1 마이그레이션 (additive, nullable)
`investment_report_items`에 컬럼 추가:

```sql
ALTER TABLE review.investment_report_items
  ADD COLUMN watch_recommendation JSONB;
```

- nullable, server_default 없음(기존 행 NULL). CHECK 없음. prod 적용은 operator-gated(`alembic upgrade head` 별도 실행).

### 3.2 ORM
`app/models/investment_reports.py` `InvestmentReportItem`:

```python
watch_recommendation: Mapped[dict | None] = mapped_column(JSONB)
```

`InvestmentReportItemResponse`(스키마 read 모델)에 `watch_recommendation: dict | None = None` 추가(legacy NULL 직렬화).

### 3.3 Pydantic 스키마 (`app/schemas/investment_reports.py`)

```python
class WatchInvalidation(BaseModel):
    kind: Literal["price_below", "condition_text"]
    price: Decimal | None = None
    text: str | None = None
    model_config = ConfigDict(extra="forbid")
    # price_below → price 필수, condition_text → text 필수 (model_validator)

class WatchPriceRange(BaseModel):
    low: Decimal
    high: Decimal
    model_config = ConfigDict(extra="forbid")
    # low <= high (model_validator)

class WatchRecommendationEvidence(BaseModel):
    support: Decimal | None = None
    resistance: Decimal | None = None
    spread_bps: Decimal | None = None
    volatility_pct: Decimal | None = None
    lookback_days: int
    news_ref: str | None = None        # item.evidence_snapshot pass-through (미산출)
    screener_reason: str | None = None # pass-through
    model_config = ConfigDict(extra="forbid")

class WatchRecommendationPayload(BaseModel):
    watch_reason: str
    data_state: Literal["ok", "data_gap"]
    reference_price: Decimal | None = None
    entry_review_below_price: Decimal | None = None
    suggested_limit_price_range: WatchPriceRange | None = None
    max_chase_price: Decimal | None = None
    invalidation: WatchInvalidation | None = None
    expiry_at: datetime | None = None
    review_cadence: str = "daily"
    source_evidence: WatchRecommendationEvidence
    policy_version: str
    computed_at: datetime
    model_config = ConfigDict(extra="forbid")
    # data_state="data_gap" → 모든 price 필드 None 허용(날조 금지);
    # data_state="ok" → entry_review_below_price/suggested_limit_price_range/
    #                    max_chase_price/invalidation/reference_price 모두 not-None 요구 (model_validator)
```

## 4. 결정적 가격기준 policy

`app/services/investment_reports/watch_recommendation_policy.py` — **순수 함수 모듈**(DB/네트워크 직접 접근 없음; evidence는 호출측이 주입).

### 4.1 입력
```python
@dataclass(frozen=True)
class WatchPolicyInput:
    reference_price: Decimal          # 현재가 (quote.price)
    best_bid: Decimal | None
    best_ask: Decimal | None
    daily_highs: list[Decimal]        # 최신순 또는 정렬 명시
    daily_lows: list[Decimal]
    daily_closes: list[Decimal]
    news_ref: str | None = None
    screener_reason: str | None = None
```

### 4.2 상수 (모듈 레벨 named, 튜닝 가능)
```python
POLICY_VERSION = "v1"
LOOKBACK_DAYS = 20
VOL_FLOOR = Decimal("0.02")        # 2%
K_ENTRY = Decimal("1.0")
SUPPORT_BUFFER = Decimal("0.005")  # 0.5%
CHASE_BUFFER = Decimal("0.005")    # 0.5%
INVAL_FLOOR = Decimal("0.02")      # 2%
DEFAULT_HORIZON_DAYS = 14
ATR_PERIOD = 14
```

### 4.3 산출 규칙 (buy-side, target_kind=asset 전용)
- `support = min(daily_lows[:LOOKBACK_DAYS])`, `resistance = max(daily_highs[:LOOKBACK_DAYS])`
- `volatility_pct = max(ATR14 / reference_price, VOL_FLOOR)` (ATR 계산 불가 시 20일 close 수익률 stdev, 그것도 불가 시 VOL_FLOOR)
- `spread_bps = (best_ask - best_bid) / mid * 10000` (호가 없으면 None)
- `support_floor = support * (1 + SUPPORT_BUFFER)`
- `entry_review_below_price = clamp(reference_price * (1 - K_ENTRY * volatility_pct), low=support_floor, high=reference_price)`
- `suggested_limit_price_range = WatchPriceRange(low=support_floor, high=entry_review_below_price)` (low>high로 뒤집히면 low=high=entry로 축약)
- `max_chase_price = min(reference_price, entry_review_below_price * (1 + CHASE_BUFFER))`
- `invalidation = price_below at support * (1 - max(volatility_pct, INVAL_FLOOR))`
- `expiry_at`: 호출측이 item.valid_until 주입 시 그대로, 없으면 `computed_at + DEFAULT_HORIZON_DAYS`
- `watch_reason`: 결정적 템플릿 문자열(예: `"dip-buy review near {support} support; vol {vol}%"`)

### 4.4 data_gap (날조 금지)
`len(daily_lows) < LOOKBACK_DAYS` 또는 `reference_price` 없음 → `data_state="data_gap"`, 모든 price 필드 `None`, `watch_reason`에 사유. (프로젝트 integrity 원칙: empty/errored probe ≠ data.)

### 4.5 시간 결정성
테스트 결정성을 위해 `computed_at`은 호출측이 주입(policy 함수 인자). 모듈은 `datetime.now()` 직접 호출 안 함.

## 5. MCP 도구 surface

`app/mcp_server/tooling/`에 `investment_watch_recommend_impl` 신규 + 등록.

- 입력: `symbol: str`, `market: str`, `item_uuid: str | None = None`, `commit: bool = False`, `actor: str | None = None`
- **dry-run 기본**(`commit=False`): 시장데이터(quote+일봉) 조회 → policy 산출 → `WatchRecommendationPayload` + evidence 반환. **DB 무변경**. `item_uuid` 있으면 item.valid_until을 expiry 입력으로 사용.
- **commit=True**(+`item_uuid` 필수):
  - gate 1: item 존재 + `item_kind == "watch"` 아니어도 허용? → buy 후보(action/watch) 모두 가능하나, **AC 범위는 watch_only/limit_wait**. item의 `evidence_snapshot["action_verdict"] ∈ {"watch_only", "limit_wait"}`일 때만 허용, 아니면 actionable 거부.
  - gate 2: `data_state == "data_gap"`이면 commit 거부(advisory 빈값 저장 금지).
  - 통과 시 repository/service 경유로 item의 `watch_recommendation` 컬럼에 `payload.model_dump(mode="json")` 영속화. (`update_item_watch_recommendation(item_id, payload)` 신규 DAO.)
- 시장데이터 접근은 기존 `market_data_service.get_quote` / `get_ohlcv(period="day", count=...)` 재사용. **broker/order/order-intent 호출 0**.

도구 description에 "advisory price-review thresholds only; no order is created or submitted" 명시.

## 6. 테스트

`tests/`:
- **policy 단위** (`test_watch_recommendation_policy.py`): support/resistance/vol 산출, entry/range/max_chase/invalidation 공식, clamp 경계(엔트리가 support_floor로 바닥, current로 상한), low>high 축약, ATR fallback, data_gap(부족데이터→모든 price None, no-fabrication).
- **스키마**: WatchRecommendationPayload — data_state="ok"는 price 필드 not-None 요구, "data_gap"은 None 허용; WatchInvalidation/WatchPriceRange 불변식.
- **MCP** (`test_investment_reports_mcp.py` 확장): dry-run 무변경 / commit 영속화(컬럼 채워짐) / verdict gate 거부(watch_only/limit_wait 아닌 item) / data_gap commit 거부 / **no-mutation 경계**(broker/order 클라이언트 호출 0 — market_data만 stub).

## 7. 안전경계 / 비범위

- migration additive·nullable, prod 적용 operator-gated.
- **broker/order/order-intent mutation 없음**, scheduler/job 없음(Slice 2), 자동집행 배선 없음.
- sell-side / index / fx 추천 비범위(buy-side asset 전용).
- 알림 throttling·review job·keep/reprice/expire 분류 = **Slice 2**.

## 8. 완료 기준 매핑 (ROB-337 AC)

- ✅ AC1: watch_only/limit_wait 항목에 구체 가격기준+invalidation 저장 (§5 commit + §3 컬럼).
- ✅ AC4(부분): trigger-near watch에 매수 검토 기준 표시, 주문 미생성 (§5 advisory-only).
- ✅ AC5(부분): price-threshold policy + no-mutation 경계 테스트 (§6). stale 분류·throttling = Slice 2.
- ↪ AC2/AC3 (review job, stale/thesis-broken 설명) = Slice 2.

## 9. Slice 2 seam (다음)

Slice 2 review job은 active watch별로 `watch_recommendation`(가격기준) + `valid_until` + 현재 시장값을 읽어:
- price가 trigger/entry 근처? · invalidation 깨졌나? · 데이터 stale? · 만료 임박? → `keep / reprice / expire / review_now / data_gap` 분류 + "왜 더는 유의미하지 않은지" 설명.
- default-disabled/dry-run/read-only, material-change/trigger/failure 중심 알림(noisy success 금지).
- 본 Slice의 `watch_recommendation` 컬럼이 reprice/keep 판단의 입력 seam.
