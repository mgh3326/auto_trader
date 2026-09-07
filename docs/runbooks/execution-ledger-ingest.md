# Execution Ledger HTTP Ingest (fillwire P0)

체결 수집을 Go 데몬 `fillwire`로 옮기기 위한 **P0 계약**이다. Go는 DB에 직접 쓰지
않고 auto_trader의 토큰 인증 HTTP 표면으로 원장에 넣는다. 이 PR에서는 계약과
Python 모니터의 sink 스위치까지만 만들었다 — **Go 코드 0줄, Redis Streams 0줄,
스케줄러 등록 0건, 마이그레이션 0건**이다.

- 라우터: `app/routers/execution_ledger_ingest.py`
- 스키마: `app/schemas/execution_ledger_ingest.py`
- 공유 서비스: `app/services/execution_ledger/fill_ingest.py`(upsert + 다운스트림),
  `app/services/execution_ledger/fill_sinks.py`(모니터 sink),
  `app/services/reconcile_trigger.py`(재연결 트리거)
- 미들웨어 브랜치: `app/middleware/auth.py` — `EXECUTION_LEDGER_INGEST_PATH_PREFIX`

---

## 1. 인증

두 엔드포인트 모두 **전용 머신 토큰**만 받는다. 세션 쿠키는 절대 대체되지 않는다
(토큰 브랜치가 generic auth보다 앞에 있다).

| 설정 키 | 기본값 | 의미 |
|---|---|---|
| `EXECUTION_LEDGER_INGEST_TOKEN` | `""` | 미설정 → **403** (fail-closed) |
| `EXECUTION_LEDGER_INGEST_TOKEN_HEADER` | `X-Execution-Ledger-Ingest-Token` | 공백 → **403** |

- 토큰 불일치/누락 → **401**
- 비교는 `hmac.compare_digest`
- 🔴 **토큰 값은 repo·로그·응답·커밋에 남기지 않는다.** `env.example`에는 키 이름과
  빈 값만 있다. 실제 값 배치는 운영자(admiral) 몫이며 이 PR 범위 밖이다.

---

## 2. `POST /trading/api/execution-ledger/fills/ingest`

### 요청

```jsonc
{
  "fills": [ /* ExecutionLedgerUpsert 모양 객체 1..200개 */ ],
  "source": "fillwire" | "websocket_monitor",
  "source_run_id": "uuid (선택)"
}
```

- 배치 크기는 **1..200**. `0`이나 `201` 이상은 envelope 검증 오류(422)다.
- `source`는 **transport provenance**(누가 보냈나)이며 원장 행의 `source` 컬럼이
  아니다. 알 수 없는 값은 422.

### 항목별 계약 (전체 롤백 아님)

응답 `results[i]`는 요청 `fills[i]`와 **순서가 정확히 대응**하고, 필드는 정확히
세 개다.

```jsonc
{"status": "inserted|updated|unchanged|rejected", "row_id": 123|null, "reason": null|"..."}
```

- 개별 fill의 검증 실패나 DB 실패는 **그 항목만** `rejected`가 되고, 같은 배치의
  다른 항목은 정상 커밋된다(항목마다 savepoint).
- `reason`은 300자 상한의 정제된 한 줄이며 **payload 값을 그대로 되비추지 않는다.**
- 멱등키는 기존 DB unique key `(broker, account_mode, venue, broker_order_id,
  fill_seq)` 그대로다. 같은 payload를 두 번 보내면 두 번째는 `unchanged` +
  **같은 `row_id`** 이므로 **부분 재전송이 안전**하다.

### 서버가 강제하는 것

- **행의 `source`는 항상 `websocket`으로 강제된다.** producer가 항목 안에
  `reconciler`/`manual_import`를 넣어도 무시된다 — 이 transport 자체가 websocket
  tap이며, producer가 reconciler 증거를 자칭할 수 없다.
- **outer `source_run_id`가 transport run authority다.** 항목이 자기 값을
  선언해도 outer 값이 이긴다. outer가 없으면 항목 값이 남는다.
- `raw_payload_json`은 저장 전에 `_redact_sensitive_keys`가 다시 적용된다.

### 다운스트림 (알림 / Upbit rung 투영)

커밋 **이후에**, 웹소켓 모니터가 쓰는 것과 **같은** 함수
(`run_post_upsert_downstream`)가 항목마다 한 번 돈다. 라우터는
`websocket_monitor.py`를 import 하지 않으며 로직을 복제하지 않는다.

- `raw_payload_json`이 있으면 모니터와 동일한 normalizer로 `FillOrder`를 만든다.
- `raw_payload_json`이 **없어도** canonical 필드로 `FillOrder`를 재구성해
  알림이 나간다. raw frame은 Upbit 누적 `executed_volume`(rung 투영 증거) 같은
  추가 문맥용이며, 없다는 이유로 알림을 통째로 생략하지 않는다.
- 중복 행(`updated`/`unchanged`)은 알림을 억제한다. 단 Upbit 소액 체결이
  두 번째 전달에서야 rung 투영에 성공한 경우는 예외로 한 번 알린다(기존 의미 유지).
- 🔴 **원장 커밋이 권한자다.** 다운스트림 실패는 로그로 드러날 뿐, 이미 커밋된
  fill을 `rejected`로 세탁하거나 롤백/재삽입하지 않는다.

### curl (시크릿은 플레이스홀더)

```bash
curl -sS -X POST http://127.0.0.1:8000/trading/api/execution-ledger/fills/ingest \
  -H "Content-Type: application/json" \
  -H "X-Execution-Ledger-Ingest-Token: $EXECUTION_LEDGER_INGEST_TOKEN" \
  -d '{
        "source": "fillwire",
        "source_run_id": "00000000-0000-0000-0000-000000000000",
        "fills": [{
          "broker": "upbit",
          "account_mode": "live",
          "venue": "upbit_krw",
          "instrument_type": "crypto",
          "symbol": "BTC",
          "raw_symbol": "KRW-BTC",
          "side": "buy",
          "broker_order_id": "<broker order uuid>",
          "fill_seq": 0,
          "filled_qty": "0.0003",
          "filled_price": "92800000",
          "filled_at": "2026-09-07T03:00:00+00:00",
          "currency": "KRW",
          "source": "websocket"
        }]
      }'
```

---

## 3. `POST /trading/api/execution-ledger/reconcile/trigger`

재연결 직후 공백 구간 백필용. **새 reconcile 로직은 하나도 없다** — 기존 커널을
그대로 부른다.

```jsonc
{"market": "kr" | "us" | "crypto", "dry_run": true, "reason": "reconnect"}
```

- `dry_run` 기본값 **`true`**.
- `reason`은 **정확히 `"reconnect"`** 리터럴이다. 다른 문자열은 422.
- 디스패치: `kr` → `kis_live_reconcile_orders_impl`, `us` →
  `live_reconcile_orders_impl(market="us", broker="kis")`, `crypto` →
  `live_reconcile_orders_impl(market="crypto", broker="upbit")`.

### 응답

| 필드 | 의미 |
|---|---|
| `status` | `executed` / `deduped` / `failed` |
| `deduped` | 60초 창 안 중복 호출이면 `true` (커널 미진입) |
| `backfilled` | **실제 커밋된 booking 건수**. 커널 결과의 `action`이 `booked*`인 행만 센다 |
| `dedupe_window_seconds` | 현재 창 크기 |
| `kernel` | 커널 결과의 **bounded** 투영(성공 여부, 검증 카운트, 스캔 커버리지, 300자 절단 메시지). 원본 `reconciled` 행 목록은 노출하지 않는다 |
| `error` | 실패 시 예외 **클래스 이름**만 (raw exception 문자열/시크릿 노출 없음) |

- 🔴 **dry-run은 언제나 `backfilled: 0`이다.** `would_book*`은 커밋이 아니다.
- dedupe는 **최초 시작 시각 기준 60초**, 프로세스 로컬 monotonic 시계, market별
  1개 엔트리(상한 내장). 동시 요청도 하나만 커널에 진입한다.
- dedupe는 스키마/Redis/스케줄러를 쓰지 않는다. 프로세스가 여러 개면 창은
  프로세스마다 별개다 — 놓친 dedupe는 idempotent reconcile 한 번 더일 뿐이다.
- fill-evidence 게이트, dry-run 기본값, live broker 안전 인바리언트는 **변경 없음**.

```bash
curl -sS -X POST http://127.0.0.1:8000/trading/api/execution-ledger/reconcile/trigger \
  -H "Content-Type: application/json" \
  -H "X-Execution-Ledger-Ingest-Token: $EXECUTION_LEDGER_INGEST_TOKEN" \
  -d '{"market": "kr", "dry_run": true, "reason": "reconnect"}'
```

---

## 4. `WS_LEDGER_SINK` — Python 모니터 sink 스위치

| 값 | 동작 |
|---|---|
| `db` (**기본값**) | 기존 그대로: 모니터가 서비스 레이어로 직접 커밋하고, 다운스트림도 모니터가 실행 |
| `http` | 같은 정규화 `ExecutionLedgerUpsert`를 위 ingest API로 POST. **다운스트림은 API 서버가 소유**하므로 모니터는 중복 실행하지 않는다 |

🔴 **`EXECUTION_LEDGER_COMMIT_ENABLED=false` 인 동안에는 sink 자체가 호출되지 않는다**
(`_record_execution_ledger_fill`이 그 전에 반환). 이때는 `http` 모드여도 **모니터가
알림을 계속 소유**하므로 기존과 동일하게 알림 1회가 나가고 DB/HTTP write 는 0이다.
sink 소유권은 "http 모드" 하나가 아니라 "http 모드 **AND** commit gate on"이다.

인식하지 못하는 값은 경고 후 `db`로 떨어진다.

### 🔴 `WS_LEDGER_SINK_URL` 신뢰 경계 (토큰이 실린다)

이 요청은 **ingest 토큰을 헤더에 실어 보낸다.** 따라서 URL override가 토큰을 다른
곳으로 내보낼 수 없도록 다음을 **sink 생성 시와 매 전송 직전에 각각** 검증한다
(`assert_loopback_ingest_url`):

- scheme = `http` 만 (`https`/`file`/기타 거부)
- host = loopback 만 (`127.0.0.1` / `localhost` / `::1`). `127.0.0.1.evil.example.com`
  같은 접미사 우회는 거부
- path = **정확히** `/trading/api/execution-ledger/fills/ingest`
- userinfo(`user:pw@`) · query · fragment **전부 금지**
- `follow_redirects=False` 를 클라이언트 생성과 매 POST 호출 양쪽에 **명시** 고정 —
  검증 통과한 요청이 다른 호스트로 갈 유일한 경로가 3xx 이기 때문이다. 3xx 응답은
  non-2xx 이므로 실패로 처리되고 리다이렉트 대상은 요청되지 않는다

거부된 URL은 **소켓을 열기 전에** 직접 DB 경로로 fail-open하고 `url_blocked`
카운터를 올린다(큐에 남아 있던 항목도 같이 내린다 — 절대 통과할 수 없는 URL을
재시도로 붙잡아 두지 않는다). 경고 로그에는 **토큰도 URL 문자열도 남기지 않고**
거부 사유 코드(`host_not_loopback` 등)만 남는다.

### 유실 0 정책 (retry → fail-open)

1. POST 실패(전송 오류/타임아웃/non-2xx/malformed/`rejected`)는 **bounded FIFO
   재시도 큐**에 넣고 경고한다.
2. 다음 전달은 큐를 먼저 비우므로 브로커 순서가 보존된다.
3. `WS_LEDGER_SINK_MAX_ATTEMPTS`를 소진했거나 `WS_LEDGER_SINK_MAX_QUEUE` 상한에
   걸려 자리를 비워야 하면 **버리지 않고** 직접 DB 경로로 **fail-open**하며
   `sink_fallback` 카운터가 오른다(이 경로는 다운스트림도 함께 실행).
4. 종료 시 `stop()`이 sink를 `flush()`해 큐에 남은 fill을 같은 fallback으로 내린다.

🔴 **남는 유실 경계 하나**: DB fallback 자체가 실패하면 넣을 곳이 없다. 이때는
조용히 사라지지 않고 ERROR 로그 + `sink_fallback_failed` 카운터로 드러나며,
그 구간은 §3의 reconcile 트리거로 메운다.

### 관측

heartbeat 파일과 health 로그에 `ledger_sink`가 실린다:

```json
{"mode": "http", "queue_depth": 0, "http_success": 12, "http_failure": 1,
 "sink_fallback": 1, "sink_fallback_failed": 0, "url_blocked": 0}
```

카운터와 모드만 담으며 **토큰·payload는 절대 출력하지 않는다.**

---

## 5. 이 PR의 경계 (P0에 **없는** 것)

- Go 코드 (`fillwire` 바이너리) — spec §3 P1/P2
- Redis Streams 내구 큐 — spec §6 v2 참고 계약일 뿐, P0 범위 아님
- TaskIQ/cron/Prefect 스케줄 등록 — **0건**
- DB 마이그레이션/스키마 변경 — **0건** (`source` CHECK, unique key 모두 불변)
- 브로커 주문 mutation — **0건**. `tests/services/execution_ledger/test_no_broker_mutation.py`가
  계속 강제한다
- 운영 토큰 배치 / 배포 / merge — 운영자(admiral) 몫

## 6. 활성화 순서 (운영자)

1. 코드 배포 (기본값 그대로면 동작 변화 없음 — `WS_LEDGER_SINK=db`, 토큰 미설정 → 403)
2. secret store에 `EXECUTION_LEDGER_INGEST_TOKEN` 배치 → API 재시작
3. §2 curl로 단건 ingest 왕복 확인(멱등 2회 → 두 번째 `unchanged` + 같은 `row_id`)
4. §3 curl로 `dry_run=true` 트리거 확인(`backfilled: 0`, 60초 내 재호출 `deduped`)
5. 모니터에 `WS_LEDGER_SINK=http` + 같은 토큰 배치 → 재시작 후 heartbeat의
   `ledger_sink.http_success` 증가 및 `sink_fallback` 0 확인
6. 문제 시 되돌리기 = `WS_LEDGER_SINK=db` 로 재시작 (마이그레이션 롤백 불필요)
