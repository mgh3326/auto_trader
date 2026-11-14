# #3796 — crypto 추세추종(바구니 ④) 사전등록과 Binance USD-M 선물 데모 러너 설계

hk 4647이 요구한 설계 문서이고, hk 3796의 step 1이다.
기준 SHA는 `e1db462ed82c5ed0a8eac660c116045fd9d45b5e`이고, 모든 `path:line` 인용은 그 SHA 기준이다. 코드·마이그레이션·레지스트리 파일·스케줄러는 이 문서의 범위가 아니다(설계 문서 — code not changed).

운영자 결정(원문은 hk doc):

- `decision/2026-11-10/at-new-strategy-go` §1의 5: 1주차 실험에 crypto 추세추종(Binance 선물 데모 롱·숏 + Upbit 소액 실전)을 포함한다.
- 같은 문서 수정 1: Binance 실전은 전송 계층이 데모 호스트만 허용하는 fail-closed라 지금 경로가 없고, 데모 통과 전략이 나오면 별도 결정으로 연다. 같은 심볼 충돌로 주문 거부가 주 3건 이상이면 데모 계정 추가를 다시 올린다.
- 같은 문서 §3: 이 결정은 주문·배포·스케줄 등록의 건별 승인을 대신하지 않고, 사전등록 통과 기준을 사후에 바꾸지 않는다(바꾸려면 새 manifest 버전).
- 전략 자문 `consult/2026-11-10/at-strategy-fable52` §11 표의 ④: BTC·ETH 등 4h~일봉 시계열 모멘텀, 하락 시 현금 또는 선물 숏, 사전 판단 "중간". H5가 같은 인프라였고 T0 FAIL은 러너 결함(전송 직후 대사 경합)이지 전략 판정이 아니다.

## 1. 목적과 범위

### 1.1 목적

구현 티켓이 다시 결정하지 않고 따라갈 수 있게 다음을 정한다.

1. **사전등록**: 가설, 신호 계열, 유니버스, 수평, 진입·청산 규칙, 비용·체결 모형, 지표, 중단 규칙, 레지스트리 항목(manifest 필드와 k) — 레지스트리 PR이 기계 작업이 되도록.
2. **러너**: 어느 기존 루프·어댑터를 확장하는지와 그 이유, identity/correlation, 노출·kill switch 상수와 출처, 롱·숏과 reduceOnly 청산, 공유 데모 계정에서의 전략 간 충돌(주 3건 규칙), 수동 전용 운영.
3. **Upbit 실전 부분**: 이 티켓 묶음에 들어가는지 여부와, 들어간다면 제안 경로·슬리브·한도의 조건.
4. **인바리언트 표**와 **티켓 분해**(tier·선행·Acceptance 요지, blocking 표시).
5. **운영자에게 물을 것**: 번호 매김, 각 항목에 권고안. 금액·한도는 운영자 몫이다.

### 1.2 범위 밖

코드, manifest·registry 파일 자체, 마이그레이션, 스케줄러 등록, Binance live 호스트 허용, 기존 경로(ROB-993 루프, H5, ROB-1474)의 하드 인바리언트·게이트 변경.

### 1.3 지키는 하드 룰

| 규칙 | 이 설계의 처리 |
|---|---|
| 주문 게이트 완화 금지(AGENTS 하드룰 4) | 새 러너는 env 게이트(기본 off) + 호출마다 `confirm`의 이중 게이트를 새 이름으로 둔다. 기존 상수(레버리지 1x, notional cap, 동시 포지션 상한)는 어느 경로에서도 올리지 않는다. 새 경로의 상수는 새 locked 상수로 정의하고 값은 §6에서 운영자가 정한다. |
| 레저 쓰기는 서비스 경유(하드룰 5) | `binance_demo_order_ledger`의 모든 쓰기는 `BinanceDemoLedgerService`만 쓴다(`docs/agent-contracts/broker-and-ledger-contracts.md:85-95`). fill 기록은 브로커 증거 우선 — `status=NEW`는 bounded poll → positionRisk 순으로만 해소한다(같은 파일 :122-124). |
| 스케줄러 등록 금지(하드룰 6) | scheduleless CLI만. `--loop`도 운영자 소유 foreground 프로세스다(ROB-993과 같은 규칙, 같은 파일 :192). |
| 코어 진입 후 `internal_error` 재시도 금지(하드룰 11) | 전송 결과가 불명이면 `uncertain`/`anomaly`로 남기고 키를 재사용하지 않는다. ROB-1474의 "예외·타임아웃·조회 부재는 재전송 권한을 주지 않는다"를 그대로 따른다(`app/services/mock_strategy/crypto_runner.py:121-128`). |
| 심볼 형식(하드룰 8) | Binance 심볼(`BTCUSDT`)은 venue 기본 형식 그대로 쓰고 DB 기준 `.` 구분 변환은 `app/core/symbol.py` 경유만. |
| 사전등록(ROB-1429) | 테스트셋을 보기 전에 manifest를 등록한다. sealed 뒤에는 pending 값이 없고, 기준 변경은 새 version이다(`research/experiments/README.md:38-52`). |

### 1.4 용어

- **TSMOM**: time-series momentum — 자기 자신의 과거 수익 부호로 방향을 정하는 시계열 모멘텀(종목 간 상대 순위가 아니다).
- **에피소드**: 진입 예약 → 전송 → 청산 → 대사까지 한 포지션의 생애. 러너 상태는 `review` 스키마의 전용 테이블(H5의 `binance_h5_signals`/`binance_h5_intents` 대응물)에 둔다.
- **conflict reject**: 같은 데모 계정에서 다른 전략(ROB-1474 숏, 수동 스모크)의 포지션·미체결 때문에 러너가 진입을 차단하거나 브로커가 거부하는 사건.
- **k**: 테스트셋 열람 누적 횟수. `registry.json`의 `test_looks` 길이(`research/experiments/README.md:62-68`).

## 2. 현재 상태

### 2.1 재사용할 인프라(사실, path:line)

| 재료 | 위치 | 사실 |
|---|---|---|
| 선물 데모 클라이언트 | `app/services/brokers/binance/futures_demo/execution_client.py:97-106` | 기본 `https://demo-fapi.binance.com`. mutation은 `submit_order(..., confirm=True)`뿐이고 기본은 dry-run(`execution_client.py:472-559`) |
| 호스트 fail-closed | `futures_demo/host_allowlist.py:23-47` | `FUTURES_DEMO_HOSTS={demo-fapi.binance.com}` 엄격 일치, live·testnet은 `BinanceLiveHostBlocked` |
| env 게이트 | `futures_demo/execution_client.py:213-244` | `BINANCE_FUTURES_DEMO_ENABLED` 미설정이면 `BinanceFuturesDemoDisabled` |
| 레버리지·포지션 모드 | `execution_client.py:1007-1073`, `:335-371` | `set_leverage`는 `!=1`을 POST 전에 거부. one-way(`BOTH`)만, hedge 사전 차단 |
| reduceOnly | `execution_client.py:416-419` | close 주문에만 `reduceOnly=true` 전송 |
| 계정 전체 읽기 | `execution_client.py:720`, `:852` | `get_all_open_orders()`/`get_all_positions()` — 공유 계정 충돌 감지용(ROB-993 R3) |
| 심볼 allowlist(ROB-993/H5 경로) | `futures_demo/sizing.py:23-65` | `XRPUSDT` 기본 + `DOGEUSDT`·`SOLUSDT` fallback, `BTCUSDT`는 excluded(leg cap $10 < MIN_NOTIONAL $50) — **이 상수는 ROB-993 경로 것이다** |
| 데모 원장 | `app/services/brokers/binance/demo/ledger/service.py:70` | `BinanceDemoLedgerService`가 `binance_demo_order_ledger` 유일 writer. 상태 `planned→previewed→validated→submitted→filled→closed→reconciled` + `cancelled`/`anomaly`(`service.py:56-67`) |
| 전송 전 예약 | `service.py:300-383` | `reserve_root_planned`/`commit_planned_claim` — advisory lock + cap + 멱등(`RESERVATION_*`, `repository.py:53-55`) |
| 전략 플러그 인터페이스 | `app/services/brokers/binance/demo_strategy_loop/strategy.py:46-64` | `StrategyPlugin`: `evaluate(bars_4h_multi_symbol, *, decision_ts) -> Signal|None` |
| 완성 4h bar 빌더 | `research/nautilus_scalping/rob974_features.py`의 `build_complete_4h` | UTC 경계, 결측=NO_SIGNAL, forward-fill 금지(계약 `broker-and-ledger-contracts.md:160-165`) |
| kill switch 패턴 | `demo_strategy_loop/kill_switch.py:37-69` | `LOCKED_LIMITS` + `assert_kill_switch_limits_locked` — CLI 덮어쓰기 불가 상수의 검증 관용구 |
| correlation→forecast | `demo_strategy_loop/correlation.py`, `orchestrator.py:121-171` | `correlation_id` → ledger `extra_metadata` → `forecast_save` 학습 척추 |
| H5 수동 어댑터 | `app/services/brokers/binance/h5/`, `scripts/binance_h5_demo.py` | 별도 identity·상태 테이블·전용 게이트로 같은 클라이언트·원장을 재사용한 선례(계약 `:194-262`). `--manage-only` 관리 전용 틱(hk 3644) |
| ROB-1474 수량 수정(hk 3768) | `app/services/brokers/binance/demo/quantity.py:8-25` | `venue_order_quantity()`는 trailing zero만 제거 — venue 클라이언트의 `format(qty,"f")`는 증거용으로 유지 |
| 슬리브(ROB-1470) | `app/models/strategy_sleeves.py:59-157` | `review.strategy_sleeves` + append-only `strategy_sleeve_versions`(budget_cap·loss_limit·prereg, live는 prereg 필수 CHECK). `("upbit","crypto")` 시장 쌍 지원(:34-41). 슬리브 주문은 제안 경로만(`approval-and-shadow-contracts.md:330`) |
| 실험 레지스트리(ROB-1429) | `research/experiments/README.md:18-79` | manifest `experiment-manifest/v1`, k=`len(test_looks)`, draft→sealed→running→closed |

### 2.2 공유 데모 계정 상황

- 자문 §13(`consult/2026-11-10/at-strategy-fable52`): Binance 선물 데모 계정 1개에 **④ 추세추종과 ROB-1474 숏이 같이** 배정돼 있다. 같은 심볼 반대 방향 충돌이 생긴다.
- ROB-993 루프는 이미 공유 계정을 전제로 계정 전체 flat 게이트를 reservation 직후와 submit 직전 두 번 확인한다(`broker-and-ledger-contracts.md:176-187`).
- 결정 수정 1의 해소 조건: **같은 심볼 충돌로 주 3건 이상 거부** → 데모 계정 추가를 다시 올린다. 지금은 계정을 추가하지 않는다.

### 2.3 H5 T0 실패의 교훈

자문 §11 표 ④의 기록: H5의 T0 FAIL 원인은 "전송 직후 대사 경합"이라는 러너 결함이지 전략 판정이 아니다. 이 저장소의 확정 계약으로는 제출 후 fill 해소가 `submitted → (bounded GET poll → positionRisk) → filled → closed → reconciled`이고, 계정 flat + 미체결 0을 모두 증명하기 전에 `closed`로 전이하지 못한다(`broker-and-ledger-contracts.md:122-124`, `execution.py`의 `_close_root_if_proven` 선례 `h5/executor.py:262-292`). 새 러너는 이 순서를 그대로 물려받는다 — **전송 직후 상태 읽기 경합을 "성공"으로 서두르지 않는 것**이 H5와의 설계상 차이다.

## 3. 사전등록 설계

### 3.1 레지스트리 항목(권고안)

| 필드 | 값(권고) | 근거 |
|---|---|---|
| `hypothesis_id` | `ROB-3796` | registry는 `ROB-*`/`H-*` 혼용(`research/experiments/registry.json`). 상위 hk 3796에 대응 |
| manifest 파일 | `research/experiments/manifests/rob-3796.json` | 기존 명명 규칙 |
| `manifest_format` | `experiment-manifest/v1` | README :18-24 |
| `version` | `1` (초기 `draft`) | ④의 규칙이 이 문서에서 처음 고정되므로 draft로 등록하고, 러너 코드 SHA·비용표를 확정한 뒤 seal |
| `markets` | `["crypto"]` | — |
| `calendar` | `crypto → "24/7"` | Upbit·Binance 모두 24시간 |
| `cumulative_k` | `{prior_k: 0}` | 새 가설이라 선행 열람 없음 |
| `splits.test` | `{kind: "prospective_seal", …}` | 과거 데이터 백테스트는 탐색으로만 쓰고(§3.5), 판정은 전향(prospective) 봉인 구간에서 한다. `folds`는 빈 목록(prospective 전용) |
| `metrics.family` | 이 manifest 하나의 단일 가설(단일 primary). 다중 비교 보정 없음 | 후보 정책은 대조군 포함이라도 primary는 정해진 하나 |

`hashes`에는 사전등록 문서(이 문서 또는 별도 카드), 러너 코드 SHA, 정책 `{version, content_hash}`, 비용표를 채운다. draft 단계에서는 `{"pending": "…"}`가 허용되지만 seal에는 전부 확정값이어야 한다(README :47-52).

### 3.2 가설·신호·규칙(사전등록 카드)

한 장의 카드 — 가설, 규칙, 예산, 중단 조건, 지표, 종료일(자문 §11).

- **가설(H)**: Binance USD-M 무기한 선물의 4h~일봉 TSMOM 신호가 비용 차감 후에도 현금 대비 양의 기대수익을 낸다. 하락 국면에서는 현금(포지션 없음) 또는 선물 숏이 롱 전용 대비 낙폭을 줄인다.
- **신호 계열**: 시계열 모멘텀. 후보 정책은 봉인 전에 하나로 고정한다.
  - 권고 신호: 심볼별 완성 4h bar 집계 후 일봉 등가 수익률 `r = close_t / close_{t-L} - 1`의 부호. `L`은 후보 집합 {20, 60, 120일}에서 **탐색 구간(§3.5) 1회**로 고르고 manifest에 고정한다.
  - 변동성 스케일: 목표 연변동성 `σ*` 대비 실현 변동성(예: 20일 EWMA) 비율로 포지션 크기를 조절한다. 상수는 §6 Q2.
  - 방향: `r > 0` → 롱, `r < 0` → 숏(데모) 또는 현금(Upbit 실전 부분, §5).
- **유니버스**: `{BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT}` 권고. 단, ROB-993/H5 경로의 `FUTURES_DEMO_EXCLUDED_SYMBOLS={BTCUSDT}`·leg cap $10(`futures_demo/sizing.py:23-29`)은 **그 경로의 상수**다. 이 러너는 자체 allowlist·자체 notional cap을 새 locked 상수로 둔다(§4.3, §6 Q1). `DOGEUSDT`는 유동성·대표성 이유로 제외를 권고.
- **진입**: 완성 4h bar 마감 시 평가(ROB-993과 같은 synchronized complete-bar 게이트, 결측=NO_SIGNAL). 신호 방향이 현 포지션과 다르면 전량 청산 후 신규 진입(또는 현금). 같은 방향 유지 시 아무 것도 하지 않는다.
- **추가매수**: 없다(에피소드당 1진입 1청산).
- **청산**: (a) 신호 반전, (b) hard stop — 진입가 대비 고정 %, (c) 최대 보유 시간(예: 30일). 모든 청산은 `reduce_only=True`(`execution_client.py:416-419`). hard stop도 H5처럼 4h 마감 사이 1m 극값으로 감시할지는 §6 Q3(구현 비용 대 효과).
- **비용·체결 모형**: taker fee는 H5의 고정값 `TAKER_FEE_RATE=0.0005`(`h5/constants.py:5`)를 출발점으로 쓰고, 실제 fill의 `fee_usdt`로 원장 실측으로 대체한다(레저의 `_IMMUTABLE_FILL_METADATA_KEYS`에 `fee_usdt` 포함, `service.py:52-54`). 체결 모형은 탐색 평가에서 **conservative through**(터치≠체결)를 기본으로 한다.
- **지표**: primary = 비용 차감 후 순수익률(동일 명목 대비 현금). secondary = 최대낙폭, 샤프, 체결률(신호 대비 실제 진입), conflict reject 수.
- **중단 규칙**: (i) 봉인 구간 종료 시 primary가 비용 포함 0 이하 → 종료. (ii) kill switch 발동 조건 충족(§4.4) 시 해당 UTC일 신규 진입 중지. (iii) conflict reject가 주 3건 → 계정 분리 결정 전까지 신규 진입 보류(결정 수정 1). (iv) 월 손실 한도는 실험 예산의 그것(결정 §1의 2c, 월 100만)에 연동 — 데모 계정이라 금전 손실은 없지만 실전 승격 판정에는 같은 한도를 적용한 것으로 간주한다.

### 3.3 사전등록이 먼저인 이유

결정 §3: "사전등록 통과 기준을 사후에 바꾸지 않는다. 바꾸려면 새 manifest 버전." 따라서 순서는 **manifest draft 등록 → 러너 구현 → seal(코드 SHA·비용표 확정) → 전향 봉인 구간**이다. draft에서 seal로 갈 때 pending을 채우는 것은 허용되지만, seal 뒤의 규칙 변경은 `version+1`과 `previous_manifest_sha256` 이동이다(README :62-68).

### 3.4 탐색(backtest)과 봉인의 분리

- 과거 4h/일봉으로 신호 후보를 비교하는 것은 `tuning_budget` 안의 탐색이며 `test_looks`를 소비하지 않는다 — **봉인 `test` 구간을 건드리지 않는 한도에서**. 탐색에 쓴 구간은 manifest의 `splits`에서 validation으로 명시하고 test와 겹치지 않게 한다(`purge_sessions ≥ 최장 레이블 수평`, `gap_sessions ≥ max(5, 수평)`, README :38).
- 데이터 소스: Binance 공개 kline 아카이브(연구용 다운로드)를 권고. 데모 호스트의 kline은 실제와 다른 유동성이라 성과 추정에 쓰지 않는다 — **[모름]**: 데모 kline이 실거래 kline과 같은지 이 저장소에서 확인 못함. 탐색 데이터 소스는 티켓 CT-1에서 확정한다.
- 보고서는 `research/experiments/reports/`의 `.md`로, `Experiment-Manifest-Sha256` 마커와 `Experiment-Test-Look: no`(탐색)를 단다.

### 3.5 데모 전향 판정(봉인 구간)

- `test.kind = prospective_seal`: seal 시점부터 고정 기간(권고 8주 — 4주는 추세 신호 회전이 너무 적다, §6 Q5) 동안 러너를 수동 가동하고 원장 행으로 채점한다.
- `exposure_audit`: 채점 시 신호 대비 실제 포지션 노출 일치를 원장·브로커 읽기로 감사.
- 통과·중단은 §3.2 중단 규칙 (i)로.

## 4. 러너 설계

### 4.1 형태 선택: H5형 별도 어댑터 + StrategyPlugin 재사용

두 후보:

| 후보 | 장점 | 문제 |
|---|---|---|
| A. ROB-993 루프에 플러그인으로 탑재 | 인프라 전부 재사용, 변경 최소 | ROB-993의 locked 인바리언트가 추세추종과 안 맞는다: 동시 포지션 **1**(`kill_switch.py:37-40`), leg notional **[6,10] USDT**(`sizing.py:52-53`), 심볼 allowlist에 BTC 없음(`futures_demo/sizing.py:23-29`). 이 상수들은 CLI로도 못 바꾸는 잠금 상수고, 바꾸는 것은 하드룰 4 위반(기존 경로 인바리언트 완화) |
| B. H5형 별도 어댑터(`app/services/brokers/binance/<name>/` + 전용 CLI·상태 테이블·게이트) | 자체 locked 상수(유니버스·cap·kill switch)를 자기 identity 아래 둘 수 있다. H5가 같은 클라이언트·원장·플러그 인터페이스로 이 형태를 증명했다(계약 :194-262) | 새 코드면이 늘지만, 재사용 경계(클라이언트·원장 서비스·bar 빌더·correlation 패턴)는 그대로 |

**권고: B.** `StrategyPlugin` 인터페이스(`demo_strategy_loop/strategy.py:46-64`)와 `Signal` 데이터클래스는 재사용하고, 오케스트레이션·상태·상수는 새 패키지에 둔다. 이유: 추세추종은 바스켓 다중 포지션·긴 보유·BTC 포함 유니버스가 필요한데, 이는 ROB-993 경로의 잠금 상수를 "확장"이 아니라 "변경"해야만 들어간다.

### 4.2 패키지·CLI·identity(권고안)

| 항목 | 권고 |
|---|---|
| 패키지 | `app/services/brokers/binance/trend/` — `strategy.py`(`TrendStrategy(StrategyPlugin)`), `executor.py`, `state.py`(전용 `review` 테이블 서비스), `constants.py`(locked 상수), `alerting.py`(H5 패턴, default off) |
| identity | `IDENTITY = "TREND-TSMOM-v1"`(H5의 `H5-LS-ENV-v1` 대응, `h5/strategy.py:21-23`) |
| CLI | `scripts/binance_trend_demo.py` — `--once`/`--loop`/`--manage-only`/`--readiness`, per-call `--confirm-demo`. H5 CLI(`scripts/binance_h5_demo.py`)와 같은 형태, 스케줄러 없음 |
| env 게이트 | `BINANCE_TREND_DEMO_ENABLED`(기본 false) + 기존 `BINANCE_FUTURES_DEMO_ENABLED` + `confirm` 이중 게이트(H5의 `_guard` 패턴, `h5/executor.py:101-112`) |
| correlation | signal key = `symbol\|KST분\|side\|decimal close text`, correlation id = `binance-trend:<sha256 앞 24hex>`(H5 런북 패턴) → ledger `extra_metadata` → `forecast_save` |
| 상태 테이블 | `review.binance_trend_signals`/`review.binance_trend_intents` — H5의 signals/intents 쌍 대응, 전용 서비스만 advisory lock으로 예약(계약 :226-232) |
| `--manage-only` | 미해소 intent 정리·uncertain 차단·보유 포지션 관리만, 신규 진입 불가(hk 3644 패턴, 계약 :238-243) |
| 알림 | `BINANCE_TREND_ALERT_ENABLED` 정확히 `true`일 때만 `stopped`/`error`/`heartbeat_missed`(H5 alerting 패턴, 기본 off) |

### 4.3 노출·유니버스 상수(새 locked 상수, 값은 운영자)

| 상수 | 권고값 | 출처·비고 |
|---|---|---|
| `TREND_UNIVERSE` | `{BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT}` | 자문 §11 ④ "BTC·ETH 등". 새 allowlist이지 기존 `FUTURES_DEMO_*` 상수 변경이 아니다 |
| `TREND_LEG_NOTIONAL_CAP_USDT` | **운영자 결정(Q1)** — 권고 `$60` | BTCUSDT MIN_NOTIONAL $50를 넘어야 해서 하한은 $50. 데모 지급분 내에서 |
| `TREND_MAX_CONCURRENT_POSITIONS` | 권고 `4`(유니버스 크기) | ROB-993의 cap=1은 그 경로 것 |
| `TREND_MAX_GROSS_NOTIONAL_USDT` | 권고 `$240`(=4×$60) | 1x 레버리지라 gross가 곧 최대 증거금 |
| 레버리지·포지션 모드 | 1x isolated·one-way | 클라이언트가 강제(`execution_client.py:974-1073`). 설정 mutation은 ROB-1474 선물 배치의 확인 후 설정 경로(계약 :151-156)를 따를지, H5처럼 readback 요구만 할지는 §6 Q4 |

모든 상수는 `LOCKED` 검증 관용구(`kill_switch.py:49-69` 패턴)로 CLI 덮어쓰기를 막는다.

### 4.4 kill switch·청산 순서

| 항목 | 권고 |
|---|---|
| kill switch | (a) 연속 hard-stop 청산 N회/UTC일 → 당일 신규 진입 중지(N 권고 2, ROB-993과 동일 값·자기 identity로 스코프). (b) 계정 gross가 cap 초과 → 진입 차단(관리는 계속). (c) conflict reject 주 3건 → 신규 진입 보류 + 알림 |
| 청산 우선순위 | hard stop → 신호 반전 → time exit — H5의 hard stop/completed-bar close/time/TP 순서(계약 :233-237)에서 TP는 추세추종에 맞지 않아 뺀다. 모든 청산 reduceOnly, 브로커 증명 잔량까지만 |
| uncertain | 전송 결과 불명은 `uncertain`으로 보존, broker order id/client id와 전계정 position 증거로만 해소(계약 :228-231). 키 재사용·재전송 없음 |
| 상태머신 | 원장 `planned→…→reconciled` 그대로. `anomaly`는 terminal. `status=NEW`는 bounded poll→positionRisk 순으로만 해소(계약 :122-124) — **이것이 H5 T0 경합의 확정된 방지책이다** |

### 4.5 공유 계정 충돌 감지와 주 3건 규칙

- **감지**: 진입 예약 직후·submit 직전에 `get_all_positions()`·`get_all_open_orders()`로 계정 전체를 읽는다(ROB-993 R3 게이트와 동일, `execution_client.py:720`·`:852`). 자기 신호 심볼에 자기 identity가 아닌 포지션·미체결이 있으면 진입을 차단하고 `conflict_blocked`를 기록한다.
- **집계**: conflict는 `conflict_blocked`(자체 차단)와 `conflict_rejected`(브로커 거부, 예: 반대 방향 대기 주문 제약)로 나눠 심볼·상대 전략 추정·시각을 상태 테이블에 남긴다.
- **주 3건 규칙**: UTC 주간 합계가 3 이상이면 신규 진입을 멈추고 알림을 보낸다 — 계정 분리는 운영자 결정(수정 1의 조건 3). 해소는 운영자가 계정을 추가하거나 상대 전략 배정을 바꿀 때까지다.

### 4.6 수동 전용 운영

- TaskIQ/cron/Prefect 등록 없음(하드룰 6). `--loop`도 foreground.
- NCP 일회성 컨테이너 운영 시 데모 플래그는 `docker run -e`에만(H5 플레이북 패턴, 계약 :249-262), 공유 env 파일 편집 금지.
- 시작 전 truth gate(H5의 `binance_h5_truth_gate.py` 대응물: 서명 GET + SELECT만, 주문 도달 불가를 AST 테스트로 고정)를 권고.

## 5. Upbit 실전 부분 — 이 티켓 묶음에 넣는가

**권고: 이번 묶음의 범위 밖으로 두고, 데모 판정 뒤 별도 티켓으로 연다.** 이유:

1. Upbit에는 모의가 없어 실험이 곧 소액 실전이다(자문 §11 "Upbit에는 모의가 없다"). 실전 지출은 실험 예산 195만·월 손실 한도 100만(결정 §1의 2a·2c)의 운영자 한도 안에서 움직이고, 그 한도 배정은 운영자 몫이다.
2. 실전 경로는 슬리브(`("upbit","crypto")` 지원, `strategy_sleeves.py:34-41`) + **제안 경로만**이다 — 슬리브 주문은 `order_proposals`를 통해서만 나간다(`approval-and-shadow-contracts.md:330`). live 슬리브는 prereg 필드가 CHECK로 필수(`live_requires_prereg`, `strategy_sleeves.py:92-157`)라 **사전등록이 먼저 sealed·running 상태**여야 한다.
3. Upbit는 현물이라 숏이 없다 — "하락 시 현금" 절반만 검증한다. 데모의 롱·숏 결과가 나오기 전에 실전 카드를 만들면 판정 논리가 어긋난다.
4. 결정 §3: 실전 카나리는 위임장 버전을 운영자가 승인한 뒤다.

따라서 티켓 분해(§7)에는 Upbit 실전 티켓을 넣지 않고, Q6로 운영자에게 "데모 봉인 구간 통과 후 별도 설계"를 확인받는다. 만약 운영자가 같은 묶음을 원하면 CT-6(꼬리 티켓)으로 추가하되, 내용은 슬리브 생성 + 위임장 v1 + 제안 경로 배선이고 한도 숫자는 전부 운영자 입력이다.

## 6. 결정이 필요한 점

각 항목은 코드와 기존 결정만으로 정할 수 없다. 권고안은 이 문서의 기본 가정이다.

1. **leg notional cap과 총 노출.** BTCUSDT MIN_NOTIONAL($50) 때문에 최소 $50. **권고: leg $60·동시 4·gross $240.** 데모 지급분 안이고 BTC를 포함한 유니버스를 그대로 쓸 수 있다. 올리면 유니버스를 넓힐 수 있으나 데모 판정의 해상도는 그대로다.
2. **변동성 스케일 상수.** 목표 연변동성 `σ*`와 변동성 추정 창. **권고: `σ*=60%`·EWMA 20일**, 포지션 notional = `cap × min(1, σ*/σ̂)`로 cap 이하에서만 조절(cap을 넘는 스케일업 없음).
3. **hard stop 감시 해상도.** 4h 마감 사이에 1m 극값으로 감시(H5 방식)할지, 4h 종가로만 감시할지. **권고: 1m 감시** — H5가 이미 collector를 증명했고, 추세추종의 손절은 지연에 민감하다. 단 1m fetch 실패 시 정책(그 틱은 4h 판정만)을 manifest에 적는다.
4. **isolated·1x 설정 권한.** H5는 readback 요구만(설정 mutation 없음, 계약 :233-237)이고 ROB-1474 선물 배치는 확인 후 설정한다(계약 :151-156). **권고: H5 방식(readback 요구)** — 계정 설정을 여러 전략이 바꾸는 것은 공유 계정에서 충돌 원인이다.
5. **봉인 구간 길이.** **권고 8주.** 4주는 4h~일봉 신호의 표본이 너무 적고, 자문의 12-07 판정과 무관하게 이 가설의 종료일은 manifest에 적는다.
6. **Upbit 실전 부분의 소속.** **권고: 이 티켓 묶음에서 제외, 데모 판정 뒤 별도 티켓**(§5).
7. **탐색(backtest) 데이터 소스와 구간.** **권고: Binance 공개 kline 아카이브, 최근 3년, validation은 최근 6개월을 남기지 않고 전부 탐색에 쓰되 test는 prospective라 겹침 없음.** 데모 kline이 실거래와 같은지는 미확인이라 쓰지 않는다.
8. **대조군.** **권고: 같은 자본의 (a) 현금, (b) BTC 롱 온리 동일 규칙** — 롱·숏의 숏 기여분을 분리해서 본다. candidate_policies에 둘 다 등록하되 primary 비교는 (a)다.

## 7. 티켓 분해

한 티켓은 PR 하나다. tier 정의는 4154 문서를 따른다(T1=문서·순수 함수·읽기 테스트, T2=주문 경로 밖의 추가형 코드·마이그레이션·reader, T3=주문·원장 쓰기 경로). 모든 구현 PR은 분야 계약(`docs/agent-contracts/`)을 같은 PR에서 갱신한다. **굵은 티켓이 blocking path**다.

| ID | 티켓 | tier | 선행 | 내용·Acceptance 요지 |
|---|---|---|---|---|
| **CT-0** | 이 설계 문서 병합 | T1 | 없음 | `docs/design/3796-crypto-trend-demo.md`. 구현 티켓 입력 |
| **CT-1** | manifest draft + registry 등록 | T1 | CT-0, 운영자 Q1~Q8 답 | `research/experiments/manifests/rob-3796.json`(draft, 코드 SHA·비용표는 `pending` 허용) + `registry.json` 항목(`ROB-3796`, k=0) + 탐색 데이터 소스 결정 기록. CI lint `experiment_registry_lint` 통과 |
| **CT-2** | 탐색 backtest + 탐색 보고서 | T1 | CT-1 | kline 아카이브로 후보 L·σ* 비교 1회(tuning_budget 안), `reports/`에 `Experiment-Test-Look: no` 보고서 — 신호 상수를 manifest에 채울 근거를 만든다. seal 전이므로 test 구간을 읽지 않는다 |
| **CT-3** | trend 패키지 뼈대 + 상태 테이블 | T2 | CT-0 | `app/services/brokers/binance/trend/` 빈 뼈대가 아닌 최소 구현: `IDENTITY`, locked 상수 + 검증, `review.binance_trend_*` 마이그레이션(추가형, GRANT 조건부), 상태 서비스(advisory lock 예약), 분야 계약 절 추가. 주문 전송 경로 없음 |
| **CT-4** | `TrendStrategy` 플러그인 + executor(진입·청산 왕복) | T3 | CT-3 | `StrategyPlugin` 구현, executor가 `BinanceDemoLedgerService`·`BinanceFuturesDemoExecutionClient`로 예약→전송→bounded poll→reduceOnly 청산→대사 왕복. 계정 전체 flat 게이트 2회, uncertain/anomaly 계약대로. 게이트 `BINANCE_TREND_DEMO_ENABLED` 기본 off + `confirm`. 단위 테스트: 상태머신·echo 불일치·uncertain 비재전송 |
| **CT-5** | CLI·kill switch·알림·manage-only + manifest seal | T2 | CT-2, CT-4 | `scripts/binance_trend_demo.py`(`--once/--loop/--manage-only/--readiness`), kill switch(연속 SL·gross cap·conflict 주 3건), 알림(기본 off), truth gate 스크립트. CT-2 탐색으로 확정한 상수·코드 SHA·비용표로 manifest `sealed` + registry `running` 전이 — **전향 봉인 구간은 이 PR 병합 시점부터다** |
| CT-6 | (조건부) Upbit 실전 부분 | T3 | CT-5, 봉인 구간 통과, 운영자 위임장 승인 | `("upbit","crypto")` 슬리브 + 제안 경로 배선 + 위임장 v1. 한도는 운영자 입력. §5 권고대로면 이 티켓은 별도 설계 문서로 대체 |
| CT-7 | (조건부) 두 번째 데모 계정 분리 | T2 | conflict reject 주 3건 | 자격증명·`EXPECTED_FINGERPRINT` 분리. 발동 조건은 운영자 결정(수정 1) |

의존 순서: **CT-0 → CT-1 → CT-2** 와 **CT-0 → CT-3 → CT-4** 는 병렬 가능, 둘이 모두 끝나야 **CT-5**(seal = 봉인 시작)다. CT-4·CT-5가 브로커 송신 표면을 만들어 blocking path다. CT-6·CT-7은 조건부 꼬리다.

## 8. 알았지만 건드리지 않은 것

- ROB-993 루프·H5·ROB-1474 경로의 잠금 상수·allowlist·게이트: 변경은 하드룰 4라 설계에도 넣지 않았다. 새 경로는 자기 상수를 새로 정의한다.
- 펀딩·베이시스 캐리(바구니 ⑧): 같은 데모 계정·같은 인프라 후보라 헷갈리기 쉽지만 별도 가설이다 — 2주차 항목이고 이 설계 범위 밖.
- 호가 기록(Upbit)·H2b: 바구니 ②의 일이다. 추세 러너는 호가 데이터를 쓰지 않는다.
- 데모 계정 내 ROB-1474 숏과의 공존: §4.5의 충돌 감지·주 3건 규칙만 정했다. 상대 전략의 스케줄·유니버스 조정은 운영자 몫.
- Binance 실전 경로: 전송 계층이 데모 호스트만 허용하는 fail-closed이며 이 설계는 그것을 바꾸지 않는다. 실전 개방은 데모 통과 뒤 별도 결정.
