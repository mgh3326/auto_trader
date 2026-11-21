# hk 5750 국내주식 일일 계획 v0 설계·사전등록

상태: 설계 초안. 이 문서와 `research/experiments/manifests/hk-5750-daily-plan-score-vs-control.json` 등록은 초안(draft, k=0)이다. 코드·마이그레이션·프롬프트·스케줄러·주문 변경은 이 문서의 범위 밖이고, 구현은 §9의 티켓으로 나눈다.

근거 표기: 코드 근거는 `auto_trader@bdd4c776b path:line`이며, 아래 `path:line`은 모두 이 커밋 기준이다(§10에 한 곳에 모았다). hk 문서는 `hk:doc <key>`로 인용한다. 코드에서 확인하지 못한 것은 `[모름]`, 추정은 `[추정]`으로 적는다.

방향 근거(운영자 확정 11-20): `hk:doc decision/2026-11-20/at-direction-daily-plan-nonprice-go`(항목 1·2·3), `hk:doc consult/2026-11-20/at-direction-daily-plan-fable52` §2–§4·§7, `hk:doc consult/2026-11-20/at-pi-capacity-fable52` §3.4.

독립 검토 반영(hk 5933): `hk:doc review/2026-11-21/dots-daily-plan-v0-design`(hk 5909)의 H1~H3·M1·M2를 이 문서에 반영했다. plan-only 방향과 운영자가 승인한 15개 권고값(N=10·N'=20·K=5·`delta_min` 0.25%p 등)은 바꾸지 않는다. 반영 위치는 아래 표와 같다. M3(수동 슬롯 자동 마감)은 이 PR의 범위 밖이고 T5·T7에서 다룬다(§9).

| 검토 항목 | 요지 | 반영 절 |
|---|---|---|
| H1 | 행 키·계좌 범위·분모 | §1.3, §1.6, §2 후보 처분, §5.1 P1·P2, §7 보유 행, §9 Q7·Q16 |
| H2 | 계획 버전과 `forecast_save`의 두 번 쓰기, 부작용 0 | §8.1, §8.2, §9 T1·Q15·Q17 |
| H3 | 불변 번들·시각 3종·`known_zero`/`unavailable` | §3.1, §7.1, §7.2, §9 T2·Q12 |
| M1 | C 선별 조건부 질문, 순위·결측 규칙 | §4.0, §4.1, §4.2, manifest `question`·`control_spec` |
| M2 | 시가 라벨과 지정가 보조 지표 분리, 판정 순서 | §4.3, manifest `labels`·`metrics`·`stopping_rules` |

## 0. 요약

국내 live 세션은 지금 슬롯마다 조회하고 결정한다. 매수는 운영자 전략(3788)으로 동결되어 11-12 이후 모든 매수 판단이 기각이고, 그래서 모든 실험의 표본이 0이다. 일일 계획 v0는 **장 시작 전에** 보유 전체와 후보 전체에 대해 "세션이 무엇을 하겠는가"를 행으로 쓰고, 하루 동안 버전으로 갱신하며, 마감 뒤 채점한다. **주문은 없다.**

- AT(Python)는 결정론적 지표를 계산하고 형식과 규칙을 검증한다. 판단 점수는 장전 세션(out-of-process MCP consumer)이 쓴다. `app/**`에 in-process LLM provider를 넣지 않는다(하드룰 1, ROB-501).
- 새 기록 층을 만들지 않는다. 계획은 기존 행(후보·판단·예측·위임장 계획·lot 손절 계획)을 **가리키는 봉투**다(§1).
- 위험 매도와 물타기 금지는 점수가 아니라 규칙이다(§3).
- 세션 점수가 검증된 것이 아니므로 AT 지표만의 순위를 대조군으로 사전등록한다(§4). 20세션 뒤 못 이기면 점수 단계를 뺀다.
- v0 통과·중단 기준을 먼저 적었다(§5).

## 1. 봉투: 새 기록 층이 아니라 기존 행을 묶는 머리 + 버전

### 1.1 기존 기록과 대응

| 계획이 담는 것 | 이미 있는 저장소 | 계획이 하는 일 |
|---|---|---|
| 후보와 원천 순위 | `review.screener_pick_log` — 호출·원천·심볼·`rank`·`admission`·`gate_features`(`app/models/screener_pick_log.py:120-156`) | 후보 행이 `candidate_call_id`+심볼을 가리킨다 |
| 판단(손절가·목표가·기간·후보 연결·rung·기준가) | `review.decision_evidence` + `decision_evidence_rungs` + `decision_reference_prices`(`app/services/decision_evidence.py:21,47,56`) | 판단을 쓰지 않고 `decision_id`를 가리킨다(`forecast_save` 응답이 `decision_id`를 주지 않으므로 `forecast_id`에서 해석, §8·Q15). 기각 판단의 "샀다면 넣었을" 지정가·수량은 #2501 입력(`parse_declined_entry`, `app/services/decision_evidence.py:169`; MCP `forecast_save(declined_*)`, `app/mcp_server/tooling/forecast_tools.py:73`) |
| 확률 예측과 채점 | `review.trade_forecasts`(`app/models/review.py:1747`), 기각 코호트 버킷 `deferred_no_action`(`app/models/decision_vocabulary.py:26`, `app/models/review.py:1832`) | `forecast_id`를 가리킨다 |
| 승인된 분할 계획 | `review.mandate_plans`·`mandate_plan_tranches`·`mandate_plan_events`(`app/models/mandate.py:357,429,460`), 설계 `docs/design/3795-mandate.md` §3.2 | 매수 행이 같은 등록 어휘를 쓴다(§1.4). 계획이 있으면 `mandate_plan_id`로 가리킨다 |
| lot별 손절 계획과 기계식 손절 shadow 판정 | `review.lot_exit_plans`·`review.mechanical_loss_cut_decisions`(`app/models/mechanical_loss_cut.py:36,106`) | 위험 매도 행이 같은 판정 함수의 결과를 읽는다(§3) |
| 결정표 형식 검증 | `decision_table_validate`(`app/services/decision_table_validate/validator.py:78`) | 매수 행의 지정가·수량 형식 검증에 재사용을 검토한다(§9 T1, Q8) |

### 1.2 새로 저장하는 것: 위 어디에도 없는 세 가지뿐

1. **보유 종목의 행동**: hold / watch / trim / risk_sell. 기존 저장소에는 매수 쪽 판단만 있고 보유 종목의 일일 행동이 없다.
2. **실행 불가 사유**(닫힌 어휘): 행이 "그 행동을 지금 할 수 없는" 이유.
3. **버전 계보**: 하루에 몇 번, 무엇 때문에 어떤 행이 바뀌었는지.

그 밖에 마감 채점 결과(§5)는 행동 행에 대응하는 저장소가 없어 같은 묶음에 둔다. 새 테이블 여섯(제안, 이 PR에는 마이그레이션 없음; 번들 §7.1·연결 사실 §8.1이 위 넷에 더해진다):

```
review.daily_plan_heads      (market, session_date) 당 1행. 불변 정체성 + mode='plan_only'
review.daily_plan_versions   head 당 N행. 종류·트리거·입력 번들 해시·세션 비용. append-only
review.daily_plan_rows       version 당 N행. 행동·사유·점수 등급·가리키는 id들·매수 어휘 열. append-only
review.daily_plan_outcomes   row 당 채점 사실(지평별). append-only
review.daily_plan_input_bundles  버전 입력 번들 본문+해시(§7.1). append-only
review.daily_plan_row_links  채택 행의 forecast·판단 연결 사실(§8.1). append-only
```

위험 매도·점수·대조 순위는 열이지 새 층이 아니다. 3795의 관용구를 따른다: 행 UPDATE·DELETE·TRUNCATE는 모든 역할에 거부, `at_app`에는 객체별 조건부 GRANT(하드룰 18), downgrade는 행이 있으면 거부(`docs/design/3795-mandate.md` §3.4와 같은 형태, `[추정]` 세부는 T1).

### 1.3 머리·버전·행 모양

```
head(market='kr', session_date=D, mode='plan_only')
 ├─ version 1  kind=premarket  (D-1 완결 봉 기준, 08:30 KST 전까지)   rows: 전부(보유 ~82 + 후보 ~123)
 ├─ version 2  kind=fill       trigger_ref=<execution_ledger id>      rows: 그 종목만
 ├─ version 3  kind=event      trigger_ref=<market_event id>          rows: 그 종목만
 ├─ version 4  kind=session    (세션이 다시 판단한 행)                 rows: 그 행만
 └─ version N  kind=close      마감 사실                               rows: 없음(outcomes 만)
행의 현재 값 = row_key 별로 가장 늦은 version 의 행 (3795 계획 상태 = 이벤트 fold 와 같은 읽기)
```

- `row_key` = (`row_kind`, `market`, 정규화 `symbol`, `account_ref`). 보유 행(`row_kind=holding`)의 `account_ref`는 불투명 계좌 참조이고 `account_mode`(`kis_live`·`toss_live`)는 **속성**이다. 후보 행(`row_kind=candidate`)은 `account_ref=NULL`인 별도 키 유형이다. 정확한 정의·계좌 범위·분모는 §1.6, 이전 키 `(symbol, account_mode)`는 쓰지 않는다(11-21 읽기 전용 조회에서 같은 mode의 서로 다른 계좌가 실제로 중복 키를 만들었다).
- 버전은 **바뀐 행만** 쓴다. premarket 버전 1은 전체 행이고, 어제와 같다고 판정된 행은 `carried=true`+`carried_from_row_id`로 복사하되 세션 판단을 다시 하지 않는다(§6.2).
- 불변: 이미 쓴 행·버전은 바뀌지 않는다. 정정은 새 버전의 행이다.

### 1.4 매수 행의 어휘 = 위임장 계획 등록 어휘

`PlanInput`(`app/services/mandate/store.py:1012-1027`)의 필드 중 세션이 채울 수 있는 것만 같은 이름·같은 한도로 쓴다. 한도의 정본은 등록 함수다: `thesis` ≤ 1000자(`app/services/mandate/store.py:1257`), `invalidation_conditions` 각 ≤ 500자·최대 3개(`app/services/mandate/store.py:1258-1264`), `sector_key` ≤ 100자.

| 행의 열(매수 계열: new·add) | 위임장 등록 입력 | 비고 |
|---|---|---|
| `planned_amount`(정확한 decimal) | `planned_amount` | 세션이 정한 종목 목표 총액. KRW |
| `thesis` | `thesis` | 모든 행동 행에 쓴다(점수 단계가 빠져도 남는다, §4) |
| `invalidation_conditions[]` | `invalidation_conditions` | 같은 한도 |
| `sector_key` | `sector_key` | `symbol_sectors`(`app/models/symbol_sectors.py:11`) 출처. 없으면 null+`sector_unknown` |
| `would_be_limit_price` + `would_be_quantity` 또는 `would_be_notional`(정확히 하나) | — (#2501 `declined_*`와 같은 형식) | 정확한 decimal 문자열. 소수 JSON 숫자 거부 |

복사로 승격하려면 위 네 필드가 같은 어휘여야 한다. 계획이 못 채우는 `sleeve_id`·`account_ref`·`scope_account_refs`·`adopted_amount/adopted_evidence`·`one_share_price`·`created_by`는 승격 때 운영자 CLI가 채운다(`docs/design/3795-mandate.md` §3.2, 이 PR은 자동 등록을 하지 않는다, §8).

### 1.5 체결 뒤 재계산은 T4b fold를 재사용한다

두 번째 재계산기를 만들지 않는다. 다만 v0에서는 3788 동결과 자동 등록 없음 때문에 실계좌 위임장 계획이 0건이라 아래 fold 재사용은 계약만 정하고 실제로는 쓰이지 않는다. **v0에서 체결 뒤 실제로 다시 계산되는 것은 포지션 FIFO 재로드와 `evaluate_lot` 재호출뿐이다.** 위임장 계획이 가리키는 종목은 `fold_plan_events`(`app/services/mandate/fold.py:557`)와 `project`(`app/services/mandate/projection_rules.py:335`)가 낸 상태를 행의 `mandate_state`로 읽는다. 계획에 묶이지 않은 보유 행은 (a) 포지션 FIFO 재로드(`load_held_lots`, `app/services/mechanical_loss_cut/positions.py:122`)와 (b) 같은 순수 규칙 함수(`evaluate_lot`, `app/services/mechanical_loss_cut/rules.py:577`)를 그 종목에만 다시 부른다. 체결·사건 한 건은 그 종목의 행만 바꾼다(§6.3).

### 1.6 계좌 범위, 행 키, 완결성 분모 (H1)

근거(hk 5909, 11-21 읽기 전용 AT KR 보유 조회, 운영 전수의 확정치가 아니다): 64 포지션 행·응답 4계좌(`kis_live` 3계좌: 주문 가능 21행·비가능 9행·13행, `toss_api` 1계좌 21행). 계좌를 문자 그대로 합치면 `(symbol, account_mode)` 중복 키가 7개였고, 원장 FIFO 블록은 KIS KR 주문 가능 21행에만 붙었다. 기존 `LotKey`는 mode·시장·심볼뿐이라(`app/services/mechanical_loss_cut/rules.py:107-110`) 계좌 귀속을 증명하지 못하고, FIFO 로더는 flat·oversold 행을 만들지 않는다(`app/services/mechanical_loss_cut/positions.py:122`). 아래는 T1 이전에 고정하는 계약이며 기존 매매 범위(손절 shadow·실주문)는 바꾸지 않는다.

**(1) 계획에 포함하는 계좌군 — 명문화.** 브로커 보유 조회가 돌려주는 KR 계좌 전부를 계획 행의 대상으로 본다. 행마다 `account_scope`를 속성으로 둔다.

| `account_scope` | 뜻 | 계획 행 | 할 수 있는 행동 |
|---|---|---|---|
| `routable` | live 주문 도구가 보낼 수 있는 계좌(원장 FIFO가 붙는 KIS 주문 가능 계좌 등) | 있다 | 전부(계획일 뿐 주문 없음) |
| `read_only` | 조회만 되는 계좌(비라우팅 KIS 계좌, 원장 lot이 없는 Toss) | 있다(분모에 든다) | `hold`·`watch`와 `rule_status`만. `trim`·`add`·`risk_sell`은 `not_executable_reason=account_not_routable` |

- 포함은 "그 계좌에서 매매한다"는 뜻이 아니다. 보유 전체에 행이 있어야 한다는 P1을 계좌 단위로 닫기 위한 것이다. 계좌를 하나라도 제외하려면 제외 사유가 head에 남아야 하며 침묵 제외는 없다. 범위 변경은 새 manifest 버전이다. `read_only` 포함 여부는 운영자 확인 대상이다(Q16, 권고: 포함).
- 계좌 식별: `account_ref`는 `derive_account_ref`(`app/services/mandate/account_ref.py:19`) 규칙의 불투명 값이고 계좌번호 원문은 어디에도 저장하지 않는다. `account_mode`는 브로커 응답 라벨을 닫힌 매핑으로 정규화한 값이다(예: 보유 조회의 `toss_api` → 계획의 `toss_live`). 원래 라벨은 `holdings_source_label` 속성에 남기고, 매핑에 없는 라벨·참조를 못 정하는 행은 `account_attribution_unknown`으로 행을 남긴다(삭제하지 않는다).

**(2) 키와 유일성.** 행 키 = (`row_kind`, `market`, 정규화 `symbol`, `account_ref`).
- 심볼 정규화는 `app/core/symbol.py`(`to_db_symbol`)만 쓴다(하드룰 8). KR 코드는 6자리 정규형이다.
- `holding` 행은 `account_ref` NOT NULL, `candidate` 행은 NULL이며 이를 CHECK로 강제한다. 유일성은 버전 안에서 (`row_kind`, `market`, `symbol`, `account_ref`)이고 `candidate`는 부분 유일 인덱스로 NULL 비교 함정을 피한다(`NULLS NOT DISTINCT` 또는 `row_kind`별 부분 인덱스, T1이 택한다).
- 같은 종목을 두 계좌가 가지면 보유 행이 둘이다. 같은 계좌가 같은 종목을 두 줄로 돌려주면(응답이 lot 단위로 나뉜 경우) 계좌 단위로 합산하고 합산 전 행 수를 `source_row_count`에 남긴다.
- 후보 행과 보유 행의 관계: 후보 처분(`disposition`)은 **후보 키(심볼) 하나**에 있고, 계좌별 행동은 **보유 행마다** 따로 있다. 이미 보유 중인 후보는 후보 행을 하나 두고(`held_account_refs[]`로 보유 계좌를 가리킴), 각 계좌의 보유 행이 `candidate_row_id`로 그 후보 행을 가리켜 `add` 평가를 쓴다. 후보 행을 계좌 수만큼 복제하지 않는다. 채택된 `new`/`add`의 계좌 귀속은 후보 행의 `would_be_account_ref`(NULL 허용)이며, 못 정하면 채택은 유지되고 `account_attribution_unknown`으로 실행 불가다. 채택 하나는 `forecast_save` 하나이고(§8.1) 5624 모집단에 계좌 수만큼 부풀려 세지 않는다.
- 종료: 브로커 보유 수량이 0이 되거나 행이 사라진 보유는 조용히 없어지지 않는다. 이전 버전에 행이 있었다면 `holding_state=closed` 표지 행을 한 번 쓴다(#5762의 append-only 폐기·키 이동 마커와 같은 층). 원장이 flat·oversold라 FIFO 로더가 행을 만들지 않은 보유도 브로커에 있으면 행이 있다(`ledger_lots_absent`).

**(3) 완결성 분모 = 브로커 관측 스냅샷.** P1의 분모는 원장이 아니라 그 세션 premarket 번들에 고정된 브로커 보유 스냅샷이다(§7.1). 스냅샷은 계좌별로 `read_status ∈ {ok, failed, partial, unchecked}`, `broker_asof`, 행 목록을 가진다. 원장 lot과의 대조를 (`account_ref`, `symbol`)마다 닫힌 분류로 적는다.

| 분류 | 뜻 | 분모 | 계획 행 |
|---|---|---|---|
| `matched` | 브로커 수량 = 원장 순수량 | 든다 | 있다 |
| `broker_only` | 브로커에는 있고 원장·FIFO 블록이 없다(Toss, 비라우팅 KIS 등) | 든다 | 있다, `rule_status=unknown`+`ledger_lots_absent` |
| `quantity_mismatch` | 둘 다 있으나 수량이 다르다 | 든다 | 있다, `position_unknown` |
| `ledger_only` | 원장 순수량 > 0인데 `read_status=ok` 계좌의 브로커에 없다 | **분모 밖**, 별도 집계 | 있다, `holding_state=ledger_orphan`+`position_unknown` |
| `account_read_failed` | 그 계좌의 읽기가 `failed`/`partial`/`unchecked`라 행 집합을 모른다 | **계산 불가** | 행을 만들 수 없다 |

- 🔴 조회 실패는 0종목도 100%도 아니다. 범위 안 계좌가 하나라도 `ok`가 아니면 그 세션의 P1은 `UNMEASURABLE`로 기록하고(통과가 아니고 5.2 S4의 연속 실패 계수에 든다) 실패 계좌 목록을 head에 남긴다. 커버리지 수치(`rows_with_plan / broker_rows_in_ok_accounts`)는 `ok` 계좌에 대해서만 계산해 계좌별로 낸다. 실패 계좌의 행 수를 0으로 두거나 합산에서 빼서 비율을 100%로 만들지 않는다.
- 원장 단독·브로커 단독·수량 불일치 개수는 매 세션 보고한다. 이는 #5762의 Toss 대조·원장 비포함 보유 항목을 실제 데이터에서 닫는다.
- 번들 생성 자체는 브로커를 호출하지 않는다(§7). 브로커 스냅샷은 장전 세션의 기존 `get_holdings` 결과를 번들에 입력으로 넣은 것이며, 그 호출 시각·계좌별 상태가 스냅샷에 들어간다.

**(4) T1/T2 테스트(합격 조건).** 같은 mode의 계좌 둘이 같은 심볼을 가질 때 행 둘·유일성 위반 없음, `toss_api` 라벨이 `toss_live`로 매핑되고 매핑 밖 라벨은 `account_attribution_unknown`, 응답이 같은 계좌·심볼을 두 줄로 나눠도 합산, 수량 0 종료가 `closed` 표지를 한 번만 남김(재실행 멱등), 후보→다계좌 보유 이동에서 후보 행 하나+보유 행 둘이고 `disposition`이 심볼당 하나, 계좌 읽기 실패가 P1을 `UNMEASURABLE`로 만들고 비율을 만들지 않음, `ledger_only`가 분모를 부풀리지 않음.

## 2. 행동 정의

행동 어휘(닫힘): `new` · `add` · `trim` · `risk_sell` · `hold` · `watch`. 행마다 `action_basis ∈ {rule, scored}`를 적는다. 모든 행의 `action`은 v0에서 **계획**일 뿐이며 주문이 아니다(머리 `mode='plan_only'`).

| 행동 | 정의 | 입력 | 규칙 / 점수 | 실행 불가 사유 예 |
|---|---|---|---|---|
| `new` | 미보유 후보를 사겠다 | 후보 행, AT 지표 번들, 세션의 등급 | 규칙 문턱(§2.1)을 통과한 뒤 **점수**로 정렬·채택 | `halted_suspect`, `trading_restricted`, `cash_unknown`, `account_attribution_unknown`, `would_be_size_invalid` |
| `add` | 이미 보유한 종목을 더 사겠다 | 보유 lot, 후보 행, 번들 | 규칙 문턱 + **물타기 금지 규칙**(§3.2) 뒤 점수 | `add_blocked_lot_at_or_below_floor`(규칙), `position_unknown`, `open_order_unknown` |
| `trim` | 보유 일부를 줄이겠다 | 보유 lot, 세션의 사유 코드 | **점수·판단**(규칙 아님). 수량은 팔 수 있는 수량 이하 | `protected_quantity_unknown`, `open_order_unknown`, `parking_instrument` |
| `risk_sell` | 기계식 손절 판정이 자르라고 한 보유를 판다 | lot별 `evaluate_lot` 판정(§3.1) | **규칙**. 세션이 바꾸지 못한다 | 판정에 필요한 입력 부족은 `risk_sell`이 아니라 `hold`+`rule_status=unknown`(§3.1) |
| `hold` | 보유를 유지한다 | 보유 lot | 기본값. 규칙이 `skip`한 lot과 세션이 `hold`를 택한 lot | 위험 규칙 입력 부족이면 `rule_status=unknown`을 함께 적는다 |
| `watch` | 지금 행동하지 않고 다음 세션에 볼 조건을 적는다 | 후보 또는 보유 | **점수·판단**. 조건은 닫힌 `trigger_kind`(`price_at_or_below`·`price_at_or_above`·`event`)+값+`valid_until` | `calendar_unknown` |

`watch`를 v0에 넣는 이유: 후보 중 "채택은 아니지만 탈락도 아닌" 상태(지지선까지 대기, 사건 대기)가 있어야 후보마다 채택·탈락 사유가 있다는 §5 기준을 닫힌 어휘로 맞출 수 있다. 🔴 `watch` 행은 감시(`investment_watch_*`)를 **등록하지 않는다**. 기록만 한다. 감시 등록은 별건이다.

후보 처분(`disposition`)은 후보 키(시장+심볼, `account_ref` 없음) 하나에 있고 계좌별 행동(`action`)은 보유 행마다 따로 있다(§1.6): `adopted` · `watch` · `dropped`와 닫힌 `reason_code`(예: `adopted_top_rank`, `adopted_event`, `watch_wait_support`, `watch_wait_event`, `dropped_rule_blocked`, `dropped_low_grade`, `dropped_liquidity`, `dropped_thesis_unclear`, `dropped_concentration`, `dropped_already_held_no_add`, `dropped_outranked_by_cap`, `dropped_data_missing`). 목록의 정본은 T1의 어휘 모듈이고, 이 문서의 목록은 시작 후보다. 자유 텍스트 사유는 보조 필드(≤200자)이며 코드를 대체하지 못한다.

### 2.1 규칙 문턱(점수 이전, 후보 풀을 정한다)

세션 점수가 닿기 전에 AT가 결정론으로 거른다. 거른 후보는 `dropped_rule_blocked`+닫힌 사유로 행에 남는다(삭제 없음).

- 정지 의심: `halted_suspect`(`app/services/halt_detection.py:184,233`, ROB-1236의 N=3 의심 규칙 그대로. 확정이 아니다).
- 거래 제한 표시(`_trading_restriction_reason`, `app/mcp_server/tooling/buy_candidate_fanout.py:556`).
- 가격 부재·stale: 완결 일봉이 없거나 오래됐으면 행을 `price_missing`/`price_stale`로 두고 점수 풀에서 뺀다. 가격 없음을 0이나 중립으로 채우지 않는다.
- 주차 종목(`is_parking_instrument`, `app/services/mechanical_loss_cut/rules.py:232`)은 `trim`·`risk_sell` 대상이 아니다.

## 3. 위험 매도와 물타기 금지는 점수가 아니라 규칙이다

근거: ROB-1458 증거 `docs/research/rob-1458-strategy-evidence-2026-11-02.md`, 방향 `hk:doc consult/2026-11-20/at-direction-daily-plan-fable52` §3.2.

### 3.1 위험 매도: 기계식 손절 shadow 판정을 그대로 읽는다

- 판정 함수는 순수 `evaluate_lot`(`app/services/mechanical_loss_cut/rules.py:577`)이다: 평단 아래 → 기록된 손절가 아래 → 평단 대비 `unrealized_floor_pct` 이하 → `rising_compare_sessions_ago` 세션 전 종가보다 높지 않음 → 트리거 계열 조건 → 미체결 매도·보호 수량 차감 뒤 팔 수 있는 수량. 모든 증거 부족 경로는 `Skip`으로 끝난다. 닫힌 사유 어휘는 `SKIP_REASONS`(`app/services/mechanical_loss_cut/rules.py:34-55`)다.
- 행의 `rule_verdict`에 `{result: cut|skip, skip_reason?, trigger_key?, close, stop, unrealized_pct, plan_version, policy_version, policy_content_hash}`를 싣는다. `cut`이면 `action=risk_sell`, `action_basis=rule`이다. 세션은 이 행의 `action`·`quantity`를 바꾸지 못하고, 서버가 같은 입력(버전의 `input_bundle_sha256`이 고정한 번들)으로 다시 계산해 일치하지 않는 쓰기를 거부한다(§9 T1 검증).
- 🔴 증거 부족은 안전 확인이 아니다: 입력을 몰라서 끝난 skip(`unavailable`: `no_exit_plan`·`position_unreadable`·`price_missing`(봉 부족 포함)·`price_stale`·`halted_suspect`·`open_sell_unknown`·`protected_unknown`)은 행을 `hold`+`rule_status=unknown`+같은 사유로 남긴다. 입력을 알고 평가해 자르지 않은 skip(`evaluated`: `not_underwater`·`above_planned_stop`·`floor_not_reached`·`rising`·`not_n_day_low`·`trailing_drop_not_reached`·`covered_by_open_sell`·`below_one_unit`·`already_cut_this_run`·`cooldown`)만 `rule_status=evaluated`이고, `parking_instrument`는 `not_applicable`이다. 입력 수량 자체도 같은 구분을 따른다: 미체결 매도·보호 수량이 **0으로 확인됨**(`known_zero`)과 **읽지 못함**(`unavailable`)은 다른 값이며 번들 필드가 둘을 구분해 싣는다(전체 열거와 테스트는 §7.2). 위험 상태를 모를 때 "위험 없음"으로 보이지 않게 한다.
- 세션은 별도로 **논지 훼손 여부**만 기록한다: 행의 `thesis_status ∈ {intact, weakening, broken, unrecorded}`. 이 값은 정보이며 어떤 이벤트·제안·주문도 만들지 않는다. 위임장 쪽 `thesis_broken` 이벤트는 승인 이슈와 회고가 있어야 쓸 수 있고(`docs/design/3795-mandate.md` §5.2), v0는 그 경로를 부르지 않는다.
- **읽기 전용 경로가 필요하다(현재 코드 사실)**: `evaluate_all_lots`는 읽기 전용이 아니다. lot에 계획이 없으면 backfill 계획을 쓰고(`app/services/mechanical_loss_cut/job.py:228-231`) 판정마다 shadow 행을 쓰며(`app/services/mechanical_loss_cut/job.py:275`), `commit=False`는 커밋만 건너뛴다(`app/services/mechanical_loss_cut/job.py:282-283`). 반환은 cut 판정 목록과 skip **개수**뿐이라 lot별 skip 사유가 나오지 않는다(`app/services/mechanical_loss_cut/job.py:250,289`). 또 `run_mechanical_loss_cut`은 `MECHANICAL_LOSS_CUT_SHADOW_ENABLED`가 꺼지면 `disabled`를 돌려준다(`app/services/mechanical_loss_cut/job.py:327`). 그래서 v0는 **스위치를 켜지 않고** 같은 순수 `evaluate_lot`을 읽기 포트(`held_lots`·`latest_plans`·`bars`·`open_sell_quantities`·`protected_quantities`)만으로 lot별로 부르는 읽기 전용 평가기를 새로 둔다(하드룰 19: 스위치 기본 off 유지·shadow 모듈은 브로커·주문 import 금지). 계획이 없는 lot은 backfill을 쓰지 않고 `no_exit_plan`(unknown)으로 남긴다. shadow 행을 테이블에 남기는 기존 수동 lever(`mechanical_loss_cut.evaluate`, 스케줄 없음, `app/tasks/mechanical_loss_cut_tasks.py:20`)는 별개이며 이 문서가 바꾸지 않는다(Q4).
- 🔴 파라미터는 잠정이다: 정책 `mechanical_loss_cut`은 `parameter_source: "provisional shadow defaults pending ROB-1458 evidence"`, `thresholds_confirmed: false`, KR `unrealized_floor_pct: -5`, 기본 손절 `pct_floor 15`(`config/trading_policy.yaml:1842-1857`). 따라서 `risk_sell` 행은 shadow 판정이며 "실전에서 자를 것"이라는 주장이 아니다. 행에 `policy_version/hash`를 싣고 정책이 확정되기 전의 판정임을 `rule_verdict.thresholds_confirmed=false`로 남긴다.

### 3.2 물타기 금지: −8% 이하 lot의 추가매수는 가점·감점이 아니라 "실행 불가"

- 규칙: 보유 lot의 평단 대비 미실현이 **−8% 이하**이면 그 lot의 `add`는 `not_executable_reason = add_blocked_lot_at_or_below_floor`로 **고정**한다. 점수 풀에 넣지 않고 점수 가감도 하지 않는다. 판정 입력은 `HeldLot.average_cost`(포지션 FIFO)와 마지막 완결 종가이며 정확한 decimal 비교다(경계 −8.00% 포함, `evaluate_lot`의 정확한 비교와 같은 원칙, `app/services/mechanical_loss_cut/rules.py:630`).
- 근거(KR, ROB-1458): −8% 이하 lot에 한 실제 추가매수 n=21 성숙, D+20 중앙값 −7.9%·Q1 −15.6% → FAIL(retire), 같은 구간의 "첫 −8% 종가에 50% 추가" 체계 규칙 n=46 중앙값 −2.8%·Q1 −12.1% → FAIL(retire)(`docs/research/rob-1458-strategy-evidence-2026-11-02.md:35`, 표 `:74`). 위험 매도 쪽: KR에서 −8% 첫 도달 때 기계적으로 자른 쪽이 마지막 봉까지 실제 보유보다 나았던 경우 63.6%(n=44)(`docs/research/rob-1458-strategy-evidence-2026-11-02.md:38`).
- 🔴 증거의 한계를 행에 같이 쓴다: KR 지수가 같은 창에서 −17.4%였던 단일 체제라 "물타기가 일반적으로 나쁘다"가 아니라 이 창의 증거다(같은 문서 `:35`의 caveat). 미국(n=4, INSUFFICIENT_SAMPLE; 체계 규칙은 PASS)·코인(혼재)은 증거가 다르므로 **v0는 KR만** 대상이고 이 규칙을 다른 시장으로 넓히지 않는다(`docs/research/rob-1458-strategy-evidence-2026-11-02.md:36,37`).
- 이 −8%는 기계식 손절 정책(−5%·15%, 잠정)과 별개의 고정 상수다. 정책 `config/trading_policy.yaml`의 새 키(`daily_plan.no_add_floor_pct`)로 두고 operator PR로만 바꾼다(정책은 판단 임계값 단일 소스, `docs/agent-contracts/data-model-and-symbol-contracts.md` Trading Policy YAML 절). 값은 사전등록 manifest에 해시로 박는다. 기존 `evaluate_add_against_plan`의 `at_or_below_stop`(가격 ≤ 계획 손절가, `app/services/mechanical_loss_cut/rules.py:376-388`)은 **다른 조건**이며 합치지 않는다. 두 값 모두 행에 남는다(`add_verdict`는 기록일 뿐 매수를 막지 않는다는 기존 계약 그대로).
- 평단을 모르거나 포지션이 `unknown`이면 −8% 판정을 하지 못하므로 `add`는 `position_unknown`으로 실행 불가다(알 수 없음 ≠ −8% 위가 아니다).

## 4. 점수 분할, 대조군, 사전등록

### 4.0 질문의 형태: 조건부 비교 (M1)

v0가 답하는 질문은 "전체 후보에서 세션의 독립적 점수가 AT 지표 순위보다 우월한가"가 **아니다**. 세션은 변경 행과 `c_rank` 상위 N'(=20)만 재평가하므로(§6.2) 비교 풀 `P_t`는 C가 부분적으로 미리 고른 공통 풀이다. 따라서 질문은 조건부다.

> C가 full eligible 후보 집합 `E_t` 위에서 미리 고른 공통 풀 안에서, 세션의 가치·사업 건전성 등급 재정렬이 C의 순서보다 계획 세션 시가 기준 5세션 순수익률을 높이는가?

- `E_t` = §2.1을 통과한 후보 전체(세션이 보기 전에 번들에 고정, §7.1). `P_t` ⊆ `E_t`.
- 결과는 이 조건부 질문에만 해석한다. "점수가 후보 발굴 전체를 개선한다"로 일반화하지 않는다. 새로 평가한 행과 이월한 행의 시간차, 지표 결측이 상위군을 가르는 효과도 섞여 있으므로 선택된 후보의 결측·이월 비율을 팔별로 보고한다(§4.3 보조).
- 서버는 premarket 버전마다 `C_full_topN`(`E_t` 위의 C 상위 N, 경계 동점 포함)과 `C_pool_topN`(`P_t` 위의 C 상위 N)을 계산해 저장하고 둘의 일치 여부 `c_pool_consistent`를 기록한다. 일치하지 않는 세션은 `UNVERIFIED` 후보로 센다(§4.3의 10% 한도).

### 4.1 점수 분할

- **AT가 결정론으로 계산**: 지표 번들(§7), 규칙 문턱(§2.1), 위험 매도(§3.1), 물타기 금지(§3.2), 대조 순위 `c_rank`(§4.2), 변경 플래그(§6.2).
- **세션이 쓴다**: 두 축의 닫힌 등급, 논지, 무효 조건, 채택·탈락 사유 코드.
  - 축: `value`(가치) · `quality`(사업 건전성). 각 `strong | neutral | weak` 세 단계. 자유 점수·숫자 확신도는 받지 않는다.
  - 합성: `composite = grade_value + grade_quality`, 매핑 strong=+1·neutral=0·weak=−1(범위 −2~+2), 합성 값은 5단계뿐이라 N=10 경계에 동점이 생긴다. 동점은 `c_rank`로 깨지 않고, 경계 동점 구간의 후보 전부에 남은 몫(N − 경계 위 후보 수)을 **같은 가중으로 나눠** 평균에 넣는다(`symbol` 순서 같은 임의 규칙으로 구성원을 정하지 않는다, Q14). C의 `c_rank`는 연속값이라 동점이 드물지만 같은 분할 규칙을 쓴다. **가중(1:1)과 매핑은 에포크 동안 고정**이다. 주간 검토는 변경을 제안만 할 수 있고, 변경은 새 manifest 버전(새 에포크)으로만 적용된다. 이는 `hk:doc consult/2026-11-20/at-direction-daily-plan-fable52` §3.3의 "숫자 가중 합은 주간 검토 전까지 고정"을 사전등록과 양립시키는 해석이다(Q1).
- 근거(점수가 검증되지 않음): 세션의 확률 예측 Brier 0.247(n=2,523), 신규 매수 후보만 0.201(`hk:doc consult/2026-11-20/at-direction-daily-plan-fable52` §3.3).

### 4.2 대조군 C: 같은 후보를 AT 지표만으로 줄 세운 순위

- C는 세션 판단 없이 **AT 지표만으로** 후보 풀을 줄 세운다. 구성(제안, 봉인 때 정확한 목록과 부호를 해시로 고정, 튜닝 없음):

  | 지표 | 출처 | 부호(좋은 쪽) |
  |---|---|---|
  | 지지선까지 거리 `dist_to_support_pct` | `invest_screener_snapshots`(`app/models/invest_screener_snapshot.py:93`) | 작을수록 |
  | RSI-14 `rsi_14` | `analyze_stock_batch(quick=True)`(`app/mcp_server/tooling/analysis_quick.py:734`, 단일 산식 `app/mcp_server/tooling/market_data_indicators.py:459`) | 낮을수록 |
  | 거래대금 `daily_turnover` | `invest_screener_snapshots`(`app/models/invest_screener_snapshot.py:84`) | 클수록(유동성) |
  | PBR `pbr` | `market_valuation_snapshots`(`app/models/market_valuation_snapshot.py:52`) | 낮을수록 |
  | 외국인 순매수 5세션 합 `foreign_net` | `investor_flow_snapshots`(`app/models/investor_flow_snapshot.py:67`) | 클수록 |

  **`c_rank` 사전등록 규칙** (정확한 지표 목록·부호·아래 규칙은 봉인 때 `control_spec` 해시로 고정, 튜닝 없음):

  1. **순위 집합은 `E_t` 전체다.** `c_rank`는 C가 본 전체 후보 분포 `E_t`에서 premarket에 **한 번** 계산해 저장하고, 비교 풀 `P_t`로 줄어든 뒤 `P_t` 안에서 재정규화하지 않는다.
  2. **지표별 점수**: 지표 i에서 값 x를 가진 후보의 `score_i(x) = (n_worse + (n_tied − 1) / 2) / (n_obs − 1)`. `n_obs` = `E_t`에서 지표 i가 유효한 후보 수, `n_tied` = x와 정확히 같은 값을 가진 후보 수(자기 포함), `n_worse` = 부호표상 x보다 나쁜 후보 수. 범위 [0, 1], 1이 가장 좋다. 정확한 분수·`Decimal` 계산이며 float를 쓰지 않는다. `n_obs < 30`이면 그 지표는 그 세션 `E_t` 전체에서 결측(`indicator_min_obs`; 값 30은 봉인 전 확정, 이후 불변).
  3. **합성**: `c_rank` = 유효 지표 점수의 동일 가중 평균. 유효 지표가 3개 미만이면 `c_rank = null`(`c_rank_unavailable`, 전 지표 결측 포함). **결측을 0·중립 값으로 채우지 않는다.** 유효 지표 수 `c_rank_n_ind`를 행에 기록한다.
  4. **정렬과 동점**: `c_rank` 내림차순. 동점은 정확히 같은 값만이고, 상위 N 경계의 동점 구간에는 §4.1과 같은 규칙(남은 몫을 구간 전체에 같은 가중으로 분할)을 쓴다. `symbol` 순서 같은 임의 규칙으로 구성원을 정하지 않는다.
  5. **`c_rank = null` 후보**는 C 순위와 `P_t`에서 모두 빠진다(S와 C가 같은 후보 집합을 쓰게 하는 대칭 규칙). `E_t` 대비 빠진 후보 수·비율을 세션마다 기록한다.
  6. **지표 출처·단위**: 지표마다 출처 소스 하나와 단위를 봉인 때 고정하고, 단위가 다른 소스의 값을 한 합에 섞지 않는다. `investor_flow_snapshots.foreign_net`은 BigInteger이고 단위 열이 없으며 `source` 열만 있다(`app/models/investor_flow_snapshot.py:67`) — 값의 단위(주수/금액)는 `[모름]`이므로 T2가 수집기 코드로 확인해 `control_spec`에 적고, 확인되지 않으면 이 지표는 결측이다. 5세션 합은 **확정 시리즈**의 마지막 5개 확정 세션이며 잠정값을 쓰지 않고, 마지막 확정 세션이 D−1이 아니면 `flow_stale`로 결측이다.
  7. **봉인 전 확인(T7 점검표)**: [#5754](https://console.robinco.dev/ui/tasks/5754)의 PBR 공급 결손, [#5755](https://console.robinco.dev/ui/tasks/5755)의 최초 관측 시각 증명, [#5756](https://console.robinco.dev/ui/tasks/5756)의 값 채움률(`FILL_RATE_JSON`)을 확인한다. 지표별 채움률이 낮아 `c_rank_unavailable` 비율이 `E_t`의 10%를 넘으면 봉인하지 않고 지표 목록을 새 manifest 버전으로 고친다.

  부호는 지지선 근접·과매도·유동성·저평가·외국인 매수라는 매수 후보 탐색의 통상 방향으로 **사전 선언**한 것이며 데이터로 다시 맞추지 않는다(기존 스크리너 정렬 방향과의 일치 여부는 이 문서에서 확인하지 못했다 `[모름]` — T2에서 대조하고 다르면 봉인 전에 목록을 고친다).
- 🔴 누수 방지: `c_rank`는 premarket 입력 번들 안에서 **세션이 보기 전에** 계산·저장되고(번들 행의 `created_at` < 버전 1의 `created_at`), 세션에게 주는 번들에서는 `c_rank`를 뺀다. 세션이 대조 순위에 앵커링하지 않게 한다. 번들의 입력은 모두 `available_at ≤ plan asof`다(§7).
- 세 팔: **S**(세션 합성 등급 상위 N), **C**(`c_rank` 상위 N), **E**(풀 전체 동일 가중 평균, 참고). N = 10(Q2). 비교 풀 `P_t` = 세션 등급이 유효한 후보(오늘 판단했거나 이월 ≤ K=5세션) ∩ §2.1 통과 ∩ `c_rank` 유효 후보. C의 상위 N은 `E_t`에서 고정된 `c_rank`로 `P_t` 안에서 고른다(재정규화 없음, 위 1). 세션이 판단하는 집합 `J_t`는 §2.1을 통과한 후보 중 `c_rank` 상위 N'(N' ≥ N, §6.2)를 포함하므로 보통 `C_pool_topN = C_full_topN`이지만 이월 만료·결측으로 어긋날 수 있어 §4.0이 일치 여부를 세션마다 기록한다. `P_t`의 크기가 N 미만이면 그 세션은 점수를 매기지 않고 `UNVERIFIED`로 센다(`pool_below_n`; N보다 작은 N으로 비교하지 않는다).

### 4.3 비교와 판정

#### 4.3.1 주 라벨: 시가 기준 순위 비교용 (실행 가능성의 증거가 아니다)

- 라벨: 계획 세션 D의 **정규장 시가 → D 포함 5번째 정규장 종가** 순수익률(H-admit의 t+1 시가→t+5 종가와 같은 정의, `research/experiments/manifests/h-admit.json`). 달력 XKRX. 두 팔이 같은 시가 진입을 가정하는 것은 **등급 순위끼리의 비교를 같은 조건에 놓기 위한** 정의이고, 실제 주문이 시가에 체결될 수 있다는 증거도, 지정가 체결 편향이 상쇄된다는 증거도 아니다. 이 라벨은 주문 가능성을 주장하지 않는다.
- 비용: 정책의 `round_trip_cost_bps.kr`(`app/schemas/trading_policy.py:1782-1784`, 기본 47.4bp)을 봉인 때 정책 버전·해시로 고정해 양팔에 같게 차감한다. 같은 상수를 같은 가중으로 두 팔에서 빼므로 **`S − C` 차이에서는 상수가 상쇄된다**. 비용은 절대 수준 보고용이고 주 지표(차이)는 비용 값에 의존하지 않는다. 팔마다 다른 비용 가정이 필요해지면(예: 한쪽만 지정가) 이 상쇄 주장은 성립하지 않으며 새 manifest 버전이다. 실제 체결가에 이미 반영된 슬리피지를 다시 차감하지 않는다.
- **봉인할 규칙(분모·결측·조정)**:
  1. 분모: 팔마다 상위 N(=10) 후보의 동일 가중 평균, 경계 동점은 §4.1 규칙. 분모는 라벨이 유효한 후보 수이고 유효 수를 팔·세션마다 기록한다.
  2. 후보 단위 결측 사유(닫힌 어휘): `no_open_price`(D 시가 없음) · `untradable_d`(D 정지 의심 `halted_suspect`·거래량 0·거래 제한 표시) · `no_close_d4`(5번째 종가 없음) · `corporate_action_unadjustable`(D~D+4에 기업행위가 있고 조정 계약이 없음) · `delisted`(상장폐지·거래정지 지속). **결측을 0수익으로 채우지 않는다.** 후보는 팔 평균에서 빠지고 사유별 개수가 보고된다.
  3. 팔의 유효 후보가 ⌈N/2⌉=5개 미만이면 그 세션은 `UNVERIFIED`다. 한 팔이라도 미달이면 두 팔 모두 그 세션을 쓰지 않는다(비대칭 제외 금지).
  4. 가격·조정 계약: 시가·종가는 조정 여부와 출처를 함께 저장한 일봉 한 종류만 쓰고 소스를 섞지 않는다. 기업행위 조정 계약이 없으면 해당 후보는 `corporate_action_unadjustable`이다(`UNKNOWN`).
  5. 빈 날: 후보 집합이 없거나(`candidate_source_unavailable`) D가 XKRX 세션이 아니거나 `P_t`가 N 미만이면 그 날은 **무효 세션**이다. 0으로 채우거나 평균에서 몰래 빼지 않고 `session_void`+사유로 기록하며 §4.3.4의 품질 단계 분모에 든다. 운영자 중지·휴장은 세션 수에 세지 않는다(§5.2 끝).
- 주 지표: 성숙 세션마다 `S 상위 N 평균 − C 상위 N 평균`(순수익률)의 세션 평균. 세션 단위로 짝지은 서술 통계이며 유의성 검정이 아니다(5624와 같은 입장, `docs/design/5624-entry-ab-mock.md` §3). 최소 효과 `delta_min` = +0.25%p(5624와 같은 값 재사용, Q3). 5거래일 라벨은 연속 세션끼리 겹치므로(자기상관) 20회·효과 컷은 **서술적 실험**이고 표본 크기 문턱은 통계 검정력이 아니다.

#### 4.3.2 보조: 지정가 기반 지표는 별도 계산이며 S 대 C 효과 주장이 아니다

- 행의 `would_be_limit_price`는 **채택된 `new`/`add` 행에만** 의무다(§1.4). C 상위 N 후보 전원이 같은 기준의 지정가를 갖는다는 보장이 없다. v0는 C(AT 규칙)에 대칭적인 가격 정책을 만들지 않는다. 따라서 지정가 결과는 **지정가가 기록된 하위집합의 보조 관찰**이며 S와 C의 효과 비교에 쓰지 않는다. 두 팔의 하위집합 크기(`S∩기록`, `C∩기록`)를 함께 보고한다.
- 범위: 같은 계획 세션 D의 **정규장(09:00–15:30) 하루**만 본다. 시가 ≤ 지정가이면 시가 체결(`filled_at_open`), 아니면 저가 ≤ 지정가(touch, 낙관) 또는 저가 < 지정가(through, 보수)일 때 지정가 체결, 그 밖은 `unfilled`. D 이후 이월·야간·D+1 체결은 보지 않는다. 체결 후 수익은 체결가→D 포함 5번째 종가다.
- 세 상태는 분모와 평균에서 **섞지 않고 따로** 센다: `missing_input`(지정가 미기록·시가/저가 결측·§4.3.1 2의 사유) · `unfilled`(체결 안 됨; 수익 평균에 넣지 않고 체결률로만 보고) · `filled`(체결, 수익이 정확히 0인 경우도 `filled`로 센다). `unfilled`를 수익 0으로 평균에 넣은 보조값은 따로 라벨을 붙여 내되 주 결과와 합치지 않는다.

#### 4.3.3 기타 보조

S∩C 겹침률, 팔별 평균·분위·승률, E 대비, 판단 집합 `J_t` 크기, 이월 비율, 팔별 선택 후보의 결측·이월 비율(§4.0), `c_rank_unavailable` 비율, 등급별 평균 수익률. 모두 서술 통계다.

#### 4.3.4 판정: 품질 → 표본 → 효과 순서 (#5762)

20 계획 세션 + 마지막 평가 5세션 뒤 **한 번**, 아래 순서로만 읽는다. 앞 단계에서 끝나면 뒤 단계를 읽지 않는다.

1. **품질**: 라벨 판정 불가·무효 세션(`UNVERIFIED` 사유: 풀 구성·가격·비용 불가, `pool_below_n`, `c_pool_consistent=false`, 한 팔 유효 후보 5 미만, `session_void`)이 계획 세션의 10%(2세션)를 넘으면 `UNVERIFIED`로 끝난다. 점수는 `unproven`으로 동결한다.
2. **표본**: 품질을 통과하면 성숙 세션 < 15이면 `INSUFFICIENT_SAMPLE`이다(점수 `unproven` 동결, 기간 연장 없음, 다음은 새 에포크).
3. **효과**: 성숙 세션 ≥ 15이면 주 지표 ≥ `delta_min`이면 `SUPPORTED`, 미만이면 `NOT_SUPPORTED`(점수 단계를 뺀다 — 세션은 `thesis`·`invalidation_conditions`·`thesis_status`·처분 사유만 쓰고 채택 순위는 AT 지표 순위로 대체).

- 열람 규칙: 봉인 뒤 중간에는 안전·완결성(§5)만 보고 비교 지표를 읽거나 규칙을 바꾸지 않는다. 최종 1회 열람은 ROB-1429의 `test_looks`/`k`를 따른다(`research/experiments/README.md`).

### 4.4 사전등록 위치

실험 manifest `research/experiments/manifests/hk-5750-daily-plan-score-vs-control.json`을 등록한다(`status=draft`, `k=0`). `hashes`의 dataset/code/policy/cost_table/control 지표 목록은 `pending`이며 시작 전 봉인 때 고정한다(§9 T7). 레지스트리 lint(`scripts/ci/experiment_registry_lint.py`)는 이 PR에서 통과해야 한다.

## 5. v0 통과·중단 기준 (20 계획 세션)

지난 모의가 실패한 세 원인(기준 없음, 청산·채점 미완주, 실전과 다른 체결, `hk:doc consult/2026-11-20/at-direction-daily-plan-fable52` §3.4)을 v0에 적용한다. 기준은 **세션 시작 전 고정**이며 사후에 바꾸지 않는다. P1~P5는 브리프의 "매일"·"마다"를 그대로 20/20으로 적었다. 결손일 허용 폭은 Q13에서 따로 정한다(정하기 전까지 허용 0).

### 5.1 통과 (모두 만족)

| # | 기준 | 측정 |
|---|---|---|
| P1 | 매일 보유 전체에 행이 있다 | 계획 세션마다, premarket 번들에 고정된 브로커 보유 스냅샷(§1.6)의 (`account_ref`, `symbol`) 전부에 대해 버전 1에 보유 행이 있는 비율 = 100%이고 범위 안 모든 계좌의 `read_status=ok`. 계좌 하나라도 `ok`가 아니면 그 세션은 `UNMEASURABLE`(통과도 100%도 아님, 실패 계좌 목록 보고, S4 계수에 듦). 원장 단독·브로커 단독·불일치 개수를 함께 보고. **20/20세션** |
| P2 | 후보마다 채택·탈락 사유가 있다 | 같은 세션의 후보 집합 전체(`candidate_set_sha256`)의 후보 키(심볼)마다 `disposition`+닫힌 `reason_code`가 하나 있는 비율 = 100%(계좌별 행동 행은 센 대상이 아니다). 채택 행은 `forecast_link_state=confirmed`(§8.1)여야 센다. **20/20세션** |
| P3 | 마감 채점이 사람 없이 끝난다 | 다음 계획 세션의 premarket 시작 전까지 그 세션의 모든 행에 대한 마감 outcomes(§1.3 `close`)가 사람 개입 없이 써진다. 사람 개입 = 수동 도구 호출·DB 수정. **20/20세션** |
| P4 | 세션 1회 비용이 기록된다 | 모든 premarket 버전에 `session_cost`{wall_seconds, input_tokens, output_tokens, tool_calls}가 있다: 20/20. **문턱은 두지 않는다**(비용이 얼마인지 `[모름]`이므로 기록이 목적. 5세션 뒤 문턱 필요 여부를 운영자가 정한다, Q9) |
| P5 | 장전에 있다 | 버전 1이 08:30 KST 전에 커밋됨. **20/20세션**. 08:30은 기존 사전등록 H-admit의 결정 시각을 따른 값이다(`research/experiments/manifests/h-admit.json`의 "decided 08:30 KST of t+1"). NXT 프리마켓(08:00)·정규장(09:00) 대비 적절한지는 Q12 |
| P6 | 규칙 행 무결성 | 서버 재계산과 일치하지 않는 `risk_sell`·물타기 금지 행 0건 |

### 5.2 중단 (하나라도 해당하면 v0를 멈춘다)

- S1: 주문·제안·감시·위임장 등록이 계획 경로에서 한 번이라도 만들어짐(0건 허용). 즉시 중단·원인 보고.
- S2: ROB-501 위반 — `app/**`에 in-process LLM provider가 들어옴(정적 가드 `tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py`).
- S3: P6 위반 1건(규칙 행을 세션이 바꾼 것으로 확인) — 원인 수리 전 재개 금지.
- S4: P1·P2·P3 중 하나가 **5세션 연속** 실패.
- S5: pi 부하 기준 — load ≥ 2(4코어), I/O 대기 ≥ 10%, 가용 메모리 < 2 GB 중 하나가 **며칠** 이어지고 원인이 일일 계획 작업이면 멈추고 작업 위치를 옮긴다(`hk:doc consult/2026-11-20/at-pi-capacity-fable52` §3.4·§6, 용량 조치는 문제가 관측될 때). 부하 지표가 어디에 남는지는 `[모름]`(같은 문서 §5)이라 T7이 측정 위치를 정한다.
- S6: 20 세션이 끝났는데 P1~P6 중 하나라도 불충족이면 v0는 "미통과"다. 세션 슬롯 축소는 하지 않는다(v0 통과 뒤 별도 결정, `hk:doc decision/2026-11-20/at-direction-daily-plan-nonprice-go` 항목 4).

휴장·일시 중단(운영자 중지)은 세션 수에 세지 않는다. 시작일·끝일·달력(XKRX)은 봉인 때 고정하고 연장하지 않는다.

## 6. 규모와 주기

### 6.1 규모 [사실, 11-20 — `hk:doc consult/2026-11-20/at-direction-daily-plan-fable52` §3.4]

보유 KIS 42·Toss 40 국내 종목(양쪽 중복 가능), 장전 후보 약 123개. 한 버전의 행은 약 200. 후보 123개의 정확한 원천 합집합은 이 문서에서 확인하지 못했다 `[모름]` — 후보 집합은 입력 번들이 정의하고 해시로 고정한다(§7).

### 6.2 AT가 한 번 계산하고 세션은 바뀐 행과 상위 N만 본다

- AT는 premarket에 모든 행의 지표를 **한 번** 계산한다(완결 일봉만 읽는다. `analyze_stock_batch(quick=True)` 경로는 DB-only·외부 HTTP 0·배치당 DB 실행 상한 12회, `docs/agent-contracts/market-data-and-screener-contracts.md` analyze quick 절, 구현 `app/mcp_server/tooling/analysis_quick.py:734`). 일중에 지표를 다시 계산하지 않는다.
- **세션이 판단하는 집합 `J_t`** = 변경된 행 ∪ `c_rank` 상위 N'(= 20) 후보. 변경 플래그는 AT가 결정론으로 단다(닫힌 목록, 값은 정책 값):
  1. 규칙 판정이 어제와 다름(`risk_sell`·`rule_status`·물타기 금지 상태).
  2. 새 후보이거나 `candidate_set`에서 빠짐.
  3. `available_at`이 마지막 계획 이후인 사건이 그 심볼에 있음(공시·실적·뉴스, §7).
  4. 체결로 보유 수량이 변함.
  5. 지표가 정해진 띠를 넘음(예: 지지선 거리의 부호·분위 이동, RSI의 문턱 교차 — 띠는 봉인 때 고정).
  6. 이월 나이가 K=5세션에 도달함(영구 이월 방지).
- 안 바뀐 행은 `carried=true`로 어제 행을 가리킨다. 세션은 그 행을 다시 판단하지 않고, 등급은 이월 나이를 함께 보이며 K를 넘으면 `P_t`에서 빠진다(§4.2).
- 세션 비용은 `J_t` 크기에 비례한다 `[추정]`. 20세션 동안 `|J_t|`와 비용을 기록해 슬롯 축소 결정의 입력으로 쓴다.

### 6.3 체결·사건은 영향 종목만 재계산한다

- 체결 한 건(`execution_ledger` in-effect 행, `execution_ledger_in_effect()` 필터를 AND — 하드룰 5, #1175) 또는 사건 한 건은 **그 종목의 행**만 새 버전(`kind=fill|event`)으로 쓴다. 전 종목 재계산은 하지 않는다.
- 감시기가 있다면 상태를 메모리에 두고 상태가 바뀔 때만 DB에 쓴다. v0에는 감시기가 없다(일중 가격 트리거는 위임장 T7·별건, 규칙이 완결 일봉 종가를 쓰므로 일중에 판정이 바뀌지 않는다, `app/services/mechanical_loss_cut/rules.py:577-`의 `bars[-1].close`).
- 🔴 연구 계산은 pi 밖이다. 대조 비교(§4.3)는 행·outcomes를 내보낸 파일을 연구 호스트의 `research/` 하네스가 읽는다. pi는 지표 계산과 저장만 한다(`hk:doc consult/2026-11-20/at-pi-capacity-fable52` §3.1·§3.4).

## 7. 입력과 출처

모든 입력은 시각 네 가지(`event_at`·`first_observed_at`·`bundle_stored_at`·`bundle_closed_at`, §7.1)와 원천 버전을 함께 저장하고, `event_at ≤ asof`이면서 `first_observed_at ≤ bundle_closed_at`인 것만 번들에 넣는다. 번들은 불변이고 해시로 고정된다(§7.1). **모르는 값은 0이 아니다** — 행에 `unknown`과 닫힌 사유로 남고 §2.1·§3·실행 불가 사유로 이어진다.

| 입력 | 출처 | 비고 |
|---|---|---|
| 후보 | (a) 스크리너 스냅샷 프리셋 — `screen_stocks_snapshot`(DB-only, `app/mcp_server/tooling/screener_snapshot_tool.py`) 읽는 `invest_screener_snapshots`(`app/models/invest_screener_snapshot.py:22`) · (b) 필요 시 `screen_stocks` · (c) 후보 관측 로그 `screener_pick_log`(`app/models/screener_pick_log.py:120`) | 일 1회 스냅샷 빌드 복구는 hk 5752, 이력은 최근 5거래일만 보관(`hk:doc decision/2026-11-20/at-direction-daily-plan-nonprice-go` Q1). 후보 기록 로그는 마지막 행이 10-30이고 최근 20일 0행이다(hk 5622, 같은 consult §1) — 기록 재개 전에는 `candidate_call_id`가 null이고 행에 사유(`candidate_log_unavailable`)를 남긴다. 스냅샷이 빈 날은 **후보 0건이 아니라 head의 `gap_reason=candidate_source_unavailable`**이다(P2 판정에서 해당 세션은 "후보 집합 없음"으로 구분) |
| 보유 | 원장 FIFO(`load_held_lots`, `app/services/mechanical_loss_cut/positions.py:122`, `basis=inference`) + 브로커 보유와의 대조(`get_holdings(include_ledger_lots=True)`의 `ledger_state`, `app/services/execution_ledger/kis_lots.py:754,1031`) | 대조 불일치·`unknown`은 `position_unknown`. 외부(KIS 앱 등)에서 낸 미체결은 DB로 보이지 않는다(`external_orders_verifiable=false`)는 caveat를 행에 상수로 싣는다. 번들 생성은 DB만 읽고 브로커 호출을 하지 않는다. 브로커 대조는 장전 세션의 기존 `get_holdings` 호출 결과를 계좌별 `read_status`와 (`account_ref`, `symbol`)별 대조 분류(`matched`·`broker_only`·`quantity_mismatch`·`ledger_only`·`account_read_failed`, §1.6)로 기록한다. 분모는 이 브로커 스냅샷이다 |
| 미체결 주문 | `load_open_sell_quantities`(`app/services/mechanical_loss_cut/positions.py:233`), 보호 수량 `load_protected_quantities`(`app/services/mechanical_loss_cut/positions.py:311`), KIS `open_buy_evidence`/`open_sell_evidence`(`app/services/execution_ledger/kis_lots.py`) | 읽기 실패는 `open_order_unknown`/`protected_quantity_unknown`(0이 아님) |
| 현금 | `get_available_capital_impl`(`app/mcp_server/tooling/portfolio_cash.py:416`) | KIS 현금과 Toss 계좌 식별이 성과표의 남은 미측정 둘이다(`hk:doc consult/2026-11-20/at-direction-daily-plan-fable52` §1). 모르면 `cash_unknown` |
| 계좌 귀속 | `derive_account_ref`(`app/services/mandate/account_ref.py:19`) 규칙의 계좌 참조, Toss는 명시 `broker_account_id` 검증 | 못 정하면 `account_attribution_unknown`. 행에 계좌번호 원문은 저장하지 않는다 |
| 지표 | `analyze_stock_batch(quick=True)` 경로(`app/mcp_server/tooling/analysis_quick.py:734`), 지지·저항(`app/mcp_server/tooling/market_data_indicators.py:928`), 정지 의심(`app/services/halt_detection.py:233`) | 가격은 마지막 **마감** 일봉이며 라이브 시세가 아니다(quick의 `current_price`가 `stale`/`db_only_projection`으로 스탬프되는 계약) |
| 사건과 공개 시각 | `market_events`(`release_time_utc`, `app/models/market_events.py:81`), 투자자 수급(`investor_flow_snapshots`, 확정값은 18:10 KST 잡), 밸류에이션(`market_valuation_snapshots`, `computed_at`), 뉴스 관련성 파이프라인 | 사건의 `release_time`이 plan asof 이후이면 번들에 넣지 않는다. `computed_at`·`collected_at`이 실제 관측 시각으로 학습에 쓸 수 있는지는 `[모름]`(`hk:doc consult/2026-11-20/at-direction-daily-plan-fable52` §5) — 그래서 대조군 입력은 `available_at`을 행에 함께 기록한다 |
| 정책 | `get_trading_policy`/`config/trading_policy.yaml`(version, content_hash) | 모든 버전이 `policy_version`·`policy_content_hash`를 싣는다 |

### 7.1 불변 입력 번들 (H3)

해시가 재현을 보장하려면 입력이 불변으로 다시 읽히고 관측 시점이 검증돼야 한다. 시장 이벤트의 발표 시각과 생성 시각(`app/models/market_events.py:81,103`), 밸류에이션의 `computed_at`·`created_at`(`app/models/market_valuation_snapshot.py:64,67`), 수급의 `collected_at`(`app/models/investor_flow_snapshot.py:113`)은 같은 뜻이 아니다. 세 모델 모두 `updated_at`이 있어 행이 나중에 갱신될 수 있다.

**(1) 정체성.** 입력 번들은 `bundle_id`(UUID)와 `bundle_sha256`(아래 표준 직렬화 바이트의 SHA-256)을 가진다. 버전은 `input_bundle_id`와 `input_bundle_sha256`을 **둘 다** 저장한다. 번들 본문은 새 테이블 `review.daily_plan_input_bundles`(append-only: UPDATE·DELETE·TRUNCATE 거부, `at_app`에 SELECT·INSERT 조건부 GRANT, 하드룰 18)에 저장하고, 서버가 쓰기 때 본문을 다시 직렬화해 해시를 재계산해 일치하지 않으면 거부한다. 같은 (head, `bundle_sha256`)의 재쓰기는 기존 `bundle_id`를 돌려준다.

**(2) 표준 직렬화 `daily-plan-bundle/v1`.** UTF-8 JSON, 키 정렬, 공백 없음, `schema_version` 포함. 금액·가격·수량은 지수 없는 정확한 십진 문자열(후행 0 제거, `-0` 금지)이고 float는 `TypeError`다. 시각은 UTC RFC 3339 마이크로초 6자리 `Z`다. 행 배열은 (`row_kind`, `market`, `symbol`, `account_ref`) 순으로 정렬한다. 모르는 값은 키를 빼는 것이 아니라 `{"unavailable": "<닫힌 사유>"}` 객체로 명시한다. 같은 입력이 같은 바이트를 낸다는 골든 테스트와, `known_zero`(`{"known": "0"}`)와 `unavailable`이 다른 바이트를 낸다는 테스트를 T1에 둔다.

**(3) 입력마다 시각 네 가지를 따로 저장한다.**

| 필드 | 뜻 | 예 |
|---|---|---|
| `event_at` | 값이 가리키는 시장·발표 시각 | 시장 이벤트 `release_time_utc`, 일봉 세션 마감, 밸류에이션·수급의 `snapshot_date` 기준일 |
| `first_observed_at` | AT가 그 **값**을 처음 본(저장한) 시각 | 갱신되지 않은 행의 `created_at` |
| `bundle_stored_at` | 번들이 저장된 시각 | 서버 시계 |
| `bundle_closed_at` | 번들 마감 시각(= `asof`, 계획 cutoff 이전) | 버전 1은 D 08:30 KST 이전 |

- 원천과 버전 식별자도 입력마다 남긴다: `source_name`, `source_schema_version`, `revision_id`(원천 행 id + 값 해시).
- 🔴 **`created_at`은 값의 최초 관측 시각의 증거가 아니다.** 행이 upsert로 갱신되면 현재 값은 `updated_at` 이후에 관측된 값일 수 있다. `updated_at`이 `created_at`보다 늦거나(갱신 이력) 값의 이력이 없으면 그 값의 `first_observed_at`은 **증명되지 않았고**, 번들에서 `{"unavailable": "observed_time_unproven"}`이다. 갱신된 적 없는 행만 `created_at`을 `first_observed_at`으로 쓴다(허용 오차는 T2가 정한다). `collected_at`·`computed_at`이 실제 관측 시각인지는 `[모름]`이므로(`hk:doc consult/2026-11-20/at-direction-daily-plan-fable52` §5) 수집기 코드로 확인되기 전에는 `first_observed_at`의 근거로 쓰지 않는다.
- **포함 규칙**: 입력은 `event_at ≤ asof` **그리고** `first_observed_at ≤ bundle_closed_at`일 때만 번들에 들어간다. 공개 시각만 맞고 사후에 보강·정정된 행(`first_observed_at > bundle_closed_at`)은 그 번들에 넣지 않는다. 시점 증명이 없으면 `unknown`이다. cutoff 이후 도착한 자료는 그것을 처음 관측한 시점의 **새 번들**(이벤트·세션 버전)부터만 쓴다. 이미 저장된 번들은 고치지 않는다.
- **번들이 함께 고정하는 것**: 후보 집합과 키(원천 순위·`gate_features` 값 포함), 지표 값과 마스크(결측·`unavailable` 사유), `c_rank` 공식과 `control_spec` 해시, `E_t`·`P_t`와 `c_rank` 값, 브로커 보유 스냅샷(§1.6), 계획·가격·보호 수량·미체결 증거와 규칙 버전·정책 `version`/`content_hash`, 그리고 #5762의 `prior_decision`(cooldown·중복 판정 입력). 원천(`invest_screener_snapshots`는 최근 5거래일만 보관, hk 5752)이 지워져도 시험에 쓴 값은 번들에서 재현된다.
- **보존**: 번들은 에포크 전체와 마지막 평가 5세션, 그리고 보고서가 동결(`report_sha256`)될 때까지, 그 뒤에도 최소 180일 보존한다(권고값, 운영자 확인 Q18). 삭제는 운영자 결정 행으로만 한다.
- **08:30의 의미**: 08:30 KST는 계획 cutoff이며 "NXT 개장 전"이 아니다. NXT 프리마켓은 08:00에 열리므로 그 시각에 NXT 호가·체결이 이미 있을 수 있다. 번들 입력은 D−1 완결 일봉과 그 이전에 관측된 값뿐이고 D 일중 값은 쓰지 않는다. 대상 시장 세션(`session_date`, `calendar=XKRX`)·`cutoff_at`·`asof_at`(실제 번들 마감)을 head·번들에 저장한다.

### 7.2 `known_zero`와 `unavailable`: 위험 평가기의 skip 경로

`evaluate_lot`(`app/services/mechanical_loss_cut/rules.py:577`)의 모든 skip(`SKIP_REASONS`)은 입력을 알았는지로 나뉜다. 번들 필드는 이 구분을 값으로 가진다. "0으로 확인됨"과 "읽지 못함"은 다른 값이다.

| 분류 | `SKIP_REASONS` | 행 상태 |
|---|---|---|
| `not_applicable` | `parking_instrument` | `rule_status=not_applicable` |
| `unavailable` (입력을 몰라서 끝남) | `no_exit_plan` · `position_unreadable` · `price_missing`(봉 부족 포함) · `price_stale` · `halted_suspect` · `open_sell_unknown` · `protected_unknown` | `hold`+`rule_status=unknown`+같은 사유 |
| `evaluated` (알고 평가해 자르지 않음) | `not_underwater` · `above_planned_stop` · `floor_not_reached` · `rising` · `not_n_day_low` · `trailing_drop_not_reached` · `covered_by_open_sell` · `below_one_unit` · `already_cut_this_run` · `cooldown` | `rule_status=evaluated` |

입력 수량의 구분: 미체결 매도 수량(`open_sell_quantity`)과 보호 수량(`protected_quantity`)은 읽기에 성공해 합이 0이면 `{"known": "0"}`, 읽기에 실패했거나 판독 불가·음수이면 `{"unavailable": "open_sell_unknown"|"protected_unknown"}`다(평가기는 `None`·음수를 각각 `open_sell_unknown`·`protected_unknown`으로 처리한다, `app/services/mechanical_loss_cut/rules.py:660-663`). `unavailable`을 0으로 직렬화하는 경로는 없다.

- 🔴 **unknown은 실행 가능한 행으로 표시되지 않는다.** 필수 입력(포지션·가격·미체결 매도·보호 수량·평단)이 `unavailable`인 행의 `trim`·`add`·`risk_sell`은 `not_executable_reason`이 비어 있을 수 없다. 서버 검증기는 이 위반을 거부한다.
- T1/T2 테스트: 필수 입력을 하나씩 `unavailable`로 바꾸는 표 구동 테스트에서 `risk_sell`이 나오지 않고 `trim`·`add`에 실행 불가 사유가 붙는다. 18개 skip 사유 전부가 위 표 세 분류 중 정확히 하나에 속한다(`SKIP_REASONS`와의 집합 일치 테스트, 새 사유가 추가되면 분류가 없어 실패한다).

## 8. 연결

- **3788 매수 동결은 유지한다.** 이 문서는 동결을 읽거나 풀지 않는다. v0의 모든 행은 `mode='plan_only'`다. 위임장 쪽의 `buy_freeze_state`는 hk 4900이 측정을 공급하기 전까지 모든 계좌를 막는 fail-closed 스텁이다(`app/services/mandate/buy_freeze.py:26-36`) — 계획은 이 스텁도 건드리지 않는다.
- **위임장 자동 등록 없음.** 매수 행은 §1.4 어휘를 쓰지만 `mandate_plans`·승인 요청·묶음 카드를 만들지 않는다. 승격(후속)은 운영자가 행의 네 필드를 복사해 `scripts/mandate.py register-plan`으로 등록하는 절차다(`docs/design/3795-mandate.md` §3.2·§5.1).
- **Go 실행기 없음.** 이 문서가 낳는 코드는 Python이다.
- **기각 매수 행은 #2501의 형식을 따라 5624 모집단을 채운다.** 채택된 `new`/`add` 행마다(하루 `max_adopt` 개, 기본 10, Q2) 세션이 기존 `forecast_save`(필수 인자 `probability`·`review_date` 포함)를 `decision_bucket='deferred_no_action'`, `declined_limit_price`, `declined_quantity` 또는 `declined_notional`, 선택 `declined_quote_price`+`declined_quote_asof`·`declined_account_mode`와 함께 호출한다. 이 호출이 `decision_kind='declined'` 판단 행(BUY rung 0 + `limit` 참조가)을 쓰므로(`app/services/decision_evidence.py:169`, 5624 짝 모집단 `docs/design/5624-entry-ab-mock.md` §2) 계획 행은 그 판단·예측을 **가리키기만** 한다(행이 판단을 복제하지 않는다). 🔴 단, `forecast_save` 응답에는 `decision_id`가 없다: 응답은 `{success, action, data(예측 행), warnings}`뿐이고(`app/mcp_server/tooling/forecast_tools.py:154-161`) `_record_declined_evidence`는 `DecisionEvidenceService.record`가 돌려준 `decision_id`를 버린다(`app/mcp_server/tooling/forecast_tools.py:189-234`, `app/services/decision_evidence.py:246-277`). 판단 행은 `forecast_ids=(forecast_id,)`를 가지므로(`app/mcp_server/tooling/forecast_tools.py:179`) 계획 행은 응답의 `forecast_id`를 저장하고 `decision_id`는 `decision_evidence.forecast_ids`로 **해석**한다(T1이 해석 읽기를 포함하거나 `forecast_save` 응답에 `decision_id`를 더하는 작은 변경을 함께 한다, Q15). 🔴 주문이 아니다: 판단 행은 `decision_order_links`를 만들지 않고 retro는 계획 필드만 채우며 주문·체결로 세지 않는다(`docs/agent-contracts/market-data-and-screener-contracts.md` hk 5719 절). 기록 이전은 소급하지 않는다. KR만이므로 5624의 US 다리는 이 문서가 채우지 않는다.
- **retro/v1·Factortrail이 계획 버전을 읽는 법(후속 T6)**: 호환 확장으로 읽기 전용 operation `retro_daily_plan`을 더한다(`contracts/retro/v1/openapi.yaml`, 기존 `retro_decisions`의 모양을 따른다 — `app/services/retro_v1/retro_decisions.py:1317`, 라우트 `app/routers/retro_v1.py:25,37`). 규칙: reader 자기 `REPEATABLE READ READ ONLY` 트랜잭션(8초), 모르는 값은 null+닫힌 사유(0으로 만들지 않음), 불투명 해시 ref, 계좌번호·원문 payload 비노출, 금액은 decimal 문자열, 호환 확장 → 소비자 커밋 → `consumers.lock` 핀 갱신의 순서(`docs/runbooks/retro-consumer-pin.md`, `contracts/retro/v1/consumers.lock`). 행의 `decision_id`는 `retro_decisions`의 `decision_ref`로 이어진다. 계획 읽기는 v0 시작의 선행 조건이 아니다.
- **하드룰 점검**: 스케줄러 등록 없음(하드룰 6 — 일일 트리거는 v0에서 기존 라이브 슬롯의 세션이 MCP 도구를 호출, Dagu 등록은 별도 운영자 승인, Q6). 게이트 완화 없음(새 `DAILY_PLAN_ENABLED` 기본 false, 하드룰 4 정신). 레저 직접 쓰기 없음(하드룰 5, `execution_ledger_in_effect()` AND). 새 MCP 도구는 레인 allowlist 계약 테스트와 live 프롬프트 핀 테스트를 통과해야 한다(하드룰 17: `config/mcp_lane_allowlists/`, `tests/mcp_server/test_lane_allowlist_contract.py`, `tests/mcp_server/test_profile_tool_snapshot.py`). 새 테이블은 객체별 `at_app` GRANT(하드룰 18).

### 8.1 계획 버전과 `forecast_save`: 두 번 쓰기의 실패 계약 (H2)

사실(코드): `forecast_save`는 자기 세션에서 예측 행과 기각 판단 행(`declined_*`가 있을 때)을 한 번에 커밋한다(`app/mcp_server/tooling/forecast_tools.py:98-153`). 그 둘은 원자적이지만 **계획 버전과는 원자적이지 않다** — 계획 저장소와 `forecast_save`는 서로 다른 트랜잭션이다. 그래서 타임아웃·재실행에서 "예측은 만들었는데 계획 행이 못 가리킴"이나 중복이 생길 수 있다. 응답에 `decision_id`가 없고(§8), 이미 있는 예측에 `declined_*`를 다시 주면 `declined_not_created`로 롤백된다(`app/mcp_server/tooling/forecast_tools.py:125-139`). 판단 행의 `decision_id`는 호출마다 새 `uuid4()`이고 `forecast_ids`에 유일 제약이 없다(`app/services/decision_evidence.py:259`). 따라서 `forecast_id` 역조회만으로는 해결되지 않고, 아래 준비·확정 계약이 필요하다. 이 계약은 T1이 구현한다.

**(1) 채택 단위 멱등 키.** 채택된 행마다 `adoption_key = sha256(canonical(head_id, row_key, input_bundle_sha256, source='forecast_save'))`를 만든다. 내용(확률·`review_date`·`declined_*`·계좌)은 키에 넣지 않고 별도 `adoption_content_sha256`로 저장한다. 키와 내용을 분리하는 이유는 같은 키에 다른 내용이 오는 것을 **충돌로 감지**하기 위해서다.
- `forecast_id = uuid5(PLAN_NAMESPACE, adoption_key)`로 결정적으로 만들어 `forecast_save(forecast_id=...)`에 넘긴다(인자가 이미 있다, `app/mcp_server/tooling/forecast_tools.py:53`, `app/services/trade_journal/forecast_service.py:765`). `correlation_id = adoption_key`도 함께 넘겨 계획 행 없이 남은 고아 예측도 키로 찾을 수 있게 한다.
- 같은 키 + 같은 내용: 기존 결과를 그대로 돌려준다(재시도). 같은 키 + 다른 내용: `adoption_conflict`로 거부하고 아무것도 쓰지 않는다. DB는 (`adoption_key`) 유일이고 서비스가 내용 해시를 비교한다.

**(2) 직렬화.** 한 head의 쓰기는 head 키의 advisory lock 아래 한 트랜잭션으로 직렬화하고, 버전 순번 (`head_id`, `seq`)은 유일하다. 쓰기 요청은 `expected_prev_seq`를 싣고 어긋나면 `stale_version`으로 거부한다(덮어쓰지 않는다). 버전 쓰기 자체도 클라이언트 `idempotency_key`를 가지며 같은 키의 재시도는 같은 버전 id를 돌려준다.

**(3) 준비 → 호출 → 확정.** 한 트랜잭션으로 묶을 수 없으므로 outbox 상태를 둔다.

| 단계 | 하는 일 | 상태 |
|---|---|---|
| 준비(P) | 버전 쓰기 트랜잭션이 채택 행을 `forecast_link_state=pending`, 미리 계산한 `forecast_id`·`adoption_key`·`adoption_content_sha256`과 함께 커밋한다 | `pending` |
| 호출(F) | 세션이 `forecast_save(forecast_id=…, correlation_id=adoption_key, declined_*)`를 호출한다. **재시도 전에 읽는다**: 그 `forecast_id`가 이미 있으면 다시 쓰지 않고 확정 단계로 간다(없을 때만 호출) | — |
| 확정(C) | 확정 읽기가 존재하는 예측·판단 행을 (4)의 검증으로 대조하고 결과를 연결 사실으로 **추가**한다 | `confirmed` \| `link_missing` \| `link_ambiguous` \| `link_mismatch` |

- 계획 행은 불변이므로 확정 결과는 append-only 연결 사실 행(`daily_plan_row_links`, 제안)이다. 행의 현재 연결 상태는 가장 늦은 연결 사실이다. 같은 연결 사실의 재쓰기는 멱등이다.
- 한 버전은 채택 행이 전부 `confirmed`일 때만 `link_complete`다. 하나라도 `pending`·`link_*`이면 버전 머리에 `link_incomplete`와 미해결 행 목록이 남고, 보고서·P2·점수 비교는 `link_incomplete` 버전의 채택을 **완성본과 구별**해 센다(0으로 세지도 완료로 세지도 않는다). 미해결 `pending`이 다음 premarket 전까지 남으면 `link_missing`으로 확정 기록하고 P2 실패 사유로 센다.
- `forecast_save`가 `declined_not_created`를 돌려주면 이는 "이미 만들어짐" 신호이므로 실패가 아니라 확정(C)으로 간다. 예측을 `declined_*` 없이 다시 호출해 기존 예측을 덮어쓰는 복구는 허용하지 않는다(확률이 바뀐다).

**(4) 기존 예측·판단 검증.** 확정 읽기는 `forecast_id`로 예측 행을 읽고 `decision_evidence.forecast_ids`에 그 id를 가진 `decision_kind='declined'` 판단 행을 센다.

| 읽기 결과 | 판정 |
|---|---|
| 예측 0 | `pending`(아직 호출 전이거나 실패). 재시도 가능 |
| 예측 1, 판단 0 | `link_missing`: 판단 없이 만들어진 예측이다. 자동 복구 쓰기 없음, 운영자가 따로 정리하고 새 `adoption_key`(새 번들·행)로만 다시 한다 |
| 예측 1, 판단 1, 속성 일치 | `confirmed`, `decision_id` 저장 |
| 예측 1, 판단 ≥ 2 | `link_ambiguous`: 하나로 정하지 않고 보고한다 |
| 예측 1, 판단 1, 속성 불일치 | `link_mismatch` |

속성 일치는 시장(`instrument_type`)·정규화 심볼·`side=buy`·`decision_bucket=deferred_no_action`·`declined_limit_price`·수량/금액(정확한 십진 비교)·`declined_account_mode`와 `would_be_account_ref`의 매핑·`correlation_id = adoption_key`다. 확정 읽기는 예측·판단 행을 **읽기만 하고** 어떤 쪽에도 쓰지 않는다. 승인되지 않은 수동 수리나 단순 조회가 쓰기를 흉내 내지 않는다 — 수리된 상태는 확정 읽기를 다시 통과해야 `confirmed`가 된다.

**(5) 테스트(T1).** 같은 키 재시도가 같은 결과·같은 `forecast_id`, 같은 키+다른 내용 충돌, 동시 두 쓰기의 직렬화(둘 다 성공하거나 하나가 `stale_version`), 호출 직전·직후 프로세스 중단 후 재시도가 중복 예측을 만들지 않음, `declined_not_created`가 확정으로 이어짐, 판단 0/1/여러 개·속성 불일치 각각의 판정, `link_incomplete` 버전이 완성본으로 세어지지 않음.

### 8.2 계획 경로는 제안·주문·감시·위임장을 만들 수 없다 (H2)

v0는 `mode='plan_only'`다. 이 불변식을 allowlist 하나가 아니라 세 겹으로 검증한다.

1. **서비스 경계(정적)**: `app/services/daily_plan/`의 import 폐쇄에는 주문·제안·감시·위임장 writer·브로커 모듈이 없다(`app/services/order_proposals/`, `app/services/mandate/store.py` 등의 쓰기 표면, `investment_watch_*`, 브로커 클라이언트). `tests/services/mandate/test_static_boundary.py`와 같은 정적 테스트(직접 import·전이 폐쇄·쓰기 호출 문자열)를 둔다. `watch`는 분류일 뿐이며 가정 매도(`trim`·`risk_sell`)는 `execution_state='plan_only'`만 갖는다(CHECK 상수, 주문·제안 id를 가리키는 FK·열이 없다).
2. **DB 쓰기 권한**: 계획 쓰기는 전용 역할 `at_daily_plan_writer`(plan 테이블에만 INSERT, 번들·증거 읽기 테이블에 SELECT)로 `SET LOCAL ROLE`을 건 트랜잭션 안에서만 돈다. 그 역할은 `order_proposals`·`order_proposal_rungs`·감시·`mandate_*`에 쓰기 권한이 없다. 역할이 없는 개발 DB를 위해 GRANT는 조건부로 쓰되(하드룰 18), CI의 run-owned 테스트 DB에서는 테스트가 역할을 만들고 위 테이블에 INSERT를 시도해 `permission denied`가 나는지, plan 테이블 INSERT는 되는지 확인한다(권한 테스트가 조용히 skip되면 실패). 이 방식이 운영 역할 구조(#789)와 맞는지는 Q17에서 확인한다.
3. **부작용 0 회귀(동작)**: 계획 서비스·MCP 도구의 모든 쓰기 경로를 실행한 뒤 `order_proposals`·`order_proposal_rungs`·`order_send_intents`·감시·`mandate_*`·브로커 호출 스파이의 행 수·호출 수가 0 증가임을 DB 테스트(CI)가 확인한다. 중단 조건 S1(§5.2)은 이 테스트의 운영 측 거울이다.

`forecast_save`는 기존 도구이고 계획 경로가 아니라 세션이 직접 부른다. 이 도구가 쓰는 것은 예측·기각 판단 행뿐이며(`decision_order_links`를 만들지 않는다) 계획 서비스는 그 행을 읽기만 한다.

## 9. 구현 티켓과 열린 질문

### 9.1 티켓 (각각 PR 하나)

| # | 티켓 | 내용 | 선행(blocking) |
|---|---|---|---|
| T0 | 정책 키 operator PR | `config/trading_policy.yaml`에 `daily_plan`(`no_add_floor_pct: -8`, `max_adopt`, `judged_top_n`, `carry_max_sessions`, 변경 플래그 띠) **그리고 스키마 변경**: 정책 문서 모델이 `extra="forbid"`(`app/schemas/trading_policy.py:2335-2336`)라 새 최상위 키는 스키마에 필드를 더해야 로드된다(`mechanical_loss_cut`이 그 선례, `app/schemas/trading_policy.py:2362`). 키를 더하면 정책 `content_hash`가 바뀌므로 정책을 인용하는 기록은 새 해시를 따른다 — operator PR로만 | 없음 |
| T1 | 저장소 + MCP 쓰기·읽기 + 검증기 | 마이그레이션(네 테이블·append-only 트리거·닫힌 어휘 CHECK·`at_app` GRANT), 모델, 어휘 모듈, `app/services/daily_plan/` 저장소·검증기(형식·한도·정확한 decimal·`would_be` 크기 XOR·**규칙 행 서버 재계산 일치 검사**·§2·§3 어휘), MCP `daily_plan_write_version`/`daily_plan_get`/`daily_plan_list`(`DAILY_PLAN_ENABLED` 기본 false). 매수 행 형식 검증이 `decision_table_validate` 재사용 가능한지 스파이크(Q8) | T0 |
| T2 | 장전 입력 번들 | 후보 집합 빌더(해시 고정)·보유 lot 로드·**읽기 전용 위험 평가기**(§3.1)·물타기 금지 판정·지표·`c_rank`·변경 플래그·`J_t`·이월·결측 사유. 번들 생성은 DB-only·브로커 호출 없음 | T1(어휘·타입). 소프트: hk 5752(스냅샷 일 빌드), hk 5622(후보 로그) |
| T3 | 체결·사건 재계산 | 영향 종목 버전 쓰기, fold 재사용(위임장에 묶인 종목은 `fold_plan_events`+`project` 결과를 읽음) | T1, T2 |
| T4 | 운영자 레포 장전 프롬프트 | 세션이 번들을 읽고 등급·논지·무효 조건·처분 사유를 쓰고 채택 행마다 `forecast_save(declined_*)`를 호출. 🔴 `forecast_save`는 `probability`와 `review_date`가 필수 인자이므로(`app/mcp_server/tooling/forecast_tools.py:44-50`) 채택 행마다 세션이 확률 예측도 함께 써야 한다 — 이 예측은 점수 대조(§4)와 별개의 기록이다. **이 레포 밖**(`auto_trader-operator`) | T1, T2 |
| T5 | 마감 채점 + 대조 비교 하네스 | `outcomes` 쓰기(마감 사실·성숙 라벨), `research/daily_plan_v0/`의 S·C·E 비교(연구 호스트에서 실행, pi 아님), `kind=close` 버전. 사람 없이 끝남(P3) | T1 |
| T6 | retro 읽기 | `retro_daily_plan` operation + 계약 + 소비자 게이트 통과 | T1 (v0 시작에는 불필요) |
| T7 | 봉인·등록 | manifest 봉인(dataset·code sha·policy·비용표·대조 지표 목록·시작·끝 날짜·달력), 레지스트리 `draft → sealed → running`, heartbeat epoch 등록, pi 부하 측정 위치 결정(S5) | T1~T5 |

의존 간선: T0→T1→{T2, T5}; T2→T3; {T1,T2}→T4; T1→T6; {T1..T5}→T7. T6은 T7과 무관하다.

### 9.2 열린 질문 (권고 답)

1. **등급 축과 가중** — `value`·`quality` 두 축, 세 단계, 가중 1:1, 에포크 중 고정, 변경은 새 manifest 버전. 권고: 그대로.
2. **N과 변수** — 비교 N=10, `J_t`의 `c_rank` 상위 N'=20, 이월 K=5세션, 하루 채택 `max_adopt`=10, `c_rank` 지표 다섯과 부호(§4.2). 권고: 이 값으로 사전등록하고 튜닝하지 않는다. 결측은 지표를 평균에서 빼고 개수를 기록.
3. **효과 문턱·표본 문턱** — `delta_min` +0.25%p, 성숙 세션 ≥15/20, 판정 불가 ≤10%(5624 재사용). 권고: 같은 값(서로 다른 실험의 결과를 같은 기준으로 읽기 위해).
4. **위험 판정 읽기 경로** — (a) 스위치를 켜 shadow를 수동 실행 vs (b) 같은 순수 함수의 읽기 전용 평가기(§3.1). 권고: (b). 스위치 기본 off 유지, backfill 쓰기 회피, lot별 skip 사유 확보.
5. **−8% 상수의 위치** — 정책 YAML `daily_plan.no_add_floor_pct`(operator PR), KR만. 권고: 그대로. 미국·코인으로의 확대는 별도 증거 뒤.
6. **일일 트리거** — 입력 번들 생성(premarket)과 마감 채점을 누가 부르는가: v0는 기존 라이브 슬롯의 세션이 MCP 도구를 부르고, Dagu 등록은 도구가 준비된 뒤 따로 승인. 권고: 그대로(스케줄 등록은 운영자 승인 사항).
7. **행 키** — KIS·Toss가 같은 종목을 가지면 계좌별 행 둘. 권고: 계좌별(손절 판정·물타기 금지가 lot 단위).
8. **매수 행 형식 검증** — `decision_table_validate`를 합성 표로 재사용 vs 얇은 계획 검증기. 권고: T1 스파이크로 합성이 닫힌 문법에 맞는지 먼저 확인하고, 안 맞으면 호가 단위(`_validate_krx_grid`)·최소 주문 금액 검사만 공용 함수로 뽑아 쓴다. 이 결정은 T1 PR에서 한다.
9. **세션 비용 문턱** — v0는 기록만(P4). 권고: 5세션 뒤 운영자가 문턱 필요 여부를 정한다.
10. **후보 집합의 정의** — 후보 약 123개의 원천 합집합과 프리셋 목록을 확정해야 한다 `[모름]`. 권고: T2에서 프리셋별 상한과 합집합 규칙을 정해 `candidate_set_sha256`으로 고정하고 T7 봉인에 포함.
11. **마감 채점 시각의 완결성** — 일봉이 늦게 들어오는 날을 "채점 불가"로 둘지 재시도할지. 권고: 다음 premarket 전까지 재시도 1회, 그래도 없으면 해당 행 outcomes=`unknown`(0 금지)이며 P3는 그 날을 실패로 센다.
12. **계획 마감 시각** — v0는 08:30 KST로 둔다(H-admit의 결정 시각 선례). NXT 프리마켓 08:00 구간과 정규장 09:00 사이 어디가 맞는지는 라이브 슬롯 시각에 달렸다 `[모름]`. 권고: 08:30 유지, 슬롯 시각 확인 뒤 T7 봉인 때 확정.
13. **결손일 허용 폭** — P1~P5를 20/20으로 적었다. 외부 입력 장애가 하루 있어도 v0 통과를 막을지. 권고: P1·P2·P6는 허용 0(자동 규칙 필드), P3·P5는 원인이 기록된 장애일 1일까지 허용하되 허용일은 보고서에 따로 표시하고 5.2 S4(5세션 연속 실패)는 그대로.
14. **동점 처리** — N=10 경계의 동점을 분할 가중으로 처리(§4.1). 권고: 그대로(구성원을 임의 규칙으로 정하지 않기 위해). 대안은 `c_rank`로 깨는 것이나 S와 C가 섞인다.
15. **판단 id 해석** — `forecast_save`가 `decision_id`를 돌려주지 않는다(§8). (a) 계획 행이 `forecast_id`만 저장하고 `decision_evidence.forecast_ids`로 해석 vs (b) `forecast_save` 응답에 `decision_id`를 더함. 권고: (a). `forecast_save`의 응답 계약을 건드리지 않고 읽기에서 해결한다.

## 10. 근거 핀 (`auto_trader@bdd4c776b`)

| 항목 | 위치 |
|---|---|
| 위임장 계획 이벤트 fold | `app/services/mandate/fold.py:35,95,160,557` |
| T4b 투영 규칙 | `app/services/mandate/projection_rules.py:237,335` |
| 계획 등록 입력·한도 | `app/services/mandate/store.py:1012-1027,1257-1264` |
| 계획 모델 | `app/models/mandate.py:357,429,460` |
| 계정 참조 | `app/services/mandate/account_ref.py:19` |
| 매수 동결 스텁(hk 4900) | `app/services/mandate/buy_freeze.py:26-36` |
| 기계식 손절 순수 규칙 | `app/services/mechanical_loss_cut/rules.py:34-55,107-110,232,376-388,577-725` |
| 평가 잡(쓰기 포함) | `app/services/mechanical_loss_cut/job.py:165-295,318-345` |
| shadow 행 조회 | `app/services/mechanical_loss_cut/store.py:329` |
| lot·미체결·보호 수량 로더 | `app/services/mechanical_loss_cut/positions.py:122,233,311` |
| 모델(계획·판정) | `app/models/mechanical_loss_cut.py:36,106` |
| 정책(잠정) | `config/trading_policy.yaml:1842-1857` · 비용 `app/schemas/trading_policy.py:1782-1784` |
| 스위치 기본 off | `app/core/config.py:1145-1149` |
| scheduleless 태스크 | `app/tasks/mechanical_loss_cut_tasks.py:20` |
| 후보 로그 모델·게이트 | `app/models/screener_pick_log.py:120-156`, `app/services/screener_pick_log.py:79`, `app/core/config.py:1436` |
| 판단 근거 | `app/services/decision_evidence.py:21,47,56,118,139,169` |
| 예측·기각 버킷 | `app/models/review.py:1747,1832`, `app/models/decision_vocabulary.py:26` |
| `forecast_save` 시그니처·기각 입력·응답·판단 기록 | `app/mcp_server/tooling/forecast_tools.py:44-50,73,154-161,179,189-234`, `app/services/decision_evidence.py:246-277` |
| 정책 스키마(`extra="forbid"`) | `app/schemas/trading_policy.py:2335-2336,2362` |
| H-admit 결정 시각 선례 | `research/experiments/manifests/h-admit.json` (features `available_at`: "decided 08:30 KST of t+1") |
| 스냅샷 모델 | `app/models/invest_screener_snapshot.py:22,84,93` |
| quick 지표·RSI·지지 | `app/mcp_server/tooling/analysis_quick.py:734`, `app/mcp_server/tooling/market_data_indicators.py:459,928` |
| 정지 의심 | `app/services/halt_detection.py:184,233` |
| 거래 제한 표시 | `app/mcp_server/tooling/buy_candidate_fanout.py:556` |
| 밸류에이션·수급 | `app/models/market_valuation_snapshot.py:52`, `app/models/investor_flow_snapshot.py:67` |
| 사건 공개 시각 | `app/models/market_events.py:81` |
| 섹터 | `app/models/symbol_sectors.py:11` |
| 현금 | `app/mcp_server/tooling/portfolio_cash.py:416` |
| KIS lot 증거 | `app/services/execution_ledger/kis_lots.py:754,1031` |
| 결정표 검증 | `app/services/decision_table_validate/validator.py:78` |
| retro 판단 읽기 | `app/services/retro_v1/retro_decisions.py:1317`, `app/routers/retro_v1.py:25,37`, `contracts/retro/v1/openapi.yaml:650` |
| ROB-1458 증거 | `docs/research/rob-1458-strategy-evidence-2026-11-02.md:35,36,37,38,74` |
| 위임장 설계 | `docs/design/3795-mandate.md` §3.2(`:183`)·§4.2(`:536`)·§5.1(`:998`)·§8(`:1201`) |
| 진입 방식 A/B 설계 | `docs/design/5624-entry-ab-mock.md` §2–§3 |
| 실험 레지스트리 규칙 | `research/experiments/README.md`, `scripts/ci/experiment_registry_lint.py` |
