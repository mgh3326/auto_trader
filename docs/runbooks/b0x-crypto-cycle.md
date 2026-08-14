# B0-X crypto 사이클 런북 — Upbit shadow 본선 + Binance Spot Demo 사이드카

> `B0_UNVALIDATED` · `SELL_SIDE_MODEL_MISMATCH` · `FIDELITY_INCONCLUSIVE_COVERAGE`

계약 정본: `~/work/herdr-inbox/b0x-experiment-contract-v1-20260808.md` (운영자 확정 2026-08-08)
계좌맵 정본: `~/services/auto_trader-operator/mock/CLAUDE.md` §1 + `strategy_order_exceptions` ②

## 0. 이것이 무엇이 아닌가

**채점·승격 도구가 아니다.** B0-X 는 운영자 성문 규칙(B0)을 정책표에서 결정적으로 파생해
모의 계좌에서 기계 실행하고 그 결과를 *관측*한다. 산출물은 ①실행 충실도 ②규칙-현실 괴리
실측 ③운영자 학습뿐이며, 어떤 전략의 검증 근거도 아니다(D3 prospective confirmatory 아님).

`SELL_SIDE_MODEL_MISMATCH` 는 **버그가 아니라 관측 대상**이다. 매도측은 문서 B0 그대로
(R1/R2 50/50) 실행하며, calibration 이 밝힌 「실제 운영자는 전량매도」와의 괴리를 보는 것이
목적이다. 이 괴리를 "고치는" PR 은 실험을 무효화한다.

## 1. 두 레인

| | 본선 | 사이드카 |
|---|---|---|
| 이름 | Upbit shadow-sim | Binance Spot Demo |
| 계좌 | 없음 (가상 원장 JSON) | Binance Demo (공유 계정) |
| 실주문 | **0** — 어떤 베뉴에도 나가지 않음 | Demo 베뉴 한정, `--confirm` 필요 |
| 종목 | 표의 전 행 | **BTC/ETH/SOL-USDT 3종만** |
| envelope | 미적용 (계약 §4 각주, 기록만) | 적용 (§4 crypto 열) |
| 체결 | 합성 (아래 §4 터치 규칙) | 실제 매칭 엔진 |
| 존재 이유 | 신호·타이밍 증거 | 본선의 터치=체결 가정이 얼마나 낙관적인지 **측정** |

## 2. 사전 조건

1. `policy_table.v1` 최신 crypto 표가 있어야 한다.
   ```bash
   uv run python -m scripts.build_policy_table --market crypto
   ```
   출력 위치 = `~/services/auto_trader-operator/policy-tables/latest-crypto.json`.
   이 기본 경로는 §47 Prefect가 `policy-tables/`만 직접 커밋하는 정상 경로라 CLI가
   stderr에 공유 체크아웃 경고를 남긴다. 수동·격리 빌드는 반드시 별도
   `--out-dir /안전한/개인/경로`를 명시한다.
   B0-X 는 이 디렉토리를 **읽기만** 한다 (생성기를 호출하지 않는다).
2. 사이드카를 쓸 경우에만: `B0X_SIDECAR_ENABLED=true` + `BINANCE_SPOT_DEMO_ENABLED=true`
   + `BINANCE_SPOT_DEMO_API_KEY/SECRET` (또는 canonical `BINANCE_DEMO_API_*`).
   🔴 **`.env.prod` 전체를 로드하지 말 것.** 필요한 4개 변수만 추출해 주입한다 —
   prod DATABASE_URL 이 딸려오면 안 된다.

## 3. 실행

```bash
# 본선 — 실주문 0. 자격증명 불필요.
uv run python -m scripts.run_b0x_cycle --lane shadow

# 결정성 증명 (아무것도 쓰지 않고, 베뉴 접촉 없음)
uv run python -m scripts.run_b0x_cycle --lane shadow --derivation-only --repeat 2
# → DERIVATION_DETERMINISM=IDENTICAL 이어야 한다

# 사이드카 — 기본은 read-only + dry-run. mutation HTTP 0건.
B0X_SIDECAR_ENABLED=true BINANCE_SPOT_DEMO_ENABLED=true \
  uv run python -m scripts.run_b0x_cycle --lane sidecar

# 사이드카 실제 제출 (운영자 게이트). 아래 §7 체크리스트 통과 후에만.
... --lane sidecar --confirm
```

산출물: `~/work/herdr-artifacts/b0x/<lane>/` 아래
`cycles.jsonl`(append-only) · `<ts>-cycle.md` · `operator-notices.jsonl` ·
그리고 **shadow 레인만** `portfolio.json`(가상 장부 — 실제로 매 사이클 쓰인다).

🔴 **사이드카에는 레인 상태 파일이 없다.** 이전 판 런북은 `attributed_book.json` 을
산출물로 적었지만 그런 파일은 **한 번도 생성된 적이 없다** — 레포 전체에 읽기 경로만
있고 쓰기 경로가 0건이었다. 계약 v1.5 ① 이 그 경로를 폐기했고(아래 §4.5), 사이드카
계좌 상태는 매 사이클 브로커 읽기에서만 나온다.

## 4. 🔴 터치 규칙 — 본선이 무엇을 체결로 세는가

정본 = `scripts/b0x/crypto/shadow.py` 모듈 docstring (`TOUCH_RULE_STATEMENT` 로 모든
사이클 기록에 박제된다). 요약:

지정가 `P` 로 시각 `t` 에 접수된 주문은, `t` 이후 개장한 **완료된** 4h 봉 중

- **BUY** — `bar.low <= P`
- **SELL** — `bar.high >= P`

를 만족하는 **가장 이른** 봉에서 체결되며, **체결가는 정확히 `P`** 다.

명시적 귀결:

1. **형성 중인 봉은 체결이 아니다.** Upbit 분봉 엔드포인트는 미완성 봉을 반환하므로
   `completed_bars()` 가 이를 버린다.
2. 슬리피지·가격개선 없음.
3. **전량 아니면 미체결.** 부분체결·호가 깊이 미모형.
4. **호가 대기열 미모형 — 이것이 핵심 낙관이다.** 실제 지정가는 앞선 대기열이 소진돼야
   체결된다. 터치=체결은 체결을 **과대보고**하며, 특히 시장이 겨우 스친 레벨에서 심하다.
   👉 사이드카가 재려는 편향이 정확히 이것이다.
5. 수수료 0.05% 양방향 부과 (`UPBIT_KRW_FEE_RATE`).
6. 봉 하나가 매수·매도 레벨을 동시에 스치면 둘 다 체결한다 — OHLC 로는 봉 내부 순서를
   알 수 없다.

## 5. 사이클 골격 (두 레인 공통, 순서가 곧 안전 속성이다)

```
writer lock → 표 게이트 → 계좌 상태 → kill switch → 파생 → 실행 → 기록
```

- **writer lock 이 먼저**여야 두 프로세스가 동시에 "flat" 을 읽고 둘 다 제출하는 일이 없다.
- **표 게이트가 계좌 읽기보다 먼저**여야 계좌 상태로 표 부재를 우회할 수 없다.

### 표가 없거나 STALE 이면 그 사이클 주문 0 (계약 §2-2)

5가지 독립 경로, 각각 사유 코드가 기록된다:

| 사유 | 뜻 |
|---|---|
| `table_missing` | `latest-crypto.json` 없음 |
| `stale_marker_present` | 생성기가 `latest-crypto.STALE` 을 씀 |
| `schema_mismatch` | `policy_table.v1` 아님 / market 불일치 |
| `hash_mismatch` | 재계산 해시 ≠ 박제 해시 = **생성 후 편집됨** |
| `stale_by_age` | `generated_at` 이 8h(= 4h 주기 ×2)보다 오래됨 |

`stale_by_age` 는 B0-X 자체 추가다 — 생성기가 STALE 마커를 쓰기 전에 죽으면 낡은 표가
영원히 재생될 수 있다. 주문을 **늘리는 방향으로는 절대 작동하지 않는다**.

본선에서 표가 불가용이면 기존 가상 대기주문도 **전량 취소**한다 (조용한 재사용 금지).

## 6. envelope — 덮어쓸 수 없는 상수

`scripts/b0x/envelope.py` 의 `CRYPTO_SIDECAR_ENVELOPE`:

```
주문 10 USDT · 종목 총 50 USDT · 동시 포지션 ≤ 3 · 일 신규 ≤ 2 · 일 손실 5 USDT → kill
```

- CLI 플래그 없음, 환경변수 없음. `envelope.py` 는 `os` 를 import 조차 하지 않는다
  (AST 가드가 강제).
- `assert_envelope_locked()` 가 모든 네트워크/DB 호출 **이전에** 동치성을 검사한다.
  넓히든 좁히든 계약값이 아니면 fail-closed.
- LOT_SIZE 내림 **이후 실현 notional** 을 다시 검사한다 (ROB-993 R3 교훈).
- 🔵 **매수측만** 캡한다. 매도를 캡하면 청산이 막혀 재고가 갇힌다.

### kill switch vs 캡

§4 표는 성격이 다른 숫자를 한 표에 섞어 놓았다. 구분해서 구현했다:

- **kill** = `일일 정지` 행 = 일 손실 5 USDT. 도달 시 그날 **모든 신규 주문 중단 + B0-X
  잔여 주문 취소 + 운영자 통보**. 재개는 운영자 결정이므로 코드가 스스로 풀지 못한다.
- **캡** = 나머지 4개. 개별 주문을 사유와 함께 막을 뿐 레인을 세우지 않는다. 캡 포화는
  정상 사이클 결과다.

일 신규 캡이 후보보다 적을 때의 **동점 처리 = 심볼 사전순**. 기계에 재량이 없다.

### 6.1 🔴 상한 입력 = 브로커 진실 (계약 v1.5 ①)

**무엇이 틀렸었나.** 상한 입력이 `attributed_book.json` 에서 왔다 — 읽기 경로만 있고
쓰기 경로가 0건인 파일. 매 사이클 `None` 을 읽었으므로 카운터가 항상 0에서 시작했고,
계약이 **UTC 일자당** 이라 쓴 상한이 실제로는 **사이클 하나 안에서만** 구속했다.
crypto 는 4h 표 = 일 6사이클이므로 실효 일일 신규 상한은 2가 아니라 **12** 였다. 게다가
`submit_planned` 은 오염만 보고 계좌의 미체결 장부를 보지 않아 **같은 심볼·같은 레벨에
매 사이클 주문이 적층**될 수 있었다.

🔴 교훈: **「상한이 발화했다 ≠ 그 상한이 의도한 기간 동안 구속한다」.** 사이클 하나만
보면 상한은 정상 발화했다. 그 발화가 작동 증거처럼 보였다. **2사이클 연속**만이 둘을
구분한다.

**지금.** `scripts/b0x/broker_truth.py` 가 세 정의를 리터럴로 구현한다:

| 계약 v1.5 ① | 사이드카 입력 |
|---|---|
| 동시 포지션 = non-dust 매도가능 base 잔고의 수 | `FreshTruth.sellable_base_assets` (dust 판정 = `sellable_qty`, 계약 v1.2 §8 그대로 — MIN_NOTIONAL 확대 금지) |
| 동일 심볼 재제출 = 자기 미체결 있으면 신규 제출 금지 | `FreshTruth.own_open_order_symbols` (`clientOrderId` 가 `b0xc` 로 시작하는 미체결) |
| 일일 신규 = {자기 미체결 ∪ non-dust 포지션 ∪ 당 사이클 신규 제출} distinct | 위 둘의 합집합에서 시작해 파생 중 누적 |

- **상태 파일 없음.** 사이클 간에 carry 되는 값이 0개 — 그래서 stale 이 될 수도, 조용히
  비어버릴 수도 없다. `LaneAccountState.broker_truth` 는 기본값 없는 **필수** 필드다.
- **동일 심볼 규칙은 side 무관.** 리터럴이 side 가 아니라 심볼을 지명한다 — 미체결 매수가
  같은 심볼의 매도 rung 도 막는다. 레포 관행(「매수측만 캡」)보다 넓지만, 좁히면 계약 위반.
- **두 곳에서 검사한다**: 파생(행 단위 skip)과 `submit_planned`(디스패치 직전). 후자가
  이 결함의 실제 발생 지점이었다.
- 🔵 shadow 레인은 §4 각주대로 envelope 미적용이라 이 규칙도 적용되지 않는다. 애초에
  `place_derived_orders` 가 매 사이클 가상 장부를 **교체**하므로 적층이 불가능하다.
- 🔴 **realized P&L 입력이 없다.** 사이드카는 체결을 귀속 장부로 reconcile 하지 않으므로
  (계약 v1.3 ②, 매수측 표본 한정) `일 손실 5 USDT` kill 은 발화할 수 없다. 사라진 파일이
  0을 공급하던 시절과 동작은 같지만, 이제 산출물이 `realized_pnl_source` 로 그 사실을
  명시한다 — 측정된 0이 아니라 **입력 부재**다.

## 7. 사이드카 제출 전 체크리스트

`--confirm` 을 붙이기 전에 전부 확인한다.

- [ ] **`operator_contract.yaml` 이 B0-X 를 등재하고 있다** — 계좌맵 **기계판독 정본**
      (계약 v1.3 ①). `account_lanes.binance_demo = B0-X-crypto-sidecar` ·
      `reassignments.binance_demo.sidecar_scope = buy_side_fill_fidelity_sample_only` ·
      `strategy_order_exceptions ∋ b0x-adapter-orders-20260808`
- [ ] `mock/CLAUDE.md` §1 Binance Demo 행이 위와 **모순되지 않는다** (참조 표면 —
      충돌 시 YAML 이 이긴다. 🔴 산문에만 등재된 레인은 검사기가 강제할 수 없어서, 그
      상태로 나간 주문이 계약 v1.3 ① 의 취소 대상이 됐다)
- [ ] **CR-S1 TPR 이 재개되지 않았다** — 재개 시 TPR 우선권 (별개 계약)
- [ ] fresh truth 가 `contaminated: false` — 공유 Demo 계정에 다른 writer 의 미체결/**매도
      가능** 잔고 없음 (거래단위 미만 dust 는 예외 — §8-1)
- [ ] dry-run 사이클의 `planned` 를 눈으로 검토했다 (심볼 3종 · notional ≤ 10 USDT)
- [ ] 표가 fresh 하다 (`policy_table_age_seconds` 확인)

🔴 **오염 시 제출 차단은 코드가 강제한다** (`SidecarContaminated`). §4 종목별·동시 포지션
캡은 다른 writer 가 같은 장부를 쓰는 순간 의미를 잃기 때문이다 (ROB-993 §5 실패 모드).

## 8. 오염(CONTAMINATED) 판정

계약 §2-3 writer=1 은 두 방향으로 지켜진다:

- `writer_lock` — B0-X **프로세스 두 개**가 같은 레인을 동시에 파생하지 못한다
  (`flock`, non-blocking, 두 번째는 즉시 실패).
- `foreign_*` — B0-X 가 만들지 않은 **베뉴 상태**. `clientOrderId` 가 `b0xc-` 로 시작하지
  않는 미체결, 또는 **매도 가능한** base 자산 잔고가 있으면 그 사이클은 `CONTAMINATED` 이며
  관측은 계속하되 제출은 막힌다.

### 8-1. dust 예외 — 계약 v1.2 (2026-08-08)

잔고 판정 기준은 **"0 이 아니다"** 가 아니라 **"floor(LOT_SIZE) 후 매도 가능 수량 > 0"**
이다 (`sidecar.sellable_qty`). 최소 거래단위(`LOT_SIZE.minQty`) 미만 잔고는 floor 후 정확히
0 이 되어 **어떤 가격으로도 주문을 구성할 수 없다** — 청산도, 캡 우회도 불가능하다.

🔴 **MIN_NOTIONAL 로 확대하지 말 것.** 이 3종의 MIN_NOTIONAL 은 5 USDT 로 §4 주문 캡
(10 USDT) 과 같은 자릿수다. notional 기준 dust 판정은 **이 레인 자기 주문만 한 외부 재고를
통과시키는 게이트**가 된다. 규칙은 "팔 수 없으니까 dust" 이지 "작으니까 dust" 가 아니다.
`spot_demo.sizing.compute_close_qty` 와 `portfolio_overview_service` 는 같은 단어를 더 넓은
뜻(= MIN_NOTIONAL 미만)으로 쓰므로 **여기서 재사용 금지**.

경위: X-S(2026-08-08)가 ROB-298/ROB-307 왕복이 남긴 BTC `0.00000972`(minQty `0.00001`)·
SOL `0.00094600`(minQty `0.001`)을 실측했다. 청산이 물리적으로 불가능한데 오염 판정은
이를 봐준 적이 없어 `SIDECAR_ARMED=YES` 가 **영구 도달 불가**였다. 계약 v1.2 가 판정을
정합 수정했다. dust 는 숨겨지지 않는다 — 관측 기록의
`base_assets_with_nonzero_balance` 에 그대로 남고, `foreign_base_assets` 에서만 빠진다.

다른 전략의 미체결은 **취소하지 않는다** (mock/CLAUDE.md §4). kill 시에도 `b0xc-` 접두
주문만 취소한다.

### 8-2. 잔여 주문 취소 — 자기 주문만 (계약 §2-4)

kill switch 의 「잔여 주문 취소」와 계약 v1.3 ① 의 재등재 취소는 같은 도구를 쓴다.

```bash
# 스캔만 — 계좌 전체를 읽고 아무것도 취소하지 않는다. 항상 이걸 먼저 돌린다.
B0X_SIDECAR_ENABLED=true BINANCE_SPOT_DEMO_ENABLED=true \
  uv run python -m scripts.run_b0x_cancel --scan-only

# 실제 취소 (운영자 게이트)
... uv run python -m scripts.run_b0x_cancel --confirm
```

- 🔴 **읽기는 계좌 전체**(`get_all_open_orders`, symbol 생략)다. 심볼 3종만 훑으면
  네 번째 심볼에 얹힌 남의 주문을 못 보고 "깨끗하다" 고 보고하게 된다.
- 🔴 **취소 대상은 `b0xc-` 접두 주문뿐**이다. 그 외는 `NOT_MINE` 으로 출력만 하고 손대지
  않으며, **취소하는 플래그는 존재하지 않는다.** 귀속 불가(=clientOrderId 없음)도 남의
  것으로 친다(fail-closed).
- 같은 심볼에 남의 주문이 얹혀 있어도 안전하다 — 판정 축은 심볼이 아니라 접두다.
- foreign 주문이 하나라도 보이면 exit code 3 (실패가 아니라 **운영자가 봐야 할 사실**).

## 9. 알려진 경계 · 미해결

- **KRW→USDT 이식은 미검증이다** (`CROSS_QUOTE_RATIO_TRANSFER`). 표는 KRW 호가고 Binance 는
  USDT 호가라, 절대가격이 아니라 **무차원 비율(레벨/직전종가)** 만 이식해 Binance 기준가에
  적용하고 베뉴 tick 으로 스냅한다 (매수 내림·매도 올림). 이식 자체의 타당성은 이 실험이
  검증하지 않는다.
- **본선에 envelope 이 없다**는 것은 계약 §4 각주 그대로다. 결과적으로 첫 사이클이 표의
  자격 행 전부에 신규 진입할 수 있다. 이는 버그가 아니라 관측값이며, KR 열처럼 세션 캡을
  걸고 싶다면 **계약 §4 개정** 사안이지 어댑터 수정 사안이 아니다.
- **가상 시드 잔고 100만 KRW** (`SEED_CASH_KRW`) 는 계약 숫자가 아니라 가상 장부에 경계를
  주기 위한 고정값이다. 모든 산출물에 박제된다.
- 🔴 **사이드카 v1 은 체결을 귀속 장부에 reconcile 하지 않는다.** B0-X 장부가 비어 있으므로
  *매도 가능한* base 잔고는 전부 `foreign` 으로 읽히며, **B0-X 자기 체결이 만든 잔고도
  그렇다** (§8-1 dust 예외는 이를 완화하지 않는다 — 체결된 주문은 정의상 minQty 이상이다).
  결과적으로 첫 체결 다음 사이클은 `CONTAMINATED` 로 제출을 거부한다.
  과보수적이지만 방향은 옳다: 귀속 안 된 잔고를 "내 것 아님, 계속 진행" 으로 처리하면
  레인 자신의 재고가 §4 종목별·동시 포지션 캡을 우회하게 된다.
  체결 인식 reconcile 은 후속 작업이다.
- 🔴 **그래서 이 레인의 지위는 「매수측 체결충실도 표본」으로 축소됐다**
  (계약 v1.3 ②, 산출물에 `sidecar_scope=buy_side_fill_fidelity_sample_only` 로 박제).
  🔵 이전 판(「운영자 개입 1회당 **왕복** 1회」)은 부정확했다 — 매도측 R1/R2 는 v1
  자동 경로에서 **도달 불가**다. 이중 장벽이라 그렇다: ①파생 단계에서 B0-X 장부가 비어
  `no_position_to_sell` 로 스킵되고, ②설령 매도가 파생돼도 그 잔고가 sellable 이라
  `submit` 이 `SidecarContaminated` 로 막힌다. 이 레인이 실제로 산출하는 것은 **매수
  지정가 안착 + 자기정지**이며, **매도측 관측은 본선(Upbit shadow, 왕복 가능)의 몫**이다.
  귀속 기반 완화(자기 fill delta 분리 — ROB-993 선례)는 후속 **서명 후보**일 뿐 미승인.
- 스케줄러 등록 없음 (v1 수동 kickoff). 연속 clean 사이클 N회 후 자동화는 별도 운영자 결정.

## 10. 테스트

```bash
uv run pytest tests/scripts/b0x/ -q
```

포함: envelope 잠금(CLI/env 불변) · writer lock 다중프로세스 · 표 게이트 5경로 ·
파생 결정성 · B0 규칙 매핑 · 캡 · kill switch 발화 · 터치 규칙 · 사이드카 allowlist/
비율이식/오염 차단 · 금지 import 정적 가드(LLM·live 주문면·스케줄러).
