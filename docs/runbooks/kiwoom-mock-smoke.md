# Kiwoom Mock Order Smoke

> **MCP_PROFILE (ROB-488)**: `kiwoom_mock_*` 도구는 더 이상 default MCP surface에
> 등록되지 않는다. MCP 도구로 smoke를 수행하려면 `MCP_PROFILE=kiwoom`으로 서버를
> 띄운 세션에서 호출해야 한다 (CLI smoke 스크립트는 프로파일과 무관).

ROB-319. Operator-safe smoke for the Kiwoom **mock-investment** (모의투자) order
lifecycle: submit → order history → modify (if supported) → cancel → reconcile.

Builds on the ROB-97 foundation (PR #667) and the confirmed `place_order` work
(PR #830). ROB-319 wired the account-read MCP tools to real broker calls and
implemented confirmed `modify`/`cancel`.

## Safety boundaries

This smoke performs **real Kiwoom mock broker mutation**, allowed only inside
these boundaries:

- **Mock host only.** `KiwoomMockClient` rejects any base URL other than
  `https://mockapi.kiwoom.com` and re-checks the resolved host before sending
  (transport-layer fail-closed). The live host (`api.kiwoom.com`) is a defensive
  constant that no code path can select.
- **KRX only.** `NXT`/`SOR` and any non-`KRX` exchange are rejected before any
  network call.
- **US 미지원 (KRX 전용).** kiwoom_mock은 KRX 국내주식 전용이며 US/해외 주문은
  지원하지 않는다(`_exchange_error`가 non-KRX를 네트워크 호출 전 거부). US는 별도
  product decision(미활성).
- **ROB-418 — account-read 필수 파라미터:** kt00018(잔고)는 `qry_tp`, kt00009(미체결/
  이력)는 `stk_bond_tp`를 요구한다(누락 시 `return_code 2` 필수입력 파라미터 오류).
  기본값(`qry_tp="1"`, `stk_bond_tp="0"`)은 Kiwoom enum 관례이며 **이 mock smoke로
  값의 scope 정확성을 확정**한다. ROB-399와 동일 버그(이 fix로 covered).
- **ROB-460 — account-cash reads의 `dmst_stex_tp`:** 2026-06-09 live에서
  `kiwoom_mock_get_positions`/`get_orderable_cash`가 `return_code 2`
  (필수입력 파라미터=`dmst_stex_tp`, 국내거래소구분)로 재실패했다. account-cash
  reads **kt00018(잔고) + kt00010(주문가능, with-symbol)** 의 요청 본문에
  `dmst_stex_tp="KRX"`를 채운다. 이 값은 order 엔드포인트(kt10000-kt10003)에서 이미
  검증된 값(추측 아님)이며 mock은 KRX 전용이다. **경계 결정:** order-history reads
  **kt00009/kt00007**는 의도적으로 미변경 — 이미 ROB-418로 복구됐고 `dmst_stex_tp`
  필요가 입증되지 않았다(작동 중인 엔드포인트에 추측 파라미터를 더해 회귀시키지 않음).
  아래 smoke 체크리스트로 4개 read 도구를 한 번에 검증하여 잔여 누락을 선제 포착한다.
- **`dry_run=False` requires `confirm=True`** on every order-mutating tool.
- **No live anything.** No KIS live, Kiwoom live, Alpaca live, or real-money
  calls. No scheduler / recurring automation.
- **No secrets printed.** The CLI reports only the **names** of missing env keys,
  never their values. Broker responses are mock-only and contain no credentials.
- **Cancel-before-submit.** `full` mode only submits a real order because cancel
  is wired; it always attempts to cancel any order it opened (finally-block) and
  reconciles. If cancel ever regresses, stop after dry-run.

## Required env (mock only)

| Env key | Purpose |
|---|---|
| `KIWOOM_MOCK_ENABLED=true` | Master gate (default `false`) |
| `KIWOOM_MOCK_APP_KEY` | Mock app key |
| `KIWOOM_MOCK_APP_SECRET` | Mock app secret |
| `KIWOOM_MOCK_ACCOUNT_NO` | Mock account number |

`TESTNET`/live env vars do nothing here. Without these four keys the smoke
fails closed.

## CLI

```bash
# 1. Config presence (names only, no values)
uv run python -m scripts.kiwoom_mock_smoke --mode preflight

# 2. Dry-run preview (no broker mutation; price floored to KRX tick)
uv run python -m scripts.kiwoom_mock_smoke --mode preview \
    --symbol 005930 --price 50000 --quantity 1

# 3. Full real mock lifecycle (requires --confirm)
uv run python -m scripts.kiwoom_mock_smoke --mode full \
    --symbol 005930 --price 50000 --quantity 1 \
    --new-price 49900 --new-quantity 1 --confirm
```

Exit codes:
- `0` — smoke OK (or stopped cleanly after dry-run when `--confirm` omitted)
- `2` — anomaly: an order was/may have been opened and could not be confirmed
  cancelled, or its id could not be parsed. **Manual cleanup required** — see the
  emitted `cleanup_required` / `anomaly` step and the reconciliation output.

`full` mode without `--confirm` stops after the dry-run and emits a `stop` step.

## Choosing a non-marketable price

Scope is **not** widened to add a Kiwoom quote/chart endpoint (the chart client
stays deferred). To pick a conservative buy limit well below market that will
**remain pending** long enough to modify/cancel:

1. Reference an existing auto_trader KIS quote/orderbook out of band (KIS remains
   the KR market-data source).
2. Pick a price safely below the current bid but **inside the KRX daily price
   band (±30%)** and **tick-aligned**. The CLI floors `--price` to the KRX tick
   via `app/mcp_server/tick_size.py::get_tick_size_kr`, but a price outside the
   daily band will still be rejected by the broker (a safe failure — re-pick).
3. Pass it as `--price` (operator-approved override).

## Smoke sequence (`full` mode)

1. Preflight — config presence (names only).
2. Price tick-alignment — `--price` floored to KRX tick.
3. `kiwoom_mock_preview_order`.
4. `kiwoom_mock_place_order(dry_run=True)`.
5. `kiwoom_mock_place_order(dry_run=False, confirm=True)` → capture `ord_no`.
6. `kiwoom_mock_get_order_history` confirms the pending/accepted order.
7. `kiwoom_mock_modify_order(dry_run=False, confirm=True)` if `--new-price` and
   `--new-quantity` are supplied (a modify may reissue the order number — the CLI
   tracks the new id for cancel).
8. `kiwoom_mock_cancel_order(dry_run=False, confirm=True)` — in a finally-block.
9. Final `kiwoom_mock_get_order_history` + `kiwoom_mock_get_positions`
   reconciliation.

## Account-read 파라미터 검증 (ROB-418 / ROB-460 / ROB-891)

각 fix 후, 4개 read 도구를 **한 번에** 호출해 `return_code 2`
(필수입력 파라미터 누락)가 없는지 전수 확인한다 — 부분 수정으로 인한 재실패를 막는다.

> **ROB-891 교정:** no-symbol `get_orderable_cash`는 kt00018(잔고)이 아니라
> **kt00001(예수금상세현황)** 을 사용한다. kt00018의 `prsm_dpst_aset_amt`는
> 추정예탁자산으로 보유증권 평가액을 포함하므로 주문가능현금의 근거가 될 수 없다.

| 도구 | broker API | 필수 파라미터 | 기대 |
|---|---|---|---|
| `kiwoom_mock_get_positions` | kt00018 | `qry_tp`, `dmst_stex_tp` | `return_code 0` |
| `kiwoom_mock_get_orderable_cash` (no symbol) | **kt00001** | `dmst_stex_tp` | `return_code 0` |
| `kiwoom_mock_get_orderable_cash` (with symbol) | kt00010 | `dmst_stex_tp`, `stk_cd` | `return_code 0` |
| `kiwoom_mock_get_order_history` | kt00009 | `stk_bond_tp` | `return_code 0` |

### TR 역할 구분

| TR | 역할 | 응답 필드 |
|---|---|---|
| kt00018 | 잔고/포지션 (sellable 포함) | `acnt_evlt_remn_indv_tot` |
| kt00001 | no-symbol 예수금/주문가능현금 | `ord_alow_amt` |
| kt00010 | symbol/side/price 주문가능금액 | `ord_alowa` |
| kt00009 | 주문 이력 | `acnt_ord_cntr_prst_array` |

- 어떤 도구든 `필수입력 파라미터=<name>`(`return_code 2`)가 나오면 그 `<name>`을
  기록하고 해당 broker API 본문에 추가하는 follow-up을 연다(추측 금지, 증명된 누락만).
- 특히 kt00009/kt00007은 ROB-460에서 의도적으로 `dmst_stex_tp` 미추가 — 이 smoke가
  실제로 그 파라미터를 요구하는지 확정한다.

## Phase A — Contract sweep (read-only, ROB-898)

`--mode contract`는 4개 account-read TR을 **read-only**로 순차 호출하여 배포 후
계약 무결성을 검증한다. Phase B(주문 mutation)와 완전히 분리된다.

### CLI

```bash
uv run python -m scripts.kiwoom_mock_smoke --mode contract
```

### Exit codes (contract mode)

| Code | 의미 |
|---|---|
| `0` | 모든 TR `return_code 0`, api_id 일치, provenance mock 확인 |
| `2` | 하나 이상 단계 실패 (non-zero RC, transport error, malformed) |
| `4` | config 누락 (KIWOOM_MOCK_ENABLED 등) |

### 출력 형식

각 단계는 JSON 한 줄로 출력된다:

```json
{
  "step": "contract_step",
  "stage": "positions",
  "tool": "kiwoom_mock_get_positions",
  "expected_api_id": "kt00018",
  "actual_api_id": "kt00018",
  "api_id_match": true,
  "kst_time": "2026-07-15T21:30:00+09:00",
  "deploy_sha": "14005d3",
  "return_code": 0,
  "return_msg": "정상",
  "evidence_kind": "positions",
  "provenance": {
    "broker": "kiwoom",
    "environment": "mock",
    "account_mode": "kiwoom_mock",
    "host": "mockapi.kiwoom.com"
  },
  "success": true,
  "pass": true
}
```

**출력 금지**: token, secret, Authorization header, account number, credential 원문,
전체 raw request/response. `return_msg`에 민감 패턴이 감지되면 `[SANITIZED]`로 치환.

### 안전장치

- **Read-only**: mutation tool(place/modify/cancel)은 guard로 교체되어 호출 시
  `SmokeRejected` 발생. `mutations_performed: 0`이 summary에 항상 포함.
- **Mutation 불가**: `--confirm` 플래그와 무관하게 contract mode는 주문을 수행하지 않는다.
- **Live host fail-closed**: `kiwoom_mock_base_url`이 `api.kiwoom.com`이면 즉시 종료(exit 2).
- **Mock 환경 증명**: `validate_kiwoom_mock_config()` + host 검증 통과해야만 sweep 시작.
- **순차 실행**: 동일 TR pacing 계약 준수, 순차 실행.
- **오류 후 재시도 금지**: 한 단계가 실패해도 다음 단계로 진행하되, 어떤 보상/재시도도 수행하지 않는다.
- **return_code=20 (capability refusal)** 은 절대 success로 변환하지 않는다.
- **Non-zero return_code**는 모두 failure로 처리한다.

### 배포 SHA 확인 절차

1. sweep 출력의 `deploy_sha` 필드를 확인한다.
2. `git rev-parse --short HEAD`로 실제 배포 SHA와 비교한다.
3. SHA가 다르면 sweep 결과가 해당 배포의 증거가 아니다 — 재배포 후 재실행.
4. ROB-891/ROB-893 PR이 merge된 커밋 이후의 SHA인지 확인한다.

### Stop conditions

sweep 실행 중 다음 중 하나라도 발생하면 해당 단계를 fail로 기록하고 다음 단계로 진행한다:

| 조건 | 처리 |
|---|---|
| `return_code=20` | fail, 절대 success로 완화하지 않음 |
| `return_code=2` (필수 파라미터 누락) | fail, 누락된 파라미터 이름을 기록 |
| transport 오류 | fail, `error_type` 필드에 예외 타입 기록 |
| malformed 응답 | fail, `fail_reason: "malformed_response"` |
| non-zero return_code | fail |

### Rollback / escalation

1. sweep summary의 `failed_stages`를 확인한다.
2. 실패한 TR과 해당 이슈(ROB-891: kt00001/kt00010, ROB-418: kt00018/kt00009)를
   연결한다.
3. 실패가 ROB-891/ROB-893 fix의 regression이면 해당 이슈를 reopen한다.
4. 새로운 파라미터 누락이면 follow-up 이슈를 생성한다 (추측 금지).
5. sweep 결과와 `deploy_sha`를 PR이나 인시던트 기록에 첨부한다.

## Phase B — Order mutation smoke (`full` mode)

Phase A contract sweep이 전부 pass한 후에만 Phase B 주문 mutation을 실행한다.
Phase B는 기존 `--mode full` 절차를 따른다 (위 "Smoke sequence" 참조).

## Cleanup / verification after smoke

- Re-run with `--mode preflight` is not enough — inspect the final
  `final_reconcile_history` / `final_reconcile_positions` output.
- If any order remains open, record its `ord_no` and cancel it (re-run cancel via
  the MCP tool or the broker UI). Do **not** report the smoke as clean while an
  order is open.
- Confirm no live endpoint was contacted (the host allowlist guarantees this; the
  CLI never accepts a non-mock host or non-KRX exchange).

## PR evidence table template

| Step / tool | symbol | dry_run / confirm | order id | broker status | cleanup |
|---|---|---|---|---|---|
| preflight | — | — | — | ok / missing keys (names) | — |
| preview | 005930 | dry | — | success | — |
| place dry | 005930 | dry | — | success | — |
| place confirmed | 005930 | confirm | `00001112…` | return_code=0 | — |
| order history | — | — | `00001112…` | pending | — |
| modify confirmed | 005930 | confirm | `0000777…` | return_code=0 / unsupported | — |
| cancel confirmed | 005930 | confirm | `0000777…` | return_code=0 | closed |
| final reconcile | — | — | — | 0 open orders | clean |

Omit all secret values. If `modify`/`cancel`/account queries return a non-zero
`return_code`, record the `return_msg` as **unsupported evidence** and a
follow-up — never fake success.

## API-contract note

The Kiwoom mock request body field names and the orderable-cash candidate keys
(`_ORDERABLE_CASH_KEYS` in `orders_kiwoom_variants.py`) are mirrored from the
Kiwoom REST docs and validated by unit tests with fakes. The real mock API is
first exercised by this smoke. If a field name is wrong, the tool degrades to an
explicit `kiwoom_mock_evidence_invalid` failure with `cash: null` and
`cash_source: "*_unavailable"` rather than faking success — capture that as a
follow-up and adjust the field mapping.
