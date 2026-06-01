# ROB-415 — report_quality_summary.grade partial 번들 과대평가 강등

- **이슈**: ROB-415 (E라인 E1, read-only report surface)
- **유형**: Bug fix
- **작성일**: 2026-06-02
- **연관**: ROB-421(오케스트레이션), ROB-414(E1 선행, candidate 해소), ROB-323(grade core-vs-external 정책), ROB-366 B10(honesty demotion)

## 증상 / 근본 원인

`build_report_quality_summary`가 `bundle_status="partial"`이고 `candidate_universe`가 stale인 번들에서도 `grade="high_confidence"`로 보고함.

재현(bundle_uuid `7bee448a...`, 2026-06-01): `bundle_status=partial`, `fresh_coverage_pct=67`, `external_cross_check_status=unavailable`, `candidate_universe` stale(partition_date 2026-05-29, stale_count 66054/fresh 0), 그럼에도 `grade=high_confidence`.

근본 원인: 현재 강등 규칙은
```
core_incomplete OR (thin_coverage AND no_cross_check)
```
이고 `thin_coverage`는 **집계 internal 커버리지 %**(`internal_pct < HIGH_CONFIDENCE_MIN_COVERAGE_PCT=70`)에만 의존한다. `candidate_universe`(매수 후보의 소스 = 리포트의 핵심 목적)가 단일 stale 상태여도, 다른 optional 내부 kind(news/symbol/invest_page 등)가 fresh면 집계 %가 70 이상으로 희석되어 강등이 발화하지 않는다. 외부 audit kind는 분모에서 제외되므로 % 희석이 더 커진다.

`fundamentals`/`sentiment`의 0/32 커버는 per-symbol 커버리지로, `freshness_summary`의 kind가 아니라 grade 입력에 아예 들어오지 않는다(별도 결정으로 본 PR 범위 밖, 아래 Non-goals 참조).

## 기대 동작 (이슈 Acceptance)

`candidate_universe` stale + 외부 cross-check unavailable 등이 겹치면 grade가 `high_confidence`에서 강등되어야 한다. 현재 grade는 다운스트림(stale_gate/리포트 신뢰표시)을 오도할 수 있다.

## 설계

### 변경 표면

- `app/services/action_report/common/diagnostics.py` — `build_report_quality_summary` 단일 함수.
- migration 0, read-only, 새 입력 파라미터 없음(`freshness_summary` + `bundle_status`만 사용). broker/order/watch/order-intent mutation 없음.

### 정책 (ROB-323 보존)

ROB-323 불변식: **외부 audit probe 미실행(absence)만으로는 grade를 깎지 않는다**(un-run operator probe가 fresh 리포트를 tank하면 안 됨). 강등은 internal 신호 기반, 외부는 *보상* cross-check로만 진입.

`candidate_universe`는 optional internal kind이지만 매수 후보의 소스라 staleness가 리포트의 핵심 목적을 직접 훼손한다. 따라서 강등 규칙에 `candidate_universe` non-fresh를 `thin_coverage`와 **동일한 rescue 게이트**로 추가한다(usable cross-check가 있으면 rescue 가능, 외부 미실행이면 강등):

```
candidate_universe_non_fresh = (candidate_universe present in summary) and (status != "fresh")
demote (high_confidence → informational_only) if:
    core_incomplete
    OR ((thin_coverage OR candidate_universe_non_fresh) AND no_cross_check)
```

- non-fresh = `soft_stale` / `hard_stale` / `partial` / `unavailable` / `failed` 등 `"fresh"`가 아닌 모든 상태.
- `no_cross_check`(기존 정의 유지) = external_status is None OR external_status in `CRITICAL_KIND_DEGRADING_STATUSES`(hard_stale/unavailable/failed). 즉 fresh/soft_stale 등 usable cross-check는 rescue.
- 집계 % 무관하게 발화 → repro(희석된 ≥70%)를 정확히 잡음.

### 구현 디테일

- 기존 `for kind, info in summary.items()` 루프 내에서 `candidate_universe` 상태를 캡처(별도 쿼리/순회 없음): `kind == "candidate_universe"`일 때 `candidate_universe_present = True`, `candidate_universe_status = status`. 루프는 이미 `isinstance(info, Mapping)`을 통과한 항목만 처리하므로 malformed candidate_universe는 absent로 취급(fail-open).
- `candidate_universe_non_fresh = candidate_universe_present and candidate_universe_status != "fresh"`.
- `else`(grade 후보 = high_confidence) 블록의 기존 데모션 조건에 `candidate_universe_non_fresh`를 OR로 추가.

### 기존 테스트 영향 (검산)

| 테스트 | candidate_universe | 외부 | 결과 |
|---|---|---|---|
| `splits_core_optional_external_coverage` | 부재 | unavailable | high_confidence 유지 (non_fresh=False, %=80) ✓ |
| `demotes_on_thin_optional_coverage_without_cross_check` | unavailable | 없음 | informational_only 유지 ✓ |
| `thin_coverage_stays_high_with_passing_cross_check` | unavailable | fresh | high_confidence 유지 (rescue) ✓ |
| `thin_coverage_stays_high_with_soft_stale_cross_check` | unavailable | soft_stale | high_confidence 유지 (soft_stale=usable rescue, ROB-323 의미 보존) ✓ |
| `demotes_when_external_cross_check_hard_stale` | unavailable | hard_stale | informational_only 유지 ✓ |

→ 기존 테스트는 무변경. 변경은 **새 케이스**(희석된 ≥70%에서 candidate stale + no cross-check)에서만 발생.

## 테스트 (TDD)

`tests/services/action_report/common/test_diagnostics.py`:

1. **repro 케이스**: core 4종 fresh + candidate_universe `soft_stale`(또는 hard_stale) + news/symbol/invest_page fresh(internal_pct ≥70) + 외부 unavailable → `informational_only` (이전 high_confidence). `core_fresh_coverage_pct == 100` 확인.
2. **candidate stale + passing cross-check**: 같은 구성 + toss_remote_debug fresh → `high_confidence` 유지(rescue).
3. **candidate fresh**: candidate_universe fresh + 나머지 fresh → `high_confidence` 유지(데모션 미발화).
4. 기존 회귀 전건 green.

## 안전 경계 / Non-goals

- grade는 display/audit 메타데이터 — 백엔드 게이팅이 읽지 않음. 강등은 단방향(high_confidence → informational_only).
- migration 0, broker/order/watch/order-intent mutation 없음, read-only.
- `fundamentals`/`sentiment` per-symbol 0커버를 grade 입력으로 추가하지 않음(시그니처/모든 호출부/커버리지 집계 변경 = scope creep). candidate_universe staleness가 후보-데이터 빈약의 대리 신호. 후보가 stale이면 그 fundamentals/sentiment도 fresh일 수 없으므로 동일 강등으로 귀결.
- ROB-323 외부 fail-open 불변식 보존(외부 probe absence 단독으로는 강등 안 함).
- `HIGH_CONFIDENCE_MIN_COVERAGE_PCT`/`thin_coverage` 임계 자체는 변경하지 않음.
