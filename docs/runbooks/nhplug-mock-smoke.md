# NHPLUG 모의 smoke (1단계 read-only · 2단계 MCP 주문 왕복)

이 문서의 앞부분은 NHPLUG 모의투자 통합의 **read-only 1단계** 런북이다. 계좌목록·국내주식 잔고·국내주식 현재가만 조회한다. 2단계(#711 송신 경로 + #849 `nh_mock_*` MCP 도구)의 주문 왕복 절차는 맨 아래 [2단계](#2단계--nh_mock_-mcp-주문-왕복-849) 절에 있다. 스케줄러는 어느 단계에도 없다.

## 안전 경계

- 데이터 클라이언트는 `https://moapi.nhplug.com:8443`만 허용한다. 다른 scheme·호스트·포트·경로는 거부한다.
- 요청을 만든 뒤 `send` 직전에 `request.url.scheme`, `request.url.host`, `request.url.port`, path를 다시 확인한다. OAuth와 데이터 양쪽에서 `follow_redirects=False`를 명시한다. 3xx는 따라가지 않고 실패한다. 이는 호스트 경계만이 아니라 APP KEY/SECRET 경계다. httpx는 cross-origin redirect에서 custom credential header를 자동 제거하지 않을 수 있으므로 redirect를 따르면 secret이 외부 host로 전달될 수 있다.
- 데이터 allowlist는 `/n2/acctinfo`, 국내 잔고, 국내 현재가 세 path뿐이며, allowlist 검사는 토큰 해석과 소켓 생성 전에 실행된다.
- 계좌목록에서 `acct_type=03`인 값만 allowlist에 넣는다. `01`·`02`는 거부 타입 상수이며, 동일 계좌번호가 상충하는 type으로 중복되면 전체 응답을 거부한다. `NHPLUG_MOCK_ACCOUNT_NO`도 반드시 broker 응답의 `03` allowlist에 있어야 한다. 검증된 allowlist는 dispatcher에 bind되며, balance/quote dispatch는 caller가 선택적으로 넘긴 allowlist 없이 이를 필수로 사용한다.
- 잔고와 시세 요청은 시작 시와 `send` 직전 두 번 configured `act_no` allowlist를 확인한다. 시세 본문에는 계좌번호가 없지만, 같은 verified configured account를 두 번 확인해 이중 판별 상태를 유지한다.
- 접근토큰은 벤더 제약상 운영 OAuth 호스트에서만 발급된다. 운영 호스트를 아는 코드는 `app/services/brokers/nhplug/auth.py` 하나이며, `POST /oauth2/token`, `POST /oauth2/revoke`만 allowlist한다. OAuth dispatch도 데이터 dispatch와 같은 `NHPLUG_MOCK_ENABLED` master gate 뒤에 있다. 데이터 클라이언트는 운영 호스트 상수나 import를 갖지 않는다.
- 벤더 Python SDK는 의존하거나 import하지 않는다. 호스트는 env가 아닌 코드 상수이며, `NHPLUG_BASE_URL`과 `NHPLUG_AUTH_URL`은 읽지 않는다.

## 보장 강도와 제거 불가 위험

보장 강도는 **"우발 방지 + 정적 검출"**이다. 구조적 불가능이라는 주장이 아니다.

- 운영 계좌는 같은 고객번호 아래 실재하고 같은 APP KEY로 접근 가능하다. `acct_type=03` allowlist는 벤더 격벽이 아니라 이 코드가 거는 검증이다.
- Kiwoom live read-only의 3중 방어 ③(계좌번호를 프로세스 환경에 두지 않음)에 대응물은 없다. NH는 `/n2/acctinfo` 응답에 운영 계좌도 항상 함께 내려주므로, 프로세스가 운영 계좌를 전혀 알지 못하게 만드는 방법이 벤더 설계상 없다.
- 벤더 기본값은 운영이다. 이 때문에 벤더 SDK를 사용하지 않고, mock data host와 auth host를 물리적으로 분리한 자체 클라이언트를 사용한다.
- OAuth 토큰은 운영에서 발급되고 양쪽 환경에 통용된다. auth path allowlist는 그 예외를 두 path로 좁힐 뿐, 운영 토큰 발급 자체를 제거하지 못한다.

## 자격증명 파일과 게이트

운영자가 직접 전용 파일을 만든다. 구현자는 파일을 만들거나 키 값을 이동하지 않는다.

```dotenv
NHPLUG_APP_KEY=...
NHPLUG_APP_SECRET=...
NHPLUG_MOCK_ACCOUNT_NO=...
```

권장 파일명은 `.env.nhplug-mock.native`다. 이 파일에는 정확히 위 세 키만 있어야 하며 `DATABASE_URL`을 포함하면 안 된다. `NHPLUG_MOCK_ENABLED=true`은 파일이 아니라 실행 환경에서 별도로 명시한다. CLI는 파일명 또는 `ENV_FILE`에 `prod`가 있으면 거부하고, 누락·추가 키는 **이름만** 보고한다.

`NHPLUG_MOCK_ENABLED`은 default-disabled다. 미설정 또는 truthy가 아닌 값에서는 모든 OAuth 및 데이터 dispatch가 fail-closed 된다.

## 실행

다음 명령은 계좌번호·토큰·키 값·원문 broker body를 출력하지 않는다.

```bash
# 네트워크 0회: gate, env-file shape, static read allowlist만 확인
NHPLUG_MOCK_ENABLED=true uv run python -m scripts.nhplug_mock_smoke \
  --env-file .env.nhplug-mock.native --mode preflight

# `/n2/acctinfo`로 acct_type=03을 검증한 뒤 국내 잔고를 조회
NHPLUG_MOCK_ENABLED=true uv run python -m scripts.nhplug_mock_smoke \
  --env-file .env.nhplug-mock.native --mode account --confirm-read

# 같은 계좌 검증 뒤 국내주식 현재가를 실측
NHPLUG_MOCK_ENABLED=true uv run python -m scripts.nhplug_mock_smoke \
  --env-file .env.nhplug-mock.native --mode quote --symbol 005930 --confirm-read
```

모드는 정확히 `preflight`, `account`, `quote` 셋뿐이다.

- `preflight`: 네트워크 0회. 전용 파일·gate·read-only allowlist를 확인한다.
- `account`: 계좌목록을 받아 `acct_type=03`으로 `NHPLUG_MOCK_ACCOUNT_NO`를 검증하고, 그 검증된 계좌의 잔고만 조회한다.
- `quote`: 먼저 같은 계좌 검증을 수행한 뒤 현재가 한 건을 조회한다. 벤더 문서의 모의 시세 지원 여부가 모순되므로, broker가 거부하면 response code와 함께 exit 2로 실패한다. 성공으로 위장하지 않는다.

`account`과 `quote`는 `--confirm-read`도 요구한다. 이는 조회의 추가 운영자 의도 확인이며, 주문용 gate가 아니다.

## dry-run / confirm 계약

`app.services.brokers.nhplug.contracts.DryRunConfirmContract`는 미래 action을 위한 타입 계약이다. 기본은 `dry_run=True`, `confirm=False`이며 non-dry action은 `confirm=True` 없이는 허용되지 않는다.

그러나 이 단계에는 그 타입을 소비하는 dispatch 메서드가 없다. 주문 관련 메서드·endpoint·TR을 만드는 것은 범위 밖이며, AST guard가 알려진 주문 endpoint/TR, vendor SDK import, 운영 host 문자열의 잘못된 위치, host-override env 참조를 빌드에서 차단한다.

## 현재 정지점

2026-08-24 구현·단위 테스트에서는 합성 `httpx.MockTransport`만 사용했다. `NHPLUG_MOCK_ACCOUNT_NO` 전용 파일이 운영자에 의해 배치되기 전에는 실제 smoke를 실행하지 않는다. 배치 확인 후 별도 지시가 있을 때에만 위 `account`와 `quote` 명령을 실행한다.

## 2단계 — nh_mock_* MCP 주문 왕복 (#849)

2026-09-28 운영자 승인(모의계좌 한정 주문·정정·취소·레저·reconcile, 실계좌 제외)의 완료 기준인 **장중 지정가 매수 → 조회 → 정정 → 취소 → reconcile 왕복**과 **"빈 배열 ≠ 미체결 없음"** 실증 절차다. 🔴 **머지·배포 뒤 desk 또는 strategy-lab 이 운영자 입회 하에서만** 실행한다. 구현자·tester 는 실행하지 않는다(테스트는 가짜 NH 와 테스트 DB 만 썼다).

### 도구

MCP 서버가 `NH_MOCK_MCP_ENABLED=true` 일 때 DEFAULT 프로필에만 아홉 개가 등록된다. live 프로필·레인 allowlist 에는 없다.

| 도구 | 성격 | 네트워크 |
|---|---|---|
| `nh_mock_preview_order` | 지정가 문법 검사만 | 0 |
| `nh_mock_place_order` / `nh_mock_modify_order` / `nh_mock_cancel_order` | 주문 변경. 기본 `dry_run=True`, 전송은 `dry_run=False` + `confirm=True` + `idempotency_key` | acctinfo 1 (+정정·취소는 전체 조회) + 주문 1 |
| `nh_mock_get_order_history` / `nh_mock_get_order_detail` | 일자별 전체·미체결 조회 + 레저 행 | acctinfo 1 + 조회 |
| `nh_mock_get_positions` / `nh_mock_get_orderable_cash` | 잔고 조회(`Output_1` 보유, `Output_0.orr_pbl_amt`) | acctinfo 1 + 잔고 1 |
| `nh_mock_reconcile_orders` | 수동 reconcile. 기본 `dry_run=True`(조회만), 쓰기는 `dry_run=False` + `confirm=True`. **주문은 보내지 않는다** | acctinfo 1 + 조회 3 |

모든 호출은 매번 새 클라이언트로 `/n2/acctinfo` 를 읽어 `NHPLUG_MOCK_ACCOUNT_NO` 가 `acct_type=03` 으로 나올 때만 진행한다. `order_type` 은 정확히 `"limit"` 만 받는다(시장가·대소문자 변형·호가코드·`None` 은 네트워크 전에 `limit_order_only`). 정정·취소는 **이 레저가 보내 번호가 결속된(reconcile 로 `accepted` 이후가 된) 당일 주문**만 받는다.

### 사전 조건 (운영자)

1. #711 마이그레이션(`20260926_task711_nhplug_dispatch`) 적용, 키 버전 표 v1 을 운영자 역할로 등록(설계 §8 D-KEY), 해당 `NHPLUG_STAGE2_ROOT_SECRET_V1` 을 MCP 서버 런타임에 배치.
2. 설계 §8 차단 조건(B-DB·B-HOST·B-VENDOR)을 확인한 뒤에만 `NHPLUG_STAGE2_{KEY,TIME,DB,HOST,VENDOR}_CONFIRMED=true`, `NHPLUG_MOCK_ENABLED=true`, `NH_MOCK_MCP_ENABLED=true` 를 켠다. 자격증명 `NHPLUG_APP_KEY`·`NHPLUG_APP_SECRET`·`NHPLUG_MOCK_ACCOUNT_NO` 는 운영자가 런타임 env 에 둔다(도구는 누락 시 키 **이름만** 보고).
3. KRX 정규장 중. 테스트 종목·가격을 정해 둔다: 예) `005930` 1주, 가격은 `get_quote` 현재가 대비 약 −20% 이면서 **하한가 이내**, 호가 단위에 맞춘 값(체결되지 않을 만큼 멀리).
4. 멱등키 세 개를 미리 정해 기록한다(16–64자 `[A-Za-z0-9_-]`, 예: `ltref849-place-20261001a`). **재시도는 반드시 같은 키로**. 새 키로 다시 보내면 중복 주문 위험이 있으며, 대부분은 진행 중 예약(`in_flight_order_exists`)이 막는다.

### 절차

모든 단계에서 기대와 다른 `status`·`error` 가 나오면 **즉시 멈추고** 새 주문을 내지 않는다. `uncertain` 은 실패가 아니라 "reconcile 전 미확정" 이다 — 벤더가 성공 코드를 문서화하지 않아 성공 증명 표가 비어 있으므로 모든 송신은 먼저 `uncertain` 이 된다(설계 §4.3).

0. **계좌·현금 확인**: `nh_mock_get_orderable_cash()` → `status=ok`, `cash` 정수. `nh_mock_get_positions()` → `positions_state` 기록. `mock_account_rejected`/`mock_account_unverified` 면 중단.
1. **미리보기**: `nh_mock_preview_order(symbol, side="buy", quantity=1, price=P1)` → `status=preview`, `network_calls=0`.
2. **지정가 매수**: `nh_mock_place_order(..., price=P1, idempotency_key=K1, dry_run=False, confirm=True)` → 기대 `status=uncertain`, `reconcile_required=true`, `retry_allowed=false`, `ack_evidence_order_id=N`. `N` 기록. 응답을 못 받았으면 **같은 K1** 으로 다시 호출한다(행을 돌려줄 뿐 다시 보내지 않는다).
3. **reconcile 로 결속**: `nh_mock_reconcile_orders()`(dry run) → `status=dry_run`, 해당 행 `planned_action=verify_own_number`, `would_be_status=verification_pending`, `verification_pending_row_ids` 에 그 행, `would_be_unresolved_row_ids=[]`. 이어서 `nh_mock_reconcile_orders(dry_run=False, confirm=True)` → **`status=reconciled`, `success=true`**, `unresolved_row_ids=[]`, 행 `state=open`, `broker_order_id=N`. dry run 이 `would_be_status=uncertain`(목록에 `N` 이 아직 없음)이면 잠시 뒤 dry run 을 한 번 더 본다. 확정 실행이 `partial`/`uncertain`/`unknown` 이면 **왕복을 멈추고 보고한다**(아래 상태 표). reconcile 은 주문을 보내지 않으므로 한 번 더 확정 실행하는 것은 허용되지만, 그래도 `reconciled` 가 아니면 중단·보고한다.
4. **조회**: `nh_mock_get_order_detail(order_id=N)` → `success=true`, `status=listed`(=`broker_view`), `owned_by_ledger=true`, `derived_status=open`. `success=false`·`status=unknown`·`reason=order_listing_incomplete` 면 전체 목록 조회가 불완전한 것이다 — 레저 행이 있어도 확인된 것이 아니므로 조회를 반복하거나 중단한다. `nh_mock_get_order_history()` → `orders_state=complete`, `open_orders_state=present`, `open_orders` 에 `N`.
5. **정정**: `nh_mock_modify_order(order_id=N, new_price=P2, new_quantity=1, idempotency_key=K2, dry_run=False, confirm=True)` (P2 도 체결되지 않을 가격) → `status=uncertain`, `ack_evidence_order_id=M`. reconcile dry run → `would_be_status=verification_pending`(원주문 행과 정정 행이 `verification_pending_row_ids`). reconcile(확정 실행) → **`status=reconciled`, `success=true`**, 원주문 행 `modified`(`successor_order_id=M`), 정정 행 `confirmed`. `partial` 이면 원주문 행만 재검증되고 정정 행이 `uncertain` 으로 남은 것이다 — 멈추고 보고한다.
6. **취소**: `nh_mock_cancel_order(order_id=M, idempotency_key=K3, dry_run=False, confirm=True)` → `status=uncertain`, `ack_evidence_order_id=C`. reconcile(확정 실행) → **`status=reconciled`, `success=true`**, 취소 행 `confirmed`, `unresolved_row_ids=[]`.

reconcile 응답 상태 표(확정 실행 `status`, dry run 은 같은 단어를 `would_be_status` 로 예측):

| `status` | 뜻 | desk 행동 |
|---|---|---|
| `reconciled` (`success=true`) | 세 조회가 모두 완전하고 대상 행(`uncertain`/`sending` 행 + 결속 행)이 **전부** 해결됨. `unresolved_row_ids=[]` | 다음 단계 진행 |
| `partial` (`success=false`) | 일부 행은 해결(`resolved_row_ids`), 나머지는 여전히 `uncertain`/`sending`(`unresolved_row_ids`) | **멈추고 보고**. 새 주문·정정·취소 금지 |
| `uncertain` (`success=false`) | 대상 행 중 해결된 것이 하나도 없음. `unresolved_row_ids` 의 행이 그대로 미확정 | **멈추고 보고**. 새 주문·정정·취소 금지 |
| `unknown` (`success=false`) | 조회 하나 이상이 불완전(`incomplete_scopes`)하거나 결속 행 재검증 실패(`unverified_row_ids`). 미해결 행이 있어도 이 값이 우선 | 정상 완료로 기록하지 않는다. 조회 반복 또는 중단·보고 |
| `verification_pending` (dry run 전용) | 미해결로 남을 것으로 예측되는 행은 없고, `verification_pending_row_ids` 의 행은 확정 실행의 레저 검사로만 판정됨 | 확정 실행. 결과는 `reconciled` 또는 `unknown` 이며 dry run 만으로 `reconciled` 를 기록하지 않는다 |

dry run 예측은 같은 조회·레저 상태에서 확정 실행보다 **좋게** 나오지 않는다(`partial`/`uncertain` 예측은 확정 실행에서 같은 값이거나 `unknown`).
7. **"빈 배열 ≠ 미체결 없음" 실증**: 마지막으로 `nh_mock_get_order_history()` → 미체결 목록이 비었을 때 `open_orders_state` 는 **`unknown`** 이어야 하며(`"none"` 같은 값은 존재하지 않는다) 사유에 `empty_open_listing_is_not_evidence_of_no_open_orders` 가 있어야 한다. 운영자가 HTS/앱에서 미체결이 실제로 없음을 눈으로 확인하고 기록한다.

### 결과 기록

단계별 도구 응답의 `status`, `state`, `ledger_row_id`, 주문번호 `N`/`M`/`C`, reconcile 의 `rows[].state` 를 기록한다. 계좌번호·토큰·키 값은 응답에 없고 기록하지 않는다.

### 중단·미해결 처리

- `uncertain` 행은 같은 계좌·종목·방향의 새 주문을 **날짜와 무관하게** 막는다(설계 §5.2). reconcile 로 풀리지 않으면 T9h(후보 확인)·T14(위험 인수 종결)는 운영자 역할의 1회용 승인 행이 필요한 운영자 전용 절차이며 MCP 로는 할 수 없다(설계 §4.5–4.6). 이 절차의 운영자 CLI 는 아직 없으므로 행을 그대로 두고 보고한다.
- 정정·취소가 `order_not_bound_reconcile_first` 면 3단계 reconcile 을 먼저 한다. `order_not_owned` 는 이 레저가 보낸 주문이 아니라는 뜻이다 — HTS 에서 직접 낸 주문은 이 도구로 정정·취소하지 않는다.
- reconcile 은 전체·미체결·체결 세 조회가 모두 완전하고 결속 행이 전부 재검증되며 **미해결 행이 하나도 없을 때만** `status=reconciled` 다. 하나라도 불완전하면 `status=unknown`, `success=false` 이며 `incomplete_scopes`·`unverified_row_ids` 에 이름이 남는다. 행이 `uncertain`/`sending` 으로 남으면 `status=partial`(일부 해결) 또는 `status=uncertain`(해결 0), `success=false` 이며 `unresolved_row_ids` 에 이름이 남는다. 어느 것도 정상 완료로 기록하지 않는다 — 멈추고 보고한다.
- `listing_incomplete`/`order_not_listed` 는 브로커 조회가 그 주문을 미체결로 보여주지 못한 것이다. 없다는 증거가 아니므로 다시 보내지 말고 조회를 반복하거나 중단한다.
