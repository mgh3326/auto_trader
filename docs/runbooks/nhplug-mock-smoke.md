# NHPLUG 모의 smoke (Stage 1 read-only + Stage 2 주문)

이 문서의 앞부분은 NHPLUG 모의투자 통합의 **read-only 1단계** 런북이다(계좌목록·국내주식 잔고·국내주식 현재가). **Stage 2(#711, 운영자 결정 2026-09-25)** 의 모의계좌 지정가 주문·정정·취소·레저·reconcile 스모크는 맨 아래 "Stage 2" 절이다. 스케줄러는 어느 단계에도 없다.

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

`app.services.brokers.nhplug.contracts.DryRunConfirmContract`는 주문 dispatch 계약이다. 기본은 `dry_run=True`, `confirm=False`이며, Stage 2의 유일한 소비자인 `client._post_mutation` 계열 메서드는 `dry_run=False` **그리고** `confirm=True`(정확한 bool)일 때만 토큰·소켓 I/O로 진행한다. AST guard는 주문 endpoint를 `client.py`의 KRX 4개 path로만 좁히고, 그 밖의 주문 endpoint/TR, vendor SDK import, 운영 host 문자열의 잘못된 위치, host-override env 참조를 빌드에서 차단한다.

## 현재 정지점

2026-08-24 구현·단위 테스트에서는 합성 `httpx.MockTransport`만 사용했다. `NHPLUG_MOCK_ACCOUNT_NO` 전용 파일이 운영자에 의해 배치되기 전에는 실제 smoke를 실행하지 않는다. 배치 확인 후 별도 지시가 있을 때에만 위 `account`와 `quote` 명령을 실행한다.


## Stage 2 — 모의계좌 지정가 주문·정정·취소·레저·reconcile (#711)

운영자 결정(`decision/2026-09-25/nhplug-stage2-mock`) 승인 범위 원문: "NH나무 **모의계좌 한정**: 잔고·포지션·미체결·체결 조회 + **지정가(limit)** 주문·정정·취소 + 레저·reconcile. kiwoom_mock 어댑터 미러." / "제외: 실계좌 전부, 시장가 주문, 스케줄러 등록(별도 승인), 계좌 배정(연동 완료 후 별도 결정)."

완료 기준(결정문): 장중 모의 스모크 왕복 실증(주문→조회→정정→취소→reconcile)과 "빈 배열 ≠ 미체결 없음" 문제 부재 검증. **이 스모크는 머지 후 운영자(operator-desk)가 mac-personal에서 KRX 정규장 중에 실행한다.** 구현자는 실행하지 않았다(개발 중 NH API 호출 0회, 합성 fixture·fake transport만 사용).

### 안전 경계 요약

- 주문 path는 `client.py`의 `/krstock/order/v1/{cashBuy,cashSell,modify,cancel}` 4개뿐. 본문은 `nmn_pr_tp_cd=01`(지정가)·`orr_cnd_dit_cd=00`·`rmt_mkt_cd=KRX`·`sor_mkt_sli_yn=N` 고정. 시장가는 도구(`limit_orders_only`), client 진입, build 후 send 직전 3곳에서 거부된다.
- 매 작업마다 `/n2/acctinfo`로 `acct_type=03`을 새로 검증하고, build된 요청 바이트의 `act_no`를 send 직전에 다시 대조한다. 01/02·상충 type 응답은 거부.
- `NHPLUG_MOCK_ENABLED=true`는 **프로세스 환경변수**여야 한다(파일에만 있으면 모든 dispatch가 fail-closed). 모든 주문은 `dry_run=False` + `confirm=True`(CLI는 `--confirm-mock-order`).
- 레저 `review.nhplug_mock_order_ledger`: broker 전송 **전** `submitting` 행 커밋(레저 없으면 전송 없음). 주문번호가 읽혀야만 `accepted`. send 이후 실패는 `acceptance_uncertain`(재시도 금지). 체결·종결 상태는 reconcile만, 두 조회 소스가 합의한 증거로만 기록.
- 미체결: `ost_cns_dit=2`(미체결)와 `ost_cns_dit=0`(전체) 두 조회가 모두 완전하고 합의할 때만 `none_confirmed`. 빈 배열·블록 누락·오류형 응답·페이지 미완료는 `unknown`.

### 개발 중 검증하지 못한 벤더 가정 (스모크가 확인)

1. 조회 응답의 `itg_orr_no`(통합주문번호)가 주문 응답의 `mkt_orr_no`(시장주문번호)와 같다(KRX·비SOR). 다르면 4단계에서 `placed order is not visible as open` 으로 중단된다.
2. 정정은 새 주문번호를 돌려주고 원주문 수량은 `cor_qty`로 옮겨진다(다르면 reconcile이 `unknown`을 남긴다 — 종결로 오판하지 않는다).
3. 모의 호스트가 `dailyOrderExecution`을 지원한다(미지원이면 0단계 baseline이 `unknown`).
4. 모의 시세(`currentPrice`)는 벤더 문서상 미지원(IGW40023)이므로 가격은 다른 소스로 정한다.

### 사전 준비 (mac-personal)

1. canonical repo `main`을 머지 커밋 이상으로 갱신: `cd /Users/mgh3326/work/auto_trader && git fetch --prune origin && git switch main && git pull --ff-only`.
2. 레저 DB 결정: CLI는 앱 설정의 `DATABASE_URL`(기본 `.env`, 또는 `ENV_FILE=<non-prod 파일>`)을 쓴다. `ENV_FILE`에 `prod`가 들어가면 거부한다. **권장: 로컬 개발 DB**(운영 DB 사용 여부는 운영자 결정). 그 DB에 마이그레이션 적용: `uv run alembic upgrade head` (새 revision `20260925_task711_nhplug_ledger`, 테이블 1개 추가만).
3. 전용 자격증명 파일 `.env.nhplug-mock.native`(Stage 1과 동일, 정확히 `NHPLUG_APP_KEY`/`NHPLUG_APP_SECRET`/`NHPLUG_MOCK_ACCOUNT_NO` 3키). 내용을 출력하거나 복사하지 않는다.
4. 가격 결정(KRX 09:00–15:20, 동시호가 제외): 대상 `005930`. auto_trader `get_quote`(KIS) 등으로 현재가 `C`를 확인하고, **체결되지 않을 매수 지정가**를 틱 단위로 정한다. 예) `C=71,000`이면 `P1=67,400`(약 -5%), `P2=67,000`(정정가, P1보다 한두 틱 아래). 둘 다 상·하한가(±30%) 안, 5만~20만원 구간 틱=100원.

### 실행 순서와 기대값

모든 명령은 repo 루트에서, 명령 단위로만 게이트를 켠다. 출력은 줄마다 JSON이며 키·토큰·계좌번호·고객명·원문 body는 포함되지 않는다. **출력 전체를 보고서에 붙인다.**

```bash
F=.env.nhplug-mock.native
# 0) 오프라인 preflight (네트워크 0회)
NHPLUG_MOCK_ENABLED=true uv run python -m scripts.nhplug_mock_order_smoke --env-file $F --mode preflight
#    기대: status=ready, network_calls=0, mutation_path_count=4, order_type=limit_only

# 1) 잔고/포지션
NHPLUG_MOCK_ENABLED=true uv run python -m scripts.nhplug_mock_order_smoke --env-file $F --mode positions --confirm-read
#    기대: 0_account_verified(acct_type=03) 다음 positions.success=true, cash.orderable_krw >= P1

# 2) 미체결 baseline (빈 배열 검증 1차)
NHPLUG_MOCK_ENABLED=true uv run python -m scripts.nhplug_mock_order_smoke --env-file $F --mode open-orders --confirm-read
#    기대: open_orders_state=none_confirmed(기존 주문 없을 때) 또는 present.
#          sources[0](all)·sources[1](open) 모두 complete=true, response_codes 기록.
#          unknown이면 중단하고 reasons/sources를 보고(벤더 가정 3 또는 빈 배열 문제).

# 3) 왕복: 주문 -> 조회 -> 정정 -> 조회 -> 취소 -> 조회 -> reconcile -> 빈 배열 확인 (토큰 1회)
NHPLUG_MOCK_ENABLED=true uv run python -m scripts.nhplug_mock_order_smoke --env-file $F \
  --mode roundtrip --confirm-mock-order --symbol 005930 --quantity 1 --price <P1> --modify-price <P2>
```

3)의 단계별 기대값:

| step | 기대 |
|---|---|
| `1_positions` | `success=true` |
| `2_open_orders_before` | `unknown`이 아님 |
| `3_place_limit_buy` | `status=accepted`, `broker_order_id=N1`, `ledger_id` 존재, `dispatch_started=true` |
| `4_open_orders_after_place` | `open_orders_state=present`, `open_orders[].order_no`에 `N1` (가정 1 검증) |
| `5_modify_limit_price` | `status=accepted`, `broker_order_id=N2`, `full_quantity=true` |
| `6_open_orders_after_modify` | `N2`가 open |
| `7_cancel_full` | `status=accepted`, `original_verified_by=broker_listing` |
| `8_open_orders_after_cancel` | `N2`가 open 목록에 없음 |
| `9_reconcile_apply` | `success=true`, `unresolved=0`; place행 `modified`, modify행 `cancelled`, cancel행 `confirmed`, 모두 `verified` |
| `10_empty_listing_check` | `open_orders_state=none_confirmed`(다른 주문 없을 때), 두 source `complete=true` |
| `summary` | `status=ok`, `empty_listing_is_two_source_confirmed=true` |

```bash
# 4) 빈 배열 검증 2차 (독립 프로세스로 다시)
NHPLUG_MOCK_ENABLED=true uv run python -m scripts.nhplug_mock_order_smoke --env-file $F --mode open-orders --confirm-read
#    기대: none_confirmed, 두 source complete=true. open source response_codes(예: 13578=조회할 내역 없음)를 보고에 기록.

# 5) 체결/주문 이력과 레저 증거
NHPLUG_MOCK_ENABLED=true uv run python -m scripts.nhplug_mock_order_smoke --env-file $F --mode history --confirm-read --scope all
uv run python - <<'PY'
import asyncio
from sqlalchemy import text
from app.core.db import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as s:
        rows = await s.execute(text("SELECT id, operation_kind, status, reconcile_state, broker_order_id, original_order_id, filled_qty, cancelled_qty FROM review.nhplug_mock_order_ledger ORDER BY id DESC LIMIT 10"))
        for r in rows: print(tuple(r))
asyncio.run(main())
PY
#    기대: 3행(place=modified, modify=cancelled, cancel=confirmed), reconcile_state=verified
```

MCP로 같은 흐름을 돌릴 경우(선택): `NHPLUG_MOCK_ENABLED=true` + `NHPLUG_APP_KEY`/`NHPLUG_APP_SECRET`/`NHPLUG_MOCK_ACCOUNT_NO`를 MCP 서버 **프로세스 환경**에 둔 DEFAULT 프로필에서 `nhplug_mock_get_positions` → `nhplug_mock_get_open_orders` → `nhplug_mock_place_order(symbol, side="buy", quantity=1, price=P1, dry_run=False, confirm=True)` → `nhplug_mock_get_open_orders` → `nhplug_mock_modify_order(order_id=N1, symbol, new_price=P2, dry_run=False, confirm=True)` → `nhplug_mock_get_open_orders` → `nhplug_mock_cancel_order(order_id=N2, symbol, dry_run=False, confirm=True)` → `nhplug_mock_get_open_orders` → `nhplug_mock_reconcile_orders(dry_run=False)` → `nhplug_mock_get_open_orders` 순서이며 기대값은 위 표와 같다. CLI와 같은 `operations` 함수를 쓴다.

### 중단(abort) 절차

- roundtrip은 예상 밖 상태·예외·Ctrl-C에서 `step=abort` 한 줄을 출력하고, 살아 있는 테스트 주문(`live_test_order_id`)을 전량 취소 시도(`cleanup_cancel`)한 뒤 exit 2로 끝난다. **자동 재주문·재시도는 없다.**
- abort 후: `--mode open-orders --confirm-read`로 확인. 테스트 주문이 남아 있으면 `NHPLUG_MOCK_ENABLED=true uv run python -m scripts.nhplug_mock_order_smoke --env-file $F --mode cancel --confirm-mock-order --symbol 005930 --order-id <번호>`. 그것도 `original_order_unverified` 등으로 거부되면 NH나무 모의투자 앱/HTS에서 수동 취소한다.
- 정리 후 `--mode reconcile --confirm-read --apply`로 레저를 맞추고 결과(`unresolved`)를 보고한다. `acceptance_uncertain` 행은 재주문하지 말고 보고한다.
- 매수가 체결돼 버렸다면(가격 과대) 1주가 모의 잔고에 남는다: `--mode place --confirm-mock-order --side sell --quantity 1 --price <매수1호가 이하>`로 정리 후 reconcile.
- 즉시 차단: 명령 앞의 `NHPLUG_MOCK_ENABLED=true`를 빼면 모든 dispatch가 fail-closed.

### 중단하고 보고해야 하는 판정

- 2) baseline 또는 4) 재확인이 `unknown` — 빈 배열/오류형 응답 문제 또는 모의 호스트의 조회 미지원. `sources[*].reason`, `response_codes`를 그대로 보고.
- 4단계 `placed order is not visible as open` — 벤더 가정 1(주문번호 동일성) 불성립. 주문을 취소하고 보고(후속 수정 필요).
- 9단계 `unresolved > 0` 또는 `source_disagreement` — 두 조회 소스 불일치. 결과 JSON 보고.
