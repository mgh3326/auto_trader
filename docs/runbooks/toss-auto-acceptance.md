# Toss 자동 제출 acceptance — 운영자 전용

> TOSS-AUTO-FULL의 live acceptance 절차다. 이 문서는 운영자만 실행한다.
> 개발/검증 워커는 이 절차의 명령, MCP 도구, 계좌 읽기·주문·취소를 실행하지 않는다.

## 범위와 중단 원칙

이 acceptance는 Toss 실계좌에서 최소 1주 단위의 비시장성(limit) 주문을 사용한다. 자동
매수와 post-submit 취소가 실제로 일어나므로, 아래의 **A와 B 모두** 독립 검증 서명 후에만
Toss auto-veto 게이트를 상시 활성화할 수 있다. veto는 사전 승인이 아니라 사후 취소권이다.

다음 중 하나라도 발생하면 즉시 더 이상의 자동 주문을 중단한다.

- 원 주문 ID의 Toss broker terminal 상태가 `CANCELLED`로 확인되지 않는다.
- `toss_reconcile_orders(order_id=<원 주문 ID>, dry_run=False)`가 원 주문을 `cancelled`로
  수렴시키지 못한다.
- Telegram 카드 또는 Discord 미러에서 종목·수량·가격·사유 중 하나라도 빠진다.
- 예상 밖 체결, 부분 체결 수량 불일치, stale snapshot, ledger anomaly, Discord/Telegram
  전달 실패가 발생한다.

중단은 성공으로 해석하지 않는다. 원 주문과 cancel replacement ID, proposal ID, Telegram
message ID, reconcile 원문을 보존하고 `NEEDS_VERIFY`로 올린다.

## 사전 조건

1. 이 PR의 오프라인 fixture를 먼저 실행한다.

   ```bash
   uv run python scripts/toss_auto_acceptance_fixture.py --offline-fixture
   ```

   이 명령은 pytest fixture만 실행하며 broker/account/MCP에 접촉하지 않는다.

2. 독립 검증자가 A/B fixture, 손절·캡·terminal mutant 결과를 검토하고 서명한다. 새 scheduler,
   cron, 배포 자동화는 이 절차의 일부가 아니다.

3. 운영자만 기존 비밀 관리 경로에서 필요한 Toss/Telegram/Discord 설정을 확인한다. 값은 콘솔,
   티켓, PR에 출력하지 않는다. 최소한 다음 gate가 의도적으로 arm되어 있어야 한다.

   - `ORDER_PROPOSALS_ENABLED=true`
   - `ORDER_PROPOSALS_TELEGRAM_ENABLED=true`
   - `ORDER_PROPOSALS_AUTO_APPROVE=true`
   - `ORDER_PROPOSALS_AUTO_APPROVE_MODE=expanded` (acceptance 목적일 때만)
   - `ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED=true`
   - `TOSS_API_ENABLED=true`, `TOSS_LIVE_ORDER_MUTATIONS_ENABLED=true`

   마지막 Toss gate는 broker mutation을 허용한다. 이 작업 트리는 모두 기본 `false`이며,
   운영자 외에는 arm하지 않는다.

4. 실행 계좌/통화/세션을 한 개만 지정하고, 해당 Toss account에 이미
   `toss_auto_submission_freeze`가 없는지 확인한다. 있으면 자동 acceptance를 시작하지 말고
   기존 주문을 terminal/reconcile까지 처리한다. acceptance 주문은 반드시 사유(thesis)를
   포함하며, 현재가보다 충분히 아래인 매수 또는 위인 매도 limit으로 둔다. 시장가·시장성
   가격·손절(`loss_cut`)·본전 경계·`policy_deviation` 태그는 사용하지 않는다.
   `table_disagreement`는 감사 기록으로 남을 수 있으나 자동승인 차단 태그는 아니며,
   acceptance fixture는 혼동을 피하기 위해 어느 태그도 사용하지 않는다.

5. Discord mbp-server notification route의 해당 KR/US webhook을 확인한다. 이 구현은 새
   webhook client를 만들지 않고 `app/monitoring/trade_notifier/`의 market-routed
   `send_auto_veto_card_mirror`를 사용한다.

## Acceptance A — post-submit veto의 실취소와 reconcile

1. 지정 계좌에서 최소 금액의 **1주**, 비시장성 Toss limit `place` proposal을 생성한다.
   thesis는 한 문장 이상으로 명시한다. 자동 접수 Telegram 카드와 Discord mirror가 모두
   다음 필드를 보이는지 캡처한다.

   - 종목
   - 수량
   - 가격
   - 사유(thesis 요약)

2. card의 `취소`를 한 번만 누른다. 반환되는 cancel replacement ID는 기록하되, 성공 근거로
   쓰지 않는다. Toss cancel 응답은 **요청 수락**일 뿐 원 주문의 terminal 증거가 아니다.

3. 원 주문 ID를 대상으로 Toss closed order evidence를 읽어 `CANCELLED`를 확인한다. 이어서
   다음을 실행한다.

   ```text
   toss_reconcile_orders(order_id=<원 주문 ID>, dry_run=False)
   ```

   합격 기준은 결과가 **원 주문 ID**를 가리키며 `local_status=cancelled` 및
   `action=marked_cancelled|booked|noop_already_booked`인 것이다. replacement ID만
   `cancelled`인 결과, cancel 요청 success, 혹은 dry-run 결과는 불합격이다.

4. proposal rung과 Toss live ledger가 모두 원 주문 terminal 상태로 닫혔는지 확인한다. 어느
   하나라도 불명확하면 gate를 끄고 §원복으로 간다.

## Acceptance B — fill → 동결 → 취소 제안 → 지연 중 2차 체결 → terminal

이 절차는 부분 체결을 억지로 만들기 위해 시장성 주문을 내지 않는다. 관찰 가능한 자연
부분 체결이 없으면 실패가 아니라 **미실행**이며, gate를 유지하지 않고 다음 적합한 세션으로
재예약한다.

1. 최소 금액 1주의 비시장성 auto proposal을 한 개만 제출한다. Toss evidence로 첫 부분 체결을
   확인한 뒤 `toss_reconcile_orders(order_id=<원 주문 ID>, dry_run=False)`로 수렴시킨다.
   해당 Toss account는 `toss_auto_submission_freeze` 상태가 되어야 하며, 새 자동 entry는
   ordinary human-approval card로 fallback해야 한다.

2. 현재 원 주문의 `remaining_quantity`를 fresh broker snapshot으로 읽어 `action="cancel"`
   proposal을 생성한다. snapshot의 원 주문 ID·종목·side·limit price·remaining quantity가
   proposal rung과 정확히 일치해야 한다. Telegram 승인 카드는 여기서 **누르지 않고 대기**한다.

3. 승인 대기 중 원 주문의 **2차 체결**을 Toss broker evidence에서 관찰한다. 누적 체결 및
   remaining quantity를 reconcile로 수렴시킨다. 이 단계가 없으면 B는 합격할 수 없다.

4. 오래된 cancel card를 승인한다. stale `remaining_quantity` snapshot 때문에 cancellation
   mutation 전에 거절되어야 한다. 원 주문을 취소한 것으로 기록하거나 자동 재시도하지 않는다.

5. 2차 체결 후 fresh snapshot으로 새 cancel proposal을 만들고 사람이 승인한다. 원 주문의
   terminal `CANCELLED` evidence를 얻은 뒤 다음 targeted reconcile을 실행한다.

   ```text
   toss_reconcile_orders(order_id=<원 주문 ID>, dry_run=False)
   ```

   proposal cancel rung과 original Toss ledger가 broker-terminal evidence로 닫혀야 B가
   합격이다. 원 주문이 `FILLED`가 되면 취소 성공으로 바꾸지 말고 실제 체결 수량으로
   수렴시켜 실패/재평가로 보고한다.

### 같은 세션 동결 해제 조건 (감사 기준)

`toss_auto_submission_freeze`가 사라져 같은 날 두 번째 자동주문이 가능해지는 것은 아래
연언이 모두 참일 때뿐이다. 운영자는 이 조건을 **원 place order ID 기준으로** 확인한다.

1. 해당 auto proposal의 모든 rung이 `state=filled`이고, 각각의 양수
   `filled_qty == quantity`다.
2. 각 rung의 broker/client/correlation identity가 정확히 하나의 Toss `place` ledger row에
   대응하고, symbol·market·side·limit price·quantity·filled quantity가 모두 일치한다.
3. 그 place row는 `status=filled`, `broker_status=FILLED`, `reconciled_at` 존재,
   `requires_manual_review=false`, `manual_review_reason=null`,
   `last_reconcile_error=null`이며, 매수는 평균 체결가가 limit 이하/매도는 limit 이상이다.
4. 그 place row의 `broker_order_id`를 `original_order_id`로 직접 참조하는 `cancel` 또는
   `modify` ledger sibling이 있다면, 모두 terminal·reconciled이고 review/error residue가
   없어야 한다. pending/partial/anomaly, `reconciled_at=null`, manual review, 또는
   reconcile error 하나라도 있으면 동결은 유지된다.

누락·중복·불일치·조회 오류는 해제가 아니라 `frozen`이다. 이것이 충족되지 않은 상태에서
새 자동 entry가 ordinary human-approval card로 내려가는 것은 정상적인 fail-closed 동작이다.

## 캡 유도와 활성화 한계

🔴 **이 절에 캡 수치를 하드코드하지 않는다.** TOSS-AUTO-FULL(§51) 당시 이 문서는
`KR/US 캡 = 정책 밴드 상단 × 동시 신규 최대 1종목`이라는 표를 실었지만, 그 유도는
§106차(직접 설정으로 전환)와 §127차/§147차(슬롯 개념 자체가 철폐)를 거치며 무효가
됐고, 남은 표의 수치도 §133차(KR 1,000,000→2,000,000)·§145차(crypto 1,000,000→
5,000,000, 일일 5,000,000→10,000,000)로 실제 값이 계속 움직여 반복적으로 drift를
냈다(BL-7). **수치 중복 자체가 drift의 원인**이므로, 표를 갱신하는 대신 정본 참조로
대체한다 — 앞으로 정책이 또 바뀌어도 이 문서는 낡지 않는다.

현행 캡은 아래 정본에서만 읽는다. 이 문서에는 절대 그 값을 복사해 오지 않는다.

- 파일: `config/trading_policy.yaml` → `order_proposals.auto_approve.per_order_cap`
  / `order_proposals.auto_approve.daily_cap` (마켓 키: `kr`/`us`/`crypto`, 통화는
  KR·crypto는 KRW, US는 USD)
- MCP 도구: `get_trading_policy(market=<kr|us|crypto>, lane="sell")` — 응답에
  `{version, content_hash}`가 echo되므로 참조 시점을 함께 기록한다
- acceptance 실행 전 반드시 이 정본을 다시 읽어 그 세션의 실제 캡을 acceptance
  기록(§ Acceptance A/B 로그)에 값과 정책 버전으로 함께 남긴다. 이 표를 인용하지 않는다.

🔴 **§156차(2026-08-26) 캡 산정 기준 변경**: expanded 모드의 marketable
`take_profit` 매도 1종만 캡을 `max(limit_price, current_price) × quantity`
(집행가 기준)로 측정한다. resting 매도·매수·`off` 모드는 기존대로
`limit_price × quantity` 기준을 유지한다(상세: `docs/runbooks/order-proposal-auto-approve-expand.md`
"§156 marketable take_profit" 절). acceptance 세션에서 marketable 매도가
관측되면 이 기준으로 캡 소진량을 대조한다.

동시 신규 진입 수의 상한은 §147차(2026-08-24) 이후 슬롯 개수가 아니라 **주문가능
현금뿐**이다(`buy.new_entry_overflow` 룰도 같은 결정으로 삭제됨). 캡은 이와 무관하게
독립적으로 정책 파일에서 직접 설정된다.

다른 buy/sell tier를 수정하거나 cap을 운영 중에 임의 조정하지 않는다. `loss_cut`,
예상 실현손익 `<= 0`, ±1% 본전 경계, `policy_deviation`, 분류 불가 항목은 계속 human
approval 전용이다. `table_disagreement`는 감사/기록 표면에 보존되지만 이 분류의
차단 사유는 아니다.

## 원복

1. 자동 신규 제출을 먼저 끈다.

   ```text
   ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED=false
   ORDER_PROPOSALS_AUTO_APPROVE=false
   ```

2. 아직 open인 원 주문은 기존 Toss 운영 절차로 broker terminal evidence를 확보한다. cancel
   요청 성공만으로 local 상태를 `cancelled`로 쓰지 않는다.

3. `toss_reconcile_orders(order_id=<원 주문 ID>, dry_run=False)`를 반복해 terminal ledger와
   proposal rung을 수렴시킨다. evidence를 못 얻으면 local 상태를 열린/불확실로 유지하고
   운영자 수동 검토로 넘긴다.

4. acceptance artifacts와 실패 원문을 보존한다. Toss gate는 A와 B 모두 재검증될 때까지
   기본 OFF로 둔다.
