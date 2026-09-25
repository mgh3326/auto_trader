# #711 — NHPLUG 모의계좌 주문 송신 경로 상태 기계 (설계)

- 상태: **설계 문서 r2 — 코드 변경 0.** 재구현은 이 문서가 승인된 뒤 별도 PR 에서 한다.
- 입력: 운영자 결정 A(2026-09-26 — PR #2100 은 열린 채 보류, 송신 경로 설계를 먼저 쓴다),
  운영자 결정 `decision/2026-09-25/nhplug-stage2-mock`(모의계좌 한정 · 지정가 주문·정정·취소 · 레저·reconcile ·
  시장가/실계좌/스케줄러/배정 제외), 독립 tester 보고 r1–r4 의 BLOCKER 13건(`herdr-inbox/jobs/
  711-nhplug-stage2-20260925-1235/tester-report-r{1,2,3,4}.md`), 이 문서의 독립 리뷰 r1(`design-review-r1.md`,
  BLOCKER 5 · MAJOR 3).
- 개정 r2(리뷰 r1 + director-1 결정 반영): B1 송신 단계 lease·fence·프로세스 소멸 증명 없이는 재주문 차단을 풀지
  않음 · B2 인바운드 멱등키 + DB 예약 + 운영자 전용 "진짜 두 번째 주문" 경로 · B3 송신이 시작됐을 수 있는 이후의
  모든 예외는 `uncertain`(접수번호는 증거로만) · B4 부정 응답은 `uncertain`, "주문 없음 증명" 코드 목록은 비어 있음(벤더
  문서 인용) · B5 속성 일치는 운영자 확인 후보일 뿐 자동 바인딩 금지 · M6 같은 상태 자기 전이 표 · M7 INSERT 트리거와
  `IS DISTINCT FROM` 불변성 · M8 버전·길이 구분 digest 인코딩 + 골든 벡터(Python·PostgreSQL 17 일치 확인).
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
| **S1 상태 기계** | 행은 `intent → claimed → sending → {accepted, uncertain} → reconciled` 만 지난다(`rejected` 는 §4.3 의 빈 목록 때문에 현재 도달 불가). 합법 전이는 §2.3·§2.4 표가 전부이며 DB 트리거가 강제한다. |
| **S2 본문 = claim 행** | 본문을 정하는 모든 필드는 intent 행에 고정되고, 본문은 claim 된 행에서 순수 함수로만 만든다. 송신 API 는 수량·가격·범위·계좌를 받지 않는다. DB 생성 컬럼 `body_digest`(§3.3 인코딩·골든 벡터)가 intent 필드·claim 조건·전송 바이트를 묶는다. |
| **S3 송신 시작 가능 이후 = uncertain** | `sending` fence 커밋 이후의 모든 예외·비정상 결과(연결 종료·`aclose()`·취소·프로세스 사망·부정 응답 포함)는 `uncertain` 이다. 그 사이에 읽은 접수번호는 증거 컬럼에만 남는다. `withdrawn` 은 fence **이전**에만 가능하다. |
| **S4 단일 사용 claim** | claim 은 조건부 UPDATE 하나로만 얻고 즉시 커밋한다. 같은 행의 재생·새 요청 ID·두 번째 클라이언트·동시 호출·다른 digest·다른 계좌는 0행이다. 토큰·lease 신원은 되돌릴 수 없다. |
| **S5 행 단위가 아닌 요청 단위 멱등** | 호출자가 준 멱등키로 같은 요청은 같은 행으로 수렴한다. DB 부분 UNIQUE 로 (계좌, 날짜, 종목, 방향)당 진행 중·미해결 주문은 하나뿐이고, 같은 본문의 같은 날 재주문은 운영자 전용 경로로만 만든다. |
| **S6 부재는 증거가 아니다** | 빈 배열·블록 누락·13578·오류형·미완료 페이지는 "미체결 없음"이 아니다. 종결 상태는 양성 증거 두 개로만 기록한다. 속성만 같은 목록 행은 후보일 뿐이다. |
| **S7 차단 해제에는 송신자 소멸 증명이 필요하다** | 송신 단계에 들어간 행의 재주문 차단은 lease 만료 + lease 취소 + 보유 프로세스 소멸 증명 + 유예 시간이 모두 충족돼야 운영자가 풀 수 있다(§4.6). |
| **S8 DB 가 마지막 벽** | INSERT·UPDATE 트리거와 CHECK·부분 UNIQUE 가 전이·불변 필드·증거 필드·예약을 강제한다. 서비스 계층(하드룰 5)은 그대로이고 트리거는 두 번째 벽이다. |

---

## 1. 배경

PR #2100 은 독립 검증 4회에서 BLOCKER 13건을 받았고(§6.1), 이 문서 r1 은 리뷰에서 BLOCKER 5·MAJOR 3을 받았다
(§6.2). 공통 원인은 한 가지다 — **벤더에 멱등키가 없는데, 송신 경로의 안전 성질이 코드 위치마다 따로 조립돼 있었다.**
대표 사례(모두 `b945ccc76`):

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
| `accepted` | 송신 절차가 예외 없이 끝났고 `mkt_orr_no` 를 읽음, 또는 §4.5 증명 | 접수 | 아니오 | 아니오 |
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
| **D** dispatcher | claim·lease 보유자(송신 경로) | T3–T8, T9a |
| **R** reconcile | 목록 증거로만 판정. 수동 실행만(스케줄러 금지) | T9b, T11–T13, 자기 전이(후보 기록) |
| **V** recovery | reconcile 실행 시작부의 시간 기반 정리. 별도 스케줄 없음 | T10, T10i, T8v |
| **H** operator | 운영자 CLI(사유·증적 필수). MCP 로는 불가 | T9h, T14, T15(두 번째 주문 intent) |

### 2.3 상태 전이 — 이 표가 전부다

| # | 전이 | 주체 | 선행 조건(조건부 UPDATE/INSERT 에 포함) | 기록 |
|---|---|---|---|---|
| T1 | ∅ → `intent` | O | 게이트·검증(§5.1) 통과. INSERT 가 멱등키·예약·중복 UNIQUE 를 통과(§5.2) | 본문 필드, `account_binding`, `idempotency_key`, 새 `client_request_id`(DB 가 `body_digest` 생성) — 커밋 후 `(row_id, request_id, body_digest)` 반환 |
| T2 | `intent → withdrawn` | O | `state='intent' AND claim_token IS NULL` | `withdraw_reason` |
| T3 | `intent → claimed` | D | §5.3 claim SQL | `claim_token`, `claimed_at`, `claim_deadline`, lease 신원(host·pid·프로세스 시작 시각·boot id) |
| T4 | `claimed → withdrawn` | D | `state='claimed' AND claim_token=:t AND sending_at IS NULL` + 타입 `PreSendRefusal` | `withdraw_reason` |
| T5 | `claimed → sending` (**fence**) | D | §5.4 fence SQL(`state='claimed'`, 토큰, lease 미취소, `now() < claim_deadline`) | `sending_at`, `lease_expires_at` — 단독 커밋, 직후 §4.1 로컬 기한 검사 → `send()` |
| T6 | `sending → accepted` | D | `state='sending' AND claim_token=:t` + 송신 절차(컨텍스트 종료 포함)가 예외 없이 끝남 + `mkt_orr_no>0` 읽음 | `broker_order_id`, `ack_order_id` |
| T7 | `sending → rejected` | D | 위 + 응답 코드가 해당 엔드포인트의 "주문 없음 증명" 목록(§4.3, 현재 빈 집합)에 있음 | 응답 코드 |
| T8 | `sending → uncertain` | D | 그 밖의 모든 결과·예외 | `uncertain_reason`, 읽었으면 `ack_evidence_order_id` |
| T8v | `sending → uncertain` | V | `lease_expires_at < now()` | `uncertain_reason='lease_expired_without_result'` |
| T9a | `uncertain → accepted` | D | 같은 토큰으로 이어진 결과 기록이 T6 조건을 만족(예: T8v 뒤 늦게 끝난 정상 호출) | `broker_order_id`, `ack_order_id` |
| T9b | `uncertain → accepted` | R | §4.5(1) 자기 접수번호 증명: `ack_evidence_order_id` 와 **같은 번호**의 완전한 전체 조회 행 + 속성 일치 | `broker_order_id`, 증거 JSON |
| T9h | `uncertain → accepted` | H | §4.5(2) 운영자가 후보 행을 HTS/앱에서 이 요청의 주문으로 확인 | `broker_order_id`, 운영자 ID·사유 |
| T10 | `claimed → withdrawn` | V | `claim_deadline < now() AND sending_at IS NULL` | `withdraw_reason='claim_deadline_passed'` |
| T10i | `intent → withdrawn` | V | `claim_token IS NULL AND created_at < now() − T_intent_stale` | `withdraw_reason='stale_intent'` |
| T11 | `accepted/open/partially_filled → open/partially_filled/filled/cancelled/modified` | R | §4.4 양성 두 소스 | 증거 JSON, 수량 |
| T12 | 취소·정정 요청 행 `accepted → confirmed` | R | 자기 `ack_order_id` + 원주문 목록 행의 반영 수량 | 증거 JSON |
| T13 | 비종결 → `anomaly` | R | 모순 증거(심볼 불일치 등) | 수동 검토 |
| T14 | `uncertain → abandoned` | H | §4.6 차단 해제 조건 전부 | 운영자 ID·사유·증적 |

`T15`(운영자 전용 두 번째 주문)는 새 행의 T1 변형이다(§5.2 `duplicate_of`).

### 2.4 같은 상태 자기 전이(M6)

트리거는 `(old.state, new.state)` 가 §2.3 에 없으면 거부하되, 아래 자기 전이는 **표에 적힌 컬럼만** 바뀔 때 허용한다.

| 상태 | 바뀔 수 있는 컬럼 | 조건 |
|---|---|---|
| `intent`, `claimed`, `sending` | 없음 | — |
| `uncertain` | `candidate_order_ids`, `requires_manual_review`, `manual_review_reason`, `last_reconcile`, `reconcile_state`(verified 제외) | R 또는 H |
| `accepted`, `open`, `partially_filled` | `reconcile_state`, `last_reconcile`, `requires_manual_review`, `manual_review_reason`; 증거·수량 컬럼(`filled_qty`·`avg_fill_price`·`open_qty`·`cancelled_qty`·`evidence`)은 `NEW.reconcile_state='verified'` 일 때만 | R |
| 종결 상태 | 없음(`anomaly` 의 `manual_review_reason` 추가 기록만 H 허용) | — |

**금지(트리거 거부, 예시):** 표 밖의 모든 (old,new) 쌍, `sending/uncertain/accepted/… → withdrawn`, `→ intent`,
`intent → sending`, `intent → accepted`, `abandoned → *`, 종결 → 비종결.

### 2.5 그림

```
   T1(O) / T15(H)          T3(D)                 T5(D, fence)
 ∅ ─────────────▶ intent ─────────▶ claimed ─────────────────▶ sending
                   │ T2(O)            │ T4(D) / T10(V)            │ T6(D)           T8(D) / T8v(V)
                   │ T10i(V)          ▼                           ▼                     ▼
                   └─────────────▶ withdrawn                  accepted ◀─T9a/T9b/T9h── uncertain ──T14(H)──▶ abandoned
                                                                 │ T11/T12(R)
                                                                 ▼
                                   open · partially_filled · filled · cancelled · modified · confirmed · anomaly(T13)
```

### 2.6 프로세스 사망과 늦게 깨어난 dispatcher (B1)

| 멈춘/죽은 위치 | 남는 상태 | 이후 | 늦게 깨어나면 |
|---|---|---|---|
| T3 전 | `intent` | V: T10i | claim 조건 `state='intent'` 에서 0행 → 송신 0 |
| T3 후 · T5 전 | `claimed` | V: T10(`claim_deadline` 경과) | fence 조건 `state='claimed' AND now()<claim_deadline` 에서 0행 → 송신 0 |
| T5 커밋 결과 불명 | `claimed` 또는 `sending` | 커밋 확인 실패 시 **보내지 않는다**. `sending` 이면 V: T8v | — |
| T5 후 · `send()` 전(로컬 기한 검사 전) | `sending` | V: T8v(lease 만료) | 로컬 기한(§4.1) 초과로 **보내지 않고** T8 |
| 로컬 기한 검사 후 · 소켓 쓰기 전(수 μs) | `sending` | V: T8v | 보낼 수 있다 — **그래서** T14 는 보유 프로세스 소멸 증명을 요구한다(§4.6) |
| `send()` 중·후 | `sending` | V: T8v | 결과 기록은 T9a 로만 |

---

## 3. 브로커 본문의 claim 행 결속 (요구 2)

### 3.1 필드 표

| 본문을 정하는 것 | 벤더 키(`Input_0`) | 컬럼·타입·제약 | digest 태그 | T3 에서 | 전송 바이트 재검사 |
|---|---|---|---|---|---|
| 조작 종류 | 경로 | `operation_kind` text CHECK `place/modify/cancel` | `op` | digest 일치·RETURNING | 경로 정확 일치 |
| 방향 | 경로(매수/매도) | `side` text **NOT NULL** CHECK `buy/sell`(정정·취소는 원주문 방향, T1 목록에서) | `side` | 〃 | 경로 |
| 종목 | `iem_cd` | `symbol` text CHECK `^[0-9]{6}$` | `sym` | 〃 | ✔ |
| 수량 | `orr_qty`/`cor_qty` | `quantity` **bigint** CHECK `>0`; 전량 취소만 NULL | `qty` | 〃 | ✔ |
| 지정가(KRW 정수) | `orr_pr`/`cor_pr` | `price` **bigint** CHECK `>0`; 취소는 NULL | `px` | 〃 | ✔ |
| 원주문번호 | `org_mkt_orr_no` | `original_order_id` text CHECK `^[1-9][0-9]{0,9}$`; `place` 는 NULL | `org` | 〃 | ✔ |
| 정정·취소 범위 | `all_pat_dit_cd`(1/2) | `amend_scope` text CHECK `full/partial`; `place` 는 NULL; 취소 `full ⇔ quantity IS NULL` | `scope` | 〃 | ✔ |
| 계좌 | `act_no` | `account_binding` text CHECK `^[0-9a-f]{64}$`(HMAC, §3.4) | `acct` | WHERE 직접 + digest | HMAC 재계산 |
| 고정 상수 | `nmn_pr_tp_cd=01`·`orr_cnd_dit_cd=00`·`ssl_nmn_pr_dit_cd=00`·`rmt_mkt_cd=KRX`·`sor_mkt_sli_yn=N`·`sop_cnd_pr=0` | `body_schema_version` smallint CHECK `=1`(버전이 상수 집합을 고정) | 접두 `nhplug-body-v1` | 〃 | 지정가 전용 형태 |
| 요청 ID | (벤더 본문에 없음) | `client_request_id` uuid UNIQUE | ✗ | WHERE 직접 | — |
| 멱등키 | (벤더 본문에 없음) | `idempotency_key` text CHECK `^[A-Za-z0-9_-]{16,64}$` | ✗ | — | — |

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
  `태그=바이트길이:값;`, NULL 은 `태그=-;` 로 이어 붙인 것. 정수는 부호·앞자리 0·소수점 없는 십진 표기. 요청 ID·멱등키는 포함하지 않는다.
- `body_digest` = 정규 바이트열의 SHA-256 소문자 hex(64자). DB 에서는 생성 컬럼이다:

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
-- body_digest text GENERATED ALWAYS AS (review.nhplug_body_digest_v1(operation_kind, side, symbol,
--   quantity, price, original_order_id, amend_scope, account_binding)) STORED
```

- 골든 벡터(`acct` = `0123456789abcdef` × 4). 아래 digest 는 Python 구현과 PostgreSQL 17 생성 컬럼(위 SQL)에서 **둘 다
  같게 나오는 것을 일회용 DB 로 확인**했다. 재구현은 이 표를 테스트 고정값으로 쓴다.

| # | op | side | sym | qty | px | org | scope | body_digest |
|---|---|---|---|---|---|---|---|---|
| V1 | place | buy | 005930 | 1 | 67400 | NULL | NULL | `c50a25c0482954e41a8e9313c8556c71b99ee33749ca4a5341f94f6957941266` |
| V2 | place | sell | 005930 | 3 | 71000 | NULL | NULL | `9ac76bf5887dd8b39bd0428c1b74fdc2229b00de310361d5f58c75fb4ad432a6` |
| V3 | modify | buy | 005930 | 1 | 67000 | 1000123 | full | `c497547afdf9fbb421e58ffbd14750f7796a91756c512509005ebbd7d9989a43` |
| V4 | modify | buy | 005930 | 1 | 67000 | 1000124 | partial | `b37a023c5747807f497938e03403ca9c3bb455573030852c9bba6148b6e4ba83` |
| V5 | cancel | buy | 005930 | NULL | NULL | 1000130 | full | `9117f83d3a28264b7f76105182167210349aa9ffba6b34ef9d260f5aa1c94726` |
| V6 | cancel | buy | 005930 | 2 | NULL | 1000131 | partial | `d9859c9828409743187b4f6a293b562e3cf93035096de38a59a5c84daccb7c3c` |

  V1 의 정규 바이트열(검증용): `nhplug-body-v1|op=5:place;side=3:buy;sym=6:005930;qty=1:1;px=5:67400;org=-;scope=-;acct=64:0123…cdef;`
- 스키마 버전을 올리면 새 함수 `…_v2` 와 새 벡터를 추가한다. 기존 행의 digest 는 바뀌지 않는다.

### 3.4 계좌 바인딩

- 계좌번호는 저장하지 않는다. `account_binding = HMAC-SHA256(K_bind, act_no)` 소문자 hex. `K_bind` 는 서버 비밀에서
  HKDF(label `nhplug-account-binding-v1`)로 파생하며 로그·응답에 나오지 않는다(Q1).
- `act_no` 는 **claim 범위의 값**이다: D 는 이번 송신에서 `/n2/acctinfo` 로 검증한 `acct_type=03` 계좌번호의 HMAC 을
  T3 WHERE 에 넣고, 일치한 경우에만 그 계좌번호로 본문을 만든다(T3 RETURNING 이 계좌번호를 돌려주지 않는다).
- 전송 바이트 재검사: 디코드한 `act_no` 의 HMAC 을 다시 계산해 넣은 정규 바이트열의 digest 가 행의 `body_digest` 와 같아야 한다.

---

## 4. 불확실성 (요구 3)

### 4.1 경계와 fence

"송신이 시작됐을 수 있는 순간" = **T5 fence 커밋 이후**다. 순서는 규범이다.

```
claim   = ledger.claim(row_id, request_id, intent_digest, binding, lease_identity)  # T3; 0행 → ClaimRejected(행 불변)
row     = claim.row
body    = build_body(row, verified_act_no)                     # 순수
request = httpx.Request("POST", MOCK_BASE_URL + path, json={"Input_0": body}, headers=...)
check_all_pre_send(row, body, request)                         # host·port·scheme·path·act_no·지정가·digest; 실패 → T4
t0      = monotonic()
fence   = ledger.fence(claim.token)                            # T5 단독 커밋; 결과 불명·0행 → 보내지 않고 종료
if monotonic() - t0 > D_LOCAL: record(T8, "fence_deadline_exceeded_not_sent"); return   # fence 재확인(로컬)
parsed  = None
try:                                                           # ← 이 줄부터 모든 것이 uncertain 경계
    async with transport(follow_redirects=False, timeout=HTTP_TIMEOUT) as http:     # 생성·종료 모두 경계 안
        response = await http.send(request)
        parsed   = read_and_parse(response)                    # 접수번호를 읽으면 evidence 에만 보관
    outcome = ACCEPTED if parsed.order_no else UNCERTAIN(parsed.reason)
except BaseException as exc:
    outcome = UNCERTAIN(category(exc), ack_evidence=parsed.order_no if parsed else None)
    await shield(ledger.record(claim.token, outcome))          # T8 (조건: state='sending' AND 토큰)
    raise
await ledger.record(claim.token, outcome)                      # T6 또는 T8
```

- `D_LOCAL`(예: 1 초)은 fence 커밋 왕복 이후 `send()` 까지 허용하는 로컬 기한이다. fence 는 `lease_expires_at = now() +
  L_SEND` 를 쓰고, `HTTP_TIMEOUT`(연결+쓰기+풀+읽기 합) + `D_LOCAL` + 여유 < `L_SEND` 가 되게 정한다(Q3). 살아 있는
  프로세스는 lease 만료 뒤에 소켓 쓰기를 시작할 수 없다. 로컬 검사와 소켓 쓰기 사이에서 **프로세스 자체가 멈추는**
  경우만 남으며, 그 경우는 §4.6 의 소멸 증명이 막는다.
- `D_LOCAL` 초과처럼 실제로 `send()` 를 부르지 않았어도 fence 이후라면 `uncertain` 으로 기록한다(S3 단일 규칙). 사유가
  `…_not_sent` 이므로 운영자가 §4.6 절차로 빨리 닫을 수 있다.

### 4.2 분류 표(B3)

| 발생 지점 | 예 | 결과 |
|---|---|---|
| T1 이전 | 게이트·confirm·시장가·형식·멱등키 충돌·예약 충돌·레저 불가 | 행 없음(또는 기존 행 반환) |
| T3 실패 | 이미 claim·요청 ID 다름·digest·바인딩 불일치 | `ClaimRejected` — **행 불변** |
| T3~T5 | 호스트·계좌·지정가·digest 재검사 실패, 토큰 발급 실패 | T4 `withdrawn` |
| T5 결과 불명 | 커밋 중 DB 오류 | 보내지 않음. `sending` 이면 V: T8v |
| T5 이후 전부 | 로컬 기한 초과, 타임아웃, 연결 거부·리셋, TLS, 3xx(리다이렉트 금지), 4xx/5xx, 읽기 오류, JSON 아님, 객체 아님, 부정 응답 코드(§4.3), 성공 코드+번호 없음, **컨텍스트 종료·`aclose()` 오류(번호를 이미 읽었어도)**, `CancelledError`·`KeyboardInterrupt`·`SystemExit`, 알 수 없는 예외 | T8 `uncertain` (+ 읽은 번호는 `ack_evidence_order_id`) |
| T5 이후 정상 완료 + 번호 | 컨텍스트 종료까지 예외 없음 | T6 `accepted` |
| 프로세스 사망 | kill, OOM | V: T8v |

- operations 의 분류는 **`ClaimRejected`·`PreSendRefusal` 만 송신 전**이고 그 밖은 전부 uncertain 이다(`operations.py:467`
  의 반대). O 는 fence 이후 행을 `withdrawn` 으로 만들 수 없다(트리거).
- 호출자 응답: `uncertain` 은 `success=false, status="uncertain", reconcile_required=true, retry_allowed=false` 와
  `ack_evidence_order_id`(있으면), 멱등키를 돌려준다. 같은 멱등키 재호출은 같은 행을 돌려줄 뿐 보내지 않는다(§5.2).

### 4.3 부정 응답(B4)

- 엔드포인트별 "주문이 생성되지 않았음을 증명하는 코드" 목록 `NO_ORDER_PROOF_CODES[path]` 를 둔다. **현재 네 경로 모두
  빈 집합**이다. 근거: 벤더 명세 `x-response-status`(서문 인용)와 네 주문 경로의 응답 정의에 그런 코드가 없고, 벤더 SDK
  가 코드 판정을 명시적으로 거부한다. 코드를 추가하려면 벤더 문서의 인용(또는 벤더 확인서)과 운영자 승인을 함께 남긴다.
- 따라서 비성공 코드 + 번호 없음은 `uncertain` 이다(T7 은 목록이 비어 있는 동안 도달 불가). 운영상 결과: 주문가능금액 부족 같은
  확실해 보이는 거부도 §4.5–4.6 절차로 닫힐 때까지 그 종목·방향의 새 주문을 막는다 — 멱등키 없는 벤더에서 중복 송신보다 나은 쪽이다.

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
- 수량 산술: `filled + open + cancelled + modified == order_qty` 가 아니면 `unknown`. 미체결 조회에 남아 있는데 전체 조회가
  종결이라 하면 `source_disagreement`.

### 4.5 uncertain 해소(B5)

1. **자기 접수번호 증명(T9b, 자동 허용).** 행에 `ack_evidence_order_id` 가 있고, 완전한 전체 조회에 **바로 그 번호**의 행이
   있으며 종목·방향·수량·가격·원주문이 일치하면 `accepted`. 번호는 우리 요청의 응답에서 읽은 것이고 목록이 같은 번호를
   보이므로 양성 두 소스다.
2. **속성 후보(자동 바인딩 금지).** 번호 증거가 없으면 속성(종목·방향·수량·가격·원주문·계좌 범위, 주문 시각이 `[sending_at −
   ε, sending_at + HTTP_TIMEOUT]` 안)이 같은 행은 `candidate_order_ids` 에 **기록만** 한다(자기 전이, M6). 후보가 하나여도
   자동으로 `accepted` 가 되지 않는다. 운영자가 HTS/앱에서 그 주문이 이 요청임을 확인한 경우에만 T9h.
3. 후보가 없거나 여럿이면 `uncertain` 을 유지하고 수동 검토를 표시한다. "보이지 않음"은 "보내지 않음"이 아니다.

### 4.6 재주문 차단 해제(B1)

재주문 차단은 §5.2 의 부분 UNIQUE 예약이다(`intent·claimed·sending·uncertain` 행이 예약을 보유). 예약에서 나가는 길과 조건:

| 나가는 전이 | 왜 안전한가 |
|---|---|
| T2/T10i `intent → withdrawn` | claim 이 없다. 이후 claim 은 `state='intent'` 조건에서 0행 |
| T4/T10 `claimed → withdrawn` | fence 가 없다. fence 는 `state='claimed' AND now()<claim_deadline` 조건이라 0행 |
| T6/T9a/T9b/T9h `→ accepted` | 주문이 존재함이 확인됐다. 같은 본문 재주문은 여전히 §5.2 중복 규칙에 막힌다 |
| **T14 `uncertain → abandoned`** | 아래 **모두** 충족 시에만(H): ① `lease_expires_at + G < now()`(유예 G, 예: 다음 거래일 시작) ② lease 취소가 먼저 커밋됨(`lease_revoked_at`) ③ lease 보유 프로세스 소멸 증명 — 같은 호스트면 기록된 `(boot_id, pid, 프로세스 시작 시각)` 조합이 더 이상 존재하지 않음을 도구가 확인, 다른 호스트면 운영자 증적 ④ 완전한 전체 조회(가능하면 세션 종료 후)에 자기 번호 증명·후보 확인이 없음 ⑤ 사유 기록 |

- 시간만으로 풀리는 경로는 없다. V 의 T8v 는 `uncertain` 으로 옮길 뿐 예약을 유지한다.
- lease 취소는 `sending` 과 `uncertain` 모두에 걸 수 있고, 취소된 lease 로는 fence·T9a 외의 어떤 쓰기도 할 수 없다.

---

## 5. 멱등·claim·fence·DB 강제 (요구 4)

### 5.1 T1 이전 전제(현 PR 에서 확정)

MCP 인자 `StrictBool`/`StrictInt`(JSON `0/1/"true"` 거부) · `NHPLUG_MOCK_ENABLED` 정확히 `"true"` · per-call `dry_run=False` +
`confirm=True`(정확한 타입·값, 계약 서브클래스 거부) · 지정가 전용 · `MockAccountAllowlist` 는 `/n2/acctinfo` 파싱으로만 생성 ·
주문은 해당 클라이언트 자신의 acctinfo 검증 계좌로만 · 모의 호스트 고정 + build 후 scheme·host·port·path·act_no 재검증 ·
`follow_redirects=False`.

### 5.2 인바운드 멱등키와 DB 예약(B2)

- **멱등키.** `dry_run=False` 인 모든 주문 요청은 호출자가 정한 `idempotency_key`(`^[A-Za-z0-9_-]{16,64}$`)를 **필수**로 가진다.
  MCP consumer 는 첫 시도 전에 키를 정하고 재시도에 같은 키를 쓴다. 스모크 CLI 는 키를 생성해 출력하고 `--idempotency-key` 로 재사용한다.
- **DB 제약.**
  - `UNIQUE (account_binding, idempotency_key)` — 같은 키는 같은 행. 같은 키 + 같은 `body_digest` 재호출은 기존 행 상태를 반환하고
    **보내지 않는다**(발송은 최초 호출자만). 같은 키 + 다른 digest 는 `idempotency_key_conflict`.
  - 부분 `UNIQUE (account_binding, order_date, symbol, side) WHERE state IN ('intent','claimed','sending','uncertain')` — 진행 중·미해결
    주문은 종목·방향당 하나. 동시 T1 두 개 중 하나는 반드시 실패한다(`in_flight_order_exists` + 막는 행 ID).
  - 부분 `UNIQUE (order_date, body_digest, duplicate_ordinal) WHERE state <> 'withdrawn'` + `CHECK ((duplicate_ordinal = 0) = (duplicate_of IS NULL))`
    — 같은 계좌·같은 본문의 같은 날 두 번째 주문은 `duplicate_ordinal ≥ 1` 이어야 하고, 그 값은 운영자 경로에서만 채운다.
- **운영자 전용 "진짜 두 번째 주문"(T15).** 운영자 CLI `--second-order-of <row_id> --confirm-second-order` 만이 `duplicate_of=<row_id>`,
  `duplicate_ordinal = 참조 행 그룹의 최대값 + 1` 을 넣는다(참조 행 `FOR UPDATE`). MCP 도구는 이 컬럼을 받지 않는다(정적 가드).
  이 경로도 진행 중 예약 제약은 우회하지 않는다.
- T1 SQL 은 `INSERT … ON CONFLICT DO NOTHING RETURNING id` 후, 0행이면 어떤 제약에 걸렸는지 조회해 위 오류 중 하나를 돌려준다.

### 5.3 T3 claim

```sql
UPDATE review.nhplug_mock_order_ledger
   SET state = 'claimed', claim_token = :new_token, claimed_at = now(),
       claim_deadline = now() + :claim_window,
       lease_host = :host, lease_pid = :pid, lease_process_started_at = :proc_start, lease_boot_id = :boot_id
 WHERE id = :row_id
   AND client_request_id = :request_id
   AND state = 'intent' AND claim_token IS NULL
   AND account_binding = :binding_of_verified_account
   AND body_digest = :intent_digest
RETURNING operation_kind, side, symbol, quantity, price, original_order_id, amend_scope,
          body_schema_version, account_binding, body_digest, claim_token;
```

D 는 RETURNING 필드로 Python 정규 바이트열을 만들어 digest 가 반환값과 같은지 확인한다(정규화 드리프트 방지). 다르면 T4.

### 5.4 T5 fence 와 결과 기록

```sql
-- T5 fence (단독 트랜잭션; 커밋 확인 후 §4.1 로컬 기한 검사 → send)
UPDATE review.nhplug_mock_order_ledger
   SET state = 'sending', sending_at = now(), lease_expires_at = now() + :l_send
 WHERE id = :row_id AND claim_token = :token AND state = 'claimed'
   AND sending_at IS NULL AND lease_revoked_at IS NULL AND now() < claim_deadline
RETURNING id;

-- T6/T8 (D), T9a (D)
UPDATE review.nhplug_mock_order_ledger SET state = :result, ...
 WHERE id = :row_id AND claim_token = :token AND state IN ('sending', 'uncertain');   -- uncertain 은 T9a 만
```

### 5.5 DB 강제(M7)

| 장치 | 내용 |
|---|---|
| UNIQUE | `client_request_id`, `claim_token`, `(order_date, broker_order_id)`, `(account_binding, idempotency_key)`, §5.2 부분 UNIQUE 2개 |
| CHECK | `state` 목록 · §3.1 컬럼 형식 · `(claim_token IS NULL) = (claimed_at IS NULL)` · `state='intent' ⇒ claim_token IS NULL` · `state NOT IN ('intent','withdrawn') ⇒ claim_token IS NOT NULL` · `sending_at IS NOT NULL ⇒ claim_token IS NOT NULL AND lease_expires_at IS NOT NULL` · `state IN ('sending','accepted','uncertain','abandoned','open','partially_filled','filled','cancelled','modified','confirmed') ⇒ sending_at IS NOT NULL` · `state='withdrawn' ⇒ sending_at IS NULL` · `amend_scope`·취소 조합 · `ack_order_id IS NULL OR ack_order_id = broker_order_id` · `filled_qty ≤ quantity` · 지정가·KRX·모의 고정 |
| **BEFORE INSERT 트리거** | `state='intent'` 만. claim·fence·lease·결과·증거·수량·후보 컬럼 전부 NULL, `reconcile_state='pending'`, `requires_manual_review=false`. `duplicate_*` 는 CHECK + 참조 행 존재 |
| **BEFORE UPDATE 트리거** | (old,new) 상태 쌍이 §2.3 에 없고 §2.4 자기 전이도 아니면 거부 · 본문 필드·`account_binding`·`idempotency_key`·`client_request_id`·`duplicate_*` 는 `NEW.x IS DISTINCT FROM OLD.x` 이면 거부(NULL→값 포함) · `claim_token`·`claimed_at`·`claim_deadline`·lease 신원·`sending_at`·`lease_expires_at` 은 NULL 에서 한 번만 채워지고 이후 불변 · `lease_revoked_at` 은 NULL→값 한 번만 · 증거·수량 컬럼은 `NEW.reconcile_state='verified'` 일 때만 변경 · 종결 상태 불변 |
| DELETE | 거부 |

### 5.6 동시성·세션

- READ COMMITTED 에서 같은 행의 두 T3·두 T5 는 행 잠금으로 직렬화되고 뒤 문장은 재평가 후 0행이다. 서로 다른 행의 동시 T1 은
  §5.2 부분 UNIQUE 로 직렬화된다.
- claim·fence·결과 기록은 **dispatcher 전용 세션**의 짧은 트랜잭션이다. 호출자 트랜잭션에 섞지 않는다.

### 5.7 재전송·중복 표

| 시도 | 막는 장치 | 송신 |
|---|---|---|
| 같은 행 재생(같은 요청 ID) | T3 `state='intent'` | 0 |
| 같은 행 새 요청 ID | T3 `client_request_id` | 0 |
| 두 번째 클라이언트·프로세스 | T3 DB 조건 | 0 |
| 같은 행 동시 두 호출 | 행 잠금 | 1 |
| 같은 멱등키 재시도(응답 유실 후 포함) | `UNIQUE (account_binding, idempotency_key)` → 기존 행 반환 | 0 추가 |
| 다른 멱등키·같은 종목·방향, 앞 주문 진행 중·미해결 | 부분 UNIQUE 예약 | 0 |
| 동시 T1 두 개 | 부분 UNIQUE | 행 1개 |
| 앞 주문 `accepted` 뒤 같은 본문 새 키 | `(order_date, body_digest, duplicate_ordinal)` | 0 — 운영자 T15 로만 |
| 수량·가격·범위 변경 | 송신 API 에 인자 없음 + 불변 트리거 + digest | 0 |
| 다른 계좌 | 바인딩 불일치 | 0 |
| fence 뒤 멈췄다 깨어난 송신자 | 로컬 기한(살아 있는 경우) · §4.6 소멸 증명 전 예약 유지 | 그 행 외 추가 송신 0 |

---

## 6. 결함 ↔ 방지 규칙 (요구 5)

### 6.1 PR #2100 BLOCKER 13건

| # | 라운드 | 결함 | 방지 규칙 | 테스트(§7) |
|---|---|---|---|---|
| 1 | r1 | MCP JSON `0/1/"true"` 가 `dry_run`/`confirm` 으로 변환 | §5.1 Strict 스키마 | T-GATE-2 |
| 2 | r1 | 손으로 만든 allowlist 로 실계좌 바인딩 | §5.1 acctinfo 전용 생성·자기 검증 계좌 + §3.4 바인딩 | T-ACCT-1..3 |
| 3 | r1 | 디스패처 권한 미검사·계약 서브클래스 | §5.1 정확한 타입·값 검사(진입과 T3 직전) | T-GATE-3 |
| 4 | r1 | 빈/미완료 목록을 "없음"·종결로 | S6, §4.4, §4.5(부재·속성만으로 해소 금지) | T-LIST-1..3, T-CAND-1 |
| 5 | r1 | 모순 수량을 검증된 체결로 | §4.4 수량 산술 | T-REC-4 |
| 6 | r1 | `unknown` 상태로 체결 수량 기록 | §5.5 INSERT·UPDATE 트리거(증거 필드는 verified 에서만, INSERT 시 NULL) | T-DB-3, T-DB-4 |
| 7 | r1 | 스모크 왕복이 unknown·미해결에도 ok | §7 T-SMOKE | T-SMOKE-1 |
| 8 | r2 | 빈 미체결 조회 + 증인 행을 "없음"·종결 근거로 | S6, §4.4 양성 두 소스 | T-LIST-2, T-REC-1..3 |
| 9 | r2 | 모순된 계속 메타데이터를 마지막 페이지로 | §4.4 페이지 규칙 | T-LIST-3 |
| 10 | r2 | 커밋된 레저 행 없이 송신 | S1·S4 claim 된 행에서만 송신 | T-CLAIM-1 |
| 11 | r3 | 복제 가능한 in-process intent·로컬 소비 | S4 DB claim + S2 + S5 멱등키·예약 + 불변 트리거 | T-CLAIM-2..6, T-IDEM-1..5 |
| 12 | r4 | 정정 범위가 claim 밖 인자 | §3.1 `amend_scope` 고정·digest·바이트 재검사, 인자 제거 | T-BODY-2, T-CLAIM-5 |
| 13 | r4 | POST 후 `aclose()` 오류가 `not_submitted` | S3·§4.1–4.2 경계·분류 반전·트리거 | T-UNC-1..4 |

### 6.2 설계 리뷰 r1 지적 8건

| 지적 | 반영 | 테스트 |
|---|---|---|
| B1 fence 뒤 멈춘 송신자가 복구·운영자 종결 후 송신 | §4.1 fence + 로컬 기한 + lease, §4.6 T14 조건(lease 만료·취소·프로세스 소멸 증명·유예), §2.6 | T-LEASE-1..5 |
| B2 새 요청 ID 마다 새 행 → 같은 업무 요청 두 번 송신 | §5.2 필수 멱등키 + 부분 UNIQUE 예약 + 중복 서수, 운영자 T15 | T-IDEM-1..5 |
| B3 `aclose()` 뒤 `accepted` 유지가 운영자 요구 3 과 충돌 | §4.2: fence 이후 예외는 전부 `uncertain`, 번호는 `ack_evidence_order_id` 로만 | T-UNC-2 |
| B4 증명 없는 부정 코드로 `rejected` 종결 | §4.3 `NO_ORDER_PROOF_CODES` 빈 집합(벤더 문서 인용) → `uncertain` | T-NEG-1 |
| B5 속성만으로 남의 주문 바인딩 | §4.5 후보 기록만, 자동 바인딩은 자기 번호 증명(T9b)뿐, 그 밖은 운영자 T9h | T-CAND-1..2 |
| M6 같은 상태 기록 불가 | §2.4 자기 전이 표 | T-SM-3 |
| M7 INSERT·NULL→값 구멍 | §5.5 BEFORE INSERT + `IS DISTINCT FROM` | T-DB-3..5 |
| M8 digest 인코딩 모호 | §3.3 버전·길이 구분 인코딩, bigint 가격, ASCII CHECK, 골든 벡터, §3.4 계좌의 claim 범위 | T-DIGEST-1..3 |

---

## 7. 재구현 필수 테스트 (요구 6)

가짜 전송·가짜 브로커·자체 일회용 DB 만 쓴다(실 NH 호출 0). **각 규칙 테스트는 그 규칙을 되돌리는 변이에서 단언 실패로
RED** 여야 하며, 변이는 테스트 파일에 기계적으로 선언한다(현 PR `test_stage2_mutants.py` 방식).

### 7.1 상태 기계·DB
- **T-SM-1** §2.3 합법 전이 전부 성공, 표 밖 (old,new) 쌍 전부 트리거 거부(쌍 전수 열거).
- **T-SM-2** 불변 필드 변경·NULL 복귀·NULL→값 거부, DELETE 거부.
- **T-SM-3** §2.4 자기 전이: 허용 컬럼만 바뀌면 통과, 그 밖 컬럼 하나라도 바뀌면 거부(상태별).
- **T-DB-1..2** §5.5 CHECK·UNIQUE 위반 INSERT/UPDATE 가 `IntegrityError`.
- **T-DB-3** INSERT 에 증거·수량·claim·lease·후보 값이 있으면 거부.
- **T-DB-4** `reconcile_state<>'verified'` 로 증거·수량 변경 거부.
- **T-DB-5** 본문 필드 NULL→값(예: `amend_scope` 나중 채우기) 거부.
- **T-MIG-1** 마이그레이션 upgrade → ORM 과 제약·인덱스·트리거·함수까지 동일 → downgrade → upgrade(일회용 DB).

### 7.2 digest(§3.3)
- **T-DIGEST-1** 골든 벡터 V1–V6: Python 인코더와 DB 생성 컬럼이 표의 값을 정확히 낸다.
- **T-DIGEST-2** 각 필드 하나만 바꾸면 digest 가 바뀐다(8 필드 × 대표값). NULL 과 빈 문자열이 구별된다.
- **T-DIGEST-3** 정규화 드리프트 주입(Python 인코더 변이) → T3 후 RETURNING 재계산 불일치 → T4, 송신 0.

### 7.3 claim·본문(§3, §5.3)
- **T-CLAIM-1** 레저 행 없음·가짜 레저·레저 서브클래스 → 토큰·소켓 0.
- **T-CLAIM-2..4** 같은 행 재생·새 요청 ID·두 번째 클라이언트+세션: 송신 1회, 본문 = 커밋 본문.
- **T-CLAIM-5** 필드·`amend_scope` 직접 변경(트리거 거부), 다른 digest(0행), 다른 계좌(0행) → 송신 0, 이후 정상 1회.
- **T-CLAIM-6** 같은 행 동시 두 dispatcher(별도 세션) → 1회. 변이: WHERE 에서 상태·토큰 조건 제거 → RED.
- **T-BODY-1** 매수·매도·정정(전량/일부)·취소(전량/일부) 전송 바이트 == `build_body(claim 행)`, digest == 골든 벡터 규칙.
- **T-BODY-2** 원주문 5주·일부 1주 정정은 `all_pat_dit_cd=2`·`cor_qty=1` 로만 나간다. 송신 API 에 범위 인자 없음(정적 가드).
- **T-BODY-3** 빌드 후 바이트 변조(수량·가격·종목·범위·계좌·호가 05·SOR·IOC) → T4, 송신 0.

### 7.4 멱등·예약(§5.2)
- **T-IDEM-1** 같은 멱등키 재호출(첫 호출의 응답 유실 후 포함) → 같은 행 반환, 송신 추가 0.
- **T-IDEM-2** 같은 키 다른 본문 → `idempotency_key_conflict`.
- **T-IDEM-3** 다른 키·같은 종목·방향, 앞 행이 `intent`/`claimed`/`sending`/`uncertain` 각각 → `in_flight_order_exists`, 송신 0.
- **T-IDEM-4** 동시 T1 두 개(별도 세션, `gather`) → 행 1개.
- **T-IDEM-5** 앞 주문 `accepted` 뒤 같은 본문 새 키 → 거부. 운영자 `--second-order-of` 로만 `duplicate_ordinal=1` 행 생성·송신 1회. MCP 는 이 필드를 받지 않는다.

### 7.5 uncertain·fence·lease(§4)
- **T-UNC-1** fence 이후 타임아웃·리셋·3xx·4xx·5xx·비JSON·객체 아님·성공 코드+번호 없음 → 전부 `uncertain`, `retry_allowed=false`.
- **T-UNC-2** POST 후 `aclose()` 예외: 번호를 읽기 전이면 `uncertain`, **읽은 뒤여도 `uncertain` + `ack_evidence_order_id`**, 호출자 응답 `success=false`,
  같은 멱등키 재호출은 송신 0. 변이: 컨텍스트 종료를 경계 밖으로 · 번호 읽음 시 `accepted` 유지 → 각각 RED.
- **T-UNC-3** fence 이후 알 수 없는 예외 → `uncertain`(분류 반전). 변이: "그 밖 = 송신 전" 복원 → RED.
- **T-UNC-4** `CancelledError`/`KeyboardInterrupt` 가 `send()` 중 → `uncertain` 기록 후 재발생.
- **T-NEG-1** 가짜 브로커가 주문을 기록한 뒤 비성공 코드·번호 없음 응답 → `uncertain`(`rejected` 아님), `NO_ORDER_PROOF_CODES` 가 빈 집합임을 단언.
- **T-LEASE-1** T5 커밋 결과 불명(커밋 예외 주입) → 송신 0, 이후 V: T8v.
- **T-LEASE-2** T3 후 T5 전 멈춤 → V: T10 → 깨어난 dispatcher 의 fence 0행, 송신 0.
- **T-LEASE-3** fence 후 `send()` 전 멈춤 + `D_LOCAL` 초과 → 보내지 않고 `uncertain(…_not_sent)`.
- **T-LEASE-4** fence 후 멈춤 → V: T8v → H 가 T14 를 시도: lease 미만료·미취소·보유 프로세스 생존(같은 호스트 pid/시작 시각 일치)·유예 미경과 각각에서 거부.
  모두 충족하면 허용. 이 동안 같은 종목·방향 새 T1 은 예약 제약으로 거부.
- **T-LEASE-5** 늦은 정상 결과: T8v 뒤 같은 토큰의 정상 완료 → T9a `accepted`.

### 7.6 reconcile·목록(§4.4–4.5)
- **T-LIST-1..3** 블록 누락·`[]`·13578·오류형·게이트웨이 오류·파싱 실패는 "없음"을 만들지 않음 · 빈 미체결 + 전체 조회 미체결 행 → `present`(kt00009) ·
  계속 신호 조합표(키 헤더/본문 × 계속 코드 × `Y/N/없음`)에서 키 없는 계속·반복 키는 불완전.
- **T-REC-1..4** 취소 종결에 자기 취소 ack 필요 · 정정 종결에 자기 정정 ack + 후속 행 필요 · 체결에 체결 조회 교차확인 필요 · 수량 합 불일치 → `unknown`.
- **T-CAND-1** uncertain(번호 증거 없음) + 관계없는 동일 속성 주문 1건이 목록에 등장 → 후보로만 기록, 상태 `uncertain` 유지, 예약 유지.
  변이: 유일 후보 자동 `accepted` → RED.
- **T-CAND-2** 번호 증거 + 같은 번호 목록 행 → T9b `accepted`. 번호는 같지만 속성 불일치 → `anomaly`.

### 7.7 게이트·계좌·호스트·스모크(회귀 방지)
- **T-GATE-1..3** `NHPLUG_MOCK_ENABLED` 정확히 `"true"` · FastMCP 와이어 `Client.call_tool` 에서 `0/1/"true"` 거부 · 계약 서브클래스·`dry_run=True`·`confirm` 비-True 거부.
- **T-ACCT-1..3** 손으로 만든 allowlist 불가 · 호출자 바인딩 allowlist 는 조회만 · 01/02·상충 type 거부.
- **T-HOST-1** build 후 호스트·포트·scheme·경로 변경 → T4, 송신 0 · 송신 후 3xx → `uncertain`.
- **T-SMOKE-1** 스모크 왕복: 단계별 상태 단언, unknown·미해결·기대 밖 상태에서 abort + 정리 취소, `ok` 를 내지 않음. 멱등키를 출력·재사용.

### 7.8 실행 규율
전체 스위트는 헤드당 한 번 `wrk heavy --` 로. tester 는 그 결과를 인용하고 커버 파일·자체 재현·자체 변이만 돈다. 장중 모의 스모크는 머지 후 operator-desk.

---

## 8. 열린 질문(재구현 착수 전 결정)

1. **Q1 `K_bind` 원천·회전.** 서버 비밀 HKDF 파생 제안. 회전 전에는 미해결 `intent`·`claimed` 가 없어야 하고, `uncertain` 행은 옛 키로 계산된
   바인딩을 유지하므로 회전이 해소 절차에 영향을 주지 않는다.
2. **Q2 벤더 가정.** `itg_orr_no == mkt_orr_no`(KRX·비SOR), 정정이 새 번호를 주고 원수량을 `cor_qty` 로 옮김, 모의 호스트의
   `dailyOrderExecution`·`ost_cns_dit=1` 지원. 스모크 확인 전까지 실패는 전부 `unknown` 쪽.
3. **Q3 시간 상수.** 제안: `claim_window` 30 초, `L_SEND` 60 초(`HTTP_TIMEOUT` 30 초, `D_LOCAL` 1 초), `T_intent_stale` 10 분, T14 유예 `G` = 다음
   거래일 시작. recovery 는 reconcile 실행의 일부이며 별도 스케줄 없음(하드룰 6).
4. **Q4 T14·T9h·T15 운영자 절차.** CLI 인자, 증적 형식(HTS 화면 번호·시각 등), 두 사람 확인 여부.
5. **Q5 부분 UNIQUE 예약의 범위.** "종목·방향당 진행 중 하나"는 래더를 순차 실행으로 만든다. 모의계좌 범위에서는 안전을 우선한다.
6. **Q6 PR #2100 처리.** 승인 후 재구현 PR 을 새 브랜치로 만들고 #2100 을 닫을지, 갱신할지.

## 9. 범위 밖

실계좌, 시장가·조건부·신용·예약·SOR/NXT 주문, 스케줄러 등록, 레인 allowlist·계좌 배정, 해외 주식, 게이트 완화.
