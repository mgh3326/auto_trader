# ROB-858 — Toss `loss_cut` 지원 결정과 `approval_issue_id` 계약

> **ROB-864로 대체됨 (2026-07-13):** 이 문서의 Paperclip
> `approval_issue_id=status=done` 승인 계약은 폐기되었다. 현재 계약은
> `approval_issue_id` optional audit note + Telegram 주문별 2단계 확인이며,
> 직접 loss-cut/defensive-trim 경로는 proposal 생성 안내와 함께 fail-close한다.
> Toss reconcile 및 broker-evidence projection 결정은 그대로 유효하다.

- 결정일: 2026-07-13 KST
- 조사 기준: canonical `~/work/auto_trader` `main` at `9ff505575bf0b1025f489037a5ee16fdf62128be`
- 범위: 정적 코드·git history·PR 본문·read-only retrospective 조회. 브로커 주문 및 DB mutation 없음.
- 결정: **GO.** Toss websocket을 전제하지 않고, Toss의 기존 order-detail polling reconcile에 proposal-rung projection과 `loss_cut` 실행 가드를 완결한 뒤 `toss_live × equity_kr|equity_us`를 명시적으로 연다.

## 1. 결론

`toss_live` 제외는 #1500에서 의도적으로 고정된 현재 계약이다. 테스트 `test_toss_live_loss_cut_remains_unsupported`와 runbook은 Toss 일반 proposal은 지원하지만 Toss `loss_cut`은 “this slice”에서 제외한다고 명시한다. 그러나 제외의 확인 가능한 직접 근거는 “websocket이 없어서 원리적으로 불가능”이 아니라 다음 두 구현 공백이다.

1. Toss proposal adapter가 `exit_intent`, `retrospective_id`, `approval_issue_id`를 Toss preview/submit에 전달하지 않으며, Toss submit은 손실 매도를 무조건 `avg_purchase_price * 1.01` floor로 막는다.
2. `toss_reconcile_orders`는 체결·journal·realized PnL·retrospective-due 원장을 갱신하지만 `OrderProposalsService.record_fill_evidence`를 호출하지 않아 proposal rung은 `resting`/`unverified`에 남는다.

두 공백 모두 기존 단건 `GET /orders/{orderId}` 증거와 polling kernel 안에서 닫을 수 있다. Websocket은 지연을 줄이는 수단이지 정확성의 필수 조건이 아니다. 단, polling이 default-off이므로 운영 cadence 또는 체결 후 명시적 reconcile은 지원 계약의 일부여야 한다.

## 2. 왜 현재 제외되어 있는가

### 확인된 근거

- ROB-800 PR #1488은 `loss_cut`을 shared `_place_order_impl`을 통과하는 crypto/KR/US live sell로 설계했다. 전제 조건은 sell+limit+live, 72시간 이내 retrospective, Paperclip `done` issue, authenticated caller, live-send approval hash다. 당시 Toss native path는 이 shared implementation 밖이었다. [PR #1488](https://github.com/mgh3326/auto_trader/pull/1488), `docs/plans/2026-07-10-ROB-800-loss-cut-exit-intent-pr1.md`.
- ROB-816 PR-3 design은 proposal-capable live lane 전부의 fill projection을 요구하며 Toss도 명시했다: KIS KR, generic US/Upbit, Toss KR/US (`docs/superpowers/specs/2026-07-11-rob-816-pr3-design.md:117`).
- 하지만 PR #1498의 실제 scope는 ROB-407 generic live ledger, 즉 US KIS/Upbit뿐이다. PR 본문은 KR/KIS도 unchanged follow-up이라고 명시하며 Toss kernel은 수정하지 않았다. [PR #1498](https://github.com/mgh3326/auto_trader/pull/1498).
- PR #1500은 Toss-native 일반 proposal routing을 추가하면서 Toss `loss_cut`을 fail-closed로 남겼다. PR 본문 test coverage에 “Toss loss-cut fail-closed rejection”이 있고, 서비스 테스트가 그 제외를 고정한다 (`tests/services/order_proposals/test_service.py:438`). Runbook도 “Toss is not a supported loss-cut binding in this slice”라고 명시한다 (`docs/runbooks/order-proposals.md:116`). [PR #1500](https://github.com/mgh3326/auto_trader/pull/1500), merge `d36af171`.

### websocket 추정의 판정

“Toss 체결 websocket 부재 때문에 제외”라는 운영 설명은 방향상 맞지만, PR/plan에서 확인되는 공식 불변식은 아니다. 코드상 더 정확한 설명은 다음과 같다.

- Toss send는 accepted-only이고 fill은 `toss_reconcile_orders`의 broker order-detail evidence로만 기록한다 (`app/models/review.py:527`, `app/tasks/toss_live_reconcile_tasks.py:1`).
- 자동 reconcile task와 fill poller는 모두 default-off다 (`app/core/config.py:696`, `app/tasks/toss_live_reconcile_tasks.py:37`, `:82`). 따라서 websocket이 없는 상태에서 polling도 켜지 않으면 자동 수렴이 없다.
- 그럼에도 수동 또는 scheduled non-dry-run polling을 실행하면 broker-confirmed fill을 안전하게 book할 수 있으므로 websocket 부재는 NO-GO 사유가 아니다. 지원 조건은 “polling/reconcile cadence가 실제로 존재할 것”이다.

## 3. websocket 없이 fill과 회고가 닫히는가

### 이미 닫힌 Toss 경로

`toss_reconcile_orders_impl`은 open/reopened ledger row를 단건 또는 batch order evidence로 확인한다 (`app/mcp_server/tooling/toss_live_ledger.py:598`). `_reconcile_one_toss_row`은 다음을 수행한다.

1. `pending`은 no-op, `none`은 cancelled/rejected 계열 상태를 기록한다 (`toss_live_ledger.py:274`, `:307`).
2. cumulative `filled_qty`와 기존 booked qty의 delta만 계산해 중복 부기를 막는다 (`:322`).
3. confirmed delta에 대해서만 fill, sell journal close, execution ledger, Toss ledger를 기록한다 (`:356`, `:404`, `:419`).
4. terminal Toss place row는 `build_retrospective_pending`의 due scan 대상이다. `filled|rejected|anomaly`는 기본 due, cancel-family는 opt-in due다 (`app/services/trade_journal/trade_retrospective_service.py:830`, `:1048`).

따라서 broker fill → local fill/journal/realized PnL → retrospective due 흐름은 polling만으로 성립한다.

### 아직 닫히지 않은 proposal 경로

Toss reconcile에는 `record_fill_evidence` 호출이 없다. 반면 generic live kernel은 broker evidence 직후 proposal projection을 별도 committed session에서 best-effort로 호출한다 (`app/mcp_server/tooling/live_order_ledger.py:240`, `:283`). 결과적으로 Toss proposal은 broker ledger와 회고 due가 terminal이어도 proposal rung/group이 terminal로 수렴하지 않는다.

이 갭은 websocket 문제가 아니라 빠진 projection call 문제다. 구현 시 Toss evidence 분류 직후, 후속 journal/ledger 부기보다 먼저 다음을 호출한다.

- `partial` → `terminal_state="partially_filled"`, cumulative `filled_qty`
- `filled` → `terminal_state="filled"`, cumulative `filled_qty`
- broker-confirmed cancel/none → `terminal_state="cancelled"`, `filled_qty=None` (기존 partial 보존)
- `pending`, transient, unknown/anomaly → terminal inference 금지
- `dry_run=True` → proposal write 금지

주의: PR-3c generic kernel의 best-effort projection은 full-fill/cancel 후 row가 open scan에서 빠져 재시도되지 않는다고 #1498 review가 명시한다. Toss에 복사할 때도 같은 영구 drift 위험이 있다. 최소 구현에서는 ERROR+Sentry만 복제하지 말고, terminal ledger와 non-terminal proposal rung을 재투영하는 idempotent sweep 또는 retryable projection marker를 포함해야 한다.

## 4. `approval_issue_id` 역설계 계약

### 무엇을 가리키는가

`approval_issue_id`는 auto_trader 내부 approval row나 Telegram callback ID가 아니다. `PAPERCLIP_API_URL/api/issues/{approval_issue_id}`에서 조회되어 JSON `status`가 정확히 소문자 `"done"`인지 확인되는 **Paperclip issue key**다 (`app/mcp_server/tooling/order_validation.py:446`). 저장 예와 tool docs가 `ROB-800` 형식을 사용하므로 Linear-style issue key를 쓰지만, “Linear에 존재/Done”만으로는 코드 계약을 증명하지 못한다. 실제 실행 환경의 Paperclip endpoint가 그 key를 `done`으로 반환해야 한다.

의미상으로는 해당 주문 1건의 손절을 승인하는 per-ticket issue여야 한다. 그러나 현재 verifier는 issue title/body, symbol, side, quantity, limit, retrospective ID와의 일치를 검사하지 않는다. 즉 기술적으로는 무관한 기존 `done` issue도 통과할 수 있다. **운영자는 umbrella 구현 이슈(예: ROB-800)를 재사용하지 말고 손절 주문별 전용 승인 issue를 발급해야 한다.** 이 의미적 binding 부재는 구현 리스크이자 후속 hardening 항목이다.

### 형식·존재·상태 검증

| 단계 | 검증 |
|---|---|
| `order_proposal_create` | `loss_cut`이면 non-empty만 확인. regex, 존재, 상태는 확인하지 않음 (`service.py:347`). |
| Telegram click preview | shared ROB-800 validator가 `^[A-Z]+-\d+$`, Paperclip HTTP 200/JSON, `status == "done"`, caller allowlist, retro age/symbol/trigger를 확인. |
| submit | 같은 필드를 그대로 전달해 다시 검증. loss-cut은 global hash mode와 무관하게 valid approval hash 필수. |
| 장애 | Paperclip URL/key 미설정, timeout, non-200, malformed JSON은 모두 not-done으로 fail-closed. 성공 상태는 process-local 60초 cache. |

현재 regex는 대문자 ASCII team key + hyphen + 숫자만 허용한다. 공백, URL, UUID, 소문자 key는 거부된다.

### 어디에 저장되는가

- proposal 경로: `review.order_proposals.approval_issue_id`에 저장되고 proposal immutable payload hash에도 포함된다 (`app/models/order_proposals.py:101`, `app/services/order_proposals/payload.py:38`). get/list 응답에도 노출된다.
- Telegram 승인 문구에는 의도적으로 표시하지 않는다. `exit_reason`과 retro ID만 표시한다 (`docs/runbooks/order-proposals.md:618`).
- shared direct `place_order(loss_cut)` live ledger에는 `exit_intent`는 저장되지만 loss-cut `approval_issue_id` 자체는 저장되지 않는다. 기존 `dt_approval_issue_id`는 `defensive_trim` 전용이다. Toss ledger에는 현재 `exit_intent`, `retrospective_id`, `approval_issue_id` 컬럼이 모두 없다. proposal row가 현재 loss-cut approval audit의 유일한 durable home이다.

### 재현 가능한 사용법

1. 손절 의사결정을 반영한 **새 retrospective row**를 만든다. symbol 일치, `trigger_type`은 `stop_loss|thesis_change`, 생성 시각은 실행까지 72시간 이내여야 한다.
2. 손절 주문 1건 전용 Paperclip-backed issue를 만든다. 제목/본문에 account, symbol, side=sell, limit/qty, retro ID, expiry를 적고 사람의 결정을 받은 뒤 상태를 `done`으로 바꾼다.
3. 아래 read-only check가 성공하는지 실행 환경에서 확인한다. 토큰 값은 출력하지 않는다.

   ```bash
   APPROVAL_ISSUE_ID=ROB-<number>
   curl -fsS \
     -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
     "$PAPERCLIP_API_URL/api/issues/$APPROVAL_ISSUE_ID" \
     | jq -e '.status == "done"'
   ```

4. 현재 지원 lane에서 proposal을 만든다. 예시의 `approval_issue_id`는 2단계의 전용 issue key다.

   ```json
   {
     "account_mode": "kis_live",
     "market": "equity_kr",
     "symbol": "087010",
     "side": "sell",
     "order_type": "limit",
     "proposer": "operator",
     "exit_intent": "loss_cut",
     "exit_reason": "thesis_change",
     "retrospective_id": 123,
     "approval_issue_id": "ROB-999",
     "rungs": [{"rung_index": 0, "side": "sell", "quantity": "1", "limit_price": "111900"}]
   }
   ```

5. proposal 생성 성공은 주문 승인이 아니다. Telegram click 때 Paperclip `done`, caller, retro freshness, holdings/price guard가 재검증되고 숫자가 달라지면 `needs_reconfirm`이 된다.

Toss는 이 문서의 구현이 배포되기 전까지 4단계에 사용할 수 없다. 현재 대안은 Toss 앱 수동 손절 후 exact fill을 sync/reconcile하고 회고를 갱신하는 경로다.

## 5. 72시간 age guard와 펩트론 retro #22

Read-only 조회 결과 retro #22는 다음 타임라인이다.

| 사건 | KST |
|---|---|
| retro #22 `created_at` | 2026-07-10 09:52:52 |
| 72h 만료 | 2026-07-13 09:52:52 |
| ROB-858 생성 | 2026-07-13 10:42:01 |
| 이 조사 시각 | 2026-07-13 11:10경 |

즉 ROB-858의 펩트론 재시도 시점에는 retro #22가 이미 약 49분 stale이었고 조사 시점 age는 약 73.3시간이다. Toss 지원 여부와 무관하게 같은 retro ID로 새 loss-cut proposal을 만들면 `stale (> 72h old)`로 거부된다.

이것은 “회고 먼저 쓰고 며칠 뒤 결행” 운용과 실제로 충돌한다. 다만 72시간은 오래된 결정을 재사용하지 못하게 하는 loss-cut freshness gate이므로 제거하거나 `updated_at` 기준으로 완화하지 않는다. `save_trade_retrospective`의 동일 correlation upsert는 기존 row를 갱신할 뿐 `created_at`을 새로 만들지 않으므로 #22 업데이트도 age를 되살리지 못한다 (`trade_retrospective_service.py:213`). 실행 결정을 다시 내린 시점에 새 correlation ID로 새 pre-trade retrospective를 만들고 그 새 ID를 proposal에 bind한다.

## 6. GO 최소 구현 플랜

### A. Toss loss-cut execution contract

1. `app/services/order_proposals/service.py`
   - `_validate_exit_binding` allowlist에 `toss_live × equity_kr|equity_us` 추가.
   - 현재 unsupported regression을 supported positive/negative matrix로 교체.
2. `app/services/order_proposals/revalidation.py`
   - Toss adapter가 preview와 submit 양쪽에 `exit_intent`, `retrospective_id`, `approval_issue_id`를 전달하도록 한다.
3. `app/mcp_server/tooling/orders_toss_variants.py`
   - Toss preview/place public/internal contract에 ROB-800 fields를 추가한다.
   - shared `_validate_loss_cut_preconditions`를 재사용해 Paperclip/caller/retro를 preview와 submit에서 재검증한다.
   - valid loss-cut context에서만 avg×1.01 floor를 면제하고 current-price `loss_cut_max_slip` band를 적용한다. 일반 Toss sell guard는 변경하지 않는다.
   - loss-cut live send는 Toss approval hash mode와 무관하게 supplied token을 검증한다.
4. `app/models/review.py` + additive Alembic migration + Toss ledger service
   - Toss accepted-only ledger에 `exit_intent`, `retrospective_id`, `approval_issue_id`를 저장해 direct/proposal 모두 audit 가능하게 한다.

### B. Reconcile projection

1. `app/mcp_server/tooling/toss_live_ledger.py`
   - evidence classification 직후 별도 session으로 `record_fill_evidence` 호출.
   - partial/fill/cancel, dry-run, duplicate evidence, cancel-after-partial 보존을 generic kernel과 동일하게 구현.
   - projection 실패가 fill booking을 rollback하지 않게 하되 재시도 가능한 drift repair를 제공.
2. `app/tasks/toss_live_reconcile_tasks.py` 및 runbook
   - 지원 SLA를 명시: fill poller enable 또는 체결 후 targeted `toss_reconcile_orders(order_id=..., dry_run=False)` 필수.

### C. 테스트

- service: Toss KR/US valid loss-cut create, wrong market/side/order type, stale/mismatch retro.
- Toss tool: Paperclip missing/not-done, caller deny, ordinary sell floor unchanged, loss-cut band accept/reject, approval hash missing/mismatch/expired, ledger audit fields.
- proposal E2E(mocked boundaries): create → Telegram approve → Toss preview → accepted submit → partial/fill/cancel reconcile → rung/group terminal.
- reconcile: dry-run read-only, duplicate cumulative evidence idempotency, projection failure then retry/sweep convergence, terminal row retrospective-due visibility.
- docs: `app/mcp_server/README.md`, `docs/runbooks/order-proposals.md`, tool descriptions에 Paperclip issue contract와 polling requirement 동기화.

### 주요 리스크와 게이트

- **승인 issue 의미 binding 부재:** 최소 구현에서 issue title/body까지 자동 검증하지 않더라도 dedicated per-ticket issue 규칙을 runbook에 강제한다. 후속으로 issue metadata에 symbol/qty/retro/payload hash를 비교하는 fail-closed verifier를 권고한다.
- **polling default-off:** cadence 없이 기능 flag만 열면 proposal과 회계가 영구 drift한다. 배포 canary 전에 fill poller/reconcile 운영 경로를 증명한다.
- **best-effort projection 유실:** terminal row가 open scan에서 빠지는 #1498의 알려진 한계를 Toss로 복제하지 않는다.
- **두 개의 72h 검사:** create 직전 fresh retro라도 승인 클릭이 늦으면 submit에서 다시 stale이 된다. proposal expiry보다 retro expiry가 먼저면 UI/메시지에 실제 유효 deadline을 보여주는 개선을 권고한다.

## 7. 배포 전까지의 대안

- Toss loss-cut: 앱에서 수동 주문, exact broker fill 확인, `toss_reconcile_orders`/수동 sync로 fill·journal·realized PnL 부기, 같은 거래 결과 회고 작성.
- canonical proposals가 반드시 필요하면 자산을 임의로 계좌 이동하거나 unrelated `done` issue를 재사용하지 않는다.
- 펩트론은 retro #22가 만료되었으므로 어느 지원 lane에서도 새 손절 proposal 전에 새 의사결정 retrospective가 필요하다.

## 8. operator `CLAUDE.md` 반영 초안 (직접 수정하지 않음)

> `approval_issue_id`는 주문별 Paperclip 승인 이슈 키(`^[A-Z]+-\d+$`)이며, Telegram click/submit 시 `$PAPERCLIP_API_URL/api/issues/<key>`가 `status="done"`을 반환해야 한다. 기존 완료 이슈를 재사용하지 말고 account/symbol/qty/limit/retro ID를 적은 전용 이슈를 발급하며, loss-cut retro는 실행 시점 기준 72h 이내의 새 row여야 한다(동일 correlation 업데이트는 `created_at`을 갱신하지 않음).
