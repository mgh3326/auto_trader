# ROB-434 — US market_valuation Finnhub fallback (field-fill) 설계

- **Linear**: ROB-434 umbrella `[screener] US non-DART fundamentals vendor — ROE/growth/dividend + 52w-high-date unblock` 의 **남은 deferred 조각** (Finnhub fallback). 신규 이슈 미생성 (Linear 쿼터 비상 240/250) → ROB-434로 직접 진행.
- **Priority**: Medium (umbrella), 이 조각 High 가치 (operator 라이브 블로커 직격)
- **날짜**: 2026-06-09
- **선행/완료**: ROB-440 (Phase 1, valuation ROE+52w via yfinance) · ROB-441 (Phase 2, fundamentals US) — 둘 다 Done·머지
- **PR**: 단독 PR (`feat(ROB-434): US valuation Finnhub fallback (field-fill)`)
- **벤더 결정 출처**: ROB-434 umbrella `/spec` D1 — "yfinance primary, Finnhub fallback (계약 문서화·구현 후속)". 이 설계가 그 "구현 후속"이다.

## 문제

operator 라이브 증거(2026-06-05): US valuation 스냅샷을 `scripts.build_market_valuation_snapshots --market us --commit` 로 빌드 시 **26행 커밋, ROE rows 0, PER 25, PBR 26, dividend 20**, 그리고 다수의 yahoo `Invalid Crumb / Session is closed` fetch 실패.

근본 원인은 yahoo 단일 소스 의존이다. ROE는 `app/services/brokers/yahoo/client.py::fetch_fundamental_info` 가 `yf.Ticker(...).info['returnOnEquity']` 에서 가져오는데, `.info` 는 crumb/cookie 인증이 필요해 (a) 키가 통째로 누락된 dict 를 반환하거나(→ `roe=None`, **지배적 케이스** — PER/PBR 은 있는데 ROE 만 빠짐), (b) crumb 재시도 1회 소진 후 예외를 던져 builder 가 해당 심볼 전체를 skip 한다. 결과적으로 US `high_yield_value`(roe≥15 + 0<per≤10)·`undervalued_breakout`(per/pbr/52w-date) 가 정직하게 활성화되지 못한다.

**설계 제약**: 현재의 fail-closed 동작(없으면 null/skip, 위조 금지)은 의도된 설계다. 이 설계는 그 게이트를 **유지**하되, yahoo gap 을 **Finnhub 보조 소스로 메우는** 회복 경로를 추가한다.

## 접근 (결정됨)

**valuation 테이블 한정 · field-fill · `source='yahoo'` 유지 · migration 0.**

`market_valuation_snapshots` 의 US 빌드 경로(`default_valuation_fetcher`)에서, 게이트가 켜져 있고 yahoo 가 valuation 필드를 null 로 남기거나 통째로 실패하면, Finnhub `company_basic_financials` metric 엔드포인트를 **심볼당 1회**(gap 이 있을 때만) 호출해 누락 필드만 채운다. 기존 yahoo 행을 그대로 두고 필드별 출처를 `raw_payload` 에 기록한다.

### 브레인스토밍에서 확정한 4개 결정

1. **범위** — valuation 테이블(`market_valuation_snapshots`) 한정. fundamentals 테이블(income history) fallback 은 범위 외(별도 후속). operator 가 실제 본 증상이 valuation "ROE rows 0" 이고, 그 테이블이 `high_yield_value`/`undervalued_breakout` 를 직접 먹인다.
2. **Provenance** — field-fill, `source='yahoo'` 유지, migration 없음. 필드별 출처는 `raw_payload['_field_provenance']` 에 기록. (대안 "별도 source='finnhub' 행"은 CheckConstraint widen migration + `_source_for_market` un-hardcode + 읽기/coverage 경로 source-preference 가 필요해 blast radius 가 큼 → 반려. "hybrid" 도 동일 이유로 반려.)
3. **Backfill 필드 범위** — 누락된 valuation 필드 전부(roe/per/pbr/dividend_yield/market_cap/high_52w/high_52w_date). `company_basic_financials` 한 번 호출로 전부 얻으므로 추가 비용 0. `high_yield_value`(roe)뿐 아니라 `undervalued_breakout`(per/pbr/52w-date) + quality guard(market_cap non-null 필수)까지 resilience 확보.
4. **Operator toggle** — `MARKET_VALUATION_FINNHUB_FALLBACK_ENABLED` settings/env 플래그, default `False`. `default_valuation_fetcher` 내부에서 게이트. `FINNHUB_API_KEY` 미설정 시 자동 inert. (레포 관례 `BINANCE_*_ENABLED` 등과 일치, CLI/job 스레딩 불필요.)

### 왜 field-fill / no-migration 인가

operator 의 지배적 케이스는 **yahoo `.info` 가 dict 는 주되 `returnOnEquity` 만 누락**(roe=None, per/pbr/dividend 존재)이다. 이건 기존 yahoo 행에 roe 하나만 메우면 되는 전형적 field-fill 케이스다. `source` 가 unique key 의 일부라 별도 'finnhub' 행은 중복행을 만들고 읽기경로(`latest_for_symbols` 는 source-agnostic recency 선택)에 source-preference 로직을 강제한다. field-fill 은 그 모든 것을 회피한다 — 단일 행, migration 0, 읽기/coverage/guard 무변경.

## 컴포넌트

### 1. 신규 서비스 헬퍼 — `app/services/market_valuation_snapshots/finnhub_fallback.py`

```python
async def fetch_valuation_finnhub(symbol: str) -> dict[str, Any]:
    """Finnhub company_basic_financials metric → valuation 필드 dict.
    실패(키없음/ImportError/API/rate-limit)는 호출자가 fail-closed 처리하도록 raise.
    """
```

- **클라이언트 접근**: `app/services/finnhub_news.py` 의 env-first 패턴(`os.getenv("FINNHUB_API_KEY")` → `settings.finnhub_api_key`)을 미러. `finnhub` 패키지 lazy import(try/except ImportError). **`app.mcp_server` import 안 함** (valuation builder 는 이미 `app.services.brokers.yahoo.client` 만 import — 서비스 레이어 일관성 유지).
- **엔드포인트**: `client.company_basic_financials(symbol, "all")["metric"]` (sync → `asyncio.to_thread`).
- **반환 키**: `_payload_from_raw` 가 읽는 키 모양과 동일 — `roe / per / pbr / dividend_yield / market_cap / high_52w / low_52w / high_52w_date`. 각 metric 누락/비정상 → 해당 키 생략 또는 None(위조 금지).
- **위치 근거(YAGNI)**: valuation 빌더 로컬 모듈. 재사용 가능한 `app/services/brokers/finnhub/` 승격은 범위 외(현재 소비자 1곳).

### 2. 트리거 — `app/services/market_valuation_snapshots/builder.py::default_valuation_fetcher` (US 분기만)

```python
# 의사코드 (US 분기)
gate = settings.market_valuation_finnhub_fallback_enabled and _finnhub_key_present()
yahoo_exc = None
try:
    raw = <기존 yahoo gather: fast_info + fundamental_info [+ 52w_date]>
except Exception as exc:
    raw, yahoo_exc = {}, exc          # 종전: 그대로 전파 → 심볼 skip
if gate and (yahoo_exc is not None or _has_missing_fields(raw)):
    try:
        metrics = await fetch_valuation_finnhub(symbol)   # 1회 호출
        filled = _fill_missing(raw, metrics, _TARGET_FIELDS)  # null 필드만 채움
        if filled:
            raw.setdefault("_field_provenance", {}).update({f: "finnhub" for f in filled})
    except Exception:
        pass                          # fail-closed: raw 그대로
if yahoo_exc is not None and not _row_usable(raw):
    raise yahoo_exc                   # 회복 못하면 종전대로 skip+warn 보존
return raw
```

- **플래그 OFF**: yahoo 예외가 종전대로 전파 → 동작 byte-identical. Finnhub 호출 0.
- **키 없음**: `_finnhub_key_present()` False → 게이트 통과 못함 → inert.
- **`_TARGET_FIELDS`**: roe/per/pbr/dividend_yield/market_cap/high_52w/low_52w/high_52w_date.
- **`_has_missing_fields`**: 위 필드 중 하나라도 None/부재.
- **`_row_usable`**: 종전 builder 의 "전 필드 null 이면 skip" 판정과 일치하는 최소 판정(어떤 metric 이든 non-null 이면 usable).
- 커스텀 `fetcher=` 를 주입한 호출자(테스트 등)는 `default_valuation_fetcher` 를 우회하므로 영향 없음.

### 3. 필드 매핑 + 단위 함정 (테스트로 못 박는 핵심 위험구간)

| 컬럼 | Finnhub metric 키 | 변환 | 함정 |
| -- | -- | -- | -- |
| `roe` | `roeTTM` | **없음** | Finnhub 는 이미 percent(예 22.0). yahoo 경로는 fraction×100. Finnhub 에 ×100 하면 2200% 오류 |
| `per` | `peTTM` | 없음 | (`peBasicExclExtraTTM` 폴백 고려) |
| `pbr` | `pbAnnual` | 없음 | (`pbQuarterly` 폴백 고려) |
| `dividend_yield` | `dividendYieldIndicatedAnnual` | **÷100** | Finnhub %(예 3.0) → 저장 ratio(0.03). PR #1154 가 컬럼이 ratio 임을 확정, guard `≤0.25` |
| `market_cap` | `marketCapitalization` | **×1_000_000** | Finnhub 백만 단위 → 절대 USD. guard `≥$100M` non-null 필수 |
| `high_52w` | `52WeekHigh` | 없음 | |
| `low_52w` | `52WeekLow` | 없음 | |
| `high_52w_date` | `52WeekHighDate` | `'YYYY-MM-DD'`→date | 파싱 실패 → None |

비정상/누락 → 해당 필드 null 유지(위조 금지). 값은 `_to_decimal`/`_to_date` 의 non-finite 거부를 거쳐 저장된다.

### 4. Operator toggle — `app/core/config.py`

`market_valuation_finnhub_fallback_enabled: bool = False` (env `MARKET_VALUATION_FINNHUB_FALLBACK_ENABLED`) 추가. `finnhub_api_key` (line 323, 기존) 와 함께 게이트.

### 5. Smoke 리포팅 (acceptance #5) — `builder.py` 결과 + `scripts/build_market_valuation_snapshots.py::_print_result`

빌드 결과(`MarketValuationBuildResult`)에 additive 집계 추가:
- **finnhub backfill 행 수** + **필드별 채움 수**(예: `roe: 14, high_52w_date: 9`).
- **필드별 non-null 커버리지** (빌드된 payload 에서 파생 → dry-run 에서도 동작).

`_print_result` 가 위를 출력. (latest snapshot date 는 기존 `snapshot_date_distribution` 에 이미 있음. freshness `coverage_counts()` 는 post-commit read 라 dry-run 에 안 맞아 핵심에서 제외, 후속 확장 여지만 언급.)

## 데이터 흐름

```
operator: build_market_valuation_snapshots --market us [--commit]
  └─> build_valuation_snapshots_for_market(fetcher=default)   # CLI/job 무변경
        └─> default_valuation_fetcher(symbol, "us")
              ├─ yahoo gather (fast_info + fundamental_info [+ 52w_date])
              │     └─ roe=None  (operator 지배 케이스) 또는 예외
              ├─ [gate ON & gap] fetch_valuation_finnhub(symbol)  # 1회
              │     └─ 누락 필드만 fill + _field_provenance 기록
              └─ raw (source='yahoo' 유지)
        └─> _payload_from_raw → MarketValuationSnapshotUpsert
        └─> (commit 시) repository.upsert  # source-agnostic, guard/read 무변경
```

읽기경로(`latest_for_symbols`)·quality guard(`apply_us_valuation_quality_guards`)·preset 로더 **전부 무변경**. Finnhub 가 채운 값은 source-agnostic guard(market_cap≥$100M, roe≤300%, dividend≤0.25)를 yahoo 값과 동일하게 통과해야 한다.

## 안전 경계

- **valuation US 한정**. KR(`naver_finance`) 경로 무변경. fundamentals 테이블 무변경.
- **Default-off inert**: 플래그 미설정 또는 `FINNHUB_API_KEY` 부재 시 Finnhub 호출 0, 동작 byte-identical.
- **Fail-closed 전부 보존**: finnhub 에러/키없음/rate-limit → 조용히 degrade(raw 그대로); yahoo 가 통째로 실패하고 회복 못하면 종전대로 심볼 skip+warn. 전 필드 null 행은 여전히 skip. 위조 0.
- **Migration: 0** (source 그대로 'yahoo', 신규 컬럼 없음).
- **broker/order/watch/order-intent mutation 0**. 라이브 트레이딩 동작 무변경.
- **secret 미출력**: CLI 는 키 값 출력 없음, 키 부재는 inert 처리(이름조차 강제 출력 안 함).
- **스케줄러 무변경**: TaskIQ/cron/Prefect 연결 없음. operator 가 CLI 로 호출.
- **Rate limit**: Finnhub free tier ~60/min. fallback 은 yahoo gap 에서만 발화(전 심볼 아님)하고 operator 런은 bounded. 별도 token-bucket 미도입(YAGNI v1) — rate-limit 예외는 fail-closed(필드 null). 런북/설명에 modest `--concurrency` 권고.

## 테스트 (TDD, 전부 오프라인 — 네트워크/DB 없음)

기존 패턴: `tests/test_invest_coverage_valuation.py`(fetcher 주입 + yahoo client monkeypatch), `tests/test_yahoo_roe_rob440.py`(`.info` mock, fail-closed null). 신규 파일 `tests/test_market_valuation_finnhub_fallback_rob434.py`.

### 단위 — 필드 매핑/단위 함정
- `roeTTM=22.0` → `roe=22.0` (NOT 2200). **×100 회귀 방지.**
- `marketCapitalization=1500`(백만) → `market_cap=1_500_000_000`.
- `dividendYieldIndicatedAnnual=3.0` → `dividend_yield=0.03`.
- `52WeekHighDate='2026-03-14'` → date(2026,3,14); 잘못된 문자열 → None.
- metric 키 누락 → 해당 필드 None(위조 0).

### 단위 — 합성 fetcher 트리거
- yahoo 부분(roe=None) + 플래그 ON + finnhub roe 보유 → `raw['roe']` 채워짐, `source` 여전히 'yahoo', `_field_provenance['roe']=='finnhub'`.
- yahoo 통째 실패(raise) + 플래그 ON + finnhub 전체 → 행 복구(필드 finnhub), 예외 re-raise 안 함.
- yahoo 통째 실패 + 플래그 ON + finnhub 도 실패 → 원 yahoo 예외 re-raise(심볼 skip 보존).
- 플래그 OFF → finnhub 호출 0, yahoo 예외 종전대로 전파.
- 플래그 ON + `FINNHUB_API_KEY` 없음 → inert(finnhub 호출 0).
- finnhub raise/rate-limit → fail-closed(raw 변경 없음).

### 단위 — 리포팅
- 빌드 결과의 finnhub backfill 카운터/필드별 non-null 커버리지 집계 정확성.

### 통합 (선택, `db_session`)
- 주입 fetcher 로 backfill 행 upsert → 행 존재 + `raw_payload['_field_provenance']` 보존.
- backfill 행(market_cap≥$100M, roe≤300%)이 `apply_us_valuation_quality_guards` 를 **통과**.

## 파일 참조

| 파일 | 변경 |
| -- | -- |
| `app/services/market_valuation_snapshots/finnhub_fallback.py` | **신규** — `fetch_valuation_finnhub` + 필드매핑/단위변환 |
| `app/services/market_valuation_snapshots/builder.py` | `default_valuation_fetcher` US 분기 트리거 + 결과 집계 |
| `app/core/config.py` | `market_valuation_finnhub_fallback_enabled` (1개) |
| `scripts/build_market_valuation_snapshots.py` | `_print_result` finnhub backfill/커버리지 출력 |
| `tests/test_market_valuation_finnhub_fallback_rob434.py` | **신규** 테스트 |

## Rollback

PR revert(코드만). migration 0, 데이터 변환 없음. 플래그 default-off 라 머지 후에도 operator 가 켜기 전까지 inert.

## 범위 외 / 후속

- fundamentals 테이블(`financial_fundamentals_snapshots`) Finnhub fallback(income statements, growth/dividend presets) — 별도 후속. `_fetch_financials_finnhub` reports[]→date-keyed 어댑터 + XBRL 라벨 alias + 4-report cap 해제 필요.
- 별도 `source='finnhub'` provenance 행(migration + 읽기경로 source-preference) — 반려, 후속 여지.
- operator US valuation 재빌드/backfill 실행 + 플래그 flip — 이 PR 밖(operator-gated).
- Finnhub rate-limit token-bucket / 재사용 가능 `brokers/finnhub/` 승격 — YAGNI, 후속.
