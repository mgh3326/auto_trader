# #3795 — 위임장(mandate): 바구니별 위임장 버전 승인과 계획 결속 자동승인 설계

hk 4881이 요구한 설계 문서이고, 상위 작업은 hk 3795다.
기준 SHA는 `742d8a772`(origin/main)이고, 이 문서의 모든 `path:line` 인용은 `auto_trader@742d8a772` 기준이다. 이 문서는 설계 문서이며 코드·마이그레이션·정책 YAML·스케줄러는 바뀌지 않는다.

표기: **[사실]** = 그 SHA의 코드·문서에서 확인한 것, **[추정]** = 확인하지 않은 추론, **[의견]** = 이 문서의 권고.

운영자 결정(원문은 hk doc):

- `decision/2026-11-10/at-new-strategy-go` §1:
  - 1 위임장 방식 채택. 운영자는 바구니별 위임장 버전을 승인하고, 그 안의 주문은 자동 집행 + 알림.
  - 1a 새 종목의 분할 계획은 하루 1회 장 시작 전 묶음 승인.
  - 1b 논지 훼손 손실 매도 허용. 처음 4주는 건별 승인, 이후 재검토.
  - 1c 그 밖에 사람 승인이 남는 경우: 위임장 변경, 한도 초과, 모의 → 실전 승격.
  - 2·2a ① 가치+물타기 신규 1,950만 / 기존 보유 물타기 585만 / 실전 실험 195만 / 검증 통과 전략 몫 585만 / 미배정 585만.
  - 2b ① 분할 규칙: 종목당 계획 금액 약 235만(8종목), 3회 분할, 1차가 대비 −15%·−30%, 분할 간 20거래일 이상, 업종 상한 35%.
  - 2c 한도: ① 바구니 낙폭 −25%, 전체 자산 낙폭 −25%(도달 시 신규·추가매수 동결, 강제 매도 아님), 실험 월 손실 한도 100만.
  - 3 기존 보유는 분류 승인 전까지 KR·US 신규 매수 제안과 기존 물타기 티어(`buy.underwater_support_net` 등)를 끈다.
  - 4 현금 규칙 B(남은 분할 몫 2차 100%·3차 50% + 4주 신규 진입 예산을 대기금, 나머지 파킹).
  - §3: 이 결정은 주문·배포·스케줄 등록의 건별 승인을 대신하지 않는다. "계획 행에 결속된 자동승인"은 자동승인 범위를 시장가 근접 매수까지 넓히는 **게이트 변경**이고, 구현 PR은 deviation record와 함께 올리며 활성화는 따로 승인받는다(하드룰 4). 실전 카나리는 각 위임장 버전을 운영자가 승인한 뒤다.
  - 수정 3: KIS와 Toss 둘 다 새 전략에 쓴다. 한 종목의 분할 계획은 한 계좌에만 둔다.
- `decision/2026-11-16/at-strategy-direction`: 위임장 설계(3795)를 지금 시작한다. Upbit 소액 실험은 3795 없이 건별 승인 카드로 열고, 자동 집행은 3795 이후 별도 승인. 자동승인 범위 확대는 deviation record + 별도 활성화 승인.
- `report/2026-11-12/at-3792-holdings-classification`: 바구니 ① 편입 4종목 — 삼성전자(005930, 계획은 Toss), 현대차(005380, Toss), 기아(000270, KIS), 우리금융지주(316140, KIS). 승인 요청 4: 물타기 티어는 계속 끄고 KR·US 신규 매수는 바구니 ① 계획으로만 연다.
- `consult/2026-11-10/at-strategy-fable52` r2 §10: 위임장 내용(대상 종목 조건, 진입 규칙, 종목별 분할 계획, 주문 방식 = 매도 1호가 +N bp 이내, 주문당·하루·바구니 한도, 손실 한도, 만료일)과 구현 경로(sleeve 버전 + 종목별 분할 계획 테이블 + 계획 결속 자동승인). §2.1: 1차는 매도 1호가 +0.3% 이내 지정가, 2·3차는 가격 트리거 → 근접가 주문, 논지 훼손이 이 바구니의 유일한 손실 매도.

## 1. 목적·범위·지키는 하드룰

### 1.1 목적

구현 티켓이 다시 결정하지 않고 따라갈 수 있게 다음을 정한다.

1. 위임장의 데이터 모델: sleeve 버전에 무엇을 더하고, 종목별 분할 계획을 어떤 테이블·상태 전이·DB 가드로 두는가.
2. 계획 결속 자동승인: 어떤 제안이 어떤 조건에서 사람 카드 없이 나가는가, 기존 자동승인 게이트 각각과의 관계, 완화되는 게이트 전부의 목록과 deviation record.
3. 사람 승인이 남는 흐름(1a·1b·1c)과 낙폭 동결(2c).
4. 알림·관측과 실패 시 동작.
5. 운영자 결정 질문(번호·권고안)과 티켓 분해.

### 1.2 범위 밖

- 실주문 활성화(어떤 env 스위치도 켜지 않는다), 마이그레이션 작성·적용, 스케줄러 등록, 정책 YAML 변경, 위임장·계획 행의 실제 생성.
- 종목 선정 규칙 자체(밸류에이션 분위 스크린, 논지 메모 작성)와 ① 백테스트(hk 3793). 이 문서는 "승인된 계획이 있을 때 그 주문을 어떻게 내보내는가"만 다룬다.
- 가격 트리거 감시 수단(fillwire 가격 트리거)의 구현. 이 문서는 트리거가 **제안을 만든다**는 인터페이스만 정한다(§4.2).
- Upbit 소액 실험(2026-11-16 결정 1)은 건별 승인 카드로 이미 열린다. 그 자동 집행은 이 설계의 바구니 형식을 그대로 쓰되 활성화는 별도다(§7 Q9).

### 1.3 지키는 하드룰

| 규칙 | 이 설계의 처리 |
|---|---|
| 하드룰 4 — 브로커 실행 표면은 default-disabled, 게이트 완화 금지 | 계획 결속 자동승인은 **게이트 완화다**. 새 스위치 `MANDATE_AUTO_APPROVE_ENABLED`(기본 false) + 시장 allowlist + 계좌 allowlist + `ORDER_PROPOSALS_AUTO_APPROVE_MODE=expanded` + 위임장 버전의 `state='live'` 가 모두 참일 때만 열린다. 완화 목록 전부를 §4.4에 적고 구현 PR에 deviation record를 붙인다. 레버리지·per-order cap·daily cap 상수는 올리지 않는다. 운영자 결정 없이 활성화하지 않는다. |
| 하드룰 6 — 스케줄러 등록 금지 | 아침 묶음 승인 카드 발송·트리거 감시·낙폭 계산은 전부 scheduleless CLI/수동 레버로 출고한다. 정기 실행이 필요하면 별도 승인(§7 Q8). |
| 하드룰 19 — 기계식 손절(ROB-1460) | 논지 훼손 손실 매도(1b)는 ROB-1460과 **다른 부류**다. ROB-1460의 결정 행 결속 경로·스위치·캡을 재사용하거나 넓히지 않는다. 1b는 처음 4주 동안 사람 카드다(§5.2). ROB-1460 스위치는 기본 off 그대로. |
| 하드룰 18 — #789 DB 역할 | 새 테이블마다 같은 마이그레이션에 조건부 `at_app` GRANT(SELECT·INSERT만, UPDATE·DELETE 없음). ROB-1470 선례 `alembic/versions/20261102_rob1470_strategy_sleeves.py:329-332`. |
| 하드룰 5 — 레저 쓰기는 서비스 경유 | 계획 상태의 "체결" 전이는 `execution_ledger_in_effect()` 의 authoritative 체결 증거로만 한다. 계획 테이블은 원장이 아니며 원장에 쓰지 않는다. |
| 하드룰 9 — 검증 규율 | 각 티켓은 변경 범위 테스트를 돌리고 DB 트리거·경쟁 테스트는 CI가 증거다. |

### 1.4 용어

- **바구니**: 전략 하나의 돈 묶음. 구현 단위는 ROB-1470 sleeve 하나(계좌 × 시장 × 통화)다. 바구니 ①이 Toss KR과 KIS KR에 걸치면 sleeve가 둘이다. 그 둘을 묶는 것이 조항의 `basket_key`이고, 바구니 한도(낙폭 동결)는 같은 `basket_key`의 모든 sleeve를 합쳐 판정한다(§5.4).
- **위임장 버전**: sleeve 버전 하나 + 그 버전에 결속된 위임장 조항(§3.1). 운영자가 승인하면 `live`.
- **분할 계획(plan)**: 한 종목·한 계좌의 계획 금액과 회차별 트리거. 회차(tranche) = 1차·2차·3차.
- **계획 결속 제안**: `order_proposals` 행이 계획 회차 하나를 가리키는 제안(§4.1).

## 2. 현재 상태

### 2.1 재사용할 인프라

| 무엇 | 위치 | 구분 | 이 설계에서의 쓰임 |
|---|---|---|---|
| sleeve 정체성(규칙·계좌·시장·통화, 불변) | `app/models/strategy_sleeves.py:59-89` | 사실 | 바구니 = sleeve. 허용 계좌·시장은 `kis_live`/`toss_live` × KR/US, `upbit` crypto(`:35-41`). |
| sleeve 버전(append-only: 상태·예산·손실 한도·사전등록·사유·설정자) | `app/models/strategy_sleeves.py:92-157` | 사실 | 위임장 버전의 뼈대. `live`는 사전등록 4필드 필수(`:120-126`). |
| 상태 어휘 `shadow/live/paused/stopped` | `app/models/strategy_sleeves.py:34`, `app/services/strategy_sleeves/vocabulary.py:29-35` | 사실 | 위임장 상태로 그대로 쓴다. |
| 두 테이블 UPDATE/DELETE/TRUNCATE 거부 트리거, 버전 직전+1, `order_proposals.sleeve_id` INSERT 때만 설정 | `alembic/versions/20261102_rob1470_strategy_sleeves.py:75-164` | 사실 | 새 테이블 가드의 선례. |
| 조건부 `at_app` GRANT(SELECT, INSERT) | `alembic/versions/20261102_rob1470_strategy_sleeves.py:329-332` | 사실 | 하드룰 18 선례. |
| `order_proposals.sleeve_id`(nullable, 변경 거부) | `app/models/order_proposals.py:127-132` | 사실 | 계획 결속 제안도 sleeve 태그를 가진다. |
| sleeve 게이트 판정(순서: unreadable → not_live → pnl_unmeasured → loss_limit → used_unmeasured → budget_exceeded) | `app/services/strategy_sleeves/gate.py:8-20`, `:57-98` | 사실 | 계획 결속 자동승인의 선행 조건으로 그대로 쓴다. |
| sleeve advisory lock(트랜잭션 끝까지) | `app/services/strategy_sleeves/store.py:139-144` | 사실 | 계획 회차 소비와 예산 측정을 같은 락 아래 직렬화. |
| dispatch에서 revalidation(브로커 호출) 전에 sleeve 게이트 | `app/services/order_proposals/dispatch.py:1171-1189`, `app/services/strategy_sleeves/dispatch_gate.py:133-173` | 사실 | 계획 게이트를 바로 뒤에 둔다(§4.3). |
| 손실 한도 도달 시 `stopped` 버전 자동 append(actor `system:sleeve_loss_gate`) | `app/services/strategy_sleeves/dispatch_gate.py:157-172`, `vocabulary.py:26` | 사실 | 위임장 손실 한도의 강제 수단. |
| 자동승인 한도 로더(min_distance·per_order·daily·mode·policy hash) | `app/services/order_proposals/auto_approve.py:340-366` | 사실 | 캡은 그대로 쓴다. |
| 자동승인 분류기 | `app/services/order_proposals/auto_approve.py:657-1297` | 사실 | §4.4의 게이트별 관계. |
| `ORDER_PROPOSALS_AUTO_APPROVE_MODE`(`off`/`expanded`, 기본 `off`) | `app/core/config.py:1050` | 사실 | 계획 결속은 `expanded`에서만. |
| 정책 per-order cap(kr 2,000,000 / us 1,500 / crypto 5,000,000), daily cap(kr 5,000,000 / us 20,000 / crypto 10,000,000) | `config/trading_policy.yaml:368-410`(per-order `:375`·`:381`·`:394`, daily `:396`·`:404`·`:410`) | 사실 | 계획 회차 금액(약 94만 = 235만 × 40%)은 KR per-order cap 아래다. |
| 시장가 근접 매수 거부(`marketable_not_resting`), 파킹만 예외 | `app/services/order_proposals/auto_approve.py:1103-1124` | 사실 | **계획 결속이 푸는 핵심 게이트**. 선례는 파킹 해제(`:1109-1114`). |
| 손실 매도 거부 `loss_cut_intent`, ROB-1460 결정 행만 해제 | `app/services/order_proposals/auto_approve.py:752-772`, `app/services/mechanical_loss_cut/gate.py:134-166` | 사실 | 결정 행 결속 자동승인의 선례이자 1b가 건드리지 않는 경로. |
| approval-required 태그 스캔(`policy_deviation` 등, 두 모드 모두 거부만) | `app/services/order_proposals/auto_approve.py:373-418`, `:793-800` | 사실 | 계획 결속에도 그대로 적용. |
| veto 가능 계좌·시장 | `app/services/order_proposals/auto_approve.py:192-209` | 사실 | KIS·Toss KR/US, Upbit crypto. Toss는 별도 설정으로 arm. |
| veto 카드 thesis 필수 | `app/services/order_proposals/auto_approve.py:1267-1273`, `dispatch.py:1128-1142` | 사실 | 계획 결속 제안은 계획의 논지 메모를 thesis로 싣는다. |
| 승인 창(세션) 경계 + 전송 직전 재표본 | `app/services/order_proposals/dispatch.py:1080-1094`, `approval_window.py:1286`, `revalidation.py:330` | 사실 | 그대로. 계획 결속은 정규장만(§4.4). |
| 자동승인 다이제스트·notices 목적지 | `app/services/order_proposals/dispatch.py:1634`, `auto_digest.py:150-155`, `app/core/config.py:1081` | 사실 | 자동 집행 알림 경로(§6). |
| 판단 근거·주문 시도 연결(`review.decision_evidence` 등) | `docs/agent-contracts/data-model-and-symbol-contracts.md` "판단 근거와 주문 시도 연결 (hk 4182)" | 사실 | 계획 회차 → 제안 → 판단 근거를 retro/v1이 읽을 수 있다. |
| deviation record 형식 | `docs/contracts/policy-freeze-deviation-20261102-mechanical-loss-cut.md:1-60` | 사실 | §4.5 초안 형식. |

### 2.2 sleeve가 이미 가진 것과 없는 것

| 위임장 조항(자문 §10) | sleeve가 가진 것 | 없는 것 |
|---|---|---|
| 바구니 예산 | `budget_cap` + 사용액 측정 게이트 [사실] | — |
| 손실 한도 | `loss_limit` + 도달 시 `stopped` 자동 append [사실] | — |
| 사전등록 | `prereg_*` 4필드, live 필수 [사실] | — |
| 상태·승인자·사유 | `state`·`set_by`·`reason` [사실] | 운영자 승인과 시스템 쓰기를 구분하는 "승인" 개념은 `set_by` 문자열뿐 |
| 직렬화 | per-sleeve advisory lock [사실] | — |
| 대상 종목 조건 | 없음 | 위임장 조항 |
| 종목별 분할 계획 | 없음 | 새 테이블 |
| 주문 방식(매도 1호가 +N bp 이내) | 없음 | 위임장 조항 |
| 주문당·하루 한도(바구니 전용) | 없음(전역 정책 cap만) | 위임장 조항(전역 cap 이하로만) |
| 업종 상한 35% | 없음(정책 `portfolio.sector_cluster_cap_pct`는 advisory) | 위임장 조항 + 판정 |
| 낙폭 동결(바구니 −25%, 전체 −25%) | 없음(손실 한도는 금액·`stopped`, 동결이 아님) | 위임장 조항 + 동결 판정 |
| 만료일 | 없음(`prereg_max_duration_days`는 통계 기간) | 위임장 조항 |
| 계획 결속 자동승인 | 없음 — sleeve 게이트는 **거부만** 한다. 통과해도 기존 분류기가 시장가 근접 매수를 거부한다 | 새 분류 부류 |

## 3. 데이터 모델 제안

### 3.1 (a) 위임장 = sleeve 버전에 무엇을 더하는가

[의견] sleeve 버전 테이블에 열을 더하지 않고, **버전에 1:1로 결속된 append-only 조항 테이블** `review.mandate_terms`를 둔다. 이유: sleeve 버전은 이미 dispatch 게이트·리포트·트리거가 읽는 안정된 표면이고, 열 추가는 그 트리거(`20261102_rob1470_strategy_sleeves.py:123-130`)와 store 검증을 함께 고쳐야 한다. 별도 테이블은 추가형 마이그레이션 하나로 끝난다.

`review.mandate_terms` (sleeve 버전당 최대 1행, `UNIQUE(sleeve_version_id)`):

| 열 | 형 | 의미 |
|---|---|---|
| `sleeve_version_id` | FK → `strategy_sleeve_versions.id` | 이 조항이 속한 위임장 버전 |
| `terms_version` | text | 조항 스키마 버전(`mandate-terms/v1`) |
| `basket_key` | text | 바구니 정체성(예 `basket-1`). 같은 바구니의 sleeve(KIS KR, Toss KR …)가 같은 값을 갖는다. 바구니 낙폭·업종 상한은 이 키로 합산. **한 바구니는 한 통화만**(같은 `basket_key`의 조항은 같은 sleeve 통화여야 함, store가 거부) — 환산 없이 합산하기 위해서다. KR과 US는 다른 바구니다 |
| `universe_rule` | text(≤500) | 대상 종목 조건(사람이 읽는 문장 + 규칙 id) |
| `allowed_symbols` | text[] | 이 버전이 계획을 받을 수 있는 종목(정규화 심볼, `app/core/symbol.py` 경유). 비면 계획 등록 불가 |
| `entry_max_premium_bps` | numeric | 근접가 상한: 지정가 ≤ 매도 1호가 × (1 + bp/10000). 자문 권고 30bp |
| `per_order_cap` | numeric | 바구니 주문당 상한. 전역 `per_order_cap` 이하만(CHECK는 못 하므로 store가 검증) |
| `daily_cap` | numeric | 바구니 하루 상한. 전역 `daily_cap` 이하만 |
| `sector_cap_pct` | numeric(0~100] | 업종 상한(2b: 35) |
| `basket_drawdown_freeze_pct` | numeric(0~100] | 바구니 낙폭 동결(2c: 25) |
| `account_drawdown_freeze_pct` | numeric(0~100] | 전체 자산 낙폭 동결(2c: 25) |
| `tranche_rule` | jsonb(닫힌 스키마) | 회차 비중·트리거·최소 간격 기본값. 2b: `{"weights":[0.4,0.3,0.3],"triggers_pct_from_first":[0,-15,-30],"min_gap_sessions":20}` |
| `loss_sell_mode` | text enum | `human_card`(1b 처음 4주) / `auto` (이후 재검토 결과). 기본 `human_card` |
| `expires_at` | timestamptz | 이 위임장 버전의 만료. 지나면 자동승인 대상 아님(사람 카드) |
| `approved_by`·`approved_at` | text·timestamptz | 운영자 승인 기록. 시스템 actor 금지(store가 거부) |
| `content_sha256` | text | 위 조항의 정규화 JSON 해시. 계획·제안이 이 해시를 인용한다 |

규칙:
- 위임장 변경 = 새 sleeve 버전 + 새 조항 행(1c). 기존 조항은 수정할 수 없다.
- `state='live'` 버전에 조항 행이 없으면 계획 결속 자동승인은 열리지 않는다(sleeve 게이트만으로는 사람 카드).
- 조항 금액은 운영자만 정한다. 코드·정책에 기본 금액 없음(ROB-1470 원칙과 같음).

### 3.2 (b) 종목별 분할 계획 테이블

두 테이블: 계획(불변 정체성) + 계획 이벤트(append-only 상태 전이). 행 UPDATE를 금지하므로 현재 상태는 마지막 이벤트다.

`review.mandate_plans`:

| 열 | 의미 |
|---|---|
| `id` | PK |
| `sleeve_id` | 바구니(→ 계좌·시장·통화가 정해진다) |
| `symbol` | 정규화 심볼 |
| `approved_terms_id` | 등록 시점 위임장 조항 행(→ 버전·해시) |
| `planned_amount` | 계획 금액(2b: 약 235만) |
| `thesis` | 논지 메모(≤1000자, veto 카드 thesis) |
| `invalidation_conditions` | text[] ≤3, 논지 훼손 조건(반증 가능 문장) |
| `sector_key` | 업종 키(업종 상한 판정용) |
| `created_by`·`created_at` | 등록자 |
| 활성 종목 예약 | **한 종목의 계획은 한 계좌에만**(수정 3). 존재 확인 트리거는 동시 INSERT를 막지 못하므로 별도 예약 테이블 `review.mandate_symbol_reservations`(`market`, 정규화 `symbol`, `plan_id`, `released_at` 없음 — 해제는 별도 append-only 해제 행)로 강제한다. 계획 INSERT와 종결 전이는 `pg_advisory_xact_lock(hashtextextended('mandate_symbol:'||market||':'||symbol,0))`를 **계좌와 무관하게** 먼저 잡고 활성 예약을 확인·기록한다(sleeve lock은 계좌별이라 직렬화하지 못한다, `app/services/strategy_sleeves/store.py:139-144`). 예약 판정은 같은 락 아래에서만 한다(§3.4) |

`review.mandate_plan_tranches` (계획당 회차 행, 계획 INSERT와 같은 트랜잭션에 생성, 불변):

| 열 | 의미 |
|---|---|
| `plan_id`, `tranche_index` (1..3) | |
| `weight` | 0.4 / 0.3 / 0.3 |
| `amount` | `planned_amount × weight`(정확한 decimal) |
| `trigger_pct_from_first` | 0 / −15 / −30 |
| `min_gap_sessions` | 20(회차 1은 0) |

`review.mandate_plan_events` (append-only):

| 열 | 의미 |
|---|---|
| `plan_id`, `seq`(직전+1) | |
| `event` | 닫힌 어휘(아래 상태 전이) |
| `tranche_index` | 회차 관련 이벤트일 때 |
| `first_fill_price` | 1차 체결 확정 이벤트에만(트리거 기준가) |
| `terms_id`·`terms_sha256` | `approved`·`terms_rebound` 이벤트에 필수, `rejected`·`retired`는 선택(묶음 스냅샷에서 왔을 때 기록), 그 외 NULL(CHECK). `terms_id`는 `mandate_terms` FK, 해시는 그 행과 일치해야 함(트리거) |
| `effective_session` | `approved`·`terms_rebound`·`resumed`에 필수: 승인 효력이 시작되는 거래일(시장 달력) |
| `resume_target` | `resumed`에 필수: `approved` 또는 `active` |
| `batch_member_id` | 묶음 승인에서 온 이벤트면 그 멤버 스냅샷(§5.1) |
| `proposal_id` | 회차가 제안을 만들었을 때 |
| `evidence` | jsonb(닫힌 키: 원장 행 id, 거래일, 사유 코드, 회고 id, 승인 이슈 id) |
| `actor` | 운영자 / `system:mandate_*` |
| `created_at` | |

상태 전이(계획 단위):

```
proposed ──(아침 묶음 승인, 1a)──▶ approved ──(1차 체결 확정)──▶ active
   │                                  │                           │
   └─(기각/만료)─▶ rejected           └─(위임장 비활성)─▶ suspended ◀─┤(동결·논지 확인 실패)
                                                                  │
                         active ──(3차 체결 또는 계획 소진)──▶ exhausted
                         active/exhausted ──(논지 훼손)──▶ thesis_broken ──(손실 매도 체결)──▶ closed
                         any ──(운영자 폐기)──▶ retired
suspended ──(운영자 재개, 1차 미체결)──▶ approved
suspended ──(운영자 재개, 1차 체결)──▶ active
approved/active ──(위임장 변경 뒤 재승인 rebind)──▶ 같은 상태(terms_rebound 이벤트)
```

계획 단위 이벤트 어휘: `proposed`, `approved`(묶음 승인, `terms_id`·`effective_session` 포함), `rejected`, `first_tranche_settled`(→active), `suspended`(사유 코드), `resumed`(운영자, 대상 상태 approved|active), `terms_rebound`(운영자 재승인, 새 `terms_id`·`terms_sha256`·`effective_session`), `exhausted`, `thesis_broken`, `closed`, `retired`. INSERT 트리거는 위 다이어그램의 전이만 허용한다.

**계획 상태는 이벤트 스트림 전체를 접어서(fold) 얻는다** — 마지막 이벤트 이름이 곧 상태가 아니다(예: `approved` 뒤 `terms_rebound`나 `resumed(→approved)`가 와도 상태는 `approved`). 순수 함수 `fold_plan_events`가 (상태, 현재 결속 조항, 효력 세션, 1차 체결가)를 돌려주고 게이트·트리거·CLI가 모두 이 함수를 쓴다.

"현재 결속 조항"은 원래 `approved_terms_id`가 아니라 **마지막 `approved` 또는 `terms_rebound` 이벤트의 `terms_id`**다. 계획 행 자체는 불변으로 두고 rebind를 이벤트로 남겨 회차·체결 이력을 보존한다(Q3).

회차 단위 이벤트: `tranche_armed`(가격·시간 조건 충족) → `tranche_proposed`(제안 생성, proposal_id) → 제안 종결 시 원장 증거로 셋 중 하나:

- `tranche_filled`: 그 회차의 누적 체결 금액이 회차 `amount`의 95% 이상(잔여가 1주 미만 포함). 회차 완료, 다음 회차 arming 가능(I7).
- `tranche_partially_filled`: 0 < 누적 체결 < 95%이고 제안이 종결(DAY 만료·취소)됨. 수량·평균가를 기록한다. 회차는 **완료가 아니다** — I7은 다음 회차를 막고, 잔여(`amount` − 누적 체결)는 다음 거래일부터 같은 회차로 다시 `tranche_armed` 될 수 있다(I8이 잔여만 허용, I9가 열린 제안 1개를 보장). 회차 간 최소 간격(I6)은 회차 안 잔여 재제안에는 적용하지 않는다. 운영자는 `tranche_closed_partial`(잔여 포기, 회차 완료로 취급)을 기록할 수 있다.
- `tranche_unfilled`: 체결 0으로 종결. 다음 거래일 다시 armed 가능.

1차 회차의 `first_fill_price`는 1차가 `tranche_filled` 또는 `tranche_closed_partial`로 끝날 때의 누적 평균가다.

### 3.3 불변식 표

| # | 불변식 | 입력 | 전이 | 강제 위치 | 관측 결과(위반 시) |
|---|---|---|---|---|---|
| I1 | 한 종목의 활성 계획은 전 계좌에서 1개 | 계획 INSERT, 종결 전이 | proposed / 종결 | 시장+심볼 키 advisory lock(계좌 무관) 아래 예약 테이블 확인·기록 + 예약 테이블 DB 트리거 검사(아래) | INSERT 거부, CLI 오류 |
| I2 | 계획 종목은 등록 시점 위임장 `allowed_symbols` 안 | 계획 INSERT | proposed | store | 거부 |
| I3 | (a) 그 sleeve의 비종결 계획 `planned_amount` 합 ≤ 그 sleeve `budget_cap`, (b) 바구니(`basket_key`) 전체 비종결 계획 합 ≤ 구성 sleeve `budget_cap` 합 | 계획 INSERT | proposed→approved | store. (a)는 sleeve lock, (b)는 바구니 키 advisory lock(`mandate_basket:<key>`)을 sleeve lock보다 먼저 잡아 직렬화 | 거부 `mandate_budget_exceeded` |
| I4 | 업종 비중 ≤ `sector_cap_pct`(바구니 평가액 + 계획 잔여 기준) | 회차 제안 | armed→proposed | 계획 게이트 | 사람 카드 `mandate_sector_cap` |
| I5 | 회차 k(≥2)는 1차 체결가 × (1+trigger) 이하 가격에서만 armed | 일봉/현재가, `first_fill_price` | →armed | 트리거 판정(순수 함수) | armed 안 됨 |
| I6 | 회차 간 최소 20거래일(직전 회차 체결 확정일 기준, 시장 달력) | 원장 체결일, 달력 | →armed | 순수 함수 | armed 안 됨 |
| I7 | 회차는 순서대로(k는 k−1 filled 뒤) | 이벤트 | →armed | 순수 함수 + 이벤트 트리거 | 거부 |
| I8 | 회차 금액 초과 주문 없음: 제안 notional(max(limit, 현재가) × 수량) ≤ 회차 `amount` − 이미 체결된 회차 금액 | 제안 rung | armed→proposed | 계획 게이트 | 사람 카드 `mandate_tranche_amount` |
| I9 | 같은 회차에 열린 제안은 1개 | 이벤트 | armed→proposed | sleeve lock + 이벤트 트리거 | 거부 |
| I10 | "filled"는 브로커 증거로만(`execution_ledger_in_effect()`, authoritative) | 원장 | proposed→filled | 대사 후 투영 CLI | 증거 없으면 전이 없음(websocket 단독은 미확정) |
| I11 | 낙폭 동결 중에는 매수 회차 armed/제안/**전송** 불가(수동 승인 포함), 보유·매도는 유지 | 낙폭 판정 | →suspended | 제안 생성 + 공통 전송 직전 경계(§5.4) | 제안 생성 거부·전송 차단 `mandate_drawdown_frozen` + 운영자 알림 |
| I12 | 위임장 버전이 live가 아니거나 만료면 자동승인 없음 | sleeve 버전·조항 | 모든 제안 | 계획 게이트 | 사람 카드 `mandate_not_live`/`mandate_expired` |
| I15 | 제안의 심볼·시장·계좌는 결속된 계획과 같다 | 제안 INSERT, dispatch | 제안 생성 / 결속 소비 | `order_proposals` INSERT 트리거(정규화 심볼·`market`·`account_mode`를 계획·sleeve와 비교) + 계획 게이트 재비교 | INSERT 거부 / 사람 카드 `mandate_binding_mismatch` |
| I16 | 계획 결속 제안은 전송 직전에 계획이 `approved`(1차) 또는 `active`(2·3차)이고 현재 결속 조항이 현재 live 버전의 조항이며 승인 효력 세션이 도래함 | 마지막 계획 이벤트 | 결속 소비 | 계획 게이트, sleeve lock 아래 | 사람 카드 `mandate_plan_not_active` |
| I17 | 파킹 allowlist 심볼(459580·357870·SGOV·BIL)은 계획 대상이 아니다 | 계획 INSERT | proposed | store + `allowed_symbols` 검증(`parking_scope`) | 거부 |
| I13 | 계획·회차·조항·이벤트 행은 수정·삭제 불가 | 모든 쓰기 | — | DB 트리거 | `restrict_violation` |
| I14 | 손실 매도는 `thesis_broken` 이벤트 뒤에만, 처음 4주 사람 카드 | 이벤트, `loss_sell_mode` | →closed | 계획 게이트 | 사람 카드 `mandate_loss_sell_human` |

### 3.4 DB 가드와 GRANT

- 여섯 테이블(`mandate_terms`, `mandate_plans`, `mandate_plan_tranches`, `mandate_plan_events`, `mandate_symbol_reservations`, `mandate_symbol_reservation_releases`) 모두 UPDATE·DELETE·TRUNCATE 거부 트리거(모든 역할). 선례 `20261102_rob1470_strategy_sleeves.py:75-130`.
- 이벤트 `seq`는 직전+1만, 이벤트 어휘·전이 표에 없는 전이는 INSERT 트리거가 거부.
- I1: 예약은 append-only 두 테이블 `review.mandate_symbol_reservations`(market, 정규화 symbol, plan_id)와 `review.mandate_symbol_reservation_releases`(reservation_id UNIQUE)로 기록하고 이력은 지우지 않는다. 부분 UNIQUE는 다른 테이블의 해제 행을 볼 수 없으므로 쓰지 않는다. 대신 예약 INSERT 트리거가 **스스로** `pg_advisory_xact_lock(hashtextextended('mandate_symbol:'||market||':'||symbol,0))`을 잡은 뒤 같은 market·symbol에 해제 행이 없는 예약이 있으면 거부한다. 해제 INSERT 트리거도 같은 락을 잡는다. 락이 트리거 안에 있으므로 어떤 writer도 우회할 수 없고, 두 동시 INSERT 중 뒤의 것은 앞의 커밋을 기다린 뒤(READ COMMITTED 재조회) 거부된다. 트리거는 READ COMMITTED가 아닌 트랜잭션을 거부한다(#1250 Q-46 게이트와 같은 이유). 해제는 계획이 종결(rejected / exhausted 뒤 보유 0 / closed / retired)될 때 한다.
- `order_proposals`에 `mandate_plan_id`·`mandate_tranche_index`(nullable) 추가, INSERT 때만 설정, `sleeve_id`·`account_mode`·`market`·정규화 `symbol`이 계획(과 그 sleeve)과 같아야 함(트리거, I15). `order_proposal_create`는 `symbol`을 따로 받으므로(`app/mcp_server/tooling/order_proposal_tools.py:703`) 이 비교 없이는 다른 종목 제안이 회차에 결속될 수 있다. 선례 `:157-164`.
- downgrade는 행이 하나라도 있으면 거부.
- 하드룰 18: 같은 마이그레이션에 조건부 `at_app` GRANT — 위 여섯 테이블과 T3의 묶음 테이블 SELECT·INSERT만, UPDATE·DELETE 없음. `order_proposals` 새 열은 기존 테이블 권한을 따른다.

## 4. 계획 결속 자동승인

### 4.1 어떤 제안이 사람 카드 없이 나가는가

아래가 **전부** 참일 때만 그 rung은 자동승인 적격이다(하나라도 거짓이면 기존과 같은 사람 카드, 사유 코드 기록).

1. 스위치: `MANDATE_AUTO_APPROVE_ENABLED=true` AND 시장이 `MANDATE_AUTO_APPROVE_MARKETS`에, 계좌가 `MANDATE_AUTO_APPROVE_ACCOUNT_MODES`에 있음 AND `ORDER_PROPOSALS_AUTO_APPROVE_MODE=expanded`. 셋 다 기본 off/빈 값.
2. 제안이 계획 결속: `mandate_plan_id`·`mandate_tranche_index` 설정, `sleeve_id`·`account_mode`·`market`·정규화 `symbol`이 계획과 일치(I15, 생성 때와 결속 소비 때 두 번), 해당 회차의 마지막 이벤트가 `tranche_proposed`이고 그 `proposal_id`가 이 제안, 그리고 **이벤트 스트림을 접은 계획 상태(`fold_plan_events`)가 `approved`(1차) 또는 `active`(2·3차)이고 suspended·thesis_broken·retired가 아님**(I16). 회차 이벤트 뒤에 계획이 정지·폐기·논지 훼손되면 이미 만든 제안도 사람 카드다.
3. sleeve 게이트 통과(`gate.py:57-98`): live, 손실 한도 미만, 예산 안.
4. 위임장 조항: 현재 live 버전에 조항 행 있음, 만료 전, 계획의 **현재 결속 조항**(마지막 `approved`/`terms_rebound` 이벤트의 `terms_id`)이 **현재 live 버전의 조항 행 id와 같음**. 해시가 같다는 이유만으로 승계하지 않는다 — 버전이 바뀌면 해시가 같아도 재승인(`terms_rebound`)이 필요하다(Q3-B). 그 이벤트의 `effective_session`이 도래해야 한다.
5. 주문 모양: `action='place'`, `order_type='limit'`, `side='buy'`, `exit_intent IS NULL`, 회차 1개당 rung 1개.
6. 가격: 지정가 ≤ 전송 직전 매도 1호가 × (1 + `entry_max_premium_bps`/10000). 지금의 KIS·Toss preview는 `current_price`만 주고 매도 1호가가 없다(`app/mcp_server/tooling/order_validation.py:1078`, `app/mcp_server/tooling/orders_toss_variants.py:1471`). 그래서 venue별 호가 읽기(KIS 국내 호가 TR / Toss 호가, US는 해당 venue 호가)를 관측 시각과 함께 읽고 신선도 상한(권고 5초) 안일 때만 쓴다(티켓 T4b). 호가가 없거나 stale이면 현재가로 대체하지 않고 사람 카드(`mandate_ask_unavailable`).
7. 회차 조건 재검: I5·I6·I7·I8 을 신선한 preview 가격으로 다시 판정.
8. 금액: 제안 notional ≤ min(전역 per-order cap, 조항 `per_order_cap`), 그날 자동승인 누계 + notional ≤ min(전역 daily cap, 조항 `daily_cap`).
9. 업종 상한(I4), 낙폭 동결 아님(I11, `buy_freeze_state`, §5.4). 동결 판정(hk 4900)이 없으면 이 조건은 참이 될 수 없다 — 판정 함수 부재·읽기 실패는 동결과 같다.
9a. 파킹 면제 없음: 계획 대상 심볼은 파킹 allowlist 밖(I17)이고, 분류기는 결속이 있는 rung에 파킹 daily-cap 면제(`auto_approve.py:835`, `:1072`)를 적용하지 않으며, 자동승인 일 누계 reader도 계획 결속 rung을 일반 누계에 넣는다(`app/services/order_proposals/service.py:121`, `:4050`의 파킹 제외에 걸리지 않게).
10. 기존 공통 게이트: approval-required 태그 스캔 통과, veto 가능 계좌·시장, thesis 존재, 정규장(KR XKRX 정규장, US XNYS 정규장), 신선한 preview `success=true`.

### 4.2 제안을 만드는 쪽

- 1차: 아침 묶음 승인(§5.1)에서 승인된 계획마다 `tranche_armed(1)` → 장중 운영자 CLI 또는 세션이 `order_proposal_create(mandate_plan_id=…, tranche_index=1)`로 제안을 만든다. 지정가는 생성 시점 매도 1호가 + N bp 이내.
- 2·3차: 가격 트리거가 armed를 만들고, 같은 경로로 제안을 만든다. 트리거 소스(fillwire 가격 트리거 또는 일봉 종가 스캔)는 별도 티켓(T7). 트리거는 **제안만 만든다** — 전송 권한은 §4.1이 dispatch에서 판정한다.
- 미리 걸어 두는 깊은 지정가는 만들지 않는다(자문 §2.1).

### 4.3 dispatch에서의 위치

`dispatch_proposal`의 sleeve 게이트(`dispatch.py:1171-1189`) **바로 뒤**, revalidation(브로커 호출) 전에 계획 게이트를 둔다. 같은 sleeve advisory lock 아래에서 계획 이벤트를 읽고 I4·I8·I9·I11·I12를 판정한다. 통과하면 분류기에 `mandate_binding`(ROB-1460의 `mechanical_binding`과 같은 모양, `dispatch.py:384-400`)을 넘기고, 분류기는 그 결속이 있을 때만 §4.4의 한 게이트를 푼다. 결속 로드 실패는 `None` = 사람 카드.

### 4.4 기존 자동승인 경로와의 관계, 완화되는 게이트 전부

| 기존 게이트 | 위치 | 계획 결속에서 | 완화인가 | deviation record 대상 |
|---|---|---|---|---|
| 모드 `off`/`expanded`, 미지 모드 거부 | `auto_approve.py:716-719` | `expanded`에서만 | 아니오(그대로) | 아니오 |
| 지원 action, target evidence | `auto_approve.py:722-741` | `place`만 | 아니오 | 아니오 |
| `order_type=limit` | `auto_approve.py:742-750` | limit만 | 아니오 | 아니오 |
| `loss_cut_intent` 거부(ROB-1460 결정 행만 해제) | `auto_approve.py:752-768` | 손실 매도는 계획 결속 대상 아님. 1b는 사람 카드 | 아니오 | 아니오 |
| `exit_intent_present` 거부 | `auto_approve.py:771-772` | exit intent 있는 제안은 대상 아님 | 아니오 | 아니오 |
| veto 가능 계좌·시장(Toss는 별도 arm) | `auto_approve.py:192-209`, `:773-790` | 그대로 요구 | 아니오 | 아니오 |
| approval-required 태그 스캔(`policy_deviation` 등) | `auto_approve.py:793-800` | 그대로 적용. 계획 결속이 태그를 무시하지 않는다 | 아니오 | 아니오 |
| preview `success` | `auto_approve.py` `:861-867` | 그대로 | 아니오 | 아니오 |
| per-order cap(전역) | `auto_approve.py:984-990`, `config/trading_policy.yaml:368-394` | 그대로, 조항 cap이 더 작으면 그 값. notional은 `max(limit, current) × qty`(파킹·ROB-1460과 같은 실행가 기준) | 아니오(오히려 엄격) | 아니오 |
| daily cap(전역) | `auto_approve.py:1072-1080`, `config/trading_policy.yaml:395-410` | 그대로 공유(계획 결속 주문도 전역 일 누계를 소비). 면제 없음 | 아니오 | 아니오 |
| **KIS·Toss preview/submit의 '현재가 초과 매수' 거부** — 정상 KIS 지정가 매수가 현재가를 넘으면 분류기 전에 거부되고, 예외는 파킹뿐 | `app/mcp_server/tooling/order_validation.py:1118`, `app/mcp_server/tooling/order_execution.py:1986`, revalidation이 분류기 전에 preview를 얻음(`app/services/order_proposals/revalidation.py:1480`, `:1599`) | 매도 1호가 + N bp 지정가는 현재가를 넘을 수 있으므로 **결속된 계획 회차에 한해** preview와 submit 모두에서 상한을 `min(매도 1호가 × (1+N bp))`로 바꾼다. 파킹 예외 플래그를 넓히지 않고 별도 플래그 `allow_mandate_bound_buy`(결속 증거 필수)를 둔다 | **예 — 분류기와 별개의 두 번째 완화** | **예** |
| **시장가 근접 매수 거부** `marketable_not_resting`(expanded) / `distance_below_minimum`(off) | `auto_approve.py:1103-1124` | **해제**: 결속이 있고 지정가 ≤ 매도 1호가 × (1+N bp) 일 때만. 파킹 해제(`:1109-1114`)와 같은 자리에 두 번째 해제를 추가 | **예 — 이 설계의 유일한 분류기 완화** | **예** |
| 매수 체결이 veto 카드보다 먼저 날 수 있음(근접가 매수의 결과) | 위 해제의 결과 | 알림은 체결 뒤일 수 있다. veto 버튼은 미체결 잔량 취소만 가능 | **예**(사람 확인 전 체결 위험 수용) | **예**(위와 같은 record에 명시) |
| `min_distance_pct` 3% 하한 | `config/trading_policy.yaml` `min_distance_pct: 3`(`auto_approve.py:1100-1102`) | expanded는 원래 0, 계획 결속도 0 | 아니오(expanded 기존 동작) | 아니오 |
| 승인 창(세션) 경계·전송 직전 재표본 | `dispatch.py:1080-1094`, `revalidation.py:330` | 그대로. 계획 결속은 정규장만(NXT·애프터·US pre/post 없음) | 아니오 | 아니오 |
| sleeve 게이트 | `dispatch.py:1171-1189` | 선행 조건 | 아니오 | 아니오 |
| Toss 동일 세션 freeze | `dispatch.py:1110-1127` | 그대로 | 아니오 | 아니오 |
| thesis 필수 | `auto_approve.py:1267-1273`, `dispatch.py:1128-1142` | 계획 `thesis`를 제안 thesis로 | 아니오 | 아니오 |
| 손실매도 가드(sell preview) | `auto_approve.py:1126-1145` | 매수만 대상이라 해당 없음 | 아니오 | 아니오 |
| 3792 승인 요청 4 / 결정 §1-3: KR·US 신규 매수 제안 off | 운영 결정(정책 YAML에 아직 없음, 결정 §5-1) | 계획 결속 매수만 예외로 연다 | **예 — 운영 결정 범위 안의 재개**(결정 3792-4가 "계획으로만 연다"고 명시) | **예**(같은 record에 근거 결정 인용) |
| 모의 → 실전 승격 | 운영 결정 1c | 스위치·allowlist 변경은 사람 승인 | 해당 없음(활성화 절차) | record의 `effective_at` |

정리: 완화는 (1) 계획 결속 매수에 한한 KIS·Toss preview/submit 현재가 초과 매수 거부 해제, (2) 분류기 `marketable_not_resting` 해제, (3) 그 결과인 veto 전 체결 위험 수용, (4) 신규 매수 off 결정에 대한 계획 결속 예외 넷이다. 파킹 daily-cap 면제는 계획 결속에 적용하지 않는다(§4.1 9a). 캡·태그 스캔·세션·sleeve·손실매도 경로는 완화하지 않는다.

### 4.5 deviation record 초안 위치와 활성화 절차

- 파일: `docs/contracts/policy-freeze-deviation-<머지일>-mandate-plan-bound-auto-approve.md`. 형식은 `policy-freeze-deviation-20261102-mechanical-loss-cut.md:1-60`과 같다. 이 설계 PR에는 넣지 않고 T5(분류기 해제) PR에 같이 넣는다.
- 초안 필드:
  - `change_id: hk3795-mandate-plan-bound-auto-approve`
  - `operator_decision_ref`: `decision/2026-11-10/at-new-strategy-go` §1-1·§3, `decision/2026-11-16/at-strategy-direction` "이 결정이 주지 않는 것", `report/2026-11-12/at-3792-holdings-classification` 승인 요청 4
  - `change_class: [auto_approval_guard_relaxation_behind_default_off_switch, marketable_buy_release_plan_bound]`
  - `affected_policy_keys`: 없음(새 env 스위치 3개, 정책 값 변경 0) — 위임장 수치는 DB 조항
  - `consumer_paths`: `app/services/order_proposals/auto_approve.py`, `dispatch.py`, `revalidation.py`, `app/mcp_server/tooling/order_validation.py`, `order_execution.py`, `orders_toss_variants.py`, `app/services/mandate/`(신규)
  - `effective_at`: 마이그레이션 적용 + 위임장 버전 live + 운영자의 별도 활성화 승인(스위치 3개 설정) 뒤. 머지만으로는 아무것도 켜지지 않는다.
  - `rollback`: `MANDATE_AUTO_APPROVE_ENABLED=false` 다음 설정 로드부터 모든 계획 결속 제안은 사람 카드. 이미 브로커가 받은 주문은 되돌리지 않고 대사로 처리.
- 활성화 승인 절차(순서, 각 단계 운영자 결정):
  1. 마이그레이션 적용(운영 마이그레이션 승인).
  2. 위임장 버전 생성(sleeve `shadow` + 조항) → 운영자 승인 → `live`.
  3. 모의 검증: 키움 mock에서 같은 계획 경로로 4주, 30건, 자문 §2.1 기계 지표(체결률 ≥80%, 슬리피지 중앙값 ≤0.3%, uncertain·중복 0). 지금 sleeve CHECK는 `kis_live`/`toss_live`/`upbit`만 받고(`app/models/strategy_sleeves.py:35-41`) KIS mock은 제안 제출 조합에 없다(`app/services/order_proposals/service.py:358`). 그래서 티켓 T1m이 sleeve 계좌 조합에 `kiwoom_mock`(KR)을 **모의 전용**으로 추가한다(추가형 CHECK 변경, live 조합·게이트 불변). 키움 mock 제안은 수동 승인 전용이므로(`approval-and-shadow-contracts.md` 키움 절) 모의는 계획 경로·호가 근접 지정가·체결 투영·기계 지표만 검증하고 분류기 해제는 검증하지 않는다(§7 Q6). KIS mock은 제안 조합이 생긴 뒤 별도.
  4. 실전 카나리: 스위치 3개를 시장·계좌 하나씩(권고: Toss KR 먼저), 1차 분할만, 종목 3~5개.
  5. 각 확대(시장·계좌 추가, 2·3차 회차 허용)는 별도 승인.

## 5. 사람 승인이 남는 흐름

### 5.1 1a — 하루 1회 장 시작 전 묶음 승인

- 대상: (a) `proposed` 계획(새 종목 등록), (b) 위임장 버전이 바뀌어 현재 결속 조항이 live 조항과 다른 `approved`/`active` 계획(재승인 대상), (c) 그날 armed가 될 회차 요약.
- 카드: ROB-870 batch 경로는 **재사용하지 않는다**. 그 멤버는 주문 제안에 FK로 묶이고 개별 승인 카드 게시를 요구하며(`app/models/order_proposals.py:531`, `app/services/order_proposals/service.py:487`), 승인 콜백이 멤버마다 `_handle_approve`를 불러 브로커 주문을 낼 수 있다(`app/services/order_proposals/telegram_callback.py:1223`, `:1388`). 대신 새 테이블 `review.mandate_plan_batches`(+멤버 행, 불변 스냅샷: 계획 id·조항 id·계획 해시)와 nonce 결속 콜백 `mpb`를 둔다. 이 콜백은 멤버 종류에 따라 계획 이벤트 `approved`/`rejected`(새 계획) 또는 `terms_rebound`/`retired`(재승인 대상)만 append하며, 각 이벤트에 스냅샷의 `terms_id`·`terms_sha256`·`effective_session`(다음 정규장 세션)·`batch_member_id`를 싣는다. 콜백 시점 스냅샷 조항이 더 이상 live가 아니면 그 멤버는 아무 이벤트도 쓰지 않는다. 콜백은 주문·제안 모듈을 import하지 않는다(정적 테스트). 멤버 1개도 허용한다. 콜백 인가·nonce 단일 소비·chat allowlist는 기존 콜백과 같은 규칙을 쓰되 코드 경로는 분리한다.
- 발송: 장 시작 전 운영자/세션이 CLI로 발송(scheduleless). 정기 실행은 §7 Q8.
- 아무것도 바뀌지 않았으면 "오늘 적용 기준" 요약만(자문 §10).
- 승인 마감: 그 거래일 정규장 개장 전. 마감 뒤 승인은 다음 거래일부터 유효(§7 Q4).

### 5.2 1b — 논지 훼손 손실 매도

- 논지 훼손 판정: 계획의 `invalidation_conditions` + 결정론 검사(공시·실적 이벤트). 판정은 `thesis_broken` 계획 이벤트를 쓴다.
- 매도 경로: 새 exit intent를 만들지 않는다. 제안 생성은 `loss_cut` 외 exit intent의 손실 매도를 거부하고(`app/services/order_proposals/service.py:1139`) KIS·Toss preview/submit도 따로 거부하며(`app/mcp_server/tooling/order_execution.py:2056`, `orders_toss_variants.py:1213`), 평단 가드는 검증된 `loss_cut` 문맥만 완화한다(`app/mcp_server/tooling/order_validation.py:180`). 그래서 1b는 **기존 `exit_intent='loss_cut'` 사람 승인 의식**(손절 2클릭 카드, `INVEST_LOSS_CUT_APPROVAL_ENABLED`·Telegram 손절 콜백)을 그대로 쓰고, 근거로 계획 id와 `thesis_broken` 이벤트 id를 rationale에 싣는다. 이 경로는 게이트를 바꾸지 않는다.
- `loss_cut` 제안이 요구하는 증거를 T8이 만든다(`app/services/order_proposals/service.py:1153-1199`): `exit_reason='thesis_change'`, 72시간 이내·같은 심볼·`trigger_type='thesis_change'`인 회고 행(`save_trade_retrospective`로 판정 직후 기록, 근거 = 충족된 `invalidation_conditions`와 결정론 검사 결과, 계획 `decision_id`가 있으면 연결), 비어 있지 않은 `approval_issue_id`(= hk 이슈/운영자 승인 기록 id; 없으면 제안을 만들지 않고 운영자에게 알림). 회고 id와 승인 이슈 id는 `thesis_broken` 이벤트 `evidence`에 남겨 제안과 결속한다. 기존 검사는 하나도 건너뛰지 않는다.
- 처음 4주(조항 `loss_sell_mode='human_card'`): `loss_cut` 제안은 분류기가 `loss_cut_intent`로 거부한다(`auto_approve.py:752-759`). ROB-1460 결정 행이 없으므로(바구니 ①은 ROB-1460 allowlist 밖, Q7) 해제되지 않는다. **기존 거부가 1b를 강제한다.**
- 4주 뒤 `auto`로 바꾸는 것은 위임장 변경(새 버전)이자 별도 게이트 완화다 — 이 설계는 그 경로를 만들지 않는다(§7 Q5).
- ROB-1460과의 관계: ROB-1460은 "기록된 손절가 이탈"이라는 가격 규칙이고, 1b는 "논지 조건 충족"이다. 같은 lot에 둘 다 해당할 수 있다. 바구니 ①의 결정 §2.1은 "논지 훼손이 유일한 손실 매도"이므로, [의견] 바구니 ① sleeve의 계좌·시장은 ROB-1460 live allowlist에 넣지 않는다(§7 Q7). ROB-1460 코드는 바꾸지 않는다.

### 5.3 1c — 한도 초과·위임장 변경·모의→실전 승격

- 한도 초과: §4.1의 3·4·8·9 중 하나라도 실패하면 사람 카드(사유 코드 `mandate_*`/`sleeve_*`). 거절이 아니라 강등(sleeve 게이트와 같은 원칙, `gate.py:1-6`).
- 위임장 변경: 새 sleeve 버전 + 새 조항 행. CLI는 운영자 actor만, 시스템 actor 거부. 변경 직후 기존 `approved` 계획의 처리는 §7 Q3.
- 모의 → 실전 승격: 스위치·allowlist 설정 = env 변경 = 기존대로 배포 때 내용 기준 승인(결정 §3).

### 5.4 2c — 낙폭 −25% 동결 (측정은 hk 4900)

auto_trader-design 결정(2026-11-16, `consult/2026-11-16/at-mandate-design-r3` 답): 낙폭 측정 — 바구니 자본, 전역 래치, 비용 출처 — 은 이 문서에서 확정하지 않고 별도 설계 **hk 4900**이 정한다. 이 문서는 요구 조건과 인터페이스만 둔다.

요구 조건(결정 2c, 4900이 지켜야 할 것):
- 바구니 낙폭 −25%, 전체 자산 낙폭 −25%에서 신규·추가 매수를 동결한다. 강제 매도 없음. 보유·매도·취소·대사·H6 보호는 그대로.
- 동결은 사람 카드로의 강등이 아니라 매수 **차단**이다(일반 Telegram 승인도 분류기 없이 `revalidate_and_submit`로 가므로, `app/services/order_proposals/telegram_callback.py:879`, `app/services/order_proposals/revalidation.py:1361`).
- 측정 불가는 0이 아니라 동결이다. 해제는 운영자 기록으로만 한다.
- 범위(어느 매수 경로·계좌·통화, 비용 출처, 해제 기록)는 4900이 정한다.

인터페이스(이 문서가 의존하는 유일한 지점):
- 단일 판정 함수 `buy_freeze_state(session, *, account_mode, market, sleeve_id | None, now) -> FreezeVerdict(frozen: bool, reason: str, evidence: dict)`. 판정 실패는 `frozen=True`.
- 계획 경로의 소비 지점: 회차 제안 생성(I11 거부)과 dispatch 계획 게이트(§4.1 조건 9). 그 밖의 매수 경계(untagged 제안, 직접 매수·정정 도구)에 거는 일은 4900의 범위다.
- **4900이 머지되기 전에는 계획 결속 자동승인(T5)을 활성화하지 않는다.** T5는 4900에 blocked이고, 그 전까지 §4.1 조건 9는 참이 될 수 없다.

## 6. 알림·관측

- 자동 집행 알림: 기존 자동승인 카드 경로를 그대로 쓴다 — notices 목적지(`app/core/config.py:1081`, `auto_digest.py:150-155`), veto 버튼, 라운드 안에서는 다이제스트(`dispatch.py:1634`). 카드에 계획 id·회차·위임장 버전·조항 해시·매도 1호가·지정가·프리미엄 bp를 싣는다.
- 체결 알림: 기존 체결 알림(hk 2834 `resolve_fill_intent`)이 rung을 찾으므로 계획 회차는 rung → 제안 → 계획으로 이어진다. 슬리피지 기준가는 지정가.
- 감사: 분류기 결정은 기존 `auto_approve_audit` 어휘에 `mandate_*` 사유를 더해 기록. 계획 이벤트 테이블이 회차별 감사 기록이다.
- 실패 시 동작:
  - 계획 게이트 읽기 실패 → 사람 카드(`mandate_unreadable`), dispatch 트랜잭션은 savepoint로 보존(`dispatch_gate.py:144-156`와 같은 패턴).
  - 알림 발송 실패 → 기존 standalone/다이제스트 실패 처리(attempt fence, 브로커 취소 보상, operator alert)를 그대로 따른다(`approval-and-shadow-contracts.md` ROB-1052 절). 새 재시도 없음.
  - 전송 결과 불명 → 기존 상태머신의 `unverified`/`uncertain`. 회차는 `tranche_proposed`에 머물고 I9가 같은 회차 재제안을 막는다. 재전송 없음.
  - 체결 투영 실패 → 회차 `tranche_proposed` 유지, 다음 대사가 재시도. 원장은 권한자.
- 관측 지표(retro/v1이 읽을 것): 회차별 트리거가 → 지정가 → 체결가 슬리피지, 체결률, 사람 카드 강등 사유 분포. 새 MCP 도구는 만들지 않는다.

## 7. 결정이 필요한 점

| # | 질문 | 선택지 | 권고안 | 근거 |
|---|---|---|---|---|
| Q1 | 위임장 조항을 어디에 두나 | A sleeve 버전에 열 추가 / B 버전에 1:1 결속된 별도 append-only 테이블 | **B** | 기존 sleeve 트리거·store·리포트를 건드리지 않는 추가형 변경(§3.1). |
| Q2 | 낙폭 동결 측정·해제 | — | **hk 4900에서 정한다** | §5.4 범위 축소 결정. |
| Q3 | 위임장 변경 뒤 기존 승인 계획 | A 새 버전에 그대로 승계 / B 다음 아침 묶음에서 재승인(`terms_rebound` 이벤트, 해시가 같아도) / C 금액·규칙이 바뀐 조항만 재승인 | **B** | 단순하고 계획이 항상 현재 조항 아래 있다. 하루 지연은 가치 진입에 병목이 아니다(자문 §10). |
| Q4 | 묶음 승인 마감 뒤 승인 | A 당일 유효 / B 다음 거래일부터 | **B** | "장 시작 전" 결정 문언. 장중 승인을 허용하면 아침 묶음의 의미가 없다. |
| Q5 | 1b 4주 뒤 | A 이 설계에 자동 경로를 미리 만든다(스위치 off) / B 4주 재검토 결과를 보고 별도 설계 | **B** | 결정 1b가 "이후 재검토". 미리 만든 해제 경로는 쓰이지 않는 완화 표면이다. |
| Q6 | 모의 검증 장소 | A 키움 mock / B KIS mock / C 둘 다 | **A 먼저, 이후 C** | 키움 mock 제안 집행이 hk 4732·4754로 이미 있다(`approval-and-shadow-contracts.md` 키움 절). sleeve 조합은 T1m으로 추가. KIS mock은 제안 조합(`service.py:358`)이 생긴 뒤. |
| Q7 | 바구니 ① 계좌·시장을 ROB-1460 live allowlist에서 빼나 | A 뺀다 / B 넣는다 | **A** | 바구니 ①의 유일한 손실 매도는 논지 훼손(자문 §2.1). 가격 손절이 같이 돌면 물타기 계획과 충돌한다. |
| Q8 | 아침 묶음·트리거·낙폭 계산 정기 실행 | A scheduleless CLI만 / B Dagu 제안 파일까지(미등록) | **A**, 정기 실행은 카나리 뒤 별도 승인 | 하드룰 6. |
| Q9 | Upbit 소액 실험(2026-11-16 결정 1)의 자동 집행 | A 이 바구니 모델(계획 대신 신호 결속)로 같이 / B 별도 설계 | **B** | 추세 신호는 분할 계획이 아니다. 같은 sleeve·알림 인프라는 쓰되 결속 대상(신호 행)이 다르다. |
| Q10 | 근접가 프리미엄 N | 10 / 30 / 50 bp | **30 bp** (조항 값, 운영자 확정) | 자문 §2.1 "+0.3% 이내". 모의 슬리피지 실측 뒤 조정. |
| Q11 | 계획 결속 주문이 전역 daily cap을 공유하나 | A 공유 / B 바구니 전용 cap만 | **A** | 캡을 넓히지 않는다(하드룰 4). 1차 분할 총액(약 280~470만)은 KR daily cap 500만 안. |

## 8. 티켓 분해

원칙: 티켓 하나 = PR 하나. tracer bullet 순서는 "모델 → 계획 등록 → 사람 카드로 끝까지 → 자동승인 해제" — 자동승인 없이도 계획 경로가 사람 카드로 끝까지 도는 상태를 먼저 만든다.

| # | 티켓 | 내용 | 선행 | T | grade | surface floor | 마이그레이션 |
|---|---|---|---|---|---|---|---|
| T1 | 위임장 조항·계획 모델 | §3의 네 테이블, 트리거, `order_proposals` 두 열, 조건부 GRANT, downgrade 거부, store(유일 writer), 순수 불변식 함수(I1~I9) | — | T2 | A | DB 가드 | **있음** |
| T2 | 위임장·계획 CLI | `scripts/mandate.py`: 조항 생성(운영자 actor만), 계획 등록(proposed), preview 기본 `--commit`, 상태 조회 | T1 | T2 | B | 운영자 CLI | 없음 |
| T3 | 아침 묶음 승인 카드 | `mandate_plan_batches`(+멤버) 불변 스냅샷, 발송 CLI, 주문 경로와 분리된 nonce 결속 콜백(계획 이벤트만 append, 주문 모듈 import 금지 정적 테스트) | T1, T2 | T2 | A | Telegram 콜백(새 경로, 기존 콜백 불변) | **있음** |
| T1m | 모의 sleeve 조합 | 키움 체결 귀속을 계좌·거래일·주문번호·rung idempotency 키로 매칭(키움 주문번호는 계좌·거래일 안에서만 유일, `app/models/review.py:874-878`; 지금 로더는 KIS만 거래일을 붙이고 `execution_ledger/supersede.py:104-106`은 키움 거래일이 없다 — `store.py:599-605`, `:661-690`), 모호한 체결은 미측정. 그리고 sleeve 계좌 조합 DB CHECK와 런타임 어휘(`SLEEVE_ACCOUNT_MARKETS`, `app/models/strategy_sleeves.py:35-41`; `create_sleeve` 검사, `app/services/strategy_sleeves/store.py:303-308`)에 `kiwoom_mock`/`equity_kr` 추가, `LEDGER_IDENTITY`에 `kiwoom_mock → (kiwoom, mock)` 추가(`vocabulary.py:126-135`의 동치 단언 유지, `store.py:599` 로더), 키움 모의 원장·체결 귀속. live 조합·게이트 불변 | T1 | T2 | A | DB CHECK·sleeve 어휘 | **있음** |
| T4 | 계획 결속 제안 + 계획 게이트(사람 카드만) | `order_proposal_create(mandate_plan_id, tranche_index)`, I15 트리거·재비교, dispatch의 계획 게이트(I4·I8·I9·I12·I16, 강등만), 회차 이벤트(부분 체결 포함), 원장 체결 투영 CLI | T1 | T2 | A | dispatch(강등만, 완화 없음) | 없음 |
| T4b | 매도 1호가 소스 | venue별 호가 읽기(관측 시각·신선도 상한·fail-closed), 계획 게이트와 분류기에 입력 | T4 | T2 | A | 읽기 전용 브로커 시세 | 없음 |
| T5 | **분류기·preview 해제(게이트 완화)** — **hk 4900에 blocked** | `mandate_binding` + `marketable_not_resting` 해제, KIS·Toss preview/submit `allow_mandate_bound_buy`, 파킹 면제 비적용, 스위치 3개(기본 off), deviation record, 정적 테스트(스위치 off면 바이트 동일) | T4, T4b, hk 4900 | **T3** | S | **자동승인·주문 게이트** | 없음 |
| T6 | 낙폭 동결 | 설계·구현은 **hk 4900**. 이 문서는 `buy_freeze_state` 인터페이스만 요구한다(§5.4) | hk 4900 | — | — | — | 4900에서 |
| T7 | 2·3차 트리거 | 가격·시간 조건 판정(I5·I6·I7) → armed → 제안 생성. scheduleless CLI | T4 | T2 | A | 제안 생성만 | 없음 |
| T8 | 논지 훼손 매도 제안(1b) | `thesis_broken` 이벤트, 기존 `exit_intent='loss_cut'` 사람 승인 의식으로 제안(rationale에 계획·이벤트 id). 새 exit intent 없음 | T4 | T2 | A | 제안 생성만(완화 없음) | 없음 |
| T9 | 알림·관측 | 카드에 계획·회차·조항 해시, audit `mandate_*` 사유, retro/v1 읽기 연결 | T4 | T2 | B | 알림 | 없음 |

Blocking: T1 → (T1m, T2, T4) → T3(T2 필요) / T4b·T7·T8·T9(T4 필요) → T5(T4·T4b·hk 4900 필요). T5는 hk 4900의 동결 판정이 머지되고 T4가 키움 모의에서 사람 카드로 끝까지 돈 뒤에만 머지한다. 실전 활성화는 어떤 티켓의 머지로도 일어나지 않는다(§4.5).

## 9. 알았지만 건드리지 않은 것

- 결정 §5-1: "11-02 KR 물타기 중단 결정이 정책 YAML에 아직 없다" — `buy.underwater_support_net`(`config/trading_policy.yaml:891`)은 이 문서에서 끄지 않는다. 결정 §1-3의 "신규 매수·물타기 티어 끄기"를 정책에 반영하는 일은 별도 티켓이다.
- 정책 `portfolio.sector_cluster_cap_pct`는 advisory(fail-open)다(`data-model-and-symbol-contracts.md` ROB-646). 위임장의 업종 상한 35%는 그것과 별개의 강등 판정이며 전역 advisory를 바꾸지 않는다.
- 현금 규칙 B(대기금 계산·파킹 매도)는 위임장과 연결되지만(남은 분할 몫 = 계획 테이블에서 계산 가능) 이 설계 범위 밖이다. 계획 테이블이 그 입력을 제공할 수 있다는 것만 적는다.
- 위임장 조항의 per-order/daily cap이 전역 cap 이하인지는 DB CHECK로 강제할 수 없다(전역 값이 YAML). store 검증과 분류기의 min()으로 이중 보호한다.
- Toss 같은 종목 반대 방향 대기 주문 거부(결정 수정 1)는 계획이 한 계좌에만 있는 I1로 대부분 피하지만, 같은 계좌의 기존 보유 trim 매도(`sell.trim_preplace`, 11-16 결정 4로 유지)와 회차 매수가 같은 종목에서 겹치면 Toss가 거부할 수 있다. 편입 종목의 trim 처리는 별도로 다룬다.
- 데이터 소스 혼용(결정 수정 3)·브로커 API 재확인(수정 4)은 이 설계와 무관하다.
