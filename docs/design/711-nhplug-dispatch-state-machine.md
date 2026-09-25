# #711 — NHPLUG 모의계좌 주문 송신 경로 상태 기계 (설계)

- 상태: **설계 문서 — 코드 변경 0.** 재구현은 이 문서가 승인된 뒤 별도 PR 에서 한다.
- 입력: 운영자 결정 A(2026-09-26 — PR #2100 은 열린 채 보류, 송신 경로 설계를 먼저 쓴다),
  운영자 결정 `decision/2026-09-25/nhplug-stage2-mock`(모의계좌 한정 · 지정가 주문·정정·취소 · 레저·reconcile ·
  시장가/실계좌/스케줄러/배정 제외), 독립 tester 보고 r1–r4(`herdr-inbox/jobs/711-nhplug-stage2-20260925-1235/
  tester-report-r{1,2,3,4}.md`)의 BLOCKER 13건.
- 기준 코드: 보류 중인 PR #2100 head `b945ccc76` (브랜치 `task711-nhplug-stage2`). 본문의 `file:line` 은
  전부 이 커밋에서 직접 읽은 것이다. origin/main 에는 아직 Stage 2 코드가 없다.
- 벤더 사실(공개 krstock OpenAPI 문서 · 벤더 SDK 소스에서 확인, 실호출 0): 주문 본문(`cashBuy`/`cashSell`/
  `modify`/`cancel` 의 `Input_0`)에 **클라이언트 주문 ID·멱등키 필드가 없다**. 따라서 브로커 쪽 중복 제거는
  불가능하고, "행 하나당 송신 최대 1회"는 전적으로 이 레포가 보장해야 한다. 접수 증거는 `Output_0.mkt_orr_no`
  하나뿐이며 응답 블록은 데이터가 있을 때만 내려온다. 같은 `rsp_cd` 가 API 마다 다른 뜻일 수 있다.
- 범위: 하드룰 14 의 Stage 2 범위 그대로 — `acct_type=03` 모의계좌, KRX 지정가 주문·정정·취소, 조회, 레저,
  reconcile. 실계좌·시장가·스케줄러·계좌 배정은 범위 밖이며 이 문서는 어떤 게이트도 완화하지 않는다.

## 0. 요약 — 재구현이 지켜야 할 규칙

| 규칙 | 내용 |
|---|---|
| **S1 상태 기계** | 행은 `intent → claimed → sending → {accepted, rejected, uncertain} → reconciled` 만 지난다. 합법 전이는 §2 표가 전부이며 DB 트리거가 강제한다. |
| **S2 본문 = claim 행** | 브로커 본문을 정하는 모든 필드(§3 표)는 intent 행에 저장되고, 본문은 claim 된 행에서 **순수 함수로만** 만든다. 호출자는 수량·가격·범위·계좌를 송신 단계에 넘길 수 없다(넘기는 것은 행 ID·요청 ID·intent digest 뿐). DB 생성 컬럼 `body_digest` 가 intent 필드·claim 조건·전송 바이트 셋을 묶는다. |
| **S3 첫 바이트 이후 = uncertain** | `sending` 표식이 커밋된 뒤의 모든 예외(연결 종료·`aclose()`·취소·프로세스 사망 포함)는 `uncertain` 이다. `not_submitted`(=`withdrawn`)는 `sending` 표식 **이전**에만, 타입이 정해진 송신 전 거부로만 가능하다. |
| **S4 단일 사용 claim** | claim 은 조건부 UPDATE 하나(행 잠금 + 상태·토큰·요청 ID·본문 digest·계좌 바인딩 일치)로만 얻고 즉시 커밋한다. 재전송·두 번째 클라이언트/프로세스·동시 호출·변조된 기대값은 0행에 걸려 송신 0회다. 토큰은 되돌릴 수 없다. |
| **S5 부재는 증거가 아니다** | 빈 배열·블록 누락·13578·오류형·미완료 페이지는 "미체결 없음"이 아니다(`unknown`). 종결 상태는 양성 증거 두 개로만 기록한다. |
| **S6 uncertain 은 자동으로 사라지지 않는다** | uncertain 은 목록의 유일한 양성 일치로만 바인딩되고, 그 밖에는 운영자 확인(attestation)으로만 종결된다. 같은 종목·방향에 미해결 uncertain 이 있으면 새 intent 를 만들지 않는다. |

---

## 1. 배경 — 왜 이 문서가 필요한가

PR #2100 은 네 차례 독립 검증에서 BLOCKER 13건을 받았다(§6). r3·r4 의 결론은 같은 결함 계열이다.
**송신 경로의 안전 성질이 코드 위치마다 따로 조립돼 있었고, 하나의 상태 기계로 서술·강제되지 않았다.**

- r3: 프로세스 안 intent 객체(`dataclasses.replace` 로 복제 가능, 소비 기록이 클라이언트 로컬)가 "행당 1회"를
  대신했다 → 커밋 행 1 개로 변조 수량 송신·재송신.
- r4-1: 정정 전량/일부(`all_pat_dit_cd`)가 claim 밖의 호출자 인자였다 — `client.py:666`
  `full_quantity: bool | None = None`, `_body_from_claim`(`client.py:368`)이 그대로 본문에 쓴다. claim
  (`ledger_service.py:136`)은 이 값을 저장·비교·반환하지 않고 전송 바이트 검사(`client.py:425`)도 보지 않는다.
- r4-2: HTTP 클라이언트 컨텍스트 종료(`client.py:786` 의 `async with`)가 `send` 를 감싼 `try`(`client.py:831`
  주변) **바깥**에서 일어난다. POST 후 `aclose()` 오류는 일반 예외로 빠져나가고, operations 는 "그 밖의 예외 =
  송신 전"(`operations.py:467`)으로 분류해 `dispatching → not_submitted`, `reconcile_required=False` 를 남긴다.

이 문서는 성질을 코드 위치가 아니라 **상태와 전이**로 정하고, 각 전이의 주체·선행 조건·내구 기록을 고정한다.

---

## 2. 상태 기계 (요구 1)

### 2.1 상태

| 상태 | 뜻 | 송신 여부 | 종결 | b945ccc 이름 |
|---|---|---|---|---|
| `intent` | 본문 전체가 커밋된 행. 아직 아무도 claim 하지 않음 | **미송신 확정** | 아니오 | `submitting` |
| `withdrawn` | claim 전 또는 `sending` 표식 전 거부로 보내지 않고 닫힘 | **미송신 확정** | 예 | `not_submitted` |
| `claimed` | 단일 사용 claim 커밋됨. 본문 고정. 송신 전 검사 중 | **미송신 확정** | 아니오 | `dispatching`(일부) |
| `sending` | `sending_at` 표식 커밋됨. 이후 첫 바이트가 나갈 수 있음 | **알 수 없음** | 아니오 | `dispatching`(일부) |
| `accepted` | 2xx + JSON + 읽을 수 있는 `mkt_orr_no` | 송신·접수 | 아니오 | `accepted` |
| `rejected` | 2xx + JSON + 비성공 코드 + 주문번호 없음 | 송신·거부(코드 판정) | 예¹ | `rejected` |
| `uncertain` | `sending` 이후 그 밖의 모든 결과 | **알 수 없음** | 아니오 | `acceptance_uncertain` |
| `open` / `partially_filled` | reconcile 이 양성 두 소스로 확인한 미체결 | 접수됨 | 아니오 | 동일 |
| `filled` / `cancelled` / `modified` | reconcile 종결(§4.4 양성 두 소스) | 접수됨 | 예 | 동일 |
| `confirmed` | 취소·정정 **요청 행**의 반영 확인 | 접수됨 | 예 | 동일 |
| `anomaly` | 증거 모순(심볼 불일치, 거부 후 목록 등장 등). 수동 검토 | — | 예 | 동일 |
| `abandoned` | 해소되지 않은 uncertain 을 **운영자가** 확인 후 닫음(§4.5) | 미확정 | 예 | (신규) |

¹ `rejected` 는 종결이지만 코드로만 판정되므로(벤더 SDK: 같은 코드가 API 마다 다른 뜻) reconcile 이 같은 주문을 목록에서 찾으면 T13 으로 `anomaly` 로 올린다 — 종결 상태에서 나가는 유일한 전이다.

### 2.2 주체

| 주체 | 설명 | 쓸 수 있는 것 |
|---|---|---|
| **O** operations | MCP 도구·스모크 CLI 가 부르는 조정 계층. 게이트·검증 후 intent 를 만든다 | intent 생성, `intent → withdrawn` |
| **D** dispatcher | 클라이언트 송신 경로. claim 토큰 보유자 | `intent → claimed → sending → 결과`, `claimed → withdrawn` |
| **R** reconcile | 목록 증거로만 판정(§4.4). 수동 실행만(스케줄러 금지) | 결과 이후 전이, uncertain 바인딩 |
| **V** recovery | reconcile 실행 시작부에서 도는 시간 기반 정리. 별도 스케줄 없음 | 오래된 `claimed → withdrawn`, 오래된 `sending → uncertain` |
| **H** operator | 운영자 CLI(사유 기록 필수) | `uncertain → abandoned` 만 |

### 2.3 합법 전이 — 이 표가 전부다

| # | 전이 | 주체 | 선행 조건(모두 조건부 UPDATE 의 WHERE) | 기록 |
|---|---|---|---|---|
| T1 | ∅ → `intent` | O | 게이트·검증 통과(§5.1), 동일 종목·방향 미해결 uncertain 없음(§4.6) | INSERT: 본문 필드 전부, `account_binding`, 새 `client_request_id`(DB 가 `body_digest` 생성) — **커밋 후** `(row_id, request_id, body_digest)` 반환 |
| T2 | `intent → withdrawn` | O | `state='intent' AND claim_token IS NULL` | `withdraw_reason` |
| T3 | `intent → claimed` | D | §5.2 claim SQL(상태·토큰 NULL·요청 ID·intent digest(=모든 본문 필드)·계좌 바인딩 일치) | `claim_token`(고유), `claimed_at`, `claimed_by` — 단독 커밋 |
| T4 | `claimed → withdrawn` | D | `state='claimed' AND claim_token=:t AND sending_at IS NULL` + 타입 `PreSendRefusal` | `withdraw_reason` |
| T5 | `claimed → sending` | D | `state='claimed' AND claim_token=:t` | `sending_at` — **단독 커밋 후에만** 첫 소켓 쓰기 |
| T6 | `sending → accepted` | D | `state='sending' AND claim_token=:t` + 2xx·JSON·`mkt_orr_no>0` | `broker_order_id`, `ack_order_id`, 응답 코드 |
| T7 | `sending → rejected` | D | 위 + 2xx·JSON·비성공 코드·주문번호 없음 | 응답 코드·마스킹된 메시지 |
| T8 | `sending → uncertain` | D 또는 V | D: 그 밖의 모든 결과·예외. V: `sending_at < now() − T_send_stale` | `uncertain_reason` |
| T9 | `uncertain → accepted` | D(늦은 ack) 또는 R | D: 같은 토큰의 늦은 2xx 접수. R: §4.5 유일 양성 일치 | `broker_order_id`; R 경로는 `ack_order_id` 없이 수동 검토 표시 |
| T10 | `claimed → withdrawn` | V | `claimed_at < now() − T_claim_stale AND sending_at IS NULL` | `withdraw_reason='stale_claim'` |
| T11 | `accepted/open/partially_filled → open/partially_filled/filled/cancelled/modified` | R | §4.4 양성 두 소스 | 증거 JSON, 수량 |
| T12 | 취소·정정 요청 행 `accepted → confirmed` | R | 자기 ack(`ack_order_id`) + 원주문 목록 행의 반영 수량 | 증거 JSON |
| T13 | `rejected → anomaly`, 임의 비종결 → `anomaly` | R | 모순 증거 | 수동 검토 표시 |
| T14 | `uncertain → abandoned` | H | 당일 세션 종료 후 완전한 목록에서도 미일치 + 운영자 사유 | 운영자 ID·사유·시각 |

**금지 전이(예시, 트리거가 전부 거부):** `sending/uncertain/accepted/… → withdrawn`, `→ intent` 로의 복귀,
`claim_token`·`sending_at`·`claimed_at` 의 NULL 복귀 또는 변경, `abandoned → *`, 종결 상태 → 다른 상태(T13 `rejected → anomaly` 만 예외),
`intent → sending`(claim 생략), `intent → accepted`.

### 2.4 그림

```
          T1(O)                T3(D, 단독 커밋)          T5(D, 단독 커밋)
   ∅ ─────────────▶ intent ─────────────────▶ claimed ─────────────────▶ sending
                      │  T2(O)                 │ T4(D, PreSendRefusal)     │
                      ▼                        │ T10(V, stale)             │ T6 / T7 / T8(D)   T8(V, stale)
                  withdrawn ◀──────────────────┘                           ▼
                                                        accepted ── rejected ── uncertain ──T14(H)──▶ abandoned
                                                           │            │T13         │T9(R 유일 일치 / D 늦은 ack)
                                                           ▼            ▼            ▼
                                             T11/T12(R, 양성 두 소스) ─▶ open · partially_filled · filled ·
                                                                          cancelled · modified · confirmed · anomaly
```

### 2.5 프로세스 사망

| 죽은 위치 | 남는 상태 | 판정 근거 | 처리 |
|---|---|---|---|
| T1 커밋 전 | 행 없음 | 커밋 전 반환 없음 | 없음 |
| T3 전 | `intent` | claim 토큰 NULL | 그대로(재시도는 같은 행 claim) |
| T3 후 · T5 커밋 전 | `claimed` | `sending_at IS NULL` ⇒ 소켓 쓰기 미시작 | V: T10 |
| T5 커밋 결과를 모름 | `claimed` 또는 `sending` | D 는 커밋 확인 실패 시 **보내지 않는다** | `sending` 이면 V: T8(보수적) |
| T5 후 | `sending` | 바이트가 나갔을 수 있음 | V: T8 → R 이 §4.5 로 해소 |

`T_claim_stale`(예: 5 분)은 송신 전 검사(메모리 연산)보다 충분히 길다. 늦게 깨어난 dispatcher 의 T5 는
`state='claimed'` 조건에 걸려 0행이 되므로 **보내지 않는다**. `T_send_stale` 은 HTTP 타임아웃 + 여유(예: 10 분)다.
늦은 결과 기록이 T8 이후에 도착하면 T9(D) 만 허용된다.

---

## 3. 브로커 본문의 claim 행 결속 (요구 2)

### 3.1 필드 표

모든 필드는 T1 에서 intent 행에 저장되고, T3 claim 의 `RETURNING` 으로 돌아온 값만 본문 생성기에 들어간다.
"T3 WHERE" 의 ✔ 는 그 필드가 `body_digest` 의 입력이어서 `body_digest = :intent_digest` 조건으로 정확히
일치해야 claim 된다는 뜻이다(호출자는 필드 값이 아니라 T1 이 돌려준 digest 만 넘긴다).

| 본문을 정하는 것 | 벤더 키(`Input_0`) | 레저 컬럼 | T1 기록 | T3 WHERE | T3 RETURNING | `body_digest` | 전송 바이트 재검사 |
|---|---|---|---|---|---|---|---|
| 조작 종류 | 경로(`cashBuy`/`cashSell`/`modify`/`cancel`) | `operation_kind` | O | ✔ | ✔ | ✔(경로) | ✔(경로 정확 일치) |
| 방향 | 경로(매수/매도) | `side` | O | ✔ | ✔ | ✔ | ✔ |
| 종목 | `iem_cd` | `symbol` | O | ✔ | ✔ | ✔ | ✔ |
| 수량 | `orr_qty` / `cor_qty` | `quantity` | O | ✔ | ✔ | ✔ | ✔ |
| 지정가 | `orr_pr` / `cor_pr` | `price` | O | ✔ | ✔ | ✔ | ✔ |
| 원주문번호 | `org_mkt_orr_no` | `original_order_id` | O | ✔ | ✔ | ✔ | ✔ |
| **정정·취소 범위** | `all_pat_dit_cd`(1 전량 / 2 일부) | **`amend_scope`(신규)** | O(목록에서 계산) | ✔ | ✔ | ✔ | ✔ |
| 계좌 | `act_no` | **`account_binding`(신규, HMAC)** | O | ✔ | ✔ | ✔(바인딩으로 치환) | ✔(HMAC 재계산) |
| 호가유형·조건·공매도·시장·SOR·정지가 | `nmn_pr_tp_cd=01` · `orr_cnd_dit_cd=00` · `ssl_nmn_pr_dit_cd=00` · `rmt_mkt_cd=KRX` · `sor_mkt_sli_yn=N` · `sop_cnd_pr=0` | `body_schema_version` + CHECK(`order_type='limit'`, `venue='KRX'`) | O | ✔(버전) | ✔ | ✔ | ✔(지정가 전용 형태) |
| 요청 ID | (벤더 본문에 없음) | `client_request_id` | O | ✔(직접) | ✔ | ✗ | — |
| 주문일 | (본문 없음, 목록 조회용) | `order_date` | O | — | ✔ | ✗ | — |

### 3.2 규칙

1. **순수 생성기.** `build_body(row, act_no) -> (path, input_0)` 는 입출력 없는 순수 함수이고 인자는
   claim 이 돌려준 행과 브로커 검증된 계좌번호뿐이다. 송신 API(`dispatch`)는 `ledger_row_id`,
   `client_request_id`, `intent_digest`(T1 반환값), `authorization`, 레저 서비스만 받는다. **수량·가격·범위·
   계좌·조작 종류 인자는 없다**(b945ccc 의 `full_quantity`·`ExpectedOrder` 둘 다 제거). digest 는 본문을
   만들 수 없는 값이므로 호출자가 본문을 바꿀 수단이 되지 못한다. 정적 가드가 시그니처를 고정한다.
2. **`amend_scope`.** `place` 는 NULL. `modify` 는 `full|partial` 필수. `cancel` 은 `full ⇒ quantity IS NULL`,
   `partial ⇒ quantity IS NOT NULL` — CHECK 로 강제. 값은 O 가 **T1 시점의 완전한 목록**에서 계산해 저장한다
   (`quantity == open_qty ⇒ full`). 송신 시점에 다시 계산하지 않는다.
3. **`account_binding`.** 계좌번호는 저장하지 않는다(현 레저 원칙 유지). 대신
   `HMAC-SHA256(K_bind, act_no)` 를 저장한다. `K_bind` 는 서버 비밀에서 HKDF(label
   `nhplug-account-binding-v1`)로 파생하며 로그·응답에 나오지 않는다. D 는 매 송신마다 `/n2/acctinfo` 로 검증한
   `acct_type=03` 계좌의 HMAC 을 계산해 T3 WHERE 에 넣는다 — 다른 계좌로 바뀌었으면 0행이다.
4. **`body_digest`.** DB 생성 컬럼(`GENERATED ALWAYS AS (sha256(convert_to(<정규 문자열>, 'UTF8'))) STORED`)이며
   정규 문자열은 `body_schema_version|operation_kind|side|symbol|quantity|price|original_order_id|amend_scope|account_binding`
   (NULL 은 빈 문자열, 수치는 정수 십진 표기)이다. 필드와 digest 가 어긋날 수 없다. T3 WHERE 에 `body_digest =
   :intent_digest` 로 들어가고, D 는 (a) RETURNING 필드로 Python 이 같은 정규 문자열을 만들어 얻은 digest 가 반환된
   `body_digest` 와 같은지(정규화 규칙 드리프트 방지), (b) **빌드된 요청 바이트**를 디코드해 역으로 만든 정규
   문자열의 digest 가 같은지를 T5 직전에 확인한다. 하나라도 다르면 `PreSendRefusal`(T4).
5. **정정 결과 번호.** 정정 응답의 새 `mkt_orr_no` 는 정정 행의 `broker_order_id` 이다. 원주문 행은 목록 증거로만
   `modified` 가 된다(§4.4).

---

## 4. 불확실성 — 첫 바이트 이후 (요구 3)

### 4.1 경계

"첫 바이트가 프로세스를 떠났을 수 있는 순간" = **T5 커밋이 성공한 직후 `send()` 를 호출하는 순간**이다.
HTTP 연결 수립·TLS·요청 쓰기는 모두 `send()` 안에서 일어난다. 따라서:

- T5 **이전**: 소켓 쓰기가 시작되지 않았음이 코드 순서로 보장된다. 이 구간의 거부만 `PreSendRefusal` 타입으로
  올리고 T4 로 닫는다.
- T5 **이후**: 어떤 예외든 결과든 `accepted`/`rejected` 조건을 만족하지 않으면 `uncertain` 이다.

### 4.2 분류 표

| 발생 지점 | 예 | 결과 상태 |
|---|---|---|
| T1 이전(O) | 게이트 off, confirm 누락, 시장가, 형식 오류, 레저 불가 | 행 없음 또는 T2 |
| T3 실패(D) | 이미 claim·요청 ID 다름·digest/바인딩/필드 불일치·행 없음 | **행을 건드리지 않음**(`ClaimRejected`) — 다른 dispatcher 가 소유 중일 수 있다 |
| T3~T5 사이(D) | 호스트·포트·경로·계좌·지정가 형태·digest 재검사 실패, 토큰 발급 실패 | T4(`withdrawn`) |
| T5 커밋 결과 불명(D) | 커밋 중 DB 연결 오류 | **보내지 않음**. 행이 `sending` 이면 V 가 T8 |
| `send()` 중·후 | 타임아웃, 연결 거부·리셋, TLS 오류, 3xx(리다이렉트 금지), 4xx/5xx | T8 |
| 응답 읽기 | 본문 읽기 오류, JSON 아님, 객체 아님 | T8 |
| 판정 | 성공 코드인데 `mkt_orr_no` 없음/읽기 불가 | T8 |
| **컨텍스트 종료** | `aclose()`/연결 풀 정리 오류(r4-2) | 이미 읽은 응답이 `accepted` 증거(`mkt_orr_no`)를 가졌으면 T6(증거 우선) + 경고, 그 밖엔 T8. **어느 경우에도 `withdrawn` 은 불가** |
| 취소·종료 신호 | `CancelledError`, `KeyboardInterrupt`, `SystemExit` | T8 을 **shield 로 기록 시도 후 재발생**. 기록 실패 시 V 가 T8 |
| 프로세스 사망 | kill -9, OOM | V 가 T8 |

### 4.3 구현 형태(규범)

```
claim   = ledger.claim(row_id, request_id, intent_digest, binding)   # T3, 단독 커밋; 0행 → ClaimRejected(행 불변)
row     = claim.row                                   # 본문의 유일한 원천
body    = build_body(row, verified_act_no)            # 순수 함수
request = httpx.Request("POST", MOCK_BASE_URL + path, json={"Input_0": body}, headers=...)  # 클라이언트 없이 생성
check_all_pre_send(row, body, request)                # host·port·path·act_no·지정가·digest(a)(b); 실패 → T4 + PreSendRefusal
ledger.mark_sending(claim.token)                      # T5, 단독 커밋; 커밋 결과 불명 → 보내지 않고 종료
outcome = UNCERTAIN("no_response")
try:                                                  # ← 여기부터 전부 uncertain 경계 안
    async with transport_client(follow_redirects=False) as http:   # 생성과 종료(aclose) 모두 경계 안
        response = await http.send(request)
        outcome  = classify(read_and_parse(response)) # accepted | rejected | uncertain
except BaseException as exc:
    if not outcome.is_accepted:                       # 주문번호를 이미 읽었으면 그 양성 증거를 유지
        outcome = UNCERTAIN(category(exc))
    await shield(ledger.record(claim.token, outcome)) # T6 또는 T8 (조건: state='sending' AND 토큰 일치)
    raise
await ledger.record(claim.token, outcome)             # T6/T7/T8
```

- **operations 의 예외 분류는 뒤집는다.** b945ccc 는 "알 수 없는 예외 = 송신 전"(`operations.py:467`)이었다.
  재구현은 **`PreSendRefusal`·`ClaimRejected` 만 송신 전**이고 그 밖의 모든 예외는 uncertain 이다(fail-closed).
  `withdrawn` 기록은 D 만 하며(T4), O 는 T5 이후의 행을 절대 `withdrawn` 으로 만들 수 없다(트리거).
- 응답 JSON 이 2xx 인데 객체가 아니면 uncertain, 비성공 코드인데 `mkt_orr_no` 가 있으면 **accepted**(증거 우선).

### 4.4 reconcile — 부재는 증거가 아니다

현재 PR 의 규칙을 그대로 규범으로 올린다(`order_evidence.py:236`, `:381`, `:470`; `reconcile_plan.py:143`, `:240`).

- 목록 페이지는 알려진 조회 코드·목록 형태의 행 블록·전부 파싱되는 행·완결된 페이지네이션일 때만 사용 가능하다.
  계속 키(헤더·본문)·계속 코드·`cts_flag=Y` 중 하나라도 있으면 다음 페이지를 따라가며, 따라갈 수 없거나 키가
  반복되면 목록은 **불완전**이다. `cts_flag=N` 이 다른 계속 신호를 지우지 못한다.
- 미체결 판정은 `present` 또는 `unknown` 뿐이다. "없음" 답은 존재하지 않는다.
- 종결 상태는 양성 증거 두 개로만 기록한다:
  `filled` = 전체 조회 행 + 체결 조회(`ost_cns_dit=1`) 같은 수량 ·
  `cancelled` = 전체 조회 행의 취소 수량 + 우리 취소 요청 행의 브로커 ack(`ack_order_id`) ·
  `modified` = 전체 조회 행의 정정 수량 + 우리 정정 요청의 ack + 원주문을 가리키는 후속 주문 행 ·
  `confirmed`(요청 행) = 자기 ack + 원주문 행의 반영 수량. 목록상 거부는 수동 검토.
- 수량 산술: `filled + open + cancelled + modified == order_qty` 가 아니면 `unknown`.
- 미체결 조회에 남아 있는데 전체 조회가 종결이라 하면 `source_disagreement`, 상태 불변.

### 4.5 uncertain 해소

| 경우 | 조치 |
|---|---|
| 같은 토큰의 늦은 2xx ack(D) | T9 → `accepted`(`ack_order_id` 기록) |
| 완전한 전체 조회에서 **유일한** 미귀속 주문이 종목·방향·수량·가격·(정정이면 원주문)과 일치하고 주문 시각 ≥ `sending_at − ε` | T9(R) → `accepted`, `ack_order_id` 는 NULL, 수동 검토 표시. 이후 T11 로 추적하되 **취소·정정의 양성 근거로는 쓰지 않는다**(자기 ack 가 없으므로) |
| 후보 0개 | `uncertain` 유지(보냈는지 알 수 없다). 세션 종료 뒤 완전한 목록에서도 0개면 `requires_manual_review` |
| 후보 2개 이상 | `uncertain` 유지 + 수동 검토(자동 선택 금지) |
| 운영자 확인(H) | HTS/앱 확인 후 사유와 함께 T14 → `abandoned`. `withdrawn` 이 아니다 — "보내지 않았음"이 증명되지 않았기 때문 |

### 4.6 재시도 규칙

- **자동 재시도 없음.** 어떤 결과도 같은 행을 다시 보내지 않는다(행당 T5 는 1회, 토큰 불변).
- 사람이 다시 주문하려면 새 intent(T1, 새 요청 ID)가 필요하다. O 는 같은 `(order_date, symbol, side)` 에
  `sending` 또는 해소되지 않은 `uncertain` 행이 있으면 T1 을 거부한다(`uncertain_order_blocks_new_intent`).
  우회 플래그는 두지 않는다 — 해소(T9) 또는 운영자 확인(T14)이 먼저다.
- `withdrawn`(송신 전 확정) 행 뒤의 새 intent 는 허용한다.

---

## 5. 단일 사용 claim — 원자성·동시성·재전송 (요구 4)

### 5.1 T1 이전의 전제(현 PR 에서 확정된 것 유지)

MCP 인자 `StrictBool`/`StrictInt`(JSON `0/1/"true"` 거부) · `NHPLUG_MOCK_ENABLED` 정확히 `"true"` ·
per-call `dry_run=False` + `confirm=True`(정확한 타입·값, 계약 서브클래스 거부) · 지정가 전용(`limit_orders_only`) ·
`MockAccountAllowlist` 는 `/n2/acctinfo` 파싱으로만 생성 · 주문은 해당 클라이언트 자신의 acctinfo 검증 계좌로만 ·
모의 호스트 고정 + build 후 scheme·host·port·path·act_no 재검증 · `follow_redirects=False`.

### 5.2 SQL

```sql
-- T3 claim (dispatcher 전용 세션, 단독 트랜잭션, 즉시 커밋)
UPDATE review.nhplug_mock_order_ledger
   SET state = 'claimed', claim_token = :new_token, claimed_at = now(), claimed_by = :dispatcher_id
 WHERE id = :row_id
   AND client_request_id = :request_id
   AND state = 'intent'
   AND claim_token IS NULL
   AND account_binding = :binding_of_verified_account
   AND body_digest = :intent_digest            -- 생성 컬럼: 모든 본문 필드의 정확 일치
RETURNING operation_kind, side, symbol, quantity, price, original_order_id,
          amend_scope, body_schema_version, account_binding, body_digest, claim_token;

-- T5 sending 표식 (단독 트랜잭션, 커밋 확인 후에만 send)
UPDATE review.nhplug_mock_order_ledger
   SET state = 'sending', sending_at = now()
 WHERE id = :row_id AND claim_token = :token AND state = 'claimed' AND sending_at IS NULL
RETURNING id;

-- T6/T7/T8 결과
UPDATE review.nhplug_mock_order_ledger
   SET state = :result, ...
 WHERE id = :row_id AND claim_token = :token AND state = 'sending';
```

`body_digest` 는 생성 컬럼이라 저장 필드와 어긋날 수 없고, 본문 필드는 T1 이후 트리거로 불변이므로 T1 이 돌려준
digest 는 claim 순간의 행 필드와 정확히 같은 값을 가리킨다. `intent_digest` 가 다르면(다른 intent 의 digest,
조작된 값) 0행이다.

### 5.3 DB 강제

| 장치 | 내용 |
|---|---|
| UNIQUE | `client_request_id`, `claim_token`, `(order_date, broker_order_id)` |
| CHECK | `state IN (...)` · `(claim_token IS NULL) = (claimed_at IS NULL)` · `state='intent' ⇒ claim_token IS NULL` · `state NOT IN ('intent','withdrawn') ⇒ claim_token IS NOT NULL` · `sending_at IS NOT NULL ⇒ claim_token IS NOT NULL` · `state IN ('sending','accepted','rejected','uncertain',…) ⇒ sending_at IS NOT NULL` · `state='withdrawn' ⇒ sending_at IS NULL` · `amend_scope IN ('full','partial')` 과 §3.2 조합 규칙 · `ack_order_id IS NULL OR ack_order_id = broker_order_id` · `filled_qty ≤ quantity` · 지정가·KRX·모의 고정 |
| **BEFORE UPDATE 트리거** | (old.state, new.state) 가 §2.3 표에 없으면 예외. `claim_token`·`claimed_at`·`sending_at`·`client_request_id`·본문 필드·`amend_scope`·`body_digest`·`account_binding` 은 한 번 채워지면 불변. 수량·체결가·증거 필드(`filled_qty`·`avg_fill_price`·`open_qty`·`cancelled_qty`·`evidence`)는 같은 UPDATE 에서 `reconcile_state='verified'` 일 때만 바뀔 수 있다. 종결 상태는 불변(`abandoned` 포함, T13 만 예외) |
| DELETE | 트리거로 거부(행은 영구 기록) |

트리거는 서비스 계층(하드룰 5)을 대체하지 않는다. 서비스 우회 쓰기·후속 코드 실수가 합법 전이 밖으로 나가지
못하게 하는 두 번째 벽이다.

### 5.4 동시성

- PostgreSQL READ COMMITTED 에서 같은 행에 대한 두 T3 는 행 잠금으로 직렬화되고, 뒤의 문장은 앞 트랜잭션 커밋 뒤
  WHERE 를 다시 평가해 `state='intent'` 가 거짓이므로 0행이다 → 정확히 하나만 claim.
- claim·sending·결과 기록은 **dispatcher 전용 세션**의 짧은 트랜잭션이다. 호출자의 열린 트랜잭션에 섞지 않는다
  (섞이면 커밋 시점이 호출자에게 달려 T5 "커밋 확인 후 송신"이 깨진다).
- 한 dispatcher 호출은 한 행만 다룬다. 같은 세션 동시 사용 금지.

### 5.5 재전송 규칙

| 시도 | T3 결과 | 송신 |
|---|---|---|
| 같은 요청 ID 로 다시(재시도·재생) | `state='intent'` 거짓 → 0행 | 0 |
| 새 요청 ID 로 같은 행 | `client_request_id` 불일치 → 0행 | 0 |
| 두 번째 클라이언트·프로세스 | 같은 DB 조건 → 0행 | 0 |
| 동시 두 호출 | 하나만 1행 | 1 |
| 수량·가격·범위를 바꾼 호출 | 송신 API 에 그런 인자가 없음(정적 가드). 행 필드 변경은 트리거 거부. 다른 digest 를 넘기면 0행 | 0 |
| 다른 계좌로 검증된 클라이언트 | 바인딩 불일치 → 0행 | 0 |

---

## 6. BLOCKER 13건 ↔ 방지 규칙 (요구 5)

| # | 라운드 | 결함(요약) | 방지 규칙 | 필수 테스트(§7) |
|---|---|---|---|---|
| 1 | r1 | MCP JSON `0/1/"true"` 가 `dry_run`/`confirm` 으로 강제 변환 | §5.1 `StrictBool`/`StrictInt` 스키마 | T-GATE-2 |
| 2 | r1 | 손으로 만든 allowlist 로 실계좌 바인딩 | §5.1 acctinfo 파싱 전용 생성 + 클라이언트 자기 검증 계좌만 + §3.2-3 계좌 바인딩 | T-ACCT-1..3 |
| 3 | r1 | 디스패처가 권한 미검사 · 계약 서브클래스 우회 | §5.1 정확한 타입·값 검사를 dispatch 진입과 T3 직전 모두 | T-GATE-3 |
| 4 | r1 | 빈/미완료 목록을 "없음"·종결로 사용 | S5, §4.4 | T-LIST-1..3 |
| 5 | r1 | 모순 수량(체결+미체결>주문)을 검증된 체결로 | §4.4 수량 산술 정확 일치 | T-REC-4 |
| 6 | r1 | `unknown` 상태로 체결 수량 기록 | §5.3 트리거 + 서비스: 수량·증거 필드는 검증된 T11 에서만 | T-DB-4 |
| 7 | r1 | 스모크 왕복이 unknown·미해결에도 ok | §7 T-SMOKE: 단계별 상태 단언, 실패 시 abort | T-SMOKE-1 |
| 8 | r2 | 빈 미체결 조회 + 증인 행을 "없음"·종결 근거로 사용 | S5: "없음" 상태 없음, 종결은 양성 두 소스 | T-LIST-2, T-REC-1..3 |
| 9 | r2 | 모순된 계속 메타데이터(`N`+계속 코드/키)를 마지막 페이지로 | §4.4 페이지 종료 규칙 | T-LIST-3 |
| 10 | r2 | 커밋된 레저 행 없이 클라이언트 송신 | S1·S4: 송신은 T3 claim 된 행에서만, 송신 API 에 행 외 입력 없음 | T-CLAIM-1 |
| 11 | r3 | 복제 가능한 프로세스 내 intent · 로컬 소비 → 변조 수량·재송신 | S4 DB claim(§5.2) + S2 본문=claim 행 + 불변 트리거 | T-CLAIM-2..6 |
| 12 | r4 | 정정 전량/일부가 claim 밖 호출자 인자 | §3.1 `amend_scope` 저장·digest(=claim WHERE)·RETURNING·바이트 재검사, 송신 API 에서 인자 제거 | T-BODY-2, T-CLAIM-5 |
| 13 | r4 | POST 후 `aclose()` 오류가 `not_submitted` 로 기록 | S3·§4.1–4.3: T5 이후 전부 uncertain, 컨텍스트 종료를 경계 안에, operations 분류 반전, 트리거가 `sending → withdrawn` 거부 | T-UNC-1..6 |

---

## 7. 재구현이 반드시 가져야 할 테스트 (요구 6)

모든 테스트는 가짜 전송·가짜 브로커·자체 일회용 DB 만 쓴다(실 NH 호출 0). **각 규칙 테스트는 그 규칙을 되돌리는
변이(mutant)에서 단언 실패로 RED** 여야 하며, 변이 목록은 테스트 파일에 기계적으로 선언한다(현 PR 의
`test_stage2_mutants.py` 방식 — 실제 모듈 사본에 문자열 치환을 적용해 탐침이 `AssertionError` 로 실패함을 확인).

### 7.1 상태 기계·DB

- **T-SM-1** §2.3 의 모든 합법 전이가 성공하고, 표에 없는 모든 (old, new) 쌍이 트리거에서 거부된다(쌍 전수 열거).
- **T-SM-2** 불변 필드(`claim_token`, `claimed_at`, `sending_at`, `client_request_id`, 본문 필드, `body_digest`,
  `account_binding`) 변경·NULL 복귀가 거부된다. DELETE 거부.
- **T-DB-1..5** 각 CHECK 위반 행 INSERT/UPDATE 가 `IntegrityError`(§5.3 전 항목). `claim_token` UNIQUE.
  T-DB-4: `reconcile_state<>'verified'` 인 체결 수량 기록 거부.
- **T-MIG-1** 마이그레이션 upgrade → ORM 스키마와 제약·인덱스·트리거까지 동일 → downgrade → upgrade(일회용 DB).

### 7.2 claim (§5)

- **T-CLAIM-1** 레저 행 없음·가짜 레저·레저 서브클래스 → 토큰 발급·소켓 0.
- **T-CLAIM-2** 재생(같은 요청 ID): 송신 정확히 1회, 본문 = 커밋 본문.
- **T-CLAIM-3** 새 요청 ID 로 같은 행: 송신 1회.
- **T-CLAIM-4** 두 번째 클라이언트 + 두 번째 세션: 송신 1회.
- **T-CLAIM-5** 변조 시도: 행 필드·`amend_scope` 직접 UPDATE(트리거 거부), 다른 intent 의 digest 전달(0행),
  다른 계좌 검증 클라이언트(바인딩 불일치 0행), Python 정규화 드리프트 주입(RETURNING 재계산 불일치 → T4) —
  모두 송신 0회, 이후 정상 claim 으로 커밋 본문 정확히 1회.
- **T-CLAIM-6** 동시성: 별도 세션·별도 클라이언트 2개를 `gather` — 정확히 1개 성공, 송신 1회.
  변이: WHERE 에서 `state='intent'`/`claim_token IS NULL` 제거 → RED.

### 7.3 본문 결속 (§3)

- **T-BODY-1** 매수·매도·정정(전량/일부)·취소(전량/일부) 각각에서 전송 바이트 == `build_body(claim 행)` == T1 digest.
- **T-BODY-2** 정정 범위: 원주문 5주·일부 1주 정정 행은 `all_pat_dit_cd=2`·`cor_qty=1` 로만 나간다.
  송신 API 에 범위 인자가 없음을 정적 가드가 확인. 변이: 생성기가 범위를 호출자 값에서 읽음 → RED.
- **T-BODY-3** 빌드 후 요청 바이트 변조(수량·가격·종목·범위·계좌·호가유형 05·SOR·IOC 각각) → T4, 송신 0.
- **T-BODY-4** `account_binding` 불일치(다른 `03` 계좌로 검증) → claim 0행.

### 7.4 uncertain (§4)

- **T-UNC-1** `send()` 타임아웃·연결 리셋·3xx·4xx·5xx·비JSON·객체 아님·성공 코드+번호 없음 → 전부 `uncertain`, 재시도 0.
- **T-UNC-2** **POST 후 전송 계층 `aclose()` 예외**(r4-2 재현): 응답을 읽기 전이면 `uncertain`, 이미 읽은 응답에
  `mkt_orr_no` 가 있었으면 `accepted` — 어느 쪽도 `withdrawn` 이 아니고 `reconcile_required=True`.
  변이: 컨텍스트 종료를 경계 밖으로 이동 → RED.
- **T-UNC-3** 알 수 없는 예외 타입이 T5 이후에 나면 `uncertain`(operations 분류 반전). 변이: "그 밖 = 송신 전" 복원 → RED.
- **T-UNC-4** `CancelledError`/`KeyboardInterrupt` 가 `send()` 중에 나면 `uncertain` 기록 후 재발생.
- **T-UNC-5** T5 커밋 결과 불명(커밋 예외 주입) → 송신 0, 이후 V 가 `sending → uncertain`.
- **T-UNC-6** recovery: 오래된 `claimed`(sending_at NULL) → `withdrawn`, 오래된 `sending` → `uncertain`,
  늦게 깨어난 dispatcher 의 T5 는 0행이라 송신 0.
- **T-UNC-7** 늦은 ack: T8(V) 이후 같은 토큰의 2xx 접수 → T9 `accepted`.

### 7.5 reconcile·목록 (§4.4–4.6)

- **T-LIST-1** 블록 누락·`[]`·13578·오류형 코드·게이트웨이 오류·행 비목록·행 파싱 실패 → 페이지 사용 불가 또는 빈 행이며,
  어떤 조합도 "없음" 을 만들지 않는다.
- **T-LIST-2** 빈 미체결 조회 + 전체 조회의 미체결 행 → `present`(kt00009 회귀). 빈 미체결 + 종결 행만 → `unknown`.
- **T-LIST-3** 계속 신호 조합 표(키 헤더/본문 × 계속 코드 × `Y/N/없음`)에서 키 없는 계속·반복 키는 불완전, `N` 이
  다른 신호를 지우지 못함.
- **T-REC-1** 취소 종결: 자기 취소 ack 없으면 `unknown`. **T-REC-2** 정정 종결: 자기 정정 ack + 후속 행 없으면 `unknown`.
  **T-REC-3** 체결: 체결 조회 교차확인 없으면 `unknown`. **T-REC-4** 수량 합 불일치 → `unknown`.
- **T-REC-5** uncertain 해소: 유일 일치 → `accepted`(수동 검토, ack 없음), 0개·복수 → 유지, 운영자 T14 만 `abandoned`.
- **T-RETRY-1** 같은 종목·방향 미해결 uncertain 이 있으면 새 intent 거부.

### 7.6 게이트·계좌·스모크(현 PR 에서 확정, 회귀 방지)

- **T-GATE-1** `NHPLUG_MOCK_ENABLED` 정확히 `"true"` 외 모두 거부. **T-GATE-2** FastMCP 와이어에서 `0/1/"true"`
  거부(실제 `Client.call_tool`). **T-GATE-3** 계약 서브클래스·`dry_run=True`·`confirm` 비-True 거부(dispatch 진입과 T3 직전).
- **T-ACCT-1** 손으로 만든 allowlist 생성 불가. **T-ACCT-2** 호출자가 바인딩한 allowlist 는 조회만. **T-ACCT-3** 01/02·상충 type 거부.
- **T-HOST-1** build 후 호스트·포트·scheme·경로 변경, 리다이렉트 → 송신 0 또는 uncertain(송신 후 3xx).
- **T-SMOKE-1** 스모크 왕복은 각 단계 상태를 단언하고, unknown·미해결·기대 밖 상태에서 abort + 정리 취소, `ok` 를 내지 않는다.

### 7.7 실행 규율

- 전체 스위트는 헤드당 한 번, `wrk heavy --` 로. tester 는 그 결과를 인용하고 커버 파일·자체 재현·자체 변이만 돈다.
- 장중 모의 스모크(주문→조회→정정→취소→reconcile, 빈 배열 검증)는 머지 후 operator-desk 가 수행한다.

---

## 8. 열린 질문(재구현 착수 전 운영자·리뷰 결정)

1. **Q1 `K_bind` 원천.** 서버 비밀에서 HKDF 파생을 제안한다. 비밀 회전 시 기존 `intent` 행은 claim 불가가 되므로
   회전 전 미해결 intent 를 `withdrawn` 처리하는 절차가 필요하다.
2. **Q2 벤더 가정.** `itg_orr_no == mkt_orr_no`(KRX·비SOR), 정정이 새 번호를 주고 원수량을 `cor_qty` 로 옮김, 모의
   호스트의 `dailyOrderExecution`·`ost_cns_dit=1` 지원 — 스모크로 확인 전까지 실패는 전부 `unknown` 쪽이다.
3. **Q3 `T_claim_stale`·`T_send_stale` 값.** 제안: 5 분·10 분. recovery 는 reconcile 실행의 일부이며 별도 스케줄 없음(하드룰 6).
4. **Q4 `abandoned` 운영 절차.** 운영자 CLI 인자(사유 필수)와 증적 형식. 자동 경로는 두지 않는다.
5. **Q5 PR #2100 처리.** 이 설계 승인 후 재구현 PR 을 새 브랜치로 만들고 #2100 을 닫을지, #2100 을 갱신할지.

## 9. 범위 밖

실계좌, 시장가·조건부·신용·예약·SOR/NXT 주문, 스케줄러 등록, 레인 allowlist·계좌 배정, 해외 주식, 게이트 완화.
