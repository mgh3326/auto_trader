# auto_trader MCP 4기능 스펙 (운영자 세션 역할 분리용) — 초안 v1, 2026-09-05, admiral(fable)
근거: Sentry 90일 433,720콜 중 상위 8도구 82%(`analyze_stock_batch` 111k · `get_operating_briefing` 87k · `order_proposal_create` 66k · `get_holdings` 43k · `order_proposal_list` 13k …). 플레이북 실측: `kr-nxt-open`·crypto §0/§AUTO는 거의 전부 결정론, 판단은 prep 결정표·R-931 재심사·adjust·방향성 랩에 집중. 목표 = 결정론 단계를 서버(도구)로, 실행/서무를 Sonnet(helmsman)으로, 판단만 Opus(navigator)로.
공통 원칙: **read-only 우선·쓰기 도구는 dry_run 기본·멱등키 필수·기존 가드(손실매도·ladder·RSI 스코어링·approval_hash·opposite-pending) 우회 0·스키마/마이그레이션은 최소(가능하면 0)**. 각 도구는 `{version, content_hash}`(trading_policy)와 `policy_version`을 응답에 echo.

## 1. `session_bootstrap_pack(market, lanes?: list[str], include?: list[str])` — 읽기 전용
- 목적: 세션 시작 고정 5~8콜을 1콜로. 응답 크기 상한(기본 64KB, `compact=true`로 요약형).
- 입력: `market ∈ {kr,us,crypto}`; `include` 기본 = `["briefing","holdings","cash","resting","pending_retros","due_forecasts","policy","recent_context"]`.
- 출력(각 섹션 = 기존 도구 응답의 **부분집합**, 새 필드 추가 없음): `briefing`(get_operating_briefing) · `holdings`(get_holdings include_current_price=true) · `cash`(get_available_capital/get_cash_balance) · `resting`(order_proposal_list state∈{pending,resting} + 라이브 원장 미체결) · `pending_retros`(trade_retrospective_pending, count+상위 20) · `due_forecasts`(forecast_resolve dry_run=true) · `policy`(get_trading_policy market×lane 요약 + version/hash) · `recent_context`(session_context_get_recent limit=10) · `meta`(생성시각·각 섹션 소스·데이터 상태 fresh/stale/missing·소요 ms).
- 실패 처리: 섹션별 fail-open — 한 소스 실패는 그 섹션만 `{"error":..., "state":"missing"}`; 전체 실패 아님. 브로커 호출은 기존 도구와 동일 경로(새 HTTP 표면 0).
- 테스트: 각 섹션이 원 도구 결과와 **바이트 동일 부분집합**(fixture = 실제 도구 응답 캡처), 상한 초과 시 compact 강등, 부분 실패 격리. allowlist: 전 레인(analysis_readonly 포함).

## 2. `decision_table_validate(table, market)` — 순수 검증(쓰기 0)
- 입력: `kr-nxt-decision-table/v1`(및 crypto/us 변형) JSON 원문.
- 검사(전부 결정론, 플레이북 §3·사이징 절 이관): schema·`decision_table_hash` 재계산 일치(canonicalize 규칙 = UTF-8/키 사전순/공백 없음/배열 순서 보존/NaN 금지) · 행별 `scenario_id` 유일 · condition 소스/연산자/값/freshness 열거값 · `symbol/action/account/side/order_type/rung` 열거값 · price/qty가 기록된 공식·입력·반올림·tick·상하한에서 정확히 재계산되는지 · 사이징 밴드(정책) · deep-limit 거리/부호/경계 · loss guard(손실매도 차단) · 최소 주문액 · 당일 연쇄/반대주문(opposite-pending) · 섹터 클러스터 집중도(advisory) · `parent_correlation_id` 형식.
- 출력: `{valid, violations:[{row, rule, expected, actual, severity∈{block,advisory}}], recomputed:{hash, rows:[{scenario_id, price, qty}]}, policy:{version,content_hash}}`. block 1개라도 있으면 `valid=false`.
- 테스트: 실 결정표 artifact 픽스처(최근 prep artifact 3개 verbatim) + 뮤턴트(price 1tick 어긋남·hash 1바이트·loss guard 위반) 전부 block. 부작용 0 정적 가드(DB write 호출 없음).

## 3. `decision_table_apply(artifact_id, table_hash, dry_run=true, confirm=false)` — 검증된 행을 한 트랜잭션으로 집행
- 전제: `analysis_artifact_get(artifact_id)`의 표와 `table_hash` 일치 + `decision_table_validate` valid(내부 재호출). 불일치/invalid → fail-closed, 아무것도 안 씀.
- 동작(행 단위, 순서 보존): `order_proposal_create`(기존 함수 호출, 기존 가드·approval_hash·clientOrderId 규칙 그대로) · `investment_watch_create`(watch 행) · `forecast_save`(forecast 행) · `session_context_append`(1건 요약) · artifact에 `applied:{hash, at, proposal_ids}` 마킹.
- 멱등: `(artifact_id, table_hash)` 키 — 이미 applied면 no-op + 기존 id 반환(`already_applied=true`). 부분 실패 시 롤백(단일 DB 트랜잭션; proposal_create가 외부 부작용을 갖지 않는 한 — 브로커 전송은 proposal 승인 경로에 남으므로 이 도구는 브로커에 닿지 않는다 = **주문 전송 0**).
- `dry_run=true` 기본: 생성될 proposal/watch/forecast 미리보기만. `dry_run=false`는 `confirm=true` 필수(기존 mutation 도구 관례).
- 테스트: 실 artifact 픽스처 → dry_run 미리보기가 실제 apply 결과와 동일 · 멱등 2회 호출 · 중간 실패 롤백 · 브로커 클라이언트 호출 0 정적 가드. allowlist: execution 레인만.

## 4. `proposal_revalidate(market, proposal_ids?: list, dry_run=true)` — 기존 제안 재판정 라벨링
- 목적: crypto §0 "제안 11건 전수 재평가"의 결정론 부분. 각 제안을 라이브가·정책·보유/현금·원장 상태 대비 재판정.
- 라벨: `keep`(앵커 유효·가드 통과) · `dead_anchor`(라이브가가 앵커 밴드 밖/체결 불가) · `guard_blocked`(손실매도·opposite-pending·최소주문 등 현재 시점 위반) · `filled_or_expired`(원장상 종료) · `stale_policy`(policy hash 변경) — 각 라벨에 근거 수치(현재가·앵커·거리 bp·위반 규칙).
- `dry_run=true`: 라벨만. `dry_run=false`(confirm 필수): `filled_or_expired`만 `order_proposal_void`(기존 함수) — **supersede/재제안은 하지 않는다**(판단 영역, navigator 몫).
- 테스트: 라이브가 픽스처(get_quote 실 응답 모양)·원장 픽스처, 라벨 결정 경계, void 대상 한정 정적 가드. allowlist: execution + analysis(dry_run만).

## 역할 배선(참고, 별건)
- navigator(Opus): prep 결정표 작성 → `decision_table_validate` 결과 보고 수정 → artifact 저장. R-931 재심사·adjust·방향성 랩.
- helmsman(Sonnet 상주, lane.event 수신): 킥오프 이벤트 → `session_bootstrap_pack` → `decision_table_apply(dry_run)` → 조건 매칭 확인 → `apply(dry_run=false, confirm=true)`; 체결 이벤트 → fill-event-triage; 4h마다 `proposal_revalidate` → `dead_anchor`/`guard_blocked`만 navigator에 escalate.
- 기대: 하루 Opus 세션 9~10 → 3, Opus 컨텍스트 내 도구 응답 50%↓, tick/사이징 결정론 오류 0.

## 구현 순서·이슈
ROB-A `session_bootstrap_pack`(1주, 위험 최소) → ROB-B `decision_table_validate` → ROB-C `decision_table_apply` → ROB-D `proposal_revalidate`. 각 이슈 = 캡틴 1건, 검증자 Opus high, 실 artifact/응답 픽스처 필수. 스키마 변경 = C의 artifact `applied` 마킹(기존 JSON payload 내 필드, 마이그레이션 0).

## Canonical decision-table shape v1.1

Source: merged operator contract
`auto_trader-operator e86d0c1` (and `ebd4b41`), the v1.1 sections of
`prompts/kr-nxt-prep.md`.

The only complete-validation encoding for `action.rungs` is:

```json
"rungs": [{
  "rung": 1,
  "price_min": "<int KRW, tick-aligned>",
  "price_max": "<int KRW, tick-aligned, >= price_min>",
  "qty": "<int shares>",
  "tick": "<int KRW tick size>",
  "formula": "<optional text: input path, rounding direction, bounds>"
}]
```

### 결정표 정규형 강제 — v1.1 (2026-09-05, admiral/ROB-1348 검증기 전제)

2026-09-02~04 실 산출물이 날마다 다른 4가지 모양(columnar+템플릿 / rungs 리스트 / rungs 한국어 산문 / rungs 스칼라)으로 나왔고, 09-04에는 `analysis_artifact_save`에 저장된 표와 audit-evidence의 표가 **서로 다른 표**(hash 상이)였다. 아래를 지키지 않은 결정표는 기계검증(`decision_table_validate`, ROB-1348)에서 `valid=false`(block)가 되며 open이 소비하지 못한다.
1. `decision_table.rows`는 **행 객체의 배열만** 허용한다. `columns`+행배열, `tier_condition_template`, `action_contract` 같은 columnar/템플릿 전개형은 금지한다. 전개가 필요하면 prep 안에서 전개한 **결과 행**을 적는다.
2. `action.rungs`는 위 스키마의 **객체 배열만** 허용한다. 산문 문자열·스칼라 필드(`price_min: 318000` 단독)·`prices:[...]` 병렬 리스트 금지. `price_min`/`price_max`/`qty`/`tick`은 숫자(정수)이고 `formula`만 텍스트다. rung 수는 배열 길이다.
3. **동일 표 3자 일치**: report 머리말의 `decision_table_hash`, `analysis_artifact_save` payload의 `decision_table`+`decision_table_hash`, audit-evidence의 `decision_table`+`decision_table_hash`는 **같은 객체·같은 hash**여야 한다. 저장 후 artifact를 `analysis_artifact_get`으로 다시 읽어 hash를 대조하고, 다르면 prep 실패로 취급해 재저장한다(축약본·요약본 저장 금지).
4. 상위 확장 키(`row_match_unit`, `condition_evaluation`, `common_conditions`, `common_invalidation` 등)는 허용하되 `decision_table.extensions: ["<key>", ...]`에 이름을 열거한다. 열거되지 않은 상위 키는 검증기가 advisory로 보고한다.
5. 저장 전 자체 검사: 모든 행의 `rungs[*].price_min/price_max/qty/tick`이 정수인지, `price_min <= price_max`, tick 배수인지 확인한다. `decision_table_validate` 도구가 배포되면(ROB-1348) 저장 전에 호출해 `valid=true`일 때만 저장한다 — 도구 부재 시 이 자체 검사가 대체한다.

### Validator contract

| Scope | Required fields |
| --- | --- |
| Envelope | `schema_version="kr-nxt-decision-table/v1.1"`, `market`, `decision_table`, `decision_table_hash` |
| Decision table | `rows`; core keys are `no_match_action`, `rows`, and `extensions` |
| Row | `scenario_id`, `symbols`, `conditions`, `action`, `invalidation` |
| Condition | `source`, `operator`, `value`, `max_age_seconds` for a live input |
| Action | `proposal_action`, `account_mode`, `side`, `order_type`, `rungs` |
| Rung | `rung`, integer `price_min`, `price_max`, `qty`, `tick`; optional text `formula` |

`priority`, `matched_tier`, `derivation`, `time_in_force`,
`required_thesis_fields`, `reference_price`, `minimum_order_amount`, and
`sector_concentration` are optional row/action values used by the relevant
deterministic guard. Top-level extensions are optional only when their key is
also named by `decision_table.extensions`.

`kr-nxt-decision-table/v1` is temporarily accepted only with the advisory
`schema_version_deprecated_v1` until 2026-09-12. Every other version blocks
with `schema_version_mismatch`; v1 receives the same shape checks as v1.1.
The prohibited prose, scalar, and parallel-list rungs block respectively with
`price_qty_not_machine_recomputable` or `unsupported_rungs_encoding`.
Missing fields, non-integer core rung fields, inverted bounds, and a declared
tick mismatch block with `rungs_missing_field`, `rungs_field_not_integer`,
`rungs_price_bounds_inverted`, and `rungs_price_not_tick_aligned`.
`extensions` is core. A non-core top-level key absent from that list is an
`unknown_top_level_key` advisory; a listed-but-absent key is
`extensions_entry_absent` advisory. Every violation includes this section as
`canonical_shape_ref`.
