# ROB-316 — Binance scalping backtest: 외부 프로젝트 비교 + 도입 결정

**Status:** Phase 0 (comparison / decision) — 구현 미포함. 실행은 별도 spike plan 참조.
**Issue:** [ROB-316](https://linear.app/mgh3326/issue/ROB-316)
**Author:** 문광현 (with Claude Code)
**Date:** 2026-05-26
**Decision:** ✅ **NautilusTrader 도입** (Freqtrade = 프로토타입/교차검증, Jesse = deprioritized)
**Spike plan:** `docs/plans/ROB-316-nautilustrader-adoption-spike-plan.md`
**Related:** ROB-307 (Demo scalping E2E MVP), ROB-313 (cost-capture + `scalp_trade_analytics`), ROB-315 (`/invest` scalping review loop)

---

## 1. 목적 + 확정된 로드맵

ROB-316 본래 목표는 scalping 전략의 **파라미터·비용 모델을 과거 데이터로 검증**하는 backtest 경로 마련.
검토 대화에서 다음 로드맵이 확정되어, 결정의 전제가 바뀌었다:

1. **스캘핑 백테스트** (now)
2. **ICT 트레이딩 스타일 검증** (next) — FVG / order block / liquidity sweep / 세션(killzone)
3. **스캘핑 모의투자 (paper trading)** (next)
4. **솔로/개인 사용 — 외부 배포 없음**

→ 단발 backtest가 아니라 **다(多)전략 backtest + 모의투자 플랫폼**이 필요하다.
이 로드맵에서는 **외부 프레임워크 적극 도입이 internal MVP보다 우월**하다(자체 MVP로는 ICT·멀티 타임프레임·호가·모의투자·파라미터 탐색을 결국 재구현 = 프레임워크 재발명).

> **이전 버전 정정:** 본 노트 초안은 "internal MVP + 런타임을 GPL/AGPL에 비의존"을 권고했다.
> 솔로/비배포 사용(§4)과 ICT/모의투자 로드맵(§1)을 반영해 **NautilusTrader 도입으로 변경**한다.

---

## 2. 재사용 가능한 자체 자산

| 자산 | 경로 | 도입 시 활용 |
| -- | -- | -- |
| 신호 규칙 | `app/services/brokers/binance/demo_scalping/signal.py` | 순수 규칙(SMA 7/25, breakout 20, TP30/SL20). Nautilus `Strategy`로 **포팅** + `evaluate_signal`과 동일 캔들에서 결과 일치 **parity 테스트**로 충실도 증명 |
| 비용 모델 | `demo_scalping/cost.py` | Nautilus fee/슬리피지 모델 설정값(예: `fee_rate_bps`)의 **기준 reference** — 백테스트 결과를 기존 분석과 정합 |
| 결과 스키마 | `app/models/scalp_trade_analytics.py` | 백테스트 trade-level export 컬럼 미러링 대상 (DB write 아님) |
| 기존 `backtest/` | Upbit KRW 일봉 autoresearch | **별개 시스템** — 재사용 부적합 (시장·타임프레임·전략 상이, `program.md` 잠금 규칙) |

---

## 3. 후보 매트릭스 (GitHub API 검증, 2026-05-26)

| Project | License | Stars | 최근 push | crypto/Binance | backtest fidelity | 파라미터 탐색 | 모의/live parity | 도입 비용 |
| -- | -- | -- | -- | -- | -- | -- | -- | -- |
| **NautilusTrader** | LGPL-3.0 | 23k | 활발(당일) | ★★★ spot+futures, **L2 order book + tick (ns)** | ★★★ event-driven, 결정적 | ★★ (직접 구성) | ★★★ **동일 코드 backtest=live** | 高 |
| **Freqtrade** | GPL-3.0 | 50.7k | 활발(당일) | ★★★ spot+futures (ccxt) | ★★ candle 기반 | ★★★ **hyperopt 내장** | ★★ dry-run 쉬움 | 低 |
| **Jesse** | MIT | 7.9k | 활발(당일) | ★★ crypto 전용 | ★★ candle 기반 | ★★ optimization | ★★ | 中 |
| **Hummingbot** | Apache-2.0 | 18.7k | 활발 | ★★★ connector/HFT | ★ (역사적 backtest 약함) | ★ | — (live MM) | 高 |
| vectorbt | 비표준/Commons Clause | — | 활발(PRO 상업화) | ★ | ★★ 벡터화 | ★★★ sweep | — | runtime dep 금지 |
| backtesting.py | **AGPL-3.0** | — | 보통 | ★ | ★ | ★ | — | reference only |
| backtrader | GPL-3.0 | — | 저활성 | ★ | ★★ | ★ | — | reference only |
| Blankly | LGPL-3.0 | — | 저활성 | ★★ | ★ | ★ | ◯ | quick check |
| Zipline Reloaded | Apache-2.0 | — | 보통 | ✗ (daily 포트폴리오) | ★★ | ★ | — | 부적합 |
| bt | MIT | — | 저활성 | ✗ | ★ | ★ | — | 부적합 |
| PyAlgoTrade | (archived) | — | **archived** | ✗ | ★ | ✗ | — | reject |

전체 링크: Freqtrade https://github.com/freqtrade/freqtrade · NautilusTrader https://github.com/nautechsystems/nautilus_trader · Jesse https://github.com/jesse-ai/jesse · vectorbt https://github.com/polakowo/vectorbt · backtesting.py https://github.com/kernc/backtesting.py · backtrader https://github.com/mementum/backtrader · Hummingbot https://github.com/hummingbot/hummingbot · Blankly https://github.com/blankly-finance/blankly · Zipline Reloaded https://github.com/stefan-jansen/zipline-reloaded · bt https://github.com/pmorissette/bt · PyAlgoTrade https://github.com/gbeced/pyalgotrade

---

## 4. 라이선스 경계 — 솔로/비배포 reframe

GPL/AGPL의 copyleft 의무는 **배포(conveyance)** 시점에 발동한다. 본 사용은 **솔로/개인, 외부 배포 없음**:

- **Freqtrade(GPL-3.0):** 비배포 사용이면 소스 공개 의무 사실상 없음 → GPL **비제약**.
- **NautilusTrader(LGPL-3.0):** 라이브러리 사용(동적 링크/`pip install`)은 copyleft 미발동 → **비이슈**.
- **AGPL(backtesting.py):** 네트워크 사용까지 배포로 보지만, 비배포 솔로면 역시 비제약. 다만 원칙적으로 회피 권장(미래 배포 시 가장 위험).
- **permissive(Apache/MIT):** 제약 없음.

> ⚠️ **재검토 트리거:** auto_trader/연구물을 **외부에 배포(SaaS·바이너리·오픈소스)** 하게 되면 GPL/AGPL 경계가 다시 살아난다. 그 시점에 재평가.
> ⚠️ **라이선스 재확인:** 실제 코드 재사용 전 각 repo `LICENSE` 재확인. 본 노트는 법률 자문 아님.

---

## 5. 결정 — NautilusTrader 도입

### 근거

1. **호가(order book) fidelity** — 로드맵의 두 전략 모두 마이크로구조에 갇혀 있다:
   - 스캘핑 30/20bps 엣지는 스프레드·체결 큐에 산다.
   - ICT(liquidity sweep / order block / FVG)는 "유동성이 어디 쌓였나" → 호가 depth가 있어야 **측정 가능**.
   - Nautilus만 **L2 order book + tick(나노초)** backtest를 제공 → candle 엔진이 *지어내던* 부분을 측정으로 전환.
2. **결정적 backtest = live parity** — 동일 Python 전략 코드가 backtest → 모의투자 → (원하면) live. 로드맵("백테스트→ICT 검증→모의투자")에 정확히 맞는 구조.
3. **Rust는 비이슈** — 전략은 **Python으로 작성**(`pip install -U nautilus_trader`, 프리빌트 wheel). Rust/Cython 코어는 결정적·고속 시뮬레이션이라는 *장점*. Rust 툴체인 안 만짐.
4. LGPL + 활발(23k, 당일 push) + Binance spot/futures 어댑터 + 프로급 아키텍처.

### Rust 우려에 대한 정정
"Rust라서 도입 회피"는 근거 없음. 비용은 언어가 아니라 (a) event-driven 모델, (b) 데이터 catalog 적재 플러밍이다.

---

## 6. 데이터 전략 — 호가 과거 데이터가 병목

| 데이터 | 과거 데이터 가용성 | 용도 |
| -- | -- | -- |
| klines / 1m | 무료 (data.binance.vision) | 현 수준 |
| **tick (aggTrades)** | **무료, 풍부** (data.binance.vision) | **지금 backtest** — 1m OHLC 대비 스캘핑 fidelity 대폭↑ |
| **L2 order book (full depth)** | **희소/유료** (Tardis.dev·Kaiko 등) 또는 직접 녹화 | 미래 호가 기반 backtest |

**경로:** ① tick으로 지금 backtest → ② 실시간 L2 호가로 paper trading + ③ 라이브 호가를 catalog로 **녹화**하여 자체 order book 데이터셋 축적 → ④ 축적분으로 호가 기반 과거 backtest.

---

## 7. 정직한 비용 / caveat

- event-driven 전략 API는 Freqtrade `populate_*`보다 boilerplate 많음.
- `ParquetDataCatalog` 적재 초기 플러밍 1회.
- candle 대비 tick/호가는 데이터 무겁고 저장 큼.
- **ICT 규칙 객관화는 엔진과 무관하게 어려운 부분** — 호가는 fidelity를 주지만 "무엇이 order block인가"는 직접 정의해야 함.
- 30/20bps 스캘핑은 tick fidelity에서도 **net이 음수일 가능성** — 정직하게 보고(gross/net 분리, 보수적 비용).

---

## 8. 다른 후보의 위치

- **Freqtrade** — 빠른 프로토타입 / 교차검증. `dry_run: true` 한 줄로 모의투자가 쉬움. hyperopt가 강점이라 파라미터 1차 탐색 용도로 병행 가능. GPL은 솔로라 무관.
- **Jesse** — MIT지만 솔로 사용에선 코드 소유 강점이 무의미 → **deprioritized**.
- **Hummingbot** — execution/connector 아키텍처 reference (백테스트 부적합).
- 나머지(vectorbt/backtesting.py/backtrader/Blankly/Zipline/bt) — reference-only 또는 부적합. PyAlgoTrade — reject(archived).

---

## 9. 안전 경계 (불변)

Phase 0는 **문서 작성만** — 코드 변경 없음. 도입/spike에서도 다음을 위반하지 않는다:

- 브로커 주문(submit/cancel/modify/preview/test) 금지 — 모의투자는 **simulated fill / market-data-only**
- `BINANCE_DEMO_SCALPING_ENABLED` / scheduler / TaskIQ / Prefect / recurring automation 활성화 금지
- prod env/secret mutation·print 금지 — 데이터 다운로드는 **공개 데이터(키 불필요)**
- prod DB write·backfill 금지 — 결과는 export, DB write 아님
- live trading 권한·실거래 경로 변경 금지
- 기존 안전 게이트 Demo 실행 경로(ROB-298/307) 미변경 — Nautilus는 그 경로 **밖**의 독립 연구 트랙

---

## 10. 결론 / 다음 단계

- **선택:** NautilusTrader 도입. tick-우선 backtest로 시작, 호가는 실시간 모의투자/녹화부터.
- **데이터 병목:** 과거 L2 호가는 사거나 녹화 → tick으로 즉시 시작 가능.

### 검증 결과 요약 (spike 완료, 2026-05-26)
- ✅ **NautilusTrader 도입 = GO.** **Intel Mac native source build 검증 완료** (no x86_64 wheel → Rust 빌드,
  nautilus_trader 1.227.0 / Python 3.13 / 격리 venv). 신호 재사용·parity 테스트(5/5)·tick 백테스트·per-trade export 동작.
  재현 가능 wheelhouse 정책 + `build_wheelhouse.sh` 추가 (spike plan §12).
- ⚠️ **현 30/20bps 전략 = net-after-cost No-go** (전략 문제, 툴 문제 아님). 14일 XRPUSDT tick 백테스트:
  344 trades, gross ≈ break-even, **NET −99.56 USDT = 전액 수수료**.
- ➡️ **다음 우선순위 = fee sensitivity sweep** ("어떤 비용/타깃 조합에서 net 양수가 되나").
- 상세: `docs/plans/ROB-316-nautilustrader-adoption-spike-plan.md` (§11 결과, §12 wheelhouse 정책).

### Phase 0 Acceptance Criteria

- [x] 11개 GitHub 프로젝트 링크 + 명확한 권고 (§3, §5)
- [x] 선택 방향이 왜 안전·우월한지 설명 (§1, §5; 솔로 사용으로 라이선스 제약 해소 + 호가 fidelity)
- [x] 라이선스 리스크 명시 (§4)
- [ ] 구현/테스트 — spike plan으로 이연
