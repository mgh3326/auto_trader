# US & Crypto Live Order Reconcile (ROB-407)

## What changed
US/해외 (`equity_us`) 및 crypto (`crypto`) live 주문 전송 시, fill/journal/realized_pnl을 즉시 선반영하지 않고 새 제네릭 테이블 `review.live_order_ledger`에 `accepted` (또는 `rejected`) 상태로 Accepted-only 기록만 남깁니다.
장부는 오직 **`live_reconcile_orders`** 도구를 통해 수집된 broker별 체결 증거(evidence)를 바탕으로만 최종 확정(book)됩니다.

- **US/해외 주문 (`kis` broker):** KIS 해외 일별주문 내역(`inquire_daily_order_overseas`)을 순회하며 체결 증거를 수집한 후 canonical 키로 정규화하여 `classify_fill_evidence`로 판정을 내려 반영합니다.
- **Crypto/Upbit 주문 (`upbit` broker):** Upbit 주문 상세 조회 API(`fetch_order_detail`)를 사용하여 `uuid`별 상태 및 체결 수량을 대조하여 반영합니다.
- **시장가 Crypto 주문 (Inline Confirm):** 시장가 Upbit 주문은 전송 직후 체결이 즉시 완료되므로, 전송 직후 즉시 inline으로 `fetch_order_detail`을 조회해 1회성 Reconcile을 자동 수행(선반영 없는 확정 체결 기록)합니다.

## Reconcile workflow
1. **주문 제출:** `kis_live_place_order(..., dry_run=False)` 또는 Upbit 주문 실행. `broker_status: "accepted"`, `fill_recorded: false` 확인.
2. **체결 대기:** 주문이 체결되거나 취소/만료될 때까지 대기.
3. **Reconcile 드라이런:**
   ```bash
   # live_reconcile_orders MCP 도구를 default인 dry_run=True로 실행
   live_reconcile_orders(dry_run=True)
   ```
   출력되는 각 주문의 verdict (filled / partial / pending / cancelled) 및 예상 booking 내용 확인.
4. **Reconcile 실행:**
   ```bash
   # dry_run=False로 실행하여 실제 trades/journals에 체결 반영
   live_reconcile_orders(dry_run=False)
   ```
   - 특정 시장/종목/주문번호로 대상을 한정하려면 `market`, `broker`, `symbol`, `order_id` 매개변수 사용 가능.

## Verdicts
- **`filled` / `partial`**
  - broker 확정 체결 수량/단가로 `review.trades`에 기록.
  - 매수 주문 시 `review.trade_journals`에 draft 저널을 작성하고 fill과 링크하여 active로 전환.
  - 매도 주문 시 기존 active 저널들을 FIFO 순서로 매도 체결 수량만큼 종결 처리.
  - **델타 멱등(Delta-Idempotent) booking:** 부분체결이나 여러 번 나누어 Reconcile을 실행할 경우, `LiveOrderLedger.filled_qty` 누적값과의 차이(델타)만큼만 추가 booking하여 중복 반영을 완벽 차단합니다.
- **`pending`**
  - 아직 체결되지 않고 대기 중인 상태. 아무런 장부 반영 없이 무시(re-run later).
- **`cancelled`**
  - broker 증거상 취소/종료되었으며 체결 수량이 0인 상태. ledger row 상태만 `cancelled`로 업데이트하고 저널 부작용 없이 완료.
- **`anomaly`**
  - reconcile 수행 중 에러 발생.

## `journals_closed=0` 진단 (ROB-955)

매도 verdict가 `filled`/`partial`인데도 `journals_closed=0` (`security_pnl_usd=null` 등
저널 기반 필드가 비는 경우)는 **두 가지 서로 다른 원인**에서 발생한다. 운영자가
구분하지 않으면 둘 다 "저널 누락(버그)"으로 오독하기 쉽다.

| 원인 | 판별 | 조치 |
|------|------|------|
| **A. 데이터 공백** — 매수 저널 원천이 아예 없음(저널 시스템 도입 이전 legacy 포지션 등) | `SELECT ... FROM review.trade_journals WHERE symbol = '<SYM>' AND status = 'active'` 이 **0행** | retro(trade_retrospectives) 수기 보정으로 정본화. 자동 백필은 하지 않음(ROB-955에서 백필 안 함으로 확정) |
| **B. FIFO 부분매도 < 랏 (의도된 설계)** | 활성 저널이 **실재**하지만, 매도 수량이 가장 오래된 활성 저널의 `quantity`(랏 크기)보다 작음 | **정상 동작, 무조치.** `_close_journals_on_sell` (`app/mcp_server/tooling/order_journal.py`)의 FIFO 워크가 no-lot-splitting 정책상 아무 랏도 소비하지 않고 멈춘 것 — 버그 아님 |

원인 B의 예: 활성 KIS BAC 저널(qty 3)에 매도 qty 2 < 3 → FIFO break, 저널 미종결.
원인 A의 예: XOM/AMZN 등 `review.trade_journals` 행 자체가 0인 심볼.

**향후 개선안 (스코프 밖):** reconcile 응답에 `no_active_journal`(원인 A) vs
`partial_sell_below_lot`(원인 B)을 구분하는 진단 필드를 추가하면 운영자가 SQL
없이도 원인을 즉시 구분할 수 있다. 코드 변경이 필요해 이 문서화 전용 이슈
(ROB-959)의 스코프 밖이며 별도 이슈로 후속 검토.

## Migration & Deployment
운영 적용 시, 아래 Alembic 명령어를 수행하여 `review.live_order_ledger` 테이블을 생성해야 합니다:
```bash
uv run alembic upgrade head
```
