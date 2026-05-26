# ROB-327 — Vibe-Trading 참고 모델 기반 리서치 / 백테스트 검증 / 리포트 생성 설계

> **성격:** 설계·조사 문서. 코드/의존성/서버연결/주문/스케줄러/프로덕션 DB 변경 없음.
> **프레이밍:** Vibe-Trading을 *새로 베끼는* 것이 아니라, auto_trader가 **이미 가진 것**(ROB-9 / ROB-112 / ROB-316 / ROB-320 / ROB-287/301/318) 위에 Vibe가 무엇을 *더*해 줄 수 있는지의 **gap 분석**. 각 항목은 `ADOPT` / `ADAPT` / `SKIP` 한 단어로 판정한다.

---

## 0. Reference provenance (재현성)

| 항목 | 값 |
| -- | -- |
| Vibe-Trading repo | https://github.com/HKUDS/Vibe-Trading |
| 라이선스 | MIT (`/tmp/vibe-trading-reference/LICENSE`) |
| 로컬 clone 경로 | `/tmp/vibe-trading-reference` |
| **조사 시점 HEAD** | `52f9860123295e06b87aefd60e13fba9c0fe501f` (2026-05-26, "fix(deps): bump langgraph for CVE-2026-28277") |

```bash
# 재현: 없으면 고정 경로에 shallow clone, 있으면 HEAD 재확인 후 사용
cd /Users/mgh3326/work/auto_trader
if [ ! -d /tmp/vibe-trading-reference ]; then
  git clone --depth 1 https://github.com/HKUDS/Vibe-Trading /tmp/vibe-trading-reference
fi
git -C /tmp/vibe-trading-reference log -1 --format='%H %ci %s'
```

> ⚠️ shallow clone가 stale일 수 있다(이 문서의 file:line 참조는 위 HEAD 기준). 후속 작업에서 라인 번호가 어긋나면 먼저 HEAD를 위 해시와 대조한다.

---

## 1. Executive summary

세 줄 결론:

1. **데이터:** Vibe의 OHLCV 수집 표면은 auto_trader가 *이미 우월하게* 보유한다. Vibe는 6개 로더(yfinance/ccxt/okx/akshare/tushare/futu)를 **무캐시 live fetch**로 돌리지만, auto_trader는 `yfinance`/`finnhub-python`/`opendartreader`/`binance-sdk-spot`를 직접 의존성으로 두고 **OHLCV 캐시 레이어**(`yahoo_ohlcv_cache`/`upbit_ohlcv_cache`/`kis_ohlcv_cache`)까지 갖췄다. 중국권(akshare/tushare/futu)만 우리 시장 범위 밖이며 비용·약관·지역 리스크로 **`avoid`/`reference_only`**.
2. **검증:** auto_trader의 `validated_signal_gate.v1`(ROB-320)이 Vibe `agent/backtest/validation.py`보다 **검증 게이트 측면에서 더 강하다**(진짜 OOS holdout + verdict + baseline + overfit flags + 임의-fee 해석적 재계산; Vibe는 verdict/gate가 *아예 없고* walk-forward도 retrospective consistency test일 뿐). Vibe가 **더해 주는 것**은 ① bootstrap Sharpe CI, ② Monte-Carlo permutation p-value, ③ config/strategy/artifact **SHA-256 hash trio**, ④ **markdown run-card**. 이 4개를 `validated_signal_gate`에 ADOPT하는 것이 이 문서의 최우선 후속 작업이다. **단, ROB-316/320이 이미 net-after-fee 기준에서 micro-breakout/meanrev가 죽는다는 것을 증명했으므로**(아래 §6), 추가 검증은 "새 edge 발굴"이 아니라 "fee-kill 결론의 통계적 강건성 증명"으로 프레이밍한다.
3. **도입하지 말 것:** Vibe의 in-process multi-agent runtime(`agent/src/swarm/runtime.py`)과 MCP server(`agent/mcp_server.py`)는 프로덕션 리포트 경로에 **연결하지 않는다**. `/invest/reports`는 in-process LLM 금지이며 Hermes out-of-process pull/compose/push만 허용된다(ROB-287, PR #898 static import guard). Vibe runtime은 **sandbox 전용 아이디어 소스**로만 본다.

---

## 2. Vibe-Trading reference map

Vibe는 이슈 명세가 가정한 것보다 큰 repo다. 핵심 영역:

| 영역 | 경로 | 한 줄 역할 |
| -- | -- | -- |
| Data loaders | `agent/backtest/loaders/{base,registry,yfinance_loader,ccxt_loader,okx,akshare_loader,tushare,tushare_fundamentals,futu}.py` | 시장별 fallback chain으로 OHLCV/fundamentals를 **무캐시 live fetch** |
| Backtest engines | `agent/backtest/engines/{base,crypto,global_equity,china_a,forex,global_futures,...}.py` | 시장별 vectorized 엔진; slippage/commission은 **엔진 레이어**에서 적용 |
| Validation | `agent/backtest/validation.py` | bootstrap CI + MC permutation + walk-forward(consistency). **verdict/gate 없음** |
| Run card | `agent/backtest/run_card.py` | `run_card.json` + `run_card.md`, config/strategy/artifact SHA-256 |
| Metrics | `agent/backtest/metrics.py` | sharpe/sortino/calmar/profit_factor/IR 등 15+ |
| Swarm runtime | `agent/src/swarm/runtime.py` | topological DAG + layer-wise `ThreadPoolExecutor(max_workers=4)`; `.swarm/runs/{id}/`에 run.json/tasks/events.jsonl |
| Swarm presets | `agent/src/swarm/presets/*.yaml` (30개) | committee/desk 역할·DAG 정의 (YAML) |
| Tools | `agent/src/tools/*.py` | alpha_bench / backtest / factor_analysis / trade_journal / shadow_account / web_search 등 |
| Factors | `agent/src/factors/{base,bench_runner,factor_analysis_core,registry}.py` + `zoo/` | wide-DataFrame alpha 연산자 + IC/IR 평가 |
| MCP server | `agent/mcp_server.py` | 22+ tool, stdio/SSE, **live trade execution 거부**(`risk_tier` rejects `LIVE_TRADING_OR_EXECUTION`) |

두 개의 대표 preset (이슈가 지정):

- **`investment_committee.yaml`** — 4단계 DAG: `bull_advocate` ‖ `bear_advocate` → `risk_officer` → `portfolio_manager`. 변수 `{target}`, `{market}`. 산출: 최종 long/short/wait/hedge 결정 + 실행계획.
- **`quant_strategy_desk.yaml`** — 4단계 DAG: `screener` ‖ `factor_miner` → `backtester` → `risk_auditor`. 변수 `{market}`, `{goal}`. 산출: 전략 로직 + 백테스트 metrics + overfitting/tail-risk 감사.

---

## 3. Data collection audit (진하게)

> Vibe 로더는 **전부 무캐시**(매 `fetch()`마다 live). 캐시/해시 sidecar는 *alpha_bench 팩터 패널*에만 존재(`agent/src/tools/alpha_bench_tool.py` — `<path>.pkl` + `<path>.pkl.sha256` constant-time 비교). 로더 선택은 `registry.py`의 시장별 `FALLBACK_CHAINS`로 이루어진다.

### 3.1 로더별 감사표

| Vibe 데이터/기능 | 파일/함수 | source/API | 필요 필드 | 주기/범위 | credential (env 이름만) | 라이선스/약관 리스크 | 분류 |
| -- | -- | -- | -- | -- | -- | -- | -- |
| US/global equity OHLCV | `loaders/yfinance_loader.py::fetch` (L211) | Yahoo Finance (무료 public) | OHLCV, adj close; 1D/1H/4H | 일/시간봉, on-demand | 없음 | 무료·비공식 집계, ToS상 상업적 사용 불명확 | `collectable_now` |
| Crypto OHLCV (multi-exchange) | `loaders/ccxt_loader.py::fetch` (L69) | CCXT(기본 Binance) public candles | OHLCV; 1m~1D, page limit 1000 | 분~일봉 | 없음(public). 설정: `CCXT_EXCHANGE`,`CCXT_TIMEOUT_MS`,`CCXT_FETCH_BUDGET_S` | exchange별 rate-limit/ToS | `collectable_now` |
| Crypto OHLCV (OKX) | `loaders/okx.py::fetch` (L48) | OKX V5 REST public | OHLCV; 1m~1D, 300 bars/page | 분~일봉 | 없음. 설정: `OKX_TIMEOUT_S`,`OKX_FETCH_BUDGET_S` | OKX 거래소 약관 | `collectable_now`(대체) |
| China A/HK/US/ETF/FX OHLCV | `loaders/akshare_loader.py::fetch` (L93) | AKShare 집계기 | OHLCV(qfq 조정) | 일/주/월봉 | 없음 | 무료지만 **중국 소스 집계, 안정성/ToS 불명확, 우리 시장범위 밖** | `reference_only` |
| China A-share OHLCV + fundamentals | `loaders/tushare.py::fetch` (L38) | **TuShare Pro (유료/포인트제)** | OHLCV + daily_basic(PE/PB) + 분봉(≥2000pt) | 일/분봉 | **`TUSHARE_TOKEN`** | **유료 구독·포인트 rate-limit·중국 A주 전용** | `avoid` |
| 재무제표(PIT) | `loaders/tushare_fundamentals.py::query_fundamentals` (L135) | TuShare Pro | balancesheet/cashflow/income/fina_indicator, `merge_asof` PIT | 분기 | **`TUSHARE_TOKEN`** | 위와 동일(유료) | `avoid` |
| HK/A-share OHLCV (broker) | `loaders/futu.py::fetch` (L107) | **Futu OpenAPI(로컬 OpenD daemon 필요)** | OHLCV 1D/1H/4H/1W/1M | 일/시간봉 | **`FUTU_HOST`,`FUTU_PORT`** | **유료 broker·proprietary·로컬 daemon 필요·HK/A주** | `avoid` |
| Universe(CSI300/SP500/BTC-USDT) | `tools/alpha_bench_tool.py` (L84–148) | Tushare/yfinance(Wiki constituents)/OKX | wide OHLCV(+amount,+vwap) 패널 | 백테스트 구간 | (소스별) | **SP500은 survivorship-biased(현재 구성종목만)** | `reference_only` |
| News / web | `tools/web_search_tool.py`, `web_reader_tool.py`; `mcp_server.py::web_search` | DuckDuckGo(무료), URL→Markdown | 검색결과/본문 | on-demand | 없음 | 무료, 비결정적·scraping 성격 | `reference_only` |
| Trade journal | `tools/trade_journal_tool.py`, `trade_journal_parsers.py` | broker export 파일 파싱 | 거래내역 → 행동 진단 | 수동 import | 없음 | 사용자 데이터, 외부 의존 없음 | `collectable_with_config` |
| Shadow account | `src/shadow_account/{scanner,backtester,storage,reporter}.py` | 과거 roundtrip에서 규칙 추출 | 수익 거래 → 3~5 규칙 | 수동 | 없음 | 연구용(`scan_shadow_signals` "research use only") | `collectable_with_config` |
| Factor cache(only) | `tools/alpha_bench_tool.py` (L18–49) | pickle + SHA-256 sidecar | 팩터 패널 | 캐시 | 없음 | 로컬 캐시 패턴 | `reference_only`(패턴만) |

### 3.2 registry / fallback (참고)

`loaders/registry.py`의 `FALLBACK_CHAINS`:
`a_share→[tushare,akshare]`, `us_equity→[yfinance,akshare]`, `hk_equity→[yfinance,futu,akshare]`, `crypto→[okx,ccxt]`, `futures→[tushare,akshare]`, `fund→[tushare,akshare]`, `macro→[akshare,tushare]`, `forex→[akshare,yfinance]`. `resolve_loader(market)`가 `is_available()`로 첫 사용가능 로더를 고른다(credential 실패 → 다음 후보).

**핵심 관찰:** Vibe의 us_equity/crypto/forex 경로는 우리가 이미 가진 source(Yahoo/Binance/Upbit)로 100% 대체된다. 중국권 전용 경로(tushare/futu/akshare)만 우리 범위 밖이다.

---

## 4. auto_trader data-source feasibility matrix (진하게)

auto_trader가 *이미 보유한* 데이터 표면(검증됨):

- 직접 의존성: `yfinance`, `finnhub-python`, `opendartreader`, `binance-sdk-spot`, `pandas` (`pyproject.toml`)
- OHLCV 캐시: `app/services/{yahoo_ohlcv_cache,upbit_ohlcv_cache,kis_ohlcv_cache}.py`
- 심볼 유니버스(DB 단일 소스): `kr_symbol_universe` / `us_symbol_universe` / `upbit_symbol_universe`
- 뉴스/리서치/이벤트: `finnhub_news`, `kr_news_relevance_service`, `crypto_news_relevance_service`, `news_radar_service`, `research_news_service`, `market_events`(finnhub earnings + DART), `research_reports`(Naver/KIS)
- 거래 레저(trade journal 대체): `alpaca_paper_order_ledger`, `binance_demo_order_ledger`, KIS/Kiwoom mock lifecycle

### 4.1 매핑 결정표

| Vibe 데이터 항목 | auto_trader 대체 source | 분류 | ADOPT/ADAPT/SKIP | 근거 |
| -- | -- | -- | -- | -- |
| US/global equity OHLCV (yfinance) | `yahoo_ohlcv_cache` + `us_symbol_universe` | `collectable_now` | **ADOPT** | yfinance 이미 의존성, 캐시 보유 → Vibe보다 우월 |
| KR equity OHLCV | `kis_ohlcv_cache` + `kr_symbol_universe` | `collectable_now` | **ADOPT** | Vibe엔 한국 직접 소스 없음(우리 우위) |
| Crypto OHLCV (ccxt/okx) | Upbit(`upbit_ohlcv_cache`, KRW), Binance(`binance-sdk-spot`/research fstream, USDT) | `collectable_now` | **ADAPT** | 거래소만 다름; ccxt 추상화 불필요 |
| Fundamentals (PE/PB/재무제표) | KIS fundamental + `market_events`(DART/finnhub) | `collectable_with_config` | **ADAPT** | tushare 유료 대신 기존 소스로 충당, 단 PIT 스키마는 신규 필요 |
| News / web search | `finnhub_news`, `kr_news_relevance`, `crypto_news_relevance`, `news_radar`, `research_news` | `collectable_now` | **ADAPT** | DuckDuckGo scraping 대신 기존 뉴스 파이프라인 |
| Research reports | `research_reports`(Naver/KIS, ROB-140/207) | `collectable_with_config` | **ADAPT** | 이미 ingest/read 레이어 존재 |
| Cross-sectional wide panel (alpha bench) | OHLCV 캐시들로 wide panel 조립 가능 | `collectable_with_config` | **ADAPT** | 패널 조립 유틸 신규(읽기전용 spike) |
| Trade journal | `*_order_ledger` 테이블들 | `collectable_with_config` | **ADAPT** | broker export 파싱 대신 우리 레저 |
| Shadow account | (개념만) | `reference_only` | **SKIP**(현 단계) | 아이디어 가치만; 우선순위 낮음 |
| China A-share OHLCV (tushare) | 없음 | `avoid` | **SKIP** | 유료·중국전용·시장범위 밖 |
| HK/A-share (futu) | 없음 | `avoid` | **SKIP** | 유료 broker·로컬 daemon·범위 밖 |
| China 집계 (akshare) | 없음 | `reference_only` | **SKIP** | 무료지만 중국전용·ToS 불명확 |
| Factor pickle+sha256 cache | 패턴만 | `reference_only` | **ADAPT** | sha256 sidecar 아이디어는 §6 hash trio로 흡수 |

**한 줄 요약:** auto_trader의 KR/US/crypto 시장 범위 내 모든 OHLCV·뉴스·리서치 데이터는 **기존 인프라로 대체 가능**(대부분 `collectable_now`). 중국권 전용 소스(tushare/futu/akshare)만 `avoid`/`reference_only`이며 이는 **시장 범위 밖**이라 손실이 없다.

---

## 5. Integration proposal (auto_trader 접점)

> **Non-negotiable 경계:** Vibe의 in-process swarm runtime / MCP server는 프로덕션에 연결하지 않는다. 아래 매핑은 **개념적 매핑**이며, LLM reasoning이 필요한 단계는 전부 **Hermes out-of-process**가 수행한다(`/invest/reports`는 in-process LLM 금지 — ROB-287, PR #898 import guard). Vibe runtime은 sandbox 실험·아이디어 소스로만.

### 5.1 `investment_committee` → auto_trader 리포트 스택

| Vibe 역할 (committee.yaml) | auto_trader 매핑 | 비고 |
| -- | -- | -- |
| `bull_advocate` / `bear_advocate` | `investment_stage_artifacts`: bull_reducer / bear_reducer | 이미 존재(ROB-287/301) |
| `risk_officer` | `investment_stage_artifacts`: risk_review | 동일 |
| dimension 분해(market/news/fundamentals/sentiment) | `investment_dimension_reports` | TradingAgents-style per-dimension (메모리: market dimension 우선) |
| per-target 종합 | `investment_symbol_intermediate_reports` | ROB-301 dedicated table |
| `portfolio_manager` 최종 결정 | `investment_reports` final composition | **Hermes push/compose만** |
| DAG 오케스트레이션(`runtime.py`) | (개념 참고) | **프로덕션은 Hermes 경계; Vibe runtime 직접 사용 금지** |

### 5.2 `quant_strategy_desk` → auto_trader 리서치 스택

| Vibe 역할 (desk.yaml) | auto_trader 매핑 | 비고 |
| -- | -- | -- |
| `screener` | `/invest/screener` + `invest_screener_snapshots` | 이미 존재(ROB-204) |
| `factor_miner` / alpha bench | read-only factor research spike | `reference_only` future evidence; 점수화 변경은 별도 이슈 |
| `backtester` | `research/nautilus_scalping/` | 이미 존재(ROB-316/320) |
| `risk_auditor` / validation | `validated_signal_gate.v1` + `research_gate_service`(`GateResult`) + `research_backtest_parser` | §6 참조 |
| run 영속화 | `research_run` / `research_pipeline_service`(ROB-112 `ResearchSession`/`StageAnalysis`) | 이미 존재 |

### 5.3 `alpha_bench` → factor research spike

Vibe alpha_bench는 wide OHLCV 패널 위에서 IC/IR/positive-ratio를 계산하고 alpha를 alive/reversed/dead로 분류(`factors/bench_runner.py::categorise`). auto_trader 적용은:
- **읽기 전용 spike**: 기존 OHLCV 캐시 → wide panel 조립 → IC 계산. 어떤 점수도 `/invest/screener` 프로덕션 점수에 *반영하지 않는다*(별도 이슈로 분리).
- 가치: 스크리너 후보 점수의 **future evidence**(IC가 양인 팩터인지 사후 검증).

---

## 6. Backtest validation / run-card proposal (진하게)

### 6.1 현재 자산 vs Vibe — 정밀 대조

| 검증 요소 | auto_trader `validated_signal_gate.v1` (`research/nautilus_scalping/validated_gate.py`) | Vibe `agent/backtest/validation.py` + `run_card.py` | 판정 |
| -- | -- | -- | -- |
| Walk-forward | **train/val/oos = 0.5/0.25/0.25, val-best param 선택 후 OOS 평가**(L86–137) | n_windows=5 **retrospective consistency만**(train/test 없음, L154–233) | auto_trader 우위 |
| Verdict/gate | **validated/not_validated/insufficient_data + reasons**(L177–203) | **없음**(raw stats만) | auto_trader 우위 |
| Baseline 비교 | micro_breakout + random_entry(L153–159) | 없음 | auto_trader 우위 |
| Overfit flags | low_trades / single_fold_edge / param_island(L161–175) | 없음 | auto_trader 우위 |
| Fee 처리 | **임의 fee에서 net 해석적 재계산**(REF_FEE_BPS=10, fee grid [10,7.5,5,2,0]) | 엔진 레이어에서 적용, validation은 net PnL 소비 | auto_trader 우위 |
| **Bootstrap Sharpe CI** | **없음** | `bootstrap_sharpe_ci` n=1000, 95% percentile, prob_positive, seed=42 (L97–143) | **Vibe가 더함 → ADOPT** |
| **Monte-Carlo permutation** | **없음** | `monte_carlo_test` PnL shuffle, p_value(sharpe/maxDD), n=1000 (L26–79) | **Vibe가 더함 → ADOPT** |
| **Hash trio** | **없음** | config/strategy/artifact SHA-256 (`run_card.py` L87–142) | **Vibe가 더함 → ADOPT** |
| **Markdown run-card** | GateReport JSON만(`results/rob320/meanrev.json`) | `run_card.json` + **`run_card.md`** 9개 섹션 (L145–200) | **Vibe가 더함 → ADOPT** |
| Deflated/Probabilistic Sharpe | 없음 | 없음 | 둘 다 없음 → SKIP(후속 고려) |
| Metrics | pf/expectancy/win_rate/mdd | + sortino/calmar/IR/benchmark/excess (15+) | ADAPT(필요한 것만) |

### 6.2 ROB-316/320 결론 위에 얹기 (보완①)

핵심: **추가 검증은 "새 edge를 찾자"가 아니다.** ROB-316은 30/20bps 스캘퍼가 수수료만으로 net-negative임을, ROB-320은 meanrev fade가 gross edge(PF 1.058)를 가져도 10bps taker fee에서 죽어 `not_validated`이고 micro-breakout이 XRP+BTC로 net-neg 일반화됨을 *이미 증명*했다. 따라서 bootstrap CI + MC permutation의 목적은:

- **이미 음수인 net 결과의 통계적 강건성 확인** — "net edge 없음"이 표본 노이즈가 아니라 통계적으로 유의하게 0 이하임을 보강.
- run-card는 *합격 도장*이 아니라 **fee-kill 결론의 감사 가능한 증거 아티팩트**.

### 6.3 제안 (ADOPT 상세 — 후속 이슈로 pre-spec, §9 F1)

`validated_gate.py`에 다음을 *추가*(기존 OOS/verdict/baseline/fee 머신은 유지):
1. `bootstrap_sharpe_ci(net_returns, n=1000, conf=0.95, seed)` — observed/ci_lower/ci_upper/prob_positive
2. `monte_carlo_permutation(trade_pnls, n=1000, seed)` — p_value_sharpe / p_value_maxdd
3. `run_card_hashes()` — candidate config(sorted-key JSON) / strategy module / result artifact의 sha256
4. `write_run_card(report, out_dir)` — `run_card.json` + `run_card.md`(net-after-fee 강조). 위치: `research/nautilus_scalping/results/<task>/run_card.{json,md}`
5. verdict 확장: 기존 게이트 통과 + bootstrap `ci_upper < 0`(또는 `prob_positive` 낮음) → "net edge 없음"을 **통계적으로** 확정하는 reason 추가.

**경계:** 전부 `research/nautilus_scalping/`(import-guarded venv) 내부. 프로덕션 `research_gate_service.GateResult` 변경은 별도 이슈(§9 F-stub).

---

## 7. Report-stage proposal (얇게 — ROB-287/301/318이 소유)

이 영역은 이미 ROB-287(Hermes 생성)/ROB-301(symbol intermediate)/ROB-318(audit+bugfix)이 소유하므로 **중복 설계하지 않는다.** Vibe에서 가져올 것은 *한 가지 패턴*뿐:

- **Run-card를 citation/evidence로 연결:** Vibe run-card의 `data_sources` + `reproducibility.{config_hash,strategy_hash}` + `artifacts[].sha256` 패턴을, final report가 각 stage artifact를 **출처+해시로 인용**하는 방식에 적용. 즉 dimension/symbol intermediate report는 자신이 소비한 evidence(snapshot id, run-card hash)를 명시.
- **Hermes 경계 재확인(보완②):** stage 간 LLM reasoning은 Hermes가 out-of-process로 수행. auto_trader는 결정적 evidence + persistence만 제공. Vibe committee runtime은 이 경계를 넘지 않는다.

`/invest/screener` 점수나 프로덕션 report 생성 로직 변경은 **이 이슈 범위 밖**(후속 분리).

---

## 8. Risks and non-goals

**Non-goals (이 이슈에서 하지 않음):**
- Vibe-Trading을 runtime dependency로 추가하지 않는다.
- Vibe MCP/API/server를 프로덕션 Hermes/auto_trader에 연결하지 않는다.
- broker/order/watch/order-intent mutation 금지. KIS/Upbit/Binance 실주문 금지.
- 프로덕션 DB backfill 금지. scheduler/Prefect 배포 생성·활성화 금지.
- secrets/API key **값** 문서화 금지(이름만: `TUSHARE_TOKEN`/`FUTU_HOST`/`FUTU_PORT`/`CCXT_EXCHANGE` 등).
- 외부 사이트 scraping 구현 금지(소스 후보/ToS 리스크만 정리).
- `/invest/screener` 점수·프로덕션 report 로직 변경은 후속 이슈.

**Risks:**
- **약관 리스크(보완④):** Vibe MIT ≠ 데이터 소스 약관. yfinance(비공식·상업적 사용 불명확), DuckDuckGo(scraping 성격), tushare/futu(유료·중국전용), SP500 universe(survivorship bias). 우리는 KR/US/crypto 시장 범위 내 기존 소스만 사용하므로 대부분 회피된다.
- **stale clone:** §0의 HEAD 해시로 라인 번호 대조.
- **검증 over-claim 위험:** run-card가 "합격" 신호로 오해되지 않게, net-after-fee 기준과 ROB-316/320 결론을 run-card에 명시.

---

## 9. Suggested follow-up issues / PR slices

### 고가치 pre-spec (바로 Linear에 붙일 수 있음)

**F1 — `validated_signal_gate`에 bootstrap CI + MC permutation + run-card 추가**
- 범위: `research/nautilus_scalping/validated_gate.py`에 `bootstrap_sharpe_ci` / `monte_carlo_permutation` / `run_card_hashes` / `write_run_card` 추가. 기존 OOS/verdict/baseline/fee 머신 유지.
- Acceptance criteria:
  - [ ] `bootstrap_sharpe_ci(n=1000, conf=0.95, seed)` → observed/ci_lower/ci_upper/median/prob_positive, seeded 재현 가능
  - [ ] `monte_carlo_permutation(n=1000, seed)` → p_value_sharpe, p_value_maxdd (PnL shuffle, 단측)
  - [ ] config/strategy/artifact SHA-256 (sorted-key JSON / 파일 내용)
  - [ ] `run_card.json` + `run_card.md`를 `results/<task>/`에 출력, net-after-fee와 ROB-316/320 결론 명시
  - [ ] verdict에 "net edge 통계적으로 0 이하" reason 추가
  - [ ] `research/nautilus_scalping/` 밖 변경 없음, 새 import guard 위반 없음, 단위 테스트(seed 고정) 포함
  - [ ] 프로덕션 `research_gate_service` 변경 없음

**F2 — Fundamentals PIT 패널 (collectable_with_config)**
- 범위: KIS fundamental + `market_events`(DART/finnhub)에서 point-in-time 재무 필드를 wide-panel로 조립하는 **읽기 전용** 유틸 + 스키마. Vibe `tushare_fundamentals`의 `merge_asof(direction="backward")` PIT 패턴 참고(소스는 우리 것).
- Acceptance criteria:
  - [ ] `TUSHARE_TOKEN` 등 외부 유료 소스 미사용(이름만 문서 참조)
  - [ ] PIT 누수 없음(announcement date 이후에만 노출) 테스트
  - [ ] DB write는 기존 repository 경유, 브로커/주문 mutation 없음
  - [ ] dry-run 기본, 프로덕션 backfill 없음

### Stub (제목+이유만)

- **F3** — alpha_bench read-only IC spike (factor research, 점수 미반영). *이유: 스크리너 후보의 future evidence.*
- **F4** — Run-card hash를 dimension/symbol intermediate report의 citation/evidence로 연결. *이유: 재현성 있는 evidence 추적.*
- **F5** — Cross-sectional wide-panel 조립 유틸(OHLCV 캐시 → panel). *이유: F3의 입력.*
- **F6** — `research_gate_service.GateResult`에 통계 검증 필드 productionize (F1 검증 후). *이유: research-side 검증을 프로덕션 게이트로.*
- **F7** — Deflated/Probabilistic Sharpe 평가(둘 다 미보유). *이유: 다중비교 보정.*
- **F8** — Trade-journal 진단을 `*_order_ledger` 위에서 재현(Vibe shadow account 패턴). *이유: 실거래 행동 진단.*

---

## 10. Claude / Hermes handoff notes

- **In-process LLM 금지:** `/invest/reports` 스테이지 파이프라인은 Gemini/OpenAI/Grok/Hermes를 in-process 호출하지 않는다(ROB-287, PR #898 static import guard가 `app/services/action_report/snapshot_backed/` + `app/services/investment_stages/` 전체에서 강제). Vibe `agent/src/swarm/runtime.py`의 in-process agent loop는 **프로덕션에 이식 금지**.
- **Hermes 경계:** LLM reasoning/composition은 Hermes out-of-process(pull → compose → push). auto_trader는 결정적 evidence + persistence만 제공.
- **Sandbox 전용 조건:** Vibe runtime/MCP를 *실험*하려면 ① `/tmp` clone 격리, ② live trade execution 경로 없음(Vibe도 `risk_tier`로 거부), ③ 어떤 산출물도 프로덕션 DB/리포트에 자동 반영 금지, ④ 외부 데이터 소스는 우리 시장 범위(KR/US/crypto) 기존 소스로 대체.
- **검증 작업 격리:** F1 등 검증 코드는 `research/nautilus_scalping/`(import-guarded venv) 안에서만. 프로덕션 게이트 변경은 별도 이슈에서 검토 후.
- **다음 단계:** 이 문서 승인 → F1을 첫 구현 슬라이스로 분리(가장 명확한 ADOPT, 기존 자산에 순수 additive).
