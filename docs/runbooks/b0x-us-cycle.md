# B0-X US 사이클 런북 — `alpaca_paper_lab`

> `B0_UNVALIDATED` · `SELL_SIDE_MODEL_MISMATCH` ·
> `CROSS_MARKET_TRANSFER_UNVALIDATED`

계약 정본은 `~/work/herdr-inbox/b0x-experiment-contract-v1-20260808.md`다.
이 레인은 전체 파일 digest가 아니라 **버전 `v1.6` + 아래 인용 절**에 결속한다.

- §1: US 이식은 `CROSS_MARKET_TRANSFER_UNVALIDATED`를 추가한다.
- §2-2 v1.1: 표가 없거나 STALE이거나 36시간을 넘으면 주문 0이다.
- §4 US: 신규 `$150~450`, 종목 총 신규×5, 동시 ≤10, 일 신규 ≤3,
  일 손실 `−2.5% NAV`, US RTH.
- §8 v1.5 ①: envelope 입력은 같은 사이클의 broker truth다.
- §8 v1.6: `kis_mock`의 원장 미체결 예외만 허용한다. US에는 적용하지 않는다.

## 0. 계좌맵 게이트

기계판독 정본은 `~/services/auto_trader-operator/operator_contract.yaml`이다.
매 사이클/운영 kickoff 전에 아래를 다시 대조한다. `mock/CLAUDE.md` §1은 참조
표면일 뿐이고, 충돌하면 YAML의 더 보수적인 해석으로 멈춘다.

```bash
cd ~/services/auto_trader-operator
git fetch --prune origin
grep -n 'b0x-adapter-orders-20260808' operator_contract.yaml
```

확인할 정확한 사실은 다음과 같다.

| YAML 사실 | 기대값 |
|---|---|
| `account_lanes.alpaca_paper_lab` | `B0-X-US` |
| `strategy_order_exceptions.b0x-adapter-orders-20260808.surfaces` | `alpaca_paper_lab` 포함 |
| 같은 예외의 `writer` | `b0x_adapter_single` |

`alpaca_paper_lab`과 기본 운영 계좌 `alpaca_paper`는 별개다. 이 어댑터는
`account_mode="alpaca_paper_lab"`을 모든 read에 명시하고, 기본 계좌로 fallback하지
않는다. 계좌맵 충돌은 `NEEDS_UPSTREAM(account_map_conflict)`이며 주문 0이다.

같은 YAML의 과거 `alpaca_account_cleanup_20260805`에는 lab suffix `a9e6cd`의
`UBER qty=1`이 1회성 legacy cleanup으로 남아 있다. `reuse_after_execution` 및 scope
확장이 금지돼 있으므로 이 사실을 B0-X 소유권·현재 잔고·새 cleanup 권한으로 해석하지
않는다. 실제 잔여 여부와 귀속은 반드시 §2의 fresh truth로 다시 확인한다.

## 1. 실행과 세션

```bash
# 수동·scheduleless 관측/계획 경로. --confirm 옵션은 없다.
uv run python -m scripts.run_b0x_us_cycle
```

`app.mcp_server.tooling.market_session.us_market_session`의 `regular` 결과만 US
RTH로 인정한다. 장외이면 표·계좌·원장 read 전에
`outside_us_regular_session`으로 끝난다.

US 표에는 `quote_currency="USD"` 및 `new_entry_notional_usd`가 반드시 있어야 하며,
선택값은 signed `$150~450` 안이어야 한다. 누락·통화 불일치·범위 이탈은 계좌 read 전에
`invalid_us_table_sizing`으로 주문 0이다. 공통 파생기의 legacy fallback을 US 표 누락의
대체값으로 사용하지 않는다.

이 CLI는 `confirm=False`만 전달한다. 따라서 preview, submit, cancel broker 호출은
0건이다. 새 TaskIQ/cron/launchd 등록은 이 런북의 범위가 아니다.

산출물은 기존 `scripts.b0x.ledger`의 append-only 관측 기록뿐이다. 레인 상태 파일은
새로 만들지 않으며, `attributed_book.json`을 읽거나 쓰지 않는다.

## 2. fresh truth와 잔여 귀속

표가 유효한 뒤에만 다음 기존 read-only `alpaca_paper_*` 표면을 **lab 계좌로만** 같은
사이클에 읽는다.

1. 계좌 cash/NAV
2. 계좌 전체 positions
3. 계좌 전체 open orders (`status=open`)
4. 기존 Alpaca ledger recent rows

open-order 또는 bounded ledger 응답이 limit에 닿으면 완전성을 증명할 수 없으므로
`lab_fresh_truth_unavailable`로 fail-closed 한다. lab 자격증명이 없거나 profile/응답의
`account_mode`가 다르더라도 빈 계좌로 해석하지 않는다.

귀속은 추정하지 않는다.

- `lifecycle_correlation_id`가 `b0xu-`로 시작하는 lab execution evidence가 broker
  order id와 정확히 하나로 연결될 때만 open order를 자기 것으로 본다.
- position은 symbol별 b0xu fill의 순수량이 broker 수량과 정확히 일치할 때만 자기
  position이다. fill이 없거나 수량 evidence가 불완전하면 foreign/linkage failure다.
  매수가 fill price만 불완전한 경우에는 소유권을 꾸며 바꾸지 않고 cumulative deployment를
  unreadable로 표시하여 추가매수를 막는다.
- 그 밖의 open order/position은 foreign으로 기록하고 `CONTAMINATED`로 분리한다.
  관측은 남기되 submit은 막으며, foreign 주문/포지션을 취소·청산하지 않는다.

## 3. envelope와 broker truth

`scripts.b0x.envelope.US_ALPACA_PAPER_LAB_ENVELOPE`는 변경 불가 상수다.

| 항목 | 값 |
|---|---:|
| 종목당 신규 | `$150~450` (표의 선택점 `$300`) |
| 종목당 총투입 | `$2,250` (`$450×5`) |
| 동시 포지션 | `≤10` |
| 일일 신규 진입 | `≤3` |
| kill | 일 실현손실 `≤ −2.5% × fresh NAV` |

NAV가 없거나 0 이하이면 `MissingNavForRatioKill`과 동등하게 주문 0이다. kill의 수치와
실현손익은 모두 USD 절대값으로 비교하며, 산출물에는 원 비율·fresh NAV·계산된 USD 임계값을
함께 기록한다.

§8 v1.5 ①의 세 입력은 모두 현재 broker 응답에서 온다.

| 계약 정의 | US 구현 |
|---|---|
| 동시 포지션 | full positions 중 `qty_available > 0`인 non-dust sellable symbol 수 |
| 동일 심볼 재제출 | broker open orders 중 정확히 귀속된 `b0xu-` pending symbol은 side와 무관하게 차단 |
| 일일 신규 | 자기 pending symbol ∪ account-wide sellable position symbol ∪ 이 사이클 새 symbol의 distinct 수 |

Alpaca는 open-orders 조회 표면을 제공한다. 따라서 US는 `PendingUnreadable`이나
`kis_mock_order_ledger` 예외로 빠지지 않으며, 그 응답을 빈 것으로 추정하지도 않는다.

## 4. 제출 경계

이 변경의 기본 production mutation seam은 **의도적으로 미배선**이다.

- `submit_planned_order`/`cancel_own_open_orders`에 기본 broker callback은 없다.
  승인되지 않은 호출은 `LabMutationNotWired`로 실패한다.
- 테스트는 fake/stub callback을 명시 주입한다. 실제 broker preview/POST/DELETE가 아니다.
- 향후 승인된 lab 전용 boundary가 연결되더라도 `B0X_US_ENABLED=true`와 호출별
  `confirm=True`가 둘 다 필요하다. 그 boundary는 lab 자격증명, trusted quote evidence,
  idempotency 및 broker read-back도 독립적으로 검증해야 한다.

기존 automated Alpaca token 경로는 기본 `alpaca_paper`용 서버 보관 preview protocol이다.
이를 adapter-local shortcut으로 `alpaca_paper_lab`에 재사용하지 않는다. 이 CLI에는
`--confirm`도 없으므로 이번 라운드의 첫 US RTH 관측은 계획/기록만 수행한다.

## 5. 라벨과 범위

헤더에는 inherited table labels와 US 추가 라벨을 포함한다. 특히 아래 세 문구는 유지한다.

- `B0_UNVALIDATED`
- `SELL_SIDE_MODEL_MISMATCH`
- `CROSS_MARKET_TRANSFER_UNVALIDATED`

`SHARED_ACCOUNT_HISTORY`는 Binance sidecar 전용이므로 US `SHARED_HISTORY_ACCOUNTS`에
추가하지 않는다. writer caveat은 추정 문구가 아니라 §0에서 대조한
`account_lanes.alpaca_paper_lab=B0-X-US` 및 예외의 writer/surface 사실을 그대로 쓴다.

이 패키지는 `scripts.b0x` 공통 envelope, kill switch, broker truth, derivation,
table gate, writer lock, observation ledger를 재사용한다. crypto/KR 실행 경로, default
`alpaca_paper`, live broker, scheduler는 변경하거나 호출하지 않는다.
