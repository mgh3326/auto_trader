# screener_bakeoff — 스크리너 소스 베이크오프 (§140차)

**read-only 연구.** 이 패키지는 SELECT 만 한다. 주문·워치·제안·정책 mutation 0,
브로커 호출 0, 애플리케이션 테이블 쓰기 0. 산출물은 `artifacts/` 의 CSV/JSON/MD 뿐이다.

**이 결과는 정책 사용 전 별도 검증 전제다.** 즉시 정책·가중치 변경 근거로 쓰지 말 것.

---

## §0 사전등록 동결 기록

`spec.py` 의 상수는 **채점 결과를 보기 전에** 고정했다. 결과를 본 뒤 상수를 바꾸면
그것은 새 실험이며 `EXPERIMENT_ID` 가 바뀌어야 한다.

동결 후 결과 열람 **전에** 이루어진 수정은 아래가 전부다:

| id | 수정 | 사유 |
|---|---|---|
| A1 | 유동성 정의 = `daily_turnover` → `daily_volume × latest_close` | `invest_screener_snapshots.daily_turnover` 가 2026-07-21 이전 전 파티션에서 NULL. 두 값 모두 결정일에 박제된 컬럼이라 look-ahead 없음 |
| A2 | RSI 재구성 시드 버그 수정 | `close.diff()` 의 선두 NaN 이 프로덕션에서 0.0 으로 치환되어 ewm 시드가 되는데, 초판이 그 0 을 버려 프로덕션과 값이 달랐다. `tests/research/test_screener_bakeoff_contract.py::test_rsi_matches_production` 로 고정 |
| A3 | 무작위 대조군 시드를 `hash()` → `hashlib.sha256` | CPython 문자열 해시는 프로세스마다 랜덤이라 "시드 고정"이 거짓이었다. 지금은 재실행 시 `picks.csv` 가 바이트 동일 |

---

## §1 무엇을 비교했나

| 계열 | 소스 | 입력 |
|---|---|---|
| snapshot preset (KR 17종) | 연속상승·거래량급증·등락률 상하위·거래대금·수급모멘텀·쌍끌이·과매도(RSI≤30)·가치/성장/배당 9종 | `invest_screener_snapshots`, `invest_kr_fundamentals_snapshots`, `investor_flow_snapshots` — **전부 결정일에 프로덕션 라이터가 박제한 행** |
| snapshot preset (US 9종) | 연속상승·거래량급증·등락률 상하위·거래대금·가치 4종 | `invest_screener_snapshots`, `market_valuation_snapshots(us,tvscreener)` |
| snapshot preset (crypto 8종) | 거래대금·과매도·모멘텀·펀딩 2종·OI·롱숏쏠림·RSI≤45 | `invest_crypto_screener_snapshots` |
| reconstructed | **현행 주력** `tv_rsi45` = 유동성 필터 + RSI-14 ≤ 45 오름차순 | `kr/us_candles_1d` 의 **결정일 이하 봉만** |
| control | 무작위 10종 (시드 고정) + 부트스트랩 null 2000회 | 동일 유동성 유니버스 |
| benchmark | 유동성 유니버스 등가중 | 동일 |

각 소스는 결정일마다 상위 **N=10** 을 낸다(게이트 변형은 상위 100 풀을 게이트에 통과시킨 뒤 상위 10).

## §2 채점

* 진입가 = 결정일 종가(스냅샷 자신의 `latest_close`).
* 성과 = D+5 / D+20 (KR·US 는 거래일, crypto 는 스냅샷 일자).
* **초과수익 = 픽 수익 − 같은 날 같은 시장의 등가중 벤치마크 수익** (페어링). 표본 기간의
  KR 시장이 크게 하락했기 때문에 절대수익은 소스 판별력이 아니라 시장 방향을 재는 값이다.
  **판정은 초과수익으로만 한다.**
* 집계는 **날짜 단위**로 먼저 접는다(같은 날 10픽은 독립 표본이 아니다).

## §3 look-ahead 통제

* 스냅샷 소스는 결정일 파티션 행만 읽는다. 그 행은 프로덕션이 그날 쓴 것이다.
* 재구성 소스·게이트는 `PricePanel.window()` 로 **결정일 이하 봉만** 자른다.
* `tests/research/test_screener_bakeoff_contract.py` 가 (a) 창이 결정일 다음 봉을 절대
  포함하지 않음, (b) 미래 봉을 잘라내도 게이트 판정이 불변, (c) 채점이 진입봉을 MFE/MAE 에
  넣지 않고 horizon 밖 봉에 반응하지 않음을 기계 검증한다.
* 같은 파일이 RSI·볼린저·피보나치·클러스터링·지지강도를 **프로덕션 함수와 동일값**으로 고정한다
  (`market_data_indicators`, `analysis_quick._build_support_resistance`).

## §4 알려진 한계 (보고서에 그대로 실린다)

1. **upside(≥40%) 게이트는 중화됐다.** 시점별 애널리스트 컨센서스 이력이 이 DB 에 없다.
   따라서 모든 게이트 통과 수치는 **상한**이다.
2. **창 중첩.** 결정일이 매일이라 D+20 표본은 이웃끼리 19일을 공유한다. t 통계는 **서술용**이며
   유의성 판정은 부트스트랩 null 백분위로 한다(그것도 횡단면 선택만 통제, 시계열 중첩은 미통제).
3. **표본 기간이 짧고 한 국면이다.** KR 47/32 결정일, US 42/27, crypto 71/56. 기간 대부분이
   KR 대폭 하락 국면 — 가치·역발상 유리, 모멘텀 불리 쪽으로 편향될 수 있다.
4. **생존편향.** 픽의 forward 봉이 끊기면 `truncated` 로 표시하고, 창 끝 검열(`censored`)과
   실제 상폐/거래정지/커버리지 구멍을 분리해 센다. 헤드라인은 **미검열 행만** 쓴다.
5. **소스별 커버리지 불균등.** `kr.oversold_recovery` 는 픽의 31.8%, `kr.stable_growth` 는
   30.0% 가 `kr_candles_1d` 에 봉이 없어 채점 불능이다(전자는 ETN/ELW 계열 코드 혼입,
   후자는 3개 고정 종목의 봉 부재). 이 두 소스 수치는 나머지와 동급으로 읽으면 안 된다.
6. **프로덕션 프리셋과의 문언 차이**는 `spec.py` 의 각 `SourceSpec.caveats` 에 종목별로 적었다
   (예: 3년평균 성장률 조건이 이 테이블엔 없어 1년 YoY 프록시로 대체, 연속증가 스트릭 조건 생략 등).

## §5 재현

```bash
export DATABASE_URL=postgresql://.../auto_trader     # read-only 계정 권장
uv run python -m research.screener_bakeoff.run_bakeoff
uv run python -m research.screener_bakeoff.aggregate
uv run python -m research.screener_bakeoff.bootstrap
uv run python -m research.screener_bakeoff.report
uv run pytest tests/research/test_screener_bakeoff_contract.py
```

산출: `artifacts/picks.csv`, `picks_scored.csv`, `scorecard.csv`,
`scorecard_datelevel.csv`, `bootstrap_null.csv`, `universe_returns.csv`,
`report_tables.md`, `run_meta.json`.
