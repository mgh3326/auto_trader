# ROB-1391 — US 원 지정가 3세션 재제출 SHADOW (연구 전용, 주문 0)

ROB-1383 사전등록안 v1_review(운영자 승인 Q-1018-1 = A)의 실험 장치다. 실제·모의 주문 API 호출 0,
주문/제안/watch/레저 쓰기 경로 0, 브로커 클라이언트 import 0, 운영 DB 는 SELECT 만(READ ONLY 트랜잭션),
네트워크는 `data.alpaca.markets` GET 만. 런타임(`app/**`)·정책(`config/**`)·마이그레이션(`alembic/**`) 변경 0,
스케줄러 등록 0.

🔴 사전등록 원문은 비공개다. 레포에 들어오는 것은 규칙의 구현과 비식별 fixture 뿐이다. 원문 파일은
`seal --prereg` 로 **sha256 만** 봉인되고 복사되지 않는다. 원장·결과(`--ledger-dir`, `--out`)에는
주문번호·금액·손익·세션 정보가 들어가므로 **반드시 비공개 위치**에 둔다(레포 안 금지).

## 무엇을 하는가

| 단계 | 내용 |
|---|---|
| 부모 | 승인 이후 새로 생긴 US 상장 보통주/ETF 의 실제 매수 지정가 DAY 주문 중, 정규 세션 만료와 정수 잔량이 원장으로 증명된 것. D0 = 그 만료 세션 |
| C | 추가 재제출 0회. D0 까지의 실제 체결만 |
| T | D1–D3 에 같은 절대가 P_i·남은 정수 잔량만 가상 재제출(가격 추격·예산 추가 금지) |
| 체결(주) | 보수 모형: 제출 이후 1분봉 `low ≤ P−1틱` **그리고** 그 분의 유효 ask ≤ P, 수량 ≤ ⌊그 분 거래량의 1%⌋, 정수주, 잔량·예산 이내, 가격 = P |
| 체결(민감도) | 낙관 모형 `low ≤ P` (별도 열 T_opt) |
| M1 / M2 | M1 = Σ체결액/ΣB_i, M2 = Σ(D+20 순자산 − B_i)/ΣB_i (D0 후 20번째 세션 종가, 현금이자 0, 청산비용 차감) |
| 통계 | symbol 군집 bootstrap 10,000 (seed 1383), 귀무중심 단측 p 2개에 Holm(0.025/0.05), 날짜 블록 민감도 |
| 중단 | operational / policy_change / loss latch — 파일 원장에 영속, 자동 재개 없음 |

UNKNOWN(ask·거래량·시각 자료 부재, 빈/깨진 응답, 중복 분봉, redirect, 페이지 상한 초과)은 0 이나 미체결로
바꾸지 않는다. UNKNOWN 이 하나라도 있으면 주 지표는 `UNDETERMINED` 이고(분모 ΣB 는 배정 부모 전체로 고정),
complete-case 수치는 민감도로만 나온다. 수수료표가 봉인되지 않으면 M2·순성과 검정·Holm 은 출력하지 않는다.
latch 는 해당 `step`/`register` 이벤트 안에 함께 기록된다(한 줄 fsync — 중간 사망으로 stop 이 사라지지 않는다). 한 번 UNKNOWN 이 된
T 팔은 이후 세션을 시뮬레이션하지 않고(`prior_unknown`) 집계에 UNKNOWN 으로 남는다.

## 입력

- **부모**: `review.toss_live_order_ledger`(Toss live US), `review.live_order_ledger`(KIS live US, ROB-407),
  `review.kis_live_order_ledger` 의 `equity_us` 행(계약상 없어야 함 — 있으면 `unexpected_source_*` 로 제외 기록),
  `review.order_proposal_rungs`+`review.order_proposals`(승인 예산 B_i), `us_symbol_universe`(OTC·레버리지·보통주 판정).
  SQL 은 `db_source.py` 의 상수가 전부다.
- **시세**: Alpaca market data(`/v2/stocks/bars` 1Day·1Min `adjustment=raw`, `/v2/stocks/quotes`). feed 는 seal 때 봉인
  (기본 `sip`). 🔴 `iex` 는 단일 거래소 호가라 NBBO 가 아니고 1분 거래량도 IEX 분만이다 — ask 유효성과 1% 캡이 과소 추정된다.
  받은 원문 응답은 `ledger-dir/market_data/<sha256>.json` 에 저장되고 이벤트에 digest 가 남는다.
- **달력**: `exchange_calendars` XNYS(휴장·조기폐장), ET/UTC 둘 다 저장. 봉인 뒤 달력이 바뀌면 operational latch.
- **정책**: `app.services.trading_policy_service.policy_version_stamp()` — 매 실행 재대조, 다르면 `STOPPED_BY_POLICY_CHANGE`.

## 필요한 env (이름만)

| 이름 | 용도 |
|---|---|
| `<DSN 변수 이름>` (`--database-url-env` 로 이름을 넘김) | 운영 DB **읽기 전용** DSN. 값은 출력·로그·예외에 남지 않는다 |
| `ALPACA_DATA_API_KEY` / `ALPACA_DATA_API_SECRET` | 시세 전용 키(없으면 `ALPACA_PAPER_API_KEY`/`_SECRET` 이름도 조회). `ENV_FILE` 로 전용 파일을 줄 수 있으나 `.env.prod`·`.env.dev`·`.env` 는 거부된다 |

## 운영 실행 (desk, NCP) — 정확한 명령

전제: 이 PR 이 머지되고, 실행 위치가 **배포 중인 main 과 같은 SHA 의 깨끗한 git checkout** 이다(코드 SHA 가
봉인과 다르면 모든 명령이 거부된다). 출력 위치는 비공개(예: `$PRIVATE/rob1391/`).

### 0) T0 봉인 — 1회, 운영자 실행 배치 승인 뒤

```bash
cd <clean checkout at deployed main SHA>
uv run python -m research.rob1391_us_resubmit_shadow seal \
  --ledger-dir "$PRIVATE/rob1391/ledger" \
  --prereg "$PRIVATE/rob1391/prereg_v1_review.md" \
  --approval-ref "<operator-desk 태그 또는 hk:doc 근거, 예: Q-1018-1=A hk 1605>" \
  --deployed-policy-version "<get_trading_policy 응답 version>" \
  --deployed-policy-hash "<get_trading_policy 응답 content_hash>" \
  --feed sip \
  --fee-schedule "$PRIVATE/rob1391/fee_schedule.json" \
  --approve-deviation D1_exit_hold_to_d20 \
  --approve-deviation D2_resubmit_gate_subset \
  --approve-deviation D3_budget_fill_notional_only \
  --approve-deviation D4_policy_epoch_invariant_basis \
  --approve-deviation D5_accept_outside_regular_session \
  --approve-deviation D6_ledger_budget_basis \
  --deviation-ref "hk:doc decision/2026-10-18/rob1391-deviations"
```

- 운영자 결정 Q-1018-2 = A(D1–D6 전부 승인, at-integrator 릴레이 10-18). 🔴 seal 전에
  `handoffkeep doc get decision/2026-10-18/rob1391-deviations` 로 그 기록이 실재하고 D1–D6 을 담는지
  desk 가 대조한다 — 기록이 없거나 내용이 다르면 seal 하지 않는다(태그 없는 승인 문구는 증거가 아니다).

- 시작 세션 = 봉인 시각 **다음에 개장하는** XNYS 세션, 그로부터 연속 20 세션이 등록 창(사후 연장 불가).
- `--fee-schedule` 을 빼면 `net_metrics_status=PENDING_FEE_SCHEDULE` 로 봉인되어 순성과(M2) 검정은 끝까지
  보류된다(봉인 뒤 추가 불가). 형식은 `costs.py` docstring(닫힌 키, 문자열 소수, `fx=none_usd_ledger`).
- 🔴 **사전등록 문언과 다른 규칙은 전부 이름 붙은 deviation 이고, seal 은 승인 없이 거부한다.**
  필수(구현상 이미 적용 — 승인 없으면 seal 불가): `D1_exit_hold_to_d20`(라이브 exit 재생 불가 → D+20 보유),
  `D2_resubmit_gate_subset`(D1–D3 게이트 재검증 범위). 선택(기본은 원문 그대로의 엄격 규칙, 승인 시에만 완화):
  `D3_budget_fill_notional_only`(예산 캡에서 비용 제외), `D4_policy_epoch_invariant_basis`(주문 시점 정책 스탬프 없는
  부모 허용), `D5_accept_outside_regular_session`(정규장 밖 접수 DAY 주문 허용), `D6_ledger_budget_basis`(승인 rung
  없을 때 P×Q 를 B_i 로). 목록·문구는 manifest `deviations.catalog` 에 봉인된다.
- 기본(엄격) 등록 조건: 정규장 안 접수 · Toss 는 `time_in_force=DAY` 기록 · 승인 proposal rung(B_i) 존재 ·
  rung 의 주문 시점 정책 스탬프(`source_asof.auto_approved.policy_version`) 존재. 빠지면 제외 사유로 기록된다.

### 1) 매 US 세션 마감 뒤 — 하루 1회

```bash
uv run python -m research.rob1391_us_resubmit_shadow advance \
  --ledger-dir "$PRIVATE/rob1391/ledger" \
  --database-url-env <DSN 변수 이름> \
  --through <방금 마감된 US 세션 YYYY-MM-DD>
```

- `advance` 는 밀린 `(step S, register S)` 쌍을 세션 순서대로 처리한다(빠뜨린 날은 다음 실행이 순서대로 메운다).
  같은 세션 재실행은 무변경. 마감 + 30분 전에는 미래참조 가드가 거부한다.
- 권장 시각: 그 세션의 Toss/KIS reconcile 이 끝난 뒤(Toss US DAY 만료는 08:50 KST 경 기록됨) — 예: 10:00 KST.
- 종료 코드: `0` 정상/무변경, `2` 거부(순서·무결성·checkout 불일치), `3` D0 후보가 아직 reconcile 전 —
  reconcile 뒤 재실행(D1 마감이 지나면 그 후보는 `unreconciled_at_deadline` 으로 제외 기록), `4` 이번 실행에서
  중단 latch 발생.
- 수동 단계 실행: `step --session S` / `register --session S` (순서 강제는 같다).
- 상태: `uv run python -m research.rob1391_us_resubmit_shadow status --ledger-dir "$PRIVATE/rob1391/ledger"`

### 2) 평가·보고

```bash
uv run python -m research.rob1391_us_resubmit_shadow evaluate --ledger-dir "$PRIVATE/rob1391/ledger" --out "$PRIVATE/rob1391/out"
uv run python -m research.rob1391_us_resubmit_shadow report   --ledger-dir "$PRIVATE/rob1391/ledger" --out "$PRIVATE/rob1391/out"
```

- 수집 창 20 세션이 모두 등록되고 모든 부모가 D+20 에 도달하기 전에는 `INTERIM — 판정 없음` 이며 지표·CI·p 값을
  출력하지 않는다(중간값으로 조기 종료·튜닝 금지).
- 판정 문구: `진행 검토 후보`(부모 ≥30, symbol ≥10, Holm 둘 다 통과, 보수 ΔM1>0·ΔM2>0, M2_T>0, stop 없음,
  UNKNOWN 0, 수수료표 봉인) / `위험 신호로 종료`(stop 또는 M2_T<0) / 그 외 `결론 유보`. 인과 확증 표현은 쓰지 않는다.

### 기대 실행 빈도·기간

- 등록: 봉인 다음 세션부터 20 세션(약 4주). 추적: 마지막 D0 + 23 세션까지(D3 체결 + 20 보조 지표) — 총 약 9주.
- 매 세션 1회 `advance`. 🔴 **타이머/스케줄은 이 PR 에 없다.** 운영자 승인 후 desk 가 등록한다(만들지 않음).

## 중단(latch) 시 절차

`advance` 가 `STOP latched: <type> / <reason>` 와 종료 코드 4 를 내면:

1. 더 이상 신규 배정·재제출이 일어나지 않는다. **latch 를 지우는 명령은 없다**(이벤트 종류 자체가 없고, 줄을 지우면
   `events.head` 불일치로 원장이 거부된다).
2. `advance` 는 계속 돌려도 된다 — D+20 관측은 read-only 로 계속되어 중단된 장부도 끝까지 관측된다(검열 숨김 방지).
3. 원인을 desk → at-integrator → 운영자로 보고한다. 재개는 새 사전등록·새 epoch(새 `--ledger-dir`)로만 한다.

| latch | 원인 |
|---|---|
| `operational` | 쓰기 경로/브로커 모듈 로드(`write_path_reachable`), 원장 reconciliation 불일치(그 세션 재제출 전체 중단), 중복 parent, B_i 초과, 수량 불일치, 창 밖·마감 후 체결(미래참조), 상수·달력 drift |
| `policy_change` | 배포 정책 `{version, content_hash}` 가 봉인과 다름 → `STOPPED_BY_POLICY_CHANGE` |
| `loss` | T 가상 자산 ≤ E0×0.95(가상 USD 500 손실) 또는 단일 부모 순손실 ≤ −5%×B_i |

## 코드

`constants.py`(봉인 상수) · `us_calendar.py` · `ledger.py`(append-only 해시 체인 + head) · `manifest.py` ·
`db_source.py`(SELECT·READ ONLY) · `parents.py`(정규화·제외 사유·episode dedupe) · `market_data.py`(GET·host pin·UNKNOWN) ·
`fills.py` · `costs.py` · `valuation.py` · `engine.py`(register/step/latch) · `stats.py` · `report.py` · `cli.py`.

테스트: `tests/research/rob1391_us_resubmit_shadow/` (DB·네트워크 없음; `test_db_source_pg.py` 와 PG 읽기전용 테스트는
`AUTO_TRADER_TEST_DATABASE_URL` 로 일회용 PostgreSQL 을 줄 때만 돈다 — localhost:5432 를 가리키지 말 것).
