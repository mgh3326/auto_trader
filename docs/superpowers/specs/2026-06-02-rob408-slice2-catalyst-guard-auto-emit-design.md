# ROB-408 Slice 2 — catalyst 가드를 auto_emit verdict에 배선 (frozen evidence) 설계

- **이슈**: ROB-408 (오케스트레이션 ROB-411 C라인) — Slice 2
- **상태**: design (브레인스토밍 승인 완료)
- **날짜**: 2026-06-02
- **선행**: ROB-408 Slice 1(`app/services/market_events/catalyst/` — `evaluate_catalyst_guard`/`CatalystEvent`/`resolve_polarity`/`CATALYST_CATEGORIES`, main 65a48670).

---

## 1. 배경 — 갭과 통합 지점

Slice 1이 catalyst foundation(taxonomy + query_service + 순수 가드)을 깔았지만 **consumer가 없다** — 회고의 통증("호재 직전 트림")을 시스템이 아직 못 막는다.

탐색 결과:
- action_report는 **US 전용**(`us/action_classifier.py`). catalyst는 KR 중심 → US 직접 배선은 시장 불일치로 가치 낮음.
- **`snapshot_backed/auto_emit.py`의 `EvidenceAutoEmitter.propose()`**가 frozen 번들에서 verdict(held `sell_review`, candidate `buy_review`/`watch_only`/`data_gap`)를 생성. 스냅샷 루프가 kind별 dispatch하며 **`market` kind는 현재 미사용(skip)**. catalyst 이벤트는 Slice 1 이후 `market` 스냅샷 `payload["events"]`(market_events 행)에 포함됨.
- verdict는 `IngestReportItem(evidence_snapshot=dict)`로 emit; `evidence_snapshot`은 free-form dict라 **additive 부착 가능**(migration/schema 변경 0).

## 2. 목표·범위

`auto_emit.propose()`가 frozen `market` 스냅샷의 catalyst 이벤트를 읽어 Slice 1 가드를 적용하고, verdict의 `evidence_snapshot["upcoming_catalyst"]` + reason에 **경고만 부착**한다.

- **frozen evidence만** (라이브 `CatalystQueryService`/DB 호출 없음 — snapshot-backed 불변성).
- **verdict/action/decision_bucket/intent 불변** — 경고(annotate)만, suppression 아님(이슈 문구 "경고 플래그").
- **migration/새 스냅샷/새 필드 없음** (`evidence_snapshot` dict additive).

**안전 경계** (ROB-411 상속): broker/order/watch/order-intent mutation 없음, prod backfill 없음, scheduler 없음. 시장-불가지(번들이 KR이면 KR catalyst 적용).

## 3. 데이터 경로 (frozen)

`propose()` 스냅샷 루프에 분기 추가:
```python
elif kind == "market":
    market_payload = payload   # payload["events"] = market_events 행 리스트
```

per-symbol catalyst 추출 헬퍼(신규, 순수): `_catalyst_events_for_symbol(market_payload, symbol, *, now_date, within_days) -> list[CatalystEvent]`:
- `market_payload.get("events", [])` 중 `category ∈ CATALYST_CATEGORIES` AND `symbol` 일치 AND `event_date ∈ [now_date, now_date+within_days]`.
- frozen 이벤트 dict → `CatalystEvent`: `days_until = event_date - now_date`, `polarity = resolve_polarity(category, None)`(frozen엔 raw_payload 없음 → **category-default 극성만**), title/source 매핑.
- `now_date`: emitter에 주입(`now` 콜러블/파라미터, 기본 KST today). 파싱 실패 이벤트는 방어적으로 skip.

## 4. 가드 적용 + side 매핑

- held sell verdict(`sell_review`) → `side="trim"`: `evaluate_catalyst_guard(events, side="trim", within_days)` → positive 촉매 D-N 내면 `flag="upcoming_positive_catalyst"`.
- candidate `buy_review` → `side="buy"`: negative 촉매 D-N 내면 `flag="upcoming_negative_catalyst"`.
- `within_days` 기본 7(모듈 상수 `CATALYST_GUARD_WITHIN_DAYS`). `watch_only`/`data_gap` verdict은 가드 미적용(actionable 아님).

## 5. 부착 (additive, verdict 불변)

guard.flag 있을 때만:
```python
evidence["upcoming_catalyst"] = {
    "flag": guard.flag,
    "nearest_days": guard.nearest_days,
    "reason": guard.reason,
    "positive": [{"symbol","category","event_date","days_until"} ...],
    "negative": [...],
}
```
- 기존 reason/rationale에 flag 단서 추가(소비자/리포트 표면화).
- **`side`/`intent`/decision_bucket(`_stamp`)·rationale 핵심 미변경** — 경고만. guard.flag 없으면 부착 없음(noise 0).

## 6. 안전 / degrade

- `market` 스냅샷 부재 / `events` 없음 / catalyst 0건 → 가드 미적용(조용히 skip), 기존 verdict 그대로.
- 이벤트 dict 파싱 실패(필드 누락/형식 오류) → 해당 이벤트만 skip, 크래시 없음.

## 7. 테스트 (TDD)

1. held sell + frozen positive catalyst(conference) D-N 내 → `evidence_snapshot["upcoming_catalyst"].flag == "upcoming_positive_catalyst"` + reason; `side`/`intent` 불변.
2. candidate buy_review + negative catalyst(policy_regulation) D-N 내 → flag negative.
3. 범위 밖(event_date > within_days) / 무관 category(earnings) / market 스냅샷 부재 → upcoming_catalyst 부착 없음, verdict 불변.
4. `_catalyst_events_for_symbol`: 필터(category/symbol/range) + days_until + polarity(category-default).
5. 결정성: 동일 번들 2회 = 동일 출력. parsing 실패 이벤트 방어.
- 기존 auto_emit 테스트 패턴(in-memory 스냅샷 객체)으로 DB-free.

## 8. 비목표 (YAGNI)

- US `action_classifier` 배선(시장 불일치, 가치 낮음).
- verdict/action/decision_bucket/suggested_trim_pct 변경(경고만, suppression 아님).
- 라이브 `CatalystQueryService` 호출(frozen만).
- raw_payload 극성 override(frozen 미지원 — category-default만).
- 실제 catalyst 소스 ingestion(별도 후속).
