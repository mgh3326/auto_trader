# ROB-449 / ROB-450 / ROB-452 MCP 도구 활성화 · 검증 런북

> **MCP_PROFILE (ROB-488)**: 크립토 전용 도구(`get_crypto_*`, `get_funding_rate`,
> `get_open_interest`, `get_long_short_ratio`, `get_kimchi_premium`, `get_upbit_*`,
> `get_crypto_fear_greed`(구 `get_fear_greed_index`))는 default surface에서 분리되어
> `MCP_PROFILE=crypto` 서버에서만 등록된다. 크립토 검증/운영 세션은 crypto 프로파일을
> 사용할 것 (generic 주문 도구 + `live_reconcile_orders`는 crypto 프로파일에 포함).

## 개요

이 런북은 세 개의 auto_trader MCP feature에 대한 **operator 활성화 + 검증** 절차만 다룹니다.

| 이슈 | feature | 머지 PR |
|------|---------|---------|
| ROB-449 | `get_retail_sentiment` (Naver 종목토론 aggregate) | #1184, #1196 |
| ROB-450 | `get_cost_basis_distribution` (self-OHLCV VPVR 평단분포 추정) | #1190 |
| ROB-452 | crypto 4-tool (`get_crypto_market_regime` / `get_crypto_catalysts` / `get_crypto_order_flow` / `get_crypto_social`) + insight Prefect flow | #1172, #1173, #1175, #1182 |

세 feature 모두 **코드 구현 완료 + `main` 머지 완료** 상태이며 추가 코드 변경은 없습니다. 모든 도구는 read-only(브로커/주문/계좌 mutation 없음)이고 fail-open(외부 소스 장애 시 구조화된 error/missing/null로 graceful degrade)입니다. 이 문서는 (a) 로컬에서 실행 가능한 hermetic 코드 검증과 (b) operator만 수행하는 라이브 스모크/게이트 플립을 명확히 구분합니다.

> **DEFERRED**: ROB-450 commit 메시지에 언급된 `get_execution_strength`(KR 체결강도, 신규 KIS TR `FHPST01060000` 필요)는 **ROB-462로 이연(DEFERRED)**되었으며 이 활성화의 범위가 **아닙니다**. 이 런북의 어떤 단계도 `get_execution_strength`를 등록/활성화하지 않습니다.

---

## 검증 증거 (이 런북 작성 시 로컬 실행, `origin/main` 기준)

작성 시점에 아래 hermetic 검증을 모두 직접 실행해 green을 확인했습니다(외부 네트워크 없음):

| 검증 | 명령 | 결과 |
|------|------|------|
| 6개 도구 + flow 단위테스트 | `uv run pytest -p no:xdist tests/test_retail_sentiment_tool.py tests/test_mcp_cost_basis_distribution.py tests/test_crypto_market_regime_tool.py tests/test_crypto_catalysts_tool.py tests/test_crypto_order_flow_social.py tests/test_invest_crypto_insight_snapshots_flow.py` | **35 passed, 1 skipped** |
| MCP 등록 부팅 스모크 (`on_duplicate="error"`, 이름충돌 0) | `uv run pytest -p no:xdist tests/test_mcp_tool_registration_boot.py` | **3 passed** |
| 6개 도구 등록 surface 해석 | `register_all_tools` → `mcp.get_tool(name)` × 6 | **6/6 RESOLVE** (params 시그니처 일치) |
| 6개 핸들러 import 해석 | `from app.mcp_server.tooling.fundamentals.* import handle_*` | **6/6 OK** |

- 1 skipped = `test_invest_crypto_insight_snapshots_flow.py::test_insight_flow_imports_cleanly` (Prefect import 환경 가드, 환경 조건부 skip — 회귀 아님).
- 등록 surface 해석 결과(파라미터):
  - `get_retail_sentiment` → `market, symbol, window`
  - `get_cost_basis_distribution` → `buckets, market, symbol`
  - `get_crypto_market_regime` → `(없음)`
  - `get_crypto_catalysts` → `days, symbol`
  - `get_crypto_order_flow` → `count, symbol`
  - `get_crypto_social` → `symbol`

---

## 공통 전제

| 항목 | 적용 feature | 내용 |
|------|-------------|------|
| **프로덕션 재배포** | 449, 450, 452 모두 | main에 머지된 코드의 컨테이너 재배포가 선행되어야 함(표준 CI/CD: `docker build → push → pull → restart`). 재배포 없이는 어떤 도구도 노출되지 않음. |
| **`RETAIL_SENTIMENT_LIVE_ENABLED`** | 449 전용 | 기본 `false` (`app/core/config.py:335`). ToS 보호용 default-off 게이트. operator가 `true`로 플립해야 Naver 라이브 fetch 발생. 미설정 시 `status='disabled'` honest scaffold 반환. |
| **`INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED`** | 452 전용 (Prefect flow) | 기본 `false` (dry-run). KR/US 스크리너 스냅샷 flow와 **공유**하는 commit 게이트. Prefect worker env에서 `true`로 설정해야 `crypto_insight_snapshots` 영속화. **MCP 도구 자체에는 게이트 없음** — 이 env는 Prefect flow의 write만 제어. |
| **게이트 불필요** | 450, 452 도구 | `get_cost_basis_distribution` + crypto 4-tool은 환경변수 게이트가 **없음**. 재배포 즉시 라이브(read-only, fail-open). |

**env var 주의**: 모든 env var 이름은 case-sensitive(전부 대문자/언더스코어). Pydantic V2가 env에서 자동 변환하며 `true`/`false`/`1`/`0` 값을 사용. 게이트 변경 후 MCP 서버 컨테이너 재시작/설정 리로드 필요.

**라이브 스모크 = handler 프로브**: 이 MCP 서버는 streamable-http transport이며 임의의 REST 경로(`/api/mcp`, `/tool/...`)가 **없습니다**. operator 라이브 검증은 프로덕션 런타임(env 세팅 완료)에서 핸들러를 직접 호출하는 `uv run python` 프로브로 수행합니다(아래 각 섹션). 정식 MCP 클라이언트(Claude)에서 도구를 호출해도 동일.

---

## ROB-449 — get_retail_sentiment (Naver 종목토론 aggregate retail-activity signal, KR, operator-gated)

### 도구 목록

| 이름 | 시그니처 | 게이트 | 기본 동작 |
|------|----------|--------|-----------|
| `get_retail_sentiment` | `(symbol: str, market: str = 'kr', window: str = '1d') -> dict` | `RETAIL_SENTIMENT_LIVE_ENABLED` (default `false`) | `status='disabled'` honest scaffold 반환 (fetch 전, fail-open) |

- 핸들러: `app/mcp_server/tooling/fundamentals/_retail_sentiment.py:34` (`handle_get_retail_sentiment`)
- 등록: `app/mcp_server/tooling/fundamentals_handlers.py:344` (`@mcp.tool(name='get_retail_sentiment')`)
- aggregate 카운트만 반환: `activity_rank`, `post_count`, `comment_count`, `reaction_count`, `overheat_flag`. raw 본문/제목/작성자 절대 노출 안 함.
- top-N에 없는 심볼 → `status='not_ranked'` (`activity_rank=None`, `post_count` 키 없음 — missing ≠ zero). overheat flag는 `rank <= 5`에서 발화.

### 활성화 단계 (operator)

1. **배포**: 머지된 코드(#1184 + #1196)를 operator 환경에 재배포(표준 CI/CD).
2. **ToS 검토**: 활성화 전 Naver 종목토론 엔드포인트 ToS 검토(필수, 아래 안전 경계 참고).
3. **게이트 플립**: 런타임 환경에 env var 설정 (`.env` / `docker-compose.yml` / K8s secret 중 택1).
   ```bash
   RETAIL_SENTIMENT_LIVE_ENABLED=true
   ```
   설정은 매 호출마다 읽힘(`_retail_sentiment.py:59`)이지만, env 반영을 위해 MCP 서버 컨테이너 재시작 권장.
4. **라이브 스모크 검증** (operator-only, 외부 네트워크 — 아래 "라이브 스모크" 참고).

### 검증 명령

**로컬 코드 검증 — hermetic, 외부 네트워크 없음 (에이전트 실행 가능):**

```bash
# 1. default-off: 게이트 false면 status='disabled' honest scaffold
uv run pytest -p no:xdist tests/test_retail_sentiment_tool.py::test_disabled_by_default -v
#   기대: PASSED — status='disabled', note에 'RETAIL_SENTIMENT_LIVE_ENABLED' 안내

# 2. ranked 심볼: ranking.state='fresh'일 때 카운트 파싱 + overheat(rank<=5) 발화
uv run pytest -p no:xdist tests/test_retail_sentiment_tool.py::test_ranked_symbol_returns_counts_and_overheat -v
#   기대: PASSED — status='ok', activity_rank/post_count 채워짐, overheat_flag True

# 3. not_ranked: top-N 부재 → activity_rank=None(0 아님), post_count 키 없음
uv run pytest -p no:xdist tests/test_retail_sentiment_tool.py::test_not_ranked_is_not_zero -v
#   기대: PASSED — status='not_ranked', activity_rank is None, 'post_count' 키 부재

# 4. fail-open: fetch가 state='unavailable' 반환 시 핸들러도 unavailable 전파
uv run pytest -p no:xdist tests/test_retail_sentiment_tool.py::test_unavailable_when_fetch_degrades -v
#   기대: PASSED — status='unavailable'

# 5. 라이브 shape 파싱(#1196): contents[] → code/rank/post_count; raw posts/title/content 절대 미노출
uv run pytest -p no:xdist tests/test_retail_sentiment_tool.py::test_extract_live_contents_shape -v
#   기대: PASSED — sevenDayStats에서 post_count 추출, 'posts'/'title'/'content' 전 항목 부재

# 6. fetcher fail-open: 네트워크/파싱 에러 시 state='unavailable' + 빈 items + errorReason
uv run pytest -p no:xdist tests/test_retail_sentiment_tool.py::test_fetch_rankings_fail_open -v
#   기대: PASSED — state='unavailable', items==[], errorReason 채워짐

# 7. KR only: 비-KR 심볼(AAPL)은 ValueError
uv run pytest -p no:xdist tests/test_retail_sentiment_tool.py::test_kr_only -v
#   기대: PASSED — ValueError ('Korean' 관련 메시지)

# 8. 전체 스위트 (8 tests, hermetic)
uv run pytest -p no:xdist tests/test_retail_sentiment_tool.py -v
#   기대: 8 passed
```

**라이브 스모크 — operator-only (외부 네트워크, Naver 의존, 프로덕션 런타임에서 실행):**

```bash
# 게이트 ON + env 세팅된 런타임에서 핸들러 직접 프로브
RETAIL_SENTIMENT_LIVE_ENABLED=true uv run python -c "
import asyncio
from app.mcp_server.tooling.fundamentals._retail_sentiment import handle_get_retail_sentiment
print(asyncio.run(handle_get_retail_sentiment('005930')))
"
#   기대: status='ok' 또는 'not_ranked'(또는 소스 다운 시 'unavailable');
#         aggregate 필드(activity_rank/post_count/comment_count/reaction_count/overheat_flag) 존재;
#         raw text/title/content/author 키 없음.
```

> **참고**: #1196 머지 전에는 라이브 응답이 top-level `contents[]` shape여서 `items_count=0`이 나왔습니다.
> 활성화 전, fetcher가 `items_count>0`을 반환하는지(패치 동작) 1회 확인 권장:
> `uv run python -c "import asyncio; from app.services.naver_finance.discussion import fetch_discussion_rankings; print(asyncio.run(fetch_discussion_rankings(size=20)))"`

### 롤백

1. 런타임 환경에서 `RETAIL_SENTIMENT_LIVE_ENABLED=false` 설정. 핸들러가 Naver fetch 없이 `status='disabled'`로 복귀.
2. MCP 서버 컨테이너 재시작/설정 리로드(매 호출 읽히지만 clean state 보장).
3. 롤백 검증: 위 handler 프로브 → `status='disabled'` + note 메시지 확인.

### 안전 경계 / 리스크

- **AGGREGATE-ONLY 계약 강제**: raw 본문/제목/작성자/닉네임 절대 미노출. `_extract_ranked_items`(`app/services/naver_finance/discussion.py`)는 `{code, rank, post_count, comment_count, reaction_count}`만 추출. ROB-199 `StockDetailDiscussionSignal` validator(`app/schemas/invest_stock_detail.py:213`)가 detail 레벨에서 별도 차단.
- **FAIL-OPEN**: 네트워크 에러/Naver 다운/JSON 파싱 에러/타임아웃이 핸들러를 crash시키지 않음. `fetch_discussion_rankings`가 Exception을 catch하여 `state='unavailable'` + 빈 items + errorReason 반환.
- **비공식 엔드포인트 리스크**: Naver 종목토론 API는 문서화되지 않음. ToS 준수는 (a) aggregate 카운트만(강제), (b) rate limit(size 20, 캐시 TTL ≈ 10분), (c) user-agent에 의존. **operator는 활성화 전 ToS 검토 필요.** Naver 차단/레이아웃 변경 시 graceful degrade.
- **MISSING ≠ ZERO**: top-N 부재 = `status='not_ranked'`(zero-count 아님). per-symbol zero 쿼리는 미구현(top-N market-wide만 가능).
- **DEFERRED 필드**: `momentum`/`bull_bear_lean`/`top_themes`는 v1에서 의도적 미산출(NLP classifier 필요, follow-up). 사용자는 null이 아닌 명시적 미산출 상태 확인.
- **KR ONLY**: 6자리 KR 종목코드(`005930`)만 허용, US ticker/crypto/ISIN 거부.
- **No mutation / no keys**: read-only, KIS/Upbit API 키 불필요.
- **open risk**: Naver 엔드포인트 변동성(레이아웃/필드 무통보 변경), ToS 불명(법무 검토 권장), `not_ranked` 모호성(zero vs off-list vs error 구분 불가 → 수동 fallback), 히스토리 스냅샷 없음, rate limit self-imposed(런타임 knob 없음 — `_CACHE_TTL_SECONDS` 변경 시 재배포 필요).

---

## ROB-450 — get_cost_basis_distribution (self-OHLCV VPVR 평단분포 estimate)

### 도구 목록

| 이름 | 시그니처 | 게이트 | 기본 동작 |
|------|----------|--------|-----------|
| `get_cost_basis_distribution` | `(symbol: str, market: str \| None = None, buckets: int = 10) -> dict` | **없음** | enabled, 배포 즉시 라이브 (예외 시 fail-open 구조화 error) |

- 등록 wrapper: `app/mcp_server/tooling/fundamentals_handlers.py:423` (무조건 등록, env var 게이트 없음)
- impl: `app/mcp_server/tooling/fundamentals/_cost_basis_distribution.py:32` (`get_cost_basis_distribution_impl(symbol, market, buckets, preloaded_df=None)`)
- `_fetch_ohlcv_for_volume_profile`(kr/us/crypto) + `_calculate_volume_profile` 재사용. buckets는 `[2,100]`로 clamp.
- 시장 지원: kr(KIS), us(Yahoo), crypto(Upbit). `estimate=True` + `method='vpvr_self_ohlcv'`(정확한 holder cost 파일 아님 — license-clean proxy). `vwap_estimate`, `pct_holders_underwater/in_profit`, `heaviest_bucket` 계산. US는 `fetch_us_live_last_price`로 fresh price overlay(실패 시 `current_price_stale=True`).

> **DEFERRED**: `execution_strength`(체결강도)는 KIS per-tick TR `FHPST01060000` 부재로 이연 → **ROB-462**. crypto taker-flow는 ROB-452 `get_crypto_order_flow`가 커버. 이 도구는 체결강도를 반환하지 않음.

### 활성화 단계 (operator)

1. **배포**: 머지된 PR #1190을 재배포(표준 CI/CD).
2. **자동 등록**: MCP 서버 부팅 시 `get_cost_basis_distribution`이 무조건 등록됨(게이트 없음).
3. **즉시 라이브**: 배포 직후 추가 게이트 플립/Prefect 단계 없이 도구 활성.

### 검증 명령

**로컬 코드 검증 — hermetic, 외부 네트워크 없음 (에이전트 실행 가능):**

```bash
# 1. 코어 VPVR: 버킷 합 ~100%, underwater+in_profit ~100%, 필수 필드 존재
uv run pytest -p no:xdist tests/test_mcp_cost_basis_distribution.py::test_cost_basis_estimate_shape_and_sums -v
#   기대: PASSED — estimate=True, method='vpvr_self_ohlcv', 버킷 share 합 ~100%,
#         underwater+in_profit ~100%, heaviest_bucket/vwap_estimate 채워짐

# 2. clamp: buckets=1 입력 → 2 출력 (min=2, max=100)
uv run pytest -p no:xdist tests/test_mcp_cost_basis_distribution.py::test_buckets_clamped -v
#   기대: PASSED — 출력 버킷 수가 [2,100]로 clamp

# 3. fail-open: 빈 OHLCV → error dict (예외 raise 아님)
uv run pytest -p no:xdist tests/test_mcp_cost_basis_distribution.py::test_empty_data_fail_open -v
#   기대: PASSED — 'error' in out

# 4. 입력 검증: 빈 symbol → ValueError
uv run pytest -p no:xdist tests/test_mcp_cost_basis_distribution.py::test_requires_symbol -v
#   기대: PASSED — symbol 누락 시 ValueError

# 5. 전체 스위트 (4 tests)
uv run pytest -p no:xdist tests/test_mcp_cost_basis_distribution.py -v
#   기대: 4 passed
```

**라이브 스모크 — operator-only (외부 네트워크, KIS/Yahoo/Upbit 의존, 프로덕션 런타임에서 실행):**

```bash
# KR equity (KIS) / US (Yahoo) / crypto (Upbit) — handler impl 직접 프로브
uv run python -c "
import asyncio
from app.mcp_server.tooling.fundamentals._cost_basis_distribution import get_cost_basis_distribution_impl
for sym, mkt in [('005930','kr'), ('AAPL','us'), ('KRW-BTC', None)]:
    r = asyncio.run(get_cost_basis_distribution_impl(sym, mkt, 10))
    print(sym, '->', {k: r.get(k) for k in ('instrument_type','source','estimate','method','vwap_estimate','pct_holders_underwater','pct_holders_in_profit')})
"
#   기대: estimate=True, method='vpvr_self_ohlcv';
#         005930→source='kis'/instrument_type='equity_kr', AAPL→source='yahoo'/'equity_us',
#         KRW-BTC→source='upbit'/'crypto'; pct_holders_underwater + pct_holders_in_profit ≈ 100.
```

### 롤백

1. 도구는 read-only·게이트 없음 → 런타임 롤백 불필요. 완전 제거 시 PR #1190(commit `5a5fd6b4`) revert 후 재배포.
2. 재배포 후 도구 즉시 사용 불가 (clear할 캐시/게이트 state 없음).

### 안전 경계 / 리스크

- **read-only**: 브로커/주문/포지션 mutation 없음. OHLCV read + 계산만.
- **fail-open**: 예외는 구조화된 error dict 반환(raise 아님).
- **estimate=True honest label**: `method='vpvr_self_ohlcv'`는 심볼 자체 OHLCV proxy(정확한 holder cost 파일 아님). license-clean, Naver 위젯 스크레이프 ToS 리스크 회피.
- **aggregate-only**: 버킷/퍼센트는 파생 통계, 개별 holder 포지션 아님.
- **120일 trailing window**(`_PERIOD_DAYS`): 내부 상수, 사용자 설정 불가. **buckets [2,100]** clamp(범위 밖 입력 silent clamp).
- **게이팅 금지**: cost-basis는 보조/confirm 신호 — 리포트 게이팅에 쓰지 말 것.
- **open risk**: OHLCV staleness(as_of/period_days 메타로 완화), upstream 벤더 변동(KIS 소스), Upbit rate limit(tool 내 backoff 없음), zero-range candle binning artifact, US live price 엔드포인트 실패 시 stale=True로 투명 degrade, market=None일 때 market-type 추론 부정확(`market='kr'/'us'` 명시 권장).

---

## ROB-452 — Crypto data gap (regime / catalysts / order_flow / social + insight Prefect flow)

### 도구 목록

| 이름 | 시그니처 | 게이트 | 기본 동작 |
|------|----------|--------|-----------|
| `get_crypto_market_regime` | `() -> dict` | 없음 | read-only, DB 불가 시 fail-open 구조화 error |
| `get_crypto_catalysts` | `(symbol: str \| None = None, days: int = 14) -> dict` | 없음 | read-only, per-source fail-open (3 블록 독립 집계) |
| `get_crypto_order_flow` | `(symbol: str, count: int = 200) -> dict` | 없음 | read-only, fail-open (Upbit `/v1/trades/ticks` 불가 시 구조화 error) |
| `get_crypto_social` | `(symbol: str) -> dict` | 없음 | read-only, fail-open (CoinGecko 불가 시 구조화 error) |

- 핸들러: `_crypto_regime.py:77`, `_crypto_catalysts.py:92`, `_crypto.py:142`(order_flow), `_crypto.py:208`(social)
- 등록: `fundamentals_handlers.py:290`(regime), `:304`(catalysts), `:320`(order_flow), `:332`(social)
- **regime**: `crypto_insight_snapshots` 테이블 read. 필드별 독립 fresh/stale/missing/disabled. 기본 `fng`만 populate; tvl/stablecoin/breadth는 snapshot job에서 operator가 provider 활성화해야 채워짐. `aggregate_oi`는 disabled PoC(coinglass, API 키 필요). stale 임계 ≈ 24h.
- **catalysts**: `token_unlocks`(Tokenomist PoC stub → `state='disabled'`), `upbit_notices`(keyless, days 윈도), `market_warnings`(Upbit, CAUTION만). symbol = 마켓 스코프(예 `XRP`), None = market-wide.
- **order_flow**: Upbit `/v1/trades/ticks`(public, keyless). volume-weighted `taker_buy_ratio`/`taker_sell_ratio`/`net`([-1,1]). symbol → `KRW-{BASE}` 정규화. count `[1,500]` cap. 사용 가능 tick 없으면 None(0 아님).
- **social**: CoinGecko 커뮤니티/개발자 신호(`sentiment_votes_up_pct`, `twitter_followers`, `reddit_subscribers`, `dev_commits_4w`). 데이터 없으면 null(에러 아님).

### 활성화 단계 (operator)

1. **프로덕션 재배포** (표준 CI/CD). **deploy 시점 플립할 feature flag 없음** — 4개 도구 전부 read-only, 도구 자체 게이트 없음.
2. **Prefect flow 등록** (`invest_crypto_insight_snapshots`): `robin-prefect-automations` repo에 deployment 등록, 스케줄 권장 **09:20 KST**(daily refresh), **PAUSED by default**. (crypto screener #1156 / KR fundamentals #1163 동형 패턴 — `docs/runbooks/invest-screener-snapshots.md` 참고.)
3. **commit 게이트 확인**: Prefect worker 환경이 `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED`(default `false`)에 접근 가능한지 확인. operator가 worker env에서 `true`로 플립해야 flow가 스냅샷 영속화.
   ```bash
   # Prefect worker env
   INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED=true
   ```
4. **deployment unpause**: Prefect UI 또는 CLI로 `crypto_insight_snapshots` deployment 활성화.
5. **스케줄 실행 검증**: 09:20 KST 실행 로그 모니터. 게이트 `true`면 commit, `false`면 dry-run 로그 확인.
6. **(선택) provider 확장**: tvl/stablecoin/breadth/oi 채우려면 snapshot 빌드 job providers에 `defillama tradingview`(+coinglass API key) 추가. 미설정 시 해당 필드는 honest `missing`/`disabled`.

### 검증 명령

**로컬 코드 검증 — hermetic, 외부 네트워크 없음 (에이전트 실행 가능):**

```bash
# regime: fng fresh / 나머지 missing / aggregate_oi disabled
uv run pytest -p no:xdist tests/test_crypto_market_regime_tool.py::test_fng_fresh_others_missing_oi_disabled -v
#   기대: PASSED — regime 블록 구조 정확, source='crypto_insight_snapshots'

# regime fail-open: DB 에러 → 구조화 error payload (raise 아님)
uv run pytest -p no:xdist tests/test_crypto_market_regime_tool.py::test_db_error_returns_structured_payload -v
#   기대: PASSED — 'error' key 존재, 예외 미발생

# catalysts: 3 블록 독립 집계 (token_unlocks disabled, market_warnings CAUTION-only)
uv run pytest -p no:xdist tests/test_crypto_catalysts_tool.py::test_catalysts_aggregates_three_blocks -v
#   기대: PASSED — 3 소스 존재, token_unlocks state='disabled', warnings CAUTION만

# catalysts per-source fail-open: 한 소스 예외가 도구를 죽이지 않음
uv run pytest -p no:xdist tests/test_crypto_catalysts_tool.py::test_catalysts_fail_open_per_source -v
#   기대: PASSED — 소스별 독립 'unavailable', 완전한 payload 반환

# order_flow: volume-weighted ratio + 심볼 정규화 + count cap
uv run pytest -p no:xdist tests/test_crypto_order_flow_social.py::test_order_flow_volume_weighted -v
#   기대: PASSED — volume-weighted taker_buy/sell_ratio/net, source='upbit', symbol KRW-정규화

# order_flow: tick 없으면 None (0 아님)
uv run pytest -p no:xdist tests/test_crypto_order_flow_social.py::test_order_flow_empty_is_none -v
#   기대: PASSED — ratio 필드 None

# social: CoinGecko 필드 매핑 + 결측 graceful degrade
uv run pytest -p no:xdist tests/test_crypto_order_flow_social.py::test_social_maps_fields tests/test_crypto_order_flow_social.py::test_social_degrades_on_missing_blocks -v
#   기대: PASSED — 필드 매핑 정확 / 결측 시 null degrade(에러 없음)

# Prefect flow scaffold (게이트 와이어링 + 등록 deferred)
uv run pytest -p no:xdist tests/test_invest_crypto_insight_snapshots_flow.py -v
#   기대: 6 passed, 1 skipped (skip=test_insight_flow_imports_cleanly, Prefect import 환경 가드)

# crypto 4파일 통합
uv run pytest -p no:xdist tests/test_crypto_market_regime_tool.py tests/test_crypto_catalysts_tool.py tests/test_crypto_order_flow_social.py tests/test_invest_crypto_insight_snapshots_flow.py
#   기대: 23 passed, 1 skipped (regime 4 + catalysts 6 + order_flow_social 7 + flow 6+1skip)
```

**라이브 스모크 — operator-only (외부 네트워크 / DB / Prefect 의존, 프로덕션 런타임에서 실행):**

```bash
# regime(DB) + order_flow(Upbit) + social(CoinGecko) handler 프로브
uv run python -c "
import asyncio
from app.mcp_server.tooling.fundamentals._crypto_regime import handle_get_crypto_market_regime
from app.mcp_server.tooling.fundamentals._crypto import handle_get_crypto_order_flow, handle_get_crypto_social
print('regime    ->', asyncio.run(handle_get_crypto_market_regime()))
print('order_flow->', asyncio.run(handle_get_crypto_order_flow('KRW-BTC')))
print('social    ->', asyncio.run(handle_get_crypto_social('bitcoin')))
"
#   기대: regime=fng fresh(tvl/stablecoin/breadth는 provider 설정 따라 fresh/disabled/missing),
#         order_flow=Upbit taker ratio(또는 tick 없으면 None), social=CoinGecko 신호(또는 null degrade).
# + Prefect 09:20 KST 실행 로그: 게이트 true=commit / false=dry-run 확인.
```

### 롤백

1. **Prefect flow 비활성화**: Prefect UI 또는 `prefect deployment pause`로 deployment pause. flow run 중단, 기존 스냅샷은 DB에 유지(read-only).
2. **dry-run 유지하며 영속화만 차단**: Prefect worker env에서 `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED=false`(default). 다음 run은 `dry_run=True`로 write 없음.
3. **4개 MCP 도구**: 롤백 불필요(read-only, write 없음). 완전 제거 시 `fundamentals_handlers.py`의 해당 `@mcp.tool` 블록 제거 후 재배포.

### 안전 경계 / 리스크

- **4개 도구 전부 read-only**: 브로커/주문 mutation 없음. credential 불필요(public API: Upbit `/v1/trades/ticks`, CoinGecko, `crypto_insight_snapshots` DB read).
- **fail-open 설계**: 외부 소스(Upbit/CoinGecko/DB) 불가/불완전 시 구조화 error/missing/null 반환, 절대 raise 안 함.
- **snapshot 스키마**: row별 독립 fresh/stale/missing state. 글로벌 'table stale' 룰 없음.
- **`INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED` 게이트**: default `false`(dry-run). KR/US 스크리너 스냅샷 flow와 공유. operator가 명시적으로 `true` 플립해야 영속화.
- **order_flow 규약**: `taker_buy_ratio = BID / (BID + ASK)`(repo 규약). BID=taker buy, ASK=taker sell. volume-weighted(trade-count 아님). source=`upbit`(Binance 아님).
- **PoC stub**: Tokenomist token-unlocks(`state='disabled'`, no rows — API 키 adapter 필요, ROB-452 범위 밖), Coinglass aggregate_oi(`state='disabled'`, API 키 필요).
- **scope-out(무료 공개소스 부재)**: whale/on-chain netflow·거래소 reserve, Upbit 개인/법인 매매동향, per-coin 정식 Fear&Greed, crypto retail 토론감성 — 전부 유료 only 또는 부재. 이슈에 명시.
- **게이팅 금지**: order_flow/social은 보조 confirm 신호 — 리포트 게이팅에 쓰지 말 것.
- **open risk**: Upbit/CoinGecko rate-limit(루프 호출 시 backoff/캐시 필요), snapshot job 실패 시 tvl/stablecoin/breadth → 'missing'(operator는 job 로그/Prefect 모니터 필수), Prefect 등록 deferred(미등록 시 daily 미실행 → regime 필드 'missing'), commit env 미설정 시 dry-run(영속화 안 됨).

---

## 검증 체크리스트

### (a) 로컬 코드 검증 [에이전트가 실행 가능 — hermetic, 외부 네트워크 없음]

```
공통:
[ ] uv run pytest -p no:xdist tests/test_mcp_tool_registration_boot.py   # 3 passed (on_duplicate=error, 이름충돌 0)

ROB-449:  (8 passed)
[ ] uv run pytest -p no:xdist tests/test_retail_sentiment_tool.py -v

ROB-450:  (4 passed)
[ ] uv run pytest -p no:xdist tests/test_mcp_cost_basis_distribution.py -v

ROB-452:  (23 passed, 1 skipped)
[ ] uv run pytest -p no:xdist tests/test_crypto_market_regime_tool.py tests/test_crypto_catalysts_tool.py tests/test_crypto_order_flow_social.py tests/test_invest_crypto_insight_snapshots_flow.py

전체 한 번에:  (35 passed, 1 skipped)
[ ] uv run pytest -p no:xdist tests/test_retail_sentiment_tool.py tests/test_mcp_cost_basis_distribution.py tests/test_crypto_market_regime_tool.py tests/test_crypto_catalysts_tool.py tests/test_crypto_order_flow_social.py tests/test_invest_crypto_insight_snapshots_flow.py
```

### (b) operator 라이브 활성화 [operator-only — 외부 네트워크 / 게이트 / Prefect]

```
공통:
[ ] 세 feature 코드의 컨테이너 프로덕션 재배포 (표준 CI/CD)

ROB-449 (get_retail_sentiment):
[ ] Naver ToS 검토 완료 (활성화 전 필수)
[ ] (활성화 전) fetch_discussion_rankings(size=20) → items_count>0 확인 (#1196 패치 동작)
[ ] RETAIL_SENTIMENT_LIVE_ENABLED=true (런타임 env) + MCP 서버 재시작
[ ] 라이브 스모크: handle_get_retail_sentiment('005930') → status='ok'|'not_ranked', aggregate 필드 존재, raw 텍스트/제목/작성자 키 없음
[ ] (롤백 시) RETAIL_SENTIMENT_LIVE_ENABLED=false → status='disabled' 확인

ROB-450 (get_cost_basis_distribution):
[ ] 배포 즉시 라이브 (게이트 플립 불필요)
[ ] 라이브 스모크: get_cost_basis_distribution_impl('005930','kr',10) → estimate=True, method='vpvr_self_ohlcv', source='kis', underwater+in_profit≈100
[ ] (확인) get_execution_strength는 ROB-462로 DEFERRED — 이 활성화 범위 아님

ROB-452 (crypto 4-tool + Prefect flow):
[ ] 4개 MCP 도구는 배포 즉시 라이브 (게이트 플립 불필요)
[ ] robin-prefect-automations에 invest_crypto_insight_snapshots deployment 등록 (09:20 KST, PAUSED by default)
[ ] (영속화 활성화 시) INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED=true (Prefect worker env)
[ ] Prefect deployment unpause + 09:20 KST 실행 로그: commit(게이트 true) 또는 dry-run(false) 확인
[ ] post-deploy 스모크: handle_get_crypto_market_regime() → fng fresh, tvl/stablecoin/breadth fresh|disabled|missing
[ ] (선택) snapshot providers에 defillama/tradingview(+coinglass key) 추가 시 regime 필드 populate
[ ] (롤백 시) prefect deployment pause + INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED=false
```

---

> **DEFERRED 재확인**: `get_execution_strength`(ROB-450 KR 체결강도)는 신규 KIS TR `FHPST01060000` 부재로 **ROB-462로 이연**되었으며, 이 런북의 어떤 활성화 단계에도 포함되지 않습니다. crypto taker-flow가 필요하면 ROB-452 `get_crypto_order_flow`를 사용하십시오.
