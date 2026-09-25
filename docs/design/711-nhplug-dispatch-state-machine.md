# #711 — NHPLUG 모의계좌 주문 송신 경로 상태 기계 (설계)

- 상태: **설계 문서 r4 — 코드 변경 0.** 재구현은 이 문서가 승인된 뒤 별도 PR 에서 한다.
- 입력: 운영자 결정 A(2026-09-26 — PR #2100 은 열린 채 보류, 송신 경로 설계를 먼저 쓴다),
  운영자 결정 `decision/2026-09-25/nhplug-stage2-mock`(모의계좌 한정 · 지정가 주문·정정·취소 · 레저·reconcile ·
  시장가/실계좌/스케줄러/배정 제외), 독립 tester 보고 r1–r4 의 BLOCKER 13건(`herdr-inbox/jobs/
  711-nhplug-stage2-20260925-1235/tester-report-r{1,2,3,4}.md`), 이 문서의 독립 리뷰 r1(`design-review-r1.md`,
  BLOCKER 5 · MAJOR 3), r2(`design-review-r2.md`, BLOCKER 4 · MAJOR 4), r3(`design-review-r3.md`, BLOCKER 4 · MAJOR 1).
- 개정 r2(리뷰 r1 + director-1 결정): B1 lease·fence·프로세스 소멸 증명 없이는 재주문 차단 해제 금지 · B2 인바운드
  멱등키 + DB 예약 + 운영자 전용 두 번째 주문 · B3 송신 시작 가능 이후 예외는 전부 `uncertain`(접수번호는 증거로만) ·
  B4 "주문 없음 증명" 코드 목록은 비어 있음(벤더 문서 인용) · B5 속성 일치는 운영자 확인 후보 · M6 자기 전이 표 ·
  M7 INSERT 트리거·`IS DISTINCT FROM` · M8 버전·길이 구분 digest + 골든 벡터.
- 개정 r3(리뷰 r2, §6.3): R1 HMAC 키 회전에도 변하지 않는 계좌 식별자 `account_ref`(추가 전용 계좌 등록부, 모든 키
  버전 보존) · R2 첫 요청 바이트 쓰기 **직전**의 동기 게이트(await 없음) + lease 호스트에서의 기계 검증 프로세스 소멸
  증명(원격 증언 분기 삭제) · R3 `sending` 에서 나가는 모든 전이가 lease 를 원자적으로 닫음(별도 취소 전이 불필요) ·
  R4 멱등·중복 컬럼 NOT NULL + T1/T15 트리거 규칙 · R5 늦은 결과의 접수번호를 `uncertain` 에 한 번 기록하는 T8e ·
  R6 단계별 타임아웃 합을 총 상한으로 쓰지 않음 — 종단 기한 + 안전성은 "첫 쓰기 기한 < lease 만료" 부등식에만 의존 ·
  R7 같은 키 재시도가 아직 `intent` 인 행을 이어받아 claim · R8 운영자 전이(T9h·T14·T15)는 별도 DB 역할만 넣을 수 있는
  1회용 운영자 승인 행을 요구.
- 개정 r4(리뷰 r3 지적만, director-1 방향, §6.4): Q1 fence 이후 정리·기록까지 하나의 예외 경계 · Q2 T6/T9a 는 엔드포인트별
  성공 증명 코드가 있어야만 — 벤더가 문서화한 코드가 없어 목록이 비어 있으므로 번호가 있어도 `uncertain`(번호는 증거) ·
  Q3 `accepted` 계열 상태는 양수이고 같은 `broker_order_id`·`ack_order_id` 를 NULL 이 통과할 수 없는 CHECK 로 요구(§5.5 의
  잘못된 주장 정정) · Q4 계좌 최초 등록 시 DB 에 등록된 모든 보존 키 버전의 바인딩을 한 트랜잭션에서 삽입 · Q5 T9h 는
  dispatcher 가 송신 단계를 떠났음이 증명된 뒤에만.
- 기준 코드: 보류 중인 PR #2100 head `b945ccc76`(브랜치 `task711-nhplug-stage2`). `file:line` 은 이 커밋에서 읽었다.
- 벤더 사실(공개 krstock OpenAPI "API명세서 260911" · 벤더 SDK `PLUG-OpenAPI/nhplug-sdk` `572a908` 소스, 실호출 0):
  주문 본문(`cashBuy`/`cashSell`/`modify`/`cancel` 의 `Input_0`)에 **클라이언트 주문 ID·멱등키 필드가 없다**.
  접수 증거는 `Output_0.mkt_orr_no` 뿐이고 응답 블록은 데이터가 있을 때만 내려온다. 명세의 `x-response-status` 는
  "HTTP 200 이어도 업무 오류일 수 있다 … `rsp_cd` 를 단일 코드와 비교해 판정하지 마세요 — 정상 응답코드는 한 가지가
  아니며 API 마다 다릅니다" 라고 쓰고, 네 주문 엔드포인트는 `200` 응답(`Output_0` + `message`)만 문서화할 뿐 "주문이
  생성되지 않았음"을 뜻하는 코드를 하나도 정의하지 않는다. SDK `nhplug/client.py` 머리말도 "같은 rsp_cd 값이 API 마다
  정상일 수도 오류일 수도 있다"며 판정을 하지 않는다.
- 범위: 하드룰 14 의 Stage 2 범위 그대로. 실계좌·시장가·스케줄러·계좌 배정은 범위 밖이며 어떤 게이트도 완화하지 않는다.

## 0. 요약 — 재구현이 지켜야 할 규칙

| 규칙 | 내용 |
|---|---|
| **S1 상태 기계** | 행은 `intent → claimed → sending → {accepted, uncertain} → reconciled` 만 지난다. `rejected`(T7)와 응답만으로의 `accepted`(T6·T9a)는 §4.3 의 두 목록이 비어 있어 현재 도달 불가이며, `accepted` 는 reconcile 의 자기 번호 증명(T9b) 또는 운영자 확인(T9h)으로만 된다. 합법 전이는 §2.3·§2.4 표가 전부이며 DB 트리거가 강제한다. |
| **S2 본문 = claim 행** | 본문을 정하는 모든 필드는 intent 행에 고정되고, 본문은 claim 된 행에서 순수 함수로만 만든다. 송신 API 는 수량·가격·범위·계좌를 받지 않는다. DB 생성 컬럼 `body_digest`(§3.3 인코딩·골든 벡터)가 intent 필드·claim 조건·전송 바이트를 묶는다. |
| **S3 송신 시작 가능 이후 = uncertain** | `sending` fence 커밋 이후의 모든 예외·비정상 결과(연결 종료·`aclose()`·취소·프로세스 사망·부정 응답·성공 증명 코드 없는 응답 포함)는 `uncertain` 이다. 정리(소켓 종료) 실패도 같은 경계 안이다. 읽은 접수번호는 증거 컬럼에만 남고, 늦게 읽혀도 한 번 기록된다(T8e). `withdrawn` 은 fence **이전**에만 가능하다. |
| **S4 단일 사용 claim** | claim 은 조건부 UPDATE 하나로만 얻고 즉시 커밋한다. 같은 행의 재생·새 요청 ID·두 번째 클라이언트·동시 호출·다른 digest·다른 계좌는 0행이다. 토큰·lease 신원은 되돌릴 수 없다. |
| **S5 요청 단위 멱등** | 호출자가 준 멱등키로 같은 요청은 같은 행(또는 미송신이 증명된 뒤의 다음 시도)으로 수렴한다. 멱등·예약·중복 UNIQUE 는 키 회전에도 변하지 않는 `account_ref` 위에 걸리고 관련 컬럼은 전부 NOT NULL 이다. 같은 본문의 같은 날 재주문은 운영자 승인 행이 있어야만 만든다. |
| **S6 부재는 증거가 아니다** | 빈 배열·블록 누락·13578·오류형·미완료 페이지는 "미체결 없음"이 아니다. 종결 상태는 양성 증거 두 개로만 기록한다. 속성만 같은 목록 행은 후보일 뿐이다. |
| **S7 첫 바이트 기한과 차단 해제** | fence 뒤 요청의 첫 바이트는 로컬 기한(`t0 + D_START`) 안에서만, await 없는 동기 검사 직후에만 쓸 수 있고, `D_START` 는 DB lease 만료보다 충분히 앞선다. 송신 단계에 들어간 행의 재주문 차단은 lease 호스트에서 기계로 검증한 프로세스 소멸 + 유예 + 운영자 승인 행이 모두 있어야 풀린다(§4.6). |
| **S8 DB 가 마지막 벽** | INSERT·UPDATE 트리거와 CHECK·NOT NULL·부분 UNIQUE·DB 역할 권한이 전이·불변 필드·증거 필드·예약·운영자 전이를 강제한다. 서비스 계층(하드룰 5)은 그대로이고 DB 는 두 번째 벽이다. |

---

## 1. 배경

PR #2100 은 독립 검증 4회에서 BLOCKER 13건을 받았고(§6.1), 이 문서는 리뷰 r1(BLOCKER 5·MAJOR 3, §6.2)과
r2(BLOCKER 4·MAJOR 4, §6.3)를 거쳤다. 공통 원인은 한 가지다 — **벤더에 멱등키가 없는데, 송신 경로의 안전 성질이
코드 위치마다 따로 조립돼 있었다.** 대표 사례(모두 `b945ccc76`):

- 정정 전량/일부가 claim 밖의 호출자 인자였다: `client.py:666` `full_quantity`, `_body_from_claim`(`client.py:368`)이
  본문에 쓰고 claim(`ledger_service.py:136`)·전송 바이트 검사(`client.py:425`)는 보지 않는다.
- HTTP 클라이언트 컨텍스트 종료(`client.py:786` `async with`)가 `send`(`client.py:831`) 주변 `try` 바깥이라, POST 뒤
  `aclose()` 오류가 operations 의 "그 밖의 예외 = 송신 전"(`operations.py:467`)으로 분류돼 `not_submitted` 가 된다.

---

## 2. 상태 기계 (요구 1)

### 2.1 상태

| 상태 | 뜻 | 송신 여부 | 예약 보유(§5.2) | 종결 |
|---|---|---|---|---|
| `intent` | 본문 전체 + 멱등키가 커밋된 행. claim 없음 | 미송신 확정 | 예 | 아니오 |
| `withdrawn` | fence 이전 거부로 닫힘 | 미송신 확정 | 아니오 | 예 |
| `claimed` | 단일 사용 claim + lease 신원 커밋. 본문 고정, 송신 전 검사 중 | 미송신 확정 | 예 | 아니오 |
| `sending` | fence 커밋. 이후 첫 바이트가 나갈 수 있음 | 알 수 없음 | 예 | 아니오 |
| `accepted` | 송신 절차가 예외 없이 끝났고 성공 증명 코드(§4.3, 현재 없음) + `mkt_orr_no`, 또는 §4.5 증명 | 접수 | 아니오 | 아니오 |
| `rejected` | §4.3 "주문 없음 증명" 코드일 때만 — **목록이 비어 있어 현재 도달 불가** | 거부 증명 | 아니오 | 예 |
| `uncertain` | fence 이후 그 밖의 모든 결과 | 알 수 없음 | 예 | 아니오 |
| `open` / `partially_filled` | reconcile 이 양성 두 소스로 확인한 미체결 | 접수 | 아니오 | 아니오 |
| `filled` / `cancelled` / `modified` | reconcile 종결(§4.4) | 접수 | 아니오 | 예 |
| `confirmed` | 취소·정정 **요청 행**의 반영 확인 | 접수 | 아니오 | 예 |
| `anomaly` | 증거 모순. 수동 검토 | — | 아니오 | 예 |
| `abandoned` | 해소 불가 uncertain 을 §4.6 조건 충족 후 운영자가 닫음 | 미확정 | 아니오 | 예 |

b945ccc 이름 대응: `intent=submitting`, `withdrawn=not_submitted`, `claimed·sending=dispatching`,
`uncertain=acceptance_uncertain`.

### 2.2 주체

| 주체 | 설명 | 허용 |
|---|---|---|
| **O** operations | MCP 도구·스모크 CLI 의 조정 계층 | T1, T2 |
| **D** dispatcher | claim·lease 보유자(송신 경로). 같은 멱등키로 재시도한 호출자도 `intent` 행에 대해 D 가 될 수 있다(§5.2) | T3–T8, T8e, T9a, T13d |
| **R** reconcile | 목록 증거로만 판정. 수동 실행만(스케줄러 금지) | T9b, T11–T13, 자기 전이(후보 기록) |
| **V** recovery | reconcile 실행 시작부의 시간 기반 정리. 별도 스케줄 없음 | T10, T10i, T8v |
| **H** operator | 운영자 CLI. 각 전이는 운영자 DB 역할만 넣을 수 있는 1회용 승인 행(§5.8)을 소비한다. MCP 로는 불가 | T9h, T14, T15 |

### 2.3 상태 전이 — 이 표가 전부다

| # | 전이 | 주체 | 선행 조건(조건부 UPDATE/INSERT 에 포함) | 기록 |
|---|---|---|---|---|
| T1 | ∅ → `intent` | O | 게이트·검증(§5.1) 통과. 계좌 해석(§3.4). INSERT 가 멱등키·예약·중복 UNIQUE 를 통과(§5.2). 트리거: `duplicate_ordinal=0 AND duplicate_of IS NULL AND second_order_authorization_id IS NULL` | 본문 필드, `account_ref`, `idempotency_key`, `attempt_no`, 새 `client_request_id`(DB 가 `body_digest` 생성) — 커밋 후 `(row_id, request_id, body_digest)` 반환 |
| T2 | `intent → withdrawn` | O | `state='intent' AND claim_token IS NULL` | `withdraw_reason` |
| T3 | `intent → claimed` | D | §5.3 claim SQL | `claim_token`, `claimed_at`, `claim_deadline`, lease 신원(§4.6) |
| T4 | `claimed → withdrawn` | D | `state='claimed' AND claim_token=:t AND sending_at IS NULL` + 타입 `PreSendRefusal` | `withdraw_reason` |
| T5 | `claimed → sending` (**fence**) | D | §5.4 fence SQL(`state='claimed'`, 토큰, `now() < claim_deadline`) | `sending_at`, `lease_expires_at` — 단독 커밋, 그 뒤 첫 쓰기 게이트(§4.1) |
| T6 | `sending → accepted` | D | `state='sending' AND claim_token=:t` + 송신 절차(정리 포함)가 예외 없이 끝남 + §4.3 성공 술어(HTTP 200 · `rsp_cd ∈ SUCCESS_PROOF_CODES[path]` · `mkt_orr_no>0`) — **목록이 비어 현재 도달 불가**. 트리거: `rsp_cd` 가 DB 표 `review.nhplug_success_proof_code(path, rsp_cd)` 에 있어야 함 | `broker_order_id`=`ack_order_id`, `ack_source='response'`, `success_rsp_cd`, `lease_closed_at`, `dispatcher_done_at` |
| T7 | `sending → rejected` | D | 위 + 응답 코드가 해당 엔드포인트의 "주문 없음 증명" 목록(§4.3, 현재 빈 집합)에 있음 | 응답 코드, `lease_closed_at` |
| T8 | `sending → uncertain` | D | `state='sending' AND claim_token=:t`, 그 밖의 모든 결과·예외(번호가 있어도 성공 증명 코드가 없으면 여기) | `uncertain_reason`, 읽었으면 `ack_evidence_order_id`, `lease_closed_at`, `dispatcher_done_at` |
| T8v | `sending → uncertain` | V | `state='sending' AND lease_expires_at < now()` | `uncertain_reason='lease_expired_without_result'`, `lease_closed_at` (= lease 취소) |
| T8e | `uncertain → uncertain` | D | `claim_token=:t AND ack_evidence_order_id IS NULL`(T8v 가 먼저 닫은 뒤 D 가 늦게 읽은 번호) | `ack_evidence_order_id`, `late_result_at`, `dispatcher_done_at` — 한 번만 |
| T9a | `uncertain → accepted` | D | `claim_token=:t AND dispatcher_done_at IS NULL` + T6 성공 술어를 만족하는 늦은 결과(**현재 도달 불가**) + (`ack_evidence_order_id` 가 NULL 이거나 같은 번호) | `broker_order_id`=`ack_order_id`, `ack_source='response'`, `success_rsp_cd`, `dispatcher_done_at` |
| T9b | `uncertain → accepted` | R | §4.5(1) 자기 접수번호 증명: `ack_evidence_order_id` 와 **같은 번호**의 완전한 전체 조회 행 + 속성 일치. 트리거: `NEW.broker_order_id = NEW.ack_order_id = OLD.ack_evidence_order_id AND NEW.ack_source='own_evidence' AND NEW.reconcile_state='verified'` | `broker_order_id`=`ack_order_id`, `ack_source`, 증거 JSON |
| T9h | `uncertain → accepted` | H | §4.5(2) 운영자가 후보를 확인 + **dispatcher 가 송신 단계를 떠났음이 증명됨**(§4.5(2): `dispatcher_done_at IS NOT NULL` 또는 승인 행의 `dispatcher_gone_proof=true`). 트리거: 종류 `bind_candidate` 승인 행이 이 행·이 번호와 일치하고 미소비, 번호 ∈ `candidate_order_ids`, `OLD.ack_evidence_order_id` 가 NULL 이거나 같은 번호, 위 조건 | `broker_order_id`=`ack_order_id`, `ack_source='operator'`, `resolution_authorization_id` |
| T10 | `claimed → withdrawn` | V | `claim_deadline < now() AND sending_at IS NULL` | `withdraw_reason='claim_deadline_passed'` |
| T10i | `intent → withdrawn` | V | `claim_token IS NULL AND created_at < now() − T_intent_stale` | `withdraw_reason='stale_intent'` |
| T11 | `accepted/open/partially_filled → open/partially_filled/filled/cancelled/modified` | R | §4.4 양성 두 소스 | 증거 JSON, 수량 |
| T12 | 취소·정정 요청 행 `accepted → confirmed` | R | 자기 `ack_order_id` + 원주문 목록 행의 반영 수량 | 증거 JSON |
| T13 | `accepted/open/partially_filled/uncertain → anomaly` | R | 모순 증거(심볼 불일치, 같은 번호·다른 속성 등). `intent`·`claimed`·`sending` 에서는 불가. `uncertain` 에서는 `ack_evidence_order_id` 와 같은 번호의 목록 행이 있을 때만(주문 존재의 양성 증거) | 수동 검토 |
| T13d | `uncertain → anomaly` | D | `claim_token=:t AND dispatcher_done_at IS NULL` + 늦게 읽은 번호가 기록된 `ack_evidence_order_id` 와 다름 | `conflicting_order_id`, `dispatcher_done_at`, 수동 검토 |
| T14 | `uncertain → abandoned` | H | §4.6 차단 해제 조건 전부 + 종류 `abandon` 승인 행 | `resolution_authorization_id`, 사유 |

`T15`(운영자 전용 두 번째 주문)는 새 행의 T1 변형이다(§5.2·§5.8: `duplicate_of`, 양의 `duplicate_ordinal`, 종류
`second_order` 승인 행을 가리키는 `second_order_authorization_id`).

**lease 닫기(r3).** `sending` 에서 나가는 모든 전이(T6·T7·T8·T8v)는 같은 UPDATE 에서 `lease_closed_at = now()` 를 쓴다.
D 의 T6/T8 은 자기 lease 반납, V 의 T8v 는 취소다. 별도 취소 전이는 없고, CHECK 가
`state NOT IN ('intent','claimed','sending','withdrawn') ⇒ lease_closed_at IS NOT NULL` 을 강제한다(§5.5). lease 가 닫혀도 같은
토큰의 결과 **기록**(T8e·T9a·T13d)은 막지 않는다 — lease 는 송신을 막는 장치이고, 기록은 증거를 보존한다.

**dispatcher 완료 표시(r4).** D 의 결과 기록(T6·T7·T8·T8e·T9a·T13d)은 한 디스패치의 **마지막** 쓰기이고, 같은 UPDATE 에서
`dispatcher_done_at = now()` 를 쓴다(NULL→값 한 번). 그 뒤 D 는 이 행에 더 쓰지 않는다. T8v 는 이 값을 쓰지 않는다 —
V 는 D 가 아직 결과를 들고 있을 수 있음을 모르기 때문이다. T8e·T9a·T13d 는 `dispatcher_done_at IS NULL` 을 조건으로 한다.

### 2.4 같은 상태 자기 전이(M6)

트리거는 `(old.state, new.state)` 가 §2.3 에 없으면 거부하되, 아래 자기 전이는 **표에 적힌 컬럼만** 바뀔 때 허용한다.

| 상태 | 바뀔 수 있는 컬럼 | 조건 |
|---|---|---|
| `intent`, `claimed`, `sending` | 없음 | — |
| `uncertain` | (a) `candidate_order_ids`, `requires_manual_review`, `manual_review_reason`, `last_reconcile`, `reconcile_state`(verified 제외) — R 또는 H · (b) T8e: `ack_evidence_order_id`(NULL→값 한 번), `late_result_at`(NULL→값 한 번), `dispatcher_done_at`(NULL→값 한 번) — D, 또는 번호 없는 늦은 결과면 `dispatcher_done_at` 만 | (b) 는 OLD 값이 NULL 일 때만 |
| `accepted`, `open`, `partially_filled` | `reconcile_state`, `last_reconcile`, `requires_manual_review`, `manual_review_reason`; 증거·수량 컬럼(`filled_qty`·`avg_fill_price`·`open_qty`·`cancelled_qty`·`evidence`)은 `NEW.reconcile_state='verified'` 일 때만 | R |
| 종결 상태 | 없음(`anomaly` 의 `manual_review_reason` 추가 기록만 H 허용) | — |

**금지(트리거 거부, 예시):** 표 밖의 모든 (old,new) 쌍, `sending/uncertain/accepted/… → withdrawn`, `→ intent`,
`intent → sending`, `intent → accepted`, `abandoned → *`, 종결 → 비종결, 값이 있는 `ack_evidence_order_id` 변경.

### 2.5 그림

```
   T1(O) / T15(H)          T3(D)                 T5(D, fence)
 ∅ ─────────────▶ intent ─────────▶ claimed ─────────────────▶ sending
                   │ T2(O)            │ T4(D) / T10(V)            │ T6(D)           T8(D) / T8v(V)
                   │ T10i(V)          ▼                           ▼                     ▼            ⟲ T8e(D)
                   └─────────────▶ withdrawn                  accepted ◀─T9a/T9b/T9h── uncertain ──T14(H)──▶ abandoned
                                                                 │ T11/T12(R)             │ T13d(D)
                                                                 ▼                        ▼
                                   open · partially_filled · filled · cancelled · modified · confirmed · anomaly(T13)
```

### 2.6 프로세스 사망과 늦게 깨어난 dispatcher (B1·R2)

| 멈춘/죽은 위치 | 남는 상태 | 이후 | 늦게 깨어나면 |
|---|---|---|---|
| T3 전 | `intent` | 같은 키 재시도가 이어받거나(§5.2) V: T10i | claim 조건 `state='intent'` 에서 0행 → 송신 0 |
| T3 후 · T5 전 | `claimed` | V: T10(`claim_deadline` 경과) | fence 조건 `state='claimed' AND now()<claim_deadline` 에서 0행 → 송신 0 |
| T5 커밋 결과 불명 | `claimed` 또는 `sending` | 커밋 확인 실패 시 **보내지 않는다**. `sending` 이면 V: T8v | — |
| T5 후 · 첫 쓰기 게이트 전(연결·TLS 대기 포함) | `sending` | V: T8v(lease 만료) | 게이트가 `t0 + D_START` 초과를 보고 **쓰지 않음** → T8/T8e 기록 |
| 게이트 검사 후 · 쓰기 시스템 호출 전(await 없는 몇 개의 바이트코드) | `sending` | V: T8v | 보낼 수 있다 — **그래서** T14 는 그 프로세스가 lease 호스트에서 더 이상 존재하지 않음을 기계로 확인해야 한다(§4.6). 멈춘 프로세스는 존재하므로 T14 가 거부되고 예약이 남는다 |
| 첫 쓰기 후 | `sending` | V: T8v | 결과 기록은 T8e·T9a·T13d 로만 |

---

## 3. 브로커 본문의 claim 행 결속 (요구 2)

### 3.1 필드 표

| 본문을 정하는 것 | 벤더 키(`Input_0`) | 컬럼·타입·제약 | digest 태그 | T3 에서 | 전송 바이트 재검사 |
|---|---|---|---|---|---|
| 조작 종류 | 경로 | `operation_kind` text NOT NULL CHECK `place/modify/cancel` | `op` | digest 일치·RETURNING | 경로 정확 일치 |
| 방향 | 경로(매수/매도) | `side` text NOT NULL CHECK `buy/sell`(정정·취소는 원주문 방향, T1 목록에서) | `side` | 〃 | 경로 |
| 종목 | `iem_cd` | `symbol` text NOT NULL CHECK `^[0-9]{6}$` | `sym` | 〃 | ✔ |
| 수량 | `orr_qty`/`cor_qty` | `quantity` bigint CHECK `>0`; 전량 취소만 NULL(조작별 CHECK) | `qty` | 〃 | ✔ |
| 지정가(KRW 정수) | `orr_pr`/`cor_pr` | `price` bigint CHECK `>0`; 취소만 NULL(조작별 CHECK) | `px` | 〃 | ✔ |
| 원주문번호 | `org_mkt_orr_no` | `original_order_id` text CHECK `^[1-9][0-9]{0,9}$`; `place` 만 NULL(조작별 CHECK) | `org` | 〃 | ✔ |
| 정정·취소 범위 | `all_pat_dit_cd`(1/2) | `amend_scope` text CHECK `full/partial`; `place` 만 NULL; 취소 `full ⇔ quantity IS NULL` | `scope` | 〃 | ✔ |
| 계좌 | `act_no` | `account_ref` uuid NOT NULL FK → 계좌 등록부(§3.4) | `acct` | WHERE 직접 + digest | 바이트의 `act_no` 를 등록부로 해석해 같은 `account_ref` |
| 고정 상수 | `nmn_pr_tp_cd=01`·`orr_cnd_dit_cd=00`·`ssl_nmn_pr_dit_cd=00`·`rmt_mkt_cd=KRX`·`sor_mkt_sli_yn=N`·`sop_cnd_pr=0` | `body_schema_version` smallint NOT NULL CHECK `=1`(버전이 상수 집합을 고정) | 접두 `nhplug-body-v1` | 〃 | 지정가 전용 형태 |
| 요청 ID | (벤더 본문에 없음) | `client_request_id` uuid NOT NULL UNIQUE | ✗ | WHERE 직접 | — |
| 멱등키 | (벤더 본문에 없음) | `idempotency_key` text **NOT NULL** CHECK `^[A-Za-z0-9_-]{16,64}$` | ✗ | — | — |

모든 값이 ASCII 로 제한되므로 바이트 길이 = 문자 길이다.

### 3.2 규칙

1. **순수 생성기.** `build_body(row, verified_act_no) -> (path, input_0)` 는 입출력 없는 순수 함수다. 송신 API 는
   `(ledger, row_id, request_id, intent_digest, authorization)` 만 받는다. 수량·가격·범위·계좌·조작 종류 인자는 없다
   (b945ccc 의 `full_quantity`·`ExpectedOrder` 제거). 정적 가드가 시그니처를 고정한다.
2. **범위는 T1 에서 고정.** `amend_scope` 는 O 가 T1 시점의 완전한 목록에서 계산(`quantity == open_qty ⇒ full`)해 저장하고
   송신 시점에 다시 계산하지 않는다.
3. **정정 결과 번호.** 정정 응답의 새 `mkt_orr_no` 는 정정 행의 `broker_order_id` 다. 원주문 행은 목록 증거로만 `modified`.

### 3.3 digest 인코딩과 골든 벡터(M8)

- 정규 바이트열 v1 = ASCII 접두 `nhplug-body-v1|` 뒤에 태그 순서 `op, side, sym, qty, px, org, scope, acct` 로 각 필드를
  `태그=바이트길이:값;`, NULL 은 `태그=-;` 로 이어 붙인 것. 정수는 부호·앞자리 0·소수점 없는 십진 표기. `acct` 는
  `account_ref` 의 소문자 36자 정규 UUID 표기다(r3 — 키 회전에 불변). 요청 ID·멱등키는 포함하지 않는다.
- `body_digest` = 정규 바이트열의 SHA-256 소문자 hex(64자). DB 에서는 NOT NULL 생성 컬럼이다:

```sql
CREATE FUNCTION review.nhplug_body_field(tag text, v text) RETURNS text
  LANGUAGE sql IMMUTABLE PARALLEL SAFE
  RETURN tag || CASE WHEN v IS NULL THEN '=-;' ELSE '=' || octet_length(v)::text || ':' || v || ';' END;
CREATE FUNCTION review.nhplug_body_digest_v1(op text, side text, sym text, qty bigint, px bigint,
                                             org text, scope text, acct text) RETURNS text
  LANGUAGE sql IMMUTABLE PARALLEL SAFE
  RETURN encode(sha256(convert_to('nhplug-body-v1|'
      || review.nhplug_body_field('op', op)     || review.nhplug_body_field('side', side)
      || review.nhplug_body_field('sym', sym)   || review.nhplug_body_field('qty', qty::text)
      || review.nhplug_body_field('px', px::text) || review.nhplug_body_field('org', org)
      || review.nhplug_body_field('scope', scope) || review.nhplug_body_field('acct', acct), 'UTF8')), 'hex');
-- body_digest text NOT NULL GENERATED ALWAYS AS (review.nhplug_body_digest_v1(operation_kind, side, symbol,
--   quantity, price, original_order_id, amend_scope, account_ref::text)) STORED
```

- 골든 벡터(`account_ref` = `3f2b8c1e-7a4d-4e6b-9c0a-5d1e2f3a4b5c`). 아래 digest 는 Python 구현과 PostgreSQL 17 생성
  컬럼(위 SQL, `uuid` 컬럼의 `::text`)에서 **둘 다 같게 나오는 것을 일회용 DB 로 확인**했다. 재구현은 이 표를 테스트 고정값으로 쓴다.

| # | op | side | sym | qty | px | org | scope | body_digest |
|---|---|---|---|---|---|---|---|---|
| V1 | place | buy | 005930 | 1 | 67400 | NULL | NULL | `a4793578393129f350e1c6c8ebf6b014f4f365a44b112a4153a2a9e8c4b2a416` |
| V2 | place | sell | 005930 | 3 | 71000 | NULL | NULL | `4f2800121e82f2c497abc77f8fcab597e645f7f49d3e96b45bd498f01afb85fb` |
| V3 | modify | buy | 005930 | 1 | 67000 | 1000123 | full | `0de1c5c727a6169a5818633547d6e0bbe4582a1c801c5183eeaf43ac892a819a` |
| V4 | modify | buy | 005930 | 1 | 67000 | 1000124 | partial | `a0450b4ca9605972c1d6e8261aae8cee739ccc7e7969f0e5616174977cebf787` |
| V5 | cancel | buy | 005930 | NULL | NULL | 1000130 | full | `ffecfc4affd110a0f81a3c1ac033d87e423898121e852b01dc485ea9c69b51be` |
| V6 | cancel | buy | 005930 | 2 | NULL | 1000131 | partial | `972c72857e8b939a22691b3fc75ef73d87dea515009d3d7c10c3eee27617a382` |

  V1 의 정규 바이트열(검증용): `nhplug-body-v1|op=5:place;side=3:buy;sym=6:005930;qty=1:1;px=5:67400;org=-;scope=-;acct=36:3f2b8c1e-7a4d-4e6b-9c0a-5d1e2f3a4b5c;`
- 스키마 버전을 올리면 새 함수 `…_v2` 와 새 벡터를 추가한다. 기존 행의 digest 는 바뀌지 않는다.

### 3.4 계좌 식별 — 키 회전에 불변인 `account_ref` (R1)

계좌번호는 어디에도 저장하지 않는다. 대신 두 추가 전용(UPDATE·DELETE 트리거 거부) 테이블을 둔다.

| 테이블 | 컬럼 | 제약 |
|---|---|---|
| `review.nhplug_mock_account_ref` | `account_ref uuid PK`, `created_at` | — |
| `review.nhplug_mock_account_binding` | `key_version smallint NOT NULL`, `binding text NOT NULL`(HMAC-SHA256 hex64), `account_ref uuid NOT NULL FK`, `created_at` | `PRIMARY KEY (key_version, binding)`, `UNIQUE (account_ref, key_version)` |

- `binding_v(act_no) = HMAC-SHA256(K_v, act_no)`, `K_v` 는 서버 비밀에서 HKDF(label `nhplug-account-binding-v<v>`)로
  파생한다. **쓰인 적 있는 모든 키 버전은 보존한다**(새 바인딩 발급만 멈추는 "은퇴"는 가능, 파기는 불가). 키·계좌번호는
  로그·응답에 나오지 않는다.
- **해석 `resolve_account_ref(act_no)`** — 이번 송신에서 `/n2/acctinfo` 로 검증한 `acct_type=03` 계좌번호에 대해, 보존된
  **모든** 키 버전으로 바인딩을 계산해 한 트랜잭션에서 조회한다.
  0. **보존 버전 집합은 DB 가 정한다(r4).** 추가 전용 표 `review.nhplug_mock_key_version(key_version PK, created_at)` 가 보존 버전의
     정본이다. 해석 트랜잭션은 먼저 `pg_advisory_xact_lock_shared(K_REG)` 를 잡고 이 표를 읽는다. 표의 버전 중 하나라도 이
     프로세스에 키가 없으면 `key_version_unavailable` 로 멈춘다(fail-closed). 새 버전 추가는 `pg_advisory_xact_lock(K_REG)`
     (배타)을 잡은 운영자 트랜잭션에서만 하므로, 동시에 해석하는 모든 프로세스는 **같은 버전 집합**을 본다. 버전 행은 삭제·수정 불가.
  1. 찾은 행이 전부 같은 `account_ref` 를 가리키고 모든 보존 버전 행이 있으면 그 값.
  2. 일부 버전에서만 찾았으면 같은 트랜잭션에서 **빠진 모든 보존 버전** 행을 같은 `account_ref` 로 추가(원자적 이관)하고 그 값.
  3. 어느 버전에서도 못 찾았으면 새 `account_ref` 를 만들고 **모든 보존 버전의 바인딩 행을 같은 트랜잭션에서** 삽입한다. 같은
     계좌를 동시에 처음 등록하는 두 트랜잭션은 같은 버전 집합을 쓰므로 반드시 같은 `(key_version, binding)` PK 에서 충돌하고,
     진 쪽은 롤백 후 재조회해 이긴 쪽의 `account_ref` 를 쓴다. 그래서 한 계좌가 두 `account_ref` 로 갈라질 수 없다.
  4. 서로 다른 `account_ref` 를 찾으면 `account_binding_conflict` 로 멈춘다(fail-closed). 규칙 0·3 아래에서는 도달하지 않아야 하는 방어선이다.
- 그래서 키를 회전해도 같은 계좌는 같은 `account_ref` 이고, 멱등·예약·중복 UNIQUE 와 `body_digest` 가 그대로 유지된다.
- T1 은 해석 결과를 저장하고, T3 은 D 가 방금 해석한 값을 WHERE 에 넣는다(RETURNING 은 계좌번호를 돌려주지 않는다).
  D 는 일치한 경우에만 그 계좌번호로 본문을 만든다.
- 전송 바이트 재검사: 바이트에서 디코드한 `act_no` 를 등록부로(읽기 전용 조회) 해석한 값이 행의 `account_ref` 와 같아야 한다.

---

## 4. 불확실성 (요구 3)

### 4.1 경계, fence, 첫 쓰기 게이트 (R2·R6)

"송신이 시작됐을 수 있는 순간" = **T5 fence 커밋 이후**다. 순서는 규범이다.

```
claim   = ledger.claim(row_id, request_id, intent_digest, account_ref, lease_identity)   # T3; 0행 → ClaimRejected(행 불변)
row     = claim.row
body    = build_body(row, verified_act_no)                     # 순수
request = httpx.Request("POST", MOCK_BASE_URL + path, json={"Input_0": body}, headers=...)
check_all_pre_send(row, body, request)                         # host·port·scheme·path·act_no·지정가·digest; 실패 → T4
transport = GatedTransport(retries=0, follow_redirects=False)  # 새 연결 풀, 생성만 — 소켓 I/O 없음; 실패 → T4
t0      = clock_boottime()                                     # fence UPDATE 를 보내기 **전에** 읽는다
fence   = ledger.fence(claim.token)                            # T5 단독 커밋; 결과 불명·0행 → 보내지 않고 종료
transport.arm(first_write_deadline = t0 + D_START)             # 게이트 무장
parsed, meta, failure = None, None, None
try:                                                           # ← 하나의 경계: fence 이후의 모든 일(정리 포함)
    try:
        async with asyncio.timeout_at(loop_deadline(t0 + D_SEND)):  # 종단 기한: send + 응답 읽기
            async with httpx.AsyncClient(transport=transport) as http: # 컨텍스트 진입·종료 모두 경계 안
                response = await http.send(request)            # 첫 애플리케이션 쓰기 직전에 게이트가 동기 검사
                meta, parsed = response_meta(response), read_and_parse(response)   # 번호를 읽으면 evidence
    finally:
        await transport.hard_close(timeout=D_CLOSE)            # 정리도 같은 경계 안 — 실패하면 아래 except 로
except BaseException as exc:                                   # 취소·정리 실패·알 수 없는 예외 전부
    failure = exc
outcome = classify(path, meta, parsed, failure)                # 순수·전역(total) 함수, 예외를 던지지 않는다(§4.3)
await shield(ledger.record_final(claim.token, outcome))        # T6/T8, 이미 uncertain 이면 T9a/T8e/T13d; 제한된 재시도
if failure is not None:
    raise failure
```

- **하나의 예외 경계(r4).** fence 커밋 뒤의 송신·응답 읽기·클라이언트 종료·`hard_close` 는 모두 바깥 `try` 안에 있고, 그
  바깥에는 결과 계산(`classify`, 예외 없음)과 기록 하나만 있다. 그래서 정리 실패·취소가 기록을 건너뛸 수 없고, 이미 읽은 번호는
  항상 `outcome` 에 실린다. 기록 `record_final` 은 `shield` 아래에서 lease 만료 전까지 제한된 재시도를 한다. **남는 한계:** DB 에
  끝내 쓰지 못하면 행은 `sending` 에 남아 V 의 T8v 로 `uncertain` 이 되고, 읽은 번호는 DB 에 남지 않는다(구조화 로그에만 행
  ID 와 함께 남긴다). 이 경우도 방향은 "미해결·재주문 차단"이지 중복 송신이 아니며, 해소는 후보·운영자 절차(§4.5)로 한다.
- **첫 쓰기 게이트.** `GatedTransport` 는 네트워크 백엔드를 감싼다. TLS 가 올라간 뒤 스트림의 **첫 애플리케이션 데이터
  쓰기 호출 안에서**, 실제 쓰기 시스템 호출 바로 앞에 `clock_boottime() < t0 + D_START` 를 동기로 검사하고(사이에 await
  없음) 넘으면 쓰지 않고 `FirstWriteDeadlineExceeded` 를 던진다. 연결 획득·TCP·TLS 대기는 모두 게이트 앞이다. 연결은
  디스패치마다 새로 만들고 재사용·재시도(`retries=0`)·리다이렉트는 없다. 이 HTTP 라이브러리가 네트워크 백엔드를 노출하지
  않으면 전송 계층을 그 하위 라이브러리(httpcore 연결 풀)로 직접 구성한다 — 게이트 위치는 T-LEASE-6 이 증명한다.
- **시계.** `clock_boottime()` 은 Linux `CLOCK_BOOTTIME`(일시 정지 시간 포함, 되돌아가지 않음)이다. `t0` 는 fence UPDATE 를
  보내기 전에 읽으므로 DB 가 쓰는 `sending_at = now()` 보다 실제 시각이 앞선다.
- **안전 부등식(유일한 타이밍 가정).** `lease_expires_at = sending_at + L_SEND` 이고 `D_START + 시계 오차 여유 < L_SEND` 이다
  (제안 `D_START`=2 초, `L_SEND`=120 초). 그러면 살아서 실행 중인 D 는 V 가 T8v 를 할 수 있는 시각 이후에 **첫 바이트를
  쓸 수 없다**. 첫 바이트가 나간 요청의 나머지 바이트·응답 읽기는 같은 한 번의 송신이다.
- **단계별 타임아웃은 총 상한이 아니다(R6).** httpx 의 connect/write/pool/read 타임아웃은 각 단계의 무활동 상한이라 합이
  전체 시간을 묶지 못한다. 그래서 (a) 송신·읽기 전체를 `asyncio.timeout_at(t0 + D_SEND)` 로 자르고(제안 30 초), (b) 정리는
  `D_CLOSE`(제안 5 초) 뒤 소켓을 강제 종료한다. 그래도 안전성은 (a)(b)에 의존하지 않는다: 진행 중인 송신이 얼마나 오래
  걸리든 V 는 lease 만료 뒤 `uncertain` 으로 옮길 뿐이고, D 의 늦은 결과는 T8e·T9a·T13d 로 기록된다.
- `D_START` 초과처럼 실제로 쓰지 않았어도 fence 이후라면 `uncertain` 으로 기록한다(S3 단일 규칙). 사유가
  `first_write_deadline_exceeded_not_sent` 이므로 운영자가 §4.6 절차로 빨리 닫을 수 있다.

### 4.2 분류 표(B3)

| 발생 지점 | 예 | 결과 |
|---|---|---|
| T1 이전 | 게이트·confirm·시장가·형식·멱등키 충돌·예약 충돌·계좌 해석 실패·레저 불가 | 행 없음(또는 기존 행 반환) |
| T3 실패 | 이미 claim·요청 ID 다름·digest·`account_ref` 불일치 | `ClaimRejected` — **행 불변** |
| T3~T5 | 호스트·계좌·지정가·digest 재검사 실패, 전송 계층 생성 실패, 토큰 발급 실패 | T4 `withdrawn` |
| T5 결과 불명 | 커밋 중 DB 오류 | 보내지 않음. `sending` 이면 V: T8v |
| T5 이후 전부 | 첫 쓰기 기한 초과, 종단 기한 초과, 타임아웃, 연결 거부·리셋, TLS, 3xx(리다이렉트 금지), 4xx/5xx, 읽기 오류, JSON 아님, 객체 아님, 부정 응답 코드(§4.3, 번호가 있어도), 성공 증명 코드 없는 응답(번호가 있어도 — 현재 모든 응답), **컨텍스트 종료·`aclose()`·`hard_close` 오류(번호를 이미 읽었어도)**, `CancelledError`·`KeyboardInterrupt`·`SystemExit`, 알 수 없는 예외 | T8 `uncertain` (+ 읽은 번호는 `ack_evidence_order_id`; 이미 T8v 였으면 T8e) |
| T5 이후 정상 완료 + 성공 증명 코드 + 번호 | 정리까지 예외 없음, `rsp_cd ∈ SUCCESS_PROOF_CODES[path]` — **목록이 비어 현재 도달 불가** | T6 `accepted`(이미 T8v 였으면 T9a) |
| 프로세스 사망 | kill, OOM | V: T8v |

- operations 의 분류는 **`ClaimRejected`·`PreSendRefusal` 만 송신 전**이고 그 밖은 전부 uncertain 이다(`operations.py:467`
  의 반대). O 는 fence 이후 행을 `withdrawn` 으로 만들 수 없다(트리거).
- 호출자 응답: `uncertain` 은 `success=false, status="uncertain", reconcile_required=true, retry_allowed=false` 와
  `ack_evidence_order_id`(있으면), 멱등키를 돌려준다. 같은 멱등키 재호출은 같은 행을 돌려줄 뿐 보내지 않는다(§5.2).

### 4.3 응답 판정 — 두 증명 목록(B4·r4)

`classify(path, meta, parsed, failure)` 는 순수·전역 함수다(어떤 입력에도 예외 없음):

```
if failure is None and meta.http_status == 200 and parsed.shape_ok
   and parsed.rsp_cd in SUCCESS_PROOF_CODES[path] and parsed.order_no is positive int:   -> ACCEPTED(order_no, rsp_cd)
if failure is None and meta.http_status == 200 and parsed.shape_ok
   and parsed.rsp_cd in NO_ORDER_PROOF_CODES[path] and parsed.order_no is None:           -> REJECTED(rsp_cd)
otherwise                                                                                 -> UNCERTAIN(reason, ack_evidence=parsed.order_no if any)
```

- **성공 증명 목록 `SUCCESS_PROOF_CODES[path]` — 네 경로 모두 빈 집합.** 벤더 명세는 주문 네 경로에 `200` 응답 스키마만 두고
  성공 코드를 정의하지 않으며, `x-response-status` 는 성공 판정을 "기대한 `Output_N` 블록에 값이 있는지 → `rsp_msg` 문구가
  완료를 뜻하는지, `rsp_cd` 는 로그·문의용으로 보관"으로 안내한다. 자유 문구(`rsp_msg`) 해석은 자동 판정 근거로 쓰지 않는다.
  그래서 번호가 있는 응답도 지금은 전부 `uncertain` + `ack_evidence_order_id` 이고, `accepted` 는 reconcile 의 T9b(자기 번호가
  완전한 목록에 보임)로 된다. 운영상 결과: 모든 주문이 첫 reconcile 까지 그 종목·방향의 예약을 쥔다.
- 목록은 코드가 아니라 **DB 표** `review.nhplug_success_proof_code(path, rsp_cd, citation, approved_by, created_at)` 이다. INSERT 는 운영자
  역할(§5.8)만 가능하고, 트리거가 `ack_source='response'` 인 `accepted` 전이에서 `success_rsp_cd` 가 이 표의 (경로, 코드)인지 검사한다.
  코드를 추가하려면 벤더 문서 인용(또는 벤더 확인서)과 운영자 승인을 함께 남긴다. 스모크에서 관찰된 코드만으로는 넣지 않는다.

#### 부정 응답

- 엔드포인트별 "주문이 생성되지 않았음을 증명하는 코드" 목록 `NO_ORDER_PROOF_CODES[path]` 를 둔다. **현재 네 경로 모두
  빈 집합**이다. 근거: 벤더 명세 `x-response-status`(서문 인용)와 네 주문 경로의 응답 정의에 그런 코드가 없고, 벤더 SDK
  가 코드 판정을 명시적으로 거부한다. 코드를 추가하려면 벤더 문서의 인용(또는 벤더 확인서)과 운영자 승인을 함께 남긴다.
- 따라서 비성공 코드 + 번호 없음은 `uncertain` 이다(T7 은 목록이 비어 있는 동안 도달 불가). 운영상 결과: 주문가능금액 부족 같은
  확실해 보이는 거부도 §4.5–4.6 절차로 닫힐 때까지 그 종목·방향의 새 주문을 막는다 — 멱등키 없는 벤더에서 중복 송신보다 나은 쪽이다.
- 부정 코드인데 번호가 함께 온 응답은 `rejected` 도 `accepted` 도 아닌 `uncertain` + `ack_evidence_order_id` 다.

### 4.4 reconcile — 부재는 증거가 아니다

현재 PR 의 목록 규칙을 규범으로 올린다(`order_evidence.py:236`, `:381`, `:470`; `reconcile_plan.py:143`, `:240`).

- 페이지는 알려진 조회 코드·목록 형태 행 블록·전부 파싱되는 행·완결된 페이지네이션일 때만 사용 가능하다. 계속 키(헤더·
  본문)·계속 코드·`cts_flag=Y` 중 하나라도 있으면 따라가고, 따라갈 수 없거나 키가 반복되면 **불완전**이다. `cts_flag=N` 은 다른
  계속 신호를 지우지 못한다.
- 미체결 판정은 `present`/`unknown` 뿐이다. "없음" 답은 없다.
- 종결은 양성 두 소스로만: `filled` = 전체 조회 행 + 체결 조회(`ost_cns_dit=1`) 같은 수량 · `cancelled` = 전체 조회 행의 취소 수량 +
  우리 취소 요청 행의 `ack_order_id` · `modified` = 전체 조회 행의 정정 수량 + 우리 정정 요청의 `ack_order_id` + 원주문을 가리키는
  후속 행 · `confirmed` = 자기 `ack_order_id` + 원주문 행의 반영 수량. `ack_evidence_order_id`(uncertain 증거)는 `ack_order_id`
  가 아니므로 이 규칙의 근거가 되지 않는다(T9b 로 `accepted` 가 된 뒤에만 `broker_order_id` 로 추적).
  위 규칙의 "우리 `ack_order_id`" 는 `ack_source` 가 `response`·`own_evidence`(우리 응답에서 읽은 번호)이거나 `operator`(운영자가
  이 요청의 주문임을 확인하고 승인 행으로 기록)인 값이다. 출처는 증거 JSON 에 함께 남는다.
- 수량 산술: `filled + open + cancelled + modified == order_qty` 가 아니면 `unknown`. 미체결 조회에 남아 있는데 전체 조회가
  종결이라 하면 `source_disagreement`.

### 4.5 uncertain 해소(B5)

1. **자기 접수번호 증명(T9b, 자동 허용).** 행에 `ack_evidence_order_id` 가 있고(T8 또는 T8e 로 기록), 완전한 전체 조회에
   **바로 그 번호**의 행이 있으며 종목·방향·수량·가격·원주문이 일치하면 `accepted`. 번호는 우리 요청의 응답에서 읽은 것이고
   목록이 같은 번호를 보이므로 양성 두 소스다. 번호는 같은데 속성이 다르면 T13 `anomaly`.
2. **속성 후보(자동 바인딩 금지).** 번호 증거가 없으면 속성(종목·방향·수량·가격·원주문, 주문 시각이 `[sending_at − ε,
   sending_at + D_START + ε]` 안)이 같은 행은 `candidate_order_ids` 에 **기록만** 한다(자기 전이, §2.4). 후보가 하나여도 자동으로
   `accepted` 가 되지 않는다. 운영자가 HTS/앱에서 그 주문이 이 요청임을 확인하고 `bind_candidate` 승인 행을 넣은 경우에만 T9h.
   **T9h 는 dispatcher 가 송신 단계를 떠났음이 증명된 뒤에만(r4):** (a) `dispatcher_done_at IS NOT NULL`(D 가 마지막 기록을 마침 —
   그 뒤 D 는 이 행에 쓰지 않으므로 늦은 번호가 바인딩과 충돌할 수 없다), 또는 (b) 승인 행이 §4.6 ③ 과 같은 lease 호스트
   프로세스 소멸 증명을 담아 `dispatcher_gone_proof=true` 인 경우. 어느 쪽도 아니면 트리거가 거부한다. D 가 살아 있는 동안
   운영자 바인딩이 앞서는 경쟁은 이 조건으로 없어진다.
3. 후보가 없거나 여럿이면 `uncertain` 을 유지하고 수동 검토를 표시한다. "보이지 않음"은 "보내지 않음"이 아니다.

### 4.6 재주문 차단 해제(B1·R2)

재주문 차단은 §5.2 의 부분 UNIQUE 예약이다(`intent·claimed·sending·uncertain` 행이 예약을 보유). 예약에서 나가는 길과 조건:

| 나가는 전이 | 왜 안전한가 |
|---|---|
| T2/T10i `intent → withdrawn` | claim 이 없다. 이후 claim 은 `state='intent'` 조건에서 0행 |
| T4/T10 `claimed → withdrawn` | fence 가 없다. fence 는 `state='claimed' AND now()<claim_deadline` 조건이라 0행 |
| T6/T9a/T9b/T9h `→ accepted` | 주문이 존재함이 확인됐다. 같은 본문 재주문은 여전히 §5.2 중복 규칙에 막힌다 |
| T13/T13d `→ anomaly` | 주문이 존재한다는 양성 증거(번호)가 있는 모순이다. 같은 본문 재주문은 중복 규칙에 막힌다 |
| **T14 `uncertain → abandoned`** | 아래 **모두** 충족 시에만(H): ① `lease_closed_at IS NOT NULL`(T8/T8v 가 항상 씀) ② `lease_expires_at + G < now()`(유예 G, 제안: 다음 거래일 시작) ③ **lease 호스트에서 기계로 검증한 프로세스 소멸**(아래) ④ 완전한 전체 조회(가능하면 세션 종료 후)에 자기 번호 증명·후보 확인이 없음 ⑤ 위 결과를 담은 `abandon` 승인 행(§5.8) |

- **lease 신원**(T3 이 한 번 쓰고 불변): `machine_id`(`/etc/machine-id`), `boot_id`(`/proc/sys/kernel/random/boot_id`), PID 네임스페이스
  inode(`/proc/self/ns/pid`), `pid`, 프로세스 시작 시각(`/proc/<pid>/stat` 22번째 필드, clock tick).
- **소멸 증명**(T14 CLI 가 그 호스트에서 실행, 결과를 승인 행에 기록): 같은 `machine_id` 이어야 하고, (a) 현재 `boot_id` 가 다르면
  재부팅 = 소멸 · (b) 같은 부팅·같은 PID 네임스페이스면 `/proc/<pid>` 가 없거나 시작 시각이 다르면 소멸 · (c) 같은 부팅·다른 PID
  네임스페이스면 호스트 전체 `/proc/*/ns/pid` 에 그 inode 가 하나도 없을 때만 소멸(호스트 수준 가시성 필요). 그 밖(다른 호스트,
  확인 불가, 멈춘(`T` 상태) 프로세스 존재)은 **거부**다.
- **원격 증언 분기는 없다.** lease 호스트를 쓸 수 없으면 그 행은 `uncertain` 으로 남고 그 종목·방향 예약도 남는다(fail-closed, §8 Q4).
- 시간만으로 풀리는 경로는 없다. V 의 T8v 는 `uncertain` 으로 옮길 뿐 예약을 유지한다.

---

## 5. 멱등·claim·fence·DB 강제 (요구 4)

### 5.1 T1 이전 전제(현 PR 에서 확정)

MCP 인자 `StrictBool`/`StrictInt`(JSON `0/1/"true"` 거부) · `NHPLUG_MOCK_ENABLED` 정확히 `"true"` · per-call `dry_run=False` +
`confirm=True`(정확한 타입·값, 계약 서브클래스 거부) · 지정가 전용 · `MockAccountAllowlist` 는 `/n2/acctinfo` 파싱으로만 생성 ·
주문은 해당 클라이언트 자신의 acctinfo 검증 계좌로만 · 모의 호스트 고정 + build 후 scheme·host·port·path·act_no 재검증 ·
`follow_redirects=False`.

### 5.2 인바운드 멱등키와 DB 예약(B2·R4·R7)

- **멱등키.** `dry_run=False` 인 모든 주문 요청은 호출자가 정한 `idempotency_key`(`^[A-Za-z0-9_-]{16,64}$`, NOT NULL)를 **필수**로
  가진다. MCP consumer 는 첫 시도 전에 키를 정하고 재시도에 같은 키를 쓴다. 스모크 CLI 는 키를 생성해 출력하고 `--idempotency-key` 로 재사용한다.
- **DB 제약**(모든 키 컬럼 NOT NULL — `account_ref`, `idempotency_key`, `order_date`, `symbol`, `side`, `body_digest`, `duplicate_ordinal`, `attempt_no`).
  - 부분 `UNIQUE (account_ref, idempotency_key) WHERE state <> 'withdrawn'` — 한 키에 살아 있는 시도는 하나. `attempt_no` 는 같은 키의 시도 번호(1부터).
  - 부분 `UNIQUE (account_ref, order_date, symbol, side) WHERE state IN ('intent','claimed','sending','uncertain')` — 진행 중·미해결
    주문은 종목·방향당 하나. 동시 T1 두 개 중 하나는 반드시 실패한다(`in_flight_order_exists` + 막는 행 ID).
  - 부분 `UNIQUE (account_ref, order_date, body_digest, duplicate_ordinal) WHERE state <> 'withdrawn'` + `duplicate_ordinal` NOT NULL
    DEFAULT 0 CHECK `>= 0` + `CHECK ((duplicate_ordinal = 0) = (duplicate_of IS NULL))` + `CHECK ((duplicate_ordinal = 0) =
    (second_order_authorization_id IS NULL))` — 같은 계좌·같은 본문의 같은 날 두 번째 주문은 `duplicate_ordinal ≥ 1` 이어야 하고, 그 값은 T15 로만 채운다.
- **같은 키 재호출의 규칙**(키 조회는 `pg_advisory_xact_lock(hash(account_ref, idempotency_key))` 아래에서):
  | 그 키의 행 | 결과 |
  |---|---|
  | 없음 | T1(`attempt_no=1`) |
  | digest 가 다른 행이 하나라도 있음 | `idempotency_key_conflict` |
  | 살아 있는 행이 `intent` | **그 행을 이어받는다(R7)**: 이 호출자가 D 로서 그 행에 T3 을 시도한다. claim 은 단일 사용이므로 동시 재시도 중 하나만 보낸다 |
  | 살아 있는 행이 `claimed`/`sending`/`uncertain`/그 뒤 상태 | 그 행의 상태를 돌려준다. 보내지 않는다 |
  | 모든 행이 `withdrawn` | 미송신이 증명됐으므로 새 시도 T1(`attempt_no = max + 1`) |
- **운영자 전용 "진짜 두 번째 주문"(T15, R8).** 운영자 CLI `--second-order-of <row_id> --confirm-second-order` 가 먼저 운영자 DB
  역할로 종류 `second_order` 승인 행을 넣고, 새 행에 `duplicate_of=<root_id>`, `second_order_authorization_id` 를 채운다.
  `duplicate_ordinal` 은 INSERT 트리거가 계산한다(§5.5). MCP 도구는 이 컬럼들을 받지 않는다(정적 가드). 이 경로도 진행 중
  예약 제약은 우회하지 않는다.
- T1 SQL 은 `INSERT … ON CONFLICT DO NOTHING RETURNING id` 후, 0행이면 어떤 제약에 걸렸는지 조회해 위 오류 중 하나를 돌려준다.

### 5.3 T3 claim

```sql
UPDATE review.nhplug_mock_order_ledger
   SET state = 'claimed', claim_token = :new_token, claimed_at = now(),
       claim_deadline = now() + :claim_window,
       lease_machine_id = :machine_id, lease_boot_id = :boot_id, lease_pid_ns = :pid_ns,
       lease_pid = :pid, lease_process_start = :proc_start
 WHERE id = :row_id
   AND client_request_id = :request_id
   AND state = 'intent' AND claim_token IS NULL
   AND account_ref = :account_ref_resolved_now
   AND body_digest = :intent_digest
RETURNING operation_kind, side, symbol, quantity, price, original_order_id, amend_scope,
          body_schema_version, account_ref, body_digest, claim_token;
```

D 는 RETURNING 필드로 Python 정규 바이트열을 만들어 digest 가 반환값과 같은지 확인한다(정규화 드리프트 방지). 다르면 T4.
같은 키로 행을 이어받은 재시도 호출자는 그 행의 `client_request_id`·`body_digest` 를 조회해 같은 조건으로 claim 한다.

### 5.4 T5 fence 와 결과 기록

```sql
-- T5 fence (단독 트랜잭션; t0 는 이 문장을 보내기 전에 읽는다)
UPDATE review.nhplug_mock_order_ledger
   SET state = 'sending', sending_at = now(), lease_expires_at = now() + :l_send
 WHERE id = :row_id AND claim_token = :token AND state = 'claimed'
   AND sending_at IS NULL AND now() < claim_deadline
RETURNING id;

-- T6/T8 (D): lease 반납과 dispatcher 완료를 같은 문장에서
UPDATE review.nhplug_mock_order_ledger
   SET state = :result, lease_closed_at = now(), dispatcher_done_at = now(), ...   -- T6: ack_source='response', success_rsp_cd
 WHERE id = :row_id AND claim_token = :token AND state = 'sending';
-- 0행이면(V 가 먼저 T8v) 같은 토큰으로 T9a / T8e / T13d 중 하나 (모두 dispatcher_done_at IS NULL 조건):
UPDATE ... SET state = 'accepted', broker_order_id = :n, ack_order_id = :n,            -- T9a (현재 도달 불가)
               ack_source = 'response', success_rsp_cd = :code, dispatcher_done_at = now()
 WHERE id = :row_id AND claim_token = :token AND state = 'uncertain' AND dispatcher_done_at IS NULL
   AND (ack_evidence_order_id IS NULL OR ack_evidence_order_id = :n);
UPDATE ... SET ack_evidence_order_id = :n, late_result_at = now(), dispatcher_done_at = now()   -- T8e
 WHERE id = :row_id AND claim_token = :token AND state = 'uncertain' AND dispatcher_done_at IS NULL
   AND ack_evidence_order_id IS NULL;
UPDATE ... SET state = 'anomaly', conflicting_order_id = :n, dispatcher_done_at = now(),       -- T13d
               manual_review_reason = 'ack_evidence_mismatch'
 WHERE id = :row_id AND claim_token = :token AND state = 'uncertain' AND dispatcher_done_at IS NULL
   AND ack_evidence_order_id IS NOT NULL AND ack_evidence_order_id <> :n;
-- 번호 없는 늦은 결과(예: 정리 실패만 남음)는 dispatcher_done_at 만 채운다(자기 전이, §2.4).

-- T8v (V): lease 취소를 같은 문장에서
UPDATE review.nhplug_mock_order_ledger
   SET state = 'uncertain', uncertain_reason = 'lease_expired_without_result', lease_closed_at = now()
 WHERE id = :row_id AND state = 'sending' AND lease_expires_at < now();
```

### 5.5 DB 강제(M7·R3·R4)

| 장치 | 내용 |
|---|---|
| NOT NULL | `client_request_id`, `account_ref`, `idempotency_key`, `attempt_no`, `order_date`, `operation_kind`, `side`, `symbol`, `body_schema_version`, `body_digest`, `duplicate_ordinal`, `state`, `reconcile_state`, `requires_manual_review`, `created_at` |
| UNIQUE | `client_request_id`, `claim_token`, `(order_date, broker_order_id)`, `second_order_authorization_id`, `resolution_authorization_id`, §5.2 부분 UNIQUE 3개 |
| CHECK | `state` 목록 · §3.1 컬럼 형식과 조작별 NULL 조합 · `(claim_token IS NULL) = (claimed_at IS NULL)` · `state='intent' ⇒ claim_token IS NULL` · `state NOT IN ('intent','withdrawn') ⇒ claim_token IS NOT NULL` · `sending_at IS NOT NULL ⇒ claim_token IS NOT NULL AND lease_expires_at IS NOT NULL` · `state IN ('sending','accepted','uncertain','abandoned','open','partially_filled','filled','cancelled','modified','confirmed') ⇒ sending_at IS NOT NULL` · `state='withdrawn' ⇒ sending_at IS NULL` · **`state NOT IN ('intent','claimed','sending','withdrawn') AND sending_at IS NOT NULL ⇒ lease_closed_at IS NOT NULL`** · `state='sending' ⇒ lease_closed_at IS NULL` · §5.2 중복·승인 CHECK · 지정가·KRX·모의 고정 · **식별자(r4)**: `state NOT IN ('accepted','open','partially_filled','filled','cancelled','modified','confirmed') OR (broker_order_id IS NOT NULL AND ack_order_id IS NOT NULL AND ack_source IS NOT NULL AND broker_order_id ~ '^[1-9][0-9]{0,9}$' AND ack_order_id = broker_order_id)` · `state NOT IN ('intent','claimed','sending','withdrawn','uncertain','abandoned','rejected') OR (broker_order_id IS NULL AND ack_order_id IS NULL AND ack_source IS NULL)` · `ack_order_id IS NOT DISTINCT FROM broker_order_id` · `ack_source IS NULL OR ack_source IN ('response','own_evidence','operator')` · `ack_source IS DISTINCT FROM 'response' OR success_rsp_cd IS NOT NULL` · `ack_source IS DISTINCT FROM 'operator' OR resolution_authorization_id IS NOT NULL` · **수량(r4)**: `filled_qty IS NULL OR (quantity IS NOT NULL AND filled_qty >= 0 AND filled_qty <= quantity)` |
| **BEFORE INSERT 트리거** | `state='intent'` 만. claim·fence·lease·결과·증거·수량·후보 컬럼 전부 NULL, `reconcile_state='pending'`, `requires_manual_review=false`. **일반 T1**: `duplicate_ordinal=0 AND duplicate_of IS NULL AND second_order_authorization_id IS NULL`, `resolution_authorization_id IS NULL` · **T15**: `duplicate_of` 가 가리키는 행을 `FOR UPDATE` 로 잠그고, 그 행이 `duplicate_ordinal=0` 인 뿌리이며 같은 `account_ref`·`order_date`·`body_digest` 이고 `withdrawn` 이 아님을 확인한 뒤 `duplicate_ordinal = (그 뿌리 묶음의 최대값) + 1` 을 **트리거가** 쓴다(호출자 값 무시) · 승인 행이 종류 `second_order`, 같은 뿌리·digest·계좌·날짜, 미소비여야 하고 트리거가 소비 처리한다 |
| **BEFORE UPDATE 트리거** | (old,new) 상태 쌍이 §2.3 에 없고 §2.4 자기 전이도 아니면 거부 · 본문 필드·`account_ref`·`idempotency_key`·`attempt_no`·`client_request_id`·`duplicate_*`·`second_order_authorization_id` 는 `NEW.x IS DISTINCT FROM OLD.x` 이면 거부(NULL→값 포함) · `claim_token`·`claimed_at`·`claim_deadline`·lease 신원·`sending_at`·`lease_expires_at`·`lease_closed_at`·`ack_evidence_order_id`·`late_result_at`·`resolution_authorization_id`·`dispatcher_done_at`·`conflicting_order_id`·`ack_source`·`success_rsp_cd` 는 NULL 에서 한 번만 채워지고 이후 불변 · 증거·수량 컬럼은 `NEW.reconcile_state='verified'` 일 때만 변경 · T9b·T9h·T14 는 §2.3 의 트리거 조건(자기 번호 일치 또는 `resolution_authorization_id` 가 가리키는 일치·미소비 승인 행)을 검사하고 승인 행을 소비 처리 · `resolution_authorization_id` 는 T9h·T14 에서만 채워진다 · 종결 상태 불변 |
| **CHECK 의 NULL 규칙(r4 정정)** | r3 의 "모든 CHECK 가 `UNKNOWN` 을 통과시키지 않는다"는 주장은 **틀렸다**: `ack_order_id IS NULL OR ack_order_id = broker_order_id` 는 `broker_order_id` 가 NULL 이면 `UNKNOWN` 이 되어 통과하고, `filled_qty <= quantity` 도 한쪽이 NULL 이면 통과한다. PostgreSQL CHECK 는 `UNKNOWN` 을 통과로 본다. r4 규칙: 모든 CHECK 는 "상태가 해당 없음(`state NOT IN (…)`, `state` 는 NOT NULL) **OR** (필요한 컬럼 각각 `IS NOT NULL` AND 비교)" 형태로 쓰고, 비교 연산자의 피연산자는 NOT NULL 컬럼이거나 같은 AND 안에서 `IS NOT NULL` 로 먼저 묶이거나 `IS [NOT] DISTINCT FROM` 이다. `false AND UNKNOWN = false` 이므로 NULL 이 섞인 행은 위반이 된다. T-DB-7 이 각 CHECK 의 nullable 피연산자마다 NULL 을 넣어 위반을 단언한다 |
| **accepted 전이 가드(r4)** | 트리거: `→ accepted` 는 `ack_source` 별로 — `response`: OLD.state 가 `sending`(T6) 또는 `uncertain` 이고 `OLD.dispatcher_done_at IS NULL`(T9a), `(path, success_rsp_cd)` 가 성공 증명 표에 있음, `OLD.ack_evidence_order_id` 가 NULL 이거나 같은 번호 · `own_evidence`: OLD.state=`uncertain`, `NEW.broker_order_id = OLD.ack_evidence_order_id`, `NEW.reconcile_state='verified'`(T9b) · `operator`: OLD.state=`uncertain`, 일치·미소비 `bind_candidate` 승인 행, `OLD.ack_evidence_order_id IS NULL OR = NEW.broker_order_id`, `OLD.dispatcher_done_at IS NOT NULL OR 승인 행.dispatcher_gone_proof`(T9h). 그 밖은 거부 |
| DELETE | 거부 |
| 계좌 등록부 | §3.4 세 테이블(`account_ref`, `account_binding`, `key_version`)은 INSERT 만(UPDATE·DELETE 트리거 거부). `key_version` INSERT 는 운영자 역할만 |

### 5.6 동시성·세션

- READ COMMITTED 에서 같은 행의 두 T3·두 T5·T5 대 T10·T6/T8 대 T8v 는 행 잠금으로 직렬화되고 뒤 문장은 재평가 후 0행이다.
  서로 다른 행의 동시 T1 은 §5.2 부분 UNIQUE 로, 같은 키 재호출은 advisory xact lock 으로 직렬화된다.
- claim·fence·결과 기록은 **dispatcher 전용 세션**의 짧은 트랜잭션이다. 호출자 트랜잭션에 섞지 않는다.

### 5.7 재전송·중복 표

| 시도 | 막는 장치 | 송신 |
|---|---|---|
| 같은 행 재생(같은 요청 ID) | T3 `state='intent'` | 0 |
| 같은 행 새 요청 ID | T3 `client_request_id` | 0 |
| 두 번째 클라이언트·프로세스 | T3 DB 조건 | 0 |
| 같은 행 동시 두 호출 | 행 잠금 | 1 |
| T1 커밋 후 D 사망, 같은 키 재시도 | 재시도가 같은 `intent` 행을 이어받아 T3 | 1 (R7) |
| 같은 키 재시도(행이 claimed 이후) | 부분 `UNIQUE (account_ref, idempotency_key)` + 규칙표 → 기존 행 반환 | 0 추가 |
| 같은 키 재시도(모든 시도 withdrawn) | 미송신 증명 → 새 시도 | 1 |
| HMAC 키 회전 후 같은 키·같은 본문 재시도 | `account_ref` 불변(§3.4) → 위 규칙 그대로 | 0 추가 (R1) |
| 다른 키·같은 종목·방향, 앞 주문 진행 중·미해결 | 부분 UNIQUE 예약 | 0 |
| 동시 T1 두 개 | 부분 UNIQUE | 행 1개 |
| 앞 주문 `accepted` 뒤 같은 본문 새 키 | `(account_ref, order_date, body_digest, duplicate_ordinal)` | 0 — 운영자 T15 로만 |
| `idempotency_key`/`duplicate_ordinal` NULL 직접 INSERT | NOT NULL | 0 (R4) |
| 엉뚱한 `duplicate_of`·임의 ordinal | T15 트리거(뿌리·묶음 일치, 트리거 계산 ordinal, 승인 행) | 0 (R8) |
| 수량·가격·범위 변경 | 송신 API 에 인자 없음 + 불변 트리거 + digest | 0 |
| 다른 계좌 | `account_ref` 불일치 | 0 |
| fence 뒤 멈췄다 깨어난 송신자 | 첫 쓰기 게이트(`t0 + D_START`) · 게이트 뒤 극소 창은 T14 소멸 증명 전 예약 유지 | 그 행 외 추가 송신 0 |

### 5.8 운영자 승인 행(R8)

- 테이블 `review.nhplug_mock_operator_authorization`: `id`, `kind`(`second_order`/`bind_candidate`/`abandon`), `target_row_id`,
  기대 값(`account_ref`, `order_date`, `body_digest`, 후보 번호), `evidence`(T14 소멸 증명 출력 등), `dispatcher_gone_proof` bool(T9h 에서 §4.5(2)(b) 를 쓸 때 true, 증명은 `evidence` 에), `operator_id`, `reason`,
  `created_at`, `consumed_by_row_id`, `consumed_at`. 모든 컬럼 NOT NULL(소비 두 컬럼 제외).
- **DB 역할.** INSERT 권한은 운영자 전용 역할 `nhplug_operator` 에만 있다. 애플리케이션(MCP 서버) 역할은 SELECT 만 가진다.
  소비 처리(`consumed_*` 를 NULL→값 한 번)는 레저 트리거가 `SECURITY DEFINER` 함수로 한다. 승인 행은 UPDATE(소비 외)·DELETE 거부.
- 그래서 서비스 버그나 MCP 경로는 T9h·T14·T15 에 필요한 승인 행을 만들 수 없고, 한 승인으로 두 행을 움직일 수도 없다.
- 성공 증명 표(§4.3)와 키 버전 표(§3.4)의 INSERT 도 같은 운영자 역할 전용이다.
- 한계: DB 는 T14·T9h(b) 소멸 증명의 **내용**을 검증하지 못한다 — 증명은 lease 호스트의 CLI 가 기계로 수행하고 결과를 `evidence` 에 남긴다.

---

## 6. 결함 ↔ 방지 규칙 (요구 5)

### 6.1 PR #2100 BLOCKER 13건

| # | 라운드 | 결함 | 방지 규칙 | 테스트(§7) |
|---|---|---|---|---|
| 1 | r1 | MCP JSON `0/1/"true"` 가 `dry_run`/`confirm` 으로 변환 | §5.1 Strict 스키마 | T-GATE-2 |
| 2 | r1 | 손으로 만든 allowlist 로 실계좌 바인딩 | §5.1 acctinfo 전용 생성·자기 검증 계좌 + §3.4 해석 | T-ACCT-1..3 |
| 3 | r1 | 디스패처 권한 미검사·계약 서브클래스 | §5.1 정확한 타입·값 검사(진입과 T3 직전) | T-GATE-3 |
| 4 | r1 | 빈/미완료 목록을 "없음"·종결로 | S6, §4.4, §4.5(부재·속성만으로 해소 금지) | T-LIST-1..3, T-CAND-1 |
| 5 | r1 | 모순 수량을 검증된 체결로 | §4.4 수량 산술 | T-REC-4 |
| 6 | r1 | `unknown` 상태로 체결 수량 기록 | §5.5 INSERT·UPDATE 트리거(증거 필드는 verified 에서만, INSERT 시 NULL) | T-DB-3, T-DB-4 |
| 7 | r1 | 스모크 왕복이 unknown·미해결에도 ok | §7 T-SMOKE | T-SMOKE-1 |
| 8 | r2 | 빈 미체결 조회 + 증인 행을 "없음"·종결 근거로 | S6, §4.4 양성 두 소스 | T-LIST-2, T-REC-1..3 |
| 9 | r2 | 모순된 계속 메타데이터를 마지막 페이지로 | §4.4 페이지 규칙 | T-LIST-3 |
| 10 | r2 | 커밋된 레저 행 없이 송신 | S1·S4 claim 된 행에서만 송신 | T-CLAIM-1 |
| 11 | r3 | 복제 가능한 in-process intent·로컬 소비 | S4 DB claim + S2 + S5 멱등키·예약 + 불변 트리거 | T-CLAIM-2..6, T-IDEM-1..8 |
| 12 | r4 | 정정 범위가 claim 밖 인자 | §3.1 `amend_scope` 고정·digest·바이트 재검사, 인자 제거 | T-BODY-2, T-CLAIM-5 |
| 13 | r4 | POST 후 `aclose()` 오류가 `not_submitted` | S3·§4.1–4.2 하나의 경계(정리 포함, r4)·분류 반전·트리거 | T-UNC-1..5 |

### 6.2 설계 리뷰 r1 지적 8건

| 지적 | 반영 | 테스트 |
|---|---|---|
| B1 fence 뒤 멈춘 송신자가 복구·운영자 종결 후 송신 | §4.1 fence + 첫 쓰기 게이트 + lease, §4.6 T14 조건, §2.6 (r3 에서 강화: §6.3 R2·R3) | T-LEASE-1..7 |
| B2 새 요청 ID 마다 새 행 → 같은 업무 요청 두 번 송신 | §5.2 필수 멱등키 + 부분 UNIQUE 예약 + 중복 서수, 운영자 T15 (r3: R1·R4·R7·R8) | T-IDEM-1..8, T-ROT-1..2 |
| B3 `aclose()` 뒤 `accepted` 유지가 운영자 요구 3 과 충돌 | §4.2: fence 이후 예외는 전부 `uncertain`, 번호는 `ack_evidence_order_id` 로만 (r3: T8e) | T-UNC-2, T-EVID-1 |
| B4 증명 없는 부정 코드로 `rejected` 종결 | §4.3 `NO_ORDER_PROOF_CODES` 빈 집합(벤더 문서 인용) → `uncertain` | T-NEG-1 |
| B5 속성만으로 남의 주문 바인딩 | §4.5 후보 기록만, 자동 바인딩은 자기 번호 증명(T9b)뿐, 그 밖은 운영자 T9h | T-CAND-1..2 |
| M6 같은 상태 기록 불가 | §2.4 자기 전이 표 | T-SM-3 |
| M7 INSERT·NULL→값 구멍 | §5.5 BEFORE INSERT + `IS DISTINCT FROM` (r3: NOT NULL 목록) | T-DB-3..6 |
| M8 digest 인코딩 모호 | §3.3 버전·길이 구분 인코딩, bigint 가격, ASCII CHECK, 골든 벡터 | T-DIGEST-1..3 |

### 6.3 설계 리뷰 r2 지적 8건

| 지적 | 반영 | 테스트 |
|---|---|---|
| 1 BLOCKER `K_bind` 회전이 세 중복 제약을 우회 | R1: §3.4 추가 전용 계좌 등록부 + 모든 키 버전 보존 + 해석 시 원자적 이관, 제약·digest 는 `account_ref` 위 | T-ROT-1..2 |
| 2 BLOCKER HTTP 직전 재검사 없음·원격 소멸 증명 미정 | R2: §4.1 전송 계층 생성 → fence → 첫 애플리케이션 쓰기 직전 동기 게이트(`CLOCK_BOOTTIME`), 안전 부등식 `D_START + 여유 < L_SEND`; §4.6 lease 호스트 기계 검증만, 원격 분기 삭제 | T-LEASE-6, T-LEASE-4 |
| 3 BLOCKER lease 취소의 합법 전이 없음 | R3: §2.3 T6·T7·T8·T8v 가 같은 UPDATE 에서 `lease_closed_at` 기록, CHECK, T14 조건 ① 이 이것을 요구 | T-LEASE-2..4, T-SM-1 |
| 4 BLOCKER NULL 이 멱등·중복 UNIQUE 를 우회 | R4: §5.5 NOT NULL 목록 + 승인 CHECK + 일반 T1/T15 트리거 규칙 | T-NULL-1, T-IDEM-5 |
| 5 MAJOR 늦은 close 오류가 번호 증거를 잃음 | R5: T8e(`uncertain` 에 토큰 일치·NULL→값 한 번) + 불일치 T13d | T-EVID-1..2 |
| 6 MAJOR `HTTP_TIMEOUT` 합을 총 상한으로 사용 | R6: §4.1 종단 기한 + 정리 상한, 안전성은 첫 쓰기 기한 부등식에만 의존, 늦은 결과는 T8e/T9a/T13d | T-LEASE-7 |
| 7 MAJOR T1 커밋 후 사망 시 같은 키 재시도가 영원히 막힘 | R7: §5.2 규칙표 — `intent` 이어받기, 전부 `withdrawn` 이면 새 시도 | T-IDEM-6..7 |
| 8 MAJOR T15 계보·주체 증명 부족 | R8: §5.5 T15 트리거(뿌리·묶음 일치, 트리거 계산 ordinal), §5.8 운영자 DB 역할 전용 1회용 승인 행 | T-AUTH-1..3 |

### 6.4 설계 리뷰 r3 지적 5건(r4)

| 지적 | 반영 | 테스트 |
|---|---|---|
| 1 BLOCKER `hard_close` 실패가 기록을 건너뜀 | Q1: §4.1 fence 이후 송신·읽기·종료·`hard_close` 를 하나의 바깥 `try` 로, 그 밖에는 예외 없는 `classify` + `shield` 된 기록 하나. DB 기록 자체 실패의 한계 명시 | T-UNC-5 |
| 2 BLOCKER 번호 있는 부정 응답이 `accepted` | Q2: §4.3 `classify` 규범 — T6/T9a 는 `SUCCESS_PROOF_CODES[path]`(DB 표, 운영자 역할만, 벤더 인용 필수)가 있어야 함. 벤더가 성공 코드를 문서화하지 않아 **빈 집합** → 번호가 있어도 `uncertain` + 증거 | T-NEG-1, T-SUCC-1 |
| 3 BLOCKER 식별자 없는 `accepted`·CHECK 의 NULL 통과 | Q3: §5.5 상태별 식별자 CHECK(양수·같음·NOT NULL), `IS NOT DISTINCT FROM`, 수량 CHECK 재작성, `ack_source` 별 accepted 전이 가드, r3 의 잘못된 주장 정정 | T-DB-7 |
| 4 BLOCKER 혼합 버전 동시 최초 등록이 계좌를 가름 | Q4: §3.4 보존 버전 집합의 정본을 DB 표로, 공유/배타 advisory lock, 최초 등록 시 모든 보존 버전 바인딩을 한 트랜잭션에서 삽입 → 동시 생성자는 같은 PK 에서 충돌 | T-ROT-3 |
| 5 MAJOR T9h 가 진행 중 D 의 늦은 번호를 가림 | Q5: `dispatcher_done_at`(D 의 마지막 기록이 씀), T9h 는 그 값 또는 소멸 증명(`dispatcher_gone_proof`)이 있어야 함, T8e/T9a/T13d 는 `dispatcher_done_at IS NULL` 조건 | T-CAND-3 |

---

## 7. 재구현 필수 테스트 (요구 6)

가짜 전송·가짜 브로커·자체 일회용 DB 만 쓴다(실 NH 호출 0). **각 규칙 테스트는 그 규칙을 되돌리는 변이에서 단언 실패로
RED** 여야 하며, 변이는 테스트 파일에 기계적으로 선언한다(현 PR `test_stage2_mutants.py` 방식).

### 7.1 상태 기계·DB
- **T-SM-1** §2.3 합법 전이 전부 성공(T6·T8·T8v 후 `lease_closed_at` 채워짐 포함), 표 밖 (old,new) 쌍 전부 트리거 거부(쌍 전수 열거).
- **T-SM-2** 불변 필드 변경·NULL 복귀·NULL→값 거부, DELETE 거부.
- **T-SM-3** §2.4 자기 전이: 허용 컬럼만 바뀌면 통과, 그 밖 컬럼 하나라도 바뀌면 거부(상태별). 값이 있는 `ack_evidence_order_id` 변경 거부.
- **T-DB-1..2** §5.5 CHECK·UNIQUE 위반 INSERT/UPDATE 가 `IntegrityError`.
- **T-DB-3** INSERT 에 증거·수량·claim·lease·후보 값이 있으면 거부.
- **T-DB-4** `reconcile_state<>'verified'` 로 증거·수량 변경 거부.
- **T-DB-5** 본문 필드 NULL→값(예: `amend_scope` 나중 채우기) 거부.
- **T-DB-6** `lease_closed_at` 없이 `sending` 에서 나가는 UPDATE 거부(CHECK).
- **T-NULL-1** `idempotency_key`·`duplicate_ordinal`·`account_ref`·`attempt_no` 가 NULL 인 직접 INSERT 거부. 변이: NOT NULL 제거 → 같은 본문 두 행이 차례로 들어가 둘 다 claim·송신되는 시나리오가 GREEN 이 되어 RED.
- **T-DB-7** CHECK 의 NULL 열거(r4): 식별자·수량 CHECK 의 nullable 피연산자마다 NULL 을 넣은 INSERT/UPDATE 가 위반. 직접 쓴 잘못된 T6 — `accepted` 인데 `broker_order_id`·`ack_order_id` 둘 다 NULL · `ack_order_id` 만 있음 · 두 값이 다름 · `0`/비숫자 · `ack_source` 없음 · `response` 인데 `success_rsp_cd` 가 표에 없음 — 전부 거부. 변이: r3 의 `ack_order_id IS NULL OR …` 복원 → RED.
- **T-MIG-1** 마이그레이션 upgrade → ORM 과 제약·인덱스·트리거·함수·역할 권한까지 동일 → downgrade → upgrade(일회용 DB).

### 7.2 digest(§3.3)
- **T-DIGEST-1** 골든 벡터 V1–V6: Python 인코더와 DB 생성 컬럼이 표의 값을 정확히 낸다.
- **T-DIGEST-2** 각 필드 하나만 바꾸면 digest 가 바뀐다(8 필드 × 대표값). NULL 과 빈 문자열이 구별된다.
- **T-DIGEST-3** 정규화 드리프트 주입(Python 인코더 변이) → T3 후 RETURNING 재계산 불일치 → T4, 송신 0.

### 7.3 claim·본문(§3, §5.3)
- **T-CLAIM-1** 레저 행 없음·가짜 레저·레저 서브클래스 → 토큰·소켓 0.
- **T-CLAIM-2..4** 같은 행 재생·새 요청 ID·두 번째 클라이언트+세션: 송신 1회, 본문 = 커밋 본문.
- **T-CLAIM-5** 필드·`amend_scope` 직접 변경(트리거 거부), 다른 digest(0행), 다른 `account_ref`(0행) → 송신 0, 이후 정상 1회.
- **T-CLAIM-6** 같은 행 동시 두 dispatcher(별도 세션) → 1회. 변이: WHERE 에서 상태·토큰 조건 제거 → RED.
- **T-BODY-1** 매수·매도·정정(전량/일부)·취소(전량/일부) 전송 바이트 == `build_body(claim 행)`, digest == 골든 벡터 규칙.
- **T-BODY-2** 원주문 5주·일부 1주 정정은 `all_pat_dit_cd=2`·`cor_qty=1` 로만 나간다. 송신 API 에 범위 인자 없음(정적 가드).
- **T-BODY-3** 빌드 후 바이트 변조(수량·가격·종목·범위·계좌·호가 05·SOR·IOC) → T4, 송신 0.

### 7.4 멱등·예약·계좌(§3.4, §5.2, §5.8)
- **T-IDEM-1** 같은 멱등키 재호출(행이 claimed 이후, 첫 호출의 응답 유실 후 포함) → 같은 행 반환, 송신 추가 0.
- **T-IDEM-2** 같은 키 다른 본문 → `idempotency_key_conflict`(앞 시도가 withdrawn 이어도).
- **T-IDEM-3** 다른 키·같은 종목·방향, 앞 행이 `intent`/`claimed`/`sending`/`uncertain` 각각 → `in_flight_order_exists`, 송신 0.
- **T-IDEM-4** 동시 T1 두 개(별도 세션, `gather`) → 행 1개. 같은 키 동시 재호출 두 개 → 행 1개, 송신 1.
- **T-IDEM-5** 앞 주문 `accepted` 뒤 같은 본문 새 키 → 거부. 운영자 승인 행 + `--second-order-of` 로만 `duplicate_ordinal=1` 행 생성·송신 1회. MCP 는 이 필드를 받지 않는다.
- **T-IDEM-6** T1 커밋 직후 O/D 사망 → 같은 키 재시도가 같은 `intent` 행을 claim 해 송신 1회. 변이: "intent 면 반환만" → 주문 유실로 RED.
- **T-IDEM-7** 같은 키의 앞 시도가 T4/T10/T10i 로 `withdrawn` → 재시도가 `attempt_no=2` 행으로 송신 1회. 앞 시도가 `sending`/`uncertain` 이면 새 시도 0.
- **T-IDEM-8** 같은 키로 이어받기 두 개 동시 → claim 1개, 송신 1회.
- **T-ROT-1** 키 v1 로 `uncertain` 행 생성 → 키 v2 로 회전 → 같은 키·같은 본문 재시도: 같은 `account_ref`, 예약·멱등 충돌, 송신 0. 등록부에 v2 바인딩이 원자적으로 추가됨.
- **T-ROT-2** 키 v1 로 `accepted`(응답 유실) → 회전 → 같은 키 재시도 송신 0 · 새 키·같은 본문 → 중복 규칙 거부. 변이: 해석이 현재 버전만 조회 → RED. 서로 다른 `account_ref` 를 가리키는 두 버전 → `account_binding_conflict`.
- **T-ROT-3** 혼합 버전 동시 최초 등록(r4): 키 버전 표에 v1·v2, 두 세션이 같은 새 계좌를 동시에 해석(`gather`, 커밋 전 대기 지점 주입) → `account_ref` 1개, 바인딩은 v1·v2 모두 같은 ref. 표의 버전 키가 없는 프로세스 → `key_version_unavailable`, 쓰기 0. 해석이 공유 잠금을 쥔 동안 버전 추가는 대기. 변이: 현재 버전 바인딩만 삽입 → 두 ref 로 갈라져 RED.
- **T-AUTH-1** 애플리케이션 역할로 승인 행 INSERT → 권한 오류. 성공 증명 표·키 버전 표 INSERT 도 권한 오류. 승인 행 없이 T9h/T14/T15 → 트리거 거부.
- **T-AUTH-2** T15 에서 다른 계좌·다른 날짜·다른 digest·뿌리가 아닌 행을 가리키는 `duplicate_of`, 호출자가 넣은 임의 ordinal → 거부 또는 트리거 값으로 덮임.
- **T-AUTH-3** 한 승인 행으로 두 번째 전이 → 거부(1회용). 종류가 다른 승인 행 → 거부.

### 7.5 uncertain·fence·lease(§4)
- **T-UNC-1** fence 이후 타임아웃·리셋·3xx·4xx·5xx·비JSON·객체 아님·성공 코드+번호 없음 → 전부 `uncertain`, `retry_allowed=false`.
- **T-UNC-2** POST 후 `aclose()` 예외: 번호를 읽기 전이면 `uncertain`, **읽은 뒤여도 `uncertain` + `ack_evidence_order_id`**, 호출자 응답 `success=false`,
  같은 멱등키 재호출은 송신 0. 변이: 컨텍스트 종료를 경계 밖으로 · 번호 읽음 시 `accepted` 유지 → 각각 RED.
- **T-UNC-3** fence 이후 알 수 없는 예외 → `uncertain`(분류 반전). 변이: "그 밖 = 송신 전" 복원 → RED.
- **T-UNC-4** `CancelledError`/`KeyboardInterrupt` 가 `send()` 중 → `uncertain` 기록 후 재발생.
- **T-NEG-1** 가짜 브로커가 주문을 기록한 뒤 비성공 코드·번호 없음 응답 → `uncertain`(`rejected` 아님), `NO_ORDER_PROOF_CODES` 가 빈 집합임을 단언. **부정 코드 + 번호**(r4) → `uncertain` + `ack_evidence_order_id`, `accepted` 아님.
- **T-SUCC-1** 성공 모양의 200 + 번호, 성공 증명 표가 비어 있음 → `uncertain` + 증거(T6 아님), 이후 R 의 T9b 로 `accepted`. 테스트 픽스처가 운영자 역할로 (경로, 코드)를 넣으면 같은 응답 → T6. 다른 경로의 코드 → `uncertain`. `classify` 는 임의 입력(퍼징)에 예외를 던지지 않는다. 변이: "번호만 있으면 ACCEPTED" 복원 → RED.
- **T-UNC-5** 번호를 읽고 클라이언트가 정상 종료된 뒤 `hard_close` 가 `OSError`/취소를 던짐 → 행 `uncertain` + `ack_evidence_order_id`(T8), V 가 먼저 T8v 한 경우 → T8e. 기록 DB 쓰기 실패 주입 → 행 `sending` 유지 후 V: T8v, 추가 송신 0. 변이: `hard_close` 를 바깥 경계 뒤 `finally` 로 → RED.
- **T-EVID-1** 번호를 읽은 뒤 `aclose()` 가 멈춘 사이 V 가 T8v → D 의 close 예외 → T8e 로 `ack_evidence_order_id` 기록 → R 의 T9b 로 해소 가능. 변이: T8e 제거 → 번호 유실로 RED.
- **T-EVID-2** T8e 뒤 다른 번호의 늦은 결과 → T13d `anomaly`. 같은 번호의 정상 결과 → T9a.
- **T-LEASE-1** T5 커밋 결과 불명(커밋 예외 주입) → 송신 0, 이후 V: T8v.
- **T-LEASE-2** T3 후 T5 전 멈춤 → V: T10 → 깨어난 dispatcher 의 fence 0행, 송신 0.
- **T-LEASE-3** fence 후 연결 단계에서 멈춤(가짜 백엔드가 connect 를 `D_START` 넘게 지연) → 게이트가 쓰지 않음, 가짜 브로커 수신 0, 행 `uncertain(first_write_deadline_exceeded_not_sent)`.
- **T-LEASE-4** fence 후 멈춤 → V: T8v(`lease_closed_at` 기록) → H 의 T14: 유예 미경과·다른 `machine_id`·같은 부팅에서 pid+시작 시각 생존·멈춘(`T`) 프로세스 존재 각각에서 거부. `boot_id` 변경 또는 프로세스 소멸 + 유예 + 승인 행이면 허용. 이 동안 같은 종목·방향 새 T1 은 예약 제약으로 거부. 시작 행을 미리 취소된 상태로 만들지 않고 실제 전이 경로(T3→T5→T8v→T14)로 간다.
- **T-LEASE-5** 늦은 정상 결과: T8v 뒤 같은 토큰의 정상 완료 → T9a `accepted`.
- **T-LEASE-6** 게이트 위치 증명: 컨텍스트 진입 **뒤**, 첫 애플리케이션 쓰기 직전에 시계를 `D_START` 넘게 전진 → 소켓 쓰기 0(가짜 네트워크 스트림의 write 기록으로 단언). TLS 핸드셰이크 쓰기는 게이트를 통과하지 않음. 변이: 게이트를 fence 직후(await 앞)로 옮김 · 게이트 제거 → RED.
- **T-LEASE-7** 느린 다단계 전송(각 단계는 httpx 단계 타임아웃 안, 합은 `D_SEND` 초과) → 종단 기한으로 취소, `uncertain`. 그 사이 V 가 T8v 해도 추가 송신 0, D 의 늦은 기록은 T8e/T9a.

### 7.6 reconcile·목록(§4.4–4.5)
- **T-LIST-1..3** 블록 누락·`[]`·13578·오류형·게이트웨이 오류·파싱 실패는 "없음"을 만들지 않음 · 빈 미체결 + 전체 조회 미체결 행 → `present`(kt00009) ·
  계속 신호 조합표(키 헤더/본문 × 계속 코드 × `Y/N/없음`)에서 키 없는 계속·반복 키는 불완전.
- **T-REC-1..4** 취소 종결에 자기 취소 ack 필요 · 정정 종결에 자기 정정 ack + 후속 행 필요 · 체결에 체결 조회 교차확인 필요 · 수량 합 불일치 → `unknown`.
- **T-CAND-1** uncertain(번호 증거 없음) + 관계없는 동일 속성 주문 1건이 목록에 등장 → 후보로만 기록, 상태 `uncertain` 유지, 예약 유지.
  변이: 유일 후보 자동 `accepted` → RED.
- **T-CAND-2** 번호 증거 + 같은 번호 목록 행 → T9b `accepted`. 번호는 같지만 속성 불일치 → `anomaly`.
- **T-CAND-3** V 의 T8v 뒤 D 가 아직 응답 대기 중(`dispatcher_done_at` NULL), R 이 후보 123 기록 → 소멸 증명 없는 `bind_candidate` 승인 행으로 T9h → 트리거 거부. D 가 늦게 번호 456 을 읽음 → T8e(`ack_evidence_order_id=456`, `dispatcher_done_at` 채움) → 123 으로 T9h → 거부(자기 증거 번호와 다름), 456 이 목록에 보이면 R 의 T9b. D 가 번호 없이 끝남(`dispatcher_done_at` 만 채움) → 123 으로 T9h 허용. `dispatcher_done_at` 이 채워진 뒤 D 의 추가 쓰기는 0행. `dispatcher_gone_proof=true` 승인 행이면 D 미완료여도 T9h 허용. 변이: T9h 의 dispatcher 조건 또는 증거 번호 일치 조건 제거 → RED.

### 7.7 게이트·계좌·호스트·스모크(회귀 방지)
- **T-GATE-1..3** `NHPLUG_MOCK_ENABLED` 정확히 `"true"` · FastMCP 와이어 `Client.call_tool` 에서 `0/1/"true"` 거부 · 계약 서브클래스·`dry_run=True`·`confirm` 비-True 거부.
- **T-ACCT-1..3** 손으로 만든 allowlist 불가 · 호출자 바인딩 allowlist 는 조회만 · 01/02·상충 type 거부.
- **T-HOST-1** build 후 호스트·포트·scheme·경로 변경 → T4, 송신 0 · 송신 후 3xx → `uncertain`.
- **T-SMOKE-1** 스모크 왕복: 단계별 상태 단언, unknown·미해결·기대 밖 상태에서 abort + 정리 취소, `ok` 를 내지 않음. 멱등키를 출력·재사용.

### 7.8 실행 규율
전체 스위트는 헤드당 한 번 `wrk heavy --` 로. tester 는 그 결과를 인용하고 커버 파일·자체 재현·자체 변이만 돈다. 장중 모의 스모크는 머지 후 operator-desk.

---

## 8. 열린 질문(재구현 착수 전 결정)

1. **Q1 HMAC 키 관리.** 서버 비밀 HKDF 파생 제안. 회전은 새 버전 추가뿐이고 옛 버전은 보존한다(§3.4). 비밀 저장소와 버전 목록의 배치는 운영자 결정.
2. **Q2 벤더 가정.** `itg_orr_no == mkt_orr_no`(KRX·비SOR), 정정이 새 번호를 주고 원수량을 `cor_qty` 로 옮김, 모의 호스트의
   `dailyOrderExecution`·`ost_cns_dit=1` 지원. 스모크 확인 전까지 실패는 전부 `unknown` 쪽.
3. **Q3 시간 상수.** 제안: `claim_window` 30 초, `D_START` 2 초, `D_SEND` 30 초, `D_CLOSE` 5 초, `L_SEND` 120 초, `T_intent_stale` 10 분,
   T14 유예 `G` = 다음 거래일 시작. recovery 는 reconcile 실행의 일부이며 별도 스케줄 없음(하드룰 6).
4. **Q4 운영자 절차.** T14·T9h·T15 CLI 인자, 승인 행 증적 형식, 두 사람 확인 여부, `nhplug_operator` 역할 자격증명의 보관. lease 호스트를 잃은 행(소멸 증명 불가)은 설계상 영구 차단이며, 이를 풀 절차가 필요하면 별도 운영자 결정으로 정한다.
5. **Q5 부분 UNIQUE 예약의 범위.** "종목·방향당 진행 중 하나"는 래더를 순차 실행으로 만든다. 모의계좌 범위에서는 안전을 우선한다.
6. **Q6 PR #2100 처리.** 승인 후 재구현 PR 을 새 브랜치로 만들고 #2100 을 닫을지, 갱신할지.
7. **Q7 dispatcher 배치.** 소멸 증명 (c) 는 호스트 수준 `/proc` 가시성이 필요하다. dispatcher 를 컨테이너에서 돌리면 T14 CLI 를 호스트 권한으로 돌릴 수 있어야 한다.
8. **Q8 성공 증명 코드.** 벤더에 주문 네 경로의 성공 `rsp_cd` 를 문서로 확인받으면 §4.3 표에 넣어 T6 을 켤 수 있다. 그 전에는 모든 주문이 첫 reconcile 까지 `uncertain`(예약 보유)이다 — 모의계좌 범위에서는 운영자가 수용 여부를 정한다.

## 9. 범위 밖

실계좌, 시장가·조건부·신용·예약·SOR/NXT 주문, 스케줄러 등록, 레인 allowlist·계좌 배정, 해외 주식, 게이트 완화.
