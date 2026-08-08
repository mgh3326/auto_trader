# B0-X KR 사이클 런북 — kis_mock

> `B0_UNVALIDATED` · `SELL_SIDE_MODEL_MISMATCH` · `FIDELITY_INCONCLUSIVE_COVERAGE`

계약 정본: `~/work/herdr-inbox/b0x-experiment-contract-v1-20260808.md` (운영자 확정 2026-08-08)
계좌맵 정본: `~/services/auto_trader-operator/mock/CLAUDE.md` §1
코어 정본: `scripts/b0x/{envelope,kill_switch,state,derivation,ledger,labels,cycle}.py`
(X-C, crypto, PR #1814 이 세웠다 — 이 잡은 코어를 재구현하지 않고 승계한다. 아래 §5)

## 0. 🔴 이 PR 의 스코프 — 계좌맵 게이트 미해결 + 제출 미배선

**두 가지 이유로 이 PR 은 kis_mock 에 어떤 주문도(preview 포함) 내지 않는다.**

1. **계좌맵 게이트가 아직 열려 있다.** `mock/CLAUDE.md` §1 산문은 "B0-X 어댑터 주문"
   예외가 등록됐다고 말하지만, 그 예외를 실제로 강제하는 기계판독 파일
   `operator_contract.yaml` 의 `strategy_order_exceptions` 목록에는 이 예외가 **없다**.
   같은 파일은 `kis_mock` 을 `mutation_policy_until_canonical_envelope_and_exception_
   registration: no_new_orders` 로 명시한다. `mock/CLAUDE.md` 자신의 규칙(§4: "정확히
   등록되지 않은 mutation 은 preview 포함 실행하지 않는다")이 이 상태에서 취해야 할
   행동을 이미 정해 놓았다.
2. **주문 제출 자체가 미배선이다** (아래 §6). Binance 사이드카와 달리 KIS 는 mock/live
   를 구조적으로 분리하는 전용 클라이언트가 없다 — 어느 통합점을 쓸지는 운영자/리뷰어
   판단이 필요한 안전 설계 질문이며, 이 어댑터가 혼자 결정하지 않는다.

이 두 조건 중 하나만 풀려도 제출이 열리는 게 아니다 — **둘 다** 풀려야 한다.

## 1. 이것이 무엇이 아닌가

crypto 런북 §0 과 동일 — B0-X 는 채점·승격 도구가 아니라 관측이다.
`SELL_SIDE_MODEL_MISMATCH` 는 버그가 아니라 관측 대상이다 (매도측은 문서 B0 그대로
R1/R2 50/50 실행 — "고치는" PR 은 실험을 무효화한다).

## 2. 세션 — KRX RTH v1 (정규장만)

crypto 에는 없는 KR 고유 게이트다. `app.services.kis_mock_runner.session.
is_krx_regular_session` 을 그대로 재사용한다 (기존 kis_mock 러너가 신규 진입에 쓰는
fail-closed XKRX 캘린더 게이트 — 손으로 09:00-15:30 KST 창을 다시 짜지 않는다, 그러면
공휴일을 놓친다). NXT·시간외는 명시적으로 배제한다.

RTH 밖이면 표·계좌 I/O **전혀 없이** `zero_order_reason=outside_krx_regular_session` 으로
그 사이클이 끝난다 — 사이클 골격(§4)에서 가장 싼 게이트를 가장 먼저 두는 원칙 그대로다.

## 3. 사전 조건

1. `policy_table.v1` 최신 KR 표가 있어야 한다 (이 잡의 선행조건 — 표 생성기는 별도
   작업/운영자 kickoff, `latest-kr.json`).
   ```bash
   uv run python -m scripts.build_policy_table --market kr
   ```
   출력 위치 = `~/services/auto_trader-operator/policy-tables/latest-kr.json`.
   B0-X 는 이 디렉토리를 **읽기만** 한다.
2. `KIS_MOCK_ENABLED=true` + `KIS_MOCK_APP_KEY/APP_SECRET/ACCOUNT_NO` — read-only 계좌
   조회(잔고·보유)에 필요하다. 주문 제출용이 아니다 (§0).

## 4. 실행

```bash
# Preview — 파생 + 계획까지, 제출 없음. 세션 밖이어도 안전(RTH 게이트가 zero-order로 끝낸다).
uv run python -m scripts.run_b0x_kr_cycle

# 결정성 증명 (아무것도 쓰지 않음, fresh 계좌 스냅샷은 1회 읽는다)
uv run python -m scripts.run_b0x_kr_cycle --derivation-only --repeat 2
# → DERIVATION_DETERMINISM=IDENTICAL 이어야 한다
```

🔴 **`--confirm` 플래그가 없다.** crypto 사이드카와 달리, 있어봤자 매 호출이
`KrMockSubmissionNotWired` 를 던지므로 있으면 오히려 "제출 가능"으로 오독된다.

산출물: `~/work/herdr-artifacts/b0x/kis_mock/` 아래
`cycles.jsonl`(append-only) · `<ts>-cycle.md` · `attributed_book.json` ·
`operator-notices.jsonl` — crypto 사이드카와 동일 레이아웃(`scripts.b0x.ledger`, 코어 그대로).

## 5. 코어 승계 — 무엇을 재사용했고 무엇을 더했는가

| 파일 | 상태 |
|---|---|
| `envelope.py` | **재사용 + 추가.** `Envelope.daily_loss_kill_basis` 필드 추가(기본값 `"absolute"` — crypto 동작 불변). `KR_MOCK_ENVELOPE`(§6) 신규 상수, `_LOCKED_ENVELOPES["kr"]` 등록. |
| `kill_switch.py` | **재사용 + 추가.** `evaluate()` 가 `daily_loss_kill_basis="pct_of_nav"` 일 때 `state.nav × 비율` 로 절대 임계값을 계산 후 비교(§7). `state.nav is None` 이면 `MissingNavForRatioKill` 로 fail-closed. crypto 의 절대 비교 경로는 그대로. |
| `state.py` | **재사용 + 추가.** `LaneAccountState.nav` 필드 추가(기본 `None`, 해시 대상 — NAV 는 cash/positions 만으로 재구성 불가한 mark-to-market 값이므로 결정성 주장이 성립하려면 해시 입력이어야 한다). |
| `derivation.py` | **재사용, 무변경.** L1/L2·물타기·R1/R2 50/50 전부 시장무관 — KR 도 그대로 통과한다. |
| `table_source.py` | **재사용 + 추가.** `MAX_TABLE_AGE["kr"] = 36h` 등록(§8) — 계약 v1.1 §2-2 리터럴, 워커 임의 수치 아님. |
| `ledger.py`, `labels.py` | **재사용, 무변경.** |
| `cycle.py` | **재사용 + export.** `_base_record`/`_render_report` 를 `base_record`/`render_cycle_report` 로 공개해 KR 사이클이 재사용한다(이름만 바꿈, crypto 두 레인 동작 불변 — `tests/scripts/b0x/test_cycle.py` 로 확인). |
| `scripts/b0x/kr/mock.py`, `scripts/b0x/kr/cycle.py` | **신규.** KIS 계좌 read facade·tick 정렬·사이징·RTH 게이트·사이클 골격. |

## 6. 🔴 제출은 미배선이다 — `scripts.b0x.kr.mock.unwired_submit_order`

Binance 사이드카는 `demo-api.binance.com` 에만 닿을 수 있는 **전용 클라이언트**를 재사용해
"mock 임을 코드 구조가 보장"한다. KIS 는 그런 전용 mock 클라이언트가 없다 —
`app.mcp_server.tooling.order_execution._place_order_impl` 하나가 `is_mock` 불리언 하나로
`kis_live_*`/`kis_mock_*` 를 모두 처리한다. 어느 통합점(그 함수를 감싸는
`orders_kis_variants._place_order_variant` 재사용 vs 이 패키지 전용의 더 좁은 함수)이
안전한지는 이 PR 이 혼자 정하지 않는다.

`confirm=False`(기본, 유일하게 테스트된 경로)는 계획까지만 하고 제출을 아예 시도하지
않는다. `confirm=True` 로 부르면 `unwired_submit_order` 가 항상
`KrMockSubmissionNotWired` 를 던진다 — "아직 안 만듦"이 "제출해서 0건 확인함"으로
둔갑하지 않도록, 조용한 no-op 대신 예외로 막는다(orch 가 전달한 X-C 교훈: `live_contact=0`
류 상수 스탬프를 보고로 내세우지 말라는 지적을 KR 은 코드 구조로 지킨다).

**후속 작업 조건 (둘 다 필요):**
1. §0-1 의 `operator_contract.yaml` 등록 — 계좌맵 게이트 해소.
2. 제출 통합점 선택 — 운영자/리뷰어 사인오프.

## 7. NAV 상대 kill switch — 통화 단위 정합성

crypto 사이드카의 kill 은 `일 손실 5 USDT` (절대값, `daily_loss_kill_basis="absolute"`).
KR 은 계약 §4 그대로 `일 손실 −2.5% NAV` (**비율**). `evaluate()` 는

```
effective_kill = state.nav * envelope.daily_loss_kill   # basis="pct_of_nav" 일 때만
```

를 먼저 계산해 **KRW 절대값**으로 변환한 뒤에만 `state.realized_pnl_today`(역시 KRW)와
비교한다. `KillSwitchDecision.canonical()` 은 `daily_loss_kill`(유효 절대 임계값) ·
`daily_loss_kill_basis` · `daily_loss_kill_config`(원 비율) · `nav_snapshot` 을 전부
기록한다 — 두 숫자가 실제로 같은 통화인지 산출물만 보고 검산할 수 있게.

이것은 orch 가 relay 한 X-C 검증 MEDIUM 결함(shadow kill 이 USDT 상수를 KRW 실현손익에
직접 비교)을 KR 이 그대로 베끼지 않았다는 증거다 — crypto 자체 결함은 이 PR 의 범위가
아니며 손대지 않았다.

NAV = fresh 읽기의 `cash`(주문가능현금, 없으면 예수금총액) + Σ `evlu_amt`(보유종목 평가금액),
매 사이클 재계산한다(`scripts.b0x.kr.mock.read_fresh_truth`) — 고정값이 아니다.

## 8. `MAX_TABLE_AGE` — KR = 36h (계약 v1.1 §2-2)

🔴 **정정 이력**: 최초 지시는 "계약에 없으니 KR 에 age 게이트를 넣지 말고
`NEEDS_UPSTREAM(table_age_gate_not_in_contract)` 로 보고하라"였다. 이 지시는
**철회됐다** — 운영자가 X-C 검증이 찾은 안전장치(crypto 전용 8h)를 계약 자체로
승격시켰다.

**계약 v1.1 §2-2** (sha256 `97278b0e8b8000e2e663c936328686001af5850087897270
bc80a95ebf8f6b2e`, 운영자 확정 2026-08-08, 원문 = `~/work/herdr-inbox/
b0x-experiment-contract-v1-20260808.md`):

> 표가 없거나 `STALE` 이거나 `MAX_TABLE_AGE` 초과면 그 사이클은 주문 0
> (조용한 재사용·재계산 금지, 사유 기록).
> **MAX_TABLE_AGE (v1.1, 운영자 확정 2026-08-08): crypto 8h · KR 36h · US 36h**
> — X-C 검증 발 안전장치의 계약 승격, 3시장 공통 적용.

`table_source.MAX_TABLE_AGE["kr"] = timedelta(hours=36)` — 이 PR 이 리터럴 그대로
등록했다(US 는 별도 잡(X-U)의 몫이므로 여기서 추가하지 않는다). 초과 시 사유 코드는
기존 5경로와 동일한 문(`load_policy_table`)을 통해 `stale_by_age` 로 기록되며,
5-경로 게이트 로직 자체는 무변경 — crypto 도 이미 쓰던 문에 값 하나를 더했을 뿐이다.

회귀 가드:
- `tests/scripts/b0x/test_kr_envelope_and_kill_switch.py::
  test_max_table_age_kr_is_36h_per_contract_v1_1` — 값 자체.
- `tests/scripts/b0x/kr/test_cycle.py::
  test_table_older_than_36h_derives_zero_orders_with_reason` /
  `test_table_just_under_36h_still_derives_orders` — 사이클 종단 경계 동작.

## 9. envelope — 덮어쓸 수 없는 상수

`scripts/b0x/envelope.py` 의 `KR_MOCK_ENVELOPE`:

```
종목당 신규 30만 KRW · 총투입 상한 = 신규×5(150만 KRW) · 동시 포지션 ≤ 10 ·
일 신규 진입 ≤ 3 · 일 손실 −2.5% NAV → kill
```

- CLI 플래그 없음, 환경변수 없음 — `envelope.py` 는 `os` 를 import 하지 않는다.
- `assert_envelope_locked()` 가 모든 계좌 읽기 **이전에** 동치성을 검사한다.
- **매수측만** 캡한다 — 매도(청산)를 캡하면 재고가 갇힌다. floor 이후 실현 notional
  재검사(ROB-993 R3 교훈, `scripts.b0x.kr.mock.plan_orders`).
- "물타기 회차 상한 없음"(계약 §4)은 `config.averaging_k_levels` 가 무제한이라는 뜻이지
  이 envelope 의 어느 필드가 무한이라는 뜻이 아니다 — 누적 지출은 종목당 총투입 캡이 막는다.

## 10. 알려진 경계 · 미해결

- **§0 의 두 조건**이 가장 크다 — 계좌맵 게이트 + 제출 미배선.
- **오염(CONTAMINATED) 판정 미구현.** crypto 사이드카는 venue 의 `foreign_*` 잔고/미체결을
  탐지해 오염 시 제출을 막는다. KR 은 주문 읽기(order-history) 서피스를 아직 연결하지
  않았다 — 어차피 §6 때문에 제출 자체가 막혀 있어 이번 PR 에서는 영향이 없지만, 제출을
  배선하는 후속 작업은 이 갭도 함께 닫아야 한다.
- **NXT/시간외는 완전 배제.** 정규장 매도의 NXT 연장(ROB-671) 같은 다른 레인의 규칙은
  B0-X 에 적용하지 않는다 — 계약이 "정규장만" 이라고 명시했다.
- 스케줄러 등록 없음 (v1 수동 kickoff, crypto 와 동일).

## 11. 테스트

```bash
uv run pytest tests/scripts/b0x/ -q          # 코어 + crypto + kr 전부
uv run pytest tests/scripts/b0x/kr/ -q       # kr 전용
```

kr 전용 포함: tick 정렬(KRX 2023+ 표와 일치) · 매수/매도 사이징 whole-share floor ·
NAV = cash + Σ evlu_amt · RTH 게이트(세션 밖 → zero-order, 표 I/O 전혀 없음) · 표 게이트
5경로 재사용 · `MAX_TABLE_AGE["kr"]=36h`(계약 v1.1 §2-2) 값 고정 + 37h/35h59m 경계
사이클 종단 동작 · NAV 비율 kill(단위 정합성, 재계산됨을 증명) · 결정성(2회 동일 해시) ·
`confirm=True` 가 항상 fail-closed 함 · CLI 에 envelope 필드/`--confirm` 플래그 없음 ·
KIS 주문 서피스 금지 import 정적 가드.
