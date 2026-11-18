# #3795 — 위임장(mandate): 바구니별 위임장 버전 승인과 계획 결속 자동승인 설계

hk 4881이 요구한 설계 문서이고, 상위 작업은 hk 3795다. 이 판은 hk 5272의 개정판이다: dots 독립 리뷰(hk doc `review/2026-11-16/dots-at-mandate-design`)의 수정 권고와 운영자 답(hk doc `decision/2026-11-18/at-3794-3795-recommended`)을 반영했다. 리뷰 항목별 반영 위치는 §10에 있다.

기준 SHA: 원래 문서의 `path:line` 인용은 `auto_trader@742d8a772` 기준이다. 개정에서 새로 인용한 사실은 줄마다 `auto_trader@ac4f9ed5c path:line`으로 적었다. 원래 문서가 인용한 파일은 두 SHA 사이에 바뀌지 않았다(`git diff --stat 742d8a772 ac4f9ed5c -- <원래 문서의 인용 경로>` 출력 없음, 2026-11-19 확인). 그래서 SHA 표기가 없는 인용은 두 SHA 어느 쪽에서 읽어도 같은 줄이다. 개정이 새로 인용한 `docs/agent-contracts/broker-and-ledger-contracts.md`는 그 사이에 바뀌었으므로 `ac4f9ed5c`의 줄 번호로만 읽는다.

이 문서는 설계 문서이며 코드·마이그레이션·정책 YAML·스케줄러는 바뀌지 않는다. 운영자 답(2026-11-18)의 "이 결정이 주지 않는 것"을 그대로 따른다:

> 3795: 구현·마이그레이션·자동승인 완화·실전 활성화·주문 승인 아님(하드룰 4, deviation record 필요).

이 문서의 "결정됨"은 설계 선택지가 정해졌다는 뜻이다. 각 구현 PR, 운영 마이그레이션, 스위치 설정, 위임장 버전 승인, 주문은 저마다 기존 절차의 승인을 따로 받는다.

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
- `report/2026-11-12/at-3792-holdings-classification`: 바구니 ① 편입 4종목 — 삼성전자(005930, 계획은 Toss), 현대차(005380, Toss), 기아(000270, KIS), 우리금융지주(316140, KIS). 승인 요청 4: 물타기 티어는 계속 끄고 KR·US 신규 매수는 바구니 ① 계획으로만 연다. 이 보고서는 승인 **요청**이다. 승인 기록은 `decision/2026-11-12/at-b02-reconcile-and-3792` §hk 3792 항목 1(편입 4종목을 바구니 ① 계획 대상으로 둔다. 실제 분할 매수는 백테스트·사전등록·위임장 승인 뒤)과 항목 4(기존 물타기 티어는 계속 끈다. KR·US 신규 매수는 바구니 ① 계획으로만 연다. 계획 결속 자동승인은 별도 구현·승인)다. 이 문서가 "3792 결정"이라고 부르는 것은 그 결정 문서다.
- `decision/2026-11-18/at-3794-3795-recommended` §hk 3795: 리뷰 §6의 Q1~Q13 권고안을 전부 채택(§7). 다음 단계는 이 개정 PR 1건, 머지 뒤 구현 티켓 분해.
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
- Upbit 소액 실험(2026-11-16 결정 1)은 건별 승인 카드로 이미 열린다. 그 자동 집행(신호 결속)은 이 문서가 설계하지 않는다. 별도 설계다(§7 Q9, 결정됨 B).
- 미결 주문의 자동 취소와 보유의 자동 매도. 계획을 폐기해도 이 설계는 주문을 취소하거나 보유를 팔지 않는다(§3.7).
- 낙폭 측정의 세부(§5.4, hk 4900), 현금 규칙 B의 구현, 결정 §1-3(신규 매수·물타기 티어 끄기)의 정책 반영. 셋 다 실전 활성화의 선행 조건으로만 연결한다(§4.5 체크리스트).

### 1.3 지키는 하드룰

| 규칙 | 이 설계의 처리 |
|---|---|
| 하드룰 4 — 브로커 실행 표면은 default-disabled, 게이트 완화 금지 | 계획 결속 자동승인은 **게이트 완화다**. 새 스위치 `MANDATE_AUTO_APPROVE_ENABLED`(기본 false) + 시장 allowlist + 계좌 allowlist + `ORDER_PROPOSALS_AUTO_APPROVE_MODE=expanded` + 위임장 버전의 `state='live'` + 인증된 승인 기록이 있는 조항(§3.5) + 그 조항에 결속된 활성화 프로필과 그 프로필의 회차 상한(§3.6)이 모두 참일 때만 자동승인이 열린다(D2·D3). preview·submit의 근접가 허용(D1)은 스위치와 allowlist 뒤에 있고 승인 방식과 무관하다(§4.4 "D1의 범위"). 완화 목록 전부를 §4.4에 적고 구현 PR에 deviation record를 붙인다. 레버리지·per-order cap·daily cap 상수는 올리지 않는다. 운영자 결정 없이 활성화하지 않는다. 계획 결속 제안을 만들 수 있는 계좌도 따로 닫아 둔다(`MANDATE_PLAN_PROPOSAL_ACCOUNT_MODES`, 기본 빈 값, §4.2). |
| 하드룰 6 — 스케줄러 등록 금지 | 아침 묶음 승인 카드 발송·트리거 감시·낙폭 계산은 전부 scheduleless CLI/수동 레버로 출고한다(§7 Q8, 결정됨 A). 실행이 빠지면 권한이 늘지 않고 줄어든다: 카드가 없으면 승인도 없고, 트리거 스캔이 없으면 armed 회차도 없고, 낙폭 측정이 stale이면 동결이다(§5.4). 정기 실행은 카나리 뒤 별도 승인이다. |
| 하드룰 19 — 기계식 손절(ROB-1460) | 논지 훼손 손실 매도(1b)는 ROB-1460과 **다른 부류**다. ROB-1460의 결정 행 결속 경로·스위치·캡을 재사용하거나 넓히지 않는다. 1b는 이 설계에서 언제나 사람 카드다. 처음 4주 뒤의 재검토 결과는 별도 설계로만 반영한다(§5.2). ROB-1460 스위치는 기본 off 그대로이고, 바구니 ①을 기계식 손절에서 빼는 방식이 계좌 전체에 미치는 영향은 §5.2의 표다. |
| 하드룰 18 — #789 DB 역할 | 새 테이블마다 같은 마이그레이션에 조건부 `at_app` GRANT(SELECT·INSERT만, UPDATE·DELETE 없음). ROB-1470 선례 `alembic/versions/20261102_rob1470_strategy_sleeves.py:329-332`. |
| 하드룰 5 — 레저 쓰기는 서비스 경유 | 계획 상태의 "체결" 전이는 `execution_ledger_in_effect()` 의 authoritative 체결 증거로만 한다. 계획 테이블은 원장이 아니며 원장에 쓰지 않는다. |
| 하드룰 9 — 검증 규율 | 각 티켓은 변경 범위 테스트를 돌리고 DB 트리거·경쟁 테스트는 CI가 증거다. |

### 1.4 용어

- **바구니**: 전략 하나의 돈 묶음. 구현 단위는 ROB-1470 sleeve 하나(계좌 × 시장 × 통화)다. 바구니 ①이 Toss KR과 KIS KR에 걸치면 sleeve가 둘이다. 그 둘을 묶는 것이 조항의 `basket_key`다. 바구니의 정체성은 **(영역, `basket_key`)**다 — 영역은 실계좌(`live`) 또는 모의(`mock`)이고, 같은 `basket_key`라도 영역이 다르면 다른 바구니다. 예산·업종 상한·바구니 누계·낙폭·아침 배치·바구니 lock이 모두 이 키로 합친다(§3.8, §4.6, §5.4).
- **위임장 버전**: sleeve 버전 하나 + 그 버전에 결속된 위임장 조항(§3.1). 운영자가 승인하면 `live`.
- **분할 계획(plan)**: 한 종목·한 계좌의 계획 금액과 회차별 트리거. 회차(tranche) = 1차·2차·3차.
- **계획 결속 제안**: `order_proposals` 행이 계획 회차 하나를 가리키는 제안(§4.1).
- **승인 기록**: 인증된 운영자 액션(nonce 결속 Telegram 콜백) 하나가 남기는 append-only 행. 조항과 활성화 프로필은 내용 해시가 일치하는 승인 기록이 있어야 효력이 있다(§3.5).
- **활성화 프로필**: 자동승인을 어느 조항 버전에, 몇 차 회차까지 여는지 적은 승인된 기록(§3.6). 스위치(env)와 별개이고 둘 다 필요하다.
- **전역 누계 / 바구니 누계**: 전역 누계는 기존 자동승인 일 누계(계좌 × 시장 × KST 일)이고, 바구니 누계는 같은 `basket_key`의 모든 sleeve를 합친 계획 결속 자동승인 일 누계다(§4.6).
- **강등 / 차단**: 강등은 자동승인만 못 하고 사람 카드로 가는 것(사람이 그 제안을 승인하면 전송된다). 차단은 사람이 승인해도 전송하지 않는 것(복구 뒤 새 제안이 필요하다). 실패별 분류는 §4.7 한 표가 정본이다.
- **편입액**: 계획 등록 시점에 그 계획의 보유 범위(실계좌 계획이면 KIS·Toss 두 실계좌 합계, 모의 계획이면 그 모의 계좌)가 이미 들고 있는 그 종목의 금액. 계획 금액에 포함된다(§3.7, §3.8).
- **계좌 참조(`account_ref`)**: 브로커 조회로 확인한 실제 주문 계좌의 불투명한 참조. 조항과 계획에 고정되고, 전송 직전에 실제 송신 요청의 계좌와 대조한다(§4.6 "계좌 결속").

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

### 2.3 개정에서 새로 확인한 사실

전부 `auto_trader@ac4f9ed5c` 기준이다. 리뷰 R1~R5와 §3 표가 묻는 지점을 코드에서 다시 읽은 것이다.

| # | 무엇 | 위치 (`auto_trader@ac4f9ed5c`) | 구분 | 이 설계에서의 뜻 |
|---|---|---|---|---|
| F1 | 자동승인 일 누계의 범위는 `account_mode` × `market` × `broker_account_id` × KST 일이다. 같은 키의 advisory lock을 잡고 읽는다 | `app/services/order_proposals/service.py:4050-4078` | 사실 | "전역 daily cap"은 계좌별이다. KIS KR과 Toss KR은 누계가 따로다(§4.6). |
| F2 | 누계에 들어가는 행은 `source_asof.auto_approved`가 있는 제안의 rung이고 `cancel` 제안은 뺀다. rung 상태(체결·취소 여부)는 거르지 않는다 | `app/services/order_proposals/repository.py:255-313`, `:429-476` | 사실 | 누계는 "그날 자동화가 약정한 금액"이다. 미체결·취소돼도 돌려주지 않는다. |
| F3 | `auto_approved` 스탬프는 브로커 호출이 끝난 뒤, 모든 rung 결과가 `submitted_acked`/`submitted_resting`일 때만 기록되고 그 뒤에 커밋된다 | `app/services/order_proposals/dispatch.py:184-188`, `:1357-1375`, `:1455` | 사실 | 스탬프는 전송 **뒤** 기록이다. |
| F3a | 그래서 전송 결과가 불명(`unverified`)인 주문은 이 누계에 안 잡힐 수 있다 | F3에서 유도 | 추정 | 다른 경로가 보정하는지는 감사하지 않았다. 계획 결속은 스탬프에 기대지 않고 전송 **전** fence를 따로 둔다(§4.6). |
| F4 | dispatch의 lock 순서: 제안별 dispatch lock(`order_proposals:auto_dispatch:<proposal_id>`) → sleeve lock(sleeve 게이트 안) → 자동승인 계좌·일 lock(누계 읽기). 셋 다 트랜잭션 끝까지 잡는다 | `app/services/order_proposals/dispatch.py:1079`, `:1171-1191`, `app/services/order_proposals/service.py:4145-4149`, `app/services/strategy_sleeves/store.py:139-144`, `app/services/order_proposals/repository.py:494-498` | 사실 | 새 lock은 이 순서의 정해진 자리에만 끼운다(§4.6). |
| F5 | sleeve 사용액 = 보유 원가 + 열린 매수 주문 + 원장에 아직 없는 체결 수량. sleeve 자기 rung의 체결만 귀속한다 | `app/services/strategy_sleeves/book.py:16-24`, `:234` | 사실 | 미결 매수는 예산에 이미 들어간다. 계획 이전부터 있던 보유(편입액)는 sleeve 사용액에 없다(§3.8). |
| F6 | 분류기의 가격·수량 유효값 검사: `current_price`·`limit_price`·`quantity` 중 하나라도 없거나 유한한 양수가 아니면 `price_or_quantity_missing`으로 거부. 비유한값은 파싱 단계에서 없음으로 처리 | `app/services/order_proposals/auto_approve.py:295-300`, `:869-908` | 사실 | 계획 결속에도 그대로 적용(§4.4의 독립 행). |
| F7 | `toss_live` preview에 `current_price`가 없을 때만 KIS 시세를 한 번 읽어 **`current_price` 입력만** 대체한다 | `app/services/order_proposals/auto_approve.py:871-891`, `docs/agent-contracts/approval-and-shadow-contracts.md:203`("자동승인 가격 폴백 (#1067)" 절) | 사실 | 이 폴백은 매도 1호가를 만들지 않는다. 매도 1호가는 대체 금지다(§4.1 조건 6). |
| F8 | sleeve 상태 변경은 기존 행 수정이 아니라 새 버전 append다. 그리고 상태·예산·손실 한도·사전등록이 모두 직전과 같으면 store가 버전을 쓰지 않고 거부한다 | `app/services/strategy_sleeves/store.py:353-424`(거부는 `:405-411`) | 사실 | `shadow`→`live`는 새 버전 행이다. 조항은 live 버전 행에 결속돼야 한다. 조항만 바뀌는 위임장 변경은 지금 함수로는 버전을 못 올리므로 조항을 동반한 append 함수를 따로 둔다(§3.5). |
| F9 | ROB-1460 live 스위치 판정은 스위치 AND 시장 목록 AND 계좌 목록이다. 시장 목록과 계좌 목록은 독립된 두 CSV라서 허용 범위는 둘의 곱집합이다. live 스위치·시장 목록·계좌 목록과 shadow 스위치 모두 기본 off/빈 값 | `app/services/mechanical_loss_cut/gate.py:54-65`, `app/core/config.py:1141-1144` | 사실 | 한 계좌의 한 시장만 빼는 설정은 없다(§5.2 영향 표). |
| F9a | 스위치 판정을 통과해도 live로 나가려면 그 시장의 `thresholds_confirmed`가 참이어야 하고, backfilled 계획(손절가를 명시한 매수 없이 정책 기본값으로 채운 lot)은 `live_include_backfilled`가 참일 때만 대상이다. 지금 정책은 kr·us·crypto 모두 두 값이 false다. 손절을 명시하지 않은 매수는 lot의 backfilled 표시를 지우지 못한다 | `app/services/mechanical_loss_cut/gate.py:68-79`, `config/trading_policy.yaml:1846`·`:1866`(kr), `app/services/mechanical_loss_cut/plans.py:168-169`, `docs/agent-contracts/broker-and-ledger-contracts.md:598` | 사실 | lot 단위 구분이 있지만 바구니 ①을 빼 주지는 않는다(F9b, §5.2). |
| F9b | lot exit plan 기록이 켜져 있으면, 이전 계획도 보유도 없는 종목의 첫 매수는 손절가를 명시하지 않아도 정책 기본 손절가로 `backfilled=False`인 `entry` 계획을 받는다. backfilled로 남는 것은 계획 없이 이미 보유 중이던 lot에 손절을 명시하지 않고 더 산 경우뿐이다 | `app/services/mechanical_loss_cut/plans.py:157-195`, `app/services/mechanical_loss_cut/rules.py:400-432`, `app/core/config.py:1140` | 사실 | 바구니 ①의 신규 종목 lot은 backfilled가 아니다. `live_include_backfilled=false`로는 제외되지 않는다. |
| F12 | sleeve 생성은 `rule_id`가 `CANARY_RULES`에 있을 때만 된다. 그 표에는 지금 ROB-1458 규칙만 있다 | `app/services/strategy_sleeves/store.py:303-306`, `app/services/strategy_sleeves/vocabulary.py:60-` | 사실 | 바구니 ①의 sleeve를 만들려면 규칙 id를 그 표에 더해야 한다(T1). |
| F10 | KRX 호가 단위 표(주식): 2,000원 미만 1원, 5,000원 미만 5원, 20,000원 미만 10원, 50,000원 미만 50원, 200,000원 미만 100원, 500,000원 미만 500원, 그 이상 1,000원. 매수는 내림 | `app/mcp_server/tick_size.py:75-101`, `:119-135` | 사실 | 2,000원 이상 종목에서 호가 단위는 가격대 경계에서 가격의 최대 0.25%(25bp)다(표에서 계산). 그래서 30bp 상한의 실효 프리미엄이 종목 가격대에 따라 달라진다(§7.1 E4). |
| F11 | 자동 dispatch는 pending rung이 둘 이상이면 사람 승인으로 보낸다 | `app/services/order_proposals/revalidation.py:1283-1292` | 사실 | "회차 1개당 rung 1개"(§4.1 조건 5)와 맞는다. |

## 3. 데이터 모델 제안

### 3.1 (a) 위임장 = sleeve 버전에 무엇을 더하는가

결정됨(Q1 = B, 운영자 2026-11-18): sleeve 버전 테이블에 열을 더하지 않고, **버전에 1:1로 결속된 append-only 조항 테이블** `review.mandate_terms`를 둔다. 이유: sleeve 버전은 이미 dispatch 게이트·리포트·트리거가 읽는 안정된 표면이고, 열 추가는 그 트리거(`20261102_rob1470_strategy_sleeves.py:123-130`)와 store 검증을 함께 고쳐야 한다. 별도 테이블은 추가형 마이그레이션 하나로 끝난다. 운영자 답의 조건은 "인증된 승인 기록 결속"이고 그 계약은 §3.5다.

`review.mandate_terms` (sleeve 버전당 최대 1행, `UNIQUE(sleeve_version_id)`):

| 열 | 형 | 의미 |
|---|---|---|
| `sleeve_version_id` | FK → `strategy_sleeve_versions.id` | 이 조항이 속한 위임장 버전 |
| `terms_version` | text | 조항 스키마 버전(`mandate-terms/v1`) |
| `basket_key` | text | 바구니 이름(예 `basket-1`). 같은 바구니의 sleeve(KIS KR, Toss KR …)가 같은 값을 갖는다. 바구니의 정체성은 (영역, `basket_key`)이고 영역은 그 sleeve의 계좌 모드에서 정해진다(실계좌 `live` / 키움 모의 `mock`). 모의 sleeve와 실계좌 sleeve가 같은 `basket_key`를 써도 **서로 다른 바구니**이고 어떤 집계에서도 합치지 않는다. 바구니 예산·업종 상한·누계·낙폭은 이 정체성으로 합산한다. **한 바구니는 한 시장·한 통화만**(같은 영역·`basket_key`의 조항은 sleeve 시장과 통화가 같아야 함, store가 거부) — 환산 없이 합산하기 위해서다. KR과 US는 다른 바구니다 |
| `account_ref` | text | 이 sleeve의 주문이 실제로 나가는 계좌의 불투명한 참조(§4.6 "계좌 결속"). 승인 요청을 만들 때 브로커 조회로 확인한 값이고 승인 내용 해시에 들어간다. 원문 계좌번호는 저장하지 않는다 |
| `universe_rule` | text(≤500) | 대상 종목 조건(사람이 읽는 문장 + 규칙 id) |
| `allowed_symbols` | text[] | 이 버전이 계획을 받을 수 있는 종목(정규화 심볼, `app/core/symbol.py` 경유). 비면 계획 등록 불가 |
| `entry_max_premium_bps` | numeric | 근접가 상한: 지정가 ≤ 매도 1호가 × (1 + bp/10000), 호가 단위로 **내림**한 값. 30bp는 시험 후보이고 실전 값은 측정 증거 뒤 운영자가 정한다(§7 Q10). 손실 한도가 아니다 |
| `per_order_cap` | numeric | 이 sleeve의 주문당 상한. 전역 `per_order_cap` 이하만(CHECK는 못 하므로 store가 검증) |
| `daily_cap` | numeric | **바구니** 하루 상한(같은 `basket_key`의 모든 sleeve 합산, §4.6). 전역 `daily_cap` 이하만 |
| `sector_cap_pct` | numeric(0~100] | 업종 상한(2b: 35). 분자·분모는 §3.8 |
| `basket_drawdown_freeze_pct` | numeric(0~100] | 바구니 낙폭 동결(2c: 25) |
| `account_drawdown_freeze_pct` | numeric(0~100] | 전체 자산 낙폭 동결(2c: 25) |
| `tranche_rule` | jsonb(닫힌 스키마) | 회차 비중·트리거·최소 간격 기본값. 2b: `{"weights":[0.4,0.3,0.3],"triggers_pct_from_first":[0,-15,-30],"min_gap_sessions":20}` |
| `loss_sell_mode` | text | `mandate-terms/v1`은 `human_card` 한 값만 받는다(CHECK). 자동 경로는 이 설계에 없다(§5.2, Q5 = B) |
| `expires_at` | timestamptz | 이 위임장 버전의 만료. 지나면 자동승인 대상 아님(사람 카드) |
| `approval_id` | FK → `mandate_approvals.id`, UNIQUE | 이 조항 행을 만든 인증된 승인 기록(§3.5). NOT NULL — 승인 기록 없이는 조항 행이 생기지 않는다 |
| `content_sha256` | text | 승인된 내용의 해시. 입력은 **내용 열만**이다: `terms_version`부터 `expires_at`까지의 조항 열, `sleeve_id`, 그리고 그 버전의 sleeve 값(예산·손실 한도·사전등록 4필드). 행 식별자(`sleeve_version_id`·`approval_id`)와 이 열 자신은 넣지 않는다 — 승인 요청을 만들 때는 아직 없는 값이다. 승인 요청의 `content_sha256`과 같은 값이다(§3.5). 승인 기록·활성화 프로필·계획·제안이 이 해시를 인용한다 |

규칙:
- 위임장 변경 = 새 sleeve 버전 + 새 조항 행(1c). 기존 조항은 수정할 수 없다.
- `state='live'` 버전에 조항 행이 없으면 계획 결속 자동승인은 열리지 않는다(sleeve 게이트만으로는 사람 카드). 손실 한도 게이트가 붙이는 `stopped` 버전(`system:sleeve_loss_gate`)이나 운영자가 조항 없이 붙인 버전에는 조항이 없다. 다시 여는 것은 새 `live` 버전 + 새 조항 + 새 승인 기록이다.
- 조항 금액은 운영자만 정한다. 코드·정책에 기본 금액 없음(ROB-1470 원칙과 같음).
- 바구니 단위 값(`daily_cap`·`sector_cap_pct`·두 낙폭 동결 값)은 같은 `basket_key`의 조항들 사이에서 같아야 한다. 그런데 버전은 sleeve마다 따로 올라가므로 바꾸는 도중에는 값이 잠깐 다를 수 있다. 그래서 store는 불일치를 거부하지 않고, 판정은 그 바구니의 현재 live 조항들 중 **가장 엄격한 값**(cap은 최소, 동결은 가장 얕은 낙폭)을 쓰며 불일치 사실을 감사 기록에 남긴다(`mandate_basket_terms_mismatch`, 정보용). 낙폭 값의 사용은 hk 4900이 정한다.

### 3.2 (b) 종목별 분할 계획 테이블

두 테이블: 계획(불변 정체성) + 계획 이벤트(append-only 상태 전이). 행 UPDATE를 금지하므로 현재 상태는 이벤트를 접어서 얻는다(아래 `fold_plan_events`).

`review.mandate_plans`:

| 열 | 의미 |
|---|---|
| `id` | PK |
| `sleeve_id` | 바구니(→ 계좌·시장·통화가 정해진다) |
| `symbol` | 정규화 심볼 |
| `approved_terms_id` | 등록 시점 위임장 조항 행(→ 버전·해시) |
| `account_ref` | 등록 시점 조항의 `account_ref`를 옮긴 값(불변). 이 계획의 주문은 이 계좌로만 나간다. 계좌 참조가 다른 조항으로는 재승인(`terms_rebound`)할 수 없다(§4.6) |
| `scope_account_refs` | 이 계획의 **보유 범위**(§3.7)에 속한 실제 계좌 각각의 계좌 참조(불변). 실계좌 계획이면 KIS 계좌와 Toss 계좌 둘, 모의 계획이면 그 키움 모의 계좌 하나. 편입액이 0이어도 등록 때 브로커 조회로 확인해 고정한다 — 확인하지 못하면 계획을 등록하지 않는다. 주문 계좌의 `account_ref`는 이 중 하나다 |
| `planned_amount` | 계획 금액(2b: 약 235만). 그 종목의 목표 총액이며 편입액을 포함한다 |
| `adopted_amount` | 편입액(≥ 0, `planned_amount` 이하 — CHECK). 실계좌 계획이면 계획 등록 시점에 KIS·Toss **두 실계좌가 합쳐서** 이미 들고 있는 그 종목의 금액(3792 보고서의 분류 규칙과 같은 기준), 모의 계획이면 그 모의 계좌의 보유 금액(§3.7 "보유 범위"). 새 종목은 0. 운영자가 등록 때 넣고 이후 시세로 갱신하지 않는다. 산입 규칙은 §3.8 |
| `adopted_evidence` | jsonb(닫힌 키: 보유 조회 시각, 계좌 참조별 수량·금액(0도 적는다), 기준 가격과 그 출처). **항상 필수** — 편입액이 0이라는 것도 `scope_account_refs`의 각 계좌를 조회해 확인한 결과다 |
| `thesis` | 논지 메모(≤1000자, veto 카드 thesis) |
| `invalidation_conditions` | text[] ≤3, 논지 훼손 조건(반증 가능 문장) |
| `sector_key` | 업종 키(업종 상한 판정용) |
| `created_by`·`created_at` | 등록자 |
| 활성 종목 예약 | **한 종목의 계획은 한 계좌에만**(수정 3). 존재 확인 트리거는 동시 INSERT를 막지 못하므로 별도 예약 테이블 `review.mandate_symbol_reservations`(`realm`, `market`, 정규화 `symbol`, `plan_id`, `released_at` 없음 — 해제는 별도 append-only 해제 행)로 강제한다. `realm`은 계획의 계좌 모드에서 정해지는 영역이다: 실계좌(`kis_live`·`toss_live`)는 `live`, 키움 모의는 `mock`. 모의 계획이 같은 종목의 실계좌 계획을 막지 않도록 영역을 키에 넣는다(바구니 정체성과 보유 범위를 영역으로 나눈 것과 같은 구분, §3.1·§3.7). 계획 INSERT와 `released` 전이는 종목 lock(키 `mandate_symbol:<realm>:<market>:<symbol>`의 해시에 대한 `pg_advisory_xact_lock`, §4.6의 L1)을 **계좌와 무관하게** 먼저 잡고 활성 예약을 확인·기록한다(sleeve lock은 계좌별이라 직렬화하지 못한다, `app/services/strategy_sleeves/store.py:139-144`). 예약 판정은 같은 락 아래에서만 한다(§3.4). 예약은 권한 상태가 끝났다고 풀리지 않는다. `released` 이벤트와 같은 트랜잭션에서만 풀린다(§3.7) |

`review.mandate_plan_tranches` (계획당 회차 행, 계획 INSERT와 같은 트랜잭션에 생성, 불변):

| 열 | 의미 |
|---|---|
| `plan_id`, `tranche_index` (1..3) | |
| `weight` | 0.4 / 0.3 / 0.3 |
| `amount` | `(planned_amount − adopted_amount) × weight`(정확한 decimal). 새로 사는 금액만 회차로 나눈다 |
| `trigger_pct_from_first` | 0 / −15 / −30 |
| `min_gap_sessions` | 20(회차 1은 0) |

`review.mandate_plan_events` (append-only):

| 열 | 의미 |
|---|---|
| `plan_id`, `seq`(직전+1) | |
| `event` | 닫힌 어휘(아래 상태 전이) |
| `tranche_index` | 회차 관련 이벤트일 때 |
| `first_fill_amount`·`first_fill_quantity` | 1차 회차 완료 이벤트에만: 1차 체결 금액 합과 체결 수량 합(둘 다 정확한 decimal). 트리거 기준가는 이 둘의 비이고 나눈 값은 저장하지 않는다(아래 "회차 기준값") |
| `terms_id`·`terms_sha256` | `approved`·`terms_rebound` 이벤트에 필수, `rejected`·`retired`는 선택(묶음 스냅샷에서 왔을 때 기록), 그 외 NULL(CHECK). `terms_id`는 `mandate_terms` FK, 해시는 그 행과 일치해야 함(트리거) |
| `effective_session` | `approved`·`terms_rebound`·`resumed`에 필수: 승인 효력이 시작되는 거래일(시장 달력) |
| `resume_target` | `resumed`에 필수: `approved` 또는 `active` |
| `batch_member_id` | 묶음 승인에서 온 이벤트면 그 멤버 스냅샷(§5.1) |
| `proposal_id` | 회차가 제안을 만들었을 때. `tranche_proposed`·`tranche_send_fenced`에 필수. FK 제약은 두지 않는다(§4.6) |
| `notional`·`kst_day`·`approval_mode`·`account_ref`·`attempt_id`·`activation_profile_id` | `tranche_send_fenced`에 필수: 전송 금액, 그 fence가 속한 KST 일, 승인 방식(`auto`/`human`), 그 시도가 쓰는 계좌 참조(계획의 것과 같아야 한다 — 트리거), 송신 시도 id(fence 하나는 transport 시도 하나에만 쓰인다). `activation_profile_id`는 `auto`일 때만 필수(판정에 쓴 활성화 프로필 행, §3.6). 그 외 이벤트에서는 NULL(CHECK) |
| `fenced_seq` | `tranche_send_released`에 필수: 해제하는 fence 이벤트의 `seq`(같은 계획). fence 하나에 해제는 많아야 하나(트리거). 해제의 금액과 KST 일은 짝인 fence에서 읽는다 |
| `thesis_seq` | `thesis_evidence_refreshed`에 필수: 증거를 갱신하는 대상인 `thesis_broken` 이벤트의 `seq`(같은 계획). 그 외 NULL(CHECK) |
| `evidence` | jsonb(닫힌 키: 원장 행 id, 거래일, 사유 코드, 회고 id, 승인 이슈 id, 미결·불명 주문 수, 보유 수량과 조회 시각, 보유 처리 종류) |
| `actor` | 운영자(`operator:telegram:<사용자 id>` 또는 운영자 CLI actor) / `system:mandate_*` |
| `created_at` | |

계획은 축이 둘이다. **권한 상태**는 "이 계획으로 새 주문을 낼 권한이 있는가"이고, **정리 상태**는 "이 계획이 차지한 종목 예약·예산·업종 몫을 풀어도 되는가"다. 원래 문서는 둘을 하나로 봐서 `retired` 순간에 예약을 풀었다. 그러면 이미 브로커에 나간 주문이나 보유가 남은 채로 같은 종목의 새 계획을 다른 계좌에 만들 수 있다(리뷰 R2). 그래서 둘을 나눈다.

권한 상태 전이(계획 단위):

```
proposed ──(아침 묶음 승인, 1a)──▶ approved ──(1차 회차 완료)──▶ active
   │                                  │                           │
   └─(기각)─▶ rejected                └─(운영자 정지)─▶ suspended ◀───┤(운영자 정지)
                                                                  │
                         active ──(3차 회차 완료)──▶ exhausted
                         approved/active/suspended/exhausted ──(논지 훼손)──▶ thesis_broken ──(매수 주문 종결 + 계획 계좌 보유 0 확인)──▶ closed
                         any ──(운영자 폐기)──▶ retired
suspended ──(운영자 재개, 1차 미체결)──▶ approved
suspended ──(운영자 재개, 1차 체결)──▶ active
approved/active ──(위임장 변경 뒤 재승인 rebind)──▶ 같은 상태(terms_rebound 이벤트)
```

묶음 승인에서 응답 없이 만료된 `proposed` 계획은 `rejected`가 아니다. `proposed`로 남아 다음 거래일 배치에 다시 들어간다(§5.1).

- `suspended`는 운영자가 기록하는 정지뿐이다. 시스템이 계획을 `suspended`로 옮기는 전이는 없다. 낙폭 동결과 위임장 비활성(sleeve가 live가 아님, 조항 만료)은 계획 상태를 바꾸지 않는 **판정**이고, 각각 §4.7의 자기 줄(차단 `mandate_drawdown_frozen`, 강등 `sleeve_not_live`·`mandate_expired`)로 처리된다. 원래 문서의 "(위임장 비활성)·(동결) → suspended" 전이는 없앴다.
- "회차 완료"는 그 회차의 `tranche_filled` 또는 `tranche_closed_partial`이다. `exhausted`는 3차 회차 완료다.
- `thesis_broken`은 보유가 있을 수 있는 모든 권한 상태에서 기록할 수 있다: `approved`(1차가 부분 체결된 채 남았거나 편입 보유가 있는 경우 포함)·`active`·`suspended`·`exhausted`. 원래 문서는 `active`·`exhausted`만 허용해서, 1차 부분 체결분이나 편입 보유만 있는 `approved` 계획은 논지가 깨져도 손실 매도 흐름을 시작할 수 없었다. `thesis_broken` 뒤에는 그 계획의 새 매수 제안이 만들어지지 않고 새 전송 fence가 커밋되지 않는다(I16). 이미 브로커에 나간 매수 주문은 살아 있고 자동으로 취소하지 않는다(§3.7): 운영자가 기존 취소 경로로 취소하고, 취소 전에 체결되면 투영이 그 체결을 계획에 기록한다. 손실 매도 제안(T8)의 카드는 그 계획의 미결 매수 주문 수를 보여 준다. 예약·예산·업종 몫은 그대로 유지된다.
- `thesis_evidence_refreshed`는 이력에 `thesis_broken` 이벤트가 있고 아직 `released`가 아닌 계획에 쓸 수 있다 — 권한 상태가 `thesis_broken`일 때뿐 아니라 그 뒤 `retired`가 된 미해제 계획에도 쓸 수 있다. 권한 상태를 바꾸지 않는다(`retired`는 `retired`로 남고 매수 권한은 돌아오지 않는다). 최초 논지 훼손(`thesis_seq`)에 새 회고 id와 승인 이슈 id를 덧붙이는 append-only 증거 갱신이다(§5.2). 폐기 뒤에도 미결·불명 매수의 대사가 끝나기 전에는 예약이 남아 손실 매도 가드가 계속 걸리므로, 그 동안에도 증거를 갱신할 길이 있어야 한다. 계획 단위 이벤트 어휘에 든다.
- `closed`는 논지 훼손 뒤 그 계획의 매수 주문이 전부 종결됐고(대사 증거, 닫히지 않은 전송 fence 0) **계획 계좌**의 그 종목 보유가 0임을 보유 조회로 확인했을 때 투영 CLI가 쓴다(계획이 사고파는 것은 계획 계좌의 주식뿐이다). 미결 매수가 남아 있으면 보유가 0이어도 `closed`를 쓰지 않는다 — 그 주문이 체결되면 보유가 다시 생긴다. 다른 계좌에 같은 종목이 남아 있으면 `closed` 뒤에도 예약은 §3.7의 보유 처리 증거가 있어야 풀린다. 손실 매도 제안(기존 `loss_cut` 경로)에 계획 결속 열을 달지 않는다.
- 누가 쓰는가: `proposed`·`suspended`·`resumed`·운영자 `retired`·`tranche_closed_partial`, 그리고 `holdings_disposed` 가운데 운영자 기록인 종류(`kept_outside_basket`)는 운영자 CLI(T2). `approved`·`rejected`·`terms_rebound`·재승인 기각의 `retired`는 묶음 승인 콜백(T3a). `first_tranche_settled`·`exhausted`·`closed`·회차 체결 이벤트·나머지 정리 이벤트는 투영 CLI(T4b). `thesis_broken`과 `thesis_evidence_refreshed`는 T8의 판정, 또는 운영자 CLI(T2, 사유 필수). `tranche_armed`·`tranche_proposed`는 제안 생성 경로(T4a, 2·3차 arming은 T7). fence 두 이벤트는 T4c(사람 경로)와 T5(자동 경로).

계획 단위 이벤트 어휘: `proposed`, `approved`(묶음 승인, `terms_id`·`effective_session` 포함), `rejected`, `first_tranche_settled`(→active), `suspended`(사유 코드), `resumed`(운영자, 대상 상태 approved|active), `terms_rebound`(운영자 재승인, 새 `terms_id`·`terms_sha256`·`effective_session`), `exhausted`, `thesis_broken`, `closed`, `retired`, 그리고 정리 이벤트 `orders_settled`·`holdings_disposed`·`released`(§3.7). INSERT 트리거는 위 다이어그램과 §3.7의 전이만 허용한다.

**계획 상태는 이벤트 스트림 전체를 접어서(fold) 얻는다** — 마지막 이벤트 이름이 곧 상태가 아니다(예: `approved` 뒤 `terms_rebound`나 `resumed(→approved)`가 와도 상태는 `approved`). 순수 함수 `fold_plan_events`가 (권한 상태, 정리 상태, 현재 결속 조항, 효력 세션, 1차 체결 금액·수량, 회차별 체결 금액과 닫히지 않은 fence)를 돌려주고 게이트·트리거·CLI가 모두 이 함수를 쓴다.

"현재 결속 조항"은 원래 `approved_terms_id`가 아니라 **마지막 `approved` 또는 `terms_rebound` 이벤트의 `terms_id`**다. 계획 행 자체는 불변으로 두고 rebind를 이벤트로 남겨 회차·체결 이력을 보존한다(Q3).

회차 단위 이벤트: `tranche_armed`(가격·시간 조건 충족) → `tranche_proposed`(제안 생성, proposal_id) → 제안 종결 시 원장 증거로 셋 중 하나:

- `tranche_filled`: 그 회차의 누적 체결 금액이 회차 `amount`의 95% 이상(체결이 있고 잔여가 1주 미만인 경우 포함). 회차 완료, 다음 회차 arming 가능(I7).
- `tranche_partially_filled`: 0 < 누적 체결 < 95%이고 제안이 종결(DAY 만료·취소)됨. 수량·평균가를 기록한다. 회차는 **완료가 아니다** — I7은 다음 회차를 막고, 잔여(`amount` − 누적 체결)는 다음 거래일부터 같은 회차로 다시 `tranche_armed` 될 수 있다(I8이 잔여만 허용, I9가 열린 제안 1개를 보장). 회차 간 최소 간격(I6)은 회차 안 잔여 재제안에는 적용하지 않는다. 운영자는 `tranche_closed_partial`(잔여 포기, 회차 완료로 취급)을 기록할 수 있다.
- `tranche_unfilled`: 체결 0으로 종결. 다음 거래일 다시 armed 가능.

계획 결속 rung의 모든 전송(자동·사람)은 브로커 호출 전에 `tranche_send_fenced`(금액·KST 일·승인 방식)를 독립 트랜잭션으로 커밋하고, 미전송이 증명되면 `tranche_send_released`를 남긴다(§4.6).

회차 기준값(2b, 테스트 계약으로 고정한다 — 리뷰 §1 2b):

| 값 | 정의 | 정해지지 않는 경우 |
|---|---|---|
| 회차 체결 금액 | 그 회차의 제안들에 귀속된 authoritative 원장 체결의 Σ(체결가 × 수량). 수수료·세금은 넣지 않는다. websocket 단독 행은 넣지 않는다(I10) | — |
| 회차 완료 | `tranche_filled`(체결 금액 ≥ `amount`의 95%, 또는 **체결이 있고** 잔여가 그 시점 1주 값 미만) 또는 `tranche_closed_partial`(운영자 잔여 포기) | 체결 0인 회차는 완료가 될 수 없다: `tranche_filled`도 `tranche_closed_partial`도 쓸 수 없다(트리거 거부). 체결 0인 채로 1주 값이 회차 `amount`를 넘으면 그 회차는 제안을 만들 수 없는 채로 남고, 운영자가 계획을 폐기하고 금액을 고쳐 다시 등록한다. 계획 등록 때 모든 회차 `amount`가 등록 시점 1주 값 이상인지 검사하고 아니면 등록을 거부한다(`mandate_tranche_below_one_share`). 1주 값은 등록 직전 완결 일봉의 종가(DB 일봉)이고, 읽지 못하면 등록을 거부한다 |
| 1차 기준가 | 1차 회차가 **완료된 시점**의 누적 체결 평균가 = 1차 체결 금액 ÷ 1차 체결 수량. 이 몫은 순환소수일 수 있으므로 **저장하지 않는다.** 완료 이벤트에 `first_fill_amount`(체결 금액 합)와 `first_fill_quantity`(체결 수량 합)를 한 번 기록하고 바꾸지 않는다. 트리거 판정(I5)은 나눗셈 없이 한다: 회차 k가 armed 될 조건은 `현재가 × first_fill_quantity × 100 ≤ first_fill_amount × (100 + trigger_pct_from_first)`(경계 포함). 모든 항이 정확한 decimal이라 구현마다 결과가 같다. 화면에 보이는 평균가는 표시용 반올림일 뿐 판정에 쓰지 않는다 | 1차가 완료 전(부분 체결 진행 중)이면 기준가가 없고 2차는 armed가 될 수 없다(I5·I7) |
| 회차 체결 확정일(I6 간격의 시작) | 그 회차에 귀속된 **마지막 authoritative 체결의 거래일**(시장 달력). `tranche_filled`든 `tranche_closed_partial`이든 같다. 운영자가 잔여 포기를 기록한 날이 아니다 | 체결 증거가 없으면 확정일도 없다 |
| 재제안 가능 잔여(소진액 반영) | `amount` − 회차 체결 금액. 같은 회차에 열린 제안이 있으면 재제안 자체가 불가(I9). 같은 회차에 전송 결과 불명 제안이 있으면 잔여를 알 수 없으므로 재제안 차단(§4.7) | 불명 제안이 대사로 종결될 때까지 |
| 재제안 수량 | floor(잔여 ÷ max(지정가, 매도 1호가)). 0이고 그 회차에 체결이 있으면 회차를 `tranche_filled`로 닫는다 | 0이고 체결도 없으면 위 줄대로 제안 없음 |

고정 예제(테스트 fixture로 그대로 쓴다, 금액은 예시): 회차 `amount` 940,000원.
1. 1일차: 3주 × 100,000원 체결 뒤 DAY 만료 → 체결 금액 300,000(31.9%) → `tranche_partially_filled`. 잔여 640,000. 기준가 없음, 2차 불가.
2. 2일차: 매도 1호가 98,000원, 지정가 98,200원 → 수량 floor(640,000 ÷ 98,200) = 6주. 6주 × 98,100원 체결 → 체결 금액 누계 888,600(94.5%). 95% 미만이지만 잔여 51,400원이 1주 값(98,200원)보다 작다 → `tranche_filled`.
3. 완료 이벤트에 `first_fill_amount` = 888,600, `first_fill_quantity` = 9를 기록한다(평균가 98,733.33…원은 표시용이고 저장하지 않는다). 체결 확정일 = 2일차. 2차(−15%)는 그 뒤 20거래일이 지나고 `현재가 × 9 × 100 ≤ 888,600 × 85`일 때 armed가 될 수 있다: 현재가 83,923원이면 75,530,700 ≤ 75,531,000이라 충족, 83,924원이면 75,531,600 > 75,531,000이라 불충족. 3차(−30%)의 경계는 `현재가 × 900 ≤ 888,600 × 70 = 62,202,000`, 즉 69,113원까지 충족이고 69,114원은 불충족이다.
4. 변형: 2일차에 운영자가 잔여를 포기하면(`tranche_closed_partial`) `first_fill_amount` = 300,000, `first_fill_quantity` = 3(평균가 100,000원), 체결 확정일 = 1일차다. 포기된 잔여 640,000원은 다음 회차로 넘기지 않는다(2·3차 `amount`는 불변).

### 3.3 불변식 표

| # | 불변식 | 입력 | 전이 | 강제 위치 | 관측 결과(위반 시) |
|---|---|---|---|---|---|
| I1 | 한 종목의 미해제 계획은 같은 영역(실계좌 / 모의) 안에서 전 계좌 1개. 권한 상태가 종결(`retired` 등)이어도 `released` 전이면 예약은 살아 있다(I19) | 계획 INSERT, `released` 전이 | proposed / released | 시장+심볼 키 advisory lock(계좌 무관) 아래 예약 테이블 확인·기록 + 예약 테이블 DB 트리거 검사(아래) | INSERT 거부, CLI 오류 |
| I2 | 계획 종목은 등록 시점 위임장 `allowed_symbols` 안 | 계획 INSERT | proposed | store | 거부 |
| I3 | (a) 그 sleeve의 미해제 계획 신규 매수액(`planned_amount − adopted_amount`) 합 ≤ 그 sleeve `budget_cap`, (b) 바구니(영역, `basket_key`) 전체 합 ≤ 구성 sleeve `budget_cap` 합 — (a)가 모든 sleeve에서 성립하면 따라 나오므로 따로 판정하지 않는다. "미해제" = `released` 이벤트가 없는 계획(§3.7) | 계획 INSERT | proposed→approved | store. lock 순서는 §4.6: 바구니 키 lock(`mandate_basket:<key>`, 같은 등록에서 I4도 판정하므로 잡는다) → sleeve lock | 거부 `mandate_budget_exceeded` |
| I4 | 업종 비중 ≤ `sector_cap_pct`. 분자·분모는 §3.8(계획 금액 기준, 편입액 포함) | 계획 INSERT, 회차 제안 | proposed / armed→proposed | 계획 등록 때 store(거부), dispatch 때 계획 게이트(조항이 바뀌어 넘게 된 경우) | 등록 거부 `mandate_sector_cap` / dispatch는 사람 카드 `mandate_sector_cap` |
| I5 | 회차 k(≥2)는 1차 기준가 × (1+trigger) 이하 가격에서만 armed. 비교는 나눗셈 없이 체결 금액 합과 수량 합으로 한다(§3.2 "회차 기준값") | 일봉/현재가, `first_fill_amount`·`first_fill_quantity` | →armed | 트리거 판정(순수 함수) | armed 안 됨 |
| I6 | 회차 간 최소 20거래일(직전 회차 체결 확정일 기준, 시장 달력) | 원장 체결일, 달력 | →armed | 순수 함수 | armed 안 됨 |
| I7 | 회차는 순서대로(k는 k−1 filled 뒤) | 이벤트 | →armed | 순수 함수 + 이벤트 트리거 | 거부 |
| I8 | 회차 금액 초과 주문 없음: 제안 notional(max(limit, 현재가) × 수량) ≤ 회차 `amount` − 이미 체결된 회차 금액. 그리고 계획 전체로 체결 금액 누계 + notional ≤ `planned_amount − adopted_amount` | 제안 rung | 제안 생성, armed→proposed, 전송 직전 | 제안 생성(거부), 계획 게이트, 전송 직전 경계 | 생성 때 넘으면 제안 거부. 생성 뒤 가격이 움직여 회차 잔여를 넘으면 사람 카드 `mandate_tranche_amount`(카드에 넘는 금액 표시). 계획 전체 한도를 넘으면 전송 차단 `mandate_plan_amount_exceeded` |
| I9 | 같은 회차에 열린 제안은 1개. 전송 결과 불명 제안이 있는 회차는 새 제안도 전송도 없다 | 이벤트 | armed→proposed, dispatch, 전송 직전 | sleeve lock + 이벤트 트리거 + 계획 게이트 + 전송 직전 경계 | 제안 생성 거부 / 전송 차단 `mandate_tranche_duplicate`·`mandate_tranche_uncertain` |
| I10 | "filled"는 브로커 증거로만(`execution_ledger_in_effect()`, authoritative) | 원장 | proposed→filled | 대사 후 투영 CLI | 증거 없으면 전이 없음(websocket 단독은 미확정) |
| I11 | 낙폭 동결(측정 불가·stale 포함)이 커밋된 뒤에는 그 범위의 매수 회차 armed/제안이 없고 **새 전송 fence가 커밋되지 않는다**(수동 승인 포함). 그 전에 커밋된 fence는 POST 한 번까지 나갈 수 있고 그 금액과 예약은 대사 때까지 남는다. 보유·매도는 유지 | 낙폭 판정(관측 버전·유효 기한 포함) | 없음(판정이다. 계획 상태를 바꾸지 않는다) | 제안 생성 + dispatch 계획 게이트 + fence 트랜잭션(동결 범위 lock 아래에서 판정을 읽고 fence를 쓴다. 동결 writer는 같은 lock을 배타로 — hk 4900). POST 직전 재확인은 best-effort(§4.6·§5.4) | 제안 생성 거부·fence 거부 `mandate_drawdown_frozen` + 운영자 알림 |
| I12 | 위임장 버전이 live가 아니거나 조항이 없거나 만료면 자동승인 없음 | sleeve 버전·조항 | 모든 제안 | sleeve 게이트(live 여부, 기존) + 계획 게이트(조항·만료) | 사람 카드 `sleeve_not_live`(기존 코드)/`mandate_terms_missing`/`mandate_expired` |
| I15 | 제안의 심볼·시장·계좌 모드는 결속된 계획과 같고, 실제 송신 요청의 계좌는 계획의 계좌 참조(`account_ref`)와 같다 | 제안 INSERT, dispatch, fence 트랜잭션, POST 직전 | 계획 등록 / 제안 생성 / 결속 소비 | `order_proposals` INSERT 트리거(정규화 심볼·`market`·`account_mode`를 계획·sleeve와 비교) + 계획 게이트 재비교 + fence 트랜잭션 + POST 직전(어댑터가 실제 요청의 계좌에서 참조를 계산해 대조, §4.6 "계좌 결속") | INSERT 거부 / fence 거부·전송 차단 `mandate_binding_mismatch`·`mandate_account_mismatch`(사람 승인으로 넘어가지 않는다, §4.7) |
| I16 | 계획의 권한 상태가 `approved`(1차)·`active`(2·3차)가 아니게 된 뒤 — `retired`·`suspended`·`thesis_broken` 등이 커밋된 뒤 — 에는 그 계획의 **새 전송 fence가 커밋되지 않는다.** 효력 세션 전에도 같다. 그 전에 커밋된 fence는 POST 한 번까지 나갈 수 있고, 그 fence가 대사로 닫힐 때까지 예약·예산은 풀리지 않는다. 자동승인은 추가로 현재 결속 조항이 현재 live 버전의 조항이어야 한다 | 계획 이벤트 스트림(`seq` 순서) | 결속 소비, fence 트랜잭션 | 이벤트 INSERT 트리거(권한 상태가 `approved`/`active`가 아닌 계획의 fence INSERT 거부, `UNIQUE(plan_id, seq)`) + 계획 게이트 + fence 트랜잭션의 판정. POST 직전 재확인은 best-effort | 권한 상태·효력 세션 위반은 fence 거부 `mandate_plan_not_active`. 조항만 낡은 경우(재승인 대기)는 사람 카드 `mandate_terms_stale` |
| I17 | 파킹 allowlist 심볼(459580·357870·SGOV·BIL)은 계획 대상이 아니다 | 계획 INSERT | proposed | store + `allowed_symbols` 검증(`parking_scope`) | 거부 |
| I13 | 계획·회차·조항·이벤트 행은 수정·삭제 불가 | 모든 쓰기 | — | DB 트리거 | `restrict_violation` |
| I14 | 실계좌 영역에 미해제 계획이 있는 종목의 손실 매도(`exit_intent='loss_cut'`)는 계좌와 무관하게 그 계획의 `thesis_broken` 이벤트 뒤에만 만들고 보낼 수 있고, 항상 사람 카드다. 시간이 지났다는 이유로 바뀌지 않는다(§5.2) | 시장·종목 예약(계좌 무관), `thesis_broken`·`thesis_evidence_refreshed` 이벤트의 `evidence`(회고 id), `loss_sell_mode` | →closed | 세 곳(T4c): `loss_cut` 제안 생성(종목 lock 아래), 사람 승인 콜백의 nonce 소비 전, 브로커 호출 직전. 계획 등록은 같은 종목 lock 아래에서 열린 `loss_cut` 제안이 있으면 거부(T1). 그리고 기존 분류기의 `loss_cut_intent` 거부(자동승인 차단, 변경 없음) | 생성 거부·승인 거부·전송 차단 `mandate_thesis_not_broken` / 계획 등록 거부 `mandate_open_loss_cut` / 통과한 제안은 사람 카드(기존 손절 2클릭 의식) |
| I18 | 자동승인되는 회차 번호 ≤ 활성화 프로필의 `max_tranche_index`. 프로필은 현재 live 조항에 결속되고 승인 기록이 있으며 만료·철회되지 않았다(R1, §3.6) | 활성화 프로필 최신 버전, 조항, 회차 번호 | 결속 소비 | 계획 게이트(sleeve lock 아래에서 매번 다시 읽는다) | 사람 카드 `mandate_activation_missing`·`mandate_tranche_not_activated` |
| I19 | 종목 예약·예산(I3)·업종 몫(I4)은 계획이 `released`가 될 때까지 유지된다. `released`는 미결 주문 0, 불명 주문 0(닫히지 않은 fence 0), 보유 처리 증거가 모두 있을 때만 쓸 수 있다. 보유 0의 증거는 계획이 등록 때 고정한 범위 계좌(`scope_account_refs`)와 참조가 일치하는 계좌의 조회여야 한다(R2, §3.7) | 주문 원장·제안 상태·fence·보유, 계좌 참조 | →released | 이벤트 INSERT 트리거 + store, 종목 lock 아래 | `released` INSERT 거부. 예약이 남아 같은 종목의 새 계획 등록이 거부된다(I1) |
| I20 | 자동승인은 전역 누계와 바구니 누계를 **둘 다** 통과해야 한다. 두 누계는 자동 전송 fence로 세고, fence는 브로커 호출 전에 따로 커밋되며 미전송이 증명된 때에만 해제된다. 전역 누계는 계획 결속 판정에서 `broker_account_id` 키로 나누지 않는다(R3, §4.6) | 전역 누계 reader, `tranche_send_fenced`/`tranche_send_released` 이벤트 | 결속 소비 | 계획 게이트·분류기, lock 순서 §4.6 | 사람 카드 `daily_cap_exceeded`(전역)·`mandate_basket_daily_cap`(바구니) |
| I21 | 조항 행과 활성화 프로필 행은 내용 해시가 같은 인증된 승인 기록 없이는 존재하지 않는다. 승인 기록과 묶음 승인 이벤트는 허용된 chat에서 운영자 principal 목록에 있는 발신자가 눌렀을 때만 생긴다(Q1, §3.5) | 승인 요청 스냅샷, 승인 기록 | 조항·프로필 INSERT | INSERT 트리거(승인 기록 FK·해시 일치·1:1) + 계획 게이트 재확인 | INSERT 거부 / 전송 차단 `mandate_binding_broken` |
| I22 | 묶음 승인 배치는 바구니 × 대상 거래일당 1개이고, 그 바구니의 앞 배치가 닫힌 뒤에만 새 배치를 만든다. 한 계획이 열린 멤버를 둘 갖지 않는다(1a, §5.1) | 배치 INSERT | 배치 생성 | `UNIQUE(realm, basket_key, target_session)` + 배치 INSERT 트리거(앞 배치의 열린 멤버 검사, 바구니 lock 아래) | 배치 INSERT 거부. 재발송은 같은 배치 |
| I23 | 계획 결속 제안은 §4.1의 조건이 **전부** 참일 때만 자동승인된다. 계획 결속이 있는데 계획 게이트의 결속 증거(`mandate_binding`)가 없는 rung은 분류기의 어떤 다른 경로로도 자동승인되지 않는다(§4.1 조건 0) | `mandate_plan_id`, `mandate_binding` | 결속 소비 | 분류기 진입부(T4a가 넣는 거부) | 사람 카드 `mandate_not_eligible` + 계획 게이트의 사유 |
| I24 | **전송 권한의 단위는 커밋된 전송 fence다.** 계획 결속 rung의 브로커 호출은 그 시도의 fence가 커밋된 뒤에만, fence 하나에 POST 한 번만 한다(자동·사람 승인 모두). 닫히지 않은 fence가 있는 회차는 새 제안도 새 fence도 없다. advisory lock의 보유는 권한의 근거가 아니다(§4.6) | `tranche_send_fenced`/`tranche_send_released` 이벤트, 계획 이벤트 스트림 | fence 트랜잭션 ~ 대사 | 별도 세션의 fence 트랜잭션(판정과 INSERT를 한 트랜잭션에), 이벤트 INSERT 트리거 | fence 커밋을 확인하지 못하면 전송 안 됨 `mandate_fence_unavailable` / 닫히지 않은 fence면 `mandate_tranche_uncertain` |

### 3.4 DB 가드와 GRANT

- 이 설계의 새 테이블은 모두 열하나다. T1이 아홉(`mandate_approval_requests`, `mandate_approvals`, `mandate_terms`, `mandate_activation_profiles`, `mandate_plans`, `mandate_plan_tranches`, `mandate_plan_events`, `mandate_symbol_reservations`, `mandate_symbol_reservation_releases`), T3a가 둘(`mandate_plan_batches`, `mandate_plan_batch_members`)을 만든다(§8과 같은 수). 열하나 모두 UPDATE·DELETE·TRUNCATE 거부 트리거(모든 역할). 선례 `20261102_rob1470_strategy_sleeves.py:75-130`.
- 이벤트 `seq`는 직전+1만, 이벤트 어휘·전이 표에 없는 전이는 INSERT 트리거가 거부. 전송 fence(`tranche_send_fenced`)도 이 스트림의 이벤트다: 트리거는 그 `seq` 시점의 권한 상태가 `approved`/`active`가 아닌 계획에 대한 fence INSERT를 거부한다. 그래서 `retired`·`suspended`·`thesis_broken`보다 뒤에 커밋되는 fence는 존재할 수 없고, 그 순서는 쓰는 쪽의 lock이 아니라 `UNIQUE(plan_id, seq)`와 이 트리거가 정한다(§4.6).
- I1: 예약은 append-only 두 테이블 `review.mandate_symbol_reservations`(realm, market, 정규화 symbol, plan_id)와 `review.mandate_symbol_reservation_releases`(reservation_id UNIQUE)로 기록하고 이력은 지우지 않는다. 부분 UNIQUE는 다른 테이블의 해제 행을 볼 수 없으므로 쓰지 않는다. 대신 예약 INSERT 트리거가 **스스로** `pg_advisory_xact_lock(hashtextextended('mandate_symbol:'||realm||':'||market||':'||symbol,0))`을 잡은 뒤 같은 realm·market·symbol에 해제 행이 없는 예약이 있으면 거부한다. 해제 INSERT 트리거도 같은 락을 잡는다. 락이 트리거 안에 있으므로 어떤 writer도 우회할 수 없고, 두 동시 INSERT 중 뒤의 것은 앞의 커밋을 기다린 뒤(READ COMMITTED 재조회) 거부된다. 트리거는 READ COMMITTED가 아닌 트랜잭션을 거부한다(#1250 Q-46 게이트와 같은 이유). 해제 행은 그 계획의 `released` 이벤트가 같은 트랜잭션에 있을 때만 INSERT된다(트리거가 확인). 권한 상태가 `retired`·`rejected`·`exhausted`·`closed`가 됐다는 것만으로는 해제하지 않는다(§3.7, I19).
- `order_proposals`에 `mandate_plan_id`·`mandate_tranche_index`(nullable) 추가, INSERT 때만 설정, `sleeve_id`·`account_mode`·`market`·정규화 `symbol`이 계획(과 그 sleeve)과 같아야 함(트리거, I15). `order_proposal_create`는 `symbol`을 따로 받으므로(`app/mcp_server/tooling/order_proposal_tools.py:703`) 이 비교 없이는 다른 종목 제안이 회차에 결속될 수 있다. 선례 `:157-164`.
- downgrade는 행이 하나라도 있으면 거부.
- 하드룰 18: 테이블을 만드는 마이그레이션마다 조건부 `at_app` GRANT — 위 열하나 테이블 SELECT·INSERT만, UPDATE·DELETE 없음. `order_proposals` 새 열은 기존 테이블 권한을 따른다.

### 3.5 인증된 승인 기록 (Q1의 조건)

리뷰 §1 Q1 판정: 조항에 승인자 문자열만 적는 것으로는 부족하고, 인증된 운영자 액션과 그 기록을 검증하는 계약이 필요하다. 원래 문서의 `approved_by`·`approved_at` 열은 누가 쓰든 채울 수 있는 문자열이었다.

[사실] 이 레포의 Telegram 승인 콜백은 요청마다 chat allowlist, `from.id`의 형식(정확한 정수), 단일 소비 nonce, 게시된 카드 결속을 확인한다(`auto_trader@ac4f9ed5c` `docs/agent-contracts/approval-and-shadow-contracts.md:105` — "Telegram 승인 콜백 durable inbox (W5)"의 R37 identifier boundary 문단). 그러나 **발신자가 운영자인지는 확인하지 않는다**: 콜백 정규화는 chat id를 allowlist와 비교하고 `from.id`는 형식만 검증한다(`auto_trader@ac4f9ed5c` `app/services/order_proposals/telegram_callback.py:2181-2207`). 허용된 chat이 그룹이면 그 그룹의 다른 멤버가 누른 버튼도 같은 검사를 통과한다. 운영자 CLI의 actor는 비어 있지 않은 문자열이면 받고 시스템 actor 이름만 거부한다(`auto_trader@ac4f9ed5c` `app/services/strategy_sleeves/store.py:131-136`). 누가 입력했는지는 호스트 접근 통제에 기댄다. 즉 지금 있는 어느 표면도 그대로는 "인증된 운영자 액션"이 아니다.

[의견] 그래서 조항과 활성화 프로필의 승인은 **nonce 결속 Telegram 카드 하나의 콜백**으로만 생기고, 그 콜백은 기존 검사에 **운영자 principal 대조**를 더한다. CLI는 승인 요청(초안 스냅샷)을 만들 뿐 승인을 기록하지 못한다.

운영자 principal 대조(위임장 콜백 `mta`·`mpb` 공통):

- 설정 `MANDATE_APPROVER_TELEGRAM_USER_IDS`(기본 빈 값): 위임장 승인을 할 수 있는 Telegram 사용자 id의 닫힌 목록. 빈 값이면 **아무도** 승인할 수 없다(fail-closed) — 조항·프로필·묶음 승인이 전부 기록되지 않는다.
- 콜백은 nonce를 소비하기 **전에** `from.id`(정확한 정수)가 그 목록에 있는지 확인한다. 없으면 승인 기록·이벤트를 쓰지 않고 nonce도 소비하지 않으며 닫힌 사유 `mandate_approver_not_allowed`를 돌려준다. 같은 chat의 다른 멤버가 눌러도 카드는 운영자가 누를 수 있는 상태로 남는다.
- chat allowlist 검사는 그대로 둔다. 두 검사 모두 통과해야 한다(허용된 chat **그리고** 허용된 발신자). chat이 개인 chat인지 그룹인지에 기대지 않는다.
- 승인 기록의 `principal`은 이 대조를 통과한 `from.id`다. 목록에 없는 id가 `principal`인 승인 기록은 존재할 수 없다(콜백이 쓰지 않는다). 계획 게이트는 소비 시 승인 기록의 `principal`이 **지금** 목록에 있는지는 다시 보지 않는다 — 승인 시점의 인증이 기록의 근거이고, 승인자를 목록에서 빼는 것으로 과거 승인을 무효로 하려면 새 위임장 버전(또는 프로필 철회)을 쓴다.
- **계획 결속 제안의 주문 승인**에도 같은 대조를 한다: 사람 승인 콜백의 nonce 소비 전 차단 판정(§4.7)이 `from.id`를 이 목록과 대조하고, 목록에 없으면 nonce를 남긴 채 `mandate_approver_not_allowed`로 거부한다(T4c). 계획 결속이 아닌 제안의 승인·veto·손절 콜백에는 적용하지 않는다. 그 경로의 발신자 인증은 이 설계의 범위가 아니다(§9).
- 테스트 계약(T3a·T3b): 허용된 chat에서 목록에 없는 `from.id`가 누르면 아무 행도 쓰이지 않고 nonce가 남는다. 목록이 비어 있으면 누구의 승인도 기록되지 않는다. 목록에 있는 `from.id`가 허용되지 않은 chat에서 누르면 기존대로 거부된다.

`review.mandate_approval_requests`(불변 스냅샷):

| 열 | 의미 |
|---|---|
| `id` | PK |
| `subject_kind` | `terms` / `activation_profile` |
| `sleeve_id` | 대상 sleeve |
| `base_sleeve_version` | 요청을 만들 때의 sleeve 최신 버전 번호. 승인 시점에 달라졌으면 그 요청은 아무것도 쓰지 않는다 |
| `content` | jsonb — 승인 대상 내용 전체의 정규화 JSON. `terms`면 조항 열과 함께 그 버전의 sleeve 값(예산·손실 한도·사전등록 4필드)도 담는다. 운영자가 승인하는 것은 위임장 버전 전체다. `activation_profile`이면 프로필 열 |
| `content_sha256` | 그 해시. 카드에 표시한다 |
| `approval_ref` | 이 승인의 근거 hk 문서 key 또는 hk 이슈 id(NOT NULL, 공백 거부) |
| `requested_by`·`created_at` | 요청을 만든 운영자 CLI actor |

`review.mandate_approvals`(append-only):

| 열 | 의미 |
|---|---|
| `id` | PK |
| `request_id` | FK, UNIQUE — 요청 하나에 승인 기록은 많아야 하나 |
| `decision` | `approved` / `denied` |
| `subject_sha256` | 승인한 내용 해시. 요청의 `content_sha256`과 같아야 한다(트리거) |
| `principal` | 운영자 principal 대조를 통과한 콜백의 `from.id`를 canonical decimal 문자열로(W5와 같은 표기) |
| `nonce_digest` | 소비한 nonce의 일방향 digest |
| `decided_at` | |

흐름:

1. 운영자 CLI가 승인 요청을 만든다(기본 dry-run, `--commit` 때만 쓰기). 카드 발송도 CLI(scheduleless). 한 번 live였던 sleeve의 사전등록 4필드는 바꿀 수 없으므로(`auto_trader@ac4f9ed5c` `alembic/versions/20261102_rob1470_strategy_sleeves.py:99-112`) 요청의 그 값이 직전 버전과 다르면 요청 생성을 거부한다. 같은 트리거는 값이 직전과 같은 버전의 append는 막지 않는다.
2. 카드는 내용 전체와 해시, 근거 key, 그리고 조항이면 직전 live 조항과의 차이를 보여 준다. 활성화 프로필이면 §4.5의 "승인 화면 표시 항목"을 보여 준다.
3. 콜백 `mta`(묶음 승인 콜백 `mpb`와 같은 계열, 주문·제안 모듈 import 금지 정적 테스트)가 한 트랜잭션에서: 승인 기록 INSERT → (`terms`) 새 sleeve 버전(`state='live'`, `set_by = operator:telegram:<principal>`) + 그 버전에 결속된 조항 행 INSERT / (`activation_profile`) 프로필 행 INSERT. `base_sleeve_version`이 낡았거나 nonce가 이미 소비됐으면 아무것도 쓰지 않는다. 조항만 바뀌고 sleeve 값은 그대로인 변경도 새 버전을 붙여야 하는데 기존 `set_sleeve_version`은 그런 버전을 거부한다(§2.3 F8). 그래서 T1의 store는 "승인 기록과 조항을 동반한 버전 append" 함수를 따로 두고, 기존 함수와 ROB-1470의 DB 트리거는 바꾸지 않는다.
4. `denied`는 승인 기록만 남기고 다른 행을 만들지 않는다.

소비 시 검증(I21): 계획 게이트는 조항·프로필을 읽을 때 (1) 그 행의 `approval_id`가 가리키는 승인 기록이 `approved`이고 그 `subject_sha256`이 행의 `content_sha256`과 같은지, (2) 행의 내용 열(위 두 표의 `content_sha256` 정의, 조항이면 그 sleeve 버전의 값 포함)로 다시 계산한 해시가 `content_sha256`과 같은지 확인한다. 하나라도 다르면 결속 파손으로 전송 차단(`mandate_binding_broken`, §4.7)이다.

경계:
- 이 콜백은 주문을 만들지도 보내지도 않는다. 쓰는 것은 승인 기록, sleeve 버전, 조항, 활성화 프로필뿐이다.
- 시스템 actor는 승인 기록을 만들 수 없다. 손실 한도 게이트가 붙이는 `stopped` 버전은 조항·승인 기록 없이 붙는다(권한을 줄이는 방향이라 승인이 필요 없다).
- 아침 묶음 승인(§5.1)은 계획 이벤트의 `actor`에 같은 principal 표기를 남긴다. 그 승인은 이 표가 아니라 계획 이벤트와 배치 멤버 스냅샷이 기록이다.
- 웹 승인 화면(`/trader`)으로 같은 승인을 받는 것은 이 설계에 없다.

### 3.6 활성화 프로필 — 회차 상한의 기계적 강제 (R1, Q12)

리뷰 R1: 원래 문서는 "실전 카나리는 1차만, 2·3차는 별도 승인"이라고 했지만 스위치 셋은 전체·시장·계좌뿐이고 조항에도 회차 상한이 없었다. 2·3차 트리거(T7)가 생긴 뒤에는 같은 스위치로 후속 회차가 자동승인될 수 있었다. 운영자 답 Q12: 1차-only를 기계적으로 강제하는 보완을 먼저 반영한다.

`review.mandate_activation_profiles`(append-only, sleeve당 버전 직전+1):

| 열 | 의미 |
|---|---|
| `id`, `sleeve_id`, `profile_version` | 버전은 sleeve 안에서 직전+1만(트리거) |
| `terms_id` | 이 프로필이 여는 조항 행. 그 sleeve의 조항이어야 한다(트리거) |
| `max_tranche_index` | 0~3. 자동승인이 허용되는 가장 큰 회차 번호. **스키마 기본값 1**. 0은 철회(자동승인 없음) |
| `effective_session` | 효력이 시작되는 거래일 |
| `expires_at` | 이 프로필의 만료(NULL 불가). 지나면 자동승인 없음 |
| `approval_id` | FK → `mandate_approvals.id`, UNIQUE, NOT NULL |
| `content_sha256` | 내용 열(`sleeve_id`·`profile_version`·`terms_id`·`max_tranche_index`·`effective_session`·`expires_at`)의 정규화 JSON 해시. `id`·`approval_id`와 이 열 자신은 넣지 않는다. 승인 요청의 `content_sha256`과 같은 값이다 |

규칙:
- 자동승인 조건(§4.1 조건 1a): 그 sleeve의 **최신** 프로필이 있고, 그 `terms_id`가 현재 live 조항 행이고, 효력 세션이 도래했고 만료 전이며, 제안의 회차 번호 ≤ `max_tranche_index`. 하나라도 아니면 사람 카드다(I18).
- 계획 게이트는 이 값을 sleeve lock 아래에서 **매번 다시 읽는다**. 프로세스 기동 때 읽어 둔 값을 쓰지 않는다.
- 회차 확대(1 → 2, 2 → 3)는 새 프로필 버전 = 새 승인 요청 + 새 승인 기록이다. 기존 행을 고칠 수 없다. "2·3차 트리거 CLI를 돌리지 않는다"는 운영 약속에 기대지 않는다: 트리거가 2차 제안을 만들어도 프로필이 1이면 그 제안은 사람 카드로 간다.
- 위임장 변경(새 조항 행)이 오면 기존 프로필의 `terms_id`가 live 조항과 달라지므로 자동승인이 닫힌다. 다시 여는 것은 새 프로필이다.
- 철회는 `max_tranche_index = 0`인 새 버전이다. 급한 정지는 스위치(`MANDATE_AUTO_APPROVE_ENABLED=false`)가 먼저다.
- 프로필은 스위치를 대신하지 않는다. 스위치(env, 배포 승인)와 프로필(인증된 승인 기록)이 **둘 다** 참이어야 한다.
- 감사: 자동승인 결정 기록과 자동 `tranche_send_fenced` 이벤트에 프로필 id·버전·`max_tranche_index`를 남긴다.
- 모의(키움 mock) sleeve도 같은 프로필을 쓴다. 다만 키움 mock 제안은 수동 승인 전용이라(§4.5) 프로필이 자동승인을 여는 일은 없다.

### 3.7 폐기와 정리의 분리 — 예약·예산 해제 조건 (R2)

`retired`는 **추가 권한 정지**다: 그 계획으로 새 회차가 armed 되지 않고, 새 제안이 만들어지지 않고, 이미 만든 제안에도 새 전송 fence가 커밋되지 않는다(I16). `retired`보다 먼저 커밋된 fence는 POST 한 번까지 나갈 수 있고, 그 주문은 대사가 종결을 증명할 때까지 예약·예산을 차지한다(§4.6). `retired`는 주문 취소가 아니고 보유 처분도 아니다. `rejected`·`exhausted`·`closed`도 같다.

**이 설계는 미결 주문을 자동으로 취소하지 않고 보유를 자동으로 팔지 않는다.** 이미 브로커가 받은 회차 주문은 폐기 뒤에도 살아 있다(DAY 주문은 그날 끝난다). 취소가 필요하면 운영자가 기존 취소 경로로 직접 한다.

권한 상태가 `rejected`·`retired`·`closed`가 된 계획은 정리 상태를 따로 거친다. `exhausted`와 `thesis_broken`은 보유 중인 계획이므로 정리 대상이 아니고 예약·예산·업종 몫에 그대로 산입된다. 그 계획의 보유가 0이 된 뒤에는 같은 정리 이벤트로 풀 수 있다.

```
(권한 상태: rejected / retired / closed, 또는 exhausted 뒤 보유 0)
        │
        ▼
   winding_down ──(orders_settled)──▶ orders_clear ──(holdings_disposed)──▶ disposable ──(released)──▶ released
```

| 정리 이벤트 | 쓸 수 있는 조건(전부 참) | 증거(`evidence`) | 쓰는 쪽 |
|---|---|---|---|
| `orders_settled` | 이 계획에 결속된 모든 제안의 모든 rung이 종결 상태이고, 전송 결과 불명(`unverified`·`uncertain`) rung이 0이며, 브로커에 나간 주문은 원장 대사로 종결(체결·취소·만료·거절)이 증명됐다. 닫히지 않은 전송 fence(§4.6)가 0 | 제안·rung id, 원장 행 id, 대사 실행 id | 대사 뒤 투영 CLI(`system:mandate_projection`) |
| `holdings_disposed` | 셋 중 하나: (a) `zero_holdings` — 그 계획의 **보유 범위**(아래) 전부에서 그 종목 보유 수량이 0임을 보유 조회로 확인. 조회한 각 계좌의 계좌 참조가 계획이 등록 때 고정한 `scope_account_refs`와 **하나씩 일치**해야 한다. 범위의 한 계좌라도 조회하지 못하거나, 지금 설정된 계좌의 참조가 등록 때의 것과 다르거나, 참조를 계산할 수 없으면 unknown이고 쓰지 않는다, (b) `kept_outside_basket` — 운영자가 남은 보유를 바구니 밖 보유로 둔다고 기록(3792 분류의 "보유"와 같은 취급), (c) `never_adopted` — 그 계획이 한 번도 `approved`가 된 적이 없다(편입이 효력을 가진 적이 없고 이 계획의 주문도 없다) | 보유 조회 시각·수량 / 운영자 기록 / 이벤트 이력 | (a)·(c) 투영 CLI(읽기 전용) / (b) 운영자 CLI |
| `released` | 앞의 둘이 모두 있다 | 두 이벤트 seq | store, 종목 lock 아래. 같은 트랜잭션에서 예약 해제 행 INSERT |

보유 범위는 계획의 계좌 모드로 정한다. 편입액(§3.8)에 센 범위와 같다.

| 계획의 계좌 모드 | 보유 범위 | 보유가 남아 해제가 거부되는 사례(테스트 계약) |
|---|---|---|
| 실계좌(`kis_live`·`toss_live`) | KIS·Toss 두 실계좌(계획 계좌의 편입 보유와 계획의 매수분, 그리고 계획 계좌가 아닌 쪽의 보유분) | 계획 계좌에 1주라도 남음 → `zero_holdings` 거부. 계획 계좌는 0인데 다른 실계좌에 남음 → 거부. 모의 계좌의 보유는 보지 않는다 |
| 모의(`kiwoom_mock`) | 그 키움 모의 계좌 하나 | 모의 계좌에 매수 체결분이 남음 → `zero_holdings` 거부(두 실계좌가 0이어도). 모의 계좌가 0이면 실계좌에 같은 종목이 있어도 허용 — 실계좌 보유는 모의 계획의 범위가 아니다 |

모의 계획의 편입액도 같은 범위로 센다: 그 모의 계좌의 보유다(보통 0).

**보유 범위의 계좌는 등록 때 고정한다.** 계획의 `account_ref`는 주문이 나가는 계좌 하나뿐이지만 보유 범위에는 상대 계좌도 있다(실계좌 계획이면 KIS와 Toss 둘 다). 상대 계좌를 고정하지 않으면, 등록 뒤에 그 계좌 모드의 설정이 다른 계좌로 바뀌었을 때 "지금 설정된 계좌가 0"이라는 조회를 "등록 당시 그 계좌가 0"으로 잘못 읽는다. 예: 두 계좌 모두 그 종목이 없을 때 Toss 계획을 등록했고, 그 뒤 KIS 계좌 A가 다른 경로로 그 종목을 사고 KIS 설정이 계좌 B로 바뀌면, Toss와 B의 0만 보고 예약을 풀 수 있다 — A에 남은 주식은 다음 계획의 편입액에서도 빠진다. 그래서 편입액이 0이어도 범위의 각 계좌 참조를 `scope_account_refs`에 고정하고, `zero_holdings`는 그 참조들과 일치하는 계좌의 조회만 증거로 받는다. 일치하지 않거나 확인할 수 없으면 unknown이고 해제가 거부된다. 그때 예약을 풀 수 있는 것은 운영자의 `kept_outside_basket` 기록뿐이고(사정을 아는 사람의 명시적 기록), 다음 계획의 편입액은 **그 시점에 설정된** 범위 계좌를 새로 조회해 새 참조와 함께 고정한다.

테스트 계약: (a) 등록 뒤 상대 계좌의 설정이 다른 계좌로 바뀌면 두 계좌가 모두 0으로 조회돼도 `zero_holdings`가 거부된다. (b) 범위 계좌의 참조를 확인하지 못하면 계획 등록이 거부된다(편입액 0 포함). (c) 참조가 모두 일치하고 전부 0이면 허용된다.

규칙:
- `released` 전까지 그 계획은 종목 예약(I1), sleeve·바구니 예산(I3), 업종 몫(I4)에 **계속 산입된다**. 불명·미결 주문이 하나라도 있으면 풀리지 않는다.
- 폐기된 계획의 주문이 나중에 체결되면 원장 투영은 그 체결을 그 계획에 그대로 기록한다(`tranche_filled`·`tranche_partially_filled`는 권한 상태가 끝난 계획에도 append할 수 있다). 사실 기록이지 권한이 아니다.
- 보유를 계좌 사이로 옮기는 수단은 이 설계에 없다. `kept_outside_basket`은 "두 계좌 중 어디든 그 종목 보유가 남은 채로 예약을 푼다"는 운영자 기록이다. 그 기록 없이는 보유가 남아 있는 동안 예약이 풀리지 않으므로 같은 종목의 새 계획은 어느 계좌에도 등록되지 않는다. 정리 뒤 새 계획을 등록하면(어느 계좌든) 그 시점의 두 계좌 보유 합계가 새 계획의 편입액이 된다(§3.8). 그래서 남은 보유가 새 계획에서 빠지는 일은 없다.
- 승인된 적이 없는 `rejected` 계획은 주문도 편입 효력도 없으므로 투영 CLI 한 번으로 `released`까지 간다.
- §5.1의 재승인 기각은 `retired`다. 기각 시점에 미결 주문이 있으면 카드 응답에 그 수를 보여 주고, 계획은 `winding_down`에 머문다.

### 3.8 편입액과 업종 상한의 산입 규칙 (R3 일부)

편입 종목(3792 결정 항목 1)은 계획을 등록하는 계좌에 이미 보유가 있다. 그 금액을 어디에 세는지 정하지 않으면 두 번 세거나 한 번도 안 세게 된다.

[의견] 근거는 3792 보고서의 분류 규칙이다: "두 계좌 합계 평가액이 종목당 계획액(약 235만) 미만"이라 분할 여지가 있다고 봤다. 즉 계획 금액은 기존 보유를 포함한 목표 총액이다.

| 기호 | 정의 |
|---|---|
| P | `planned_amount`. 그 종목의 목표 총액 |
| A | `adopted_amount`. 등록 시점 편입액 = 실계좌 계획이면 그 종목의 KIS·Toss 두 계좌 보유 합계(모의 계획이면 그 모의 계좌의 보유, §3.7). 등록 때 조회한 계좌의 참조를 `scope_account_refs`에 함께 고정한다. 등록 뒤 불변. 계획 계좌가 아닌 쪽의 보유분은 금액으로만 세고, 계획은 그 주식을 사고팔지 않는다(3792 분류 "보유(계획은 다른 계좌)") |
| N | P − A. 새로 살 수 있는 금액. 회차 `amount`의 합 |

| 판정 | 세는 값 | 이유 |
|---|---|---|
| sleeve·바구니 예산(I3) | N | sleeve 사용액은 sleeve 자기 rung의 체결만 본다(§2.3 F5). 편입 보유는 사용액에 없으므로 예산 쪽 계획 합에도 넣지 않는다. 예산은 새로 쓰는 돈의 한도다 |
| 업종 상한(I4) 분자 | 그 업종(`sector_key`)의 미해제 계획 P 합 | 업종 쏠림은 이미 들고 있는 금액도 노출이다. P에 A가 들어 있으므로 정확히 한 번 센다 |
| 업종 상한(I4) 분모 | 그 바구니(영역, `basket_key`) 구성 sleeve의 현재 `budget_cap` 합 + 그 바구니 미해제 계획 A 합 | 바구니의 총 용량이다. 지금 등록된 계획 합을 분모로 쓰면 첫 계획이 언제나 100%가 된다 |
| 회차 금액(I8) | N × 비중 | 새로 사는 금액만 나눈다 |
| 낙폭(§5.4) | hk 4900이 정한다 | 이 문서는 정하지 않는다 |

업종 상한은 **계획 금액 기준**이다. 시세 평가액을 쓰지 않는다. 시세 기준은 가격이 움직일 때마다 판정이 바뀌고 조회 실패 처리가 필요하다. 계획 금액 기준은 "계획을 전부 집행하면 그 업종에 얼마가 들어가는가"를 등록 시점에 확정적으로 답한다. 판정 시점은 계획 등록(넘으면 등록 거부)과 dispatch(조항·예산 버전이 바뀌어 넘게 된 경우 사람 카드)다.

범위 밖인 보유: 바구니에 편입되지 않은 **종목**의 보유는 A에도 분자에도 넣지 않는다. 누락이 아니라 바구니 밖이기 때문이다. 계좌 전체의 업종 쏠림은 기존 advisory(`portfolio.sector_cluster_cap_pct`)의 몫이다(§9). 편입 종목은 다르다: 계획 계좌가 아닌 쪽의 같은 종목 보유도 A에 들어간다. 그것을 빼면 3792 보고서가 "분할 여지"를 잰 기준(두 계좌 합계 대 계획액)보다 더 많이 사게 된다.

예제(금액은 설명용이며 실제 보유가 아니다). 바구니 ① KR: Toss sleeve `budget_cap` 1,000만, KIS sleeve 950만.

| 계획 | 계좌 | 업종 | P | A | N | 회차(40/30/30%) |
|---|---|---|---|---|---|---|
| X(편입, 두 계좌 보유) | Toss | 자동차 | 235만 | 100만(Toss 85만 + KIS 15만) | 135만 | 54만 / 40.5만 / 40.5만 |
| Y(편입) | KIS | 자동차 | 235만 | 100만 | 135만 | 54만 / 40.5만 / 40.5만 |
| Z(신규) | Toss | 자동차 | 235만 | 0 | 235만 | 94만 / 70.5만 / 70.5만 |
| W(편입) | KIS | 금융 | 235만 | 60만 | 175만 | 70만 / 52.5만 / 52.5만 |

- 예산: Toss sleeve N 합 = 135 + 235 = 370만 ≤ 1,000만. KIS sleeve = 135 + 175 = 310만 ≤ 950만. 바구니 = 680만 ≤ 1,950만.
- 업종 분모 = 1,950만 + A 합(100 + 100 + 0 + 60 = 260만) = 2,210만. 35% = 773.5만.
- 자동차 분자 = 235 × 3 = 705만 ≤ 773.5만 → 통과. 여기에 자동차 네 번째 계획(P 235만, A 0)을 등록하려 하면 분자 940만 > 773.5만 → 등록 거부(`mandate_sector_cap`).
- 누락 사례: A를 분자에서 빼면 자동차는 135 + 135 + 235 = 505만으로 보이고 네 번째 계획(740만)이 통과한다. 실제 자동차 노출은 940만 ÷ 2,210만 = 42.5%다.
- 중복 사례: A를 예산에도 넣으면 Toss sleeve가 100만을 쓰지도 않았는데 쓴 것으로 잡혀, sleeve 사용액(체결 기준)과 계획 합(등록 기준)이 서로 다른 금액을 말하게 된다.
- 두 계좌 보유 사례: X의 KIS 보유 15만은 A에 들어가 N을 줄이고(135만) 자동차 분자에도 P로 한 번 들어간다. KIS sleeve의 예산·사용액에는 들어가지 않는다. 이 15만을 빼고 A를 85만으로 잡으면 N이 150만이 되어 두 계좌 합계가 계획 금액 235만을 15만 넘는다.
- 폐기 사례: Y를 `retired` 해도 `released` 전에는 자동차 분자가 705만 그대로다(§3.7).

영역이 다른 같은 이름의 바구니(테스트 fixture로 그대로 쓴다). 실계좌 바구니 `basket-1`(예산 합 1,950만, 편입액 0)에 자동차 계획 X·Y·Z·W가 각 235만이고, 모의 바구니 `basket-1`(예산 합 1,950만)에 자동차 계획 X가 235만 있다고 하자.

- 두 영역을 합치면 자동차 = 1,175만 ÷ 3,900만 = 30.1%로 35% 아래다. 이렇게 세면 안 된다 — 모의 자본이 실계좌의 업종 상한을 묽힌다.
- 바구니 정체성이 (영역, `basket_key`)이므로 따로 센다: 실계좌 바구니는 940만 ÷ 1,950만 = 48.2%로 35%(682.5만)를 넘는다. 실계좌의 세 번째 자동차 계획(705만 > 682.5만)부터 등록이 거부된다. 모의 바구니는 235만 ÷ 1,950만 = 12.1%다.
- 예산(I3), 바구니 누계 B, 아침 배치, 낙폭, 바구니 lock도 같은 경계다. 모의 바구니의 어떤 값도 실계좌 바구니의 판정에 들어가지 않고 그 반대도 같다.

## 4. 계획 결속 자동승인

### 4.1 어떤 제안이 사람 카드 없이 나가는가

아래가 **전부** 참일 때만 그 rung은 자동승인 적격이다. 하나라도 거짓이면 자동승인은 없다. 그 뒤가 사람 카드(강등)인지 전송 차단인지는 실패 종류에 따라 다르고, §4.7의 표가 정본이다. 아래 각 조건의 괄호는 그 표의 분류를 옮긴 것이다.

0. **다른 길이 없다(I23).** 계획 결속 제안(`mandate_plan_id`가 있는 제안)은 아래 1~10이 전부 참일 때에만 자동승인된다. 기존 분류기는 `expanded`에서 지정가가 현재가 아래인 매수를 계획과 무관하게 자동승인할 수 있다(거부 조건이 `limit_price >= threshold`뿐이다, `auto_trader@ac4f9ed5c` `app/services/order_proposals/auto_approve.py:1104-1108`). 그대로 두면 스위치가 꺼져 있거나 활성화 프로필의 회차 상한을 넘은 계획 결속 매수도 현재가 아래로만 내면 그 길로 자동승인된다. 그래서 분류기는 계획 결속 rung을 만나면 계획 게이트가 넘긴 `mandate_binding`이 있을 때만 판정을 계속하고, 없으면 `mandate_not_eligible`로 거부한다(사람 카드). 이 거부는 T4a가 넣는다. 거부를 더하는 변경이라 완화가 아니다. (강등)
1. 스위치: `MANDATE_AUTO_APPROVE_ENABLED=true` AND 시장이 `MANDATE_AUTO_APPROVE_MARKETS`에, 계좌가 `MANDATE_AUTO_APPROVE_ACCOUNT_MODES`에 있음 AND `ORDER_PROPOSALS_AUTO_APPROVE_MODE=expanded`. 셋 다 기본 off/빈 값. (강등)

   1a. 활성화 프로필(§3.6, I18): 그 sleeve의 최신 프로필이 현재 live 조항에 결속되고, 승인 기록이 있고, 효력 세션이 도래했고 만료 전이며, 이 제안의 회차 번호 ≤ `max_tranche_index`. 스위치가 켜져 있어도 프로필이 없거나 회차가 상한을 넘으면 자동승인은 없다. (강등)
2. 제안이 계획 결속: `mandate_plan_id`·`mandate_tranche_index` 설정, `sleeve_id`·`account_mode`·`market`·정규화 `symbol`이 계획과 일치(I15, 생성 때·결속 소비 때·fence 트랜잭션에서), 해당 회차의 마지막 이벤트가 `tranche_proposed`이고 그 `proposal_id`가 이 제안, 그리고 **이벤트 스트림을 접은 계획의 권한 상태(`fold_plan_events`)가 `approved`(1차) 또는 `active`(2·3차)이고 suspended·thesis_broken·retired가 아님**(I16). 회차 이벤트 뒤에 계획이 정지·폐기·논지 훼손되면 이미 만든 제안에는 전송 fence가 커밋되지 않는다(§4.6). (차단)
3. sleeve 게이트 통과(`gate.py:57-98`): live, 손실 한도 미만, 예산 안. (강등 — ROB-1470의 기존 원칙 그대로)
4. 위임장 조항: 현재 live 버전에 조항 행 있음, 그 조항의 승인 기록이 유효(I21), 만료 전, 계획의 **현재 결속 조항**(마지막 `approved`/`terms_rebound` 이벤트의 `terms_id`)이 **현재 live 버전의 조항 행 id와 같음**. 해시가 같다는 이유만으로 승계하지 않는다 — 버전이 바뀌면 해시가 같아도 재승인(`terms_rebound`)이 필요하다(Q3 = B, 결정됨). 그 이벤트의 `effective_session`이 도래해야 한다. (조항 없음·만료·재승인 대기는 강등, 승인 기록·해시 불일치는 차단)
5. 주문 모양: `action='place'`, `order_type='limit'`, `side='buy'`, `exit_intent IS NULL`, 회차 1개당 rung 1개. 계획 결속 열은 이 모양의 제안에만 달 수 있다(다른 모양은 제안 생성이 거부한다). (제안 생성 거부)
6. 가격 입력의 유효값과 근접가:
   - 기존 검사 그대로: `current_price`·`limit_price`·`quantity`가 모두 유한한 양수(§2.3 F6). 하나라도 아니면 `price_or_quantity_missing`. 이 검사는 계획 결속이 풀지 않는다. (강등)
   - 매도 1호가(ask)는 **별도 입력**이다. 지금의 KIS·Toss preview는 `current_price`만 주고 매도 1호가가 없다(`app/mcp_server/tooling/order_validation.py:1078`, `app/mcp_server/tooling/orders_toss_variants.py:1471`). 그래서 venue별 호가 읽기(KIS 국내 호가 TR / Toss 호가, US는 해당 venue 호가)를 관측 시각과 함께 읽는다(티켓 T4d). ask는 유한한 양수여야 하고 관측 시각이 신선도 상한(시험 값 5초, Q10) 안이어야 한다.
   - 지정가 ≤ 전송 직전 ask × (1 + `entry_max_premium_bps`/10000)을 호가 단위로 내림한 값.
   - **ask는 대체하지 않는다.** ask가 없거나 유한한 양수가 아니거나 stale이면 `current_price`로도, Toss의 KIS 시세 폴백(§2.3 F7)으로 얻은 값으로도 대신하지 않는다. 그 폴백은 `current_price` 입력만 채우는 기존 동작이고 계획 결속에서도 그 뜻 그대로다. ask가 없으면 사람 카드(`mandate_ask_unavailable`). (강등)
7. 회차 조건 재검: I5·I6·I7·I8 을 신선한 preview 가격으로 다시 판정. (I5~I7 불충족과 I8 회차 잔여 초과는 강등, I8 계획 전체 한도 초과는 차단)
8. 금액(§4.6, I20): 제안 notional ≤ min(전역 per-order cap, 조항 `per_order_cap`) AND 전역 누계 + notional ≤ 전역 daily cap AND 바구니 누계 + notional ≤ 조항 `daily_cap`. 두 누계는 따로 재고 **둘 다** 통과해야 한다. (강등)
9. 업종 상한(I4, 강등), 낙폭 동결 아님(I11, `buy_freeze_state`, §5.4, 차단). 동결 판정(hk 4900)이 없으면 이 조건은 참이 될 수 없다 — 판정 함수 부재·읽기 실패·측정 stale은 동결과 같다.

   9a. 파킹 면제 없음: 계획 대상 심볼은 파킹 allowlist 밖(I17)이고, 분류기는 결속이 있는 rung에 파킹 daily-cap 면제(`auto_approve.py:835`, `:1072`)를 적용하지 않으며, 자동승인 일 누계 reader도 계획 결속 rung을 일반 누계에 넣는다(`app/services/order_proposals/service.py:121`, `:4050`의 파킹 제외에 걸리지 않게).
10. 기존 공통 게이트: approval-required 태그 스캔 통과, veto 가능 계좌·시장, thesis 존재, 정규장(KR XKRX 정규장, US XNYS 정규장), 신선한 preview `success=true`. (기존 동작 그대로)

### 4.2 제안을 만드는 쪽

- 1차: 아침 묶음 승인(§5.1)에서 승인된 계획마다 `tranche_armed(1)` → 장중 운영자 CLI 또는 세션이 `order_proposal_create(mandate_plan_id=…, tranche_index=1)`로 제안을 만든다. 지정가는 생성 시점 매도 1호가 + N bp 이내.
- 2·3차: 가격 트리거가 armed를 만들고, 같은 경로로 제안을 만든다. 트리거 소스(fillwire 가격 트리거 또는 일봉 종가 스캔)는 별도 티켓(T7). 트리거는 **제안만 만든다** — 전송 권한은 §4.1이 dispatch에서 판정한다. 활성화 프로필의 회차 상한이 1이면 이렇게 만들어진 2·3차 제안은 사람 카드로 간다(§3.6).
- 미리 걸어 두는 깊은 지정가는 만들지 않는다(자문 §2.1).
- 계획 결속 제안을 만들 수 있는 계좌는 설정 `MANDATE_PLAN_PROPOSAL_ACCOUNT_MODES`(기본 빈 값)에 있는 것뿐이다. 빈 값이면 어떤 계좌에도 계획 결속 제안이 생기지 않는다. 모의 검증 때는 `kiwoom_mock`만 넣는다. live 계좌 값은 T4c(전송 직전 경계)가 머지되기 전의 코드가 받지 않고, 그 뒤에도 넣는 것 자체가 env 변경이라 운영자 승인이 필요한 활성화 단계다(§4.5 "실전 사람 카드 단계"). 이 설정은 게이트를 푸는 스위치가 아니라 새로 닫아 두는 스위치다.
- 제안 생성 때 거부되는 것: 계좌가 위 설정에 없음, 계획 권한 상태가 `approved`/`active`가 아님, 효력 세션 미도래, 같은 회차에 열린 제안이나 결과 불명 제안이 있음(I9), notional이 회차 잔여나 계획 전체 한도를 넘음(I8), 결속 불일치(I15), 낙폭 동결(I11). 거부된 요청은 제안 행을 만들지 않는다.

### 4.3 dispatch와 전송 직전 경계에서의 위치

판정 지점은 둘이다.

**(가) dispatch 계획 게이트.** 두 단계다. 가격이 필요 없는 판정은 revalidation(브로커 호출) 전에, 신선한 가격이 필요한 판정은 분류기 안에서 한다.

- **1단계(가격 무관)**: `dispatch_proposal`의 sleeve 게이트(`dispatch.py:1171-1189`) **바로 뒤**, revalidation 전. 계획 결속 제안이면 sleeve 게이트에 들어가기 **전에** 바구니 lock을 먼저 잡는다(lock 순서는 §4.6). sleeve advisory lock 아래에서 계획 이벤트·조항·승인 기록·활성화 프로필을 읽고 I4(업종)·I9(중복·불명)·I11(동결)·I12·I15·I16·I18·I21과 I7(회차 순서)을 판정하고, 바구니 누계의 현재 값 B를 읽는다. 전부 통과하면 `mandate_binding`(ROB-1460의 `mechanical_binding`과 같은 모양, `dispatch.py:384-400`)을 만든다. 하나라도 실패하면 결속은 `None`이고 사유를 기록한다.
- **2단계(가격 필요)**: 분류기 안, 신선한 preview와 매도 1호가가 있는 시점. `mandate_binding`이 있을 때만 I5·I6(가격 트리거·간격 재검)·I8(회차 잔여·계획 전체 한도)·근접가(§4.1 조건 6)·주문당 한도·두 누계(§4.6, 금액은 max(지정가, 현재가) × 수량)를 판정하고, 통과하면 §4.4의 분류기 게이트 하나(D2)를 푼다. B는 1단계에서 읽은 값을 쓴다 — 바구니 lock을 계속 쥐고 있으므로 그 사이에 바뀌지 않는다.
- `mandate_binding`이 `None`인 계획 결속 rung은 분류기가 `mandate_not_eligible`로 거부한다(§4.1 조건 0). 이 게이트는 자동승인 여부만 정한다. `mandate_plan_id`가 NULL인 제안은 이 분기에 들어가지 않는다.

**(나) 계획 결속 전송 직전 경계 — fence 트랜잭션의 판정.** 사람이 Telegram에서 승인하면 분류기 없이 `revalidate_and_submit`로 간다(`app/services/order_proposals/telegram_callback.py:879`, `app/services/order_proposals/revalidation.py:1361`). 그래서 dispatch 게이트만으로는 "사람이 승인해도 보내면 안 되는 것"을 막지 못한다. 계획 결속 rung은 승인 방식(자동·사람)과 무관하게 **전송 fence를 쓰는 트랜잭션 안에서**(§4.6) 아래를 다시 읽고, 하나라도 실패하면 fence를 쓰지 않는다. fence가 없으면 전송도 없다.

| 전송 직전 재확인 | 실패 시 |
|---|---|
| 결속 무결성: 계획·회차 행 존재, 심볼·시장·계좌 모드 일치와 계좌 참조 일치(I15), 조항·승인 기록 해시(I21) | 차단 `mandate_binding_mismatch`·`mandate_account_mismatch`·`mandate_binding_broken` |
| 계획 권한 상태 `approved`/`active`, 효력 세션 도래(I16) | 차단 `mandate_plan_not_active` |
| 같은 회차에 다른 열린 제안·결과 불명 제안·닫히지 않은 전송 fence 없음(I9·I24, §4.6) | 차단 `mandate_tranche_duplicate`·`mandate_tranche_uncertain` |
| 계획 전체 한도(I8의 둘째 절) | 차단 `mandate_plan_amount_exceeded` |
| 낙폭 동결 아님(I11) | 차단 `mandate_drawdown_frozen` |
| 위 판정에 필요한 행을 읽지 못함 | 차단 `mandate_unreadable` |

같은 차단 판정(하나의 순수 함수)을 네 곳에서 부른다: 사람 승인 콜백의 nonce 소비 전(§4.7), preview를 얻기 직전(D1 플래그를 세우기 전, §4.4), fence 트랜잭션 안, POST 직전. **권한을 정하는 것은 fence 트랜잭션 안의 판정 하나다** — 그 판정과 fence INSERT가 한 트랜잭션이고, 계획 상태와의 순서는 이벤트 스트림의 `seq`와 전이 트리거가 정한다(§4.6). 나머지 셋은 불필요한 일을 일찍 끊는 best-effort 확인이다. fence가 커밋된 뒤에 계획이 폐기되거나 동결이 걸리면 그 fence의 POST 한 번은 나갈 수 있고(POST 직전 재확인이 보면 막지만 보장은 아니다), 그 금액과 예약은 대사 때까지 남는다.

이 경계는 금액 cap·업종 상한·활성화 프로필·조항 만료를 다시 보지 않는다. 그것들은 자동승인의 조건이고, 사람이 수치를 보고 승인한 제안에는 적용하지 않는다(§4.7). 이 경계는 거부만 한다. 계획 결속이 없는 제안에는 닿지 않는다.

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
| **가격·수량 유효값 검사** `price_or_quantity_missing`: `current_price`·`limit_price`·`quantity`가 없거나 유한한 양수가 아니면 거부 | `auto_trader@ac4f9ed5c` `app/services/order_proposals/auto_approve.py:295-300`, `:869-908` | 그대로 적용. 추가로 매도 1호가도 유한한 양수 + 신선도 상한 안이어야 한다(§4.1 조건 6). 새 입력이 하나 더 생기므로 검사는 늘어난다 | 아니오(오히려 엄격) | 아니오 |
| Toss `current_price` 폴백(#1067): preview에 현재가가 없을 때만 KIS 시세로 `current_price` 입력을 대체 | `auto_trader@ac4f9ed5c` `app/services/order_proposals/auto_approve.py:871-891` | `current_price` 입력에는 기존 동작 그대로. **매도 1호가에는 폴백이 없다** — 폴백 값을 ask로 쓰지 않고, ask가 없으면 사람 카드(`mandate_ask_unavailable`). 두 입력의 차이를 구현이 섞지 않도록 테스트로 고정한다(§4.8) | 아니오 | 아니오 |
| per-order cap(전역) | `auto_approve.py:984-990`, `config/trading_policy.yaml:368-394` | 그대로, 조항 cap이 더 작으면 그 값. notional은 `max(limit, current) × qty`(파킹·ROB-1460과 같은 실행가 기준) | 아니오(오히려 엄격) | 아니오 |
| daily cap(전역) | `auto_approve.py:1072-1080`, `config/trading_policy.yaml:395-410` | 그대로 공유(계획 결속 주문도 전역 일 누계를 소비, Q11 = A 결정됨). 면제 없음. 바구니 누계가 따로 더해져 둘 다 통과해야 한다. 전역 누계 reader는 해제되지 않은 자동 전송 fence도 세도록 고친다 — 더 많이 세는 방향이다(§4.6) | 아니오(오히려 엄격) | 아니오 |
| **KIS·Toss preview/submit의 '현재가 초과 매수' 거부** — 정상 KIS 지정가 매수가 현재가를 넘으면 분류기 전에 거부되고, 예외는 파킹뿐 | `app/mcp_server/tooling/order_validation.py:1118`, `app/mcp_server/tooling/order_execution.py:1986`, revalidation이 분류기 전에 preview를 얻음(`app/services/order_proposals/revalidation.py:1480`, `:1599`) | 매도 1호가 + N bp 지정가는 현재가를 넘을 수 있으므로 **결속된 계획 회차에 한해** preview와 submit 모두에서 상한을 `min(매도 1호가 × (1+N bp))`로 바꾼다. 파킹 예외 플래그를 넓히지 않고 별도 플래그 `allow_mandate_bound_buy`(결속 증거 필수)를 둔다. 이 플래그가 서는 조건은 아래 "D1의 범위" | **예 — 분류기와 별개의 두 번째 완화** | **예** |
| **시장가 근접 매수 거부** `marketable_not_resting`(expanded) / `distance_below_minimum`(off) | `auto_approve.py:1103-1124` | **해제**: 결속이 있고 지정가 ≤ 매도 1호가 × (1+N bp) 일 때만. 파킹 해제(`:1109-1114`)와 같은 자리에 두 번째 해제를 추가 | **예 — 이 설계의 유일한 분류기 완화** | **예** |
| 매수 체결이 veto 카드보다 먼저 날 수 있음(근접가 매수의 결과) | 위 해제의 결과 | 알림은 체결 뒤일 수 있다. veto 버튼은 미체결 잔량 취소만 가능 | **예**(사람 확인 전 체결 위험 수용) | **예**(위와 같은 record에 명시) |
| `min_distance_pct` 3% 하한 | `config/trading_policy.yaml` `min_distance_pct: 3`(`auto_approve.py:1100-1102`) | expanded는 원래 0, 계획 결속도 0 | 아니오(expanded 기존 동작) | 아니오 |
| 승인 창(세션) 경계·전송 직전 재표본 | `dispatch.py:1080-1094`, `revalidation.py:330` | 그대로. 계획 결속은 정규장만(NXT·애프터·US pre/post 없음) | 아니오 | 아니오 |
| sleeve 게이트 | `dispatch.py:1171-1189` | 선행 조건 | 아니오 | 아니오 |
| Toss 동일 세션 freeze | `dispatch.py:1110-1127` | 그대로 | 아니오 | 아니오 |
| thesis 필수 | `auto_approve.py:1267-1273`, `dispatch.py:1128-1142` | 계획 `thesis`를 제안 thesis로 | 아니오 | 아니오 |
| 손실매도 가드(sell preview) | `auto_approve.py:1126-1145` | 매수만 대상이라 해당 없음 | 아니오 | 아니오 |
| 3792 결정 항목 4 / 결정 §1-3: KR·US 신규 매수 제안 off | 운영 결정(정책 YAML에 아직 없음, 결정 §5-1). 승인 기록: `decision/2026-11-12/at-b02-reconcile-and-3792` §hk 3792 항목 1·4 | 계획 결속 매수만 예외로 연다 | **예 — 운영 결정 범위 안의 재개**(3792 결정 항목 4가 "바구니 ① 계획으로만 연다"고 명시) | **예**(같은 record에 근거 결정 인용) |
| 모의 → 실전 승격 | 운영 결정 1c | 스위치·allowlist 변경과 활성화 프로필 승인은 사람 승인 | 해당 없음(활성화 절차) | record의 `effective_at` |

정리: 완화는 넷이다. deviation record에 이 번호 그대로 적고, 실전 활성화 승인(§4.5 체크리스트)은 이 네 번호를 하나씩 인용한다.

| 번호 | 완화 | 위 표의 행 |
|---|---|---|
| D1 | 계획 결속 매수에 한한 KIS·Toss preview/submit '현재가 초과 매수' 거부 해제 | "KIS·Toss preview/submit의 '현재가 초과 매수' 거부" |
| D2 | 분류기 `marketable_not_resting` 해제 | "시장가 근접 매수 거부" |
| D3 | 그 결과인 veto 전 체결 위험 수용 | "매수 체결이 veto 카드보다 먼저 날 수 있음" |
| D4 | 신규 매수 off 결정에 대한 계획 결속 예외 | "KR·US 신규 매수 제안 off" |

**D1의 범위.** `allow_mandate_bound_buy`는 다음이 전부 참일 때만 선다: (1) 스위치 `MANDATE_AUTO_APPROVE_ENABLED`가 켜져 있고 그 시장·계좌가 두 allowlist에 있다, (2) 제안이 계획 결속이고 preview를 얻기 직전의 차단 판정을 통과했다(전송 직전 경계와 같은 판정 함수다, §4.3 (나). D1은 preview 단계에서 이미 필요하므로 그 판정을 preview 앞에서 한 번 더 한다), (3) 신선한 매도 1호가가 있다, (4) 지정가 ≤ 매도 1호가 × (1+N bp)의 호가 단위 내림. 승인 방식(자동·사람)과 활성화 프로필의 회차 상한은 D1의 조건이 **아니다**: 스위치가 켜진 계좌에서는 사람이 승인한 계획 결속 매수도 근접가로 나갈 수 있다(그렇지 않으면 강등된 근접가 제안을 사람이 승인해도 기존 가드가 거부한다). 스위치가 꺼져 있으면 D1은 닫혀 있고, 사람이 승인한 계획 결속 매수는 기존 가드대로 지정가가 현재가 이하일 때만 나간다. 그래서 D1은 기본 off 스위치 뒤에만 있고, 스위치 off에서 preview·submit의 결과는 변경 전과 같다(§4.8 V12·V13).

완화하지 않는 것: 파킹 daily-cap 면제는 계획 결속에 적용하지 않는다(§4.1 9a). 가격·수량 유효값 검사, 캡, 태그 스캔, 세션, sleeve, 손실매도 경로는 그대로다. 계획 결속은 매수·`place`·`exit_intent` 없음만 대상이므로 파킹 매도, cash-funding 매도, 손실 매도의 어느 예외 경로에도 들어가지 않는다. 이 세 가지는 전용 테스트로 고정한다(§4.8). 캡을 "유지한다"는 주장은 §4.6의 누계 계약과 §4.8의 경쟁 테스트가 통과해야 성립한다.

D4와 정책의 관계: 결정 §1-3의 "신규 매수·물타기 티어 끄기"는 아직 정책 YAML에 반영되지 않았다(§9). 그 반영은 별도 티켓이고 이 설계의 티켓이 아니다. 다만 그 티켓이 끝나지 않은 채 D4를 실전에서 열면 "계획으로만 연다"가 성립하지 않으므로, 그 티켓의 완료(또는 운영자 예외 기록)는 실전 활성화의 선행 조건이다(§4.5 체크리스트 C6).

### 4.5 deviation record 초안 위치와 활성화 절차

- 파일: `docs/contracts/policy-freeze-deviation-<머지일>-mandate-plan-bound-auto-approve.md`. 형식은 `policy-freeze-deviation-20261102-mechanical-loss-cut.md:1-60`과 같다. 이 설계 PR에는 넣지 않는다. 완화가 코드에 들어오는 PR이 넣는다: D4(신규 매수 off에 대한 계획 결속 예외)는 실계좌에 계획 결속 제안이 생길 수 있게 되는 T4c PR이 이 파일을 만들면서 적고, D1~D3은 T5 PR이 같은 파일에 더한다.
- 초안 필드:
  - `change_id: hk3795-mandate-plan-bound-auto-approve`
  - `operator_decision_ref`: `decision/2026-11-10/at-new-strategy-go` §1-1·§3, `decision/2026-11-16/at-strategy-direction` "이 결정이 주지 않는 것", `decision/2026-11-12/at-b02-reconcile-and-3792` §hk 3792 항목 1·4(그 근거 보고서 `report/2026-11-12/at-3792-holdings-classification` 승인 요청 4), `decision/2026-11-18/at-3794-3795-recommended` §hk 3795와 "이 결정이 주지 않는 것"
  - `relaxations`: §4.4의 D1~D4를 번호 그대로
  - `change_class: [auto_approval_guard_relaxation_behind_default_off_switch, marketable_buy_release_plan_bound]`
  - `affected_policy_keys`: 없음(정책 값 변경 0) — 위임장 수치는 DB 조항. 새 env는 다섯이다: 완화를 여는 스위치 3개(`MANDATE_AUTO_APPROVE_ENABLED`·`_MARKETS`·`_ACCOUNT_MODES`), 계획 결속 제안 생성 계좌를 닫아 두는 `MANDATE_PLAN_PROPOSAL_ACCOUNT_MODES`(T4a), 위임장 승인자를 닫아 두는 `MANDATE_APPROVER_TELEGRAM_USER_IDS`(T3a·T3b). 뒤의 둘은 완화가 아니고 기본값이 "아무것도 안 됨"이다
  - `consumer_paths`: `app/services/order_proposals/auto_approve.py`, `dispatch.py`, `revalidation.py`, `repository.py`·`service.py`(전역 누계 reader 보강 — 당일 자동 fence는 스위치와 무관하게 모든 자동승인의 누계에 들어가고, 스위치가 켜졌거나 당일 fence가 남은 계좌 모드·시장에서는 계획 결속이 아닌 자동승인도 모든 키를 합산한 같은 G를 읽는다. 더 엄격해지는 변경이고, 스위치가 꺼져 있고 당일 fence도 없으면 변경 전과 같다. L4a lock은 모든 자동승인 dispatch가 항상 잡는다), `app/mcp_server/tooling/order_validation.py`, `order_execution.py`, `orders_toss_variants.py`, `app/services/mandate/`(신규)
  - `effective_at`: 완화마다 다르다. D4는 실전 사람 카드 단계(§4.5 4단계: 실전 위임장 버전 승인 + 계획 결속 제안 생성 계좌 설정) 뒤. D1은 스위치 3개가 그 시장·계좌에 설정된 뒤(활성화 프로필과 무관, §4.4 "D1의 범위"). D2·D3은 스위치 + 활성화 프로필 승인 기록 뒤(§4.5 5단계). 어느 것도 머지만으로는 켜지지 않는다.
  - `rollback`: `MANDATE_AUTO_APPROVE_ENABLED=false` 다음 설정 로드부터 모든 계획 결속 제안은 사람 카드이고 D1도 닫힌다. 스위치는 새 자동승인만 닫는다 — 이미 남은 당일 자동 fence는 그날 모든 자동승인의 전역 누계에 계속 들어가고(§4.6), 닫히지 않은 fence는 대사 때까지 그 회차를 막는다. 계획 결속 제안 자체를 멈추려면 `MANDATE_PLAN_PROPOSAL_ACCOUNT_MODES`를 비운다. 이미 브로커가 받은 주문은 되돌리지 않고 대사로 처리.
- 활성화 절차(순서, 각 단계는 그 단계의 운영자 결정이 따로 필요하다. 이 문서는 어느 단계도 승인하지 않는다):
  1. 마이그레이션 적용(운영 마이그레이션 승인).
  2. 모의 위임장 버전: 먼저 `MANDATE_APPROVER_TELEGRAM_USER_IDS`에 운영자의 Telegram 사용자 id를 넣는다(env 변경. 비어 있으면 어떤 위임장 승인도 기록되지 않는다, §3.5). 그 뒤 키움 mock sleeve에 대해 운영자 CLI가 승인 요청(조항 + sleeve 값)을 만들고, 운영자가 Telegram 카드로 승인하면 `live` 버전과 조항이 같이 생긴다(§3.5). `MANDATE_PLAN_PROPOSAL_ACCOUNT_MODES`에 `kiwoom_mock`을 넣는다(env 변경).
  3. 모의 검증: 키움 mock에서 같은 계획 경로로 4주, 30건, 자문 §2.1 기계 지표(체결률 ≥80%, 슬리피지 중앙값 ≤0.3%, uncertain·중복 0). 지금 sleeve CHECK는 `kis_live`/`toss_live`/`upbit`만 받고(`app/models/strategy_sleeves.py:35-41`) KIS mock은 제안 제출 조합에 없다(`app/services/order_proposals/service.py:358`). 그래서 티켓 T1m-b가 sleeve 계좌 조합에 `kiwoom_mock`(KR)을 **모의 전용**으로 추가한다(추가형 CHECK 변경, live 조합·게이트 불변). 키움 mock 제안은 수동 승인 전용이므로(`approval-and-shadow-contracts.md` 키움 절) 모의는 계획 경로·호가 근접 지정가·체결 투영·기계 지표만 검증하고 분류기 해제는 검증하지 않는다(Q6 = A 먼저, 이후 C. 결정됨). **4주·30건은 계획 경로의 증거이지 D1~D3 완화가 맞게 동작한다는 증거가 아니다.** 완화 경로는 §4.8의 전송 없는 검증이 따로 증명한다. KIS mock은 제안 조합이 생긴 뒤 별도.
  4. **실전 사람 카드 단계**: 체크리스트의 "사람 카드" 항목을 전부 채운 뒤, 실전 sleeve의 위임장 버전 승인(§3.5)과 `MANDATE_PLAN_PROPOSAL_ACCOUNT_MODES`에 live 계좌를 넣는 env 변경. 이 단계부터 실계좌에 계획 결속 매수가 **사람 카드로** 나갈 수 있다. 자동승인은 아직 없다(스위치 off, 프로필 없음).
  5. **실전 자동승인 단계(카나리)**: 체크리스트의 나머지 항목을 채운 뒤, 활성화 프로필(`max_tranche_index = 1`)을 승인하고 스위치 3개를 시장·계좌 하나씩 연다(권고: Toss KR 먼저). 종목 3~5개.
  6. 각 확대는 별도 승인이다: 시장·계좌 추가는 스위치 변경, 2·3차 회차 허용은 새 활성화 프로필 버전(§3.6).

**실전 활성화 체크리스트.** 실계좌에 첫 계획 결속 매수가 나가는 것은 4단계(사람 카드)이므로, 실제 돈과 관련된 선행 조건은 4단계 앞에 둔다. 운영자가 4단계·5단계를 승인하기 전에 설계 세션이 그 단계의 항목마다 증거 위치를 붙여 올린다. 하나라도 비면 올리지 않는다. 항목을 건너뛰는 것은 운영자 예외 기록(hk 결정 문서 key)이 있을 때만이고, 그 key를 그 항목에 적는다.

| # | 필요 시점 | 항목 | 증거 | 출처 |
|---|---|---|---|---|
| C1 | 사람 카드 | T1·T1m-a·T1m-b·T2·T3a·T3b·T4a·T4b·T4c·T4d·T9 머지, 운영 마이그레이션 적용 | PR·head SHA, alembic 현재 리비전 | §8 |
| C2 | 사람 카드 | hk 4900의 동결 판정이 **구현되어** 머지됐고(설계 문서 머지가 아니다) §5.4의 요구를 테스트로 충족 | PR·테스트 이름 | §5.4, 리뷰 §5 |
| C3 | 사람 카드 | 키움 mock **모의 검증 기간 충족**: 실제 mock 계좌에서 사람 카드로 끝까지 30건·4주, 기계 지표 충족. 테스트 fixture 통과, 그리고 한 번 끝까지 돈 "모의 통합 통과"(§8)와 구분해 적는다 | 검증 보고서 hk key | §4.5 3단계 |
| C5 | 사람 카드 | 현금 규칙 B(결정 §1-4) 작업 완료. 1차 분할이 쓸 현금과 2·3차 대기금이 규칙 B대로 준비됐는지 | 그 작업의 PR·보고서 hk key, 또는 운영자 예외 기록 key | 결정 2a·4, 리뷰 §1 2a |
| C6 | 사람 카드 | 결정 §1-3(KR·US 신규 매수 제안 off, 물타기 티어 off)의 정책 반영 완료 | 정책 PR·`policy_version`, 또는 운영자 예외 기록 key | §4.4 D4, 리뷰 §3 |
| C7 | 사람 카드 | 편입·신규 매수 재개의 승인 기록 확인. 그 결정의 조건(실제 분할 매수는 백테스트·사전등록·위임장 승인 뒤)이 충족됐는지 | `decision/2026-11-12/at-b02-reconcile-and-3792` §hk 3792 항목 1·4, 백테스트(hk 3793)·사전등록 기록 | 리뷰 §1 |
| C8 | 사람 카드 | 손실 매도 가드(I14)가 머지돼 있다 — T4c에 들어 있으므로 C1에 포함되고, 임시 절차로 대신할 수 없다. 그 위에서 논지 훼손 **판정의 자동화**(T8) 준비 여부: 준비됐으면 PR. 아니면 임시 운영 절차를 문서로 적고 운영자가 그 절차로 여는 데 동의한 기록. 임시 절차의 순서는 §5.2의 "증거의 순서" 그대로다: 승인 이슈 확보 → 새 `thesis_change` 회고 기록(id 확보) → 그 두 id를 실어 `thesis_broken`(72시간 뒤면 `thesis_evidence_refreshed`)을 CLI로 기록 → 같은 두 id를 인용해 기존 손절 2클릭 카드. 이벤트를 먼저 쓰고 회고를 나중에 만들면 가드가 받지 않는다. 이 순서 전체를 T4c의 통합 테스트가 한 번 돈다(§5.2 테스트 계약 (i)) | T4c PR(가드와 §5.2의 테스트), T8 PR 또는 임시 절차 문서 + 운영자 기록 key | 리뷰 §5 |
| C10 | 사람 카드 | ROB-1460 live 설정 상태가 Q7 원칙과 맞는지(§5.2 영향 표에서 운영자가 고른 방식): A1이면 시장 목록에 kr이 없음, A2면 그 코드 변경의 머지와 테스트 | 배포 env의 key 이름별 확인 결과(값은 적지 않는다) 또는 A2의 PR, 운영자 확정 기록 key | §5.2, §7 Q7 |
| C12a | 사람 카드 | deviation record가 D4를 담고 머지됨(T4c PR) | 파일 경로·PR | §4.4 |
| C11 | 사람 카드 | 체결을 부기하는 대사의 실행 담당과 주기. 계획의 `tranche_filled`·정리 이벤트·불명 해소는 authoritative 체결이 원장에 있어야 진행된다(I10, §3.7). 2026-11-18 결정의 정기 실행(hk 3794)은 종료 상태 전용이고 체결 부기는 기존 게이트 뒤에 그대로다 | 실행 담당·주기를 적은 기록 | §3.7, §6 |
| C4 | 자동승인 | T5 머지. §4.8의 전송 없는 검증 산출물 V1~V22이 그 PR에서 전부 통과 | T5 PR의 테스트 목록·CI 실행 | 리뷰 R4 |
| C9 | 자동승인 | 근접가 프리미엄 실전 값 확정: Q10의 측정 증거 목록 전부와 운영자가 정한 값. 조항의 값과 다르면 새 위임장 버전과 계획 재승인이 먼저다(§7.1) | 측정 보고서 hk key, 활성화 프로필(값이 바뀌면 조항) 승인 요청의 `approval_ref` | §7.1 |
| C12 | 자동승인 | deviation record에 D1~D3이 번호대로 더해져 머지됨(T5 PR) | 파일 경로·PR | §4.4 |
| C13 | 자동승인 | 승인 화면 표시 항목(아래)이 활성화 프로필 승인 카드에 전부 나온다 | 카드 렌더 테스트 + 실제 카드 캡처 | 리뷰 §4 |
| C14 | 자동승인 | 기존 자동승인 누계가 결과 불명 일반 자동 전송을 놓치는지(§2.3 F3a)의 감사 결과. 놓친다면 그 수정, 또는 그 범위를 적은 운영자 수용 기록 | 감사 보고서 hk key 또는 수용 기록 key | §4.6 "남는 구멍" |

**승인 화면 표시 항목(리뷰 §4).** 활성화 프로필 승인 카드(§3.5)와 5단계 승인 요청은 아래를 한 화면에 보여 준다. 숫자는 그 시점의 조항·계획·원장에서 계산한다.

1. **사람이 보기 전에 나갈 수 있는 주문 원금**: 이 프로필로 자동승인될 수 있는 회차 금액의 합(예: 종목당 계획 약 235만 × 40% = 약 94만/1차, 3~5종목이면 약 282~470만). 조항 `per_order_cap`·`daily_cap`, 전역 cap, sleeve 잔여 예산 중 더 낮은 것이 있으면 그 값과 함께.
2. **하루 금액 밖의 노출**: 기존 보유(편입액)의 손실과, 이후 거래일에 쌓이는 회차는 위 하루 금액에 들어 있지 않다.
3. **지금 미결인 주문**: 이 바구니에 결속된 열린 제안·브로커 접수 주문·결과 불명 주문의 수와 금액.
4. **취소로 되돌릴 수 없는 부분**: veto 버튼은 미체결 잔량만 취소한다. 이미 체결된 수량은 되돌리지 못한다. 자동 취소 보상이 실패하거나 체결과 겹치는 경우의 실제 복구 결과는 이 설계에서 실행으로 검증하지 않았다.
5. **30bp(또는 확정된 N)의 뜻**: 주문 시점 매도 1호가 대비 지정가 상한이다. 손실 한도가 아니다.
6. **−25%의 뜻**: 추가 매수 동결이다. 기존 포지션의 손실을 −25%로 막는 손절이 아니다. 동결 뒤에도 보유는 더 떨어질 수 있다.
7. **cap의 뜻**: 주문 노출의 상한이지 손실 보장이 아니다. 레버리지 없는 현금 매수이므로 주문 원금의 대부분 또는 전부를 잃을 수 있고 수수료·세금은 별도다.
8. 이 프로필의 회차 상한, 만료, 결속 조항 해시, D1~D4.

### 4.6 전송 경계 — 전송 fence, 전역 누계와 바구니 누계 (R3)

리뷰 R3: 계획 등록은 바구니 lock을 쓰는데 소비는 sleeve lock만 썼고, "그날 자동승인 누계"가 전역인지 바구니인지, 조항 `daily_cap`이 KIS·Toss 합산인지가 분명하지 않았다. 아래가 그 계약이다.

원칙은 하나다 — **권한의 단위는 커밋된 전송 fence다.** advisory lock은 권한의 근거가 아니다. lock은 그 DB 세션이 끝나면 풀리므로, "lock을 쥔 채 브로커 호출까지 간다"는 것은 보장이 될 수 없다(앞 판 `3bedbc2`가 그렇게 적었고, 연결 상실에서 깨진다). 이 문서는 lock을 짧은 DB 트랜잭션 하나 안의 판정을 직렬화하는 데만 쓰고, 전송 권한은 DB에 커밋된 행으로만 정한다.

**요구사항(확정).** 전송 fence 트랜잭션이 전송 권한의 **유일한 경계**다. 다음을 전부 fence 트랜잭션 **안에서** 다시 확인하고, 그 확인과 fence INSERT를 각 사실의 writer와 **원자적으로 직렬화**해야 한다.

- 전역 누계와 바구니 누계의 확인과 예약(자동 fence) — 누계에 더하는 다른 fence, 다른 자동승인과.
- 최신 활성화 프로필과 sleeve·조항의 자동승인 자격(자동 fence) — 프로필·조항·sleeve 버전의 writer, 스위치와.
- 계획 이벤트(권한 상태) — 계획 이벤트 writer와.
- 동결 — 동결 writer와.

호출자(dispatch, 승인 콜백) 쪽의 판정은 **참고용**이다. 호출자의 DB 세션이 죽거나 호출자가 쥔 lock이 풀려도 fence가 커밋되는지 여부는 달라지지 않아야 한다.

**미확정.** 위 요구사항을 만족하는 구체 메커니즘(공통 lock 키, DB 제약)은 **hk 5286**이 확정한다. 그 전에는 T4c 구현을 시작하지 않는다(§8). 이 절의 아래 내용 가운데 메커니즘에 해당하는 부분 — fence 트랜잭션이 어떤 lock을 잡는지, 누계와 자동승인 자격을 어디서 확인하는지, lock 표 — 은 PR #2466 리뷰 5차 시점의 초안이고 요구사항을 다 채우지 못한다: 누계 확인이 dispatch의 lock에 기대고(lock 표 아래 마지막 항목), fence 트랜잭션이 활성화 프로필과 sleeve·조항 자격을 다시 보지 않는다. 요구사항과 어긋나는 곳은 요구사항이 우선하고, hk 5286이 그 부분을 고쳐 쓴다. 그 PR의 리뷰 스레드 r4618309254와 r4618309267이 이 항목으로 넘어갔다(설계 세션의 범위 자르기, hk 5272).

**fence가 권한이다.**

1. 계획 결속 매수 rung의 브로커 호출은 그 시도의 `tranche_send_fenced` 이벤트가 **커밋된 뒤에만**, 그 fence 하나에 **POST 한 번**만 한다. 하위 계층의 재시도가 같은 fence로 다시 보내지 못한다. 자동승인이든 사람 승인이든 같다.
2. fence는 계획 이벤트 스트림의 이벤트다. 이벤트는 `seq` 순서로 하나씩만 붙고(`UNIQUE(plan_id, seq)`, 직전+1), INSERT 트리거가 전이 표를 강제한다(§3.4). 그 트리거는 **권한 상태가 `approved`/`active`가 아닌 계획에 대한 fence INSERT를 거부한다** — 그 `seq` 앞에 `retired`·`suspended`·`thesis_broken`(그리고 `rejected`·`closed`·`exhausted`)이 있으면 fence는 붙지 않는다. 순서를 정하는 것은 이벤트 스트림의 `seq`이고 DB 제약이다. 쓰는 쪽의 세션이나 lock에 기대지 않는다.
3. 그래서 `retired`·`suspended`·`thesis_broken`은 **그 뒤에 커밋되는 fence만 막는다.** 그 전에 커밋된 fence는 POST 한 번까지 나갈 수 있다 — fence 커밋과 POST 사이에 전이가 커밋돼도 그렇다. 그 주문은 전이보다 먼저 권한을 얻은 전송이다.
4. 커밋된 fence의 금액은 누계에 들어가고(자동 fence), 그 회차의 금액과 그 계획의 예약·예산·업종 몫은 그 fence가 **대사로 닫힐 때까지** 풀리지 않는다. 닫힘은 둘뿐이다: 전송됐고 그 주문의 종결이 원장으로 증명됨, 또는 미전송이 증명됨(§3.7의 `orders_settled`가 "닫히지 않은 fence 0"을 요구한다). 폐기된 계획도 마찬가지다.
5. 낙폭 동결도 같은 형태다: 동결(측정 불가·stale 포함)이 커밋된 뒤에는 그 범위의 fence가 커밋되지 않고, 그 전에 커밋된 fence는 POST 한 번까지 나갈 수 있다(§5.4).

**fence 트랜잭션.** fence는 호출자(dispatch 또는 승인 콜백)와 **다른 세션의 짧은 트랜잭션 하나**로 쓴다. 자동·사람 경로가 같은 것을 쓴다.

1. 동결 범위 lock LF를 공유로 잡는다(전역, 그 바구니). hk 4900의 측정 게시·동결·해제 writer는 같은 lock을 배타로 잡고 쓴다. 이 lock은 이 트랜잭션 안에서만 쥔다 — 동결 게시와 fence 커밋 중 무엇이 먼저인지를 정하기 위한 것이다.
2. 계획 이벤트를 다시 접어 전송 직전 차단 판정(§4.3 (나))을 하고, `buy_freeze_state`를 읽고, 계좌 참조를 대조한다(아래 "계좌 결속").
3. `tranche_send_fenced`(계획·회차·`proposal_id`·rung·계좌 참조·승인 방식·금액·KST 일·시도 id, 자동이면 활성화 프로필)를 INSERT한다. 트리거가 위 2번 규칙을 강제하고, `seq`가 겹치면 INSERT가 실패한다.
4. 커밋한다. 커밋을 확인하지 못하면 브로커 호출을 시작하지 않는다(`mandate_fence_unavailable`).

- 이 트랜잭션은 호출자가 쥔 lock을 기다리지 않는다: sleeve·바구니·누계 lock을 잡지 않고, 제안 행의 행 lock도 기다리지 않는다. 그래서 `mandate_plan_events.proposal_id`에는 FK 제약을 두지 않고(승인 콜백은 제안 행의 nonce lock을 쥔다 — `auto_trader@ac4f9ed5c` `app/services/order_proposals/telegram_callback.py:668-669`의 주석) 값의 일치는 잠금 없는 SELECT로 확인한다.
- 이 트랜잭션이 도중에 죽으면 fence는 커밋되지 않고 전송도 없다. 중간 상태가 남지 않는다.
- 호출자의 DB 세션이 죽어도 fence 트랜잭션의 판정은 달라지지 않는다. 판정에 쓰는 것은 그 순간 DB에 커밋돼 있는 이벤트와 동결 기록이고, 호출자의 lock이 아니다.

**전송 순서.**

| 단계 | 자동승인 | 사람 승인 |
|---|---|---|
| 판정 | dispatch가 계획 게이트 1·2단계를 통과시킨다(§4.3 (가)). 누계는 dispatch의 lock 아래에서 읽는다(아래 lock 표) | 승인 콜백의 기존 검사(게시 결속, 운영자 principal 대조 §3.5) 뒤, nonce 소비 전에 차단 판정을 한 번 한다. lock 없는 빠른 거부다 |
| fence | 위 fence 트랜잭션(`approval_mode='auto'`) | 위 fence 트랜잭션(`approval_mode='human'`) |
| POST 직전 재확인 | 아래 | 아래 |
| 브로커 호출 | fence 하나에 한 번 | fence 하나에 한 번 |
| 결과 | 접수면 fence 유지, 미전송 증명·명시적 거절이면 해제, 그 밖은 fence 유지 | 같다 |

**POST 직전 재확인은 best-effort다.** 브로커 토큰·rate limit 대기가 끝난 뒤, 어댑터가 HTTP 요청을 내보내기 바로 전(기존 pre-send hook의 자리 — `auto_trader@ac4f9ed5c` `app/services/brokers/toss/client.py:197-199`, `app/services/order_proposals/revalidation.py:1709`)에 새 짧은 트랜잭션으로 계획 상태와 동결 판정(유효 기한 포함)을 다시 읽고, 실제 송신 요청의 계좌를 계획의 계좌 참조와 대조한다. 폐기·정지·논지 훼손·동결이 보이거나 계좌가 다르거나 **읽지 못하면** 보내지 않고 그 fence를 해제한다(증명된 미전송). 이것은 불필요한 전송을 줄이는 것이지 **보장이 아니다**: 이 재확인과 POST 사이에 커밋되는 전이는 막지 못한다. 그 경우는 위 3번 규칙대로다 — 전이보다 먼저 커밋된 fence의 POST 한 번이고, 그 금액과 예약은 대사 때까지 남는다. 기존 승인 창 재표본도 같은 자리에서 그대로 돈다.

**전송 뒤의 fence.**

- 접수(`submitted_acked`·`submitted_resting`)면 fence는 그대로 남는다.
- **미전송이 증명됨**(fence 뒤 브로커 호출 전에 거부됨: POST 직전 재확인, send intent 예약 실패, 승인 창 재표본 실패) 또는 브로커의 명시적 거절이면 그 fence를 가리키는 `tranche_send_released`를 남긴다.
- 그 밖 전부(응답 없음, 시간 초과, 프로세스 사망, 결과 기록 실패, 호출 뒤 트랜잭션 롤백, 모호한 응답)는 **fence를 유지한다**. 같은 시도에 귀속된 양성 증거(원장 대사의 종결)로만 닫힌다.
- **닫히지 않은 fence**: 짝인 해제가 없고, 그 fence의 제안에 대한 회차 종결 이벤트(`tranche_filled`·`tranche_partially_filled`·`tranche_unfilled`, 원장 대사 증거)도 없는 fence. 그 회차에 하나라도 있으면 새 제안도 새 fence도 없다(`mandate_tranche_uncertain`) — 같은 제안의 앞선 시도가 남긴 것이어도, 자동이든 사람이든, 날짜가 바뀌어도 같다. 이번 시도의 fence는 판정 뒤에 쓰므로 스스로를 막지 않는다.
- 사람 승인 콜백은 rung 상태와 nonce 소비를 전송 뒤에 커밋하므로(`auto_trader@ac4f9ed5c` `app/services/order_proposals/telegram_callback.py:893`, `:1130-1133`) 브로커가 주문을 받은 뒤 롤백되면 둘 다 사라지고, 송신 키에는 거래일 salt가 들어가서(`auto_trader@ac4f9ed5c` `app/mcp_server/tooling/order_execution.py:2286-2293`) 다음 거래일의 재승인은 기존 같은 날 중복 방어에도 걸리지 않는다. 따로 커밋된 fence가 그 재전송을 막는다.
- 해제를 쓰지 못하면 fence가 남는다. 그 회차가 대사 때까지 막히고 자동 fence면 그날 cap을 더 차지할 뿐이다 — 안전한 쪽으로 틀린다.

**계좌 결속.** 원래 문서의 계획은 sleeve(계좌 모드·시장)까지만 알고 실제 주문 계좌는 몰랐다. 제안의 `broker_account_id`는 선택 입력이고(`auto_trader@ac4f9ed5c` `app/services/order_proposals/service.py:849-862`), Toss 전송은 제안의 값이 아니라 클라이언트가 해석한 계좌로 나간다(`auto_trader@ac4f9ed5c` `app/services/brokers/toss/client.py:194-195`). 그래서:

- **계좌 참조(`account_ref`)**를 둔다: 브로커 조회로 확인한 그 계좌의 식별자에서 만든 불투명한 참조(키 있는 digest). 원문 계좌번호는 저장하지 않고, 자격증명은 입력이 아니다 — 자격증명을 교체해도 같은 계좌면 같은 참조다.
- 조항(`mandate_terms.account_ref`, 승인 내용 해시에 포함)에 넣고, 계획이 등록될 때 그 값을 계획 행에 옮긴다(`mandate_plans.account_ref`, 불변). 승인 요청을 만들 때 브로커 조회로 확인한다: KIS는 설정된 계좌, Toss는 기존 계좌 목록 검증(목록에 정확히 1건, `auto_trader@ac4f9ed5c` `docs/agent-contracts/broker-and-ledger-contracts.md:1042`의 파킹 제안 계좌 절과 같은 검증)을 재사용, 키움 모의는 그 모의 계좌. 확인하지 못하면 요청을 만들지 않는다.
- 계획은 **보유 범위에 속한 모든 실제 계좌**의 참조도 등록 때 고정한다(`mandate_plans.scope_account_refs`, §3.7). 실계좌 계획이면 KIS와 Toss 둘, 모의 계획이면 그 키움 모의 계좌. 편입액이 0이어도 고정한다.
- 새 위임장 버전의 `account_ref`가 계획의 것과 다르면 그 계획은 재승인(`terms_rebound`)할 수 없다. 계좌가 바뀐 것은 새 계획이다.
- 계획 결속 제안의 `broker_account_id`는 호출자 입력을 받지 않는다. 제안 생성이 계획의 계좌를 가리키는 값으로 채운다.
- I15는 등록·제안 생성·fence 트랜잭션·POST 직전에서 계좌 참조를 비교한다. POST 직전에는 어댑터가 **실제 송신 요청에 실리는 계좌**에서 같은 방식으로 참조를 계산한다. 불일치나 계산 불가는 차단이다(`mandate_account_mismatch`).
- fence 이벤트에 그 참조를 남긴다.

**두 누계의 정의.** 둘 다 `approval_mode='auto'`인 fence로 센다.

| | 전역 누계 G | 바구니 누계 B |
|---|---|---|
| 한도 | 정책 `daily_cap`(시장별, `config/trading_policy.yaml:395-410`) | 조항 `daily_cap`(§3.1의 "가장 엄격한 값") |
| 범위 | `account_mode` × `market` × KST 일 | (영역, `basket_key`) × KST 일. 그 바구니의 모든 sleeve(KIS·Toss) 합산 |
| 계획 결속 전송 | 그날의 해제되지 않은 자동 fence 금액(스탬프 여부와 무관. 스탬프로는 다시 세지 않는다) | 그 바구니·그날의 해제되지 않은 자동 fence 금액 합 |
| 계획 결속이 아닌 전송 | 그날 `auto_approved` 스탬프가 있는 rung(§2.3 F2, 기존 그대로) | 해당 없음 |
| 금액 기준 | 기존 그대로(rung별 durable cap 측정) / fence 금액 | max(지정가, 현재가) × 수량 — per-order cap 판정과 같은 실행가 기준 |
| 사람 승인 주문 | 세지 않는다(기존 그대로). 사람 fence는 재전송 차단과 회차 금액(I8)에만 쓰인다 | 세지 않는다 |
| 미체결·취소·veto | 돌려주지 않는다(기존 그대로) | 돌려주지 않는다. 해제는 미전송 증명과 명시적 거절뿐이다 |
| 전송 결과 불명 | 계획 결속은 fence가 남으므로 포함된다. 계획 결속이 아닌 제안은 기존대로 스탬프가 없을 수 있다(§2.3 F3a) | fence가 남으므로 포함된다 |

- 자동승인 조건은 `G + notional ≤ 전역 daily_cap` **그리고** `B + notional ≤ 조항 daily_cap`이다. 한쪽만 통과하면 사람 카드다.
- 두 값은 대상이 다르다. 예: 조항 `daily_cap` 400만인 바구니가 Toss KR에서 300만, KIS KR에서 90만을 자동승인했다면 B = 390만이고, Toss KR의 G는 300만 + 그 계좌 모드의 다른 자동승인, KIS KR의 G는 90만 + 그 계좌 모드의 다른 자동승인이다. 다음 94만 회차는 B(390 + 94 > 400)에서 걸려 사람 카드로 간다. 반대로 B에 여유가 있어도 다른 전략이 G를 채웠으면 G에서 걸린다.

**전역 누계 reader의 규칙.**

- **당일의 자동 fence는 항상 들어간다.** 그 `account_mode` × `market` × KST 일의 해제되지 않은 자동 fence 금액은, 계획 결속이든 아니든 **모든** 자동승인의 전역 누계 reader에 `MANDATE_AUTO_APPROVE_ENABLED`와 무관하게 더해진다. 스위치(와 두 allowlist)는 **새** 위임장 자동승인만 닫는다. 그렇지 않으면, 결과가 불명인 계획 전송 두 건(각 190만)이 있는 날 스위치만 끄면 일반 자동승인 150만이 그 380만을 보지 못하고 통과해 전역 cap 500만을 넘을 수 있다.
- **키로 나누지 않는다.** [사실] 기존 누계는 `account_mode`·`market`·`broker_account_id`(없으면 `default`) 키별로 따로 센다(§2.3 F1). 같은 실계좌로 나가는 주문이 NULL 키와 다른 값의 키로 나뉘어 세어지면, 키마다 한도 안이어도 합은 한도를 넘는다(계획 fence 100만 뒤에 서로 다른 키의 일반 자동승인 300만이 둘 오면 각자 자기 키 + 100만만 보고 둘 다 통과해 700만이 된다). 그래서 그 계좌 모드·시장에 위임장 자동승인이 열려 있거나(두 allowlist에 있는 조합) 그날 해제되지 않은 자동 fence가 하나라도 있으면, 모든 자동승인이 **모든 `broker_account_id` 키(NULL 포함)의 스탬프 rung + 자동 fence**를 합친 같은 G를 읽는다. 계좌 모드 하나에 실계좌가 하나인 지금 배치(KIS 하나, Toss 하나)에서는 이것이 계좌별 합과 같고, 실계좌가 여럿이 되면 더 엄격할 뿐이다.
- 스위치가 꺼져 있고 그날 자동 fence도 없는 계좌 모드·시장에서만 기존 키별 누계 그대로다(판정 결과가 변경 전과 같다, §4.8 V12).
- **남는 구멍 하나**: 계획 결속이 아닌 자동 전송은 여전히 스탬프를 전송 뒤에 쓰므로, 결과 불명으로 끝난 일반 자동 전송은 G에 없을 수 있다(§2.3 F3a, [추정]). 이 구멍은 이 설계가 만든 것이 아니지만 계획 결속 판정이 읽는 G에 영향을 주므로, 자동승인 단계의 체크리스트 C14가 그 감사 결과 또는 운영자 수용 기록을 요구한다(§4.5).
- 이 변경은 일반 자동승인의 판정을 더 엄격하게만 바꾼다. 완화가 아니지만 기존 경로의 결과가 달라질 수 있으므로 deviation record의 `consumer_paths` 설명에 적는다.

**lock 표.** 전부 트랜잭션 범위 advisory lock이고 그 트랜잭션이 끝나면 풀린다. lock은 한 트랜잭션 안의 읽기와 쓰기를 직렬화할 뿐이고, 전송 권한의 근거가 아니다(위 "fence가 권한이다"). 기다리는 획득은 항상 아래 순서로만 한다.

| 순서 | lock | 키 | 누가 |
|---|---|---|---|
| L0 | 제안별 dispatch | `order_proposals:auto_dispatch:<proposal_id>`(기존, §2.3 F4) | dispatch |
| L1 | 종목 | `mandate_symbol:<realm>:<market>:<symbol>` | 계획 등록, `released` 전이, 예약·해제 테이블 트리거, `loss_cut` 제안 생성의 계획 확인(§5.2) |
| L2 | 바구니 | `mandate_basket:<realm>:<basket_key>` | 계획 등록(I3·I4), 배치 생성(I22), 조항·프로필 승인 콜백, 계획 결속 **자동** dispatch(B를 읽는다) |
| L3 | sleeve | `review.strategy_sleeves:<id>`(기존, `app/services/strategy_sleeves/store.py:139-144`) | sleeve 게이트, 계획 게이트, 계획 이벤트 writer(fence 트랜잭션 제외) |
| L4a | 자동승인 계좌 모드·일 | `order_proposals:auto_approve:<account_mode>:<market>:*:<date>`(신규) | 전역 누계를 읽는 **모든** 자동승인 dispatch(스위치와 무관하게 항상) |
| L4b | 자동승인 계좌·일 | `order_proposals:auto_approve:<account_mode>:<market>:<account>:<date>`(기존, §2.3 F1) | 기존 그대로 |
| LF | 동결 범위 | `mandate_freeze:global`, `mandate_freeze:<realm>:<basket_key>` | fence 트랜잭션은 공유로, hk 4900의 측정 게시·동결·해제 writer는 배타로. 다른 lock과 함께 쥐지 않는다 |

- 기존 순서 L0 → L3 → L4b(§2.3 F4)는 그대로다. L1·L2는 L0과 L3 사이에, L4a는 L4b 바로 앞에 놓인다. 한 트랜잭션이 여러 sleeve lock을 잡을 때(묶음 승인 콜백)는 sleeve id 오름차순이다.
- 계획 결속이 아닌 제안은 L1·L2를 잡지 않는다(`loss_cut` 제안 생성의 L1만 예외, §5.2).
- 계획 이벤트를 append하는 진입점(묶음 승인 콜백, 운영자 CLI, 투영 CLI, 제안 생성과 arming, 논지 훼손 기록)은 그 계획의 sleeve lock(L3)을 쥐고 쓴다. 이것은 `seq` 충돌을 줄이는 관례이고, 순서의 근거는 `UNIQUE(plan_id, seq)`와 전이 트리거다. 이벤트 INSERT 트리거는 advisory lock을 잡지 않는다(종목 lock을 스스로 잡는 것은 예약·해제 **테이블**의 트리거뿐이다, §3.4).
- fence 트랜잭션과 해제 writer는 L3을 잡지 않는다. 자동 경로에서는 dispatch 트랜잭션이 L3을 쥐고 있어 잡으려 하면 자기 호출자를 기다리게 된다. `seq`가 다른 writer와 겹치면 INSERT가 실패하고, 다시 읽어 판정부터 한 번 더 한다(그 사이 폐기됐으면 거기서 끝난다).
- 사람 승인 콜백은 위임장 lock을 잡지 않는다.
- **누계의 직렬화는 dispatch의 lock에 기댄다.** B는 L2 아래, G는 L4a → L4b 아래에서 읽고, 자동 fence는 그 lock을 쥔 동안 커밋된다. 이것은 한도 판정의 동시성 제어이고 전송 권한과는 별개다. dispatch의 DB 세션이 판정과 fence 커밋 사이에 죽으면 그 틈에 다른 dispatch가 같은 누계를 읽을 수 있다. fence 트랜잭션은 INSERT 직전에 B와 G를 자기 트랜잭션에서 한 번 더 읽어 한도를 넘으면 쓰지 않지만, 이것도 best-effort다(동시에 커밋되는 두 fence는 서로를 못 볼 수 있다). 커밋된 fence는 그 뒤의 모든 판정에 들어간다.

**acceptance(테스트 계약).** 사람 경로와 fence·계좌 결속의 항목은 T4c, 자동 경로와 누계의 항목은 T5가 맡는다. 실제 호출을 세는 가짜 브로커를 쓴다.

- fence 선행: fence 커밋 실패를 주입하면 자동·사람 경로 모두 브로커 호출 수가 0이다.
- fence와 전이의 순서(자동·사람 각각): `retired`가 fence보다 먼저 커밋되면 fence INSERT가 트리거에서 거부되고 브로커 호출 0. `suspended`·`thesis_broken`·동결 게시도 같다. fence가 먼저 커밋되면 그 뒤에 `retired`가 커밋돼도 POST는 한 번 나갈 수 있고, 두 번째 POST는 없다.
- **연결 상실**: fence 커밋 → 호출자의 DB 세션 종료 → `retired` 커밋 → POST. 주문은 한 번 나가고, 그 계획의 예약·예산·업종 몫은 유지되며, 그 fence가 대사로 닫히기 전에는 `orders_settled`와 `released`가 거부된다. 누계에는 그 금액이 들어 있다(자동 fence). 같은 순서를 동결 게시로도 돌린다.
- POST 직전 재확인: 재확인 시점에 `retired`·동결이 보이면 호출 0이고 fence가 해제된다. 재확인의 읽기가 실패하면 호출 0이다. 재확인을 통과한 뒤 커밋된 `retired`는 그 POST를 막지 않는다(보장이 아님을 테스트가 그대로 적는다).
- 사람 승인 롤백: 브로커 호출 뒤 승인 콜백 트랜잭션 롤백을 주입하면 rung 상태와 nonce 소비는 사라져도 fence는 남고, 같은 제안을 다시 승인해도(같은 날, 다음 거래일 둘 다) 브로커 호출 수가 늘지 않는다.
- 자동 dispatch 롤백: 브로커 호출 뒤 dispatch 트랜잭션 롤백을 주입해도 fence가 남고 다음 판정의 B·G에 들어간다.
- uncertain 보존: 결과 불명을 주입하면 해제가 기록되지 않고, 같은 회차의 재제안·새 fence가 차단된다. 하위 계층의 재시도가 같은 fence로 두 번째 호출을 만들지 못한다.
- 해제는 증명된 미전송과 명시적 거절에서만 기록된다. 그 외 결과에서 해제가 기록되면 실패다.
- 계좌 결속: 실제 송신 요청의 계좌가 계획의 계좌 참조와 다르면 호출 0(`mandate_account_mismatch`). 호출자가 준 `broker_account_id`는 무시된다. 계좌 참조가 다른 새 위임장 버전으로는 기존 계획을 재승인할 수 없다.
- 두 sleeve 동시 dispatch: 같은 바구니의 Toss·KIS 제안 둘을 동시에 dispatch 했을 때 B가 한도를 넘는 조합은 하나만 자동승인된다(DB 경쟁, CI).
- G와 B 독립: G만 넘는 경우, B만 넘는 경우 각각 사람 카드이고 사유 코드가 다르다.
- 계좌 키(전역 cap 500만): (i) 일반 자동승인이 NULL 키로 400만을 쓴 뒤 계획 결속 200만은 걸린다. (ii) 그 역순도 걸린다. (iii) 계획 자동 fence 100만 뒤 서로 다른 키의 일반 자동승인 300만 두 건 중 두 번째는 걸린다. (iv) 세 경우 모두 동시에 돌려도 한도를 넘는 조합이 통과하지 않는다(L4a).
- 스위치 off와 당일 fence(전역 cap 500만): 결과 불명인 계획 자동 전송 190만 두 건이 있는 날 스위치를 끈 뒤, 일반 자동승인 150만은 G(380 + 150 > 500)에서 걸린다. 다른 `broker_account_id` 키의 일반 자동승인을 동시에 돌려도 같다. 다음 KST 일에는 그 fence가 그날의 누계에 들어가지 않는다(회차 차단은 계속된다).
- lock 순서: 계획 등록·배치 생성·dispatch·승인 콜백·투영·`released` 전이·동결 writer를 섞어 돌려도 교착이 없다. fence 트랜잭션이 LF 말고는 advisory lock을 잡지 않는다는 것을 테스트가 단언한다.
- 기존 경로 회귀: 스위치가 꺼져 있고 그날 자동 fence가 없는 계좌 모드·시장에서 전역 누계 reader의 결과가 변경 전과 같다.

**T4c 검증 계약에 그대로 넣는 두 사례(리뷰 5차).** 위 "요구사항"의 검증이다. 어떤 메커니즘으로 통과시킬지는 hk 5286이 정하고, 사례 자체는 바꾸지 않는다.

- **두 계획, 호출자 세션 상실, 누계**(r4618309254): 기존 G와 B가 각각 250만원이고 일일 상한이 500만원이다. 서로 다른 계획의 150만원 자동 주문 A와 B가 있다(각각 주문당 상한 200만원 안이고, 서로 다른 계획이라 `seq` 충돌이 없다). A가 판정을 통과한 뒤 호출자의 DB 세션을 잃고, 그 사이 B가 같은 판정을 통과한다. 두 fence 트랜잭션을 서로 커밋하기 전에 겹치게 돌린다. **하나의 fence만 커밋돼야 한다**(누계 400만원). 둘 다 250만원을 읽고 통과해 550만원이 자동 전송되면 실패다.
- **세션 상실 뒤 프로필 철회가 먼저 커밋**(r4618309267): 자동 dispatch가 회차 상한 1인 활성화 프로필로 판정을 통과한 뒤 호출자의 DB 세션이 끊긴다. 그 다음 운영자가 `max_tranche_index = 0` 프로필을 커밋해 자동승인을 철회한다. 그 뒤에 도는 fence 트랜잭션은 이전 프로필 id로 fence를 커밋하지 못하고 **자동 매수의 브로커 호출은 0**이어야 한다. 철회 대신 조항·sleeve 상태의 변경이 먼저 커밋된 경우도 같다.

업종 35%의 분자·분모, 구성 sleeve 조항이 서로 다를 때의 처리, 편입액 산입 예제는 §3.8과 §3.1에 있다.

### 4.7 강등과 차단 — 실패 종류별 한 표 (R5)

리뷰 R5와 §1 1c: 원래 문서는 "하나라도 거짓이면 사람 카드"라고 하면서 동결은 차단, 중복은 거부, 계획 읽기 실패는 사람 카드라고 따로 적었다. 일반 승인 클릭이 데이터 불변식 위반을 합법화하면 안 된다. 아래 표가 정본이고 §3.3·§4.1·§5.3·§6은 이 표를 따른다.

원칙:
- **강등**: 그 제안이 자동승인의 권한·금액 범위 밖일 뿐, 계획과 데이터는 온전하다. 사람 카드로 가고, 사람이 승인하면 그 제안이 전송된다. 카드는 **무엇을 예외로 승인하는지** 사유 코드와 수치(한도, 현재 값, 넘는 금액)를 보여 준다.
- **차단**: 결속이 깨졌거나, 중복·불명이거나, 동결이거나, 계획에 권한이 없다. 사람이 승인해도 전송하지 않는다(전송 직전 경계, §4.3 (나)). 복구 절차를 거친 뒤 **새 제안**이 필요하다.
- 이미 보낸 주문은 어느 쪽으로도 되돌아오지 않는다. 권한을 거두는 것(스위치 off, 프로필 철회, 계획 폐기)은 다음 전송을 막을 뿐이다.

| 실패 | 사유 코드 | 분류 | 판정 지점 | 사람이 같은 제안을 승인하면 | 복구 |
|---|---|---|---|---|---|
| 스위치 off, 시장·계좌 allowlist 밖, 모드가 `expanded` 아님 | `mandate_switch_off` 등 | 강등 | dispatch | 전송된다 | 없음(정상 상태). 자동을 원하면 활성화 절차 |
| 활성화 프로필 없음·만료·철회, 프로필이 현재 조항에 결속 안 됨 | `mandate_activation_missing` | 강등 | dispatch | 전송된다 | 새 프로필 승인(§3.6) |
| 회차 번호가 프로필 상한 초과 | `mandate_tranche_not_activated` | 강등 | dispatch | 전송된다. 카드에 회차 번호와 상한 표시 | 회차 확대는 새 프로필 버전 |
| 위임장 버전이 live 아님(shadow·paused·stopped), 손실 한도 도달, 예산 초과, 손익·사용액 미측정 | `sleeve_not_live` 등 `sleeve_*`(기존 코드 그대로) | 강등 | dispatch(sleeve 게이트) | 전송된다(ROB-1470 기존 원칙) | sleeve 버전 조치(운영자) |
| 현재 live 버전에 결속된 조항이 없음(조항 없이 붙은 버전), 조항 만료 | `mandate_terms_missing`·`mandate_expired` | 강등 | dispatch | 전송된다 | 새 위임장 버전 승인 |
| 계획 결속 rung인데 계획 게이트의 결속 증거가 없음(위 줄들 중 하나로 1단계가 실패한 모든 경우의 분류기 쪽 사유) | `mandate_not_eligible` | 1단계 사유의 분류를 따른다 | dispatch(분류기 진입부, I23) | 1단계가 강등 사유로 실패했으면 전송된다. 차단 사유(I9·I11·I15·I16·I21)로 실패했으면 그 줄대로 전송 안 됨 | 1단계 사유의 복구를 따른다 |
| 계획의 결속 조항이 현재 live 조항과 다름(재승인 대기) | `mandate_terms_stale` | 강등 | dispatch | 전송된다. 카드에 옛·새 조항 해시 표시 | 다음 묶음 승인에서 `terms_rebound` |
| 주문당 한도 초과(전역 또는 조항) | `per_order_cap_exceeded`(기존)·`mandate_per_order_cap` | 강등 | dispatch(분류기) | 전송된다. 카드에 한도와 금액 표시 | — |
| 전역 누계 초과 | `daily_cap_exceeded`(기존) | 강등 | dispatch(분류기) | 전송된다 | — |
| 바구니 누계 초과 | `mandate_basket_daily_cap` | 강등 | dispatch | 전송된다 | — |
| 업종 상한 초과(dispatch 시점) | `mandate_sector_cap` | 강등 | dispatch | 전송된다. 카드에 분자·분모·비율 표시 | 계획 정리 또는 조항 변경 |
| 회차 조건 미충족(순서 I7은 1단계, 가격 트리거 I5·최소 간격 I6은 2단계) | `mandate_tranche_not_due` | 강등 | dispatch | 전송된다. 카드에 어긋난 조건 표시 | 조건 충족 뒤 재제안 |
| 회차 잔여 초과(생성 뒤 가격 변동, I8 첫째 절) | `mandate_tranche_amount` | 강등 | dispatch | 전송된다. 카드에 넘는 금액 표시 | — |
| 매도 1호가 없음·stale·무효 | `mandate_ask_unavailable` | 강등 | dispatch(2단계) | 지정가가 현재가 이하면 전송된다. 현재가를 넘으면 D1의 조건(신선한 ask)이 없으므로 기존 preview 가드가 그대로 거부한다 | 호가 소스 복구 뒤 재제안 |
| 가격·수량 입력 무효 | `price_or_quantity_missing`(기존) | 강등 | dispatch(분류기) | 기존 동작 그대로 | — |
| 전송 fence 커밋을 확인하지 못함(DB 실패, `seq` 충돌 뒤 재판정 실패) | `mandate_fence_unavailable` | **차단**(그 시도만) | fence 트랜잭션 | 그 시도는 전송 안 됨(fence가 없다). 자동 경로면 사람 카드로 가고, 사람이 다시 승인하면 다시 시도한다 | 일시적이면 다시 승인. 지속되면 DB 복구 |
| 실제 송신 요청의 계좌가 계획의 계좌 참조와 다르거나 계산할 수 없음(I15) | `mandate_account_mismatch` | **차단** | fence 트랜잭션, POST 직전 | 전송 안 됨 | 계좌 설정을 바로잡는다. 계좌가 바뀐 것이면 새 위임장 버전과 새 계획 |
| 기존 공통 게이트(태그 스캔, veto 불가 계좌, thesis 없음, 세션 밖, preview 실패) | 기존 코드 | 기존 동작 그대로 | 기존 | 기존 동작 그대로 | — |
| 결속 불일치: 제안의 심볼·시장·계좌가 계획과 다름(I15) | `mandate_binding_mismatch` | **차단** | 제안 INSERT 트리거, dispatch, 전송 직전 | 전송 안 됨 | 제안 폐기. 원인 조사 뒤 새 제안 |
| 결속 파손: 제안이 가리키는 계획·회차 행이 없음, 계획이 가리키는 조항 행(`terms_id`)이 없음, 조항·프로필의 승인 기록이 없거나 해시 불일치(I21). "버전에 조항이 아직 없다"(위 강등 줄)와 다르다 — 이쪽은 있어야 할 참조가 깨진 것이다 | `mandate_binding_broken` | **차단** | dispatch, 전송 직전 | 전송 안 됨 | 운영자 조사. 자동 복구 없음 |
| 계획 권한 상태가 `approved`/`active` 아님, 효력 세션 미도래(I16) | `mandate_plan_not_active` | **차단** | 제안 생성, dispatch, fence 트랜잭션(이벤트 트리거가 강제) | 새 fence가 커밋되지 않아 전송 안 됨. 그 전에 커밋된 fence의 POST 한 번은 예외(§4.6) | 운영자 `resumed`·재승인 또는 효력 세션 도래 뒤 새 제안. 폐기된 계획은 새 계획 |
| 같은 회차에 다른 열린 제안(I9) | `mandate_tranche_duplicate` | **차단** | 제안 생성(트리거), dispatch, 전송 직전 | 전송 안 됨 | 먼저 있던 제안의 종결 |
| 같은 회차에 전송 결과 불명 제안이 있거나 닫히지 않은 전송 fence가 있다(같은 제안의 앞선 시도가 남긴 것 포함 — 자동이든 사람 승인이든. 이번 시도의 fence는 판정 뒤에 쓰므로 스스로를 막지 않는다, §4.6) | `mandate_tranche_uncertain` | **차단** | 제안 생성, dispatch, 전송 직전 | 전송 안 됨 | 원장 대사로만 풀린다. 재전송 없음 |
| 계획 전체 한도 초과(I8 둘째 절) | `mandate_plan_amount_exceeded` | **차단** | 제안 생성, 전송 직전 | 전송 안 됨 | 계획 금액을 늘리려면 새 계획(묶음 승인) |
| 낙폭 동결, 측정 불가, 측정 stale, 판정 함수 없음(I11) | `mandate_drawdown_frozen` | **차단** | 제안 생성, dispatch, fence 트랜잭션 | 새 fence가 커밋되지 않아 전송 안 됨. 그 전에 커밋된 fence의 POST 한 번은 예외(§4.6) | 운영자 해제 기록(hk 4900이 정한다) |
| 종목 예약 충돌(I1) | `mandate_reservation_conflict` | 계획 등록 거부 | 계획 등록 | 해당 없음 — 계획이 등록되지 않으므로 제안도 없다 | 앞선 계획의 `released` |
| 실계좌 영역에 미해제 계획이 있는 종목의 `loss_cut` 제안인데(계좌 무관) 그 계획에 `thesis_broken` 이벤트가 없거나, 인용한 회고 id·승인 이슈 id가 그 이벤트나 그 증거 갱신 이벤트의 것과 같지 않음(I14) | `mandate_thesis_not_broken` | 제안 생성 거부 / **차단** | `loss_cut` 제안 생성, 사람 승인 콜백의 nonce 소비 전, 전송 직전 | 생성 때 걸리면 제안이 없다. 이미 있는 제안이면 전송 안 됨 | 승인 이슈와 새 회고를 먼저 만들고 그 두 id로 `thesis_broken`을 기록(T8 판정 또는 운영자 CLI). 회고가 72시간을 넘었으면 같은 순서로 `thesis_evidence_refreshed`(폐기된 미해제 계획에도 가능) 뒤 다시 제안. 또는 계획을 폐기·정리한 뒤 일반 경로 |
| 그 시장·종목에 종결되지 않은 `loss_cut` 제안이 있는데 계획을 등록하려 함(I14) | `mandate_open_loss_cut` | 계획 등록 거부 | 계획 등록(종목 lock 아래) | 해당 없음 — 계획이 등록되지 않는다 | 그 `loss_cut` 제안이 종결된 뒤 등록 |
| 전송 직전 재확인에 필요한 행을 읽지 못함 | `mandate_unreadable` | **차단**(전송 직전) / 강등(dispatch) | 둘 다 | dispatch에서 읽기 실패면 자동승인 없이 사람 카드가 가고 카드에 "계획 판독 실패"를 표시한다. 전송 직전에 다시 읽어 또 실패하면 전송 안 됨 | DB 복구 뒤 다시 승인 |

- 표의 "전송된다"는 그 제안이 다른 조건(세션, preview, 기존 가드, 전송 직전 경계)을 통과한다는 전제다. 특히 '현재가 초과 매수' 가드: D1(§4.4)은 스위치가 켜진 시장·계좌에서만 열리므로, 스위치가 꺼진 곳에서 사람이 승인한 계획 결속 매수는 지정가가 현재가 이하일 때만 나간다. 현재가를 넘는 근접가 제안은 사람이 승인해도 기존 가드가 거부한다(기존 동작).
- 표의 "차단"은 **그 뒤로 전송 fence가 커밋되지 않는다**는 뜻이다. 차단 사유가 생기기 전에 이미 커밋된 fence는 POST 한 번까지 나갈 수 있다(§4.6 "fence가 권한이다"). 그 주문의 금액은 누계에 남고 예약·예산은 그 fence가 대사로 닫힐 때까지 풀리지 않는다.
- 사람 승인 콜백은 nonce를 소비하기 **전에** 발신자를 운영자 principal 목록과 대조하고(§3.5, 목록에 없으면 `mandate_approver_not_allowed`) 차단 판정을 한 번 하고(승인 창 경계 검사가 nonce 소비 전에 막는 것과 같은 자리 — `auto_trader@ac4f9ed5c` `docs/agent-contracts/approval-and-shadow-contracts.md:284` — "Toss US 확장세션 승인 창 (#1116)" 절의 "거부 멤버 하나가 일괄 승인 전체를 nonce 소비 전에 막는다"), 걸리면 nonce를 남긴 채 닫힌 사유를 카드에 돌려준다. 그 뒤 fence 트랜잭션(§4.3 (나), §4.6)이 같은 판정을 다시 하고, 권한을 정하는 것은 그쪽이다. 어느 쪽에서 걸리든 브로커 호출 수는 0이다.
- 사유 코드는 `auto_approve_audit` 어휘에 더한다. 닫힌 어휘이고 자유 텍스트를 싣지 않는다.

### 4.8 주문 전송 없이 하는 완화 경로 검증 — T5의 필수 산출물 (R4)

리뷰 R4: 키움 mock 제안은 수동 승인 전용이라 모의 4주·30건은 D1~D3(§4.4)을 한 번도 지나지 않는다. 그러므로 완화 경로는 **주문을 보내지 않는** 검증으로 따로 증명해야 한다. 아래 전부가 T5 PR의 필수 산출물이고 실전 활성화 체크리스트 C4다. 이 가운데 완화 없이도 성립하는 사례(V3·V4·V5·V7·V8·V17·V18·V19·V20·V21·V22의 거부 쪽)는 그 판정 지점을 소유한 티켓(T3a·T3b·T4a·T4c)이 먼저 자기 PR의 테스트로 넣고, T5는 같은 사례를 완화가 들어간 코드에서 다시 통과시킨다. 브로커 클라이언트는 호출을 세는 가짜로 바꾸고, 모든 "자동승인 없음" 사례는 브로커 호출 수 0을 단언한다.

| # | 대상(리뷰 R4의 항목) | 검증 종류 | 기대 결과 |
|---|---|---|---|
| V1 | 미승인 계좌 | 단위 + 통합(고정 fixture) | 계좌가 `MANDATE_AUTO_APPROVE_ACCOUNT_MODES`에 없으면 자동승인 없음, 사유 `mandate_switch_off` 계열. `kiwoom_mock`·`kis_mock` 등 live가 아닌 계좌 값은 allowlist에 넣어도 자동승인이 열리지 않는다 |
| V2 | 미승인 시장 | 단위 + 통합 | 시장이 allowlist에 없으면 자동승인 없음 |
| V3 | 미승인 회차 | 단위 + 통합 | 프로필 상한 1에서 2·3차 제안은 사람 카드(`mandate_tranche_not_activated`). 프로필 없음·만료·철회·다른 조항 결속 각각 |
| V4 | 위조된 결속 | 통합(DB) | `source_asof`나 strategy 라벨로 계획 결속을 흉내 낸 제안은 결속으로 인정되지 않는다(결속은 `mandate_plan_id` 열과 계획 이벤트에서만 온다). 다른 종목·계좌·시장의 계획 id를 단 제안은 INSERT 트리거가 거부 |
| V5 | 오래된 결속 | 통합 | 제안 생성 뒤 계획이 `suspended`·`retired`·`thesis_broken`이 된 경우, 조항 버전이 바뀐 경우, 프로필이 바뀐 경우, 효력 세션 전인 경우 각각의 분류(§4.7)와 브로커 호출 0(차단 사례) |
| V6 | stale 호가 | 단위(시계 주입) | ask 관측 시각이 상한을 넘으면 자동승인 없음(`mandate_ask_unavailable`). ask 없음·0·음수·비유한값 각각. `current_price`나 Toss 폴백 값이 ask 자리에 쓰이지 않음을 단언 |
| V7 | 낙폭 unknown | 단위 + 통합 | `buy_freeze_state`가 예외·시간 초과·측정 stale·함수 없음일 때 dispatch에서 차단되고 fence 트랜잭션에서 fence가 써지지 않는다. 사람 승인 경로도 같다 |
| V8 | 중복 콜백 | 통합(경쟁) | 같은 승인 콜백을 두 번·동시에 보내도 전송은 많아야 한 번. 묶음 승인 콜백·조항 승인 콜백의 nonce 재사용은 아무 행도 쓰지 않는다 |
| V9 | 취소 지연 | fault-injection | veto 취소가 늦거나 실패하거나 체결과 겹칠 때: fence 유지, 회차 재제안 차단, 누계 유지, 운영자 알림 |
| V10 | 대사 지연 | fault-injection | 원장에 체결이 아직 없을 때: 회차는 `tranche_proposed`에 머물고 sleeve 사용액이 미기록 체결을 포함(§2.3 F5), `released` 불가 |
| V11 | 알림 실패 | fault-injection | 자동승인 카드 발송 실패 시 기존 처리(attempt fence, 브로커 취소 보상, operator alert)가 계획 결속 제안에도 같고, fence·회차 상태가 §4.6대로 남는다 |
| V12 | 스위치 off의 기존 경로 회귀 | 골든·정적 | `MANDATE_AUTO_APPROVE_ENABLED=false`(기본)이고 **그날 그 계좌 모드·시장에 해제되지 않은 자동 fence가 없을 때** 계획 결속이 없는 모든 제안의 분류 결과·감사 기록이 변경 전과 바이트 동일. 당일 자동 fence가 남아 있으면 스위치가 꺼져 있어도 그 금액이 전역 누계에 들어간다(§4.6의 스위치 off 사례가 이것을 단언한다). 스위치가 켜져 있어도 계획 결속이 없는 제안은 동일(해제되지 않은 자동 fence가 전역 누계에 더해지는 경우만 예외이고, 그것은 더 엄격한 방향이다) |
| V13 | D1 경계 | 단위 + 통합 | `allow_mandate_bound_buy`는 §4.4 "D1의 범위"의 네 조건이 전부 참일 때만 선다. 각 조건을 하나씩 거짓으로 만든 사례: 스위치 off(자동·사람 승인 모두 닫힘), 시장·계좌 allowlist 밖, 결속 없음, 차단 판정 실패, ask 없음·stale, 지정가가 상한 초과. 스위치 on에서 사람이 승인한 계획 결속 근접가 매수에는 선다. 상한은 ask × (1+N bp)의 호가 단위 내림. 파킹 예외 플래그와 서로 넓히지 않는다. 결속 없는 현재가 초과 매수는 기존대로 거부 |
| V14 | D2 경계 | 단위 | `marketable_not_resting` 해제는 결속 + 근접가 조건에서만. `off` 모드, 매도, `replace`·`cancel`, 파킹 심볼, `exit_intent` 있는 제안에는 해제가 닿지 않는다 |
| V15 | 파킹·cash-funding·손실매도 비간섭 | 단위 | 계획 결속 rung에 파킹 daily-cap 면제가 적용되지 않고 일 누계에 들어간다. 파킹 매도·cash-funding·`loss_cut` 경로의 결과가 변경 전과 같다 |
| V16 | 누계 경쟁 | 통합(DB 경쟁, CI) | §4.6 acceptance 전부 |
| V17 | 전송 직전 경계 | 통합 | §4.3 (나)의 여섯 줄 각각에서(첫 줄의 계좌 참조 불일치 포함) 자동·사람 승인 모두 fence가 써지지 않고 브로커 호출 0 |
| V18 | 다른 길 없음(I23, §4.1 조건 0) | 단위 + 통합(고정 fixture) | 지정가가 현재가 **아래**인(기존 `expanded` 분류기가 통과시키는 모양의) 계획 결속 매수가 (a) 스위치 off, (b) 프로필 없음, (c) 프로필 상한 1에서 2차, (d) 조항 만료, (e) 계획 `suspended`, (f) 2단계 실패(바구니 누계 초과, 매도 1호가 없음)일 때 전부 자동승인되지 않는다. 같은 fixture에서 계획 결속 열만 뺀 제안의 결과는 변경 전과 같다 |
| V19 | 동결과 fence의 순서(I11) | 통합 + fault-injection(시계 주입) | 동결 게시가 fence보다 먼저 커밋되면 fence가 써지지 않고 호출 0. fence 트랜잭션이 읽은 판정의 유효 기한이 지났으면 fence가 써지지 않는다. fence가 먼저 커밋되면 그 뒤의 동결은 그 fence의 POST 한 번을 막지 않는다(POST 직전 재확인이 보면 호출 0 + fence 해제 — best-effort) |
| V20 | 계좌 결속(I15) | 단위 + 통합 | 실제 송신 요청의 계좌가 계획의 계좌 참조와 다르면 호출 0. 호출자가 준 `broker_account_id`는 무시. 계좌 참조가 다른 조항으로 재승인 불가. 전역 누계의 계좌 키 사례 (i)~(iv)(§4.6) |
| V21 | 영역이 다른 같은 이름의 바구니 | 단위(고정 예제) | §3.8의 혼합 영역 예제: 모의 바구니의 예산·계획이 실계좌 바구니의 업종 비중·예산·바구니 누계·배치에 들어가지 않는다 |
| V22 | 연결 상실과 권한의 단위(I16·I24) | fault-injection(세션 종료) | fence 커밋 → 호출자의 DB 세션 종료 → `retired` 커밋 → POST. 주문은 한 번 나가고, 두 번째 POST는 없고, 예약·예산·업종 몫은 유지되며 그 fence가 대사로 닫히기 전에는 `released`가 거부된다. 순서를 바꿔 `retired`가 fence보다 먼저 커밋되면 호출 0. 동결 게시로도 같은 두 순서 |

- 고정 fixture는 계획·조항·프로필·호가·preview를 담은 정적 입력이다. 같은 입력이면 같은 결과다.
- DB 경쟁 테스트는 CI의 테스트 DB에서 돈다. 로컬 DB를 쓰지 않는다.
- 이 표의 통과는 "분류와 경계가 설계대로 판정한다"는 증거다. 실제 브로커가 근접가 주문을 어떻게 체결하는지는 증명하지 않는다. **실전 확인은 별도 운영자 승인(§4.5 5단계)으로만 한다.**

## 5. 사람 승인이 남는 흐름

### 5.1 1a — 하루 1회 장 시작 전 묶음 승인

- 대상: (a) `proposed` 계획(새 종목 등록), (b) 위임장 버전이 바뀌어 현재 결속 조항이 live 조항과 다른 `approved`/`active` 계획(재승인 대상), (c) 그날 armed가 될 회차 요약.
- 카드: ROB-870 batch 경로는 **재사용하지 않는다**. 그 멤버는 주문 제안에 FK로 묶이고 개별 승인 카드 게시를 요구하며(`app/models/order_proposals.py:531`, `app/services/order_proposals/service.py:487`), 승인 콜백이 멤버마다 `_handle_approve`를 불러 브로커 주문을 낼 수 있다(`app/services/order_proposals/telegram_callback.py:1223`, `:1388`). 대신 새 테이블 `review.mandate_plan_batches`(+멤버 행, 불변 스냅샷: 계획 id·조항 id·계획 해시)와 nonce 결속 콜백 `mpb`를 둔다. 이 콜백은 멤버 종류에 따라 계획 이벤트 `approved`/`rejected`(새 계획) 또는 `terms_rebound`/`retired`(재승인 대상)만 append하며, 각 이벤트에 스냅샷의 `terms_id`·`terms_sha256`·`effective_session`(다음 정규장 세션)·`batch_member_id`를 싣는다. 콜백 시점 스냅샷 조항이 더 이상 live가 아니면 그 멤버는 아무 이벤트도 쓰지 않는다. 콜백은 주문·제안 모듈을 import하지 않는다(정적 테스트). 멤버 1개도 허용한다. 콜백 인가·nonce 단일 소비·chat allowlist는 기존 콜백과 같은 규칙을 쓰되 코드 경로는 분리한다. 이벤트의 `actor`는 `operator:telegram:<from.id>`이고, 그 `from.id`는 운영자 principal 대조(§3.5, `MANDATE_APPROVER_TELEGRAM_USER_IDS`)를 통과한 것이어야 한다. 통과하지 못한 발신자의 버튼은 이벤트를 쓰지 않고 nonce도 소비하지 않는다.
- 발송: 장 시작 전 운영자/세션이 CLI로 발송(scheduleless, Q8 = A 결정됨). 발송하지 않은 날은 승인도 없다.
- 아무것도 바뀌지 않았으면 "오늘 적용 기준" 요약만(자문 §10).
- 재승인 대상 멤버의 기각은 그 계획을 `retired`로 만든다. 추가 권한만 멈추고 미결 주문·보유는 건드리지 않는다. 카드 응답에 그 계획의 미결 주문 수를 보여 준다(§3.7).

**배치 단일성(리뷰 §1 1a, Q4의 조건 "배치 1일 1회").**

- 배치는 **대상 거래일** T를 갖는다. T = 배치 생성 시각 이후 처음 개장하는 그 시장의 정규장 세션(시장 달력). 정규장 중에 만든 배치의 T는 다음 거래일이다.
- 시각 둘을 구분한다. **승인 마감** = T의 정규장 개장 시각(이 뒤의 승인은 T에 효력이 없다). **배치 만료** = T의 정규장 종료 시각(이 뒤에는 응답을 받지 않는다).
- 바구니 × T당 배치는 하나다: `UNIQUE(realm, basket_key, target_session)`(I22). 바구니는 (영역, `basket_key`)이므로 모의 바구니와 실계좌 바구니의 배치는 서로 별개다. 같은 T의 두 번째 배치는 INSERT가 거부된다.
- **앞 배치가 닫힌 뒤에만 새 배치를 만든다.** 닫힘 = 모든 멤버가 응답했거나 배치가 만료됐다. 그 바구니에 열린 배치가 있으면 새 배치 INSERT가 거부된다(바구니 lock 아래 트리거). 그래서 한 계획이 동시에 두 배치의 열린 멤버가 되는 일은 없다.
- **재발송**은 새 배치가 아니다. 같은 배치의 아직 응답 없는 멤버 카드를 다시 보내는 것이고, 스냅샷은 그대로다. 재발송은 그 멤버의 nonce를 새것으로 바꾸고 앞 카드의 nonce는 더 이상 받지 않는다. 멤버 하나에 결정은 많아야 하나다.
- **추가 승인**은 다음 거래일이다. 배치를 만든 뒤 등록된 계획은 그 배치에 들어가지 않고 다음 배치에 들어간다. 같은 T의 배치에 계획을 나중에 끼워 넣는 길은 없다.
- **부분 승인**: 멤버마다 승인·기각한다. 응답한 멤버만 이벤트를 쓴다.
- **만료**: 배치 만료 시각에 응답 없는 멤버는 만료된다. 만료는 이벤트를 쓰지 않는다. 새 계획은 `proposed`로, 재승인 대상은 재승인 대기(자동승인 없음, `mandate_terms_stale`)로 남아 다음 배치에 다시 들어간다.

**효력 세션(Q4 = B, 결정됨): 승인 마감 뒤의 승인은 다음 거래일부터.**

| 승인 콜백 시각 | `effective_session` |
|---|---|
| 승인 마감 전(T 개장 전) | T |
| 승인 마감 뒤 ~ 배치 만료 전(T 장중) | T 다음 거래일 |
| 배치 만료 뒤 | 없음(만료, 이벤트 없음) |

한 거래일에 효력이 생기는 승인이 두 배치에서 올 수 있다: 앞 배치의 늦은 응답(장중 승인 → 다음 거래일 효력)과 그 다음 거래일을 T로 하는 배치의 응답이다. 이것은 허용한다. 지키는 것은 "한 거래일을 대상으로 하는 배치는 하나"와 "한 계획의 열린 멤버는 하나"다.

세션 예제(KR, XKRX 정규장 09:00~15:30 KST. US는 XNYS 달력으로 같은 규칙):

| 배치 생성 | T | 승인 시각 | 결과 |
|---|---|---|---|
| 월 08:20 | 월 | 월 08:40(개장 전) | 효력 월. 월요일 장중에 1차 제안 가능 |
| 월 08:20 | 월 | 월 09:10(개장 후) | 효력 화. 월요일에는 그 계획으로 제안을 만들 수 없다 |
| 월 08:20 | 월 | 월 15:40(만료 뒤) | 만료. 화 배치에 다시 들어간다 |
| 월 10:00(장중, 월 배치의 멤버가 전부 응답한 뒤) | 화 | 월 14:00 | 효력 화 |
| 월 10:00(장중, 월 배치에 응답 없는 멤버가 남음) | — | — | 배치 생성 거부. 월 배치가 닫힌 뒤에 만든다 |
| 금 16:00(장 종료 뒤) | 다음 월 | 토 10:00 | 효력 월 |
| 화 16:00, 수요일 휴장 | 목 | 수 12:00(휴장일) | 효력 목 |
| 화 16:00, 수요일 휴장 | 목 | 목 09:05 | 효력 금 |

효력 세션 전에는 그 계획으로 제안 생성도 전송도 되지 않는다(I16, 차단).

### 5.2 1b — 논지 훼손 손실 매도

- 논지 훼손 판정: 계획의 `invalidation_conditions` + 결정론 검사(공시·실적 이벤트). 판정은 `thesis_broken` 계획 이벤트를 쓴다. 기록할 수 있는 권한 상태는 `approved`·`active`·`suspended`·`exhausted`다(§3.2). 운영자가 CLI로 직접 기록할 수도 있다(사유 필수).
- 매도 경로: 새 exit intent를 만들지 않는다. 제안 생성은 `loss_cut` 외 exit intent의 손실 매도를 거부하고(`app/services/order_proposals/service.py:1139`) KIS·Toss preview/submit도 따로 거부하며(`app/mcp_server/tooling/order_execution.py:2056`, `orders_toss_variants.py:1213`), 평단 가드는 검증된 `loss_cut` 문맥만 완화한다(`app/mcp_server/tooling/order_validation.py:180`). 그래서 1b는 **기존 `exit_intent='loss_cut'` 사람 승인 의식**(손절 2클릭 카드, `INVEST_LOSS_CUT_APPROVAL_ENABLED`·Telegram 손절 콜백)을 그대로 쓰고, 근거로 계획 id와 `thesis_broken` 이벤트 id를 rationale에 싣는다. 이 경로는 게이트를 바꾸지 않는다.
- **이벤트 없이는 손실 매도가 만들어지지도 나가지도 않는다(I14).** 기존 분류기의 `loss_cut_intent` 거부는 자동승인만 막고(`auto_trader@ac4f9ed5c` `app/services/order_proposals/auto_approve.py:752-768`), 기존 `loss_cut` 제안 생성은 같은 종목의 유효한 회고와 승인 이슈만 있으면 통과하며(`auto_trader@ac4f9ed5c` `app/services/order_proposals/service.py:1153-1199`), 사람 승인 콜백은 계획을 보지 않고 `revalidate_and_submit`로 간다(`auto_trader@ac4f9ed5c` `app/services/order_proposals/telegram_callback.py:879-893`). 그대로면 `thesis_broken` 없는 `loss_cut` 제안(예: `stop_loss` 회고로 만든 것)이 사람 승인으로 나갈 수 있다. 그래서 다음 가드를 둔다. 가드는 논지 훼손 판정의 자동화(T8)와 별개이고, 실계좌에 계획 결속 제안이 열리기 전에 있어야 하므로 **T4c**가 넣는다.
  - **무엇을 보나**: 그 `loss_cut` 제안의 시장·종목에 실계좌 영역(`live`)의 미해제 계획 예약이 있는가. 예약은 계좌와 무관한 키(§3.2)이므로 **계좌를 가리지 않는다**: Toss의 계획이 KIS 보유분까지 편입액에 센 종목이면 KIS 계좌의 `loss_cut` 제안도 그 Toss 계획에 걸린다. `loss_cut`은 실계좌 조합에서만 만들 수 있으므로(`service.py:1161-1168`) 모의 영역의 계획은 대상이 아니다.
  - **통과 조건**: 미해제 계획이 없거나, 있으면 (1) 그 계획의 이력에 `thesis_broken` 이벤트가 있고 (2) 제안이 구조화된 필드로 인용한 회고 id와 승인 이슈 id가 그 `thesis_broken` 이벤트 또는 그것을 가리키는 `thesis_evidence_refreshed` 이벤트 중 하나의 `evidence`에 있는 두 id와 같다. 아니면 `mandate_thesis_not_broken`. 계획·이벤트를 읽지 못하면 거부한다(fail-closed). 계획의 권한 상태가 `thesis_broken`이든 그 뒤 `retired`든 같다 — 미해제인 동안은 이 조건이 걸린다.
  - **증거의 순서**: 이벤트는 불변이라 나중에 만든 회고의 id를 덧붙일 수 없다. 그래서 순서는 고정이다: 승인 이슈 확보 → 새 `thesis_change` 회고 기록(`save_trade_retrospective`, id 확보) → 그 두 id를 `evidence`에 실어 `thesis_broken`(이미 있으면 `thesis_evidence_refreshed`) append → 같은 두 id를 인용한 `loss_cut` 제안. T8의 자동 판정도, T8 없이 운영자가 CLI로 하는 임시 절차(§4.5 C8)도 이 순서다.
  - **판정 지점 셋**: `loss_cut` 제안 생성(거부되면 제안 행이 없다), 사람 승인 콜백의 nonce 소비 전(거부되면 nonce가 남는다), 브로커 호출 직전(거부되면 전송 0). 생성 시점에만 보면, 계획이 없을 때 만든 `loss_cut` 카드를 계획이 생긴 뒤에 승인해 이벤트 없이 내보낼 수 있다. 뒤의 두 판정이 그것을 다시 본다. 뒤의 두 판정은 새 짧은 트랜잭션으로 읽는 best-effort 확인이고(읽지 못하면 보내지 않는다), 순서를 **보장**하는 것은 다음 항목의 생성·등록 규칙이다.
  - **계획 등록과의 직렬화**: `loss_cut` 제안 생성은 그 시장·종목의 종목 lock(L1, `live` 영역)을 쥔 짧은 트랜잭션에서 계획을 확인하고 제안 행을 쓴다. 계획 등록은 같은 lock 아래에서 그 시장·종목에 종결되지 않은 `loss_cut` 제안(어느 실계좌든)이 있으면 등록을 거부한다(`mandate_open_loss_cut`). 두 트랜잭션이 각각 상대가 **커밋한 행**을 보고 판정하므로, "계획 없이 만든 `loss_cut` 제안이 열려 있는 채로 계획이 생기는" 상태는 생기지 않는다 — 그 제안이 종결될 때까지 계획이 등록되지 않기 때문이다. 그래서 전송 시점에 lock을 쥐고 있을 필요가 없다: 승인과 전송 사이에 계획이 끼어들 수 없다. 이미 그런 상태가 있더라도(이 가드가 배포되기 전에 생긴 것) 승인 전과 전송 직전의 재확인이 본다.
  - **72시간이 지난 뒤**: 기존 손절 검사는 회고의 `created_at`이 72시간을 넘으면 제안 생성과 preview를 거부한다(`auto_trader@ac4f9ed5c` `app/services/order_proposals/service.py:1187-1197`, `app/mcp_server/tooling/order_validation.py:590-598`). 이 검사는 건너뛰지 않는다. 최초 `thesis_broken`의 회고가 만료된 뒤에도 손실 매도를 다시 시작할 수 있도록 `thesis_evidence_refreshed` 이벤트를 둔다: 이력에 `thesis_broken`이 있는 미해제 계획에, 그 `thesis_broken` 이벤트를 가리키며(`thesis_seq`), 새 회고 id(`trigger_type='thesis_change'`, 같은 종목)와 승인 이슈 id를 `evidence`에 싣는다. T8의 판정(조건이 여전히 충족됨을 다시 확인) 또는 운영자 CLI(사유 필수)가 쓴다. 계획 이벤트는 불변이므로 최초 이벤트를 고치지 않고 증거를 덧붙이는 것이다. **폐기된 계획에도 쓸 수 있다**: 논지 훼손 뒤 운영자가 계획을 `retired`로 했는데 미결·불명 매수의 대사가 72시간 넘게 걸리면, 예약이 남아 가드는 계속 걸리고 최초 회고는 만료된다. 그때도 갱신 이벤트로 새 회고를 결속해 손실 매도를 검토할 수 있다. 갱신은 매수 권한을 되살리지 않고 예약도 풀지 않는다.
  - **영향 범위**: 미해제 계획이 없는 종목의 `loss_cut` 제안은 이 가드에 걸리지 않고 결과가 변경 전과 같다. 손실이 아닌 매도는 대상이 아니다. 운영자가 논지와 무관하게 그 종목을 팔아야 하면 `thesis_broken`을 직접 기록하거나(운영자 CLI, 사유 필수) 계획을 폐기·정리(§3.7)한 뒤 일반 경로로 판다.
  - **테스트 계약(T4c)**: (a) 계획이 있는 종목에서 이벤트 없는 `loss_cut` 생성 거부. (b) 계획 없이 만든 `loss_cut` 카드가 열려 있으면 그 종목의 계획 등록 거부. (c) 가드 배포 전에 생긴 것처럼 계획과 이벤트 없는 `loss_cut` 카드가 함께 있는 fixture에서 승인은 nonce를 남긴 채 거부되고 브로커 호출 0. (d) Toss 계획(편입액에 KIS 보유 포함)이 있는 종목의 KIS `loss_cut` 제안도 (a)와 같다. (e) `loss_cut` 생성과 계획 등록을 동시에 돌리면 하나만 성공한다(DB 경쟁, CI). (f) 금요일 `thesis_broken`, 72시간 뒤: 최초 회고로는 기존 72시간 검사가 거부하고, 새 회고만 만들고 갱신 이벤트가 없으면 `mandate_thesis_not_broken`, 갱신 이벤트를 쓴 뒤에는 생성된다. (g) 계획 없는 종목의 `loss_cut`은 변경 전과 같다. (h) 미결 매수가 남은 채 `retired`가 된 계획, 72시간 경과: 최초 회고는 거부되고, 갱신 이벤트를 쓰면 손실 매도 제안이 생성돼 기존 2클릭 의식으로 가며, 계획은 `retired`로 남고 예약은 유지되고 그 계획의 매수 전송은 여전히 0이다. (i) 임시 절차 전체: 승인 이슈 → 회고 → 두 id를 실은 `thesis_broken` CLI 기록 → 같은 id의 제안 생성 성공. 순서를 바꿔 이벤트를 먼저 쓰고 회고를 나중에 만들면 `mandate_thesis_not_broken`. (j) 열린 `loss_cut` 제안이 있는 동안에는 승인 전·승인 중·전송 중 어느 시점에 계획 등록을 시도해도 거부된다. 그 제안이 종결된 뒤에는 등록된다.
- `loss_cut` 제안이 요구하는 증거를 T8이 만든다(`app/services/order_proposals/service.py:1153-1199`): `exit_reason='thesis_change'`, 72시간 이내·같은 심볼·`trigger_type='thesis_change'`인 회고 행(근거 = 충족된 `invalidation_conditions`와 결정론 검사 결과, 계획 `decision_id`가 있으면 연결), 비어 있지 않은 `approval_issue_id`(= hk 이슈/운영자 승인 기록 id; 없으면 아무것도 쓰지 않고 운영자에게 알린다). 순서는 위 "증거의 순서"대로다: 승인 이슈와 회고를 먼저 확보하고, 그 두 id를 실은 `thesis_broken` 이벤트를 쓰고, 같은 두 id로 제안한다. 기존 검사는 하나도 건너뛰지 않는다.
- 사람 카드(조항 `loss_sell_mode='human_card'`): `loss_cut` 제안은 분류기가 `loss_cut_intent`로 거부한다(`auto_approve.py:752-759`). ROB-1460 결정 행이 없으므로(바구니 ①은 기계식 손절 대상이 아니다, Q7) 해제되지 않는다. **기존 거부가 1b를 강제한다.**
- **4주의 시작 시점(리뷰 §1 1b).** "처음 4주"는 그 바구니의 **최초 실전 활성일**부터 센다: 그 바구니(`basket_key`)의 실계좌 sleeve에서 처음 생긴 계획 결속 제안의 `tranche_proposed` 이벤트 시각을 거래일로 옮긴 날이다. 이 이벤트는 실전 사람 카드 단계가 실제로 열려 있어야만 생긴다 — 위임장 버전이 승인돼 있고, 계획이 묶음 승인을 받았고, `MANDATE_PLAN_PROPOSAL_ACCOUNT_MODES`에 그 계좌가 들어 있어야 제안이 만들어진다(§4.2). 위임장 승인 시각을 쓰지 않는 이유: 승인을 먼저 해 두고 env를 나중에 바꾸면 실계좌 제안이 아직 불가능한 동안 28일이 줄어든다. 거래일로 옮기는 규칙: 그 시각이 그 시장 정규장 세션 안이면 그 세션의 날짜, 세션 밖(장 시작 전·종료 뒤·휴장일)이면 그 뒤 처음 열리는 정규장 세션의 날짜. 이 값은 append-only 이벤트에서 나오므로 나중에 바뀌지 않고 감사할 수 있다. 재검토 시점은 그 거래일부터 달력 28일 뒤다. 모의 계좌의 이벤트는 세지 않는다.
- **기간이 지나도 권한은 바뀌지 않는다(Q5 = B, 결정됨).** 4주가 지났다는 사실은 어떤 코드 경로의 입력도 아니다. 조항 스키마 `mandate-terms/v1`은 `loss_sell_mode`에 `human_card`만 받는다(CHECK). 4주 뒤의 재검토 결과로 자동 경로를 두려면 별도 설계 + 별도 deviation record + 새 조항 스키마 버전이 필요하고, 이 설계는 그 경로를 만들지 않는다. 테스트 계약: 첫 실계좌 `tranche_proposed`가 장중·장 종료 뒤·휴장일에 생긴 세 경우의 시작 거래일이 위 규칙대로 나오고, 위임장 승인만 있고 제안이 없으면 시작일이 없다. 시계를 최초 실전 활성일 + 28일, + 1년으로 옮겨도 `loss_cut` 제안은 사람 카드이고, `human_card`가 아닌 `loss_sell_mode` 값은 조항 INSERT가 거부된다.
- ROB-1460과의 관계: ROB-1460은 "기록된 손절가 이탈"이라는 가격 규칙이고, 1b는 "논지 조건 충족"이다. 같은 lot에 둘 다 해당할 수 있다. 바구니 ①의 결정 §2.1은 "논지 훼손이 유일한 손실 매도"이므로 바구니 ① 계획 종목은 기계식 손절 대상에서 뺀다(Q7, 원칙 결정됨). ROB-1460 코드는 이 설계에서 바꾸지 않는다.

**Q7 영향 표 — 원칙은 유지, 계좌 전체 영향은 따로 보인다.** 운영자 답(2026-11-18): 바구니 ①의 기계식 손절 제외 원칙은 유지하되, 계좌 전체 영향을 설계자가 분리해 보인 뒤 확정한다. 아래가 그 분리다.

[사실] ROB-1460이 어떤 lot을 live로 내보내려면 다음이 전부 참이어야 한다(§2.3 F9·F9a): (1) live 스위치, (2) 그 시장이 시장 목록에, (3) 그 계좌가 계좌 목록에(두 목록은 독립이라 허용 범위는 곱집합이다), (4) 그 시장의 `thresholds_confirmed`, (5) 그 lot의 계획이 backfilled가 아니거나 `live_include_backfilled`가 참. 종목 단위나 sleeve 단위로 빼는 설정은 없다. 지금 레포 기본값과 정책은 (1)~(5)의 설정 값이 전부 off/false다. 운영 env의 실제 값은 이 문서에서 확인하지 않았다([추정] 기본값 그대로).

[사실] (5)의 lot 구분은 바구니 ①을 빼 주지 않는다. lot exit plan 기록(`LOT_EXIT_PLAN_WRITE_ENABLED`)이 켜져 있으면, 이전 계획도 보유도 없는 종목의 첫 매수는 손절가를 명시하지 않아도 정책 기본 손절가로 `backfilled=False`인 계획을 받는다(§2.3 F9b). 즉 바구니 ①의 **신규 종목** lot은 backfilled가 아니다. backfilled로 남는 것은 기록이 켜지기 전부터 들고 있던 lot(편입 종목)뿐이다. 그래서 "계획 결속 매수는 손절가를 싣지 않는다"는 규칙과 `live_include_backfilled=false`만으로는 원칙을 지킬 수 없다(이 문서의 앞 판이 A3로 적었던 방식이고, 틀렸다).

바구니 ①의 계획은 Toss KR과 KIS KR에 있다. 그래서 "바구니 ① 계좌·시장을 allowlist에서 뺀다"를 (2)·(3)만으로 하면 **KR 시장 전체를 빼는 것**이 된다(계좌 둘 다를 빼면 그 계좌의 US도 같이 빠진다). 원칙을 실제로 보장하는 수단은 둘이다.

- **A1 — 시장 목록으로 제외(설정)**: 바구니 ①이 KR에 있는 동안 `MECHANICAL_LOSS_CUT_LIVE_MARKETS`에 kr을 넣지 않는다.
- **A2 — 계획 종목 제외(ROB-1460 코드 변경)**: (a) ROB-1460의 live 평가가 미해제 계획 예약이 있는 계좌·시장·종목의 lot을 건너뛰고, (b) 계획 결속 매수에는 lot exit plan을 쓰지 않는다. (a)만으로 원칙은 지켜지고, (b)는 계획이 풀린 뒤에 기본 손절가 계획이 남아 그 lot이 뒤늦게 대상이 되는 것을 막는다.

| 대상 lot | A1 | A2 | 기계식 손절이 없을 때의 대체 보호 |
|---|---|---|---|
| 바구니 ① 계획 종목(KIS KR·Toss KR), 신규·편입 모두 | 대상 아님 — 원칙 충족 | 대상 아님 — 원칙 충족 | 논지 훼손 매도(1b, 사람 카드), 낙폭 −25% 매수 동결(매도 아님), sleeve 손실 한도(도달 시 `stopped`) |
| 같은 두 계좌의 **바구니 밖 KR 보유**(3792 분류 "보유", 계획을 다른 계좌에 둔 같은 종목 포함) | **기계식 손절을 켤 수 없게 된다.** 지금도 off라 당장의 변화는 없지만, 나중에 이 보유에 켜려면 KR 전체를 열어야 하고 그러면 A1이 깨진다 | 켤 수 있다. 단 계획 계좌가 아닌 쪽에 든 편입 종목(금액은 편입액에 들어가지만 계획이 매매하지 않는 주식)을 대상으로 볼지는 A2의 설계가 정해야 한다 | 기존 매도 규칙(trim 등), 기존 손절 2클릭 사람 카드. 가격 규칙에 의한 자동 손실 매도는 없다 |
| 같은 두 계좌의 **다른 전략 sleeve**의 KR lot(나중에 생길 검증 통과 전략 등) | 그 전략이 가격 손절을 규칙으로 가져도 ROB-1460으로는 돌릴 수 없다 | 켤 수 있다 | 그 sleeve의 손실 한도, 그 전략 자체의 매도 규칙 |
| 같은 두 계좌의 US lot | 영향 없음(시장 목록에 US만 둘 수 있다) | 영향 없음 | — |
| Upbit crypto lot | 영향 없음 | 영향 없음 | — |
| 파킹 종목 | 영향 없음(ROB-1460이 원래 자르지 않는다) | 영향 없음 | — |

| | A1 | A2 |
|---|---|---|
| 필요한 변경 | 코드 변경 없음. 운영 규칙 하나를 체크리스트 C10과 ROB-1460 런북에 적는다 | ROB-1460 평가 경로와 매수 경로의 계획 기록에 대한 코드 변경. 하드룰 19의 표면이라 별도 티켓 + 별도 운영자 결정이 필요하다. 이 설계의 티켓이 아니다 |
| 원칙이 깨지는 조건 | kr이 시장 목록에 들어가는 것. 그때 (1)·(3)·(4)가 참이면 바구니 ①의 신규 종목 lot이 대상이 된다(계획 기록이 켜져 있었다면). 편입 lot은 (5)까지 참이어야 대상이 된다 | 코드가 막는다. 예약 읽기 실패를 "제외"로 볼지 "대상"으로 볼지 정해야 한다(원칙을 지키려면 제외) |
| 검증 | 체크리스트 C10: 배포 env에서 시장 목록에 kr이 없음을 key 이름으로 확인한 기록. 설정이 바뀌면 다시 확인해야 한다 — 코드가 지켜 주지 않는다 | 테스트: live 스위치·시장·계좌·`thresholds_confirmed`·계획 기록을 전부 켠 fixture에서 바구니 ①의 신규 lot과 편입 lot 모두 live 결정 행과 제안이 0건. 계획 결속이 아닌 lot의 결과는 변경 전과 같다 |
| 바구니 밖 KR 보유·다른 KR 전략에 기계식 손절을 쓸 계획이 있을 때 | 맞지 않는다 | 맞는다 |

[의견] 지금은 ROB-1460이 off이고 KR에 그것을 켜자는 결정이 없으므로 A1로 시작해도 잃는 것이 없다. A1은 설정 한 줄에 기대므로, KR에 기계식 손절을 켜려는 결정이 나오는 순간 A2가 그 결정의 선행 조건이 된다. **어느 방식으로 확정할지는 운영자 몫이다**(§7 Q7의 남은 확정). 확정 전에는 체크리스트 C10을 채울 수 없으므로 실전 단계도 올리지 않는다.

### 5.3 1c — 한도 초과·위임장 변경·모의→실전 승격

- 한도 초과: 금액·권한 범위의 예외(주문당 한도, 두 누계, 업종 상한, 회차 잔여, 예산, 활성화 프로필의 회차 상한)는 사람 카드다. 거절이 아니라 강등(sleeve 게이트와 같은 원칙, `gate.py:1-6`)이고, 카드는 무엇을 예외로 승인하는지 수치와 사유를 보여 준다. **결속 파손, 중복·불명, 동결, 권한 없는 계획은 한도 초과가 아니다.** 그것들은 사람이 승인해도 전송하지 않는다. 어느 실패가 어느 쪽인지는 §4.7의 한 표가 정한다.
- 위임장 변경: 새 sleeve 버전 + 새 조항 행 + 새 승인 기록(§3.5). 승인 요청은 운영자 CLI가 만들고(시스템 actor 거부) 승인은 Telegram 카드로만 기록된다. 변경 뒤 기존 `approved`·`active` 계획은 다음 묶음 승인에서 재승인(`terms_rebound`)을 받을 때까지 자동승인이 없다(Q3 = B, 결정됨). 그 사이 이미 브로커에 나간 주문은 그대로 살아 있고, 재승인을 기각하면 계획은 `retired`가 되지만 미결 주문·보유·예약은 §3.7대로 남는다. 변경은 활성화 프로필도 닫는다(§3.6).
- 모의 → 실전 승격: 스위치·allowlist 설정 = env 변경 = 기존대로 배포 때 내용 기준 승인(결정 §3). 그리고 활성화 프로필 승인(§3.6)과 체크리스트(§4.5). 둘 다 사람 승인이다.

### 5.4 2c — 낙폭 −25% 동결 (측정은 hk 4900)

auto_trader-design 결정(2026-11-16, `consult/2026-11-16/at-mandate-design-r3` 답): 낙폭 측정 — 바구니 자본, 전역 래치, 비용 출처 — 은 이 문서에서 확정하지 않고 별도 설계 **hk 4900**이 정한다. 이 문서는 요구 조건과 인터페이스만 둔다.

요구 조건(결정 2c, 4900이 지켜야 할 것):
- 바구니 낙폭 −25%, 전체 자산 낙폭 −25%에서 신규·추가 매수를 동결한다. 강제 매도 없음. 보유·매도·취소·대사·H6 보호는 그대로.
- 동결은 사람 카드로의 강등이 아니라 매수 **차단**이다(일반 Telegram 승인도 분류기 없이 `revalidate_and_submit`로 가므로, `app/services/order_proposals/telegram_callback.py:879`, `app/services/order_proposals/revalidation.py:1361`).
- 측정 불가는 0이 아니라 동결이다. 해제는 운영자 기록으로만 한다.
- 범위(어느 매수 경로·계좌·통화, 비용 출처, 해제 기록)는 4900이 정한다(Q2, 결정됨: hk 4900에서 별도 결정. 그 구현이 끝나기 전에는 자동 활성화 금지).
- **측정이 빠지면 동결이다(Q8 = A, 결정됨).** 낙폭 측정은 scheduleless다. 마지막 측정이 신선도 상한보다 오래됐으면 `buy_freeze_state`는 `frozen=True`(사유 stale)를 돌려준다. 측정 시각은 판정 결과의 `evidence`에 들어가고, 측정 CLI의 실행 담당은 운영자(또는 운영자가 지정한 세션)이며 거래일마다 장 시작 전에 돌린다. 신선도 상한의 값과 측정 시각의 정의는 4900이 정한다. 이 문서의 요구는 "측정이 없거나 오래되면 매수가 열리지 않는다" 하나다.
- 모의 계좌도 판정 대상이어야 한다. 판정이 `kiwoom_mock` sleeve에 대해 답을 내지 못하면 전송 직전 경계가 모의 전송도 전부 막아 §4.5 3단계의 모의 검증이 시작되지 않는다. 4900이 모의 계좌의 판정을 어떻게 둘지(실제 측정 또는 명시적 모의 전용 규칙) 정해야 한다.

인터페이스(이 문서가 의존하는 유일한 지점):
- 단일 판정 함수 `buy_freeze_state(session, *, account_mode, market, sleeve_id | None, now) -> FreezeVerdict(frozen: bool, reason: str, evidence: dict, observed_version, valid_until)`. 판정 실패는 `frozen=True`. `observed_version`은 판정이 읽은 측정·동결 기록의 버전이고, `valid_until`은 그 판정을 새로 읽지 않고 쓸 수 있는 마지막 시각이다(측정의 신선도 상한에서 나온다). `valid_until`이 지났거나 값이 없으면 동결로 본다.
- **동결은 그 뒤에 커밋되는 fence만 막는다(I11).** 동결(측정 불가·stale 포함)이 커밋된 뒤에는 그 범위의 전송 fence가 커밋되지 않는다. 그 전에 커밋된 fence는 POST 한 번까지 나갈 수 있고, 그 금액과 예약은 대사 때까지 남는다(§4.6). 순서는 이렇게 정한다. (1) fence 트랜잭션은 동결 범위 lock LF(전역, 그 바구니)를 공유로 잡은 채 `buy_freeze_state`를 읽고 fence를 쓴다. hk 4900의 측정 게시·동결·해제 writer는 같은 lock을 배타로 잡고 쓴다. lock은 그 짧은 트랜잭션 안에서만 쥔다. 게시가 먼저 커밋되면 fence 트랜잭션은 새 상태를 보고 fence를 쓰지 않는다. (2) fence 트랜잭션이 읽은 판정은 그 시점에 유효 기한 안이어야 한다 — 지났으면 동결로 보고 fence를 쓰지 않는다. (3) POST 직전에 판정과 유효 기한을 한 번 더 읽어 동결·기한 경과면 보내지 않고 fence를 해제한다. 이것은 best-effort이고 보장이 아니다: fence 커밋 뒤에 게시된 동결이나 그 사이에 지난 기한이 그 fence의 POST 한 번을 막지 못할 수 있다. lock 키와 유효 기한의 값은 4900이 정하고, 4900의 writer가 LF를 배타로 잡는 것은 4900 구현에 대한 요구다.
- 계획 경로의 소비 지점 셋: 회차 제안 생성(I11 거부), dispatch 계획 게이트(§4.1 조건 9), 그리고 **공통 전송 직전 경계**(`revalidate_and_submit`, 수동·자동 승인 공통). 계획 제안이 동결 전에 사람 카드로 갔다가 동결 뒤 승인되면 승인 콜백이 분류기 없이 `revalidate_and_submit`로 가므로(`app/services/order_proposals/telegram_callback.py:893`, `app/services/order_proposals/revalidation.py:1361-1365`) dispatch 판정만으로는 막지 못한다. 그래서 계획 결속 매수 rung은 승인 방식과 무관하게 fence 트랜잭션에서 `buy_freeze_state`를 읽고, `frozen` 또는 판정 실패면 fence를 쓰지 않는다(`mandate_drawdown_frozen`). 이 배선은 T4c의 범위다(§4.3 (나), §4.6). 그 밖의 매수 경계(untagged 제안, 직접 매수·정정 도구)에 거는 일은 4900의 범위다.
- **스텁과 실제 판정을 구분한다(리뷰 §5).** 4900 구현 전의 `buy_freeze_state`는 항상 `frozen=True`를 돌려주는 스텁이다. 차단 방향이라 안전하지만, 그 동안에는 **모든** 계획 결속 전송이 막힌다 — 모의 계좌도 사람 승인도 포함해서다. 그래서 스텁 상태에서는 "사람 카드로 끝까지 도는 것"을 실제 모의 계좌로 보일 수 없다. 테스트는 판정 함수를 주입한 fixture로 배선만 증명하고("fixture 통과"), 실제 키움 mock에서 끝까지 도는 것("모의 통합 통과", §8 Blocking의 (2))은 4900의 판정 구현이 머지된 뒤에만 완료로 적는다.
- **T5의 선행 조건은 4900의 "구현" 머지다.** 4900의 설계 문서가 머지된 것으로는 부족하다. 위 요구 조건을 충족하는 판정 구현과 그 테스트가 머지돼야 T5를 머지할 수 있고(§8), 그 전까지 §4.1 조건 9는 참이 될 수 없다. 실전 활성화는 그 뒤의 별도 단계다(§4.5).

## 6. 알림·관측

- 자동 집행 알림: 기존 자동승인 카드 경로를 그대로 쓴다 — notices 목적지(`app/core/config.py:1081`, `auto_digest.py:150-155`), veto 버튼, 라운드 안에서는 다이제스트(`dispatch.py:1634`). 카드에 계획 id·회차·위임장 버전·조항 해시·활성화 프로필 버전과 회차 상한·매도 1호가와 그 관측 시각·지정가·프리미엄 bp를 싣는다.
- 체결 알림: 기존 체결 알림(hk 2834 `resolve_fill_intent`)이 rung을 찾으므로 계획 회차는 rung → 제안 → 계획으로 이어진다. 슬리피지 기준가는 지정가.
- 감사: 분류기 결정은 기존 `auto_approve_audit` 어휘에 `mandate_*` 사유를 더해 기록. 계획 이벤트 테이블이 회차별 감사 기록이다.
- 실패 시 동작(강등인지 차단인지는 §4.7의 표가 정본이다. 아래는 그 표에 없는 운영 동작만 적는다):
  - 계획 게이트 읽기 실패(dispatch) → 자동승인 없음, 사람 카드에 "계획 판독 실패" 표시(`mandate_unreadable`). dispatch 트랜잭션은 savepoint로 보존(`dispatch_gate.py:144-156`와 같은 패턴). 전송 직전에 또 읽지 못하면 전송하지 않는다.
  - 알림 발송 실패 → 기존 standalone/다이제스트 실패 처리(attempt fence, 브로커 취소 보상, operator alert)를 그대로 따른다(`approval-and-shadow-contracts.md` ROB-1052 절). 새 재시도 없음. 취소 보상이 실패하거나 체결과 겹쳐도 fence와 누계는 그대로 남는다(§4.6).
  - 전송 결과 불명 → 기존 상태머신의 `unverified`/`uncertain`. 회차는 `tranche_proposed`에 머물고, 전송 fence는 유지되며, 같은 회차의 재제안과 재전송은 차단된다(`mandate_tranche_uncertain`). 원장 대사로만 풀린다. 재전송 없음.
  - 체결 투영 실패 → 회차 `tranche_proposed` 유지, 다음 대사가 재시도. 원장은 권한자.
  - 권한 회수(스위치 off, 프로필 철회, 계획 폐기)는 이미 보낸 주문을 되돌리지 않는다. 그 주문은 대사가 종결을 증명할 때까지 예약·예산·업종 몫을 차지한다(§3.7).
- 사람 카드의 표시: 강등된 계획 결속 제안의 카드는 사유 코드와 함께 예외의 수치(한도, 현재 값, 넘는 금액 또는 어긋난 조건)를 보여 준다. 사유만 있고 수치가 없는 카드는 T9의 테스트가 실패로 본다.
- 관측 지표(retro/v1이 읽을 것): 회차별 트리거가 → 지정가 → 체결가 슬리피지, 체결률, 사람 카드 강등 사유 분포. 새 MCP 도구는 만들지 않는다.

## 7. 결정 질문 — 결정됨

**결정됨(운영자 2026-11-18, `decision/2026-11-18/at-3794-3795-recommended`).** 그 결정 문서는 운영자의 채팅 답 "모두 권고대로 진행하자"를 리뷰 §6의 Q1~Q13 권고안 전부 채택으로 읽었다. 그 읽기는 설계 세션의 해석이고 다르면 운영자가 정정한다고 그 문서가 적고 있다. 이 문서는 그 기록을 따른다. 아래 표는 답과, 그 답이 붙인 조건, 그 답에 맞춰 고친 본문 절이다. 이 답들은 설계 선택을 정한 것이다. 구현·마이그레이션·자동승인 완화·실전 활성화·주문 승인이 아니다(같은 결정의 "이 결정이 주지 않는 것").

| # | 질문 | 답(결정됨) | 답에 붙은 조건 | 반영한 절 |
|---|---|---|---|---|
| Q1 | 위임장 조항을 어디에 두나 | **B** — 버전에 1:1 결속된 별도 append-only 테이블 | 인증된 승인 기록에 결속한다 | §3.1, §3.5, I21 |
| Q2 | 낙폭 동결 측정·해제 | **hk 4900에서 별도 결정** | 그 구현이 끝나기 전에는 자동 활성화 금지 | §5.4, §4.5 C2, §8 T5 선행 |
| Q3 | 위임장 변경 뒤 기존 승인 계획 | **B** — 새 버전이면 재승인(`terms_rebound`, 해시가 같아도) | 기존 미결 주문 처리(R2)를 더한다 | §4.1 조건 4, §5.1, §5.3, §3.7 |
| Q4 | 묶음 승인 마감 뒤 승인 | **B** — 다음 거래일부터 | 배치는 1일 1회. 개장 전·후와 휴장일의 효력 세션 예제 | §5.1, I22 |
| Q5 | 1b 4주 뒤 | **B** — 4주 재검토 뒤 별도 설계 | 자동 전환 없음 | §5.2, §3.1 `loss_sell_mode`, I14 |
| Q6 | 모의 검증 장소 | **A 먼저, 이후 C** — 키움 mock 먼저 | 자동승인 해제는 R4의 별도 검증이 필수 | §4.5 3단계, §4.8, §8 |
| Q7 | 바구니 ①과 기계식 손절(ROB-1460) | **제외 원칙 유지** | 계좌 전체 영향을 설계자가 분리해 보인 뒤 확정 | §5.2 영향 표. 적용 방식(A1/A2)은 아래 "남은 확정" |
| Q8 | 아침 묶음·트리거·낙폭 계산 정기 실행 | **A** — scheduleless CLI만 | 실행이 빠지면 stale 동결. 측정 시각·실행 담당을 적는다 | §1.3, §5.1, §5.4 |
| Q9 | Upbit 소액 실험의 자동 집행 | **B** — 신호 결속은 별도 설계 | — | §1.2 |
| Q10 | 근접가 프리미엄 N | **30bp는 시험 후보** | 실전 수치는 측정 증거 뒤 운영자가 확정. 수익·손실 보장 값이 아니다 | §3.1, §4.1 조건 6, 아래 증거 목록, §4.5 C9 |
| Q11 | 계획 결속 주문이 전역 daily cap을 공유하나 | **A** — 공유 | 바구니 합산·원자성(R3)을 포함한다 | §4.6, §4.4 |
| Q12 | 1차-only 활성화 프로필을 기계적으로 강제하는 보완을 먼저 반영할지 | **예** | 회차 확대는 별도 승인 | §3.6, I18, §4.1 조건 1a |
| Q13 | R1~R5와 PR 선행조건 보완 뒤 구현 티켓을 분리할지 | **예** | 지금 live 활성화·주문·마이그레이션 승인이 아니다 | 이 개정 전체, §8 |

### 7.1 Q10 — 30bp를 실전 값으로 확정하는 데 필요한 측정 증거

30bp는 자문 §2.1의 "+0.3% 이내"와 맞는 **시험 후보**다. 모의와 전송 없는 검증은 30bp로 돌린다. 실전에서 이 값이 쓰이는 것은 D1이 열리는 자동승인 단계부터다(§4.4 "D1의 범위"). 사람 카드 단계(§4.5 4단계)의 실전 조항은 이 값을 시험 후보 30으로 담지만, 그 단계에서는 스위치가 꺼져 있어 지정가가 현재가를 넘지 못하므로 값이 쓰이지 않는다. 자동승인 단계로 가기 전에 운영자가 아래 증거를 보고 값을 확정한다(§4.5 C9). 확정 값이 조항의 값과 같으면 조항은 그대로이고 증거 문서의 hk key를 활성화 프로필 승인 요청의 `approval_ref`에 적는다. 다르면 새 위임장 버전이 필요하고, 그러면 Q3 = B에 따라 기존 계획의 재승인(`terms_rebound`)과 새 활성화 프로필이 뒤따른다. 증거가 없으면 활성화 프로필 승인 요청을 올리지 않는다.

| # | 측정 | 왜 필요한가 | 원천 |
|---|---|---|---|
| E1 | venue별(KIS KR, Toss KR, 그리고 US를 열 때 그 venue) 호가 읽기에서 전송까지의 경과 시간 분포와, 신선도 상한 5초를 넘는 비율 | 5초가 실제로 지켜지는 값인지. 넘는 비율이 크면 자동승인이 거의 열리지 않는다 | T4d의 호가 읽기 기록(주문 없이 측정 가능) |
| E2 | 그 경과 시간 동안 매도 1호가가 움직인 폭(bp)의 분포 | 읽은 ask 기준 +N bp 지정가가 전송 시점에도 체결 가능한 가격인지 | 호가 기록(결정 §1-7: fillwire `[books]`·`[quotes]`) |
| E3 | 대상 종목의 스프레드((매도 1호가 − 매수 1호가) ÷ 중간가) 분포, 시간대별(개장 직후·장중·마감 전) | 스프레드와 N은 다른 것이다. 스프레드는 매도 1호가에서 사는 순간 치르는 **매매 비용**이고 N과 무관하게 발생한다(N = 0이어도 지정가가 매도 1호가면 체결될 수 있다). N은 호가를 읽은 뒤 전송까지 매도 1호가가 **움직이는 폭에 대한 여유**다(E2). 그래서 N을 스프레드보다 크게 잡아야 한다는 뜻이 아니다. 스프레드 분포는 근접가 진입의 비용 크기를 보려는 것이고, N의 크기는 E2가 정한다 | 호가 기록 |
| E4 | 호가 단위 반올림의 영향: 지정가는 ask × (1+N bp)를 호가 단위로 **내림**한다. KRX 주식의 호가 단위는 2,000원 이상 종목에서 가격대 경계 기준 가격의 최대 0.25%다(§2.3 F10). 종목 가격대별로 내림 뒤 실효 프리미엄(bp)이 얼마인지, N이 호가 단위보다 작아 한 호가 단위도 못 올리는 경우(실효 0bp — 예: N = 10bp인데 호가 단위가 20~25bp인 가격대)가 얼마나 되는지 | 정수 호가 때문에 실효 프리미엄은 N보다 작아지고, 그 폭이 종목 가격대마다 다르다 | F10의 표로 계산 + 대상 종목 가격 |
| E5 | 기록된 호가로 본 반사실: N ∈ {10, 30, 50}bp에서 "ask + N bp 지정가가 그 뒤 일정 시간 안에 체결 가능했는가"의 비율 | 세 후보를 같은 자료로 비교 | 호가 기록(주문 없음) |
| E6 | 키움 mock 30건의 체결률·슬리피지(체결가 − 지정가, 체결가 − 읽은 ask) | 계획 경로의 기계 지표(체결률 ≥ 80%, 슬리피지 중앙값 ≤ 0.3%). **한계**: 모의 체결은 실제 호가 잔량을 반영하지 않을 수 있다. 실전 체결의 증거가 아니다 | §4.5 3단계 |
| E7 | 미체결·부분 체결 비율과 회차당 재제안 횟수 | N이 작을수록 미체결과 재제안이 늘어난다. 그 운영 비용 | E5, E6 |
| E8 | venue별 차이: KIS와 Toss의 호가 출처·지연·venue 구성(Toss **분봉**은 KRX+NXT 합산이라고 결정 수정 3의 자문 메모가 적었다. 호가가 어떤지는 이 문서에서 확인하지 않았다) | 같은 N이 두 계좌에서 같은 뜻인지 | venue별 E1~E4 |

- N은 손실 한도가 아니다. 주문 시점 매도 1호가 대비 지정가의 상한이다.
- 운영자는 N을 조항마다 정한다. KIS와 Toss의 조항 값이 달라도 된다(조항은 sleeve별이다).
- 실전 카나리가 시작된 뒤의 실제 체결 자료로 N을 다시 정하는 것은 위임장 변경(새 버전)이다.

### 7.2 남은 확정과 이 개정이 새로 정한 설계 선택

운영자 확정이 남은 것(실전 활성화 전에 필요, 구현 티켓 분리는 막지 않는다):

1. **Q7 적용 방식**: §5.2 영향 표의 A1(시장 목록으로 제외, KR 전체가 빠진다) / A2(계획 종목 제외, ROB-1460 코드 변경 — 별도 티켓과 결정 필요). [의견] 지금은 A1. KR에 기계식 손절을 켜려는 결정이 나오면 A2가 먼저다.
2. **Q10 실전 값**: §7.1 증거 뒤.
3. 조항의 모든 금액·비율·만료, 계획의 금액·편입액: 승인 요청 때 운영자가 정한다. 코드·정책에 기본 금액은 없다.

이 개정이 리뷰 권고를 구체화하면서 새로 정한 설계 선택([의견]). 운영자 답이 직접 고른 것이 아니므로 이 문서의 리뷰·머지에서 다르게 정할 수 있다:

| 선택 | 내용 | 절 |
|---|---|---|
| 승인 채널 | 조항·활성화 프로필의 승인은 nonce 결속 Telegram 콜백으로만 기록한다. CLI는 요청만 만든다. 콜백은 chat allowlist에 더해 발신자를 운영자 principal 목록(`MANDATE_APPROVER_TELEGRAM_USER_IDS`, 기본 빈 값)과 대조한다 | §3.5 |
| 편입액 산입 | 계획 금액은 편입액을 포함한 목표 총액. 편입액은 두 실계좌 보유 합계. 예산에는 신규 매수액만, 업종 분자에는 계획 금액 전액 | §3.8 |
| 업종 상한의 기준 | 시세 평가액이 아니라 계획 금액 기준. 분모는 바구니 예산 합 + 편입액 합 | §3.8 |
| 바구니 단위 값이 sleeve마다 다를 때 | 가장 엄격한 값 | §3.1 |
| 4주의 시작 | 실계좌 sleeve의 첫 계획 결속 제안(`tranche_proposed`) 시각을 거래일로 옮긴 날, 달력 28일 | §5.2 |
| 회차 체결 확정일 | 마지막 authoritative 체결의 거래일(잔여 포기 기록일이 아니다) | §3.2 |
| 계획 전체 한도 초과 | 사람 승인으로도 넘지 못한다(차단) | I8, §4.7 |
| 권한이 끝난 계획의 제안 | 사람 승인으로도 전송하지 않는다(차단) | I16, §4.7 |
| 보유 처리 증거 | `zero_holdings` / `kept_outside_basket`(운영자 기록) / `never_adopted` | §3.7 |
| 계획 상태 `suspended` | 운영자 정지만. 동결·위임장 비활성은 계획 상태가 아니라 판정 | §3.2 |
| 묶음 배치 | 앞 배치가 닫힌 뒤에만 새 배치. 승인 마감(T 개장)과 배치 만료(T 장 종료)를 구분 | §5.1 |
| D1의 범위 | 스위치가 켜진 시장·계좌의 계획 결속 매수면 사람 승인에도 열린다. 스위치가 꺼져 있으면 닫힌다 | §4.4 |
| 실전 단계 | 사람 카드 단계(계획 결속 제안 생성 계좌 설정)와 자동승인 단계(프로필 + 스위치)를 나누고, 돈과 관련된 체크리스트 항목은 앞 단계에 둔다 | §4.2, §4.5 |
| 전송 fence | 자동·사람 모든 계획 결속 전송 앞에, 호출자와 다른 세션의 짧은 트랜잭션으로 판정과 함께 커밋. fence 하나에 POST 한 번 | §4.6 |
| 전역 누계의 계좌 범위 | 당일 자동 fence는 스위치와 무관하게 모든 자동승인의 누계에 들어간다. 위임장 자동승인이 열렸거나 당일 fence가 남은 계좌 모드·시장에서는 모든 자동승인이 `broker_account_id` 키로 나누지 않은 같은 G를 읽는다. 새 lock L4a는 항상 | §4.6 |
| 계좌 결속 | 조항·계획에 실제 주문 계좌의 불투명한 참조를 고정하고, 실제 송신 요청의 계좌와 첫 mutation 직전에 대조 | §4.6 |
| 권한의 단위 | 커밋된 전송 fence. 폐기·정지·논지 훼손·동결은 그 뒤에 커밋되는 fence만 막고, 그 전에 커밋된 fence는 POST 한 번까지 나갈 수 있다. advisory lock 보유는 권한의 근거가 아니다. POST 직전 재확인은 best-effort | §4.6 |
| 보유 범위 계좌의 고정 | 계획 등록 때 범위의 각 실제 계좌 참조를 편입액 0이어도 고정하고, 보유 0의 증거는 그 참조와 일치하는 계좌의 조회만 받는다 | §3.2, §3.7 |
| 바구니 정체성 | (영역, `basket_key`). 모의와 실계좌는 같은 이름이어도 다른 바구니 | §3.1, §3.8 |
| 손실 매도 가드 | 계획 종목의 `loss_cut`은 생성·승인 전·전송 직전에 `thesis_broken` 증거(회고 id·승인 이슈 id)를 확인. 계좌 무관, 종목 lock 아래. 가드는 T4c | §5.2 |

## 8. 티켓 분해

원칙: 티켓 하나 = PR 하나. tracer bullet 순서는 "모델 → 계획 등록 → 사람 카드로 끝까지 → 자동승인 해제" — 자동승인 없이도 계획 경로가 사람 카드로 끝까지 도는 상태를 먼저 만든다. 이 표는 구현 티켓을 나누기 위한 것이고, 표에 있다는 것이 그 티켓의 착수·머지·활성화 승인은 아니다.

| # | 티켓 | 내용 | 선행 | T | grade | surface floor | 마이그레이션 |
|---|---|---|---|---|---|---|---|
| T1 | 조항·승인·활성화 프로필·계획 모델 | §3.4의 아홉 테이블(`mandate_approval_requests`, `mandate_approvals`, `mandate_terms`, `mandate_activation_profiles`, `mandate_plans`, `mandate_plan_tranches`, `mandate_plan_events`, `mandate_symbol_reservations`, `mandate_symbol_reservation_releases`), 트리거, `order_proposals` 두 열과 그 INSERT 트리거, 조건부 GRANT, downgrade 거부, store(유일 writer), `fold_plan_events`와 순수 불변식 함수, lock 순서 정의(§4.6), 예약과 바구니 정체성의 영역(`live`/`mock`) 키, 조항·계획의 계좌 참조(`account_ref`)와 보유 범위 계좌 참조(`scope_account_refs`), 그 비교(I15의 등록 쪽), 권한 상태가 `approved`/`active`가 아닌 계획의 fence INSERT를 거부하는 이벤트 트리거(I16·I24), 계획 등록의 열린 `loss_cut` 제안 거부(`mandate_open_loss_cut`, §5.2), 그리고 바구니 ①의 sleeve 규칙 id를 sleeve 규칙 어휘에 추가(지금은 `CANARY_RULES`에 없는 규칙으로 sleeve를 만들 수 없다, §2.3 F12. 기존 항목은 고치지 않는다) | — | T2 | A | DB 가드, sleeve 규칙 어휘(추가만) | **있음** |
| T2 | 위임장·계획 CLI | `scripts/mandate.py`: 승인 요청 생성(조항·활성화 프로필, 운영자 actor만), 계획 등록(proposed, 편입액과 그 증거), 운영자 계획 이벤트(`suspended`·`resumed`·`retired`·`tranche_closed_partial`·`thesis_broken`·`thesis_evidence_refreshed`, `holdings_disposed`의 `kept_outside_basket` 종류), 상태 조회. **기본 dry-run, 명시적 `--commit` 때만 쓰기** | T1 | T2 | B | 운영자 CLI | 없음 |
| T3a | 아침 묶음 승인 카드 | `mandate_plan_batches`·`mandate_plan_batch_members` 두 테이블(불변 스냅샷), 배치 단일성(I22: UNIQUE와 "앞 배치가 닫힌 뒤에만" 트리거)·효력 세션·만료·재발송(§5.1), 발송 CLI, 주문 경로와 분리된 nonce 결속 콜백 `mpb`(계획 이벤트만 append, 주문 모듈 import 금지 정적 테스트), 운영자 principal 대조(`MANDATE_APPROVER_TELEGRAM_USER_IDS`, 기본 빈 값 = 승인 불가, §3.5) | T1, T2 | T2 | A | Telegram 콜백(새 경로, 기존 콜백 불변) | **있음** |
| T3b | 조항·활성화 프로필 승인 카드 | nonce 결속 콜백 `mta`(§3.5): 승인 기록 + live sleeve 버전·조항, 또는 활성화 프로필 행. 낡은 요청·nonce 재사용·목록에 없는 발신자는 무기록(운영자 principal 대조, §3.5). 주문·제안 모듈 import 금지 정적 테스트. 승인 화면 표시 항목(§4.5)의 자리 | T1, T2 | T2 | A | Telegram 콜백(새 경로), sleeve 버전 append | 없음 |
| T1m-a | 키움 체결 귀속·원장 투영 | 키움 체결 귀속을 계좌·거래일·주문번호·rung idempotency 키로 매칭(키움 주문번호는 계좌·거래일 안에서만 유일, `app/models/review.py:874-878`; 지금 로더는 KIS만 거래일을 붙이고 `execution_ledger/supersede.py:104-106`은 키움 거래일이 없다 — `store.py:599-605`, `:661-690`), 모호한 체결은 미측정. 순수 매칭 함수와 로더를 고정 fixture로 증명한다. sleeve 계좌 조합·CHECK·어휘는 이 PR에서 바꾸지 않는다 | — | T2 | A | sleeve 체결 로더(읽기) | 없음 |
| T1m-b | 모의 sleeve 조합 | sleeve 계좌 조합 DB CHECK와 런타임 어휘(`SLEEVE_ACCOUNT_MARKETS`, `app/models/strategy_sleeves.py:35-41`; `create_sleeve` 검사, `app/services/strategy_sleeves/store.py:303-308`)에 `kiwoom_mock`/`equity_kr` 추가, `LEDGER_IDENTITY`에 `kiwoom_mock → (kiwoom, mock)` 추가(`vocabulary.py:126-135`의 동치 단언 유지, `store.py:599` 로더). live 조합·게이트 불변. T1m-a의 귀속이 머지돼 있어야 이 조합의 사용액·손익이 측정된다 | T1m-a | T2 | A | DB CHECK·sleeve 어휘 | **있음** |
| T4a | 계획 결속 제안 + 계획 게이트(판정만) | `order_proposal_create(mandate_plan_id, tranche_index)`와 생성 거부(§4.2), I15 재비교, dispatch 계획 게이트(§4.3 (가): 조항·승인 기록·활성화 프로필·회차 상한·업종·회차 조건 판정, 사유와 수치 기록), lock L2 → L3, 회차 이벤트 `tranche_armed`·`tranche_proposed`. 분류기 진입부의 거부 `mandate_not_eligible`(I23, §4.1 조건 0): 이 PR에서는 `mandate_binding`을 소비하는 코드가 없으므로 **모든** 계획 결속 rung이 이 거부로 사람 카드가 된다. 이 PR은 아무 게이트도 풀지 않는다. 🔴 계획 결속 제안을 만들 수 있는 계좌는 `MANDATE_PLAN_PROPOSAL_ACCOUNT_MODES`(기본 빈 값)에 있는 것뿐이고, 이 PR의 코드는 그 값으로 `kiwoom_mock`만 받는다 | T1 | T2 | A | dispatch·분류기(판정과 거부 추가만, 완화 없음) | 없음 |
| T4b | 체결 투영과 정리 | 원장 체결 → 회차 이벤트(`tranche_filled`·`tranche_partially_filled`·`tranche_unfilled`, 2b 기준값과 고정 예제 §3.2), `first_tranche_settled`·`exhausted`·`closed`(매수 주문 종결 + 계획 계좌 보유 0 확인), 보유 범위는 계획의 계좌 모드로, 보유 0의 증거는 `scope_account_refs`와 참조가 일치하는 계좌의 조회만(§3.7), 정리 이벤트(`orders_settled`·`holdings_disposed`·`released`)와 예약 해제(§3.7), 투영 CLI(기본 dry-run, 명시적 `--commit` 때만 쓰기). 원장에는 쓰지 않는다 | T4a | T2 | A | 계획 이벤트 쓰기(원장은 읽기만) | 없음 |
| T4c | 계획 결속 전송 직전 경계와 손실 매도 가드 | §4.3 (나)의 여섯 재확인(수동·자동 승인 공통, 실패 = 전송 차단), 사람 승인 콜백의 nonce 소비 전 판정, §4.6의 fence 트랜잭션과 전송 순서(별도 세션의 짧은 트랜잭션에서 판정과 fence INSERT, fence 하나에 POST 한 번, 해제 writer, POST 직전의 best-effort 재확인과 계좌 참조 대조. I11·I15·I16·I24), 손실 매도 가드(I14, §5.2: `loss_cut` 제안 생성·승인 전·전송 직전의 `thesis_broken` 증거 결속, 계좌 무관 조회, 생성은 종목 lock 아래의 짧은 트랜잭션), `buy_freeze_state` 인터페이스와 스텁(4900 구현 전에는 항상 frozen). **이 PR이 머지돼야** 계획 결속 제안 생성 계좌로 live 계좌 값을 받는다(같은 PR에서 넓힌다. 그래도 설정 기본값은 빈 값이다). D4를 적은 deviation record 파일을 이 PR이 만든다(§4.5) | T4a, **hk 5286**(전송 fence 직렬화 메커니즘 확정 — 그 전에는 구현을 시작하지 않는다, §4.6 "미확정") | T2 | A | 전송 직전 경계(거부만), `loss_cut` 제안 생성·승인 경로(거부 추가만), live 계좌의 계획 결속 제안 생성(기본 off 설정 뒤) | 없음 |
| T4d | 매도 1호가 소스 | venue별 호가 읽기(관측 시각·유한 양수·신선도 상한·fail-closed, 대체 없음), 계획 게이트와 분류기에 입력. Q10 증거 E1의 기록 | T4a | T2 | A | 읽기 전용 브로커 시세 | 없음 |
| T5 | **분류기·preview 해제(게이트 완화)** | `mandate_binding` + D2 해제, D1 `allow_mandate_bound_buy`, 파킹 면제 비적용, 스위치 3개(기본 off), 자동 경로의 fence(`approval_mode='auto'`)와 두 누계(§4.6, I20), 전역 누계 reader 보강(당일 자동 fence는 스위치와 무관하게 항상 포함, 열렸거나 당일 fence가 남은 계좌 모드·시장에서는 모든 자동승인이 같은 G)과 항상 잡는 L4a lock, deviation record에 D1~D3 추가(파일은 T4c가 D4로 만든 것). **머지 조건: §4.8의 V1~V22가 이 PR 안에서 전부 통과** | T1m-a, T1m-b, T3a, T3b, T4a, T4b, T4c, T4d, T9, **hk 4900의 판정 구현 머지**(설계 문서 머지가 아니다), 키움 **모의 통합 통과** 보고(아래 Blocking의 (2). 4주·30건을 채우는 것은 머지 조건이 아니라 체크리스트 C3다) | **T3** | S | **자동승인·주문 게이트** | 없음 |
| T6 | 낙폭 동결 | 설계·구현은 **hk 4900**. 이 문서는 `buy_freeze_state` 인터페이스와 요구 조건만 둔다(§5.4). 이 설계의 티켓이 아니다 | hk 4900 | 4900이 정한다(그 설계 문서 작업은 T1) | 4900이 정한다(그 설계 문서 작업은 S) | 매수 전송 경계(거부만) — 구현 티켓의 값은 4900이 정한다 | 4900이 정한다 |
| T7 | 2·3차 트리거 | 가격·시간 조건 판정(I5·I6·I7, 2b 기준값) → armed → 제안 생성. scheduleless CLI. 활성화 프로필 상한이 1인 동안 이 제안은 사람 카드다(§3.6) | T4a, T4b | T2 | A | 제안 생성만 | 없음 |
| T8 | 논지 훼손 판정과 매도 제안(1b) | 논지 훼손 판정의 자동화: `invalidation_conditions` + 결정론 검사 → `thesis_broken` 이벤트(`approved`·`active`·`suspended`·`exhausted`에서), 회고 기록, 72시간 뒤의 `thesis_evidence_refreshed`(폐기된 미해제 계획 포함), 증거의 순서(승인 이슈·회고 먼저, 그 두 id로 이벤트, 같은 id로 제안 — §5.2), 기존 `exit_intent='loss_cut'` 사람 승인 의식으로 제안(rationale에 계획·이벤트 id, 카드에 미결 매수 주문 수). 가드 자체는 T4c에 있다 — T8은 그 가드를 통과하는 증거를 만든다. 새 exit intent 없음. 시계를 옮겨도 사람 카드라는 테스트(§5.2) | T4a, T4c | T2 | A | 제안 생성만(완화 없음) | 없음 |
| T9 | 알림·관측 | 카드에 계획·회차·조항 해시·활성화 프로필, 강등 카드의 사유와 수치(§6), 활성화 프로필 승인 카드의 승인 화면 표시 항목 렌더(§4.5), audit `mandate_*` 사유, retro/v1 읽기 연결 | T4a, T3b | T2 | B | 알림 | 없음 |

새 테이블 수: T1 아홉 + T3a 둘 = 열하나(§3.4와 같다). T4a~T9는 테이블을 만들지 않는다.

**불변식 소유 티켓.** 각 불변식을 어느 티켓이 구현하고 어디서 검증하는지다. 소유 티켓이 둘 이상이면 각 티켓이 자기 판정 지점을 맡고, 검증은 그 티켓 자신의 PR에 있는 테스트다. T5는 같은 사례를 완화가 들어간 코드에서 §4.8의 V 번호로 다시 통과시킨다.

| 불변식 | 구현 소유 티켓 | 검증 위치 |
|---|---|---|
| I1 종목당 미해제 계획 1개(영역별) | T1(예약 테이블·트리거·종목 lock) | T1 DB 경쟁 테스트(CI). 모의 계획이 있어도 같은 종목의 실계좌 계획이 등록된다 |
| I2 `allowed_symbols` | T1(store) | T1 단위 |
| I3 예산(sleeve·바구니) | T1(store, lock L2 → L3) | T1 DB 경쟁 테스트(CI), §3.8 예제 |
| I4 업종 상한 | T1(등록 거부) + T4a(dispatch 강등) | T1 단위(§3.8 예제 수치), T4a 통합 |
| I5 가격 트리거 | T1(순수 함수) + T7(arming) + T4a(재검) | T1 단위(§3.2 고정 예제), T7 통합 |
| I6 최소 간격 | T1(순수 함수) + T7 + T4a | 같음(체결 확정일 정의 포함) |
| I7 회차 순서 | T1(순수 함수·이벤트 트리거) + T7 + T4a | T1 DB, T7 통합 |
| I8 회차·계획 금액 | T4a(생성 거부·강등) + T4c(계획 전체 한도 차단) | T4a 단위·통합, T4c 통합 |
| I9 회차당 열린 제안 1개, 불명이면 정지 | T1(이벤트 트리거) + T4a(생성 거부) + T4c(전송 직전) | T1 DB 경쟁(CI), T4c 통합 |
| I10 체결은 원장 증거로만 | T4b | T4b 통합(원장 fixture, websocket 단독 행은 미확정) |
| I11 낙폭 동결 | T4a(생성·dispatch) + T4c(fence 트랜잭션의 판정, LF), 판정·writer는 hk 4900 | T4a·T4c 통합(판정 함수와 시계 주입: 먼저 게시된 동결이면 fence 없음, 유효 기한 경과면 fence 없음, fence가 먼저면 POST 한 번). 실제 판정은 4900의 테스트 |
| I12 live·만료 | T4a | T4a 단위 |
| I13 행 불변 | T1(트리거, 아홉 테이블) + T3a(두 테이블) | 각 PR의 DB 테스트(CI) |
| I14 손실 매도는 `thesis_broken` 뒤에만, 사람 카드 | T4c(가드: 생성·승인 전·전송 직전) + T1(계획 등록의 열린 `loss_cut` 거부, `loss_sell_mode` CHECK) + T8(판정·증거) + 기존 분류기(변경 없음) | T4c 통합(§5.2 테스트 계약 (a)~(g)), T1 DB, T8의 시계 이동 테스트 |
| I15 결속 일치(계좌 참조 포함) | T1(`order_proposals` INSERT 트리거, 조항·계획의 `account_ref`) + T4a(재비교) + T4c(fence 트랜잭션, 어댑터의 POST 직전 대조) | T1 DB, T4a·T4c 통합(실제 송신 계좌 불일치면 호출 0) |
| I16 계획 권한 상태·효력 세션(그 뒤에 커밋되는 fence만 막는다) | T1(fence INSERT를 거부하는 이벤트 트리거) + T4a(생성·dispatch) + T4c(fence 트랜잭션) | T1 DB(전이 뒤의 fence INSERT 거부, `seq` 경쟁), T4c 통합(§4.8 V22의 두 순서) |
| I17 파킹 심볼 제외 | T1(store) | T1 단위 |
| I18 활성화 프로필 회차 상한 | T1(테이블·트리거) + T3b(승인) + T4a(판정) | T4a 단위·통합 |
| I19 `released` 전 예약·예산 유지 | T1(트리거, `scope_account_refs`) + T4b(정리 이벤트, 참조 일치 확인) | T4b 통합(미결·불명·닫히지 않은 fence·보유 각각, 범위 계좌가 바뀐 경우의 거부), T1 DB |
| I20 두 누계와 자동 전송 fence | T5(fence 이벤트·사람 경로 writer는 T4c가 먼저) | §4.6 acceptance, §4.8 V16(CI DB 경쟁) |
| I24 권한의 단위는 커밋된 fence, fence 하나에 POST 한 번, 닫히지 않은 fence가 있으면 새 fence 없음 | T4c(fence 트랜잭션과 사람 경로) + T5(자동 경로) | §4.6 acceptance(순서·연결 상실·롤백·하위 재시도 금지), T4c 통합 |
| I21 인증된 승인 기록 | T1(트리거) + T3b(콜백) + T4a·T4c(재확인) | T1 DB, T3b 통합(nonce 재사용·낡은 요청), T4a·T4c 통합 |
| I22 배치 단일성 | T3a(UNIQUE + 트리거) | T3a DB(CI), §5.1 세션 예제 표를 그대로 단위 테스트로 |
| I23 계획 결속의 다른 자동승인 길 없음 | T4a(분류기 진입부 거부) | T4a 단위·통합(지정가가 현재가 아래인 계획 결속 매수가 자동승인되지 않는다) |

**Blocking.**

```
T1 ──┬─ T2 ──┬─ T3a
     │       └─ T3b ──┐
     └─ T4a ──┬─ T4b ─┼─ T7
              ├─ T4c  │
              ├─ T4d  │
              ├─ T8   │
              └───────┴─ T9
T1m-a ── T1m-b
(T1m-a, T1m-b, T3a, T3b, T4a, T4b, T4c, T4d, T9, hk 4900 구현, 모의 통합 통과) ── T5
```

- **모의 검증의 세 단계를 구분한다.** (1) fixture 통과: 각 PR의 테스트가 판정 함수와 브로커를 주입해 배선을 증명한다. 4900 구현 전에도 된다. (2) 모의 통합 통과: 실제 키움 mock 계좌에서 계획 등록 → 묶음 승인 → 제안 → 사람 카드 승인 → 전송 → 체결 투영 → 정리까지 돈다. T1m-a·T1m-b·T3a·T3b·T4a·T4b·T4c·T4d와 **hk 4900의 판정 구현**이 머지돼야 시작할 수 있다(그 전에는 스텁이 모든 계획 전송을 막는다, §5.4). "사람 카드로 끝까지 돈다"는 말은 (2)부터 쓴다. (3) 모의 검증 기간 충족: (2)를 4주 동안 30건 이상 돌려 기계 지표를 채운 것. T5의 머지 선행은 (2)이고, (3)은 실전 사람 카드 단계의 체크리스트 C3다(§4.5).
- **T4c는 hk 5286에 blocked다.** 전송 fence 트랜잭션의 직렬화 메커니즘(공통 lock 키, DB 제약)을 hk 5286이 확정하기 전에는 T4c 구현을 시작하지 않는다(§4.6 "요구사항"·"미확정"). T4c의 검증 계약에는 §4.6의 두 사례(두 계획·세션 상실·누계, 세션 상실 뒤 프로필 철회)가 들어간다. T4c에 걸린 T5·T8과 실전 단계도 그만큼 뒤로 간다.
- **T4c 전에는 live 계좌에 계획 결속 제안이 생기지 않는다.** T4a의 코드가 live 계좌 값을 받지 않기 때문이고, 설정을 바꿔도 마찬가지다. 마지막 안전 경계(전송 직전 경계)가 없는 상태로 live에 노출되는 구간이 없다. T4c 뒤에도 `MANDATE_PLAN_PROPOSAL_ACCOUNT_MODES`에 live 계좌를 넣는 것은 체크리스트의 "사람 카드" 항목을 채운 뒤의 운영자 승인 단계다(§4.5 4단계).
- **T5는 위 선행이 전부 머지되고 모의 통합 통과(2) 보고가 있은 뒤에만 머지한다.** T5의 선행인 hk 4900은 판정 **구현과 테스트**의 머지다.
- T8(논지 훼손 판정의 자동화)은 T5의 머지 선행이 아니다. 손실 매도 가드(I14)는 T8이 아니라 T4c에 있으므로 T8이 없어도 실전에서 `thesis_broken` 없는 손실 매도는 나가지 않는다. T8이 없을 때 달라지는 것은 판정과 증거를 운영자가 CLI로 직접 기록해야 한다는 점이고, 그 임시 절차와 운영자 기록을 체크리스트 C8이 요구한다(§4.5).
- T7은 T5의 선행이 아니다. T7이 먼저 머지돼도 활성화 프로필 상한이 2·3차 자동승인을 막는다(§3.6).
- 실전 활성화는 어떤 티켓의 머지로도 일어나지 않는다(§4.5).

## 9. 알았지만 건드리지 않은 것

- 결정 §5-1: "11-02 KR 물타기 중단 결정이 정책 YAML에 아직 없다" — `buy.underwater_support_net`(`config/trading_policy.yaml:891`)은 이 문서에서 끄지 않는다. 결정 §1-3의 "신규 매수·물타기 티어 끄기"를 정책에 반영하는 일은 별도 티켓이다. 그 티켓의 완료(또는 운영자 예외 기록)는 실전 활성화의 선행 조건이다(§4.5 C6, §4.4 D4).
- 정책 `portfolio.sector_cluster_cap_pct`는 advisory(fail-open)다(`data-model-and-symbol-contracts.md` ROB-646). 위임장의 업종 상한 35%는 그것과 별개의 판정이며 전역 advisory를 바꾸지 않는다. 바구니 밖 보유의 업종 쏠림은 여전히 그 advisory의 몫이다(§3.8).
- 현금 규칙 B(대기금 계산·파킹 매도)는 위임장과 연결되지만(남은 분할 몫 = 계획 테이블에서 계산 가능) 이 설계 범위 밖이다. **범위 밖이라는 말은 그것 없이 실전을 열어도 된다는 뜻이 아니다.** 현금 규칙 B는 운영자 결정(§1-4)이고, 그 작업의 완료 또는 운영자 예외 기록이 실전 활성화 체크리스트 C5다(§4.5). 계획 테이블은 그 입력(남은 회차 금액)을 제공한다.
- 위임장 조항의 per-order/daily cap이 전역 cap 이하인지는 DB CHECK로 강제할 수 없다(전역 값이 YAML). store 검증과 분류기의 min()으로 이중 보호한다.
- Toss 같은 종목 반대 방향 대기 주문 거부(결정 수정 1)는 계획이 한 계좌에만 있는 I1로 대부분 피하지만, 같은 계좌의 기존 보유 trim 매도(`sell.trim_preplace`, 11-16 결정 4로 유지)와 회차 매수가 같은 종목에서 겹치면 Toss가 거부할 수 있다. 편입 종목의 trim 처리는 별도로 다룬다.
- 데이터 소스 혼용(결정 수정 3)·브로커 API 재확인(수정 4)은 이 설계와 무관하다.
- 기존 자동승인 일 누계가 전송 결과 불명인 **일반** 자동 전송을 놓칠 수 있다는 것(§2.3 F3a)은 추정이고 이 문서에서 감사하지 않았다. 계획 결속 전송은 자체 fence로 이 가정에 기대지 않지만, 계획 결속 판정이 읽는 G에는 일반 전송의 스탬프가 들어가므로 그 구멍이 영향을 준다. 기존 경로의 수정은 이 설계의 티켓이 아니고, 자동승인 단계의 체크리스트 C14가 감사 결과 또는 운영자 수용 기록을 요구한다(§4.5).
- ROB-1460을 종목 단위로 제외하는 변경(§5.2의 A2)은 하드룰 19의 표면이라 이 설계의 티켓에 넣지 않았다.
- 기존 주문 승인 콜백(제안 승인·veto·손절)은 chat allowlist만 보고 발신자가 운영자인지는 확인하지 않는다(§3.5의 [사실]). 이 설계는 위임장 콜백과 계획 결속 제안의 승인에만 운영자 principal 대조를 더한다(§3.5). 계획 결속이 아닌 제안의 승인·veto·손절 콜백은 바꾸지 않았다.
- 위임장 자동승인이 열리지 않은 계좌 모드·시장에서는 자동승인이 `broker_account_id` 키(NULL과 다른 값)로 나뉘어 세어지는 기존 전역 누계의 동작이 그대로다. 열린 계좌 모드·시장에서만 모든 자동승인이 키로 나누지 않은 같은 G를 읽는다(§4.6).
- 사람 승인 콜백의 트랜잭션이 브로커 호출 뒤에 롤백되면 rung 상태와 nonce 소비가 사라지는 기존 동작(§4.6)은 계획 결속이 아닌 제안에는 그대로다. 이 설계는 계획 결속 전송에만 fence를 둔다.
- 자동 취소 보상이 실패하거나 체결과 겹칠 때의 실제 복구 결과는 실행으로 검증하지 않았다(리뷰 §4의 미확인 항목). §4.8 V9는 그때 예약·누계·회차 상태가 안전한 쪽으로 남는지만 본다.

## 10. 개정 이력

hk 5272(2026-11-19). 대상 리뷰: `review/2026-11-16/dots-at-mandate-design`. 운영자 답: `decision/2026-11-18/at-3794-3795-recommended`. 리뷰의 수정 권고 항목마다 반영 위치를 적는다. "문제 없음" 판정에 붙은 세부 권고도 포함한다.

| 리뷰 항목 | 권고 요지 | 반영 위치 |
|---|---|---|
| §1 1a — 묶음 승인 | 거래일당 배치 중복 생성·재발송·부분 승인·만료의 단일성. 재발송은 기존 배치, 추가 승인은 다음 거래일 | §5.1 "배치 단일성"(승인 마감·배치 만료 구분, 앞 배치가 닫힌 뒤에만 새 배치), §3.3 I22, §8 T3a |
| §1 1b — 4주 건별 승인 | 4주의 시작일을 감사 가능한 시점으로. 기간 경과만으로 권한이 바뀌지 않는 테스트 | §5.2 "4주의 시작 시점"·"기간이 지나도", §3.1 `loss_sell_mode`, §3.3 I14, §8 T8 |
| §1 1c — 변경·한도 초과·승격 | 정상적인 금액 한도 예외와 파손된 결속·중복·불명을 구분. 일반 승인 클릭이 불변식 위반을 합법화하지 않게 | §4.7, §4.3 (나), §5.3 |
| §1 2b — 분할 수치 | 부분체결·잔여포기 시 기준일·가격, 재제안 시 소진액을 테스트 계약으로 | §3.2 "회차 기준값" 표와 고정 예제, §8 T4b·T7 |
| §1 2c — 동결 | 문제 없음(측정은 hk 4900) | §5.4(Q2·Q8 조건 추가) |
| §1 2a — 현금 규칙 B | 범위 밖이 "없어도 live를 열 수 있다"는 뜻이 되지 않게, 활성화 체크리스트에 연결 | §4.5 C5(실전 사람 카드 단계 앞), §4.2 `MANDATE_PLAN_PROPOSAL_ACCOUNT_MODES`, §9, §1.2 |
| §1 3792 편입·신규매수 재개 | 보고서가 아닌 실제 승인 기록 링크 | 머리말의 운영자 결정 목록, §4.4 D4 행, §4.5 `operator_decision_ref`·C7 |
| §1 Q1 — 조항 테이블 | 승인자 문자열이 아니라 인증된 운영자 액션과 승인 기록을 검증 | §3.5, §3.1 `approval_id`, §3.3 I21, §8 T3b |
| §2 R1 — 카나리 1차만 | 활성화 프로필에 회차 상한과 버전, 소비 시 재검증, 기본 1차, 확대는 별도 활성화 기록 | §3.6, §4.1 조건 0·1a, §3.3 I18·I23, §4.5 5·6단계, §4.8 V3·V18, §8 T1·T3b·T4a |
| §2 R2 — retired와 미결 주문·잔고 | 폐기는 추가 권한 정지. 불명·미결 주문이 있으면 예약·예산 유지. 해제 조건을 대사와 보유 귀속까지 상태 전이로. 자동 취소·매도 없음 | §3.7, §3.2 권한 상태·정리 상태, §3.3 I1·I3·I19, §3.4, §5.1 재승인 기각, §1.2 |
| §2 R3 — 바구니·전역 한도 | 전역 누계와 바구니 누계를 각각 정의하고 둘 다 통과. lock·예약의 순서·원자성·롤백·uncertain 보존을 acceptance로. 업종 35%의 분자·분모, 조항 불일치 처리, 편입액 산입 예제 | §4.6(두 누계·lock 순서·예약·acceptance), §3.8(분자·분모·예제), §3.1(조항 불일치), §3.3 I20, §2.3 F1~F5 |
| §2 R4 — 모의가 검증하지 않는 완화 | 전송 없는 fixture·단위·통합·경쟁·fault-injection 검증을 T5 필수 산출물로. 미승인 계좌·시장·회차, 위조·오래된 결속, stale 호가, 낙폭 unknown, 중복 콜백, 취소·대사 지연, 알림 실패, 스위치 off 회귀 | §4.8 V1~V22, §4.5 3단계·C4, §8 T5 머지 조건 |
| §2 R5 — 강등과 차단 | 실패별로 사람이 승인할 수 있는 것과 복구·재제안이 필요한 것을 한 표로. 카드가 예외의 수치와 사유를 보여 준다 | §4.7, §4.1 머리말과 조건별 분류, §4.3 (나), §3.3 I8·I9·I15·I16, §6 |
| §3 표 1행 — 완화 넷 | deviation에 네 항목을 번호 붙여 넣고 활성화 승인과 연결 | §4.4 D1~D4, §4.5 `relaxations`·`effective_at`·C12a·C12 |
| §3 표 2행 — 가격·수량 유효값 검사 | 독립 행으로. 유한 양수·호가 신선도, Toss 현재가 fallback과 ask 대체 금지의 차이 | §4.4 "가격·수량 유효값 검사"·"Toss `current_price` 폴백" 두 행, §4.1 조건 6, §2.3 F6·F7, §4.8 V6 |
| §3 표 3행 — 파킹·cash-funding·손실매도 | 전용 테스트 필수 | §4.4 "완화하지 않는 것", §4.8 V15 |
| §3 표 4행 — 캡·태그·세션·sleeve | R3의 상태 있는 누계 증거가 있어야 유지 주장 가능 | §4.4 "완화하지 않는 것", §4.6 acceptance, §4.8 V16 |
| §3 표 5행 — 신규 매수 off 정책 | §9의 별도 티켓과 활성화 차단의 관계, 실제 승인 기록 연결 | §4.4 "D4와 정책의 관계", §4.5 C6·C7, §9 |
| §4 — 손실·실패 범위 | 활성화 승인 화면에 금액, 미결 주문, 취소로 되돌릴 수 없는 부분, 동결 뒤 보유 손실 가능성을 한 번에 | §4.5 "승인 화면 표시 항목"·C13, §3.1 `entry_max_premium_bps`, §8 T9 |
| §5 — T1 테이블 수 | §3.4와 맞추고 I10~I17의 소유 티켓·검증 위치 매핑 | §3.4, §8 T1·"불변식 소유 티켓" 표(I1~I22) |
| §5 — T1m 분리 | CHECK·어휘 변경과 체결 귀속·원장 투영을 두 PR로, 귀속이 먼저 | §8 T1m-a·T1m-b |
| §5 — T4 분리 | 제안 결속+거부 게이트, 체결 투영, 공통 전송 경계 셋으로. 마지막 안전 경계 없는 live 노출 금지 | §8 T4a·T4b·T4c와 Blocking 둘째 항 |
| §5 — T5 선행 | T1m·T3·T4 모의 검증 결과·T9를 선행으로 | §8 T5 선행, Blocking |
| §5 — T8 | 준비되지 않은 상태를 카나리 기본으로 삼지 않고 체크리스트로 연결 | §4.5 C8, §8 Blocking |
| §5 — frozen 스텁 | fixture 통과와 실제 mock 통과를 구분, 4900 구현 뒤 통합 완료 | §5.4 "스텁과 실제 판정", §8 Blocking 첫 항, §4.5 C3 |
| §5 — 4900 | 설계 문서 머지가 아니라 판정 구현+테스트 머지가 선행 | §5.4, §8 T5 선행, §4.5 C2 |
| §5 — T2 문구 | "기본 dry-run, 명시적 --commit 때만 쓰기" | §8 T2(T4b 투영 CLI도 같은 문구) |
| §6 Q1~Q13 | 운영자 답 반영 | §7 표(답별 반영 절) |
| §6 Q4 조건 | 개장 전·후와 휴장일 효력 세션 예제 | §5.1 세션 예제 표 |
| §6 Q7 조건 | 계좌 전체 영향과 대체 보호를 분리해 보인다 | §5.2 "Q7 영향 표", §7.2, §4.5 C10 |
| §6 Q8 조건 | 실행 누락 시 stale 동결, 측정 시각·실행 담당 | §5.4, §1.3 |
| §6 Q10 조건 | 5초 ask·spread·정수 호가 반올림·venue별 측정 증거 | §7.1 E1~E8, §4.5 C9 |
| 한계 [요청] 1 — 3792 승인 링크 | 보고서의 요청과 확정 기록을 구분 | 머리말, §4.5 C7 |
| 한계 [요청] 2 — 누계·예약·해제 계약과 검증 사례 | 서로 다른 sleeve 동시 주문·uncertain에서 한도·중복 방지를 입증할 계약 | §4.6, §3.7, §4.8 V16 |
| 한계 [요청] 3 — 전송 없는 검증 결과 | 키움 mock이 증명하지 않는 완화 경계 | §4.8(산출물 정의). 결과 자체는 T5 PR이 낸다 — 이 문서는 무엇을 내야 하는지만 정한다 |

PR #2466 리뷰 1차(mergewake, head `4c8debf`) 지적의 반영 위치:

| 지적 | 요지 | 반영 위치 |
|---|---|---|
| 1 (high) | 사람 승인 전송에 독립 커밋되는 회차 전송 표시가 없다 | §4.6 "fence 트랜잭션"과 "전송 뒤의 fence"(자동·사람 공통), §3.2 `tranche_send_fenced`, §3.3 I24, §8 T4c |
| 2 (high) | Telegram 콜백은 발신자가 운영자인지 인증하지 않는다 | §3.5 [사실] 정정과 "운영자 principal 대조", §5.1, I21, §8 T3a·T3b |
| 3 (high) | 사람 승인 경로의 권한 확인과 전송이 계획 이벤트 writer와 직렬화되지 않는다 | §4.6 "fence가 권한이다"(1차·3차 반영은 lock 보유 구간으로 풀려 했고, 4차에서 fence INSERT를 이벤트 스트림의 순서와 트리거로 묶었다), §4.3 (나), acceptance의 "fence와 전이의 순서" |
| 4 (high) | 전역 누계가 `broker_account_id` 키로 나뉘어 한도를 넘을 수 있다 | §4.6 "전역 누계는 같은 계좌의 모든 자동승인이 같이 읽는다"와 "계좌 결속"(3차에서 다시 고쳤다), lock L4a, acceptance "계좌 키" |
| 5 (high) | A3는 신규 lot의 기계식 손절 제외를 보장하지 않는다 | §2.3 F9b, §5.2(A3 철회, A1·A2와 검증), §7.2, §4.5 C10 |
| 6 (medium) | 1차 부분 보유가 있는 `approved`에서 논지 훼손을 기록할 수 없다 | §3.2 상태 전이와 `thesis_broken`·`closed` 설명, §5.2 |
| 7 (medium) | 순환소수 기준가를 정확히 저장할 수 없다 | §3.2 "회차 기준값"(금액·수량 저장, 나눗셈 없는 비교)과 고정 예제, I5 |
| 8 (medium) | 손실 매도의 `thesis_broken` 결속이 수동 경로에서 강제되지 않는다 | §3.3 I14, §5.2의 손실 매도 가드, §4.7, §8 T4c(1차 반영 때는 T8에 두었고 2차에서 T4c로 옮겼다) |
| 9 (medium) | `zero_holdings`가 모의 계획의 실제 계좌를 보지 않는다 | §3.7 "보유 범위" 표, §3.2 `adopted_amount`, §3.8 |
| 10 (medium) | 4주의 시작이 실제 활성 시각이 아니고 휴장일 매핑이 없다 | §5.2 "4주의 시작 시점", §7.2 |
| 11 (low) | N과 스프레드의 설명이 틀렸다 | §7.1 E3 |

PR #2466 리뷰 2차(head `efb002e`) 지적의 반영 위치:

| 지적 | 요지 | 반영 위치 |
|---|---|---|
| 1 (high) | 손실 매도 가드가 생성 시점만 본다. 계획 등록 전에 만든 `loss_cut` 카드가 계획이 생긴 뒤 이벤트 없이 나간다 | §5.2 가드의 "판정 지점 셋"·"계획 등록과의 직렬화", I14, §4.7 `mandate_thesis_not_broken`·`mandate_open_loss_cut` |
| 2 (major) | C8이 가드 없는 임시 절차로 실전을 열 수 있게 한다 | 가드를 T8에서 T4c로 옮김(C1에 포함), §4.5 C8, §8 T4c·T8·Blocking |
| 3 (major) | 가드가 제안의 계좌로만 계획을 찾는다 | §5.2 "무엇을 보나"(계좌 무관한 시장·종목 예약), I14, 테스트 계약 (d) |
| 4 (medium) | 최초 회고가 72시간 뒤 만료되면 손실 매도를 다시 시작할 수 없다 | §5.2 "72시간이 지난 뒤", §3.2 `thesis_evidence_refreshed`·`thesis_seq`, 테스트 계약 (f) |

2차 반영에서 함께 고친 것: 종목 예약의 키에 영역(`live`/`mock`)을 넣었다(§3.2, I1, L1). 모의 계획이 같은 종목의 실계좌 계획을 막던 것을 없애고, 손실 매도 가드가 실계좌 영역의 예약만 보게 하기 위해서다.

PR #2466 리뷰 3차(head `5cb0f39`) 지적과, 3차에서 나온 자문(hk doc `reports/consult-5272-ccb01706/sha256/f4a85d3a8595d10abb0b34a5a1cf7ba2cffbba2d03fda5a7cc56b0b434356048.md`)이 고치라고 한 열린 스레드의 반영 위치. 자문은 뿌리 원인을 둘로 봤다: 승인된 계획이 실제 주문 계좌에 끝까지 결속돼 있지 않은 것, 권한의 마지막 확인과 실제 송신이 같은 동시성 계약 안에 있지 않은 것. 자문은 의견이고 채택은 이 문서의 판단이다.

| 지적 | 요지 | 반영 위치 |
|---|---|---|
| 3차 1 (high) | 예약만 영역으로 나뉘고 바구니 집계는 모의와 실계좌를 합친다 | §1.4·§3.1 바구니 정체성 (영역, `basket_key`), §3.8 혼합 영역 예제, L2 키, I22, §4.8 V21 |
| 3차 2 (medium) | 증거 갱신이 `thesis_broken` 상태에서만 된다. 폐기된 미해제 계획은 72시간 뒤 막힌다 | §3.2 `thesis_evidence_refreshed`, §5.2 "72시간이 지난 뒤", 테스트 계약 (h) |
| 3차 3 (medium) | 임시 절차가 이벤트를 회고보다 먼저 쓰라고 한다 | §5.2 "증거의 순서", §4.5 C8, 테스트 계약 (i), §8 T8 |
| 1차 4 재론 (high) | 계획·제안·실제 브로커 계좌의 결속, 모든 자동 writer의 공통 G | §4.6 "계좌 결속"·"전역 누계는 같은 계좌의 모든 자동승인이 같이 읽는다", §3.1·§3.2 `account_ref`, I15, V20, C14 |
| 1차 3 재론 (high) | 사람 경로의 lock이 fence 커밋에서 풀려 그 뒤 폐기가 끼어들 수 있다 | §4.6 "fence가 권한이다"와 fence 트랜잭션(3차 반영은 호출자가 lock을 브로커 호출 반환까지 쥐는 방식이었고, 4차에서 권한의 단위를 커밋된 fence로 바꿨다), I16·I24, §4.3 (나) |
| 2차 1 재론 (high) | 손실 매도의 마지막 판정과 호출이 계획 등록과 직렬화되지 않는다 | §5.2 "판정 지점 셋"·"계획 등록과의 직렬화"(생성과 등록이 종목 lock 아래 짧은 트랜잭션에서 서로의 커밋된 행을 본다. 3차 반영의 "전송까지 lock 보유"는 4차에서 뺐다), 테스트 계약 (j) |
| CodeRabbit (minor) | fence 뒤 브로커 호출 전에 동결이 걸리는 경우가 정의되지 않았다 | I11, §5.4(동결은 그 뒤에 커밋되는 fence만 막는다 — fence 뒤의 동결은 그 fence의 POST 한 번을 막지 못할 수 있음을 명시), §4.6, V19 |
| CodeRabbit (minor) | 예약 lock 키의 `\|\|`가 표의 칸을 나눈다 | §3.2 "활성 종목 예약" 행(키를 연산자 없이 적음) |

PR #2466 리뷰 4차(head `3bedbc2`) 지적의 반영 위치. 설계 세션의 방향(hk 5272 댓글 26044)을 따랐다: 1~3차의 수정이 advisory lock의 보유 구간을 브로커 호출 쪽으로 늘리는 방식이라 늘릴 때마다 새 경합(연결 상실, 스위치, 계좌 교체)이 열렸다. lock을 권한의 근거로 쓰지 않고 커밋된 fence를 권한의 단위로 삼는다. 새 메커니즘은 더하지 않았다(3차에서 넣은 "호출자가 lock을 브로커 호출 반환까지 쥔다", 사람 승인의 try-lock, 손실 매도의 전송 중 종목 lock을 뺐다).

| 지적 | 요지 | 반영 위치 |
|---|---|---|
| 1 (high) | DB 연결이 끊기면 lock이 브로커 호출 전에 풀려, 폐기·동결이 커밋된 뒤에 전송이 시작될 수 있다 | §4.6 "fence가 권한이다"·"fence 트랜잭션"·"POST 직전 재확인은 best-effort다", I11·I16·I24를 fence 형태로 다시 씀, §4.3 (나), §4.7, §5.4, V22(fence 커밋 → 세션 종료 → `retired` 커밋 → POST → 예약·예산 유지) |
| 2 (high) | 스위치를 끄면 당일 fence가 일반 자동승인의 전역 누계에서 빠진다 | §4.6 "전역 누계 reader의 규칙"(당일 자동 fence는 스위치와 무관하게 항상), §4.5 `rollback`, V12(당일 fence가 없는 날로 한정) |
| 3 (high) | 보유 범위의 상대 계좌가 등록 때 실제 계좌에 결속돼 있지 않다 | §3.2 `scope_account_refs`·`adopted_evidence`, §3.7 "보유 범위의 계좌는 등록 때 고정한다", I19 |

원래 문서에서 바뀐 것 중 리뷰 항목에 직접 대응하지 않는 것:

- `mandate_terms`의 `approved_by`·`approved_at` 열을 없애고 `approval_id`로 바꿨다(§3.1, Q1의 조건).
- `loss_sell_mode`의 `auto` 값을 없앴다(§3.1, Q5 = B).
- `mandate_plans`에 `adopted_amount`·`adopted_evidence`를 더하고 회차 금액을 신규 매수액 기준으로 바꿨다(§3.2, §3.8).
- I4의 기준을 "바구니 평가액 + 계획 잔여"에서 계획 금액 기준으로 바꿨다(§3.8).
- I16의 위반 결과를 사람 카드에서 차단으로 바꿨다(조항만 낡은 경우는 강등으로 남겼다, §4.7).
- 계획 상태 `suspended`로 가는 시스템 전이(위임장 비활성·동결)를 없앴다. 둘은 계획 상태가 아니라 판정이다(§3.2). `closed`는 손실 매도 체결이 아니라 보유 0 확인으로 쓴다.
- 계획 결속 제안이 기존 `expanded` 분류기의 일반 경로로 자동승인되는 길을 막는 조건 0과 I23을 더했다(§4.1). 원래 문서에는 없던 거부다.
- D1(`allow_mandate_bound_buy`)이 서는 조건을 적었다: 스위치 뒤, 승인 방식과 무관(§4.4 "D1의 범위").
- 실전 활성화를 사람 카드 단계와 자동승인 단계로 나누고 계획 결속 제안 생성 계좌 설정을 더했다(§4.2, §4.5).
- 티켓 T3·T1m·T4를 나누고 T4b(매도 1호가 소스)를 T4d로 옮겼다(§8).
- 새로 인용한 레포 사실은 §2.3에 `auto_trader@ac4f9ed5c`로 모았다.
- 자동승인 누계용 "예약" 이벤트를 모든 계획 결속 전송에 쓰는 "전송 fence"로 바꿨다(§4.6).
