# ROB-407 — live 주문 선반영 차단: US/해외 + crypto 확장

**Date:** 2026-06-01
**Issue:** ROB-407 (오케스트레이션 트래커 ROB-409 A라인)
**선행:** ROB-395 (KR domestic accepted-only ledger + evidence-gated reconcile, Done/merged `bbe317a9`)

## 배경 / 문제

`app/mcp_server/tooling/order_execution.py::_execute_and_record`는 `market_type == "equity_kr"` & live일 때만
ROB-395의 accepted-only ledger 경로(`_record_kis_live_order`)로 분기한다. **US/해외(`equity_us`)·crypto(`crypto`)
live 주문은 여전히 전송 시점에 `_record_fill_and_journals`를 호출해 preview 값으로 fill/journal/realized_pnl을
선반영한다.** 지정가 미체결 시 KR과 동일한 장부 괴리(유령 active journal)·SELL realized_pnl 선계상 위험이 존재한다.

ROB-395에서 이 두 시장은 evidence 소스가 달라 **명시적으로 스코프 제외**되었다. 본 작업은 그 후속 확장이다.

### 코드 검증된 사실
- 결함 실재: `_execute_and_record` 비-KR live 분기가 `_record_fill_and_journals`로 즉시 booking.
- US/해외: `inquire_daily_order_overseas`의 `order_number`(ODNO)는 "해외주식 미지원"으로 항상 빈 값 전송 →
  거래소(NASD/NYSE/AMEX) 후보 순회 + client-side order_no/symbol 필터 필수
  (참고: `orders_modify_cancel._find_us_order_in_recent_history`).
- crypto: Upbit `fetch_order_detail(uuid)` + `state`(`wait`/`done`/`cancelled`) → `_map_upbit_state`가 이미
  pending/filled/cancelled/partial 매핑, `executed_volume`/`avg_price` 노출.

## 설계 결정 (brainstorm)

1. **제네릭 단일 테이블** `review.live_order_ledger` — US/해외 + crypto 공용. 기존 `kis_live_order_ledger`는
   KIS 전용 스키마(`krx_fwdg_ord_orgno`, `broker="kis"`/`account_mode="kis_live"` CHECK 하드코딩)라 그대로 확장은
   지저분. **KR domestic은 기존 테이블에 그대로 유지** → ROB-395 경로 무회귀. KR 흡수는 별도 이슈로 보류.
2. **crypto 시장가 = 전송 직후 inline evidence 확인.** 시장가도 accepted-only로 쓰되 같은 호출 내
   `fetch_order_detail`로 `state=done` 증거를 확인해 fill 반영. 증거 불충분이면 accepted로 남기고 reconcile에 위임.
   지정가 pending은 무조건 evidence-gated.
3. **단일 스펙 + 2 PR.** evidence 소스가 완전히 달라 한 PR이 비대해짐.
   - PR1 = 제네릭 ledger + 라우팅 전환 + US/해외 evidence 어댑터 + reconcile
   - PR2 = crypto/Upbit evidence 어댑터 + 시장가 inline 확인
4. **account_scope 네이밍 규약 준수** (ROB-297): US 단일 `market="us"`, 브로커는 account_scope로 구분.
   `kis_overseas_live` 같은 alias 금지.

## 데이터 모델

새 테이블 `review.live_order_ledger` (alembic 마이그레이션 1개, production `alembic upgrade`는 operator-gated):

```
live_order_ledger
  id (PK)
  broker            CHECK in (kis, upbit)
  account_scope     # 브로커-계좌 판별자 (예: kis_live, upbit_live) — alias 금지
  market            # us / crypto
  symbol            # DB dot-format (예: BRK.B)
  exchange          # nullable — US: NASD/NYSE/AMEX
  market_symbol     # nullable — crypto: KRW-BTC
  order_no          # KIS odno / Upbit uuid
  side              # buy / sell
  ord_qty, ord_price
  order_kind        # market / limit
  status            # accepted|pending|partial|filled|cancelled|rejected
  filled_qty        # 이미 booked된 누적 체결량 (델타 멱등용)
  avg_fill_price    # reconcile 확정 평단
  trade_id, journal_id            # reconcile booking 결과
  thesis, strategy, target_price, stop_loss, min_hold_days, exit_reason, indicators_snapshot  # intent
  created_at, updated_at
  UNIQUE(broker, account_scope, order_no)
```

## 컴포넌트

1. **Model** — `app/models/review.py::LiveOrderLedger`.
2. **Ledger 서비스** — `app/mcp_server/tooling/live_order_ledger.py`:
   - `_record_live_order(...)` — accepted-only writer (fill/journal 없음).
   - `live_reconcile_orders_impl(market=None, dry_run=True, limit=...)` — 제네릭 reconcile, broker별 어댑터 디스패치.
3. **Evidence 어댑터** — 공통 protocol `fetch_evidence(row) -> rows`; 분류는 `classify_fill_evidence` 재사용,
   broker별 필드키 매핑만 다름:
   - `UsOverseasEvidenceAdapter` — NASD/NYSE/AMEX 순회 `inquire_daily_order_overseas(symbol="%")` →
     order_no + symbol(`to_db_symbol` 정규화) client-side 필터. `_find_us_order_in_recent_history` 로직 재사용/추출.
   - `UpbitEvidenceAdapter` — `fetch_order_detail(uuid)` → state/executed_volume/avg_price.
4. **라우팅 변경** — `order_execution.py::_execute_and_record`의 `equity_us`/`crypto` live 분기를
   `_record_live_order` accepted-only로 전환. crypto 시장가는 전송 직후 `UpbitEvidenceAdapter`로 inline 확인.
5. **Reconcile MCP 도구** — 신규 `live_reconcile_orders`(dry_run 기본, `market` 필터 옵션).
   기존 `kis_live_reconcile_orders`(KR)는 그대로 유지.
6. **Fill/journal 적용** — 기존 `_save_order_fill` / `_create_trade_journal_for_buy` / `_close_journals_on_sell`
   재사용 (broker-agnostic).

## 데이터 플로우

**전송 (live US/해외, crypto 지정가):**
```
preview → submit_order → broker accept
  → live_order_ledger insert (status=accepted, fill/journal 없음)
  → response: {broker_status, fill_recorded:false, ledger_id}
```

**전송 (crypto 시장가):**
```
submit → 같은 호출 내 fetch_order_detail(uuid)
  → state=done & executed_volume>0 → classify → fill+journal, status=filled
  → 증거 불충분 → accepted-only (reconcile 위임)
```

**Reconcile (operator/스케줄, dry_run 기본):**
```
open rows(accepted|pending|partial) 로드
  → broker별 어댑터로 evidence fetch
  → classify_fill_evidence → FillVerdict
    FILLED/PARTIAL → _save_order_fill + journal (BUY=open, SELL=close+realized_pnl)
    CANCELLED/REJECTED → status=cancelled, journal 없음
    PENDING → 유지
```

## 에러 처리

- **Fail-closed**: evidence fetch 실패/미발견 → row open 유지, **절대 booking 안 함**
  (유령 journal 구조적 불가, ROB-395 원칙 계승).
- **부분체결 멱등성**: 이미 booked한 `filled_qty` 저장 → reconcile는 `broker_cumulative − already_booked`
  **델타만** booking (재실행 이중계상 방지, ROB-400 교훈).
- **US 거래소 순회**: 어느 거래소 recent history에도 order_no 없으면 pending 유지 (취소/만료 가능성 — 단정 금지).
- **SELL realized_pnl**: 확정 체결 증거에서만 계상.

## 테스트 (fake client / dry-run / read-only, 실 live 제출 0)

- 전송=accepted-only: US/crypto-limit 전송 후 journal/trade 미생성, ledger row만 (`status=accepted`).
- reconcile booking: FILLED 증거 → fill+journal; CANCELLED → journal 없음; PENDING → 무변화.
- 부분체결 델타 멱등: 동일 reconcile 2회 → 이중 booking 없음.
- crypto 시장가 inline: done 응답 → 즉시 filled; 불충분 → accepted.
- 필드키 매핑: US row(`ccld_qty`...) / Upbit row(`executed_volume`...) 각각 verdict 정확.
- KR 회귀 가드: `equity_kr` live는 기존 `kis_live_order_ledger` 경로 그대로 (분기 무변화).

## 안전 경계

- 실제 live 주문 제출 금지. 테스트는 fake client / dry-run / read-only order-history·evidence 조회 중심.
- validation 중 broker/order/watch/order-intent mutation 금지.
- production env/secret 변경 금지. secret 값 출력 금지.
- DB 마이그레이션은 PR 안에서 리뷰 가능하게 작게, production `alembic upgrade head`는 별도 operator-gated 단계.

## 완료 기준

- `equity_us`/해외 live 지정가 주문은 전송 직후 fill/journal/realized_pnl이 확정되지 않는다.
- crypto live 지정가 pending도 전송 시 accepted-only로 남고 `done` evidence 이후에만 fill 반영된다.
- 취소/거절/부분체결/미체결이 journal/PnL에 잘못 반영되지 않는다.
- KR domestic ROB-395 경로가 회귀하지 않는다.
- 테스트·PR/CI 결과 + no-side-effect smoke evidence가 Linear 댓글에 남는다.

## 스코프 외 / 후속

- KR domestic의 제네릭 테이블 흡수 (별도 이슈).
- reconcile 스케줄러(TaskIQ/cron/Prefect) 연결 — 본 작업은 operator CLI/MCP 호출만.
- 실 live reconcile 라이브 스모크 (operator-gated; ROB-395도 dry-run만 수행됨).
