# B0-X KR 사이클 런북 — kis_mock

> `B0_UNVALIDATED` · `SELL_SIDE_MODEL_MISMATCH` · `FIDELITY_INCONCLUSIVE_COVERAGE`

계약 정본: `~/work/herdr-inbox/b0x-experiment-contract-v1-20260808.md` — 결속은
**버전 문자열(v1.3) + 절 인용**이며, 전체파일 sha256 은 참고 필드일 뿐이다(계약 본문
"결속 = 버전 문자열 v1.3 + 인용 절"). 현재 참고 sha256 =
`0125e2ea96b1a54cf0b0a50e6ed85ae1f3a72e7870abe727d2734dbe20e19b1f`.
계좌맵 정본: **`~/services/auto_trader-operator/operator_contract.yaml`**
(기계판독, 2026-08-09 확정 — `mock/CLAUDE.md` §1 은 이제 참조 서술일 뿐이며 충돌 시
YAML 이 이긴다). HEAD `3f40291`(PR #33) 에서 검증 — 아래 §0.
코어 정본: `scripts/b0x/{envelope,kill_switch,state,derivation,ledger,labels,cycle}.py`
(X-C, crypto, PR #1814 이 세웠다 — 이 잡은 코어를 재구현하지 않고 승계한다. 아래 §5)

## 0. 계좌맵 게이트 — 2026-08-09 해소, 제출 배선 — 계약 v1.3 ③

**두 조건이 R1(첫 라운드) 시점엔 둘 다 열려 있었고, 이 라운드(R2) 시점엔 둘 다 닫혔다.**

1. **계좌맵 게이트.** `operator_contract.yaml`
   `mock_account_strategy_exclusive.strategy_order_exceptions` 에
   `b0x-adapter-orders-20260808` 가 등재됐고, 그 예외의
   `b0x_adapter_orders_20260808.surfaces` 에 `kis_mock` 이 포함된다.
   `resolved_account_reassignments.kis_mock.mutation_policy` 는 더 이상
   `no_new_orders` 가 아니라 `b0x_adapter_orders_only_within_envelope` 다.
   `mock/CLAUDE.md` §1 kis_mock 행은 이와 모순되지 않는다(참조 표면, YAML 이 정본).
2. **제출 통합점.** 계약 v1.3 ③ 이 `app.services.brokers.kis.mock_scalping_exec.
   adapters.KisMockBroker`(ROB-321/341) 를 지정했다 — 재구현이 아니라 기존 표면
   재사용. 아래 §6.

**그럼에도 이 PR(R2) 은 실주문 0 이다** — 계좌맵/통합점이 풀렸다고 해서 이 라운드가
실제로 kis_mock 을 향해 발사한다는 뜻은 아니다. §0.1 이 그 경계를 정확히 그린다.

### 0.1 이 PR 이 실제로 한 것과 하지 않은 것

- **한 것**: `run_kr_cycle(confirm=True)` 가 이제 `KisMockBroker` 로 실제로 라우팅된다
  (fake/stub 브로커로 오프라인 증명, §11).
- **하지 않은 것**: `scripts/run_b0x_kr_cycle.py` CLI 에 `--confirm` 플래그가 없다
  (여전히). 이 세션 동안 실 kis_mock 계정에 대한 preview/place 호출 0. 첫 실사이클은
  월요일 KRX 세션, 별도 job.

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

🔴 **`--confirm` 플래그가 없다.** 제출은 §6 대로 배선됐지만(계약 v1.3 ③), 이 CLI 는
그 배선을 노출하지 않는다 — 실사이클은 별도 job(§0.1).

산출물: `~/work/herdr-artifacts/b0x/kis_mock/` 아래
`cycles.jsonl`(append-only) · `<ts>-cycle.md` · `operator-notices.jsonl`
— crypto 사이드카와 동일 레이아웃(`scripts.b0x.ledger`, 코어 그대로).

🔴 **레인 상태 파일은 없다.** 이전 판 런북은 `attributed_book.json` 을 산출물로 적었지만
그런 파일은 **한 번도 생성된 적이 없다**(읽기 경로만, 쓰기 경로 0건). 계약 v1.5 ① 이 그
경로를 폐기했다 — 아래 §9.1.

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

## 6. 제출 배선 — `KisMockBroker` (계약 v1.3 ③)

Binance 사이드카는 `demo-api.binance.com` 에만 닿을 수 있는 **전용 클라이언트**를 재사용해
"mock 임을 코드 구조가 보장"한다. KIS 에는 그런 host-allowlisted 전용 클라이언트가 없다 —
대신 계약 v1.3 ③ 이 지정한 통합점은 `app.services.brokers.kis.mock_scalping_exec.
adapters.KisMockBroker`(ROB-321/341): `_place_order_impl(is_mock=True, ...)` 이 전
호출부에 **리터럴로 고정**된(인자 아님) 기존 리뷰·운영된 mock 전용 표면. 재구현하지 않고
그대로 재사용한다 — `scripts.b0x.kr.mock.build_kis_mock_broker`/`submit_planned_order`
가 얇은 배선 레이어일 뿐, `KisMockBroker` 자체는 서브클래싱도 몽키패치도 하지 않는다.

**하나의 문서화된 불일치, 덮지 않고 그대로 보고한다.** `KisMockBroker` 의 매수(BUY) leg
는 실 POST 직전 살아있는 오더북(bid/ask/spread/age) 을 `get_state` 콜백으로 재검증한다
(ROB-843 P1-1) — 이건 그 어댑터 자신의 라이브 WS 피드(`mock_scalping_ws`) 를 전제로 설계된
freshness 모델이다. B0-X 는 그런 피드가 없다 — `policy_table.v1` 스냅샷에서 파생할 뿐, 실시간
오더북을 보지 않는다. 가짜 호가를 지어내 안전장치를 통과시키는 대신,
`build_kis_mock_broker` 는 `get_state` 가 **항상 `None`** 을 반환하게 한다. 결과: 실제
BUY 디스패치(`confirm=True`) 는 실 HTTP POST **이전에** 그 어댑터 자신의
`PreSendFreshnessError(("no_market_state",))` 로 fail-closed 한다 — 정직하고, 특별히
새로 만든 차단이 아니라 어댑터 자신의 기존 예외다. SELL leg(`submit_exit_sell`) 는 ROB-321
자체 설계상("포지션을 닫는 매도는 항상 허용돼야 한다") 이 훅이 없어 영향받지 않는다. B0-X 용
실 시세 피드를 배선해 BUY 쪽 차단을 푸는 일은 이 PR 스코프 밖이다.

`confirm=False`(기본, CLI 가 유일하게 노출하는 경로)는 브로커를 아예 구성하지 않고 계획까지만
한다. `confirm=True` 는 실제로 `KisMockBroker.submit_buy`/`submit_exit_sell` 을 호출한다 —
"아직 안 만듦"이 "제출해서 0건 확인함"으로 둔갑하지 않도록, `KrCycleOutcome.record["submitted"]`
는 실제 호출 결과를 그대로 담는다.

**`unwired_submit_order`/`KrMockSubmissionNotWired` 는 삭제하지 않았다.** 주 경로는 더 이상
그것을 부르지 않지만, `build_kis_mock_broker`/`submit_planned_order` 를 우회해 제출을
시도하는 어떤 미래 경로에도 여전히 정직한 "아직 안 만듦" 신호로 남아 있다
(`tests/scripts/b0x/kr/test_mock.py::test_unwired_submit_order_always_raises`).

## 6.5 AST 가드 — 계약 v1.3 ③ 의 안전선

선례 = Kiwoom live read-only 가드(`CLAUDE.md` "계좌번호 부재 (3중)" ②). 파일 =
`tests/scripts/b0x/kr/test_submission_ast_guard.py`. 3 금지:

1. **`is_mock=False`** — 리터럴·변수·기본값 경유(is_mock 을 아는 콜러블을 호출하며
   키워드 자체를 생략) 전부.
2. **live 주문 모듈 import** — **allowlist** 방식(denylist 아님): `app.services.
   brokers.kis.*`/`app.mcp_server.tooling.*`/`app.services.kis_trading_service`/
   `app.services.kis_mock_runner.*` 아래에서 명시적으로 허용된 것 외 전부 거부.
   `FORBIDDEN_LIVE_MODULES` 상수가 구체적으로 알려진 live 모듈을 전수 열거하며,
   그 열거가 실제로 거부되는지 자체 회귀 테스트로 고정한다.
3. **문자열 우회** — `importlib.import_module`/`__import__`/`exec`/`eval`, 또는
   비-리터럴 속성명을 쓰는 `getattr()`.

🔴 **보장 강도 — 정직하게: "우발 방지 + 정적 검출"이지 "구조적 불가능"이 아니다.**
Binance Demo 의 host-allowlist(네트워크 계층에서 물리적으로 다른 호스트에 닿을 수
없음)와 다르다 — 이건 이 패키지 자신의 소스를 스캔하는 정적 검사이며, 가드 자체를
의도적으로 고쳐 쓰거나 이 패키지 밖에서 우회하는 것까지는 막지 못한다.

**mutant 로 실제 발화를 증명했다** (가드 존재 ≠ 가드 발화): 세 금지 각각을 실 소스
파일에 실제로 주입 → 대응 테스트가 FAIL 하는지 확인 → 원복, 3/3 확인. 가드 자신의
로직도 합성 소스로 먹인 self-test 로 별도 회귀 보호된다(같은 파일 하단). 상세 증거는
job 보고서(`impl-r2.md`) 참고, 이 파일 자체는 아니다.

`tests/scripts/b0x/kr/test_no_live_kis_order_imports.py` 는 R1 이 만든 더 좁은 denylist
가드로 **독립적으로 유지** — 새 가드로 대체되지 않았다(둘 다 통과해야 한다).

## 6.6 kill 시 잔여 주문 취소 — 구조적으로 시도 불가

crypto 사이드카는 kill 발화 시 venue 의 open-orders 읽기를 이용해 B0-X 소유 미체결을
찾아 취소한다(`scripts.b0x.cycle._cancel_b0x_open_orders`). KIS 모의투자는 이 경로가
없다 — `DomesticOrderClient.inquire_korea_orders`(TR `TTTC8036R`, 미체결 주문 조회) 는
`is_mock=True` 에 대해 **명시적으로** `RuntimeError` 를 던진다("모의투자에서 지원되지
않음"). 조회 수단이 없으니 취소 대상을 알 수 없고, `cancel_korea_order` 역시 (명시적
`krx_fwdg_ord_orgno` 없이는) 내부적으로 같은 조회를 거쳐야 해서 똑같이 막힌다.

이건 미배선이 아니라 **KIS 모의투자 API 자체의 구조적 한계**다 — 그래서 kill-switch
notice 문구를 lane 별로 분리했다(`KillSwitchDecision.operator_notice(remaining_orders_
note=...)`). KR 은 crypto 의 "잔여 주문 취소 완료" 문구를 쓰지 않고
`scripts.b0x.kr.cycle.KILL_CANCEL_UNSUPPORTED_NOTE` 로 사실대로 "취소는 구조적으로
시도 불가"라고 말한다. `record["cancelled"] = []` + `record["cancellation_unsupported"]`
로 "시도해서 실패"와 "애초에 시도 안 함"을 구분해 기록한다.

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

### 9.1 🔴 상한 입력 = 브로커 진실 (계약 v1.5 ①) — KR 은 한 입력이 조회 불가

> 🔵 **이 절은 X-E1(계약 v1.5) 시점의 상태를 기술한다.** 자기 미체결 항목은
> **계약 v1.6 ① 로 해소**됐다 — §9.2 를 먼저 읽어라. 아래 "왜 조회 불가인가"
> 두 표면의 실측은 v1.6 이후에도 그대로 사실이며(브로커는 여전히 답하지 못한다),
> `PendingUnreadable` 도 삭제되지 않고 **원장이 답하지 못할 때의 상태로 남아 있다**.
> "매 사이클 파생 0건"만 v1.6 으로 대체된 서술이다.

무엇이 틀렸었는지는 crypto 런북 §6.1 과 동일하다(상한이 사이클당으로만 구속, 상태 파일이
한 번도 쓰이지 않음). KR 도 같은 코어(`scripts/b0x/broker_truth.py`)를 쓴다 — 레인별 복붙
없음. 다만 **입력 하나가 KIS 모의투자에서 조회 불가**다.

| 계약 v1.5 ① | KR 입력 |
|---|---|
| 동시 포지션 = non-dust 매도가능 잔고의 수 | `FreshTruth.non_dust_position_symbols()` — KRX 최소 거래단위 = 1주, 1주 미만은 dust |
| 동일 심볼 재제출 = 자기(`b0xk`) 미체결 있으면 신규 제출 금지 | 🔴 **조회 불가** — 아래 |
| 일일 신규 = {자기 미체결 ∪ non-dust 포지션 ∪ 당 사이클 신규 제출} distinct | 보유 + 당 사이클 (미체결 항은 조회 불가) |

**🔴 왜 조회 불가인가 — 두 표면 모두 막혔고, 둘 다 이미 레포에 실측 기록이 있다.**

1. `DomesticOrderClient.inquire_korea_orders` (TR `TTTC8036R`, 미체결 주문 조회) 는
   `is_mock=True` 에 대해 **명시적으로 raise** 한다("모의투자에서 지원되지 않음").
   §6.6 의 kill 취소 불가와 같은 뿌리다.
2. `inquire_daily_order_domestic` (daily-ccld) 는 모의 TR 로 라우팅되긴 하지만, ROB-341
   이 **당일 모의 주문 활동이 있었는데도 `rt_cd=0` + 빈 행**을 반환하는 것을 실측했다
   (`docs/runbooks/kis-mock-scalping-smoke.md`). 그래서 스캘핑 엔진도 이걸 non-gating
   post-settlement 진단으로 강등했다. **빈 응답이 미체결 부재를 증명하지 못한다.**

**어떻게 처리했나 — fail-closed, 숨기지 않는다.** `kr_mock.KR_PENDING_UNREADABLE`
(`PendingUnreadable` 센티널) 을 상태로 실어 나르고, `BrokerTruth.resubmit_block` 이 그
상태에서 **모든 심볼을 거부**한다 — 마치 전 종목에 미체결이 걸린 것처럼. 「조회 불가」를
「미체결 없음」으로 접으면 중복 방지가 조용히 죽는데, 그건 이 조항이 막으려는 결함
그 자체다.

**귀결 — 정직하게.** 이 상태가 유지되는 동안 KR 레인은 **매 사이클 파생 주문 0건**이며,
후보 행마다 `own_pending_unreadable` + 사유가 skipped 표에 기록된다(사이클 산출물의
skipped 표가 그대로 렌더링되므로 "무엇이 막혔는지"는 남는다). 검사는 파생과
`submit_planned_order`(디스패치 직전) **두 곳**에 있다.

🔵 운영상 영향은 오늘 기준 크지 않다: KR 매수 디스패치는 이미 `PreSendFreshnessError`
로 구조적으로 막혀 있고(§10, §6), CLI 에 `--confirm` 이 없다. 즉 이 조치가 "되던 주문을
막은" 것이 아니라 **되지 않던 이유가 하나 더 명시화**된 것에 가깝다.

🔵 **해소됨 — 계약 v1.6 ① (2026-08-09, 운영자 "승인이야").** X-E1 이 후보로만 등재했던
`kis_mock_order_ledger` 가 조건부로 승인됐다. 상세 = **§9.2**.

🔴 **realized P&L 입력도 없다.** 상태 파일 제거로 `realized_pnl_today` 의 출처가
사라졌다(원래도 파일이 없어 항상 0이었다). 따라서 `−2.5% NAV` kill 은 발화할 수 없으며,
산출물이 `realized_pnl_source` 로 그 사실을 명시한다 — 측정된 0이 아니라 **입력 부재**다.

**포지션 출처.** 계약 v1.5 ③("B0-X 물타기/매도 = 자기(mock) 보유에서만 파생") 대로
`fetch_my_stocks` 보유가 곧 B0-X 장부다(kis_mock = B0-X 전용 주문 레인). `average_price`
= 브로커 매입평균가. `entry_count` 는 스냅샷에 없으므로 `0` 으로 기록한다 — 파생이 읽지
않는 값을 그럴듯하게 지어내 해시 입력에 넣지 않는다.

🔴 **누적 투입액도 조회 불가 — 물타기 fail-closed.** `B0XPosition.invested_notional` 의
정의는 **누적 투입(deployment)** 이며 "부분 매도로 줄지 않는다" — §4 종목당 총투입 캡이
현재 보유가 아니라 *이제까지 넣은 돈* 을 묶기 때문이다. 그런데 브로커 스냅샷 1회로 얻을 수
있는 건 **취득원가(`수량 × 매입평균가`)** 뿐이고, 이건 부분 매도 때 **줄어든다** → 캡이
이미 쓴 헤드룸을 되돌려준다. 그래서 이 레인은
`LaneAccountState.cumulative_deployment_readable=False` 를 선언하고, 파생이 **기존 포지션에
대한 추가(물타기)를 거부**한다(`cumulative_deployment_unreadable`). 과소평가된 숫자에 대고
사이징하느니 막는다 — 미체결 조회 불가와 같은 자세다.

🔵 **이 조치가 닫지 **못하는** 경계**: 열었다가 **전량** 청산한 종목은 보유 행 자체가 사라져
과거 투입액이 신규 진입에 보이지 않는다. 이건 브로커 진실 배선 이전부터 있던 설계 공백이며
(어느 레인이든 동일), 이번 잡이 새로 만든 것이 아니다 — 후속 과제로 남긴다.

### 9.2 🔴 KR 자기 미체결 출처 = `kis_mock_order_ledger` (계약 v1.6 ①, X-E2)

**정본 모듈: `scripts/b0x/kr/pending_ledger.py`.** §9.1 이 「조회 불가」로 남겨 둔 세 번째
입력(자기 `b0xk` 미체결)만 여기서 공급한다. 나머지 두 입력은 손대지 않았다.

**왜 예외가 정당한가 — `attributed_book` 과 무엇이 다른가.** v1.5 가 `attributed_book.json`
을 폐기한 사유는 「자기 기록」이라는 **개념**이 아니라 **쓰기 경로가 레포에 0건**이었다는
사실이다(매 사이클 `None` 로드 → 카운터 상시 0). 이 원장은 반대로 **제출 chokepoint 가 매번
강제로 쓴다**:

- `app/mcp_server/tooling/order_execution.py` `_execute_and_record` 의 **첫 동작**이
  `is_mock` 분기의 pre-submit 귀속 게이트다. 귀속 실패 → `MissingAttribution` → **브로커
  read 조차 하기 전에** 반환. 귀속 성공 → `record_signal` 이 `review.kis_mock_signal_ledger`
  행을 **전송 전에 커밋**하고, 그 쓰기가 실패하면 **주문 자체가 나가지 않는다**
  (`error_code="signal_record_unavailable"`). `correlation_id`/`strategy`/`signal_source` 는
  NOT NULL + 공백거부 CHECK.
- 전송 후 `review.kis_mock_order_ledger` 행(계약이 지명한 표)이 같은 `correlation_id` 로 쓰인다.

**두 표를 다 읽는다 — 넓힌 것은 예외가 아니라 차단이다.** 사후 order 행은 유실될 수 있고
(`LedgerWriteError` → `ledger_id=None` → `ledger_tracking_unavailable`), 브로커에 존재하는
주문의 원장 행이 없는 상태가 바로 v1.6 ③ 이 금지한 **관대한 방향(누락)** 이다. pre-submit
signal 행은 유실되면 전송이 거부되므로 그 구멍을 닫는다. 둘 다 `kis_mock` 스코프이고 둘 다
계약이 지명한 그 chokepoint 가 쓴다.

**「미체결」의 정의 — 의도적 상위집합.** 🔴 이 모듈은 `lifecycle_state` 를 **보지 않는다**.
어떤 상태 판정이든 「이미 체결/취소됐으니 풀어줘도 된다」는 추정이 되고, 추정이 틀리면
아직 걸려 있는 주문을 푼다(= 관대한 방향). 그래서 **이 레인이 당일 그 심볼에 대해 남긴 행이
하나라도 있으면 pending 이다.** 계약 v1.6 ③ 이 이 오류 방향을 문언으로 수용한다
("체결·취소분이 pending 으로 보일 수 있음 — 안전한 실패로 수용").

**유일한 경계 = KST 거래일.** KRX 주문은 당일 주문이다. ROB-671 분류기
(`app/services/brokers/kis/live_order_expiry.py` — **인용만 하고 import 하지 않는다**;
KR AST 가드가 금지 목록에 올려 뒀다)가 세션×매매구분 **전 조합의 최대 만료를 접수일 20:00
KST** 로 못박는다(정규장 매도의 NXT 연장 포함). 즉 KST 일 *D* 에 접수된 주문이 *D+1* 까지
걸려 있을 수 없다 — 시간 경과로 「체결됐겠지」를 추정하는 것이 아니라 **거래소의 주문 유효
기간**이다. §4 일일 신규 캡의 경계와 동일하므로 두 규칙이 구조적으로 일치한다.

**조회 실패 = 다시 unreadable (v1.6 ④).** 어떤 이유로든 조회가 실패하면
`pending_ledger.ledger_unreadable(...)` 이 `PendingUnreadable`(reason=
`kis_mock_ledger_pending_unreadable`) 을 돌려주고, `resubmit_block` 이 **다시 전 심볼을
거부**한다. 실패를 `()` 로 접는 코드 경로는 이 모듈에 없다. detail 에는 예외 **타입 이름만**
싣는다(DB 오류 메시지에 DSN·자격증명이 실릴 수 있고, 이 값은 산출물에 박제된다).
X-E1 의 `KR_PENDING_UNREADABLE` 상수도 **삭제하지 않았고 여전히 기본값**이다 —
`FreshTruth.broker_truth()` 를 인자 없이 부르면 v1.6 이전과 동일하게 fail-closed 다.

**포지션 진실은 그대로 브로커 (v1.6 ②).** 원장은 미체결 dedup/캡 입력에만 들어간다.
`positions` 는 계속 `fetch_my_stocks` 보유에서만 만들어지고, `broker_truth.position_symbols`
는 `non_dust_position_symbols()` 뿐이다. AST 가드가 `positions=`/`position_symbols=` 인자에
pending 파생 이름이 들어가는 것을 빌드 실패로 만든다.

**kis_mock 한정 (v1.6 ① 🔴 crypto·US 확대 금지).** `scripts/b0x/kr/**` 밖의 어떤 b0x 모듈도
`scripts.b0x.kr.pending_ledger`·`app.models.review`·kis_mock 원장 모듈을 import 할 수 없다
(AST 가드). crypto 사이드카는 v1.5 그대로 venue 의 open-orders 를 `b0xc` 로 필터해 읽는다.

**우회 제출 금지 (v1.6 ③ 신규 mutant).** 원장 기반 dedup 은 "나간 주문은 전부 원장에 있다"
가 참일 때만 성립한다. AST 가드가 `scripts/b0x/kr/**` + 러너 안의 모든 제출 호출
(`submit_buy`/`submit_exit_sell`/`_place_order_impl`/…) 이 **`kr.mock.submit_planned_order`
안에만** 존재하도록 강제하고(그 함수만이 `KisMockBroker` → `order_execution` pre-submit
게이트를 탄다), 그 안에서 `assert_resubmit_allowed` 가 제출보다 **앞줄**임을 확인한다.

**귀결 — 월요일 첫 KR 사이클.** 이 레인은 아직 한 건도 제출한 적이 없으므로 원장이
`b0xk-` 행을 0건으로 **답한다**(unreadable 아님) → 후보 행이 정상 파생된다. 같은 KST 일의
2·3사이클차는 1사이클차가 남긴 행을 읽어 **파생 0**. 실측 = `test_kr_two_cycle_sim_the_
same_symbol_is_never_submitted_twice` (고정 fixture, 실브로커 호출 0).

🔵 §10 의 `PreSendFreshnessError`(BUY leg 실디스패치 구조적 차단)와 CLI `--confirm` 부재는
**이 라운드에서도 그대로**다. 즉 v1.6 은 "파생이 되게" 만들었을 뿐 "실주문이 나가게" 만들지
않았다.

## 10. 알려진 경계 · 미해결

- **BUY leg 실 디스패치는 항상 `PreSendFreshnessError` 로 막힌다** — B0-X 용 실시간
  시세 피드가 없어서다(§6). SELL leg 는 이 훅이 없어 영향받지 않는다 — 비대칭이며,
  의도적으로 덮지 않고 문서화했다.
- **kill 시 잔여 주문 취소가 구조적으로 불가능하다**(§6.6) — KIS 모의투자 미체결조회
  API 자체가 `is_mock=True` 를 지원하지 않는다. crypto 패턴을 그대로 옮길 수 없다.
- **오염(CONTAMINATED) 판정 미구현.** crypto 사이드카는 venue 의 `foreign_*` 잔고/미체결을
  탐지해 오염 시 제출을 막는다. KR 은 주문 읽기(order-history) 서피스를 아직 연결하지
  않았다 — 위 두 항목과 마찬가지로 KIS 모의투자의 미체결조회 API 한계와 같은 뿌리다.
  🔴 §9.2 의 원장 출처는 **이 공백을 닫지 않는다**: 원장은 `b0xk-` 로 시작하는 **자기**
  correlation_id 만 답하므로, 남이 이 계좌에 낸 주문은 여전히 보이지 않는다. v1.6 ① 의 예외
  범위가 「자기 미체결」이라 넓히지 않았다.
- **원장 조회는 DB 를 탄다.** §9.2 의 출처는 브로커 read 가 아니라 `DATABASE_URL` 로의
  세션이다. DB 가 죽으면 사이클은 오류가 아니라 `own_pending_unreadable` 로 **전 심볼 차단**
  하고 계속 진행한다(v1.6 ④). 파생 0 이 나왔는데 사유가 `own_pending_unreadable` +
  reason=`kis_mock_ledger_pending_unreadable` 이면 **브로커가 아니라 DB 를 보라**.
- **`B0X_KR_ENABLED`/`assert_kr_lane_enabled` 게이트가 `run_kr_cycle` 에서 호출되지
  않는다.** crypto 사이드카는 `run_sidecar_cycle` 진입 시 `assert_sidecar_enabled()` 를
  강제한다(대응 env: `B0X_SIDECAR_ENABLED`); KR 의 대응 함수는 정의만 되어 있고 아직
  배선되지 않았다(R1 부터의 기존 갭, 이 라운드가 새로 만들지 않았다) — 배선하려면 기존
  preview 테스트 다수가 이 env 를 설정하도록 함께 바뀌어야 해서 이번 스코프(제출 경로)
  밖에 남겼다.
- **NXT/시간외는 완전 배제.** 정규장 매도의 NXT 연장(ROB-671) 같은 다른 레인의 규칙은
  B0-X 에 적용하지 않는다 — 계약이 "정규장만" 이라고 명시했다.
- 스케줄러 등록 없음 (v1 수동 kickoff, crypto 와 동일). CLI 에 `--confirm` 없음(§0.1).

## 11. 테스트

```bash
uv run pytest tests/scripts/b0x/ -q          # 코어 + crypto + kr 전부
uv run pytest tests/scripts/b0x/kr/ -q       # kr 전용
```

v1.6 ① 전용 (`tests/scripts/b0x/kr/test_ledger_pending_source.py`): KR 2사이클+ 시뮬
(1사이클차 파생 2·제출 2 → 2·3사이클차 파생 0·제출 누계 2 → 익 KST 일 파생 2·누계 4) ·
원장이 답한 심볼만 `own_pending_order_exists` 로 차단 · 조회 실패/세션 실패 → `()` 아닌
`PendingUnreadable` (예외 **타입명만**, 메시지 비노출) · 상태 기반 해제 금지(모듈이
`lifecycle_state`/`status` 를 참조하지 않음을 AST 로 고정) · KST 거래일 경계 · 원장이
포지션이 되지 않음(행동 + AST) · 타 레인 import 금지(AST) · 원장 우회 제출 경로 금지(AST,
+ 제출보다 앞선 `assert_resubmit_allowed` 순서 확인). 각 detector 는 합성 소스로 자가검증.
`tests/scripts/b0x/kr/conftest.py` 가 실 원장 리더 도달을 **AssertionError** 로 막는다
(안 그러면 실수로 도달해도 `PendingUnreadable` 로 삼켜져 테스트가 조용히 무의미해진다).

kr 전용 포함: tick 정렬(KRX 2023+ 표와 일치) · 매수/매도 사이징 whole-share floor ·
NAV = cash + Σ evlu_amt · RTH 게이트(세션 밖 → zero-order, 표 I/O 전혀 없음) · 표 게이트
5경로 재사용 · `MAX_TABLE_AGE["kr"]=36h`(main SSOT `scripts.policy_table.core.
max_table_age` 재수출, crypto 8h·us 36h 동반 확인) 값 고정 + 37h/35h59m 경계
사이클 종단 동작 · NAV 비율 kill(단위 정합성, 재계산됨을 증명) · 결정성(2회 동일 해시) ·
CLI 에 envelope 필드/`--confirm` 플래그 없음 · KIS 주문 서피스 금지 import 정적 가드
(denylist, R1) · `submit_planned_order` 가 fake 브로커로 buy/sell 을 올바른 메서드에
라우팅함(correlation_id/가격/수량 스레딩 포함) · `confirm=True` 가 fake 브로커를 통해
실제로 디스패치됨(더 이상 무조건 raise 아님) · `unwired_submit_order` 가 여전히 raise
함(kept, not deleted) · AST 가드(allowlist, R2) 3 금지 + 자체 detector self-test.
