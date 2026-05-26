# ROB-316 — NautilusTrader 도입 Spike Plan

**Status:** Plan (미실행)
**Issue:** [ROB-316](https://linear.app/mgh3326/issue/ROB-316)
**Decision doc:** `docs/plans/ROB-316-binance-scalping-backtest-project-comparison.md`
**Date:** 2026-05-26

---

## 0. Spike 목표 (가설 검증)

**가설:** NautilusTrader로 기존 trend micro-breakout 스캘핑 전략을 Binance **tick 데이터** 위에서 백테스트하고,
실시간 **L2 호가** 기반 모의투자(paper) 경로를 세울 수 있다 — 실주문·시크릿·prod DB 변경 없이.

**검증 질문:**
1. tick fidelity에서 보수적 비용을 적용해도 전략 net이 의미 있나? (또는 음수임을 정직하게 확인)
2. 포팅한 Nautilus `Strategy`가 기존 `evaluate_signal`과 **동일 신호**를 내는가 (parity)?
3. Nautilus 채택 비용(데이터 적재 + event-driven API)이 로드맵(ICT + 모의투자) 대비 정당한가?

**산출물:** 격리 환경 + 데이터 스크립트 + 포팅 전략 + parity 테스트 + 백테스트 리포트 1건 + **go/no-go 판단**.

---

## 1. 비목표 / 안전 경계 (하드)

- ❌ 실 브로커 주문(submit/cancel/modify/preview/test) — 모의투자는 **SandboxExecutionClient(simulated fill)** 또는 market-data-only
- ❌ `BINANCE_DEMO_SCALPING_ENABLED` / scheduler / TaskIQ / Prefect / cron 활성화
- ❌ prod env/secret 읽기·쓰기·print — **데이터는 공개(키 불필요)**, 실행 venue 키 미사용
- ❌ prod DB write / backfill — 결과는 로컬 Parquet/CSV export
- ❌ 기존 Demo 실행 경로(ROB-298/307 `demo_scalping/`, `futures_demo/`, ledger) 변경 — Nautilus는 그 **밖**의 독립 연구 트랙
- ❌ auto_trader 메인 런타임 의존성에 `nautilus_trader` 추가 (격리 venv — §2)

---

## 2. Step 0 — 격리 환경

auto_trader 런타임을 오염시키지 않도록 **별도 venv/디렉터리**에 둔다.

```bash
# 연구 트랙 (auto_trader pyproject 의존성과 분리)
mkdir -p research/nautilus_scalping && cd research/nautilus_scalping
uv venv .venv && source .venv/bin/activate
uv pip install nautilus_trader          # 프리빌트 wheel (darwin/arm64 확인)
python -c "import nautilus_trader, sys; print(nautilus_trader.__version__, sys.platform)"
```

검증: import 성공 + 버전 출력. wheel 미지원 플랫폼이면 여기서 중단하고 기록.

---

## 3. Step 1 — tick 데이터 확보 (공개, 키 불필요)

1심볼(XRPUSDT) · spot · bounded window(예: 2주)로 시작. 확장은 통과 후.

```bash
# data.binance.vision 공개 덤프 (aggTrades). 시크릿 불필요.
# 예: spot/daily/aggTrades/XRPUSDT/XRPUSDT-aggTrades-YYYY-MM-DD.zip
```

- 다운로드 스크립트: `research/nautilus_scalping/fetch_agg_trades.py` (HTTP GET + 무결성 체크).
- 검증: 다운로드 행 수 / 시간 범위 / 결측 일자 출력.

---

## 4. Step 2 — Nautilus catalog 적재

aggTrades → Nautilus `TradeTick` → `ParquetDataCatalog`.

- 스크립트: `research/nautilus_scalping/ingest.py` (Binance aggTrades wrangler 사용).
- 검증: catalog에서 `TradeTick` 카운트 = 원본 행 수, 첫/끝 타임스탬프 일치.

---

## 5. Step 3 — 전략 포팅 + parity 테스트

기존 규칙(SMA 7/25, breakout lookback 20, TP +30 / SL −20 bps, spot long-only)을 Nautilus `Strategy`로 포팅.

- **신호 정합 우선:** tick → 1m bar(`BarType`) 집계 후 동일 SMA/breakout 규칙 적용 (현 `signal.py` 시맨틱과 1:1).
- **parity 테스트** (필수): 동일 1m 캔들 윈도우에서
  `demo_scalping/signal.py::evaluate_signal` 결과 vs 포팅 전략의 per-bar 결정이 **일치**함을 단언.
  → 포팅 충실도 증명. 위치: `research/nautilus_scalping/tests/test_signal_parity.py`.
- **No-lookahead:** 캔들 `t` close 신호 → `t+1` open 진입 (Nautilus fill 모델).
- **보수적 fill:** tick 데이터가 intrabar 경로를 제공 → 동일 캔들 TP/SL 모호성이 candle보다 정직하게 해소. (호가 없으면 여전히 trade-tick 근사 — 리포트에 명시.)

---

## 6. Step 4 — 백테스트 실행 + 리포트

`BacktestEngine`에 fee 모델(= `cost.py` `fee_rate_bps` 기준값)·스프레드 가정 설정 후 XRPUSDT tick 윈도우 실행.

- **지표:** trades, win rate, profit factor, **net PnL after costs**, MDD, avg holding seconds, per-trade expectancy. **gross/net 분리.**
- **export:** trade-level rows → `scalp_trade_analytics` 컬럼 미러링 Parquet/CSV (DB write 아님; `instrument_id` 등 FK 제외, symbol 유지).
- **결정 비교:** tick fidelity 결과가 1m OHLC 가정과 다른가? 보수적 비용 후 살아남나?

---

## 7. Step 5 — 실시간 호가 모의투자 (stretch / 게이트)

> ⚠️ 가장 신중한 단계. **실주문 0** 보장 후에만. 시간 초과 시 follow-up 이슈로 분리.

- Nautilus `TradingNode` + Binance 어댑터로 XRPUSDT **L2 order book 실시간 구독**.
- 실행은 **SandboxExecutionClient(simulated fill against live book)** — 실행 venue 키 미사용, 실주문 없음.
- 라이브 호가 deltas를 catalog로 **녹화** → 자체 order book 데이터셋 축적(미래 호가 backtest 재료).
- 검증: 일정 시간 무주문(시뮬 fill만) + 호가 녹화 행 수 출력.

---

## 8. Step 6 — 결정 게이트 (go/no-go)

리포트에 기록:
- 전략이 tick fidelity·보수적 비용에서 net 양수인가, 음수인가 (정직하게).
- parity 테스트 통과 여부.
- 채택 비용 체감(데이터 적재 + API)이 ICT/모의투자 로드맵 대비 정당한가.

**Go:** ICT 전략 추가 + 호가 데이터 축적 본격화 (후속 이슈).
**No-go:** Freqtrade dry-run으로 모의투자만 빠르게(프로토타입) + Nautilus는 보류 기록.

---

## 9. 스코프 제어

- spike = **1심볼(XRPUSDT) · spot · bounded window · tick backtest + parity 테스트**.
- Step 5(실시간 호가 모의투자)는 stretch — 커지면 중단하고 follow-up 분리.
- futures · 멀티심볼 · ICT 전략 · 호가 과거 backtest = **후속 이슈**.

### 제안 후속 이슈
1. Nautilus futures + 멀티심볼(XRP/DOGE/SOL) tick backtest 확장
2. 라이브 L2 호가 상시 녹화 파이프라인 + catalog 운영
3. ICT 규칙 객관화(FVG/order block/liquidity) → Nautilus `Strategy` 구현 + backtest
4. (호가 데이터 축적 후) 호가 기반 스캘핑/ICT backtest

---

## 10. 산출물 체크리스트

- [x] `research/nautilus_scalping/` 격리 venv (3.13) + Rust 소스 빌드 + import 검증 (nautilus_trader 1.227.0)
- [x] `fetch_agg_trades.py` — 공개 tick 데이터 다운로드 (checksum 검증)
- [x] `ingest.py` — catalog 적재 + read-back 카운트 검증 (1,584,229 ticks)
- [x] 포팅 `Strategy` + `tests/test_signal_parity.py` (evaluate_signal 재사용, 5/5 pass)
- [x] tick 백테스트 리포트 1건 (gross/net 분리, 지표, CSV export)
- [ ] (stretch) 실시간 호가 모의투자 + 녹화 — 무주문 증명 → **follow-up 이슈로 분리**
- [x] go/no-go 판단 + no-side-effect 선언 (§11)

---

## 11. Spike 결과 (2026-05-26)

**환경:** Intel macOS x86_64 → native wheel 없음 → Rust(`rustc 1.95.0`) 설치 후 소스 빌드 성공.
nautilus_trader **1.227.0**, Python 3.13.13, 격리 venv (`research/nautilus_scalping/.venv`, auto_trader 런타임과 분리).

**데이터:** Binance Spot **XRPUSDT aggTrades**, 2026-05-01 ~ 05-14 (14일), 공개 데이터(키 불필요),
checksum 검증. catalog 적재 **1,584,229 ticks**.

**전략:** 기존 `demo_scalping/signal.py::evaluate_signal` 재사용(SMA 7/25, breakout 20, TP +30 / SL −20bps,
spot long-only). 1m bar 집계 → 신호 → 다음 tick MARKET 진입(no-lookahead) → tick-level TP/SL(보수적 SL-우선).
수수료 = taker 10bps/leg (instrument fee).

**백테스트 결과 (344 trades):**

| metric | value |
| -- | -- |
| win rate | 40.7% (140/344) |
| gross PnL | **−1.00 USDT** (사실상 flat) |
| total fees | **98.56 USDT** |
| **NET PnL** | **−99.56 USDT** |
| avg win / avg loss | +0.14 / −0.58 USDT |
| avg net return | −20.19 bps/trade |
| profit factor (net) | 0.17 |
| max drawdown | −99.70 USDT |
| avg holding | ~32분 |

**해석:** 전략은 **gross 기준 거의 break-even**이고, **손실 전액(−99.56)이 수수료**다.
40.7% × +30bps − 59.3% × −20bps ≈ 0bps(gross), 여기서 round-trip ~20bps taker fee를 빼면
−20bps/trade — 관측치(−20.19bps)와 정확히 일치. **30/20bps TP/SL은 taker 수수료에 완전히 잠식**되어
현 구성으로는 spot에서 비viable. (ROB-316 정확성 규칙 "net-after-cost가 살아남지 못하면 수익 보고 금지" 충족.)

**검증 커맨드:**
```bash
ROOT=/Users/mgh3326/work/auto_trader.rob-316; R=$ROOT/research/nautilus_scalping
# tests (5/5)
PYTHONPATH=$ROOT $R/.venv/bin/python -m pytest $R/tests/ -v
# data → catalog → backtest
python3 $R/fetch_agg_trades.py --symbol XRPUSDT --market spot --from-date 2026-05-01 --to-date 2026-05-14 --out $R/data
PYTHONPATH="$ROOT:$R" $R/.venv/bin/python $R/ingest.py --data-dir $R/data --catalog $R/catalog
PYTHONPATH="$ROOT:$R" $R/.venv/bin/python $R/backtest.py --catalog $R/catalog --symbol XRPUSDT --trade-size 100
```

> **빌드 캐시 정책 보강(후속):** 이 spike 빌드는 **명시적 wheelhouse/cache 정책 없이** 진행됐다
> (`uv pip install` 직접 빌드 → wheel은 uv 캐시에 우연히 남고 Rust target은 ephemeral로 폐기됨).
> 다음 venv/실행/Nautilus 버전에서도 관리 가능하도록 §12에 **재현 가능 wheelhouse 정책**을 추가했다.

**구현 노트 (재현 함정):**
- Intel mac은 Nautilus wheel 미제공 → Rust 빌드 필수 (Apple Silicon/Linux는 `pip install`만).
- aggTrades 덤프는 **헤더 없음 + 타임스탬프 µs** (2025년 ms→µs 전환). 잘못 주면 tick 시각 어긋남.
- 백테스트 OMS는 **HEDGING** 사용 → 각 round-trip이 개별 `Position`(per-trade 분석 용이).
  NETTING은 단일 netting position + flatten 스냅샷이라 `cache.positions_closed()`가 1만 반환(함정).

**Go / No-go:**
- ✅ **Nautilus 도입 자체는 GO** — Intel mac 소스 빌드 포함 end-to-end 검증 완료. 신호 재사용·parity·tick 백테스트·per-trade export 모두 동작. 채택 비용(event-driven API, catalog 적재)은 감당 가능 수준.
- ⚠️ **현 스캘핑 전략은 No-go (전략 문제이지 툴 문제 아님)** — 수수료 잠식. 다음이 필요:
  - maker 리베이트/낮은 수수료(BNB·VIP) 가정 재평가, 또는
  - TP/SL을 수수료 대비 충분히 크게(예: 비용의 3~5배), 또는
  - 진입 selectivity↑(win rate↑) — 호가/ICT fidelity가 여기서 의미.

**No-side-effect 선언:** 브로커 주문/시크릿/prod DB/스케줄러 mutation 0. 공개 데이터만 사용.
auto_trader 런타임 의존성 불변(격리 venv). 기존 Demo 실행 경로(ROB-298/307) 미변경.

### 다음 단계 (follow-up 후보)
1. 수수료 민감도 sweep (maker vs taker, TP/SL 배수) — "어떤 비용/타깃에서 net 양수가 되나"
2. futures(USDM) + 멀티심볼(DOGE/SOL) 확장
3. 실시간 L2 호가 모의투자 + 녹화 (stretch Step 5)
4. ICT 규칙 객관화 → Strategy 구현 (호가 fidelity 활용)

---

## 12. Wheelhouse / cache policy (Intel macOS native, 재현 가능)

**맥락:** §11 spike 빌드는 명시적 캐시 정책 없이 진행됐다(빌드는 성공했으나 wheel은 uv 캐시에
우연히 남고 Rust target은 ephemeral 폐기). 이 섹션은 "이번 세션에서만 성공한 venv"가 아니라
**다음 venv/실행/Nautilus 버전에서도 관리 가능한 빌드 체계**를 정의한다.

### 12.1 원칙 (결정)
- **Docker는 fallback으로도 사용하지 않는다** (macOS 성능).
- v1.191.0-era macOS x86 wheel은 **historical note일 뿐, primary path 아님**.
- **primary path = 최신 NautilusTrader source build on Intel macOS + local wheelhouse**.
- `nautilus_trader`를 **auto_trader main `pyproject`에 추가하지 않는다** — `research/nautilus_scalping` 격리 환경 안에서만.
- 실주문 / broker·order / scheduler / prod-DB / env·secret 변경 **없음**. 공개 패키지 소스만.

### 12.2 version-specific vs 재사용
- **최종 wheel = (version + Python ABI + macOS/arch)-specific.** 예: `nautilus_trader 1.227.0 + cp313 + macos x86_64`.
  **새 NautilusTrader 버전 → 최종 wheel 재빌드 필수.**
- **build cache(Cargo registry + cargo-target + uv cache)는 버전 간 재사용 가능** → 재빌드 시 컴파일된 crate 재사용.
- **cache key 포함 요소:** nautilus_trader version · Python minor/ABI(cp313) · OS/arch(macos-x86_64) · Rust version · source tag/sha.
  예: `nautilus_trader-1.227.0+cp313+macos-x86_64+rust-1.95.0`.

### 12.3 로컬 경로 (repo 밖, git에 넣지 않음)
| 용도 | 경로 |
| -- | -- |
| 최종 wheel | `~/wheelhouse/nautilus_trader/wheels/` |
| build logs | `~/wheelhouse/nautilus_trader/logs/` |
| Cargo home (registry) | `~/wheelhouse/nautilus_trader/cargo-home/` |
| Cargo target | `~/wheelhouse/nautilus_trader/cargo-target/` |
| uv cache | `~/wheelhouse/nautilus_trader/uv-cache/` |
| pip cache (sdist) | `~/wheelhouse/nautilus_trader/pip-cache/` (정책 추가분) |

### 12.4 빌드 스크립트
`research/nautilus_scalping/scripts/build_wheelhouse.sh [VERSION] [--smoke-only]`
- 기본 `VERSION=1.227.0` (검증됨); arch x86_64 / Python 3.13 / rust·cargo gate
- `CARGO_HOME`/`CARGO_TARGET_DIR`/`UV_CACHE_DIR`/`PIP_CACHE_DIR`를 wheelhouse 하위로 지정
- `pip wheel nautilus_trader==VERSION --no-binary nautilus_trader --no-deps` → wheelhouse
- clean temp venv 설치 + smoke(import / version / path / arch / python)
- 성공 시 wheel 파일명·크기·sha256·cache key·smoke 결과 출력; 실패 시 실패 step 출력
- `--smoke-only`: source build 생략, 기존 wheelhouse wheel만 smoke

### 12.5 설치 smoke 두 모드
- **online-prefer-local** (`--find-links <wheelhouse>`): 로컬 wheel + 빠진 dep는 PyPI. → **검증 PASS**.
- **offline-strict** (`--no-index --find-links <wheelhouse>`): wheelhouse만. → **현재 EXPECTED-FAIL** —
  dependency wheel(예: `uvloop==0.22.1`, numpy, pandas, pyarrow)이 아직 wheelhouse에 없음.
  **follow-up:** `pip wheel nautilus_trader==VERSION -w wheels`(--no-deps 제거) 또는 `pip download`로
  dependency wheel까지 채우면 완전 offline 설치 가능.

### 12.6 검증된 inventory (이 세션)
- macOS **26.5** (25F71), **x86_64**
- Python **3.13.13**, ABI **cp313**, SOABI `cpython-313-darwin`
- Rust **1.95.0** / cargo **1.95.0**
- nautilus_trader **1.227.0** — **source build** (WHEEL `Generator: poetry-core 2.3.1`, `Tag: cp313-cp313-macosx_26_0_x86_64`)
- import path: `research/nautilus_scalping/.venv/.../nautilus_trader/__init__.py`
- 보존 wheel: `nautilus_trader-1.227.0-cp313-cp313-macosx_26_0_x86_64.whl`, **156M**,
  sha256 `414201809c54f49071b37e6bb24cfeee01b772a832b24703df29198468a36824`
- ⚠️ wheel이 `macosx_26_0`로 태깅됨(빌드 호스트 SDK = macOS 26) → macOS ≥26 x86_64에서 설치.
  다른 macOS 버전에서 빌드하면 min-version 태그가 달라짐.
- 원래 spike 빌드: `uv pip install ... nautilus_trader` (명시 캐시 정책 없음; 소요시간 미계측).
  정책 적용 풀빌드 소요시간/결과는 §12.7.

### 12.7 풀빌드 검증 결과 (정책 스크립트) — 재현성 핵심 발견

`scripts/build_wheelhouse.sh 1.227.0` 클린룸 풀빌드를 실행한 결과 **cargo build 단계에서 실패**했다
(exit 101). 이건 스크립트 버그가 아니라 **upstream Rust 의존성 drift**다 — 그리고 wheelhouse 정책의
존재 이유를 그대로 증명한다.

**근본 원인:**
- nautilus_trader 1.227.0가 끌어오는 `pyo3-stub-gen 0.20.0`은 `PyEncodingWarning`을 참조하는데,
  클린 `CARGO_HOME`이 새로 resolve한 `pyo3 0.28.3`이 그 심볼을 제거함 → `rustc E0425` (unresolved name).
- nautilus 1.227.0 sdist는 이 transitive dep를 **고정(lock)하지 않음** → 매 소스 빌드가 crates.io에서
  최신을 새로 resolve → drift에 노출.
- §11의 원래 빌드(08:10, `uv pip install`)는 당시 `pyo3 ≤0.28.2`를 resolve해 **성공**했다.
  즉 동일 nautilus 버전이라도 **빌드 시점의 crates.io 상태에 따라 클린 재빌드 결과가 달라진다.**

**함의 (정책 결정):**
- **클린룸 from-source 재빌드는 crates.io에 대해 신뢰성 있게 재현되지 않는다.**
- 따라서 **재현성의 단위는 "빌드된 wheel"이다** — inputs가 아니라 **output(wheel)을 보존**한다.
  이것이 wheelhouse 정책의 핵심. **canonical artifact = 보존된 wheel**
  (`...macosx_26_0_x86_64.whl`, sha256 `414201809c…`) — **재빌드 실패 후에도 install+import 정상 검증됨**.
- 부수효과: 실패한 빌드도 cargo 컴파일은 상당 부분 진행되어 `cargo-target` ≈ **1.0G**, `cargo-home/registry`
  warmed → 향후(고정된) 빌드 가속용 캐시는 확보.
- smoke 2-mode는 `--smoke-only`로 별도 검증됨: online-prefer-local **PASS**, offline-strict **EXPECTED-FAIL**.

**재빌드 시 mitigation (follow-up):**
1. **기본: 보존 wheel 사용** — 버전 bump 전엔 재빌드하지 않는다.
2. 재빌드가 필요하면 transitive dep를 고정: 작동하는 `Cargo.lock` 캡처 후 `--locked` 빌드, 또는 추출된
   sdist에서 `cargo update -p pyo3 --precise <working>` 후 빌드, 또는 upstream이 제약을 조일 때까지 대기.
3. 작동 crate 세트를 vendor.

**소요시간:** cargo 컴파일이 수 분 진행 후 `pyo3-stub-gen`에서 실패 — 클린 성공 시간은 아님.
원래 성공 빌드(§11)는 계측되지 않았으므로 **클린 성공 빌드 시간은 아직 미확보**(정직하게 기록).
다음 성공 빌드는 스크립트가 `build duration: Ns`로 출력한다.

### 12.8 재현 커맨드
```bash
R=research/nautilus_scalping
# 풀 source build → wheelhouse (느린 Rust 컴파일; cargo-target 재사용으로 2회차부터 빠름)
$R/scripts/build_wheelhouse.sh 1.227.0
# 기존 wheel만 smoke (빠름)
$R/scripts/build_wheelhouse.sh 1.227.0 --smoke-only
# 연구 venv에 wheelhouse에서 설치
uv pip install --python $R/.venv/bin/python \
  --find-links ~/wheelhouse/nautilus_trader/wheels nautilus_trader==1.227.0
```

### 12.9 CI (옵션만 — 지금 구현 안 함)
- 크기는 문제 아님(~156M wheel). 실제 blocker: macOS **Intel** runner 가용성, Rust 빌드 시간,
  Cargo cache 크기, 비용/쿼터, artifact 보존.
- GitHub-hosted 사용 시 `macos-latest`(arm64) 금지 → **Intel runner label 명시** 필요.
- 권장 순서: (1차) 로컬 Intel MacBook build script — 재현성 최고. (2차) Intel MacBook **self-hosted**
  GitHub Actions runner + `workflow_dispatch` 수동 트리거.

---

## 13. Fee/target 민감도 sweep (2026-05-26)

**질문:** 어떤 (fee, TP/SL) 조합에서 net 양수가 되나?
**방법:** fee는 trade를 바꾸지 않으므로 (TP,SL)당 엔진 1회 실행 후 fee를 analytic 재계산
(`net(fee)=realized_pnl + commission_ref*(1-fee/10)`, exact). NautilusTrader Rust logger가
**process-global singleton**이라 한 프로세스에 BacktestEngine 2개면 panic →
**combo당 subprocess** (`fee_sweep.py --single` worker + driver). 14일 XRPUSDT spot tick.

**NET PnL (USDT):**
```
    TP/SL  trades   10bps   7.5     5.0     2.0     1.0     0.0
    30/20     344   -99.6   -74.9   -50.3   -20.7   -10.9    -1.0
    40/20     301   -87.3   -65.8   -44.2   -18.4    -9.8    -1.1
    50/30     218   -65.2   -49.6   -34.0   -15.2    -9.0    -2.7
    60/40     163   -41.2   -29.5   -17.8    -3.7    +1.0    +5.7
    80/40     130   -30.4   -21.1   -11.7    -0.5    +3.2    +6.9
   100/60      85   -20.7   -14.6    -8.5    -1.1    +1.3    +3.8
  100/100      47    -4.2    -0.8    +2.6    +6.7    +8.0    +9.4
```
break-even frontier(net>0 최대 per-leg fee): 30/20·40/20·50/30 = **NEVER**(0 fee에서도 음수),
60/40·80/40·100/60 = **fee ≤ 1bps**, 100/100 = **fee ≤ 5bps**(break-even 5~7.5 사이).

**해석:**
1. 현실 fee(spot taker 10bps, BNB 7.5bps)에선 **모든 조합 손실**.
2. tight scalp 타깃(30/20·40/20·50/30)은 **0 fee에서도 음수 → gross edge 자체가 없음**
   (1m tight 구간에서 신호는 노이즈+비용). 손절/타깃을 키워도 이 셋은 안 됨.
3. **gross edge는 타깃을 넓혀야 나타남**(100/100 @0fee = +9.4). 단 trade수 344→47로 급감.
4. 현실 cost 근처에서 유일하게 사는 건 **100/100 @ fee ≤5bps**(VIP/maker 영역).

**결론:**
- 원래 30/20 micro-breakout 스캘핑은 **구조적으로 비viable**(gross edge 없음) — fee 문제 이전에 **신호 edge 문제**.
- 살길 후보: (a) **진입 selectivity↑로 win rate 향상** — 여기서 호가/ICT fidelity가 의미를 가짐;
  (b) **maker 실행 + 넓은 타깃(100bps급) + 낮은 fee**; (c) tight-scalp 전제 폐기.
- 다음 우선순위: 신호 개선(ICT/호가 기반) 또는 100/100+maker 시나리오 심화. (fee만으로는 못 살림.)

**검증:** `PYTHONPATH=<root>:<R> .venv/bin/python fee_sweep.py --catalog catalog --symbol XRPUSDT --trade-size 100`
(export: `results/fee_sweep.csv`, 42 rows). No broker/order/secret side effect.
